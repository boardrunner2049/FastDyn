"""Paired FastDyn benchmark for fixed immediate PC chaining."""
from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile


REPO = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).resolve().parent
QEMU = Path(os.environ.get(
    "FASTDYN_QEMU_BIN", REPO / "third_party/common/qemu/build/qemu-system-arm"
))
PLUGIN = Path(os.environ.get("FASTDYN_PLUGIN_LIB", REPO / "build/libfastdyn.so"))
FASTDYN = Path(os.environ.get("FASTDYN_BIN", REPO / "fastdyn-env/bin/fastdyn"))
PAIRS = int(os.environ.get("FASTDYN_CONST_PC_BENCH_PAIRS", "5"))
ITERATIONS = int(os.environ.get("FASTDYN_CONST_PC_BENCH_ITERATIONS", "5000000"))


def checked(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, timeout=300, **kwargs)
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"{result.stdout}{result.stderr}"
        )
    return result


def build(root: Path) -> tuple[Path, str, str]:
    obj = root / "thumb.o"
    elf = root / "thumb.elf"
    checked([
        "arm-none-eabi-as", "-mcpu=cortex-m4", "--defsym",
        f"ITERATIONS={ITERATIONS}", str(FIXTURE / "thumb.S"), "-o", str(obj),
    ])
    checked(["arm-none-eabi-ld", "-T", str(FIXTURE / "thumb.ld"),
             str(obj), "-o", str(elf)])
    symbols = checked(["nm", "-n", str(elf)]).stdout.splitlines()
    values = {parts[2]: f"0x{parts[0]}" for line in symbols
              if len(parts := line.split()) == 3}
    return elf, values["redirect_site"], values["redirect_target"]


def render(root: Path, tag: str, elf: Path, site: str, target: str) -> Path:
    config = root / f"{tag}.toml"
    config.write_text(f"""[Memory]
[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size = "512K"
backend = "ram"
share = false

[Machine]
platform = "generic-cortexm"
qemu_path = "{QEMU}"
enable_gdb = false
stop_on_start = false
monitor_port = 0
qmp_socket = "{root / (tag + '.sock')}"
semihosting = true
semihosting_config = "enable=on,target=native"
coverage = false
finline = "None"

[CPU]
[[CPU.cpu0]]
arch = "arm"
machine = "cortexm"
cpu = "cortex-m4"
plugin_library = "{PLUGIN}"
binary = "{elf}"
init_nsvtor = "0x0"
twintrace = "None"

[[CPU.cpu0.modifiers]]
at = "{site}"
patch = "r15 <- {target}"
""")
    return config


def qemu_seconds(work: Path) -> float:
    for line in (work / "fastdyn_timing.jsonl").read_text().splitlines():
        record = json.loads(line)
        if (record.get("event") == "phase_end" and
                record.get("phase") == "machine0.qemu_execution"):
            return float(record["duration_s"])
    raise RuntimeError("QEMU execution timing record is missing")


def summarize(samples: list[dict[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for mode in ("legacy", "optimized"):
        values = [float(item["seconds"]) for item in samples if item["mode"] == mode]
        median = statistics.median(values)
        result[mode] = {
            "samples_seconds": values,
            "median_seconds": median,
            "min_seconds": min(values),
            "max_seconds": max(values),
            "spread_seconds": max(values) - min(values),
            "redirects_per_second": ITERATIONS / median,
        }
    legacy = result["legacy"]
    optimized = result["optimized"]
    assert isinstance(legacy, dict) and isinstance(optimized, dict)
    result["speedup"] = legacy["median_seconds"] / optimized["median_seconds"]
    return result


def run(root: Path) -> dict[str, object]:
    if PAIRS < 5:
        raise RuntimeError("FASTDYN_CONST_PC_BENCH_PAIRS must be at least 5")
    elf, site, target = build(root)
    samples: list[dict[str, object]] = []
    for pair in range(1, PAIRS + 1):
        order = ("legacy", "optimized") if pair % 2 else ("optimized", "legacy")
        for mode in order:
            tag = f"pair{pair}-{mode}"
            work = root / f"{tag}-work"
            config = render(root, tag, elf, site, target)
            env = os.environ.copy()
            env["FASTDYN_CONST_PC_CHAIN"] = "0" if mode == "legacy" else "1"
            env["PYTHONPATH"] = str(REPO / "src") + (
                os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
            )
            outcome = checked([
                str(FASTDYN), "-q", "run", "-c", str(config), "-o", str(work),
            ], cwd=REPO, env=env)
            text = outcome.stdout + outcome.stderr
            expected = "PASS constant-pc thumb checksum=iterations*3"
            if expected not in text or "FAIL constant-pc" in text:
                raise RuntimeError(f"checksum mismatch in {tag}:\n{text}")
            samples.append({
                "pair": pair,
                "mode": mode,
                "seconds": qemu_seconds(work),
                "iterations": ITERATIONS,
                "checksum": "iterations*3",
            })
    report = {
        "timing_boundary": "machine0.qemu_execution (whole QEMU process)",
        "warmup": "long bounded loop; first translation is included equally in both modes",
        "pairs": PAIRS,
        "iterations_per_run": ITERATIONS,
        "samples": samples,
        "summary": summarize(samples),
    }
    (root / "summary.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


if __name__ == "__main__":
    destination = os.environ.get("FASTDYN_CONST_PC_ARTIFACTS")
    if destination:
        artifact_root = Path(destination).resolve()
        artifact_root.mkdir(parents=True, exist_ok=False)
        print(json.dumps(run(artifact_root), indent=2, sort_keys=True))
    else:
        with tempfile.TemporaryDirectory(prefix="fastdyn-constant-pc-") as temporary:
            print(json.dumps(run(Path(temporary)), indent=2, sort_keys=True))
