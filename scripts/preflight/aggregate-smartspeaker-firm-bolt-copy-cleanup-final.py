#!/usr/bin/env python3
"""Aggregate the frozen 800 SmartSpeaker copy-cleanup held-out blocks."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
PLAN = RUN / "firm-bolt-copy-cleanup-heldout-final-precision-plan.json"
RAW_INPUTS = (
    RUN / "firm-bolt-copy-cleanup-heldout-100.json",
    RUN / "firm-bolt-copy-cleanup-heldout-precision-200.json",
    RUN / "firm-bolt-copy-cleanup-heldout-final-precision-retry-500.json",
)
OUTPUT = RUN / "firm-bolt-copy-cleanup-heldout-aggregate-800.json"
LABEL = "firm_bolt_copy_cleanup"
HELPERS = ROOT / "scripts/preflight/aggregate-smartspeaker-firm-bolt-copy-cleanup.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_helpers() -> Any:
    spec = importlib.util.spec_from_file_location("copy_cleanup_aggregate", HELPERS)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load aggregation helpers: {HELPERS}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if OUTPUT.exists():
        raise RuntimeError(f"output exists: {OUTPUT}")
    plan = json.loads(PLAN.read_text())
    expected = plan["input_raw_sha256"]
    for path in RAW_INPUTS[:2]:
        if sha256(path) != expected[path.name]:
            raise RuntimeError(f"prior held-out evidence differs: {path}")
    raw_inputs = [json.loads(path.read_text()) for path in RAW_INPUTS]
    if not all(raw["validation"]["all_executions_ok"] for raw in raw_inputs):
        raise RuntimeError("a held-out behavior check failed")

    helpers = load_helpers()
    metrics = ("target_cpu_ns", "wall_ns", "process_cpu_ns")
    comparison = {}
    for index, metric in enumerate(metrics):
        ratios = []
        for raw in raw_inputs:
            ratios.extend(helpers.paired_ratios(raw, metric))
        comparison[metric] = helpers.summarize(
            ratios,
            plan["aggregate_bootstrap_seed"] + index,
            plan["bootstrap_resamples"],
        )

    target_lower = comparison["target_cpu_ns"]["bootstrap_95_percent"][0]
    wall_lower = comparison["wall_ns"]["bootstrap_95_percent"][0]
    accepted = bool(target_lower > 1.005 and wall_lower >= 1.0)
    report = {
        "schema_version": 1,
        "status": "held_out_accepted" if accepted else "held_out_rejected",
        "all_exact_work_and_trace_checks_match": True,
        "blocks": len(comparison["target_cpu_ns"]["paired_ratios"]),
        "selection_blocks_excluded": 30,
        "selection_precision_blocks_excluded": 100,
        "stock_bolt_over_firm_bolt": comparison,
        "useful_effect_cleared": target_lower > 1.005,
        "wall_non_regression_cleared": wall_lower >= 1.0,
        "candidate_sha256": plan["candidate_sha256"],
        "raw_sha256": {path.name: sha256(path) for path in RAW_INPUTS},
        "plan_sha256": sha256(PLAN),
        "retuning": False,
        "early_stopping": False,
    }
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
