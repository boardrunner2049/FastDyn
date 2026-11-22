"""Training-only Pye validation of a B4 generation and guarded UDIV splicing."""
import argparse
import os
from pathlib import Path
import sys
import time

from run_campaign import HERE, ROOT, command, optimizer_bindings, pin, prepare_image, read, write_json
from candidates import materialize, prepare_image as verify_training_domain
from scopt.importer.import_cache import open_snapshot


def qualify(work, out):
    out.mkdir()
    product = work / "product"
    case = "update-package-version"
    elf = product / "micropython/ports/qemu/build-MPS2_AN385/firmware.elf"
    qemu = ROOT / "third_party/common/qemu/build/qemu-system-arm"
    plugin = work / "plugin-release/libfastdyn.so"
    os.environ.update(FASTDYN_ROOT=str(work / "scopt"), SCOPT_FASTDYN_SRC=str(ROOT))
    config = {"scenarios": {"training": [case]}, "prechange_scenarios": {},
              "track": "training-only known inventory with strict replay", "inventory_sweeps": 8,
              "stable_sweeps": 2, "timeout_seconds": 120}
    recipes = {"b4": {}, "native": {"host-inline": {"mode": "splice", "allow_faulting": True,
                                                      "udiv_mode": "guarded"}}}
    write_json(out / "plan.json", {"case": case, "firmware": pin(elf), "qemu": pin(qemu),
                "bridge": pin(os.environ["SCOPT_HOST_INLINE_BRIDGE"]), "config": config, "recipes": recipes,
                "per_recipe_offline_seconds": 1200, "replays_per_recipe": 1, "target_guest_pc": "0xe8fe",
                "scope": "functional training replay only"})
    optimizer_bindings(out, qemu)
    ordinary = out / "ordinary"
    report = {"state": "running", "stages": {}}
    try:
        try:
            image = prepare_image(product, ordinary, elf, qemu, plugin, config)
        except ValueError as exc:
            # A warm launch can append even after the proof-checked instrumented
            # capture. Preserve the failure and try independent qualified outputs.
            if not (ordinary / "profile/merged.profile.json").is_file():
                raise
            snapshot, = (ordinary / "store/snapshots").iterdir()
            image = dict(firmware=str(elf), config=str(ordinary / "pye.toml"), runtime=str(qemu),
                         snapshot=str(snapshot), prep=str(ordinary / "prep"), profile=str(ordinary / "profile/merged.profile.json"),
                         manifest=str(ordinary / "prep/cache/fw.sc.fastdyn.json"),
                         admission_runtime=str(ordinary / "admission-runtime.json"))
            report["ordinary_error"] = str(exc)
        snap, profile = verify_training_domain(image)
        report.update(snapshot_id=snap.snapshot_id, profile_id=profile.profile_id)
        parent = {"snapshot": image["snapshot"], "snapshot_id": snap.snapshot_id, "artifact": image["snapshot"],
                  "ancestry": [], "generations": []}
        runner = [sys.executable, HERE / "fastdyn_runner.py", "warm", "--work", product, "--run", ordinary,
                  "--qemu", qemu, "--elf", elf, "--plugin", plugin, "--case", case]
        for stage, params in recipes.items():
            start = time.monotonic()
            stage_receipt = {"state": "running", "params": params}
            report["stages"][stage] = stage_receipt
            write_json(out / "report.json", report)
            try:
                result = materialize(stage, parent, image, out / stage, params, 1200)
                stage_receipt.update(offline_s=time.monotonic() - start, generation=result)
                linked = open_snapshot(result["snapshot"])
                if stage == "native":
                    fragments = [{"unit": u.uid, "metadata": u.meta_status, "certificate": req.to_json()}
                                 for u in linked.units() for req in u.native_fragments]
                    write_json(out / "native-certificates.json", fragments)
                    udiv = [r["certificate"] for r in fragments if r["certificate"]["symbol"] == "helper_udiv"]
                    stage_receipt["udiv_sites"] = len(udiv)
                    stage_receipt["observed_target_pc_in_certificate"] = any(0xe8fe in r["guest_pcs"] for r in udiv)
                if result["snapshot_id"] == snap.snapshot_id:
                    stage_receipt["state"] = "no_change"
                    continue
                trial_start = time.monotonic()
                try:
                    command(out, "replay-" + stage, [*runner, "--snapshot", result["artifact"],
                                                    "--out", out / ("trial-" + stage)], timeout=150)
                finally:
                    stage_receipt["replay_s"] = time.monotonic() - trial_start
                stage_receipt["state"] = "validated"
            except Exception as exc:
                stage_receipt.update(state="failed", error=str(exc))
            finally:
                stage_receipt["stage_wall_s"] = time.monotonic() - start
                write_json(out / "report.json", report)
        report["state"] = "completed" if all(s["state"] == "validated" for s in report["stages"].values()) else "completed_with_refusals"
    except Exception as exc:
        report.update(state="failed", error=str(exc))
        raise
    finally:
        write_json(out / "report.json", report)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("out", type=Path)
    args = p.parse_args()
    qualify(args.work.resolve(), args.out.resolve())
