#!/usr/bin/env python3
"""Measure repeated unchanged-image FastDyn runs for the preflight baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import time
from pathlib import Path
from typing import Sequence


BENCH = re.compile(r"\[BENCH\]\[minimal\]\s+elapsed_ns=(\d+)")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fastdyn", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=15)
    args = parser.parse_args(argv)
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    runs = []
    for sample in range(args.samples):
        output = work / f"run-{sample:02d}"
        command = [str(args.fastdyn.resolve()), "-q", "run", "-c", str(args.config.resolve()), "-o", str(output)]
        before = time.perf_counter_ns()
        result = subprocess.run(command, capture_output=True, text=True)
        elapsed = time.perf_counter_ns() - before
        combined = result.stdout + result.stderr
        match = BENCH.search(combined)
        timing_path = output / "fastdyn_timing.jsonl"
        phases = {}
        if timing_path.is_file():
            for line in timing_path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if event.get("event") == "phase_end" and "duration_s" in event:
                    phases[event["phase"]] = event["duration_s"]
        runs.append({
            "sample": sample,
            "command": command,
            "returncode": result.returncode,
            "wall_ns": elapsed,
            "guest_benchmark_ns": int(match.group(1)) if match else None,
            "phases_s": phases,
            "stdout": result.stdout.splitlines(),
            "stderr": result.stderr.splitlines(),
        })
        if result.returncode != 0 or match is None:
            raise RuntimeError(f"FastDyn sample {sample} failed or omitted its guest benchmark marker")

    def values(key):
        return [run[key] for run in runs]

    phase_names = ("qemu.wait", "machine0.qemu_execution", "fastdyn.machine0.run", "fastdyn.run.total")
    report = {
        "schema_version": 1,
        "classification": "unchanged_image_fastdyn_cortex_m3_baseline",
        "config": {"path": str(args.config.resolve()), "sha256": sha256(args.config.resolve())},
        "samples": args.samples,
        "runs": runs,
        "summary": {
            "wall_ns_median": statistics.median(values("wall_ns")),
            "guest_benchmark_ns_median": statistics.median(values("guest_benchmark_ns")),
            "phase_seconds_median": {
                phase: statistics.median(run["phases_s"][phase] for run in runs)
                for phase in phase_names
            },
        },
        "claims": {"optimized_image": False, "speedup": False, "hardware_cycles": False},
    }
    path = work / "report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(path), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
