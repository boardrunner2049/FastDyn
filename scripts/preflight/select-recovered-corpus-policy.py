#!/usr/bin/env python3
"""Freeze wrapper deployment choices from the firmware-corpus training run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = ROOT / "build/preflight/recovered-firmware-corpus-2026-11-16"
WORKLOADS = (
    "crc8", "crc8dvbs2", "dshotcrc", "vListInitialise",
    "vListInitialiseItem", "vListInsertEnd", "vListInsert",
    "uxListRemove", "combined_all",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--minimum-useful-ratio", type=float, default=1.005)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output or root / "bolt-firmware-wrapper/policy-selection.json"

    choices: dict[str, Any] = {}
    timing_hashes: dict[str, str] = {}
    for workload in WORKLOADS:
        path = root / "bolt-firmware-wrapper/timing" / f"{workload}.json"
        timing = load(path)
        timing_hashes[workload] = sha256(path)
        comparisons = timing["paired_comparisons"]

        eligible_relocatable = ["reference"]
        for candidate in ("full_fop", "compact_fop"):
            cpu = comparisons[candidate]["cpu_ns"]
            if float(cpu["bootstrap_95_percent"][0]) >= args.minimum_useful_ratio:
                eligible_relocatable.append(candidate)
        relocatable = max(
            eligible_relocatable,
            key=lambda name: 1.0 if name == "reference" else float(
                comparisons[name]["cpu_ns"]["median_ratio"]
            ),
        )

        compact_cpu = comparisons["compact_fop"]["cpu_ns"]
        fixed = (
            "compact_fop"
            if float(compact_cpu["bootstrap_95_percent"][0])
            >= args.minimum_useful_ratio
            else "reference"
        )
        choices[workload] = {
            "relocatable_policy": relocatable,
            "fixed_layout_policy": fixed,
            "training_cpu": {
                name: comparisons[name]["cpu_ns"]
                for name in ("stock_bolt", "full_fop", "compact_fop")
            },
        }

    report = {
        "schema_version": 1,
        "status": "frozen_before_held_out_measurement",
        "classification": "training_only_firmware_wrapper_policy_selection",
        "minimum_useful_cpu_ratio": args.minimum_useful_ratio,
        "training": {
            "program_seed": 11,
            "benchmark_randomization_seed": 20261116,
            "paired_blocks": 30,
            "timing_report_sha256": timing_hashes,
        },
        "held_out": {
            "program_seed": 13,
            "benchmark_randomization_seed": 20261117,
            "paired_blocks": 30,
            "results_inspected_before_selection": False,
        },
        "choices": choices,
        "claims": {
            "original_mcu_performance": False,
            "fastdyn_performance": False,
            "supported_host_fixed_work": True,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "output": str(output),
        "sha256": sha256(output),
        "relocatable": {
            key: value["relocatable_policy"] for key, value in choices.items()
        },
        "fixed": {
            key: value["fixed_layout_policy"] for key, value in choices.items()
        },
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
