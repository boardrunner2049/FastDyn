#!/usr/bin/env python3
"""Run the frozen 100-block exact-work precision extension for hot FOP."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
SCHEDULE = ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17"
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
PLAN = RUN / "fop-hot-precision-plan.json"
OUTPUT = RUN / "fop-hot-precision-100.json"
LOG = RUN / "fop-hot-precision-100.log"
REPORT = RUN / "fop-hot-precision-report.json"
STOCK = RUN / "SmartSpeaker.marker_stock"
CANDIDATE = RUN / "SmartSpeaker.marker_fop_hot"
PRELOAD = SCHEDULE / "lemix_mmio_marker_schedule.so"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    plan = json.loads(PLAN.read_text())
    if plan["status"] != "frozen_before_precision_extension":
        raise RuntimeError("precision plan is not frozen")
    if sha256(CANDIDATE) != plan["candidate_sha256"]:
        raise RuntimeError("hot-FOP candidate differs from the frozen plan")
    if OUTPUT.exists() or LOG.exists() or REPORT.exists():
        raise RuntimeError("precision-extension output already exists")
    command = [
        "taskset", "-c", "24,25", sys.executable, str(BENCHMARK),
        "--variant", f"stock_bolt={STOCK}",
        "--variant", f"fop_hot={CANDIDATE}",
        "--preload", str(PRELOAD),
        "--work-units", str(plan["completed_units_per_execution"]),
        "--tick-address", "0x495670",
        "--samples", str(plan["paired_blocks"]),
        "--seed", str(plan["seed"]),
        "--output", str(OUTPUT),
    ]
    environment = dict(os.environ)
    environment["FASTDYN_MARKER_SETTLE_NS"] = str(plan["marker_settle_ns"])
    with LOG.open("w") as stream:
        result = subprocess.run(
            command, cwd=ROOT, env=environment, stdout=stream,
            stderr=subprocess.STDOUT, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"precision benchmark failed: {result.returncode}; see {LOG}")
    evidence = json.loads(OUTPUT.read_text())
    metrics = evidence["paired_comparisons"]["fop_hot"]
    accepted = bool(
        evidence["validation"]["all_executions_ok"]
        and metrics["target_cpu_ns"]["bootstrap_95_percent"][0] > 1.005
        and metrics["wall_ns"]["bootstrap_95_percent"][0] >= 1.0
    )
    report = {
        "schema_version": 1,
        "status": "accepted" if accepted else "not_profitable",
        "candidate": "fop_hot",
        "candidate_sha256": sha256(CANDIDATE),
        "stock_bolt_sha256": sha256(STOCK),
        "all_exact_work_and_trace_checks_match": evidence["validation"]["all_executions_ok"],
        "stock_bolt_over_candidate": metrics,
        "raw_evidence_sha256": sha256(OUTPUT),
        "advance_rule": plan["advance_rule"],
        "selection_was_not_retuned": plan["selection_was_not_retuned"],
    }
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
