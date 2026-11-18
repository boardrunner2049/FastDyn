#!/usr/bin/env python3
"""Extend and aggregate the unchanged full-FOP exact-work comparison."""

from __future__ import annotations

import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
SCHEDULE = ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17"
PLAN = RUN / "fop-full-precision-extension-plan.json"
ORIGINAL = RUN / "fop-full-precision-100.json"
EXTRA = RUN / "fop-full-precision-extra-200.json"
LOG = RUN / "fop-full-precision-extra-200.log"
AGGREGATE = RUN / "fop-full-precision-aggregate-300.json"
REPORT = RUN / "fop-full-precision-aggregate-report.json"
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
STOCK = RUN / "SmartSpeaker.marker_stock"
CANDIDATE = RUN / "SmartSpeaker.marker_fop"
PRELOAD = SCHEDULE / "lemix_mmio_marker_schedule.so"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def compare(
    reference: list[dict[str, Any]], candidate: list[dict[str, Any]],
    metric: str, seed: int,
) -> dict[str, Any]:
    left = {int(item["block"]): item for item in reference}
    right = {int(item["block"]): item for item in candidate}
    ratios = [
        float(left[block][metric]) / float(right[block][metric])
        for block in sorted(left)
    ]
    generator = random.Random(seed)
    medians = [
        statistics.median(
            ratios[generator.randrange(len(ratios))] for _ in ratios
        )
        for _ in range(100_000)
    ]
    return {
        "ratio_definition": f"stock BOLT {metric} / full FOP {metric}",
        "median_ratio": statistics.median(ratios),
        "bootstrap_95_percent": [
            percentile(medians, 0.025), percentile(medians, 0.975)
        ],
        "paired_ratios": ratios,
        "bootstrap_resamples": 100_000,
    }


def main() -> int:
    plan = json.loads(PLAN.read_text())
    if plan["status"] != "frozen_before_precision_extension":
        raise RuntimeError("extension plan is not frozen")
    for path, expected in (
        (STOCK, plan["stock_bolt_sha256"]),
        (CANDIDATE, plan["candidate_sha256"]),
        (ORIGINAL, plan["original_raw_sha256"]),
    ):
        if sha256(path) != expected:
            raise RuntimeError(f"frozen artifact differs: {path}")
    if any(path.exists() for path in (EXTRA, LOG, AGGREGATE, REPORT)):
        raise RuntimeError("precision-extension output already exists")

    command = [
        "taskset", "-c", "24,25", sys.executable, str(BENCHMARK),
        "--variant", f"stock_bolt={STOCK}",
        "--variant", f"fop_full={CANDIDATE}",
        "--preload", str(PRELOAD),
        "--work-units", str(plan["completed_units_per_execution"]),
        "--tick-address", "0x495670",
        "--samples", str(plan["additional_blocks"]),
        "--seed", str(plan["additional_seed"]),
        "--output", str(EXTRA),
    ]
    environment = dict(os.environ)
    environment["FASTDYN_MARKER_SETTLE_NS"] = str(plan["marker_settle_ns"])
    with LOG.open("w") as stream:
        result = subprocess.run(
            command, cwd=ROOT, env=environment, stdout=stream,
            stderr=subprocess.STDOUT, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"extension failed ({result.returncode}); see {LOG}")

    original = json.loads(ORIGINAL.read_text())
    extra = json.loads(EXTRA.read_text())
    if original["validation"] != extra["validation"]:
        raise RuntimeError("precision extension changed exact work or trace identity")
    samples: dict[str, list[dict[str, Any]]] = {}
    for name in ("stock_bolt", "fop_full"):
        first = [dict(item) for item in original["variants"][name]["samples"]]
        second = [dict(item) for item in extra["variants"][name]["samples"]]
        offset = plan["original_blocks"]
        for item in second:
            item["block"] = int(item["block"]) + offset
            item["sequence"] = int(item["sequence"]) + offset * 2
        samples[name] = first + second
    metrics = {
        metric: compare(
            samples["stock_bolt"], samples["fop_full"], metric,
            plan["additional_seed"] + index,
        )
        for index, metric in enumerate(
            ("target_cpu_ns", "wall_ns", "process_cpu_ns"), 1
        )
    }
    aggregate = {
        "schema_version": 1,
        "status": "valid_aggregate_exact_work_comparison",
        "blocks": len(samples["stock_bolt"]),
        "source_evidence": {
            "original": {"blocks": plan["original_blocks"], "sha256": sha256(ORIGINAL)},
            "extension": {"blocks": plan["additional_blocks"], "sha256": sha256(EXTRA)},
        },
        "validation": original["validation"],
        "samples": samples,
        "stock_bolt_over_full_fop": metrics,
    }
    AGGREGATE.write_text(json.dumps(aggregate, indent=2, sort_keys=True) + "\n")
    accepted = bool(
        metrics["target_cpu_ns"]["bootstrap_95_percent"][0] > 1.005
        and metrics["wall_ns"]["bootstrap_95_percent"][0] >= 1.0
    )
    report = {
        "schema_version": 1,
        "status": "accepted" if accepted else "not_profitable",
        "all_exact_work_and_trace_checks_match": original["validation"][
            "all_executions_ok"
        ],
        "blocks": aggregate["blocks"],
        "stock_bolt_over_full_fop": metrics,
        "advance_rule": plan["advance_rule"],
        "candidate_sha256": sha256(CANDIDATE),
        "aggregate_sha256": sha256(AGGREGATE),
        "selection_was_not_retuned": True,
    }
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
