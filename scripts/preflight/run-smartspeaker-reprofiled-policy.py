#!/usr/bin/env python3
"""Select a marker-profiled BOLT pipeline and verify it on held-out blocks."""

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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def benchmark(
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
        "marker_fop": OUTPUT / "SmartSpeaker.marker_fop",
        "marker_inline": OUTPUT / "SmartSpeaker.marker_inline",
        "marker_icp_inline": OUTPUT / "SmartSpeaker.marker_icp_inline",
    }
    hashes = {name: sha256(path) for name, path in variants.items()}
    expected = {
        "stock_bolt": "af6b32603c83b741c129473e5f6f18587a63e2cc44edaec1834b07c9c2a64fab",
        "marker_fop": "a84dcb853e8b7143f4269e20144cc9bfe692c34d8f65c9b16519e616acd0e02d",
        "marker_inline": "458d429cbf116eeda2f22d26f88d3dc633f2686d03a865b1f8010fc05c7c1cdc",
        "marker_icp_inline": "7afb112bd2b63597c52a73895c41de88fb892ea3271812837844c12a44557cdf",
    }
    if hashes != expected:
        raise RuntimeError(f"reprofiled binary hashes changed: {hashes}")
    profile = OUTPUT / "marker-training.fdata"
    plan = {
        "schema_version": 1,
        "status": "frozen_before_timing",
        "profile": {"path": str(profile), "sha256": sha256(profile)},
        "preload": {"path": str(PRELOAD), "sha256": sha256(PRELOAD)},
        "work": {
            "completed_units": WORK_UNITS,
            "ticks_per_unit": 10,
            "total_ticks": WORK_UNITS * 10,
            "marker_settle_ns": SETTLE_NS,
        },
        "binaries": {
            name: {"path": str(variants[name]), "sha256": expected[name]}
            for name in variants
        },
        "training": {
            "paired_blocks": 12,
            "seed": 20261217,
            "selection": (
                "largest target-CPU lower bound with target-CPU lower > 1.005 "
                "and wall lower >= 1.0"
            ),
        },
        "held_out": {
            "paired_blocks": 30,
            "seed": 20261218,
            "advance_rule": (
                "all exact-work and trace checks pass, target-CPU lower > 1.005, "
                "and wall lower >= 1.0"
            ),
        },
        "retuning_after_timing": False,
        "claim_boundary": (
            "supported-host LEMIX SmartSpeaker under an exact POSIX marker/tick "
            "schedule; not original wall-clock behavior, Cortex-M, or FastDyn"
        ),
    }
    plan_path = OUTPUT / "timing-plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text()) != plan:
            raise RuntimeError("timing plan changed")
    else:
        write_json(plan_path, plan)

    training = benchmark(
        "timing-training", 12, 20261217,
        [(name, path) for name, path in variants.items()],
    )
    candidates = list(variants)[1:]
    rows = []
    eligible = []
    for name in candidates:
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
        "training_sha256": sha256(OUTPUT / "timing-training.json"),
    }
    write_json(OUTPUT / "timing-selection.json", selection)
    if selected is None:
        print(json.dumps(selection, indent=2, sort_keys=True))
        return 2

    held_out = benchmark(
        "timing-held-out", 30, 20261218,
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
        "held_out_sha256": sha256(OUTPUT / "timing-held-out.json"),
        "advance_rule": plan["held_out"]["advance_rule"],
        "claim_boundary": plan["claim_boundary"],
    }
    write_json(OUTPUT / "timing-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 3


if __name__ == "__main__":
    raise SystemExit(main())
