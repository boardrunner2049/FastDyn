"""Measure predeclared incremental transformations on the Pye package edit."""
import argparse
import json
import os
from pathlib import Path
import random
import statistics
import sys
import time

from observe import observe
from run_campaign import HERE, ROOT, command, optimizer_bindings, pin, prepare_image, read, require, write_json
from candidates import deployment_identity, materialize, prepare_image as validate_image
from scopt.importer.import_cache import import_snapshot, open_snapshot
from scopt.report.collect import hotset_metrics
from scopt.run.deploy import resolve_artifact
from scopt.run.launcher import RunDir
from scopt.run.proof import frozen_append_counts


def orders(names, rng, blocks):
    rows = []
    for _ in range(blocks // 2):
        order = list(names)
        rng.shuffle(order)
        rows.extend((order, order[::-1]))
    rng.shuffle(rows)
    return rows


def trial(work, out, image, row, case, *, diagnostic=False):
    dest = out / "launch"
    out.mkdir()
    elf = Path(image["firmware"])
    command(out, "launch", [sys.executable, HERE / "fastdyn_runner.py", "frozen",
            "--work", work / "product", "--run", Path(image["config"]).parent, "--qemu", image["runtime"],
            "--elf", elf, "--plugin", work / "plugin-release/libfastdyn.so", "--case", case,
            "--snapshot", row["artifact"], "--out", dest, *(["--v6-stats"] if diagnostic else [])], timeout=150)
    doc = read(dest / "run.json")
    app = observe(dest / "run.json", elf, training_frozen=True)
    counts = frozen_append_counts(doc)
    times = doc["timings"]
    rd = RunDir.open(dest)
    spans = [json.loads(line) for line in (rd.work / "fastdyn_timing.jsonl").read_text().splitlines()]
    seconds, = [s["duration_s"] for s in spans if s["phase"] == "machine0.qemu_execution"
                and s["event"] == "phase_end" and s["status"] == "ok"]
    result = {"operational_ns": app["operational_elapsed_ns"], "qemu_ns": round(seconds * 1e9),
              "process_cpu_ns": round((times["children_user_s"] + times["children_sys_s"]) * 1e9),
              "wall_s": times["wall_s"], "oracle": app["observations"], "proof": pin(dest / "run.json"), **counts}
    rd.cache.unlink()
    (rd.ram / "psram.ram").unlink(missing_ok=True)
    return result


def compare(blocks, control, target, rng):
    summary = {}
    for field in ("operational_ns", "qemu_ns", "process_cpu_ns", "wall_s"):
        a = [block[control][field] for block in blocks]
        b = [block[target][field] for block in blocks]
        gains = [x/y for x, y in zip(a, b)]
        boot = sorted(statistics.median(rng.choices(gains, k=len(gains))) for _ in range(2000))
        summary[field] = {"reference_median": statistics.median(a), "candidate_median": statistics.median(b),
                          "median_paired_gain": statistics.median(gains), "bootstrap_95": [boot[49], boot[1949]]}
    return summary


def integrated_generation(stage, parent, image, dest):
    ordinary = Path(parent["snapshot"])
    snapshot = open_snapshot(ordinary)
    profile = Path(image["profile"])
    if stage == "compact-nops":
        from scopt.commands.compact_nops import generate
        receipt = generate(ordinary, profile, dest)
        imported = import_snapshot(sc=str(dest / "cache.sc"), scms=sorted(dest.glob("cache*.scm")),
                                   manifest=image["manifest"], bundle=snapshot.bundle, runtime_identity=snapshot.runtime,
                                   store=dest / "reimport")
        require(imported.snapshot.snapshot_id == receipt["output_snapshot"], "compacted snapshot identity differs")
        return {"snapshot": str(imported.store_dir), "snapshot_id": imported.snapshot.snapshot_id,
                "artifact": str(imported.store_dir), "generations": [receipt]}
    from run_campaign import LIB
    command(dest.parent, "build-continuity", [sys.executable, LIB / "package_continuity.py", ordinary, profile,
            "--guest-elf", image["firmware"], "--out", dest], timeout=240)
    receipt = read(dest / "continuity.json")
    require(receipt["snapshot"] == snapshot.snapshot_id and receipt["profile"] == read(profile)["profile_id"],
            "direct-slot emitter used a different source snapshot or profile")
    return {"artifact": str(dest), "generations": [receipt]}


def run(work, out, group, policy_path):
    policy = read(policy_path)
    group_plan = policy["groups"][group]
    case = policy["case"]
    require(case == "update-package-version" and policy["role"] == "training", "requires Pye training")
    out.mkdir()
    elf = work / "product/micropython/ports/qemu/build-MPS2_AN385/firmware.elf"
    qemu = ROOT / "third_party/common/qemu/build/qemu-system-arm"
    plugin = work / "plugin-release/libfastdyn.so"
    os.environ.update(FASTDYN_ROOT=str(work / "scopt"), SCOPT_FASTDYN_SRC=str(ROOT))
    write_json(out / "policy.json", policy)
    report = {"state": "running", "group": group, "policy": pin(policy_path), "firmware": pin(elf),
              "qemu": pin(qemu), "plugin": pin(plugin), "affinity": sorted(os.sched_getaffinity(0)),
              "arms": {}, "contrasts": {}}
    require(len(report["affinity"]) == 1, "timing requires one fixed CPU")
    keys = policy["preparation"]
    config = {"scenarios": {"training": [case]}, "prechange_scenarios": {}, "cache_track": "training-frozen",
              "track": policy["track"], "inventory_sweeps": keys["sweeps"], "stable_sweeps": keys["stable_sweeps"],
              "timeout_seconds": 120}
    def checkpoint():
        write_json(out / "report.json", report)
    try:
        optimizer_bindings(out, qemu)
        image = prepare_image(work / "product", out / "ordinary", elf, qemu, plugin, config)
        snap, profile = validate_image(image)
        report.update(image=image, profile_id=profile.profile_id, ordinary_snapshot_id=snap.snapshot_id)
        warm = {"snapshot": image["snapshot"], "snapshot_id": snap.snapshot_id,
                "artifact": image["snapshot"], "ancestry": [], "generations": []}
        baseline = time.monotonic()
        for spec in [{"name": "warm"}, *group_plan["arms"]]:
            name = spec["name"]
            arm = {"state": "building", "spec": spec}
            report["arms"][name] = arm
            start = time.monotonic()
            try:
                result = warm
                if name != "warm":
                    parent = report["arms"][spec["parent"]]
                    require(parent["state"] == "materialized", "parent unavailable")
                    budget = group_plan.get("offline_seconds", policy["offline_seconds_per_group"]) - (start - baseline)
                    require(budget > 0, "group offline budget exhausted")
                    if group == "integrated":
                        result = integrated_generation(spec["stage"], parent["generation"], image, out / name)
                    else:
                        result = materialize(spec["stage"], parent["generation"], image, out / name, spec["params"],
                                             min(budget, policy["per_stage_limit_seconds"]))
                arm["generation"] = result
                artifact = resolve_artifact(result["artifact"])
                arm["identity"] = deployment_identity(result["artifact"], image["config"], elf, qemu,
                                                      report["policy"]["sha256"])
                arm["public_hot_footprint"] = hotset_metrics(profile.doc, artifact["sc"])
                if group == "native" and name != "warm":
                    linked = open_snapshot(result["snapshot"])
                    arm["native_fragments"] = [{"unit": u.uid, "certificate": r.to_json()} for u in linked.units()
                                               for r in u.native_fragments]
                if spec.get("stage") == "direct-slot":
                    diagnostic = out / "continuity-activation"
                    trial(work, diagnostic, image, result, case, diagnostic=True)
                    counters = read(diagnostic / "launch/v6-stats.json")
                    arm["untimed_stats"] = counters
                    require(counters["loaded"] and counters["n_variants"] == 1 and counters["links_armed"] > 0
                            and counters["variant_exits"] > 0 and counters["invariant_violations"] == 0,
                            "untimed replay did not demonstrate the private transfer")
                arm["state"] = "materialized"
            except Exception as exc:
                arm.update(state="refused", error=str(exc))
            finally:
                arm["materialization_s"] = time.monotonic() - start
                checkpoint()
        by_identity = {}
        aliases = {}
        for name, arm in report["arms"].items():
            if arm["state"] == "materialized":
                aliases[name] = by_identity.setdefault(arm["identity"], name)
        require("warm" in aliases, "missing ordinary control")
        measured = list(by_identity.values())
        timing = policy["timing"]
        rng = random.Random(timing["seed"])
        schedule = orders(measured, rng, timing["blocks"])
        write_json(out / "orders.json", {"aliases": aliases, "warmup": measured, "scored": schedule})
        blocks = []
        failed = set()
        with (out / "observations.jsonl").open("x") as log:
            for index, order in enumerate([measured, *schedule], -1):
                block = {}
                for name in order:
                    if name in failed:
                        continue
                    arm = report["arms"][name]
                    record = {"block": index, "arm": name, "identity": arm["identity"]}
                    try:
                        block[name] = trial(work, out / f"trial-{index}-{name}", image, arm["generation"], case)
                        record["measurement"] = block[name]
                    except Exception as exc:
                        failed.add(name)
                        record["error"] = str(exc)
                        arm["launch_error"] = str(exc)
                    finally:
                        print(json.dumps(record), file=log, flush=True)
                    require("warm" not in failed, "ordinary control failed")
                if index >= 0:
                    blocks.append(block)
        for name in measured:
            arm = report["arms"][name]
            require(deployment_identity(arm["generation"]["artifact"], image["config"], elf, qemu,
                                        report["policy"]["sha256"]) == arm["identity"], "source artifact changed")
        for control, candidate in group_plan["contrasts"]:
            a, b = aliases.get(control), aliases.get(candidate)
            if a is None or b is None or a in failed or b in failed:
                comparison = {"state": "unavailable"}
            elif a == b:
                comparison = {"state": "identical_deployment"}
            else:
                comparison = {"state": "complete", "blocks": len(blocks), **compare(blocks, a, b, rng)}
            report["contrasts"][f"{control}/{candidate}"] = comparison
        report["state"] = "completed"
    except BaseException as exc:
        report.update(state="failed", error=str(exc))
        raise
    finally:
        checkpoint()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("group")
    p.add_argument("work", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--policy", type=Path, default=HERE / "training-contrasts.json")
    args = p.parse_args()
    run(args.work.resolve(), args.out.resolve(), args.group, args.policy.resolve())
