#!/usr/bin/env python3
"""Run the frozen high-precision stock-BOLT/Firm-BOLT replication."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
WRAPPER = ROOT / "build/preflight/bolt-firmware-wrapper-arithmetic-rev-2-2026-11-17"
SCHEDULE = ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17"
PLAN = RUN / "firm-bolt-arithmetic-precision-plan.json"
RAW = RUN / "firm-bolt-arithmetic-precision-100.json"
LOG = RUN / "firm-bolt-arithmetic-precision-100.log"
REPORT = RUN / "firm-bolt-arithmetic-precision-report.json"
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
PRELOAD = SCHEDULE / "lemix_mmio_marker_schedule.so"
STOCK = RUN / "SmartSpeaker.marker_stock"
CANDIDATE = WRAPPER / "SmartSpeaker.bolt-firmware-candidate"
CANDIDATE_LABEL = "firm_bolt_arithmetic"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    plan = json.loads(PLAN.read_text())
    journal = RAW.with_suffix(".journal.jsonl")
    if sha256(STOCK) != plan["stock_bolt_sha256"]:
        raise RuntimeError("stock BOLT artifact differs from frozen plan")
    if sha256(CANDIDATE) != plan["candidate_sha256"]:
        raise RuntimeError("Firm-BOLT candidate differs from frozen plan")
    if any(path.exists() for path in (RAW, REPORT)):
        raise RuntimeError("precision result already exists")
    command = [
        "taskset", "-c", "24,25", sys.executable, str(BENCHMARK),
        "--variant", f"stock_bolt={STOCK}",
        "--variant", f"{CANDIDATE_LABEL}={CANDIDATE}",
        "--preload", str(PRELOAD),
        "--work-units", str(plan["completed_units_per_execution"]),
        "--tick-address", "0x495670", "--samples", str(plan["paired_blocks"]),
        "--seed", str(plan["seed"]), "--journal", str(journal),
        "--output", str(RAW),
    ]
    resume = journal.exists()
    if resume:
        command.append("--resume")
    environment = dict(os.environ)
    environment["FASTDYN_MARKER_SETTLE_NS"] = str(plan["marker_settle_ns"])
    with LOG.open("a" if resume else "w") as stream:
        result = subprocess.run(
            command, cwd=ROOT, env=environment, stdout=stream,
            stderr=subprocess.STDOUT, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"precision run failed ({result.returncode}); see {LOG}")
    raw = json.loads(RAW.read_text())
    comparison = raw["paired_comparisons"][CANDIDATE_LABEL]
    selected = bool(
        raw["validation"]["all_executions_ok"]
        and comparison["target_cpu_ns"]["bootstrap_95_percent"][0] > 1.005
        and comparison["wall_ns"]["bootstrap_95_percent"][0] >= 1.0
    )
    report = {
        "schema_version": 1,
        "status": "selected_for_held_out" if selected else "not_profitable",
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
    return 0 if selected else 2


if __name__ == "__main__":
    raise SystemExit(main())
