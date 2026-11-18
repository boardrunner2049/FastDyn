#!/usr/bin/env python3
"""Run the frozen single-vCPU Firm-BOLT held-out comparison."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
WRAPPER = ROOT / "build/preflight/bolt-firmware-wrapper-width-complete-cmp-2026-11-17"
SCHEDULE = ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17"
PLAN = RUN / "firm-bolt-separated-vcpu-heldout-plan.json"
RAW = RUN / "firm-bolt-separated-vcpu-heldout-30.json"
LOG = RUN / "firm-bolt-separated-vcpu-heldout-30.log"
REPORT = RUN / "firm-bolt-separated-vcpu-heldout-report.json"
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
PRELOAD = SCHEDULE / "lemix_mmio_marker_schedule.so"
STOCK = RUN / "SmartSpeaker.marker_stock"
CANDIDATE = WRAPPER / "SmartSpeaker.bolt-qualified-compact"
LABEL = "firm_bolt_width_complete_cmp"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    plan = json.loads(PLAN.read_text())
    artifacts = {
        "stock_bolt": STOCK,
        "candidate": CANDIDATE,
        "preload": PRELOAD,
    }
    for name, path in artifacts.items():
        expected = plan["artifact_sha256"][name]
        if sha256(path) != expected:
            raise RuntimeError(f"{name} differs from frozen plan")
    if any(path.exists() for path in (RAW, LOG, REPORT)):
        raise RuntimeError("held-out output already exists")

    command = [
        "taskset", "-c", str(plan["target_cpu"]),
        sys.executable, str(BENCHMARK),
        "--variant", f"stock_bolt={STOCK}",
        "--variant", f"{LABEL}={CANDIDATE}",
        "--preload", str(PRELOAD),
        "--work-units", str(plan["completed_units_per_execution"]),
        "--tick-address", "0x495670",
        "--samples", str(plan["paired_blocks"]),
        "--seed", str(plan["seed"]),
        "--affinity-cpus", str(plan["target_cpu"]),
        "--output", str(RAW),
    ]
    environment = dict(os.environ)
    environment["FASTDYN_MARKER_SETTLE_NS"] = str(plan["marker_settle_ns"])
    environment["FASTDYN_FIXED_TICK_DRIVER_CPU"] = str(plan["driver_cpu"])
    with LOG.open("w") as stream:
        result = subprocess.run(
            command, cwd=ROOT, env=environment, stdout=stream,
            stderr=subprocess.STDOUT, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"held-out run failed ({result.returncode}); see {LOG}")

    raw = json.loads(RAW.read_text())
    comparison = raw["paired_comparisons"][LABEL]
    target_lower = comparison["target_cpu_ns"]["bootstrap_95_percent"][0]
    wall_lower = comparison["wall_ns"]["bootstrap_95_percent"][0]
    accepted = bool(
        raw["validation"]["all_executions_ok"]
        and target_lower > plan["acceptance_thresholds"]["target_cpu_ratio"]
        and wall_lower >= plan["acceptance_thresholds"]["wall_ratio"]
    )
    report = {
        "schema_version": 1,
        "status": "held_out_accepted" if accepted else "held_out_rejected",
        "measurement_environment": "single target vCPU with driver on a separate physical core",
        "calibration_excluded": True,
        "all_exact_work_and_trace_checks_match": raw["validation"][
            "all_executions_ok"
        ],
        "stock_bolt_over_firm_bolt": comparison,
        "candidate_sha256": sha256(CANDIDATE),
        "raw_sha256": sha256(RAW),
        "command": command,
    }
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
