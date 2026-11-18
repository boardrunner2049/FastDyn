#!/usr/bin/env python3
"""Measure checked in-place SmartSpeaker bodies produced after BOLT FOP."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "build/preflight/lemix-smartspeaker-frs-wrapper-2026-11-17"
PROFILE_RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
SCHEDULE_RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17"
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
PRELOAD = SCHEDULE_RUN / "lemix_mmio_marker_schedule.so"
STOCK = PROFILE_RUN / "SmartSpeaker.marker_stock"
CANDIDATES = {
    "ordinary_fop_wrapper": (
        OUTPUT / "wrapper-ordinary-fop/SmartSpeaker.bolt-qualified-compact"
    ),
    "frs_gated_wrapper": OUTPUT / "wrapper/SmartSpeaker.bolt-qualified-compact",
}
WORK_UNITS = 20_000
SETTLE_NS = 100_000
MINIMUM_CPU_RATIO = 1.005
MINIMUM_WALL_RATIO = 1.0


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def run_benchmark(
    name: str,
    *,
    samples: int,
    seed: int,
    variants: list[tuple[str, Path]],
) -> dict[str, Any]:
    output = OUTPUT / f"{name}.json"
    log = OUTPUT / f"{name}.log"
    if output.exists() or log.exists():
        raise RuntimeError(f"refusing to overwrite {output} or {log}")
    command = ["taskset", "-c", "24,25", sys.executable, str(BENCHMARK)]
    for variant_name, path in variants:
        command.extend(("--variant", f"{variant_name}={path}"))
    command.extend((
        "--preload", str(PRELOAD),
        "--work-units", str(WORK_UNITS),
        "--tick-address", "0x495670",
        "--samples", str(samples),
        "--seed", str(seed),
        "--output", str(output),
    ))
    environment = dict(os.environ)
    environment["FASTDYN_MARKER_SETTLE_NS"] = str(SETTLE_NS)
    with log.open("w") as stream:
        result = subprocess.run(
            command, cwd=ROOT, env=environment, stdout=stream,
            stderr=subprocess.STDOUT, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"{name} failed with status {result.returncode}; see {log}")
    return json.loads(output.read_text())


def main() -> int:
    wrapper_results = {
        "ordinary_fop_wrapper": OUTPUT / "wrapper-ordinary-fop/wrapper-result.json",
        "frs_gated_wrapper": OUTPUT / "wrapper/wrapper-result.json",
    }
    expected_hashes = {
        "stock_bolt": "af6b32603c83b741c129473e5f6f18587a63e2cc44edaec1834b07c9c2a64fab",
        "ordinary_fop_wrapper": "a7024e402eb6c5193d33cb1d4116042a1cf7503ae1d31e28be38ed65a689a6c5",
        "frs_gated_wrapper": "018e62f910225f92bd8e57dec57fde9683e3b69ae56fa069fddcf6ec95ff9e46",
        "preload": "b4a89aec7fb69f65f4dd1fdcc40f74136df81fd9a1a7e70cdf7e9475b29a2d44",
    }
    frozen = {"stock_bolt": STOCK, **CANDIDATES}
    for name, path in frozen.items():
        if not path.is_file() or sha256(path) != expected_hashes[name]:
            raise RuntimeError(f"missing or changed frozen binary: {name}: {path}")
    if not PRELOAD.is_file() or sha256(PRELOAD) != expected_hashes["preload"]:
        raise RuntimeError("missing or changed exact-schedule preload")
    for name, result_path in wrapper_results.items():
        result = json.loads(result_path.read_text())
        if result["status"] != "integration_accepted":
            raise RuntimeError(f"wrapper candidate is not accepted: {name}")
        if result["artifacts"]["qualified_compact"]["sha256"] != expected_hashes[name]:
            raise RuntimeError(f"wrapper ledger does not identify candidate: {name}")

    plan = {
        "schema_version": 1,
        "status": "frozen_before_timing",
        "schedule": {
            "completed_units_per_execution": WORK_UNITS,
            "ticks_per_completed_unit": 10,
            "total_ticks_per_execution": WORK_UNITS * 10,
            "marker_settle_ns": SETTLE_NS,
            "tick_counter_address": "0x495670",
            "affinity_cpus": [24, 25],
            "driver_cpu": 25,
            "preload": str(PRELOAD),
            "preload_sha256": expected_hashes["preload"],
        },
        "binaries": {
            name: {"path": str(path), "sha256": expected_hashes[name]}
            for name, path in frozen.items()
        },
        "wrapper_ledgers": {
            name: {"path": str(path), "sha256": sha256(path)}
            for name, path in wrapper_results.items()
        },
        "training": {
            "reference": "stock_bolt",
            "candidates": list(CANDIDATES),
            "paired_blocks": 12,
            "seed": 20261701,
            "selection": (
                "largest target-CPU lower 95% bound among candidates whose "
                "target-CPU lower bound exceeds 1.005 and wall lower bound is at least 1.0"
            ),
        },
        "held_out": {
            "reference": "stock_bolt",
            "paired_blocks": 30,
            "seed": 20261702,
            "advance_rule": (
                "all exact-work/trace checks pass, target-CPU lower 95% bound exceeds "
                "1.005, and wall lower 95% bound is at least 1.0"
            ),
        },
        "claim_boundary": (
            "unchanged LEMIX SmartSpeaker x86-64 machine code under the exact "
            "ten-tick-per-marker POSIX schedule and zero-backed MMIO; not the "
            "original wall-clock schedule, Cortex-M hardware, or FastDyn"
        ),
        "retuning_after_training_or_held_out": False,
    }
    plan_path = OUTPUT / "wrapper-timing-plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text()) != plan:
            raise RuntimeError("existing timing plan differs from frozen plan")
    else:
        write_json(plan_path, plan)

    training = run_benchmark(
        "wrapper-training", samples=12, seed=20261701,
        variants=[("stock_bolt", STOCK), *CANDIDATES.items()],
    )
    rows = []
    eligible: list[tuple[float, str]] = []
    for name in CANDIDATES:
        metrics = training["paired_comparisons"][name]
        cpu_lower = metrics["target_cpu_ns"]["bootstrap_95_percent"][0]
        wall_lower = metrics["wall_ns"]["bootstrap_95_percent"][0]
        qualifies = cpu_lower > MINIMUM_CPU_RATIO and wall_lower >= MINIMUM_WALL_RATIO
        rows.append({
            "candidate": name,
            "target_cpu": metrics["target_cpu_ns"],
            "wall": metrics["wall_ns"],
            "qualifies": qualifies,
        })
        if qualifies:
            eligible.append((cpu_lower, name))
    eligible.sort(reverse=True)
    selected = eligible[0][1] if eligible else None
    selection = {
        "schema_version": 1,
        "status": "selected" if selected else "no_candidate_cleared_training_gate",
        "training_sha256": sha256(OUTPUT / "wrapper-training.json"),
        "candidates": rows,
        "selected": selected,
    }
    write_json(OUTPUT / "wrapper-selection.json", selection)
    if selected is None:
        print(json.dumps(selection, indent=2, sort_keys=True))
        return 2

    held_out = run_benchmark(
        "wrapper-held-out", samples=30, seed=20261702,
        variants=[("stock_bolt", STOCK), (selected, CANDIDATES[selected])],
    )
    metrics = held_out["paired_comparisons"][selected]
    all_ok = held_out["validation"]["all_executions_ok"]
    cpu_lower = metrics["target_cpu_ns"]["bootstrap_95_percent"][0]
    wall_lower = metrics["wall_ns"]["bootstrap_95_percent"][0]
    accepted = all_ok and cpu_lower > MINIMUM_CPU_RATIO and wall_lower >= MINIMUM_WALL_RATIO
    report = {
        "schema_version": 1,
        "status": "accepted" if accepted else "not_profitable",
        "selected": selected,
        "all_exact_work_and_trace_checks_match": all_ok,
        "stock_bolt_over_candidate": metrics,
        "training_selection": selection,
        "held_out_sha256": sha256(OUTPUT / "wrapper-held-out.json"),
        "advance_rule": plan["held_out"]["advance_rule"],
        "claim_boundary": plan["claim_boundary"],
    }
    write_json(OUTPUT / "wrapper-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 3


if __name__ == "__main__":
    raise SystemExit(main())
