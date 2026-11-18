#!/usr/bin/env python3
"""Freeze the LEMIX queue deployment choice from training measurements."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WRAPPER = ROOT / "build/preflight/lemix-queue/bolt-firmware-wrapper"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wrapper", type=Path, default=DEFAULT_WRAPPER)
    parser.add_argument("--minimum-useful-ratio", type=float, default=1.005)
    args = parser.parse_args()
    wrapper = args.wrapper.resolve()
    training_path = wrapper / "policy-training-2026-11-17.json"
    training = json.loads(training_path.read_text())
    comparisons = training["paired_comparisons"]

    eligible = ["reference"]
    for candidate in ("stock_bolt", "full_fop", "compact_fop"):
        result = comparisons[candidate]["cpu_ns"]
        if float(result["bootstrap_95_percent"][0]) >= args.minimum_useful_ratio:
            eligible.append(candidate)
    relocatable = max(
        eligible,
        key=lambda name: 1.0 if name == "reference" else float(
            comparisons[name]["cpu_ns"]["median_ratio"]
        ),
    )
    compact = comparisons["compact_fop"]["cpu_ns"]
    fixed_layout = (
        "compact_fop"
        if float(compact["bootstrap_95_percent"][0])
        >= args.minimum_useful_ratio
        else "reference"
    )
    report = {
        "schema_version": 1,
        "status": "frozen_before_held_out_measurement",
        "classification": "training_only_firmware_wrapper_policy_selection",
        "workload_id": "lemix_queue_two_million_messages",
        "minimum_useful_cpu_ratio": args.minimum_useful_ratio,
        "training": {
            "paired_blocks": training["method"]["samples_per_variant"],
            "randomization_seed": training["method"][
                "interleaved_randomization_seed"
            ],
            "report_sha256": sha256(training_path),
            "cpu_results": {
                name: comparisons[name]["cpu_ns"]
                for name in ("stock_bolt", "full_fop", "compact_fop")
            },
        },
        "held_out": {
            "randomization_seed": 20261121,
            "paired_blocks": 30,
            "results_inspected_before_selection": False,
        },
        "choice": {
            "relocatable": relocatable,
            "fixed_layout": fixed_layout,
        },
        "choices": {
            "lemix_queue_two_million_messages": {
                "relocatable_policy": relocatable,
                "fixed_layout_policy": fixed_layout,
            },
        },
    }
    output = wrapper / "queue-policy-selection.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "output": str(output), "sha256": sha256(output),
        "choice": report["choice"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
