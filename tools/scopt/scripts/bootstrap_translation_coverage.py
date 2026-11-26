#!/usr/bin/env python3
"""Build and qualify the bounded Cortex-M3 pretranslation coverage fixture."""

from __future__ import annotations

import argparse
import json
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
SCOPT = ROOT / "tools/scopt"
FIXTURE = SCOPT / "fixtures/translation-coverage"
HOST_FIXTURE = SCOPT / "fixtures/host-inline"
PATCH = SCOPT / "patches/qemu-fixture-pretranslate.patch"
REVOCATION_PATCH = SCOPT / "patches/qemu-coverage-revocation.patch"


def command(*args, cwd=ROOT, env=None):
    subprocess.run(args, cwd=cwd, env=env, check=True)


def cli(*args, env=None):
    command(sys.executable, "-m", "scopt", *args, env=env)


def build_probe_qemu(source: Path, target: Path, *, revocation: bool = False) -> Path:
    revision = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"],
                                       text=True).strip()
    pinned = subprocess.check_output(
        ["git", "-C", ROOT, "rev-parse", "HEAD:third_party/common/qemu"], text=True).strip()
    if revision != pinned or not (source / "build/qemu-system-arm").is_file():
        raise ValueError(f"producer requires arm-softmmu built at FastDyn's QEMU pin {pinned}")
    command("git", "diff", "--quiet", "HEAD", "--", "accel/tcg/cpu-exec.c",
            "tcg/supercache.c", "include/tcg/supercache.h", cwd=source)
    shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    command("patch", "--forward", "--batch", "--fuzz=0", "-p1", "-i", PATCH, cwd=target)
    outputs = ["libsystem.a.p/accel_tcg_cpu-exec.c.o"]
    if revocation:
        command("patch", "--forward", "--batch", "--fuzz=0", "-p1", "-i", REVOCATION_PATCH, cwd=target)
        outputs.append("libsystem.a.p/tcg_supercache.c.o")
    build = target / "build"
    # Recompile the modified sources and relink with the existing flags. This retains
    # debug info and avoids touching the normal FastDyn QEMU binary.
    commands = json.loads(subprocess.check_output(
        ["ninja", "-t", "compdb", "-x", "c_COMPILER", "c_LINKER_RSP"], cwd=build))
    for output in (*outputs, "qemu-system-arm"):
        matches = [row["command"] for row in commands if row["output"] == output]
        if len(matches) != 1:
            raise ValueError(f"build has no unique command to produce {output}")
        argv = [arg.replace(f"{source}/", f"{target}/") for arg in shlex.split(matches[0])]
        command(*argv, cwd=build)
    return build / "qemu-system-arm"


def make_firmware(work: Path, plugin: Path) -> tuple[Path, dict[str, int]]:
    obj, elf = work / "guest.o", work / "guest.elf"
    command("arm-none-eabi-as", "-mcpu=cortex-m3", "-mthumb", "-o", obj, FIXTURE / "guest.S")
    command("arm-none-eabi-ld", "-T", HOST_FIXTURE / "guest.ld", "-o", elf, obj)
    symbols = {name: int(addr, 16) for addr, _, name in map(
        str.split, subprocess.check_output(["arm-none-eabi-nm", "-n", elf], text=True).splitlines())}
    cfg = (HOST_FIXTURE / "fastdyn.toml.in").read_text()
    values = {"FIRMWARE": str(elf), "PLUGIN": str(plugin),
              **{key.upper(): hex(symbols[key]) for key in
                 ("benchmark_start", "print_result", "benchmark_end")}}
    for key, value in values.items():
        cfg = cfg.replace(f"@{key}@", json.dumps(value))
    config = work / "coverage.toml"
    config.write_text(cfg)
    return config, symbols


