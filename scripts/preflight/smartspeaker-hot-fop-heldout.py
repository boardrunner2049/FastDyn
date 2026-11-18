#!/usr/bin/env python3
"""Freeze and finalize the unchanged SmartSpeaker hot-FOP held-out run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
PRECISION = ROOT / "build/preflight/lemix-smartspeaker-policy-precision-2026-11-17"
DEFAULT_OUTPUT = ROOT / "build/preflight/lemix-smartspeaker-hot-fop-2026-11-17"
STOCK = BASELINE / "binaries/SmartSpeaker.L1"
HOT_FOP = BASELINE / "binaries/SmartSpeaker.FH"
PRELOAD = ROOT / "build/preflight/lemix-smartspeaker/lemix_mmio_map.so"


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode() + b"\n"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: Any) -> None:
    path.write_bytes(canonical(value))


def freeze(output: Path) -> None:
    primary = BASELINE / "samples.jsonl"
    precision = PRECISION / "report.json"
    for path in (STOCK, HOT_FOP, PRELOAD, primary, precision):
        if not path.is_file():
            raise SystemExit(f"missing selection evidence: {path}")
    identity = {
        "schema_version": 1,
        "status": "frozen_before_hot_fop_held_out_measurement",
        "choice": "hot_fop",
        "baseline": {"id": "stock_bolt", "path": str(STOCK), "sha256": sha256(STOCK)},
        "candidate": {"id": "hot_fop", "path": str(HOT_FOP), "sha256": sha256(HOT_FOP)},
        "environment": {
            "preload": str(PRELOAD),
            "preload_sha256": sha256(PRELOAD),
            "interrupt_period_ticks": 10,
            "fidelity": "zero-backed MMIO addressability only; no device model",
        },
        "selection_evidence": {
            "primary_samples_sha256": sha256(primary),
            "fallback_counterexample_report_sha256": sha256(precision),
            "prior_stock_over_hot_fop_median": 1.0037504526086156,
            "prior_stock_over_hot_fop_bootstrap_95_percent": [
                0.9651045577789397, 1.0370402106348267,
            ],
            "prior_effect_status": "inconclusive",
            "hot_fop_static_optimized_loads": 39,
            "hot_fop_static_deleted_loads": 13,
            "hot_fop_share_of_full_profiled_optimized_load_frequency": 0.9853,
            "rationale": (
                "fewest existing FOP edits retaining nearly all profiled FOP opportunity; "
                "no held-out hot-FOP result was inspected"
            ),
        },
        "measurement": {
            "samples": 30,
            "duration_seconds": 30.0,
            "seed": 20261125,
            "affinity_cpus": "24,25",
            "trace_prefix_events": 400,
            "minimum_useful_ratio": 1.005,
        },
        "claim_boundaries": {
            "paced_cpu_cost_only": True,
            "whole_application_capacity": False,
            "original_mcu_speedup": False,
            "fastdyn_speedup": False,
        },
    }
    document = {
        **identity,
        "selection_id": hashlib.sha256(canonical(identity)).hexdigest(),
    }
    write(output / "selection.json", document)


def summarize(output: Path) -> None:
    selection_path = output / "selection.json"
    raw_path = output / "samples.json"
    selection = json.loads(selection_path.read_text())
    raw = json.loads(raw_path.read_text())
    expected = selection["measurement"]
    method = raw["method"]
    if (
        method["samples_per_variant"] != expected["samples"]
        or method["duration_seconds"] != expected["duration_seconds"]
        or method["order_seed"] != expected["seed"]
        or method["reference"] != "stock_bolt"
    ):
        raise SystemExit("measurement does not match frozen selection")
    expected_variants = {
        "stock_bolt": selection["baseline"],
        "hot_fop": selection["candidate"],
    }
    trace_hashes: set[str] = set()
    for name, expected_variant in expected_variants.items():
        variant = raw["variants"][name]
        if variant["sha256"] != expected_variant["sha256"] or not variant["all_ok"]:
            raise SystemExit(f"invalid or mismatched variant: {name}")
        for sample in variant["samples"]:
            if sample["interrupt_trace_prefix_events"] != expected["trace_prefix_events"]:
                raise SystemExit(f"short trace prefix: {name}")
            trace_hashes.add(sample["interrupt_trace_prefix_sha256"])
    if len(trace_hashes) != 1:
        raise SystemExit("interrupt-selection trace prefix differs")
    paired = raw["paired_comparisons"]["hot_fop"]
    interval = paired["bootstrap_95_percent"]
    if interval[0] >= expected["minimum_useful_ratio"]:
        decision = "accept_hot_fop"
    elif interval[1] < 1.0:
        decision = "reject_hot_fop_regression"
    else:
        decision = "retain_stock_bolt_inconclusive_or_below_gate"
    report = {
        "schema_version": 1,
        "status": "held_out_hot_fop_evaluation_complete",
        "selection_sha256": sha256(selection_path),
        "samples_sha256": sha256(raw_path),
        "decision": decision,
        "stock_bolt_over_hot_fop_cpu_per_marker": paired,
        "interrupt_trace_prefix": {
            "events": expected["trace_prefix_events"],
            "sha256": next(iter(trace_hashes)),
            "equal_across_all_samples": True,
        },
        "claims": selection["claim_boundaries"],
    }
    write(output / "report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stage", choices=("freeze", "summarize"), required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.stage == "freeze":
        if (output / "selection.json").exists():
            raise SystemExit("selection already exists")
        freeze(output)
    else:
        summarize(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
