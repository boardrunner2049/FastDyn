#!/usr/bin/env python3
"""Freeze and evaluate the unchanged paced SmartSpeaker deployment policy."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
APP = (
    ROOT / "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/"
    "dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/"
    "SmartSpeaker_Ported"
)
REFERENCE = APP / "build/SmartSpeaker"
STOCK = RUN / "binaries/SmartSpeaker.L1"
FULL_FOP = RUN / "binaries/SmartSpeaker.F1"
PRELOAD = ROOT / "build/preflight/lemix-smartspeaker/lemix_mmio_map.so"
PRIMARY = RUN / "paired-results.json"
DEFAULT_OUTPUT = (
    ROOT / "build/preflight/lemix-smartspeaker-policy-precision-2026-11-17"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode() + b"\n"


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical(value))


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def paired_result(
    numerator: dict[int, dict[str, Any]],
    denominator: dict[int, dict[str, Any]],
    *,
    seed: int,
) -> dict[str, Any]:
    blocks = sorted(set(numerator) & set(denominator))
    ratios = []
    for block in blocks:
        left = numerator[block]
        right = denominator[block]
        left_cost = left["cpu_seconds"] / left["interrupt_dispatches"]
        right_cost = right["cpu_seconds"] / right["interrupt_dispatches"]
        ratios.append(left_cost / right_cost)
    rng = random.Random(seed)
    medians = [
        statistics.median(
            ratios[rng.randrange(len(ratios))] for _ in ratios
        )
        for _ in range(100_000)
    ]
    interval = [percentile(medians, 0.025), percentile(medians, 0.975)]
    if interval[0] > 1.0:
        classification = "denominator_faster"
    elif interval[1] < 1.0:
        classification = "numerator_faster"
    else:
        classification = "inconclusive"
    return {
        "blocks": len(blocks),
        "ratio_definition": "numerator CPU/marker divided by denominator CPU/marker",
        "median_ratio": statistics.median(ratios),
        "bootstrap_95_percent": interval,
        "bootstrap_resamples": 100_000,
        "classification": classification,
        "denominator_clears_1_005_lower_bound": interval[0] >= 1.005,
        "paired_ratios": ratios,
    }


def freeze(output: Path, args: argparse.Namespace) -> dict[str, Any]:
    primary = json.loads(PRIMARY.read_text())
    if any(
        item["classification"] != "inconclusive"
        for item in primary["comparisons"].values()
    ):
        raise SystemExit("primary SmartSpeaker matrix no longer matches frozen basis")
    identity = {
        "schema_version": 1,
        "status": "frozen_before_precision_extension",
        "workload_id": "lemix_smartspeaker_paced_zero_mmio",
        "choice": "reference",
        "decision_rule": (
            "retain exact O0 reference because no primary BOLT arm cleared the "
            "predeclared 1.05 lower-confidence-bound gate"
        ),
        "held_out_used_for_retuning": False,
        "primary_results_sha256": sha256(PRIMARY),
        "variants": {
            "reference_policy": {"path": str(REFERENCE), "sha256": sha256(REFERENCE)},
            "stock_bolt": {"path": str(STOCK), "sha256": sha256(STOCK)},
            "full_fop": {"path": str(FULL_FOP), "sha256": sha256(FULL_FOP)},
        },
        "environment": {
            "preload": str(PRELOAD),
            "preload_sha256": sha256(PRELOAD),
            "interrupt_period_ticks": 10,
            "fidelity": "zero-backed MMIO addressability only; no device model",
        },
        "measurement": {
            "samples": args.samples,
            "duration_seconds": args.duration,
            "seed": args.seed,
            "affinity_cpus": args.cpus,
            "trace_prefix_events": 400,
        },
        "claim_boundaries": {
            "paced_cpu_cost_only": True,
            "whole_application_capacity": False,
            "original_mcu_speedup": False,
            "fastdyn_speedup": False,
        },
    }
    selection = {**identity, "selection_id": hashlib.sha256(canonical(identity)).hexdigest()}
    write_json(output / "selection.json", selection)
    return selection


def measure(output: Path, args: argparse.Namespace) -> None:
    raw = output / "samples.json"
    if raw.exists():
        raise SystemExit(f"measurement already exists: {raw}")
    command = [
        "taskset", "-c", args.cpus, sys.executable,
        str(ROOT / "scripts/preflight/benchmark-lemix-daemon.py"),
        "--paired-blocks", "--samples", str(args.samples),
        "--duration", str(args.duration), "--seed", str(args.seed),
        "--preload", str(PRELOAD),
        "--variant", f"reference_policy={REFERENCE}",
        "--variant", f"stock_bolt={STOCK}",
        "--variant", f"full_fop={FULL_FOP}",
        "--output", str(raw),
    ]
    write_json(output / "command.json", command)
    with (output / "measurement.log").open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise SystemExit(f"measurement failed with status {result.returncode}")


def summarize(output: Path, args: argparse.Namespace) -> dict[str, Any]:
    selection_path = output / "selection.json"
    raw_path = output / "samples.json"
    selection = json.loads(selection_path.read_text())
    raw = json.loads(raw_path.read_text())
    expected = selection["measurement"]
    if raw["method"]["samples_per_variant"] != expected["samples"] or (
        raw["method"]["duration_seconds"] != expected["duration_seconds"]
        or raw["method"]["order_seed"] != expected["seed"]
    ):
        raise SystemExit("measurement does not match frozen selection")
    by_variant: dict[str, dict[int, dict[str, Any]]] = {}
    trace_hashes: set[str] = set()
    for name, variant in raw["variants"].items():
        expected_variant = selection["variants"][name]
        if variant["sha256"] != expected_variant["sha256"] or (
            Path(variant["path"]).resolve() != Path(expected_variant["path"]).resolve()
        ):
            raise SystemExit(f"measured artifact does not match selection for {name}")
        if not variant["all_ok"]:
            raise SystemExit(f"invalid samples for {name}")
        by_variant[name] = {
            int(sample["block"]): sample for sample in variant["samples"]
        }
        for sample in variant["samples"]:
            if sample["interrupt_trace_prefix_events"] != expected[
                "trace_prefix_events"
            ]:
                raise SystemExit(f"short trace prefix for {name}")
            trace_hashes.add(sample["interrupt_trace_prefix_sha256"])
    if len(trace_hashes) != 1:
        raise SystemExit("interrupt-selection trace prefix differs across samples")
    comparisons = {
        "stock_bolt_over_reference_policy": paired_result(
            by_variant["stock_bolt"], by_variant["reference_policy"],
            seed=args.seed + 101,
        ),
        "full_fop_over_reference_policy": paired_result(
            by_variant["full_fop"], by_variant["reference_policy"],
            seed=args.seed + 102,
        ),
        "stock_bolt_over_full_fop": paired_result(
            by_variant["stock_bolt"], by_variant["full_fop"],
            seed=args.seed + 103,
        ),
    }
    report = {
        "schema_version": 1,
        "status": "held_out_precision_extension_complete",
        "selection_sha256": sha256(selection_path),
        "raw_samples_sha256": sha256(raw_path),
        "selection": selection,
        "interrupt_trace_prefix": {
            "events": expected["trace_prefix_events"],
            "sha256": next(iter(trace_hashes)),
            "equal_across_all_samples": True,
        },
        "comparisons": comparisons,
        "variants": {
            name: {
                "samples": len(variant["samples"]),
                "median_cpu_seconds": variant["median_cpu_seconds"],
                "median_interrupt_dispatches": variant["median_interrupt_dispatches"],
                "all_ok": variant["all_ok"],
            }
            for name, variant in raw["variants"].items()
        },
        "claims": selection["claim_boundaries"],
    }
    write_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--seed", type=int, default=20261123)
    parser.add_argument("--cpus", default="24,25")
    parser.add_argument(
        "--stage", choices=("freeze", "measure", "summarize", "all"), default="all"
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.stage in {"freeze", "all"}:
        if (output / "selection.json").exists():
            raise SystemExit(f"selection already exists: {output / 'selection.json'}")
        freeze(output, args)
    if args.stage in {"measure", "all"}:
        measure(output, args)
    if args.stage in {"summarize", "all"}:
        report = summarize(output, args)
        print(json.dumps(report["comparisons"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
