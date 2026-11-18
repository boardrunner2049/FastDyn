#!/usr/bin/env python3
"""Aggregate the frozen 130-block ABI trivial-frame selection evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
HELPERS = ROOT / "scripts/preflight/aggregate-smartspeaker-firm-bolt-copy-cleanup.py"
PLAN = RUN / "firm-bolt-trivial-frame-abi-precision-plan.json"
TRAINING = RUN / "firm-bolt-trivial-frame-abi-training-30.json"
PRECISION = RUN / "firm-bolt-trivial-frame-abi-precision-100.json"
OUTPUT = RUN / "firm-bolt-trivial-frame-abi-selection-aggregate-130.json"
LABEL = "firm_bolt_trivial_frame_abi"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    spec = importlib.util.spec_from_file_location("aggregate_helpers", HELPERS)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load aggregate helpers: {HELPERS}")
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    helpers.LABEL = LABEL
    plan = json.loads(PLAN.read_text())
    if sha256(TRAINING) != plan["training_raw_sha256"]:
        raise RuntimeError("training evidence differs from frozen plan")
    if OUTPUT.exists():
        raise RuntimeError(f"output exists: {OUTPUT}")
    training = json.loads(TRAINING.read_text())
    precision = json.loads(PRECISION.read_text())
    if not training["validation"]["all_executions_ok"]:
        raise RuntimeError("training behavior check failed")
    if not precision["validation"]["all_executions_ok"]:
        raise RuntimeError("precision behavior check failed")
    comparison = {}
    for index, metric in enumerate(
        ("target_cpu_ns", "wall_ns", "process_cpu_ns")
    ):
        values = helpers.paired_ratios(training, metric)
        values += helpers.paired_ratios(precision, metric)
        comparison[metric] = helpers.summarize(
            values,
            plan["aggregate_bootstrap_seed"] + index,
            plan["bootstrap_resamples"],
        )
    target_lower = comparison["target_cpu_ns"]["bootstrap_95_percent"][0]
    wall_lower = comparison["wall_ns"]["bootstrap_95_percent"][0]
    selected = target_lower > 1.005 and wall_lower >= 1.0
    report = {
        "schema_version": 1,
        "status": "selected_for_held_out" if selected else "training_rejected",
        "all_exact_work_and_trace_checks_match": True,
        "blocks": len(comparison["target_cpu_ns"]["paired_ratios"]),
        "stock_bolt_over_firm_bolt": comparison,
        "useful_effect_cleared": target_lower > 1.005,
        "wall_non_regression_cleared": wall_lower >= 1.0,
        "candidate_sha256": plan["candidate_sha256"],
        "training_raw_sha256": sha256(TRAINING),
        "precision_raw_sha256": sha256(PRECISION),
        "plan_sha256": sha256(PLAN),
        "retuning": False,
        "early_stopping": False
    }
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if selected else 2


if __name__ == "__main__":
    raise SystemExit(main())
