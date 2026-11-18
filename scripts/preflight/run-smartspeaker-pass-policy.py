#!/usr/bin/env python3
"""Select frozen upstream-BOLT SmartSpeaker pass combinations."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
PRELOAD = (
    ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17/"
    "lemix_mmio_marker_schedule.so"
)
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
WORK_UNITS = 20_000
SETTLE_NS = 100_000
CPU_GATE = 1.005
WALL_GATE = 1.0
CANDIDATES = ("plt_hot", "fop_plt_hot", "fop_plt_all", "combined_passes")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def run_benchmark(
    name: str, samples: int, seed: int, variants: list[tuple[str, Path]]
) -> dict[str, Any]:
    output = OUTPUT / f"{name}.json"
    log = OUTPUT / f"{name}.log"
    if output.exists() or log.exists():
        raise RuntimeError(f"refusing to overwrite {output} or {log}")
    command = ["taskset", "-c", "24,25", sys.executable, str(BENCHMARK)]
    for variant, path in variants:
        command.extend(("--variant", f"{variant}={path}"))
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
    variants = {
        "stock_bolt": OUTPUT / "SmartSpeaker.marker_stock",
        **{name: OUTPUT / f"SmartSpeaker.{name}" for name in CANDIDATES},
    }
    expected = {
        "stock_bolt": "af6b32603c83b741c129473e5f6f18587a63e2cc44edaec1834b07c9c2a64fab",
        "plt_hot": "e477e949cca258a571fb99a2afe6091169942fe5b457708d1d2265d97b78d81c",
        "fop_plt_hot": "a4bc68513516a49a0607cb5a843b090d4b9b9fbc8ee675327abec5a0b729c4f6",
        "fop_plt_all": "283903290403349ae04152213f765fdb69d81dd3313193b400a5b5b1258aed70",
        "combined_passes": "4971ea397968804939138d38d8ffea78d1942d831364b5327442794d141b7925",
    }
    actual = {name: sha256(path) for name, path in variants.items()}
    if actual != expected:
        raise RuntimeError(f"pass candidate hashes changed: {actual}")
    plan = {
        "schema_version": 1,
        "status": "frozen_before_timing",
        "candidate_build_plan_sha256": sha256(OUTPUT / "pass-screen-plan.json"),
        "profile_sha256": sha256(OUTPUT / "marker-training.fdata"),
        "preload_sha256": sha256(PRELOAD),
        "work": {
            "completed_units": WORK_UNITS,
            "ticks_per_unit": 10,
            "total_ticks": WORK_UNITS * 10,
            "marker_settle_ns": SETTLE_NS,
        },
        "binaries": {
            name: {"path": str(path), "sha256": expected[name]}
            for name, path in variants.items()
        },
        "training": {
            "paired_blocks": 12,
            "seed": 20261220,
            "selection": (
                "largest target-CPU lower 95% bound with target-CPU lower > 1.005 "
                "and wall lower >= 1.0"
            ),
        },
        "held_out": {
            "paired_blocks": 30,
            "seed": 20261221,
            "advance_rule": (
                "all exact-work/trace checks pass, target-CPU lower > 1.005, "
                "and wall lower >= 1.0"
            ),
        },
        "retuning_after_timing": False,
        "claim_boundary": (
            "supported-host exact-schedule replay using upstream BOLT passes; "
            "not original wall-clock behavior, Cortex-M, or FastDyn"
        ),
    }
    plan_path = OUTPUT / "pass-timing-plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text()) != plan:
            raise RuntimeError("existing pass timing plan differs")
    else:
        write_json(plan_path, plan)

    training = run_benchmark(
        "pass-training", 12, 20261220,
        [("stock_bolt", variants["stock_bolt"])]
        + [(name, variants[name]) for name in CANDIDATES],
    )
    rows = []
    eligible = []
    for name in CANDIDATES:
        metrics = training["paired_comparisons"][name]
        cpu_lower = metrics["target_cpu_ns"]["bootstrap_95_percent"][0]
        wall_lower = metrics["wall_ns"]["bootstrap_95_percent"][0]
        qualifies = cpu_lower > CPU_GATE and wall_lower >= WALL_GATE
        rows.append({
            "candidate": name, "target_cpu": metrics["target_cpu_ns"],
            "wall": metrics["wall_ns"], "qualifies": qualifies,
        })
        if qualifies:
            eligible.append((cpu_lower, name))
    eligible.sort(reverse=True)
    selected = eligible[0][1] if eligible else None
    selection = {
        "schema_version": 1,
        "status": "selected" if selected else "no_candidate_cleared_training_gate",
        "selected": selected,
        "candidates": rows,
        "training_sha256": sha256(OUTPUT / "pass-training.json"),
    }
    write_json(OUTPUT / "pass-selection.json", selection)
    if selected is None:
        print(json.dumps(selection, indent=2, sort_keys=True))
        return 2

    held_out = run_benchmark(
        "pass-held-out", 30, 20261221,
        [("stock_bolt", variants["stock_bolt"]), (selected, variants[selected])],
    )
    metrics = held_out["paired_comparisons"][selected]
    all_ok = all(value["all_ok"] for value in held_out["variants"].values())
    accepted = (
        all_ok
        and metrics["target_cpu_ns"]["bootstrap_95_percent"][0] > CPU_GATE
        and metrics["wall_ns"]["bootstrap_95_percent"][0] >= WALL_GATE
    )
    report = {
        "schema_version": 1,
        "status": "accepted" if accepted else "not_profitable",
        "selected": selected,
        "all_exact_work_and_trace_checks_match": all_ok,
        "stock_bolt_over_candidate": metrics,
        "selection": selection,
        "held_out_sha256": sha256(OUTPUT / "pass-held-out.json"),
        "advance_rule": plan["held_out"]["advance_rule"],
        "claim_boundary": plan["claim_boundary"],
    }
    write_json(OUTPUT / "pass-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 3


if __name__ == "__main__":
    raise SystemExit(main())
