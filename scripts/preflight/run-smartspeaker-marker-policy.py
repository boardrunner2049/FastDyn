#!/usr/bin/env python3
"""Evaluate frozen SmartSpeaker BOLT mechanisms under an exact marker schedule."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17"
BASELINE = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16/binaries"
MECHANISMS = ROOT / "build/preflight/lemix-smartspeaker-bolt-mechanisms-2026-11-17"
PRELOAD = OUTPUT / "lemix_mmio_marker_schedule.so"
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
CANDIDATES = ("full_fop", "icp", "inline_small", "icp_inline_small")
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
    command = [
        "taskset", "-c", "24,25", sys.executable, str(BENCHMARK),
    ]
    for name_value, path in variants:
        command.extend(("--variant", f"{name_value}={path}"))
    command.extend((
        "--preload", str(PRELOAD), "--work-units", str(WORK_UNITS),
        "--tick-address", "0x495670", "--samples", str(samples),
        "--seed", str(seed), "--output", str(output),
    ))
    environment = dict(os.environ)
    environment["FASTDYN_MARKER_SETTLE_NS"] = str(SETTLE_NS)
    with log.open("w") as stream:
        result = subprocess.run(
            command, cwd=ROOT, env=environment,
            stdout=stream, stderr=subprocess.STDOUT, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"{name} failed with status {result.returncode}; see {log}")
    return json.loads(output.read_text())


def main() -> int:
    manifest = json.loads((MECHANISMS / "manifest.json").read_text())
    frozen = {
        "stock_bolt": BASELINE / "SmartSpeaker.L1",
        "full_fop": BASELINE / "SmartSpeaker.F1",
        "icp": MECHANISMS / "SmartSpeaker.icp",
        "inline_small": MECHANISMS / "SmartSpeaker.inline_small",
        "icp_inline_small": MECHANISMS / "SmartSpeaker.icp_inline_small",
    }
    expected = {
        "stock_bolt": "4b7a4ee59e0ce6d01f028e8bb55d7260be13687cae6b83a176fb5c5ad797ccb0",
        "full_fop": manifest["baseline"]["sha256"],
        **{item["id"]: item["sha256"] for item in manifest["candidates"]},
    }
    if tuple(manifest["candidate_policy"]["frozen_ids"]) != CANDIDATES[1:]:
        raise RuntimeError("candidate manifest no longer matches the frozen policy")
    for name, path in frozen.items():
        if not path.is_file() or sha256(path) != expected[name]:
            raise RuntimeError(f"missing or changed binary: {name}: {path}")
    if not PRELOAD.is_file():
        raise RuntimeError(f"missing exact-schedule preload: {PRELOAD}")

    calibrations = [OUTPUT / "calibration-500.json", OUTPUT / "calibration-mechanisms-1000.json"]
    if not all(path.is_file() for path in calibrations):
        raise RuntimeError("required calibration evidence is absent")
    plan = {
        "schema_version": 1,
        "status": "frozen_before_training",
        "calibration": [
            {"path": str(path), "sha256": sha256(path)} for path in calibrations
        ],
        "schedule": {
            "completed_units_per_execution": WORK_UNITS,
            "ticks_per_completed_unit": 10,
            "total_ticks_per_execution": WORK_UNITS * 10,
            "marker_settle_ns": SETTLE_NS,
            "tick_counter_address": "0x495670",
            "preload": str(PRELOAD),
            "preload_sha256": sha256(PRELOAD),
            "affinity_cpus": [24, 25],
        },
        "frozen_binaries": {
            name: {"path": str(path), "sha256": expected[name]}
            for name, path in frozen.items()
        },
        "training": {
            "reference": "stock_bolt",
            "candidates": list(CANDIDATES),
            "paired_blocks": 12,
            "seed": 20261215,
            "selection": (
                "largest target-CPU lower 95% bound among candidates whose "
                "target-CPU lower bound exceeds 1.005 and wall lower bound is at least 1.0"
            ),
        },
        "held_out": {
            "reference": "stock_bolt",
            "paired_blocks": 30,
            "seed": 20261216,
            "advance_rule": (
                "all exact-work/trace checks pass, target-CPU lower 95% bound exceeds "
                "1.005, and wall lower 95% bound is at least 1.0"
            ),
        },
        "claim_boundary": (
            "unchanged LEMIX SmartSpeaker x86-64 machine code under an exact "
            "ten-tick-per-marker POSIX schedule and zero-backed MMIO; not the "
            "original wall-clock schedule, Cortex-M hardware, or FastDyn"
        ),
        "retuning_after_training_or_held_out": False,
    }
    plan_path = OUTPUT / "marker-measurement-plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text()) != plan:
            raise RuntimeError("existing marker plan differs from frozen plan")
    else:
        write_json(plan_path, plan)

    training = run_benchmark(
        "marker-training", samples=12, seed=20261215,
        variants=[("stock_bolt", frozen["stock_bolt"])]
        + [(name, frozen[name]) for name in CANDIDATES],
    )
    eligible: list[tuple[float, str]] = []
    rows = []
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
        "training_sha256": sha256(OUTPUT / "marker-training.json"),
        "candidates": rows,
        "selected": selected,
    }
    write_json(OUTPUT / "marker-selection.json", selection)
    if selected is None:
        print(json.dumps(selection, indent=2, sort_keys=True))
        return 2

    held_out = run_benchmark(
        "marker-held-out", samples=30, seed=20261216,
        variants=[("stock_bolt", frozen["stock_bolt"]), (selected, frozen[selected])],
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
        "held_out_sha256": sha256(OUTPUT / "marker-held-out.json"),
        "advance_rule": plan["held_out"]["advance_rule"],
        "claim_boundary": plan["claim_boundary"],
    }
    write_json(OUTPUT / "marker-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 3


if __name__ == "__main__":
    raise SystemExit(main())
