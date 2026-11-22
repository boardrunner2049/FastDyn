"""Count host paths on separately deployed Pye training replays."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from run_campaign import HERE, ROOT, optimizer_bindings, pin, prepare_image, read, require, write_json
from application_peer import uart_peer
from candidates import materialize, prepare_image as validate_image
from observe import observe
from scopt.formats.sc_v5 import ScRecord
from scopt.guard import decode_guard
from scopt.importer.import_cache import open_snapshot
from scopt.run.deploy import deploy
from scopt.run.launcher import create_run_dir, load_inputs, reset_ram, scrub_env
from scopt.run.proof import frozen_append_counts, prove_run
from scopt.validate_sym.native_udiv import verify_udiv_body

CASE = "update-package-version"
TIMEOUT = 600


def point(unit, offset, prefix_len):
    return {"offset": offset, "expected_hex": unit.code[offset:offset + prefix_len].hex()}


def plans(stage, snapshot, profile):
    """Only static, independently decoded path locations from the reimported candidate."""
    counts = {u["unit"]: u["phase"]["1"]["entries"] for u in profile["units"] if u["status"] == "exact"}
    rows = []
    for u in snapshot.units():
        pc = u.vaddrs[0] if u.vaddrs else None
        if pc is None:
            continue
        base = snapshot.header.code_address + u.tc_offset
        common = {"unit": u.uid, "base": base, "guest_pc": hex(pc), "training_entries": counts.get(u.uid, 0)}
        if stage == "native":
            for req in u.native_fragments:
                if req.symbol != "helper_udiv":
                    continue
                verify_udiv_body(req)
                splice = req.splice
                start = splice.physical_start
                code = splice.code
                require(u.code[start:start + len(code)] == code and code[:3] == b"\x85\xd2\x74",
                        "guarded UDIV certificate differs from imported bytes")
                fallback = start + 4 + int.from_bytes(code[3:4], "little", signed=True)
                require(fallback == start + len(code) - len(req.call_bytes)
                        and u.code[fallback:fallback + 6] == req.call_bytes, "fallback is not the certified CALL")
                rows.append({**common, "kind": "udiv", "points": {"entry": point(u, start, 4),
                    "fast": point(u, start + 4, 2), "fallback": point(u, fallback, 2)}})
        elif u.stub_start is not None:
            rec = ScRecord.from_bytes(u.record)
            registered = {n: (field, rec.jmp_reset_offset[n]) for n, field in enumerate(rec.jmp_insn_offset)
                          if field != 0xffff}
            start = u.stub_start
            while start < u.tc_size:
                guard = decode_guard(u.code, start, tc_abs=base, struct_off=u.tb_offset - u.tc_offset,
                                     registered=registered)
                if len(guard.slots) == 1 and guard.slots[0].registered:
                    target, = guard.slots
                    rows.append({**common, "kind": target.kind, "target_pc": hex(target.vaddr),
                                 "points": {"entry": point(u, start, 3), "match": point(u, target.jmp_off, 1),
                                            "reset": point(u, target.reset_off, 1), "fallback": point(u, guard.f_off, 3)}})
                start = guard.end
    rows.sort(key=lambda r: (-r["training_entries"], r["unit"]))
    if stage != "native":
        return [next(r for r in rows if r["kind"] == k and r["training_entries"] > 0)
                for k in ("GENERAL", "SPEC")]
    require(rows, "no certified guarded UDIV sites")
    return rows


def summarize(plan, counts):
    require(all(type(v) is int and v >= 0 for v in counts.values()) and set(counts) == set(plan["points"]),
            "missing physical breakpoint counts")
    require(counts["entry"] > 0, "unobserved source has no positive guard coverage")
    if plan["kind"] == "udiv":
        require(counts["fast"] + counts["fallback"] == counts["entry"], "UDIV paths do not conserve")
        return {"nonzero_inline": counts["fast"], "zero_helper_calls": counts["fallback"]}
    matched, reset = counts["match"], counts["reset"]
    missed = counts["entry"] - matched
    require(0 <= reset <= matched <= counts["entry"], "guard entry and slot counts disagree")
    require(counts["fallback"] == missed + (reset if plan["kind"] == "SPEC" else 0),
            "observed fallback disagrees with the decoded guard")
    return {"predicate_matched": matched, "predicate_missed": missed, "matched_unlinked": reset,
            "matched_linked": matched - reset, "lookup_fallback": counts["fallback"]}


def replay(out, image, artifact, plan):
    rd = create_run_dir(image["config"], out, qemu=image["runtime"], label="Untimed Pye path counts")
    identity = deploy(artifact, rd)["identity"]
    require(identity["checked"] and not identity["reasons"], "unchecked deployment")
    argv = read(rd.root / "qemu_argv.json")
    require(Path(argv["argv"][0]).resolve() == Path(image["runtime"]).resolve(), "QEMU differs")
    env, _ = scrub_env(extra=argv["env_set"])
    for name in argv["env_unset"]:
        env.pop(name, None)
    stage_path = Path(image["config"]).parent / "cases" / CASE / "stage.json"
    stage = read(stage_path)
    bank = Path(stage["romfs"])
    spec = {"program": pin(HERE / "session.py"), "stage": pin(stage_path), "input_sha256": pin(bank)["sha256"],
            "bank": "psram"}
    reset_ram(rd)
    inputs = load_inputs(rd, {"psram": bank})
    write_json(out / "points.json", plan)
    env["PYE_PATH_COUNTER_PLAN"] = str(out / "points.json")
    cmd = ["setarch", "x86_64", "-R", "timeout", "--kill-after=10", str(TIMEOUT),
           "gdb", "-q", "-nx", "-batch", "-x", str(HERE / "path_counts.gdb"), "--args", *argv["argv"]]
    start = time.monotonic()
    with uart_peer(spec)(rd, {"psram": bank}, "run.json", TIMEOUT + 20) as collect:
        with (out / "debugger.log").open("w") as log:
            proc = subprocess.run(cmd, cwd=argv["cwd"], env=env, stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=subprocess.STDOUT, timeout=TIMEOUT + 20)
        require(proc.returncode == 0, f"debugger exited {proc.returncode}")
        observed = read(out / "counts.json")
        require(observed["exit_code"] == 0 and not observed["errors"], "inferior or breakpoint validation failed")
        require(set(observed["checked"]) == {k for k, v in observed["counts"].items() if v},
                "an executed breakpoint lacks live byte verification")
        proof = prove_run(rd)
        result = {"verdict": proof["verdict"], "exit_code": observed["exit_code"], "timed_out": False,
                  "appended": proof["appended"], "proof": proof, "inputs": inputs}
        result["application_peer"] = collect(result)
    write_json(out / "run.json", result)
    oracle = observe(out / "run.json", Path(image["firmware"]), training_frozen=True)
    return {"state": "observed", "counts": observed["counts"], "paths": summarize(plan, observed["counts"]),
            "checked_byte_prefixes": observed["checked"], "training_oracle": oracle["observations"],
            "debugger_wall_seconds": time.monotonic() - start, **frozen_append_counts(result)}


def run(work, out):
    out.mkdir()
    qemu = ROOT / "third_party/common/qemu/build/qemu-system-arm"
    elf = work / "product/micropython/ports/qemu/build-MPS2_AN385/firmware.elf"
    plugin = work / "plugin-release/libfastdyn.so"
    require(shutil.which("gdb"), "GNU gdb is required for spawned QEMU hardware breakpoints")
    os.environ.update(FASTDYN_ROOT=str(work / "scopt"), SCOPT_FASTDYN_SRC=str(ROOT))
    config = {"scenarios": {"training": [CASE]}, "prechange_scenarios": {}, "cache_track": "training-frozen",
              "track": "untimed host path counts", "inventory_sweeps": 2, "stable_sweeps": 0, "timeout_seconds": 120}
    report = {"case": CASE, "role": "training-only diagnostic", "qemu": pin(qemu), "elf": pin(elf), "stages": {}}
    optimizer_bindings(out, qemu)
    image = prepare_image(work / "product", out / "ordinary", elf, qemu, plugin, config)
    snap, prof = validate_image(image)
    parent = {"snapshot": image["snapshot"], "snapshot_id": snap.snapshot_id, "artifact": image["snapshot"],
              "ancestry": [], "generations": []}
    for stage, options in (("native", {"host-inline": {"mode": "splice", "allow_faulting": True,
                                                        "udiv_mode": "guarded"}}), ("b3", {})):
        receipts = report["stages"][stage] = {"rows": []}
        try:
            gen = materialize(stage, parent, image, out / stage, options, 1200)
            receipts.update(input_snapshot=snap.snapshot_id, snapshot=gen["snapshot_id"], profile=prof.profile_id)
            for k, plan in enumerate(plans(stage, open_snapshot(gen["snapshot"]), prof.doc)):
                row = {"plan": plan}
                receipts["rows"].append(row)
                dest = out / f"observe-{stage}-{k}"
                try:
                    row.update(replay(dest, image, gen["artifact"], plan))
                except Exception as exc:
                    row.update(state="refused", error=str(exc))
                finally:
                    write_json(out / "report.json", report)
                    if (dest / "run.json").is_file():
                        (dest / "cache/fw.sc").unlink(missing_ok=True)
                        (dest / "ram/psram.ram").unlink(missing_ok=True)
        except Exception as exc:
            receipts["error"] = str(exc)
        write_json(out / "report.json", report)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("out", type=Path)
    args = p.parse_args()
    run(args.work.resolve(), args.out.resolve())