def check_reachability(snapshot, selection, coverage: dict, profile: dict) -> dict:
    from scopt.analysis.program import build_program

    program = build_program(snapshot)
    covered, roots = frozenset(selection.covered_units), frozenset(selection.roots)
    reachable = program.reachable(roots, covered)
    handler_vaddrs = set(coverage["entry"]["exception_vector_vaddrs"])
    handlers = [unit for unit in program.units if handler_vaddrs.intersection(unit.vaddrs)]
    profiled = {row["unit"] for row in profile["units"]
                if any(phase["entries"] for phase in row["phase"].values())}
    unresolved = [edge for edge in program.edges if edge.source in covered
                  and edge.kind == "goto_ptr" and edge.dynamic and edge.target_vaddr is None]
    if (not handlers or any(u.uid not in roots or u.uid not in reachable or u.uid in profiled
                           for u in handlers)
            or not unresolved or covered - reachable):
        raise ValueError("expected a cold exception root and unresolved indirect coverage edges")
    live_context = coverage["context"]
    if any((u.key[2], u.key[3]) == (live_context["flags"], live_context["cs_base"])
           for u in handlers):
        raise ValueError("handler capture did not retain a distinct translation context")
    return {"schema": "scopt.translation_coverage_reachability", "version": 1,
            "coverage_id": selection.coverage_id, "snapshot_id": snapshot.snapshot_id,
            "profile_id": profile["profile_id"], "reachable_units": sorted(reachable),
            "covered_units": sorted(covered), "roots": sorted(roots),
            "cold_exception_roots": [
                {"unit": u.uid, "vaddrs": list(u.vaddrs), "key": list(u.key), "entries": 0}
                for u in handlers],
            "unresolved_indirect_edges": [
                {"source": e.source, "exit": e.exit_id, "targets": list(e.candidates)}
                for e in unresolved],
            "decision": {"reason": "external_roots_and_unresolved_indirect_reentry",
                         "preserve_general_entries": True, "privatize": [], "remove": []}}


