#!/usr/bin/env python3
"""Run the frozen Firm-BOLT versus O3 SmartSpeaker comparison."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-o3-oracle-2026-11-17"
PLAN = RUN / "comparison-plan.json"
RAW = RUN / "comparison-samples.json"
LOG = RUN / "comparison.log"
REPORT = RUN / "comparison-report.json"
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
PRELOAD = (
    ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17/"
    "lemix_mmio_marker_schedule.so"
)
FIRM = (
    ROOT / "build/preflight/lemix-smartspeaker-frs-wrapper-2026-11-17/"
    "wrapper-fop-inplace/SmartSpeaker.bolt-qualified-compact"
)
O3 = RUN / "source/SmartSpeaker_Ported/build/SmartSpeaker"
O0 = (
    ROOT / "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/"
    "dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/"
    "SmartSpeaker_Ported/build/SmartSpeaker"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invert(interval: list[float]) -> list[float]:
    return [1.0 / interval[1], 1.0 / interval[0]]


def main() -> int:
    if RAW.exists() or LOG.exists() or REPORT.exists():
        raise RuntimeError("final comparison outputs already exist")
    plan = json.loads(PLAN.read_text())
    if plan["status"] != "frozen_before_final_timing":
        raise RuntimeError("comparison plan is not frozen")
    command = [
        "taskset", "-c", "24,25", sys.executable, str(BENCHMARK),
        "--variant", f"stock_bolt={FIRM}",
        "--variant", f"o3={O3}",
        "--variant", f"o0_reference={O0}",
        "--preload", str(PRELOAD),
        "--work-units", str(plan["completed_units_per_execution"]),
        "--tick-address", "0x495670",
        "--variant-tick-address", "o3=0x495458",
        "--samples", str(plan["paired_blocks"]),
        "--seed", str(plan["randomization_seed"]),
        "--output", str(RAW),
    ]
    with LOG.open("w") as stream:
        environment = dict(os.environ)
        environment["FASTDYN_MARKER_SETTLE_NS"] = str(plan["marker_settle_ns"])
        result = subprocess.run(
            command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
            env=environment, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"comparison failed ({result.returncode}); see {LOG}")

    raw = json.loads(RAW.read_text())
    o3 = raw["paired_comparisons"]["o3"]
    # The benchmark reports Firm/O3 because Firm is the frozen reference variant.
    cpu = o3["target_cpu_ns"]
    interval = cpu["bootstrap_95_percent"]
    parity = bool(
        raw["validation"]["all_executions_ok"]
        and interval[0] >= 0.95
        and interval[1] <= 1.05
    )
    report = {
        "schema_version": 1,
        "status": "comparable_to_o3" if parity else "pipeline_improvement_required",
        "all_exact_work_and_trace_checks_match": raw["validation"]["all_executions_ok"],
        "firm_bolt_cpu_cost_over_o3": cpu,
        "o3_speed_over_firm_bolt": {
            "median_ratio": 1.0 / cpu["median_ratio"],
            "bootstrap_95_percent": invert(interval),
        },
        "firm_bolt_over_o0_reference": raw["paired_comparisons"][
            "o0_reference"
        ],
        "parity_rule": plan["parity_rule"],
        "marker_settle_ns": plan["marker_settle_ns"],
        "claim_boundary": plan["claim_boundary"],
        "artifacts": {
            "firm_bolt_sha256": sha256(FIRM),
            "o3_sha256": sha256(O3),
            "o0_reference_sha256": sha256(O0),
            "preload_sha256": sha256(PRELOAD),
            "raw_samples_sha256": sha256(RAW),
        },
    }
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if parity else 2


if __name__ == "__main__":
    raise SystemExit(main())
