#!/usr/bin/env python3
"""Summarize the frozen, held-out LEMIX queue BOLT policy result."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WRAPPER = ROOT / "build/preflight/lemix-queue/bolt-firmware-wrapper"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def paired_ratio(
    timing: dict[str, Any], numerator: str, denominator: str, seed: int
) -> dict[str, Any]:
    left = {int(x["block"]): float(x["cpu_ns"]) for x in timing["variants"][numerator]["samples"]}
    right = {int(x["block"]): float(x["cpu_ns"]) for x in timing["variants"][denominator]["samples"]}
    blocks = sorted(set(left) & set(right))
    ratios = [left[index] / right[index] for index in blocks]
    generator = random.Random(seed)
    medians = [
        statistics.median(ratios[generator.randrange(len(ratios))] for _ in ratios)
        for _ in range(100_000)
    ]
    return {
        "ratio_definition": f"{numerator}_cpu_ns/{denominator}_cpu_ns",
        "paired_blocks": len(blocks),
        "median_ratio": statistics.median(ratios),
        "bootstrap_95_percent": [
            percentile(medians, 0.025), percentile(medians, 0.975)
        ],
        "bootstrap_resamples": 100_000,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wrapper", type=Path, default=DEFAULT_WRAPPER)
    args = parser.parse_args()
    wrapper = args.wrapper.resolve()
    selection_path = wrapper / "queue-policy-selection.json"
    training_path = wrapper / "policy-training-2026-11-17.json"
    held_out_path = wrapper / "policy-held-out-2026-11-17.json"
    selection = json.loads(selection_path.read_text())
    training = json.loads(training_path.read_text())
    held_out = json.loads(held_out_path.read_text())
    if selection["training"]["report_sha256"] != sha256(training_path):
        raise RuntimeError("queue selection does not bind the training report")
    if selection["held_out"]["results_inspected_before_selection"]:
        raise RuntimeError("queue selection was not frozen before held-out timing")
    if held_out["method"]["interleaved_randomization_seed"] != selection[
        "held_out"
    ]["randomization_seed"]:
        raise RuntimeError("held-out randomization seed differs from frozen selection")
    if held_out["method"]["samples_per_variant"] != selection["held_out"][
        "paired_blocks"
    ]:
        raise RuntimeError("held-out block count differs from frozen selection")
    selected_name = selection["choice"]["relocatable"]
    if held_out["variants"]["relocatable_policy"]["sha256"] != training[
        "variants"
    ][selected_name]["sha256"]:
        raise RuntimeError("held-out policy image differs from the training choice")
    stock_over_policy = paired_ratio(
        held_out, "stock_bolt", "relocatable_policy", 20261122
    )
    reference_over_policy = held_out["paired_comparisons"]["relocatable_policy"][
        "cpu_ns"
    ]
    outputs_match = all(
        bool(item["all_outputs_match"])
        for item in held_out["variants"].values()
    )
    report = {
        "schema_version": 1,
        "status": "held_out_policy_evaluation_complete",
        "classification": "supported-host fixed-work queue BOLT wrapper policy",
        "selection": {
            "path": str(selection_path),
            "sha256": sha256(selection_path),
            "frozen_before_held_out_measurement": True,
            "choice": selection["choice"],
            "minimum_useful_cpu_ratio": selection["minimum_useful_cpu_ratio"],
        },
        "training": {
            "path": str(training_path),
            "sha256": sha256(training_path),
        },
        "held_out": {
            "path": str(held_out_path),
            "sha256": sha256(held_out_path),
            "paired_blocks": held_out["method"]["samples_per_variant"],
            "randomization_seed": held_out["method"][
                "interleaved_randomization_seed"
            ],
            "reference_over_policy_cpu": reference_over_policy,
            "stock_bolt_over_policy_cpu": stock_over_policy,
            "all_outputs_match": outputs_match,
            "semantic_output": held_out["method"]["semantic_output"],
        },
        "artifacts": {
            "reference": {
                key: held_out["variants"]["reference"][key]
                for key in ("path", "sha256", "file_bytes")
            },
            "full_relocatable": {
                key: training["variants"]["full_fop"][key]
                for key in ("path", "sha256", "file_bytes")
            },
            "compact_fixed_layout": {
                key: training["variants"]["compact_fop"][key]
                for key in ("path", "sha256", "file_bytes")
            },
        },
        "workloads": {
            "lemix_queue_two_million_messages": {
                "training_choice": {
                    "relocatable": selection["choice"]["relocatable"],
                    "fixed_layout": selection["choice"]["fixed_layout"],
                },
                "all_outputs_match": outputs_match,
            },
        },
        "claims": {
            "faster_than_reference": (
                outputs_match
                and float(reference_over_policy["bootstrap_95_percent"][0]) > 1.0
            ),
            "faster_than_frozen_stock_bolt": (
                outputs_match
                and float(stock_over_policy["bootstrap_95_percent"][0]) > 1.0
            ),
            "original_cortex_m_speedup": False,
            "fastdyn_speedup": False,
        },
    }
    output = wrapper / "queue-policy-held-out-report.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "output": str(output), "sha256": sha256(output),
        "claims": report["claims"],
        "stock_bolt_over_policy_cpu": stock_over_policy,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
