#!/usr/bin/env python3
"""Aggregate frozen held-out SmartSpeaker Firm-BOLT paired blocks."""

from __future__ import annotations

import hashlib
import json
import random
import statistics
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
PLAN = RUN / "firm-bolt-width-complete-cmp-precision-plan.json"
HELDOUT = RUN / "firm-bolt-width-complete-cmp-heldout.json"
PRECISION = RUN / "firm-bolt-width-complete-cmp-precision-100.json"
OUTPUT = RUN / "firm-bolt-width-complete-cmp-aggregate-report.json"
LABEL = "firm_bolt_width_complete_cmp"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def paired_ratios(raw: dict[str, Any], metric: str) -> list[float]:
    left = {
        int(item["block"]): item
        for item in raw["variants"]["stock_bolt"]["samples"]
    }
    right = {
        int(item["block"]): item
        for item in raw["variants"][LABEL]["samples"]
    }
    assert set(left) == set(right)
    return [
        float(left[block][metric]) / float(right[block][metric])
        for block in sorted(left)
    ]


def summarize(values: list[float], seed: int, resamples: int) -> dict[str, Any]:
    generator = random.Random(seed)
    medians = [
        statistics.median(
            values[generator.randrange(len(values))] for _ in values
        )
        for _ in range(resamples)
    ]
    return {
        "median_ratio": statistics.median(values),
        "bootstrap_95_percent": [
            percentile(medians, 0.025), percentile(medians, 0.975)
        ],
        "paired_ratios": values,
        "bootstrap_resamples": resamples,
    }


def main() -> int:
    plan = json.loads(PLAN.read_text())
    if sha256(HELDOUT) != plan["heldout_raw_sha256"]:
        raise RuntimeError("held-out evidence differs from frozen plan")
    if OUTPUT.exists():
        raise RuntimeError(f"output exists: {OUTPUT}")
    heldout = json.loads(HELDOUT.read_text())
    precision = json.loads(PRECISION.read_text())
    if not heldout["validation"]["all_executions_ok"]:
        raise RuntimeError("held-out behavior check failed")
    if not precision["validation"]["all_executions_ok"]:
        raise RuntimeError("precision behavior check failed")
    metrics = ("target_cpu_ns", "wall_ns", "process_cpu_ns")
    comparison = {
        metric: summarize(
            paired_ratios(heldout, metric) + paired_ratios(precision, metric),
            plan["aggregate_bootstrap_seed"] + index,
            plan["bootstrap_resamples"],
        )
        for index, metric in enumerate(metrics)
    }
    accepted = bool(
        comparison["target_cpu_ns"]["bootstrap_95_percent"][0] > 1.0
        and comparison["wall_ns"]["bootstrap_95_percent"][0] >= 1.0
    )
    report = {
        "schema_version": 1,
        "status": "held_out_accepted" if accepted else "held_out_rejected",
        "all_exact_work_and_trace_checks_match": True,
        "blocks": len(comparison["target_cpu_ns"]["paired_ratios"]),
        "training_blocks_excluded": 30,
        "stock_bolt_over_firm_bolt": comparison,
        "useful_effect_cleared": (
            comparison["target_cpu_ns"]["bootstrap_95_percent"][0] > 1.005
        ),
        "candidate_sha256": plan["candidate_sha256"],
        "heldout_raw_sha256": sha256(HELDOUT),
        "precision_raw_sha256": sha256(PRECISION),
        "plan_sha256": sha256(PLAN),
    }
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
