"""Paired ordinary-MMIO benchmark for the saved-PC exporter cleanup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile


REPO = Path(__file__).resolve().parents[3]
QEMU = Path(os.environ.get(
    "FASTDYN_QEMU_BIN",
    REPO / "third_party/common/qemu/build/qemu-system-arm",
))
FASTDYN = Path(os.environ.get("FASTDYN_BIN", REPO / "fastdyn-env/bin/fastdyn"))
BASELINE = os.environ.get("FASTDYN_MMIO_PC_BASELINE_PLUGIN")
PATCHED = os.environ.get("FASTDYN_MMIO_PC_PATCHED_PLUGIN", str(REPO / "build/libfastdyn.so"))
PAIRS = int(os.environ.get("FASTDYN_MMIO_PC_PAIRS", "5"))


def command(args: list[str], output: Path) -> str:
    result = subprocess.run(
        args, cwd=REPO, capture_output=True, text=True, timeout=180,
    )
    text = result.stdout + result.stderr
    output.write_text(text)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}\n{text}")
    return text


def qemu_execution_seconds(work: Path) -> float:
    for line in (work / "fastdyn_timing.jsonl").read_text().splitlines():
        record = json.loads(line)
        if record.get("event") == "phase_end" and record.get("phase") == "machine0.qemu_execution":
            return float(record["duration_s"])
    raise RuntimeError("QEMU execution timing record is missing")


def trace_counts(trace: Path) -> tuple[int, int]:
    reads = writes = 0
    with trace.open(errors="replace") as stream:
        for line in stream:
            reads += " Read:" in line
            writes += " Write:" in line
    trace.unlink()
    return reads, writes


def render_config(root: Path, tag: str, plugin: str, firmware: Path, model: Path) -> Path:
    config = (REPO / "tests/integration/mmio_opt/config.toml.in").read_text()
    values = {
        "QEMU": QEMU,
        "PLUGIN": plugin,
        "FIRMWARE": firmware,
        "MODEL": model,
        "QMP": root / f"{tag}.sock",
        # This benchmark deliberately exercises the ordinary device callback.
        "MMIO_ENABLED": "false",
    }
    for key, value in values.items():
        config = config.replace(f"@{key}@", str(value))
    path = root / f"{tag}.toml"
    path.write_text(config)
    return path


def summarize(samples: list[dict[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for mode in ("baseline", "patched"):
        durations = [float(s["seconds"]) for s in samples if s["mode"] == mode]
        median = statistics.median(durations)
        accesses = int(next(s["accesses"] for s in samples if s["mode"] == mode))
        result[mode] = {
            "samples_seconds": durations,
            "median_seconds": median,
            "min_seconds": min(durations),
            "max_seconds": max(durations),
            "spread_seconds": max(durations) - min(durations),
            "mmio_accesses_per_second": accesses / median,
        }
    baseline = result["baseline"]
    patched = result["patched"]
    assert isinstance(baseline, dict) and isinstance(patched, dict)
    result["speedup"] = baseline["median_seconds"] / patched["median_seconds"]
    return result


def run(root: Path) -> dict[str, object]:
    if not BASELINE:
        raise RuntimeError(
            "set FASTDYN_MMIO_PC_BASELINE_PLUGIN to a legacy FastDyn plugin build"
        )
    if PAIRS < 5:
        raise RuntimeError("FASTDYN_MMIO_PC_PAIRS must be at least 5")

    firmware_obj = root / "firmware.o"
    firmware = root / "firmware.elf"
    command([
        "arm-none-eabi-as", "-mcpu=cortex-m4",
        str(REPO / "tests/integration/mmio_opt/firmware.S"),
        "-o", str(firmware_obj),
    ], root / "assemble.txt")
    command([
        "arm-none-eabi-ld", "-T",
        str(REPO / "tests/integration/mmio_opt/firmware.ld"),
        str(firmware_obj), "-o", str(firmware),
    ], root / "link.txt")
    command([
        str(FASTDYN), "build-model", "--no-vio",
        "tests/models/elder_echo.c", "-o", str(root / "models"), "--json",
    ], root / "model-build.txt")
    model = root / "models/elder_echo.so"

    samples: list[dict[str, object]] = []
    modes = {"baseline": BASELINE, "patched": PATCHED}
    for pair in range(1, PAIRS + 1):
        order = ("baseline", "patched") if pair % 2 else ("patched", "baseline")
        for mode in order:
            tag = f"pair{pair}-{mode}"
            work = root / f"{tag}-work"
            config = render_config(root, tag, modes[mode], firmware, model)
            outcome = command([
                str(FASTDYN), "-q", "run", "-c", str(config), "-o", str(work),
            ], root / f"{tag}.out")
            if "PASS automatic MMIO substitution" not in outcome or "FAIL" in outcome:
                raise RuntimeError(f"firmware result mismatch in {tag}")
            reads, writes = trace_counts(work / "io.log")
            samples.append({
                "pair": pair,
                "mode": mode,
                "seconds": qemu_execution_seconds(work),
                "reads": reads,
                "writes": writes,
                "accesses": reads + writes,
                "firmware_result": "PASS",
            })

    signatures = {(s["reads"], s["writes"], s["firmware_result"]) for s in samples}
    if len(signatures) != 1:
        raise RuntimeError(f"baseline/patched work differs: {sorted(signatures)}")
    report = {
        "timing_boundary": "machine0.qemu_execution (whole QEMU execution)",
        "automatic_mmio_substitution": False,
        "pairs": PAIRS,
        "samples": samples,
        "summary": summarize(samples),
    }
    (root / "summary.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


if __name__ == "__main__":
    destination = os.environ.get("FASTDYN_MMIO_PC_ARTIFACTS")
    if destination:
        artifact_root = Path(destination).resolve()
        artifact_root.mkdir(parents=True, exist_ok=False)
        print(json.dumps(run(artifact_root), indent=2, sort_keys=True))
    else:
        with tempfile.TemporaryDirectory(prefix="fastdyn-mmio-pc-") as temporary:
            print(json.dumps(run(Path(temporary)), indent=2, sort_keys=True))
