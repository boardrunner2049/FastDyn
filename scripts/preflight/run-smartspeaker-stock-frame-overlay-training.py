#!/usr/bin/env python3
"""Run the frozen exact-stock-layout ABI frame-overlay comparison."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-stock-frame-overlay-2026-11-17"
MARKER_RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
SCHEDULE = ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17"
SELECTION = RUN / "selection.json"
RAW = RUN / "training-100.json"
JOURNAL = RUN / "training-100.journal.jsonl"
LOG = RUN / "training-100.log"
REPORT = RUN / "training-report.json"
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
PRELOAD = SCHEDULE / "lemix_mmio_marker_schedule.so"
STOCK = MARKER_RUN / "SmartSpeaker.marker_stock"
CANDIDATE = RUN / "SmartSpeaker.marker_stock.checked-frame-overlay"
LABEL = "checked_frame_overlay"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    selection = json.loads(SELECTION.read_text())
    plan = selection["measurement_plan"]
    expected_stock = selection["inputs"]["stock_bolt"]["sha256"]
    expected_candidate = selection["output"]["sha256"]
    if sha256(STOCK) != expected_stock:
        raise RuntimeError("stock BOLT artifact differs from frozen selection")
    if sha256(CANDIDATE) != expected_candidate:
        raise RuntimeError("frame overlay differs from frozen selection")
    if REPORT.exists():
        raise RuntimeError("training report already exists")

    command = [
        "taskset", "-c", "24,25", sys.executable, str(BENCHMARK),
        "--variant", f"stock_bolt={STOCK}",
        "--variant", f"{LABEL}={CANDIDATE}",
        "--preload", str(PRELOAD),
        "--work-units", str(plan["fixed_work_units_per_execution"]),
        "--tick-address", "0x495670",
        "--samples", str(plan["training_blocks"]),
        "--seed", str(plan["seed"]),
        "--journal", str(JOURNAL),
        "--output", str(RAW),
    ]
    if not RAW.exists():
        resume = JOURNAL.exists()
        if resume:
            command.append("--resume")
        environment = dict(os.environ)
        environment["FASTDYN_MARKER_SETTLE_NS"] = "100000"
        with LOG.open("a" if resume else "w") as stream:
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"training run failed ({result.returncode}); see {LOG}"
            )

    raw = json.loads(RAW.read_text())
    comparison = raw["paired_comparisons"][LABEL]
    exact = bool(raw["validation"]["all_executions_ok"])
    target_gate = bool(
        comparison["target_cpu_ns"]["bootstrap_95_percent"][0]
        > float(plan["minimum_useful_target_cpu_ratio"])
    )
    wall_gate = bool(
        comparison["wall_ns"]["bootstrap_95_percent"][0]
        >= float(plan["wall_non_regression_ratio"])
    )
    selected = exact and target_gate and wall_gate
    report = {
        "schema_version": 1,
        "status": "selected_for_held_out" if selected else "training_rejected",
        "candidate": {
            "path": str(CANDIDATE),
            "sha256": sha256(CANDIDATE),
            "classification": selection["classification"],
            "selected_function_count": selection["cohort"]["selected_count"],
            "selected_profile_entry_count": selection["cohort"][
                "selected_profile_entry_count"
            ],
            "saved_live_bytes": selection["cohort"]["saved_live_bytes"],
        },
        "all_exact_work_and_trace_checks_match": exact,
        "selection_gates": {
            "minimum_useful_target_cpu_ratio": plan[
                "minimum_useful_target_cpu_ratio"
            ],
            "target_cpu_gate_cleared": target_gate,
            "wall_non_regression_ratio": plan["wall_non_regression_ratio"],
            "wall_non_regression_gate_cleared": wall_gate,
        },
        "stock_bolt_over_checked_frame_overlay": comparison,
        "raw": {"path": str(RAW), "sha256": sha256(RAW)},
        "selection": {"path": str(SELECTION), "sha256": sha256(SELECTION)},
        "command": command,
        "environment": {"FASTDYN_MARKER_SETTLE_NS": "100000"},
        "claim_boundaries": selection["claim_boundaries"],
    }
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if selected else 2


if __name__ == "__main__":
    raise SystemExit(main())
