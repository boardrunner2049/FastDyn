"""Baseline/optimized public-workflow regressions; retain auditable artifacts."""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


REPO = Path(__file__).resolve().parents[3]
QEMU = os.environ.get("FASTDYN_QEMU_BIN", str(REPO / "third_party/common/qemu/build/qemu-system-arm"))
PLUGIN = os.environ.get("FASTDYN_PLUGIN_LIB", str(REPO / "build/libfastdyn.so"))
FASTDYN = os.environ.get("FASTDYN_BIN", str(REPO / "fastdyn-env/bin/fastdyn"))


def command(args, output):
    result = subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=120)
    output.write_text(result.stdout + result.stderr)
    assert result.returncode == 0, output.read_text()
    return result.stdout + result.stderr


def load(path):
    return json.loads(path.read_text())


def execution_time(work):
    records = [json.loads(line) for line in (work / "fastdyn_timing.jsonl").read_text().splitlines()]
    return next(r["duration_s"] for r in records if r.get("event") == "phase_end"
                and r.get("phase") == "machine0.qemu_execution")


def run(root):
    started = time.perf_counter()
    command([FASTDYN, "build-model", "--mmio-opt", "--no-vio",
             "tests/models/elder_echo.c", "tests/models/mmio_safety.c",
             "tests/models/mmio_predicates.c", "-o", str(root / "models"), "--json"],
            root / "model-build.txt")
    compilation = time.perf_counter() - started
    # Compile the shipped external observer with its public build command.
    observer = root / "plugins/mmio_count"
    (observer / "host").mkdir(parents=True)
    shutil.copy2(REPO / "tests/plugins/mmio_count/host/preprocessor.py", observer / "host/preprocessor.py")
    command([FASTDYN, "build-model", "--no-vio", "--pkg", "fastdyn",
             "tests/plugins/mmio_count/runtime/mmio_count.c", "-o", str(observer / "runtime")],
            root / "observer-build.txt")
    template = (REPO / "tests/integration/mmio_opt/config.toml.in").read_text()
    summary = {"model_compilation_seconds": compilation, "cases": {}}
    for case, source, model, label in (
        ("echo", "firmware", "elder_echo", None),
        ("safety", "safety", "mmio_safety", None),
        ("predicates", "predicates", "mmio_predicates", None),
        ("alignment", "alignment", "elder_echo", None),
        ("observer", "firmware", "elder_echo", "normal"),
        ("late-observer", "firmware", "elder_echo", "late"),
    ):
        directory = root / case
        directory.mkdir()
        firmware = directory / "firmware.elf"
        command(["arm-none-eabi-as", "-mcpu=cortex-m4", str(REPO / f"tests/integration/mmio_opt/{source}.S"),
                 "-o", str(directory / "firmware.o")], directory / "assemble.txt")
        command(["arm-none-eabi-ld", "-T", str(REPO / "tests/integration/mmio_opt/firmware.ld"),
                 str(directory / "firmware.o"), "-o", str(firmware)], directory / "link.txt")
        # Explicit stripped-firmware discovery regression.
        command(["arm-none-eabi-strip", "--strip-all", str(firmware)], directory / "strip.txt")
        reports, observations = {}, {}
        for enabled in (False, True):
            mode = "optimized" if enabled else "baseline"
            work = directory / mode
            config = template
            for key, value in {
                "QEMU": QEMU, "PLUGIN": PLUGIN, "FIRMWARE": str(firmware),
                "MODEL": str(root / f"models/{model}.so"),
                "QMP": str(directory / f"{mode}.sock"),
                "MMIO_ENABLED": str(enabled).lower(),
            }.items():
                config = config.replace(f"@{key}@", value)
            if label:
                config += (f'\n[CPU.cpu0.plugins.mmio_count]\nenabled = true\n'
                           f'source = "{observer}"\nlabel = "{label}"\n')
            config_file = directory / f"{mode}.toml"
            config_file.write_text(config)
            outcome = command([FASTDYN, "-q", "run", "-c", str(config_file), "-o", str(work)],
                              directory / f"{mode}-outcome.txt")
            assert "PASS" in outcome and "FAIL" not in outcome, outcome
            if enabled:
                report = load(work / "run-artifacts/mmio_opt/installation.json")
                discovery = load(work / "run-artifacts/mmio_opt/discovery.json")
                reports[mode] = report
            if label:
                counts = dict(line.split("=", 1) for line in
                              (work / "run-artifacts/mmio_count/counts.txt").read_text().splitlines())
                observations[mode] = {k: int(v) for k, v in counts.items()
                                      if k.startswith(("reads_", "writes_"))}
            reports.setdefault(mode, {})["execution_seconds"] = execution_time(work)
            # Preserve the ordinary trace as a compressed artifact, including
            # the read-to-clear returned values and state after clearing.
            trace = work / "io.log"
            if trace.exists():
                with trace.open("rb") as inp, gzip.open(str(trace) + ".gz", "wb") as out:
                    shutil.copyfileobj(inp, out)
                trace.unlink()
        report = reports["optimized"]
        sites = report["sites"]
        fast = sum(s["fast_count"] for s in sites)
        fallback = sum(s["fallback_count"] for s in sites)
        if case == "echo":
            assert report["installed"] >= 5 and fast >= 1_000_000
            assert fallback >= 2
            assert any(s["fast_count"] and s["fallback_count"] for s in sites)
            assert any(s["fast_count"] == 2 and s["fallback_count"] == 0 for s in sites)
        elif case == "safety":
            for address in (0x40010004, 0x4001000c, 0x40010020, 0x40010024):
                assert all(not s["installed"] for s in sites if int(s["address"], 16) == address), sites
            for address in (0x40010010, 0x40010014):
                assert any(s["fast_count"] == 1 for s in sites if int(s["address"], 16) == address), sites
            with gzip.open(directory / "optimized/io.log.gz", "rt") as stream:
                trace = stream.read()
            assert trace.count("address = 0x40010004") == 2, trace
            assert "value = 0x12345678" in trace and "value = 0x00000000" in trace, trace
        elif case == "alignment":
            assert fast == 0 and report["installed"] == 0
            assert any("unaligned" in (s["reason"] or "") for s in sites)
        elif label:
            assert observations["baseline"] == observations["optimized"], observations
            assert observations["optimized"]["reads_handled"] >= 1_000_000
            assert fast == 0
            if label == "normal":
                assert report["installed"] == 0
            else:
                assert report["installed"] > 0 and fallback >= 1_000_000
        summary["cases"][case] = {
            "baseline_execution_seconds": reports["baseline"]["execution_seconds"],
            "optimized_execution_seconds": report["execution_seconds"],
            "firmware_discovery_plan_seconds": discovery["preparation_seconds"],
            "installed": report["installed"], "declined": report["prepared"] - report["installed"],
            "fast": fast, "fallback": fallback, "observer_counts": observations,
            "observation_policy": {
                "observer": label or "none",
                "baseline_io_log": "all ordinary accesses",
                "optimized_io_log": "ordinary accesses only; substituted reads omit log entries",
                "timing_interpretation": "dispatch plus logging savings; no isolated dispatch claim",
            },
        }
        print(f"PASS {case}: installed={report['installed']} fast={fast} fallback={fallback}", flush=True)
    (root / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    destination = os.environ.get("FASTDYN_MMIO_ARTIFACTS")
    if destination:
        root = Path(destination).resolve()
        root.mkdir(parents=True, exist_ok=False)
        print(f"MMIO artifacts: {root}", flush=True)
        run(root)
    elif os.environ.get("FASTDYN_KEEP_TEST_TMP") == "1":
        root = Path(tempfile.mkdtemp(prefix="fastdyn-mmio-"))
        print(f"MMIO artifacts: {root}", flush=True)
        run(root)
    else:
        with tempfile.TemporaryDirectory(prefix="fastdyn-mmio-") as temporary:
            run(Path(temporary))
