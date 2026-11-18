#!/usr/bin/env python3
"""Time the frozen exact-profile BOLT frame-opt=hot SmartSpeaker arm."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
SCHEDULE = ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17"
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
STOCK = RUN / "SmartSpeaker.marker_stock"
CANDIDATE = RUN / "SmartSpeaker.marker_fop_hot"
PRELOAD = SCHEDULE / "lemix_mmio_marker_schedule.so"
EXPECTED = {
    "stock": "af6b32603c83b741c129473e5f6f18587a63e2cc44edaec1834b07c9c2a64fab",
    "candidate": "ac7034cef35b2c88c5ab6b42470fd3116b608b029e4377358a1d9db2ec61812a",
    "preload": "b4a89aec7fb69f65f4dd1fdcc40f74136df81fd9a1a7e70cdf7e9475b29a2d44",
}


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
        "taskset", "-c", "24,25", sys.executable, str(BENCHMARK),
        "--variant", f"stock_bolt={STOCK}",
        "--variant", f"fop_hot={CANDIDATE}",
        "--preload", str(PRELOAD), "--work-units", "20000",
        "--tick-address", "0x495670", "--samples", str(blocks),
        "--seed", str(seed), "--output", str(output),
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
    metrics = report["paired_comparisons"]["fop_hot"]
    return bool(
        report["validation"]["all_executions_ok"]
        and metrics["target_cpu_ns"]["bootstrap_95_percent"][0] > 1.005
        and metrics["wall_ns"]["bootstrap_95_percent"][0] >= 1.0
    )


def main() -> int:
    for name, path in (("stock", STOCK), ("candidate", CANDIDATE), ("preload", PRELOAD)):
        if not path.is_file() or sha256(path) != EXPECTED[name]:
            raise RuntimeError(f"missing or changed artifact: {name}: {path}")
    training = benchmark("fop-hot-training", 12, 20261707)
    metrics = training["paired_comparisons"]["fop_hot"]
    selected = clears(training)
    selection = {
        "schema_version": 1,
        "status": "selected" if selected else "candidate_did_not_clear_training_gate",
        "all_exact_work_and_trace_checks_match": training["validation"]["all_executions_ok"],
        "stock_bolt_over_candidate": metrics,
        "training_sha256": sha256(RUN / "fop-hot-training.json"),
    }
    write_json(RUN / "fop-hot-selection.json", selection)
    if not selected:
        print(json.dumps(selection, indent=2, sort_keys=True))
        return 2
    held_out = benchmark("fop-hot-held-out", 30, 20261708)
    metrics = held_out["paired_comparisons"]["fop_hot"]
    accepted = clears(held_out)
    report = {
        "schema_version": 1,
        "status": "accepted" if accepted else "not_profitable",
        "all_exact_work_and_trace_checks_match": held_out["validation"]["all_executions_ok"],
        "stock_bolt_over_candidate": metrics,
        "training_selection": selection,
        "held_out_sha256": sha256(RUN / "fop-hot-held-out.json"),
    }
    write_json(RUN / "fop-hot-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 3


if __name__ == "__main__":
    raise SystemExit(main())
