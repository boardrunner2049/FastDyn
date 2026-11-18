#!/usr/bin/env python3
"""Measure the frozen in-place FOP image with target/driver CPU isolation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-frs-wrapper-2026-11-17"
PROFILE = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
SCHEDULE = ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17"
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
STOCK = PROFILE / "SmartSpeaker.marker_stock"
CANDIDATE = RUN / "wrapper-fop-inplace/SmartSpeaker.bolt-qualified-compact"
LEDGER = RUN / "wrapper-fop-inplace/wrapper-result.json"
PRELOAD = SCHEDULE / "lemix_mmio_marker_schedule.so"
EXPECTED = {
    "stock": "af6b32603c83b741c129473e5f6f18587a63e2cc44edaec1834b07c9c2a64fab",
    "candidate": "4f190f8d8792e49cc70b2dab7de5b9d5c29cf9d75e43dccfca6d3a5122e37812",
    "preload": "b4a89aec7fb69f65f4dd1fdcc40f74136df81fd9a1a7e70cdf7e9475b29a2d44",
    "ledger": "4575ff3140533ada91f2b6c5e128e2fb77a07b9032f96bf11e8a3e766c9db731",
}
WORK_UNITS = 20_000
MIN_CPU = 1.005
MIN_WALL = 1.0


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def benchmark(stem: str, blocks: int, seed: int) -> dict[str, Any]:
    output = RUN / f"{stem}.json"
    log = RUN / f"{stem}.log"
    if output.exists() or log.exists():
        raise RuntimeError(f"refusing to overwrite {output} or {log}")
    command = [
        "taskset", "-c", "24", sys.executable, str(BENCHMARK),
        "--variant", f"stock_bolt={STOCK}",
        "--variant", f"fop_inplace={CANDIDATE}",
        "--preload", str(PRELOAD),
        "--work-units", str(WORK_UNITS),
        "--tick-address", "0x495670",
        "--samples", str(blocks),
        "--seed", str(seed),
        "--affinity-cpus", "24",
        "--output", str(output),
    ]
    environment = dict(os.environ)
    environment["FASTDYN_MARKER_SETTLE_NS"] = "100000"
    with log.open("w") as stream:
        result = subprocess.run(
            command, cwd=ROOT, env=environment, stdout=stream,
            stderr=subprocess.STDOUT, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"{stem} failed with status {result.returncode}; see {log}")
    return json.loads(output.read_text())


def clears(report: dict[str, Any]) -> bool:
    metrics = report["paired_comparisons"]["fop_inplace"]
    return bool(
        report["validation"]["all_executions_ok"]
        and metrics["target_cpu_ns"]["bootstrap_95_percent"][0] > MIN_CPU
        and metrics["wall_ns"]["bootstrap_95_percent"][0] >= MIN_WALL
    )


def main() -> int:
    for name, path in (
        ("stock", STOCK), ("candidate", CANDIDATE),
        ("preload", PRELOAD), ("ledger", LEDGER),
    ):
        if not path.is_file() or sha256(path) != EXPECTED[name]:
            raise RuntimeError(f"missing or changed frozen artifact: {name}: {path}")
    plan = {
        "schema_version": 1,
        "status": "frozen_before_timing",
        "candidate_sha256": EXPECTED["candidate"],
        "stock_bolt_sha256": EXPECTED["stock"],
        "wrapper_ledger_sha256": EXPECTED["ledger"],
        "schedule": {
            "completed_units_per_execution": WORK_UNITS,
            "ticks_per_completed_unit": 10,
            "marker_settle_ns": 100000,
            "target_process_affinity": [24],
            "driver_thread_affinity": [25],
            "preload_sha256": EXPECTED["preload"],
        },
        "training": {"paired_blocks": 12, "seed": 20261705},
        "held_out": {"paired_blocks": 30, "seed": 20261706},
        "advance_rule": (
            "all exact-work/trace checks pass, target-CPU lower 95% bound exceeds "
            "1.005, and wall lower 95% bound is at least 1.0"
        ),
        "environment_change": (
            "target and driver CPU affinity are isolated to reduce subtraction noise; "
            "this is a separately identified environment, not a continuation of the "
            "shared-CPU sample"
        ),
    }
    plan_path = RUN / "isolated-inplace-plan.json"
    if plan_path.exists() and json.loads(plan_path.read_text()) != plan:
        raise RuntimeError("existing isolated plan differs from frozen plan")
    if not plan_path.exists():
        write_json(plan_path, plan)

    training = benchmark("isolated-inplace-training", 12, 20261705)
    metrics = training["paired_comparisons"]["fop_inplace"]
    selected = clears(training)
    selection = {
        "schema_version": 1,
        "status": "selected" if selected else "candidate_did_not_clear_training_gate",
        "all_exact_work_and_trace_checks_match": training["validation"]["all_executions_ok"],
        "stock_bolt_over_candidate": metrics,
        "training_sha256": sha256(RUN / "isolated-inplace-training.json"),
    }
    write_json(RUN / "isolated-inplace-selection.json", selection)
    if not selected:
        print(json.dumps(selection, indent=2, sort_keys=True))
        return 2

    held_out = benchmark("isolated-inplace-held-out", 30, 20261706)
    accepted = clears(held_out)
    report = {
        "schema_version": 1,
        "status": "accepted" if accepted else "not_profitable",
        "all_exact_work_and_trace_checks_match": held_out["validation"]["all_executions_ok"],
        "stock_bolt_over_candidate": held_out["paired_comparisons"]["fop_inplace"],
        "training_selection": selection,
        "held_out_sha256": sha256(RUN / "isolated-inplace-held-out.json"),
        "advance_rule": plan["advance_rule"],
    }
    write_json(RUN / "isolated-inplace-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 3


if __name__ == "__main__":
    raise SystemExit(main())