def produce(work: Path, qemu_source: Path, plugin_source: Path) -> None:
    from scopt.canon import canonical_json
    from scopt.formats.scm_v1 import parse_scm
    from scopt.formats.translation_coverage import produce_coverage, select_coverage
    from scopt.importer.import_cache import open_snapshot

    work.mkdir(parents=True)
    qemu = build_probe_qemu(qemu_source, work / "qemu")
    plugin = Path(shutil.copy2(plugin_source, work / "libfastdyn.so"))
    cfg, symbols = make_firmware(work, plugin)
    train = work / "train.bin"
    train.write_bytes((1).to_bytes(4, "little"))
    unprofiled, calibration = work / "unprofiled.bin", work / "handler-calibration.bin"
    unprofiled.write_bytes(bytes(4))
    calibration.write_bytes((2).to_bytes(4, "little"))
    env = dict(os.environ)
    env.pop("QEMU_SCOPT_COVERAGE_TRACE", None)
    env.pop("QEMU_SCOPT_COVERAGE_RANGE", None)
    env.setdefault("SCOPT_FASTDYN_SRC", str(ROOT))
    env.update(FASTDYN_ROOT=str(ROOT), SCOPT_RUNTIME_QEMU=str(qemu))
    control, prep = work / "control", work / "prep"
    cli("prepare", "-c", cfg, "--out", control, "--qemu", qemu,
        "--inputs", train, "--no-oracle", env=env)
    start, end, trigger = (symbols[name] for name in ("coverage_start", "coverage_end", "start"))
    control_capture = parse_scm((control / "capture.scm").read_bytes())
    if any(start <= tb.vaddr < end for tb in control_capture.tbs.values()):
        raise ValueError("control run unexpectedly translated the untrained branch")
    trace = work / "producer.trace"
    producer_env = dict(env, QEMU_SCOPT_COVERAGE_TRACE=str(trace),
                        QEMU_SCOPT_COVERAGE_RANGE=f"{start:x}:{end:x}:{trigger:x}")
    cli("prepare", "-c", cfg, "--out", prep, "--qemu", qemu, "--inputs", train,
        "--inputs", calibration, env=producer_env)
    cache = prep / "cache/host-inline.sc"
    manifest = prep / "cache/host-inline.sc.fastdyn.json"
    bundle, runtime, store = work / "bundle.json", work / "runtime.json", work / "store"
    cli("bundle", "identity", "-c", prep / "run.toml", "--manifest", manifest, "--out", bundle, env=env)
    cli("bundle", "runtime-identity", "--run-dir", prep / "oracle", "--sc", cache,
        "--scm", prep / "capture.scm", "--out", runtime, env=env)
    cli("import", prep, "--bundle", bundle, "--runtime-identity", runtime, "--store", store, env=env)
    location, = (store / "snapshots").iterdir()
    snapshot = open_snapshot(location)
    coverage = produce_coverage(snapshot, trace.read_text())
    evidence = work / "translation_coverage.json"
    evidence.write_bytes(canonical_json(coverage) + b"\n")
    complete = select_coverage(snapshot, "complete", evidence)
    layouts = work / "envlayouts"
    layouts.mkdir()
    with qemu.open("rb") as stream:
        qemu_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    env.update(SCOPT_ENVLAYOUT_PATH=str(layouts), SCOPT_RUNTIME_QEMU_SHA256=qemu_hash)
    layout = layouts / f"{qemu_hash}.json"
    cli("envlayout", "produce", "--exe", qemu, "--out", layout, env=env)
    profile = work / "profile"
    cli("profile", "run", "-c", cfg, "--snapshot", location, "--qemu", qemu,
        "--phase", f"steady@{symbols['hot_loop']:#x}", "--out", profile, "--inputs", train, env=env)
    merged_profile = profile / "merged.profile.json"
    reachability = work / "reachability.json"
    decisions = check_reachability(snapshot, complete, coverage, json.loads(merged_profile.read_text()))
    reachability.write_bytes(canonical_json(decisions) + b"\n")

    cli("optimize", location, "--pipeline", "identity", "--store", store, env=env)
    artifact, = (store / "artifacts").iterdir()
    dynamic = work / "identity-v10"
    cli("validate", artifact, "--dynamic", "-c", cfg, "--baseline", location, "--qemu", qemu,
        "--inputs", unprofiled, "--inputs", calibration, "--out", dynamic, env=env)
    result = json.loads((dynamic / "dynamic.json").read_text())
    for pair in result["runs"]:
        expected = int.from_bytes(Path(pair["input"]).read_bytes(), "little")
        for _ in range(256):
            expected = (int(f"{expected:032b}"[::-1], 2) + 0x9E3779B9) % (1 << 32)
        for side in ("baseline", "artifact"):
            run = pair[side]
            register = [line.lower() for line in run["guest_norm"] if line.startswith("Reg 0:")]
            if register != [f"reg 0:{expected:#010x}"] or run["appended"] != 0:
                raise ValueError(f"{side} failed the cold branch or exception oracle")

    output = {"snapshot": str(location), "coverage": str(evidence), "qemu": str(qemu),
              "config": str(cfg), "train": str(train), "coverage_id": complete.coverage_id,
              "control_records": len(control_capture.tbs), "pretranslated": len(complete.covered_units),
              "profile_excluded": str(unprofiled), "envlayout": str(layout),
              "profile": str(merged_profile), "identity_artifact": str(artifact),
              "dynamic": str(dynamic / "dynamic.json"), "reachability": str(reachability)}
    (work / "fixture.json").write_bytes(canonical_json(output) + b"\n")
    print(json.dumps(output, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--qemu-source", type=Path, default=ROOT / "third_party/common/qemu")
    parser.add_argument("--plugin", type=Path, default=ROOT / "build/libfastdyn.so")
    args = parser.parse_args()
    try:
        produce(args.work_dir.resolve(), args.qemu_source.resolve(), args.plugin.resolve())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"translation coverage bootstrap failed: {error}") from error


if __name__ == "__main__":
    main()
