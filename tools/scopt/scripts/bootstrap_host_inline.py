#!/usr/bin/env python3
"""Build and qualify the source-locked host-inline baseline on x86-64 Linux."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import runpy
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tools/scopt/fixtures/host-inline"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def output(argv, cwd=ROOT) -> str:
    return subprocess.check_output(argv, cwd=cwd, text=True).strip()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def command(log: Path, argv, *, cwd=ROOT) -> None:
    args = [os.fspath(arg) for arg in argv]
    line = f"(cd {shlex.quote(str(cwd))} && {shlex.join(args)})"
    print(line, flush=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        subprocess.run(args, cwd=cwd, check=True, stdout=stream, stderr=subprocess.STDOUT)


def build(work: Path, lock: dict, log: Path, reuse_build: Path | None = None) -> tuple[Path, Path]:
    start = output(["git", "rev-parse", "HEAD"])
    if lock["schema"] != "scopt.host-inline-source-lock/1":
        raise ValueError("unsupported source lock")
    command(log, ["git", "diff", "--quiet", lock.get("runtime_source_ref", lock["starting_fastdyn"]), "--", ".",
                  ":(exclude)tools/scopt"])
    observed = {pkg: output(["dpkg-query", "-W", "-f=${Version}", pkg])
                for pkg in lock["build_dependencies"]}
    for pkg, version in lock["build_dependencies"].items():
        if observed[pkg] != version:
            raise ValueError(f"{pkg}: installed {observed[pkg]}, source lock requires {version}")
    if output(["gcc", "-dumpfullversion"]) != lock["compiler"]:
        raise ValueError("compiler differs from source lock")
    if output(["ld", "-v"]) != lock["linker"]:
        raise ValueError("linker differs from source lock")
    for patch in lock["setup_patch_checks"]:
        if digest(ROOT / patch["path"]) != patch["sha256"]:
            raise ValueError(f"patch hash mismatch: {patch['path']}")
    qemu_src = ROOT / "third_party/common/qemu"
    command(log, ["git", "submodule", "update", "--init", "--depth", "1",
                  "third_party/common/qemu", "third_party/common/cmsis-svd-data",
                  "third_party/courbet_deps/SITL_Models", "third_party/courbet_deps/mavlink_headers"])
    if output(["git", "rev-parse", "HEAD"], qemu_src) != lock["qemu"]:
        raise ValueError("QEMU submodule differs from source lock")
    command(log, ["git", "diff", "--quiet", "HEAD"], cwd=qemu_src)
    if reuse_build is None:
        command(log, lock["build"]["setup"])
    else:
        previous = json.loads(reuse_build.read_text())
        if previous["source_lock"] != lock:
            raise ValueError("previous build used a different runtime source lock")
        for relative in lock["native_images"].values():
            path = ROOT / relative
            if digest(path) != previous["images"].get(str(path)):
                raise ValueError(f"previous build digest mismatch: {path}")
    command(log, ["git", "diff", "--quiet", "HEAD"], cwd=qemu_src)
    qemu, plugin = (ROOT / lock["native_images"][key] for key in ("qemu", "plugin"))
    options = json.loads((qemu.parent / "meson-info/intro-buildoptions.json").read_text())
    selected = {row["name"]: row["value"] for row in options
                if row["name"] in {"debug", "optimization"}}
    if selected.get("debug") is not True or selected.get("optimization") != "2":
        raise ValueError(f"QEMU build differs from the required -O2 -g build: {selected}")
    command(log, [qemu, "--version"])
    command(log, ["readelf", "-n", qemu, plugin])
    write_json(work / "build-evidence.json", {"starting_fastdyn": start,
               "source_lock": lock, "packages": observed, "qemu_build_options": selected,
               "images": {str(p): digest(p) for p in (qemu, plugin)}})
    return qemu, plugin


def generate_fixture(work: Path, plugin: Path, log: Path):
    obj, elf = work / "guest.o", work / "guest.elf"
    command(log, ["arm-none-eabi-as", "-mcpu=cortex-m3", "-mthumb", "-o", obj,
                  FIXTURE / "guest.S"])
    command(log, ["arm-none-eabi-ld", "-T", FIXTURE / "guest.ld", "-o", elf, obj])
    symbols = {name: int(addr, 16) for addr, _, name in
               map(str.split, output(["arm-none-eabi-nm", "-n", elf]).splitlines())}
    text = (FIXTURE / "fastdyn.toml.in").read_text()
    for key, value in {"FIRMWARE": str(elf), "PLUGIN": str(plugin),
                       **{key.upper(): hex(symbols[key]) for key in
                          ("benchmark_start", "print_result", "benchmark_end")}}.items():
        text = text.replace(f"@{key}@", json.dumps(value))
    config = work / "host-inline.toml"
    config.write_text(text)
    (work / "no-cache.toml").write_text(text.replace('supercache = "cache/host-inline.sc"\n', ""))
    cases = json.loads((FIXTURE / "inputs.json").read_text())
    calculate = runpy.run_path(str(FIXTURE / "oracle.py"))["expected_word"]
    inputs = {}
    for split, rows in cases.items():
        inputs[split] = []
        for index, row in enumerate(rows):
            seed = int(row["seed"], 0)
            expected = calculate(seed)
            if expected != int(row["expected"], 0):
                raise ValueError(f"fixture oracle changed for seed {seed:#x}")
            path = work / f"{split}-{index}.bin"
            path.write_bytes(seed.to_bytes(4, "little"))
            inputs[split].append((path, expected))
    return config, hex(symbols["hot_loop"]), inputs


def oracle(run: dict, expected: int) -> None:
    if run["exit_code"] != 0 or run["timed_out"]:
        raise ValueError(f"guest failed: {run['stdout_log']}")
    text = Path(run["stdout_log"]).read_text(errors="replace")
    observed = re.findall(r"^Reg 0:0x([0-9a-fA-F]+)\s*$", text, re.MULTILINE)
    if [int(word, 16) for word in observed] != [expected]:
        raise ValueError(f"{run['stdout_log']}: expected {expected:#010x}, got {observed}")


def qualify(work: Path, lock: dict, log: Path, reuse_build: Path | None = None) -> None:
    qemu, plugin = build(work, lock, log, reuse_build)
    qualify_images(work, qemu, plugin, log)


def qualify_images(work: Path, qemu: Path, plugin: Path, log: Path) -> None:
    from scopt.run.benchmark_memory import benchmark_ram

    config, phase, inputs = generate_fixture(work, plugin, log)
    default_ram = "/dev/shm" if FIXTURE.name in ("host-inline", "host-inline-two-rbit") else None
    with benchmark_ram(config, default_ram) as ram_root:
        _qualify_config(work, qemu, plugin, log, config, phase, inputs, ram_root)


def _qualify_config(work, qemu, plugin, log, config, phase, inputs, ram_root):
    qemu_hash, plugin_hash = digest(qemu), digest(plugin)
    envdir = work / "envlayouts"
    envdir.mkdir()
    layout = envdir / f"{qemu_hash}.json"
    os.environ.update(FASTDYN_ROOT=str(ROOT), SCOPT_FASTDYN_SRC=str(ROOT),
                      SCOPT_ENVLAYOUT_PATH=str(envdir), SCOPT_RUNTIME_QEMU=str(qemu),
                      SCOPT_RUNTIME_QEMU_SHA256=qemu_hash)
    # Load the harness after setting its checkout paths.
    from scopt.formats.envlayout_v1 import check_binding, read_envlayout
    from scopt.formats.sc_v5 import read_sc
    from scopt.run.deploy import deploy
    from scopt.run.launcher import create_run_dir, launch

    cli = [sys.executable, "-m", "scopt"]

    def scopt(*args):
        command(log, [*cli, *args])

    def flags(split):
        return [arg for p, _ in inputs[split] for arg in ("--inputs", p)]

    def run_baseline(config_path, name, case, seed=None):
        path, expected = case
        rd = create_run_dir(config_path, work / name, qemu=qemu, label=name)
        if seed is not None:
            deploy(seed.cache, rd, timeout_s=90)
        doc = launch(rd, inputs={"input": path}, timeout_s=90, prove=seed is not None)
        oracle(doc, expected)
        if seed is not None and (doc["verdict"] != "PASS" or doc["appended"] != 0):
            raise ValueError(f"{doc['stdout_log']}: warm cache reuse was not proved")
        return rd

    scopt("envlayout", "produce", "--exe", qemu, "--out", layout)
    scopt("envlayout", "show", "--file", layout)
    for split, cases in inputs.items():
        for i, case in enumerate(cases):
            run_baseline(work / "no-cache.toml", f"no-cache-{split}-{i}", case)
    cold = run_baseline(config, "cold", inputs["train"][0])
    warm = run_baseline(config, "warm", inputs["train"][0], seed=cold)
    cold_count = read_sc(cold.cache).header.record_count
    if not cold_count or read_sc(warm.cache).header.record_count != cold_count:
        raise ValueError("warm baseline appended translation blocks")

    prep, store = work / "prep", work / "store"
    scopt("prepare", "-c", config, "--out", prep, "--qemu", qemu, *flags("train"))
    for i, (_, expected) in enumerate(inputs["train"]):
        name = "run.json" if i == 0 else f"run-{i}.json"
        oracle(json.loads((prep / name).read_text()), expected)
    manifest = prep / "cache/host-inline.sc.fastdyn.json"
    runtime, bundle = work / "runtime-identity.json", work / "bundle.json"
    scopt("bundle", "identity", "-c", prep / "run.toml", "--manifest", manifest, "--out", bundle)
    scopt("bundle", "runtime-identity", "--run-dir", prep / "oracle", "--sc",
          prep / "cache/host-inline.sc", "--scm", prep / "capture.scm", "--out", runtime)
    scopt("import", prep, "--bundle", bundle, "--runtime-identity", runtime, "--store", store)
    snapshot, = (store / "snapshots").iterdir()
    admission = work / "runtime-identity.v2.json"
    scopt("admission", "identity", "--snapshot", snapshot, "--config", config, "--out", admission)
    admitted = json.loads(admission.read_text())
    env = read_envlayout(layout)
    if check_binding(env, admitted, admitted["admission"]["envlayout_id"]) is not None:
        raise ValueError("admission identity did not bind the generated EnvLayout")
    profile = work / "profile"
    scopt("profile", "run", "-c", config, "--snapshot", snapshot, "--qemu", qemu,
          "--phase", f"steady@{phase}", "--out", profile, *flags("train"))
    scopt("optimize", snapshot, "--pipeline", "identity", "--profile",
          profile / "merged.profile.json", "--store", store)
    artifact, = (store / "artifacts").iterdir()
    scopt("validate", artifact, "--snapshot", snapshot)
    captures = sorted(artifact.glob("cache*.scm"))
    scopt("import", artifact / "cache.sc", *(a for scm in captures for a in ("--scm", scm)),
          "--manifest", manifest, "--bundle", bundle, "--runtime-identity", runtime,
          "--store", work / "reimport")
    reimport, = (work / "reimport/snapshots").iterdir()
    expected_binding = json.loads((snapshot / "snapshot.json").read_text())["capture"]["binding"]
    actual_binding = json.loads((reimport / "snapshot.json").read_text())["capture"]["binding"]
    if expected_binding != actual_binding:
        raise ValueError("identity artifact reimport changed capture binding counts")
    if reimport.name != snapshot.name:
        raise ValueError("identity artifact reimport changed the snapshot ID")
    scopt("validate", artifact, "--dynamic", "-c", config, "--qemu", qemu,
          "--baseline", snapshot, "--out", work / "v10", *flags("heldout"))
    dynamic = json.loads((work / "v10/dynamic.json").read_text())
    expected_by_input = {str(path): expected for path, expected in inputs["heldout"]}
    if {pair["input"] for pair in dynamic["runs"]} != set(expected_by_input):
        raise ValueError("V10 ran a different held-out input set")
    for pair in dynamic["runs"]:
        for arm in ("artifact", "baseline"):
            if pair[arm]["appended"] != 0:
                raise ValueError(f"{arm} appended translation blocks during V10")
            run = Path(pair[arm]["run_dir"]) / "run.json"
            oracle(json.loads(run.read_text()), expected_by_input[pair["input"]])
    if digest(qemu) != qemu_hash or digest(plugin) != plugin_hash:
        raise ValueError("QEMU or FastDyn plugin changed during qualification")
    write_json(work / "baseline.json", {"snapshot": str(snapshot), "artifact": str(artifact),
               "profile": str(profile / "merged.profile.json"), "qemu_sha256": qemu_hash,
               "plugin_sha256": plugin_hash, "envlayout": str(layout),
               "capture": str(prep / "capture.scm"), "identity_reimport": str(reimport),
               "cold_and_warm_records": cold_count, "dynamic": str(work / "v10/dynamic.json"),
               "ram_root": ram_root,
               "oracle": json.loads((FIXTURE / "inputs.json").read_text())})


def main() -> None:
    global FIXTURE

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-lock", required=True, type=Path)
    parser.add_argument("--fixture", choices=("host-inline", "host-inline-two-rbit"),
                        default="host-inline", help="firmware fixture to qualify")
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--reuse-build", type=Path, help="verify binaries against prior build-evidence.json")
    args = parser.parse_args()
    if sys.platform != "linux" or platform.machine() != "x86_64" or sys.version_info[:2] != (3, 12):
        parser.error("requires x86-64 Linux and Python 3.12")
    FIXTURE = ROOT / "tools/scopt/fixtures" / args.fixture
    work = args.work_dir.resolve()
    work.mkdir(parents=True)
    log = work / "bootstrap.log"
    try:
        qualify(work, json.loads(args.source_lock.read_text()), log, args.reuse_build)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        raise SystemExit(f"bootstrap failed: {error}; see {log}") from error


if __name__ == "__main__":
    main()
