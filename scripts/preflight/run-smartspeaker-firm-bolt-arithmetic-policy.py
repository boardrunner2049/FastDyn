#!/usr/bin/env python3
"""Train the frozen native Firm-BOLT arithmetic policy."""

from __future__ import annotations

import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
WRAPPER = ROOT / "build/preflight/bolt-firmware-wrapper-arithmetic-rev-2-2026-11-17"
SCHEDULE = ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17"
PLAN = RUN / "firm-bolt-arithmetic-training-plan.json"
RAW = RUN / "firm-bolt-arithmetic-training.json"
LOG = RUN / "firm-bolt-arithmetic-training.log"
SELECTION = RUN / "firm-bolt-arithmetic-selection.json"
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
PRELOAD = SCHEDULE / "lemix_mmio_marker_schedule.so"
STOCK = RUN / "SmartSpeaker.marker_stock"
FOP = RUN / "SmartSpeaker.marker_fop"
CANDIDATE = WRAPPER / "SmartSpeaker.bolt-firmware-candidate"
SMOKE = RUN / "firm-bolt-arithmetic-smoke.json"
CANDIDATE_LABEL = "firm_bolt_arithmetic"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def paired_ratio(
    raw: dict[str, Any], left_name: str, right_name: str,
    metric: str, seed: int,
) -> dict[str, Any]:
    left = {
        int(item["block"]): item
        for item in raw["variants"][left_name]["samples"]
    }
    right = {
        int(item["block"]): item
        for item in raw["variants"][right_name]["samples"]
    }
    ratios = [
        float(left[block][metric]) / float(right[block][metric])
        for block in sorted(left)
    ]
    generator = random.Random(seed)
    medians = [
        statistics.median(
            ratios[generator.randrange(len(ratios))] for _ in ratios
        )
        for _ in range(100_000)
    ]
    return {
        "ratio_definition": f"{left_name} {metric} / {right_name} {metric}",
        "median_ratio": statistics.median(ratios),
        "bootstrap_95_percent": [
            percentile(medians, 0.025), percentile(medians, 0.975)
        ],
        "paired_ratios": ratios,
        "bootstrap_resamples": 100_000,
    }


def main() -> int:
    plan = json.loads(PLAN.read_text())
    for path, key in (
        (STOCK, "stock_bolt_sha256"),
        (FOP, "ordinary_fop_sha256"),
        (CANDIDATE, "candidate_sha256"),
        (SMOKE, "smoke_sha256"),
    ):
        if sha256(path) != plan[key]:
            raise RuntimeError(f"frozen artifact differs: {path}")
    if any(path.exists() for path in (RAW, LOG, SELECTION)):
        raise RuntimeError("training output already exists")
    command = [
        "taskset", "-c", "24,25", sys.executable, str(BENCHMARK),
        "--variant", f"stock_bolt={STOCK}",
        "--variant", f"fop_full={FOP}",
        "--variant", f"{CANDIDATE_LABEL}={CANDIDATE}",
        "--preload", str(PRELOAD),
        "--work-units", str(plan["completed_units_per_execution"]),
        "--tick-address", "0x495670", "--samples", str(plan["paired_blocks"]),
        "--seed", str(plan["seed"]), "--output", str(RAW),
    ]
    environment = dict(os.environ)
    environment["FASTDYN_MARKER_SETTLE_NS"] = str(plan["marker_settle_ns"])
    with LOG.open("w") as stream:
        result = subprocess.run(
            command, cwd=ROOT, env=environment, stdout=stream,
            stderr=subprocess.STDOUT, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"training failed ({result.returncode}); see {LOG}")
    raw = json.loads(RAW.read_text())
    primary = raw["paired_comparisons"][CANDIDATE_LABEL]
    incremental = {
        metric: paired_ratio(raw, "fop_full", CANDIDATE_LABEL, metric,
                             plan["seed"] + index)
        for index, metric in enumerate(
            ("target_cpu_ns", "wall_ns", "process_cpu_ns"), 1
        )
    }
    selected = bool(
        raw["validation"]["all_executions_ok"]
        and primary["target_cpu_ns"]["bootstrap_95_percent"][0] > 1.005
        and primary["wall_ns"]["bootstrap_95_percent"][0] >= 1.0
    )
    selection = {
        "schema_version": 1,
        "status": "selected_for_held_out" if selected else "training_rejected",
        "all_exact_work_and_trace_checks_match": raw["validation"][
            "all_executions_ok"
        ],
        "primary_stock_bolt_over_candidate": primary,
        "incremental_ordinary_fop_over_candidate": incremental,
        "candidate_sha256": sha256(CANDIDATE),
        "raw_sha256": sha256(RAW),
        "command": command,
    }
    SELECTION.write_text(json.dumps(selection, indent=2, sort_keys=True) + "\n")
    print(json.dumps(selection, indent=2, sort_keys=True))
    return 0 if selected else 2


if __name__ == "__main__":
    raise SystemExit(main())
