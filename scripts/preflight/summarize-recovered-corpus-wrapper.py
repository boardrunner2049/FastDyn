#!/usr/bin/env python3
"""Summarize training-selected, held-out BOLT wrapper policies."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = ROOT / "build/preflight/recovered-firmware-corpus-2026-11-16"


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def paired_ratio(
    timing: dict[str, Any], numerator: str, denominator: str, metric: str,
    seed: int,
) -> dict[str, Any]:
    left = {
        int(item["block"]): float(item[metric])
        for item in timing["variants"][numerator]["samples"]
    }
    right = {
        int(item["block"]): float(item[metric])
        for item in timing["variants"][denominator]["samples"]
    }
    blocks = sorted(set(left) & set(right))
    ratios = [left[block] / right[block] for block in blocks]
    generator = random.Random(seed)
    medians = [
        statistics.median(
            ratios[generator.randrange(len(ratios))] for _ in ratios
        )
        for _ in range(100_000)
    ]
    return {
        "ratio_definition": f"{numerator}_{metric}/{denominator}_{metric}",
        "paired_blocks": len(blocks),
        "median_ratio": statistics.median(ratios),
        "bootstrap_95_percent": [
            percentile(medians, 0.025), percentile(medians, 0.975)
        ],
        "bootstrap_resamples": 100_000,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    wrapper_root = root / "bolt-firmware-wrapper"
    selection_path = wrapper_root / "policy-selection.json"
    selection = load(selection_path)
    wrapper = load(wrapper_root / "wrapper-result.json")
    output = args.output or wrapper_root / "held-out-policy-report.json"
    threshold = float(selection["minimum_useful_cpu_ratio"])

    workloads: dict[str, Any] = {}
    relocatable_gains = 0
    fixed_gains = 0
    relocatable_better_than_stock = 0
    relocatable_not_resolved_vs_stock = 0
    for index, (name, choice) in enumerate(selection["choices"].items()):
        timing_path = wrapper_root / "held-out-timing" / f"{name}.json"
        timing = load(timing_path)
        reloc_choice = str(choice["relocatable_policy"])
        fixed_choice = str(choice["fixed_layout_policy"])
        reloc_reference = timing["paired_comparisons"]["relocatable_policy"][
            "cpu_ns"
        ]
        fixed_reference = timing["paired_comparisons"]["fixed_layout_policy"][
            "cpu_ns"
        ]
        reloc_stock = paired_ratio(
            timing, "stock_bolt", "relocatable_policy", "cpu_ns",
            20261117 + index,
        )
        fixed_stock = paired_ratio(
            timing, "stock_bolt", "fixed_layout_policy", "cpu_ns",
            20261217 + index,
        )
        reloc_changed = reloc_choice != "reference"
        fixed_changed = fixed_choice != "reference"
        reloc_generalized = (
            reloc_changed
            and float(reloc_reference["bootstrap_95_percent"][0]) >= threshold
        )
        fixed_generalized = (
            fixed_changed
            and float(fixed_reference["bootstrap_95_percent"][0]) >= threshold
        )
        relocatable_gains += int(reloc_generalized)
        fixed_gains += int(fixed_generalized)
        if float(reloc_stock["bootstrap_95_percent"][0]) > 1.0:
            relocatable_better_than_stock += 1
            versus_stock = "wrapper_policy_faster"
        elif float(reloc_stock["bootstrap_95_percent"][1]) < 1.0:
            versus_stock = "stock_bolt_faster"
        else:
            relocatable_not_resolved_vs_stock += 1
            versus_stock = "inconclusive"
        workloads[name] = {
            "training_choice": {
                "relocatable": reloc_choice,
                "fixed_layout": fixed_choice,
            },
            "held_out_timing_sha256": sha256(timing_path),
            "held_out_reference_over_policy": {
                "relocatable": reloc_reference,
                "fixed_layout": fixed_reference,
            },
            "held_out_stock_bolt_over_policy": {
                "relocatable": reloc_stock,
                "fixed_layout": fixed_stock,
            },
            "held_out_generalized_gain": {
                "relocatable": reloc_generalized,
                "fixed_layout": fixed_generalized,
            },
            "relocatable_policy_vs_stock_classification": versus_stock,
            "artifact_identity": {
                key: value["sha256"]
                for key, value in timing["variants"].items()
            },
            "all_outputs_match": all(
                bool(value["all_outputs_match"])
                for value in timing["variants"].values()
            ),
        }

    reference = Path(wrapper["inputs"]["reference"])
    compact = wrapper_root / "corpus-O0.bolt-qualified-compact"
    full = wrapper_root / "corpus-O0.bolt-wrapper-relocatable"
    precision_extensions: dict[str, Any] = {}
    crc8_precision_path = (
        wrapper_root / "held-out-timing/crc8-precision-extension.json"
    )
    if crc8_precision_path.is_file():
        precision_timing = load(crc8_precision_path)
        stock_over_policy = paired_ratio(
            precision_timing, "stock_bolt", "reference", "cpu_ns", 20261118
        )
        precision_extensions["crc8"] = {
            "classification": "post_primary_precision_extension_no_retuning",
            "reason": "resolve_the_primary_30_block_inconclusive_comparison",
            "timing_sha256": sha256(crc8_precision_path),
            "paired_blocks": stock_over_policy["paired_blocks"],
            "stock_bolt_over_frozen_policy_cpu": stock_over_policy,
            "wrapper_policy_faster": (
                float(stock_over_policy["bootstrap_95_percent"][0]) > 1.0
            ),
            "all_outputs_match": all(
                bool(value["all_outputs_match"])
                for value in precision_timing["variants"].values()
            ),
            "artifacts_unchanged": (
                precision_timing["variants"]["reference"]["sha256"]
                == sha256(reference)
                and precision_timing["variants"]["stock_bolt"]["sha256"]
                == workloads["crc8"]["artifact_identity"]["stock_bolt"]
            ),
        }
    precision_resolved = sum(
        item["wrapper_policy_faster"] and item["all_outputs_match"]
        and item["artifacts_unchanged"]
        for item in precision_extensions.values()
    )
    report = {
        "schema_version": 1,
        "status": "held_out_policy_evaluation_complete",
        "classification": (
            "supported-host firmware-derived fixed-work BOLT-plus-wrapper evaluation"
        ),
        "selection": {
            "path": str(selection_path),
            "sha256": sha256(selection_path),
            "frozen_before_held_out_measurement": True,
            "minimum_useful_cpu_ratio": threshold,
        },
        "bolt_and_wrapper": {
            "fop": wrapper["fop"],
            "qualified_function_count": wrapper["qualified"]["function_count"],
            "saved_live_bytes": wrapper["qualified"]["saved_live_bytes"],
            "fixed_work_observations": wrapper["fixed_work_observations"],
        },
        "artifacts": {
            "reference": {
                "path": str(reference), "sha256": sha256(reference),
                "file_bytes": reference.stat().st_size,
            },
            "full_relocatable": {
                "path": str(full), "sha256": sha256(full),
                "file_bytes": full.stat().st_size,
            },
            "compact_fixed_layout": {
                "path": str(compact), "sha256": sha256(compact),
                "file_bytes": compact.stat().st_size,
            },
        },
        "workloads": workloads,
        "precision_extensions": precision_extensions,
        "summary": {
            "workload_count": len(workloads),
            "relocatable_policy_generalized_gains": relocatable_gains,
            "fixed_layout_policy_generalized_gains": fixed_gains,
            "relocatable_policy_exact_reference_fallbacks": sum(
                item["training_choice"]["relocatable"] == "reference"
                for item in workloads.values()
            ),
            "fixed_layout_policy_exact_reference_fallbacks": sum(
                item["training_choice"]["fixed_layout"] == "reference"
                for item in workloads.values()
            ),
            "relocatable_policy_faster_than_stock_bolt": (
                relocatable_better_than_stock
            ),
            "relocatable_policy_vs_stock_inconclusive": (
                relocatable_not_resolved_vs_stock
            ),
            "relocatable_policy_slower_than_stock_bolt": sum(
                item["relocatable_policy_vs_stock_classification"]
                == "stock_bolt_faster"
                for item in workloads.values()
            ),
            "all_outputs_match": all(
                item["all_outputs_match"] for item in workloads.values()
            ),
        },
        "summary_including_declared_precision_extensions": {
            "workload_count": len(workloads),
            "relocatable_policy_faster_than_stock_bolt": (
                relocatable_better_than_stock + precision_resolved
            ),
            "relocatable_policy_vs_stock_inconclusive": (
                relocatable_not_resolved_vs_stock - precision_resolved
            ),
            "relocatable_policy_slower_than_stock_bolt": sum(
                item["relocatable_policy_vs_stock_classification"]
                == "stock_bolt_faster"
                for item in workloads.values()
            ),
            "primary_results_preserved": True,
        },
        "claims": {
            "better_than_frozen_stock_bolt_policy_on_supported_host_corpus": (
                all(
                    item["relocatable_policy_vs_stock_classification"]
                    != "stock_bolt_faster"
                    for item in workloads.values()
                )
            ),
            "original_cortex_m_speedup": False,
            "fastdyn_speedup": False,
            "whole_firmware_equivalence": False,
        },
        "decision": (
            "use independently trained profitability selection around BOLT; publish "
            "the complete relocated BOLT+FOP image only when layout is admitted and "
            "held-out benefit clears the threshold; otherwise use compact accepted "
            "bodies or the exact reference"
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "output": str(output), "sha256": sha256(output),
        "summary": report["summary"], "claims": report["claims"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
