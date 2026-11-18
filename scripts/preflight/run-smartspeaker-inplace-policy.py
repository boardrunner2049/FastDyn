#!/usr/bin/env python3
"""Evaluate the checked fixed-layout BOLT-FOP SmartSpeaker candidate."""

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
CANDIDATE = OUTPUT / "wrapper-fop-inplace/SmartSpeaker.bolt-qualified-compact"
LEDGER = OUTPUT / "wrapper-fop-inplace/wrapper-result.json"
WORK_UNITS = 20_000
SETTLE_NS = 100_000
MINIMUM_CPU_RATIO = 1.005
MINIMUM_WALL_RATIO = 1.0


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def run_benchmark(name: str, samples: int, seed: int) -> dict[str, Any]:
    output = OUTPUT / f"{name}.json"
    log = OUTPUT / f"{name}.log"
    if output.exists() or log.exists():
        raise RuntimeError(f"refusing to overwrite {output} or {log}")
    command = [
        "taskset", "-c", "24,25", sys.executable, str(BENCHMARK),
        "--variant", f"stock_bolt={STOCK}",
        "--variant", f"fop_inplace={CANDIDATE}",
        "--preload", str(PRELOAD),
        "--work-units", str(WORK_UNITS),
        "--tick-address", "0x495670",
        "--samples", str(samples),
        "--seed", str(seed),
        "--output", str(output),
    ]
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
    expected = {
        "stock": "af6b32603c83b741c129473e5f6f18587a63e2cc44edaec1834b07c9c2a64fab",
        "candidate": "4f190f8d8792e49cc70b2dab7de5b9d5c29cf9d75e43dccfca6d3a5122e37812",
        "preload": "b4a89aec7fb69f65f4dd1fdcc40f74136df81fd9a1a7e70cdf7e9475b29a2d44",
    }
    for name, path in (("stock", STOCK), ("candidate", CANDIDATE), ("preload", PRELOAD)):
        if not path.is_file() or sha256(path) != expected[name]:
            raise RuntimeError(f"missing or changed frozen artifact: {name}: {path}")
    ledger = json.loads(LEDGER.read_text())
    if (
        ledger["status"] != "integration_accepted"
        or ledger["artifacts"]["qualified_compact"]["sha256"] != expected["candidate"]
        or ledger["qualified"]["absolute_references_repaired"] != 2
    ):
        raise RuntimeError("candidate wrapper ledger is not accepted or complete")

    plan = {
        "schema_version": 1,
        "status": "frozen_before_timing",
        "candidate_mechanism": (
            "ordinary BOLT FOP with block/function layout and splitting disabled, "
            "then checked in-place adoption with BAT-backed absolute-reference repair"
        ),
        "schedule": {
            "completed_units_per_execution": WORK_UNITS,
            "ticks_per_completed_unit": 10,
            "total_ticks_per_execution": WORK_UNITS * 10,
            "marker_settle_ns": SETTLE_NS,
            "tick_counter_address": "0x495670",
            "affinity_cpus": [24, 25],
            "driver_cpu": 25,
            "preload_sha256": expected["preload"],
        },
        "binaries": {
            "stock_bolt": {"path": str(STOCK), "sha256": expected["stock"]},
            "fop_inplace": {"path": str(CANDIDATE), "sha256": expected["candidate"]},
        },
        "wrapper_ledger": {"path": str(LEDGER), "sha256": sha256(LEDGER)},
        "training": {"paired_blocks": 12, "seed": 20261703},
        "held_out": {"paired_blocks": 30, "seed": 20261704},
        "advance_rule": (
            "all exact-work/trace checks pass, target-CPU lower 95% bound exceeds "
            "1.005, and wall lower 95% bound is at least 1.0"
        ),
        "claim_boundary": (
            "unchanged LEMIX x86-64 machine-code workload under exact POSIX replay; "
            "not original-MCU execution or FastDyn throughput"
        ),
    }
    plan_path = OUTPUT / "inplace-timing-plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text()) != plan:
            raise RuntimeError("existing in-place timing plan differs from frozen plan")
    else:
        write_json(plan_path, plan)

    training_path = OUTPUT / "inplace-training.json"
    training = (
        json.loads(training_path.read_text())
        if training_path.exists()
        else run_benchmark("inplace-training", 12, 20261703)
    )
    training_metrics = training["paired_comparisons"]["fop_inplace"]
    training_ok = (
        training["validation"]["all_executions_ok"]
        and training_metrics["target_cpu_ns"]["bootstrap_95_percent"][0]
        > MINIMUM_CPU_RATIO
        and training_metrics["wall_ns"]["bootstrap_95_percent"][0]
        >= MINIMUM_WALL_RATIO
    )
    selection = {
        "schema_version": 1,
        "status": "selected" if training_ok else "candidate_did_not_clear_training_gate",
        "candidate": "fop_inplace",
        "all_exact_work_and_trace_checks_match": training["validation"]["all_executions_ok"],
        "stock_bolt_over_candidate": training_metrics,
        "training_sha256": sha256(OUTPUT / "inplace-training.json"),
    }
    write_json(OUTPUT / "inplace-selection.json", selection)
    if not training_ok:
        print(json.dumps(selection, indent=2, sort_keys=True))
        return 2

    held_out = run_benchmark("inplace-held-out", 30, 20261704)
    metrics = held_out["paired_comparisons"]["fop_inplace"]
    all_ok = held_out["validation"]["all_executions_ok"]
    accepted = (
        all_ok
        and metrics["target_cpu_ns"]["bootstrap_95_percent"][0] > MINIMUM_CPU_RATIO
        and metrics["wall_ns"]["bootstrap_95_percent"][0] >= MINIMUM_WALL_RATIO
    )
    report = {
        "schema_version": 1,
        "status": "accepted" if accepted else "not_profitable",
        "candidate": "fop_inplace",
        "all_exact_work_and_trace_checks_match": all_ok,
        "stock_bolt_over_candidate": metrics,
        "training_selection": selection,
        "held_out_sha256": sha256(OUTPUT / "inplace-held-out.json"),
        "advance_rule": plan["advance_rule"],
        "claim_boundary": plan["claim_boundary"],
    }
    write_json(OUTPUT / "inplace-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 3


if __name__ == "__main__":
    raise SystemExit(main())
