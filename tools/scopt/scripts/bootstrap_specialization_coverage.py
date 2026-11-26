#!/usr/bin/env python3
"""Run global specialization in both modes on a genuine pretranslated fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import bootstrap_host_inline as bootstrap
import bootstrap_translation_coverage as producer


def run(work: Path, qemu: Path, plugin_source: Path, selftest: Path | None) -> None:
    from scopt.canon import canonical_json
    from scopt.formats.translation_coverage import produce_coverage, select_coverage
    from scopt.importer.import_cache import open_snapshot

    work.mkdir(parents=True)
    plugin = Path(shutil.copy2(plugin_source, work / "libfastdyn.so"))
    bootstrap.FIXTURE = producer.SCOPT / "fixtures/global-specialization"
    config, phase, inputs = bootstrap.generate_fixture(work, plugin, work / "fixture.log")
    with qemu.open("rb") as stream:
        qemu_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    layouts = work / "envlayouts"
    layouts.mkdir()
    env = dict(os.environ, FASTDYN_ROOT=str(producer.ROOT),
               SCOPT_RUNTIME_QEMU=str(qemu), SCOPT_RUNTIME_QEMU_SHA256=qemu_sha,
               SCOPT_ENVLAYOUT_PATH=str(layouts), SCOPT_V6_EXES=qemu_sha)
    env.setdefault("SCOPT_FASTDYN_SRC", str(producer.ROOT))
    env.pop("QEMU_SCOPT_COVERAGE_TRACE", None)
    env.pop("QEMU_SCOPT_COVERAGE_RANGE", None)

    def cli(*args, env=env):
        producer.cli(*args, env=env)

    def flags(split):
        return [arg for path, _expected in inputs[split] for arg in ("--inputs", path)]

    elf = work / "guest.elf"
    symbols = {name: int(addr, 16) for addr, _, name in map(
        str.split, subprocess.check_output(["arm-none-eabi-nm", "-n", elf], text=True).splitlines())}
    start, end, trigger = (symbols[name] for name in ("coverage_start", "coverage_end", "start"))
    trace, prep = work / "producer.trace", work / "prep"
    probed = dict(env, QEMU_SCOPT_COVERAGE_TRACE=str(trace),
                  QEMU_SCOPT_COVERAGE_RANGE=f"{start:x}:{end:x}:{trigger:x}")
    cli("prepare", "-c", config, "--out", prep, "--qemu", qemu, *flags("train"), env=probed)
    for i, (_, expected) in enumerate(inputs["train"]):
        run_file = prep / ("run.json" if i == 0 else f"run-{i}.json")
        bootstrap.oracle(json.loads(run_file.read_text()), expected)

    manifest = prep / "cache/host-inline.sc.fastdyn.json"
    cache = prep / "cache/host-inline.sc"
    bundle, runtime, store = work / "bundle.json", work / "runtime.json", work / "store"
    cli("bundle", "identity", "-c", prep / "run.toml", "--manifest", manifest, "--out", bundle)
    cli("bundle", "runtime-identity", "--run-dir", prep / "oracle", "--sc", cache,
        "--scm", prep / "capture.scm", "--out", runtime)
    cli("import", prep, "--bundle", bundle, "--runtime-identity", runtime, "--store", store)
    snapshot, = (store / "snapshots").iterdir()
    coverage_file = work / "translation_coverage.json"
    snap = open_snapshot(snapshot)
    coverage_file.write_bytes(canonical_json(produce_coverage(snap, trace.read_text())) + b"\n")
    complete = select_coverage(snap, "complete", coverage_file)

    cli("envlayout", "produce", "--exe", qemu, "--out", layouts / f"{qemu_sha}.json")
    admission = work / "runtime.v2.json"
    cli("admission", "identity", "--snapshot", snapshot, "--config", config, "--out", admission)
    profile_dir, profile = work / "profile", work / "profile/merged.profile.json"
    cli("profile", "run", "-c", config, "--snapshot", snapshot, "--qemu", qemu,
        "--phase", f"steady@{phase}", "--out", profile_dir, *flags("train"))
    local_selftest = work / "hostsym-selftest.json"
    if selftest is not None:
        shutil.copy2(selftest, local_selftest)
    arms = {}
    for mode in ("partial", "complete"):
        config_opts = {"coverage_mode": mode, "max_code_growth_bytes": 65536}
        if mode == "complete":
            config_opts["coverage_evidence"] = str(coverage_file)
        params = work / f"{mode}-params.json"
        params.write_bytes(canonical_json({"specialize-edges": config_opts}) + b"\n")
        arm = work / mode
        cli("optimize", snapshot, "--pipeline", "global-specialization", "--profile", profile,
            "--runtime-identity", admission, "--prep", prep, "--elf", elf, "--v6-exe", qemu_sha,
            "--selftest", local_selftest, "--params", params, "--store", arm)
        artifact, = (arm / "artifacts").iterdir()
        dynamic = arm / "v10"
        cli("validate", artifact, "--dynamic", "-c", config, "--baseline", snapshot,
            "--qemu", qemu, *flags("heldout"), "--out", dynamic)
        arms[mode] = {"artifact": str(artifact), "dynamic": str(dynamic / "dynamic.json")}

    data = {"snapshot": str(snapshot), "coverage": str(coverage_file),
            "coverage_id": complete.coverage_id, "config": str(config), "qemu": str(qemu),
            "arms": arms, "profile": str(profile), "source_pc": symbols["source"],
            "consumer_pc": symbols["consumer"], "heldout": str(inputs["heldout"][0][0])}
    (work / "specialization-coverage.json").write_bytes(canonical_json(data) + b"\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--qemu", required=True, type=Path)
    parser.add_argument("--plugin", type=Path, default=producer.ROOT / "build/libfastdyn.so")
    parser.add_argument("--selftest", type=Path)
    args = parser.parse_args()
    run(args.work_dir.resolve(), args.qemu.resolve(strict=True), args.plugin.resolve(strict=True),
        args.selftest.resolve(strict=True) if args.selftest else None)


if __name__ == "__main__":
    main()
