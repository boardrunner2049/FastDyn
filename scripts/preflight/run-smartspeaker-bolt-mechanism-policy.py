#!/usr/bin/env python3
"""Train and evaluate the frozen BOLT-native SmartSpeaker mechanism policy."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "build/preflight/lemix-smartspeaker-bolt-mechanism-policy-2026-11-17"
BASELINE = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16/binaries"
MECHANISMS = ROOT / "build/preflight/lemix-smartspeaker-bolt-mechanisms-2026-11-17"
PRELOAD = (
    ROOT / "build/preflight/lemix-smartspeaker-accelerated-overlay-2026-11-17/"
    "lemix_mmio_10x_tick.so"
)
BENCHMARK = ROOT / "scripts/preflight/benchmark-lemix-daemon.py"
MINIMUM_USEFUL_RATIO = 1.005


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def run_benchmark(
    name: str,
    *,
    samples: int,
    duration: int,
    seed: int,
    variants: list[tuple[str, Path]],
) -> dict[str, Any]:
    output = OUTPUT / f"{name}.json"
    log = OUTPUT / f"{name}.log"
    if output.exists() or log.exists():
        raise RuntimeError(f"refusing to overwrite {output} or {log}")
    command = [
        "taskset", "-c", "24,25", sys.executable, str(BENCHMARK),
        "--paired-blocks", "--samples", str(samples),
        "--duration", str(duration), "--seed", str(seed),
        "--preload", str(PRELOAD),
    ]
    for variant, path in variants:
        command.extend(("--variant", f"{variant}={path}"))
    command.extend(("--output", str(output)))
    with log.open("w") as stream:
        result = subprocess.run(
            command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"{name} failed with status {result.returncode}; see {log}")
    return json.loads(output.read_text())


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((MECHANISMS / "manifest.json").read_text())
    frozen_ids = manifest["candidate_policy"]["frozen_ids"]
    if frozen_ids != ["icp", "inline_small", "icp_inline_small"]:
        raise RuntimeError(f"unexpected frozen candidate IDs: {frozen_ids}")

    binaries = {
        "stock_bolt": BASELINE / "SmartSpeaker.L1",
        "full_fop": BASELINE / "SmartSpeaker.F1",
        **{
            item["id"]: Path(item["image"])
            for item in manifest["candidates"]
        },
    }
    expected_hashes = {
        "stock_bolt": "4b7a4ee59e0ce6d01f028e8bb55d7260be13687cae6b83a176fb5c5ad797ccb0",
        "full_fop": manifest["baseline"]["sha256"],
        **{item["id"]: item["sha256"] for item in manifest["candidates"]},
    }
    for name, path in binaries.items():
        if not path.is_file() or sha256(path) != expected_hashes[name]:
            raise RuntimeError(f"missing or changed frozen binary: {name}: {path}")

    calibration = OUTPUT / "calibration.json"
    if not calibration.is_file():
        raise RuntimeError("run the recorded one-block calibration before training")
    plan_path = OUTPUT / "measurement-plan.json"
    plan = {
        "schema_version": 1,
        "status": "frozen_before_training",
        "calibration": {
            "path": str(calibration),
            "sha256": sha256(calibration),
            "purpose": "smoke and duration calibration only; not candidate selection",
        },
        "environment": {
            "schedule": "separately identified 10x POSIX tick schedule",
            "preload": str(PRELOAD),
            "preload_sha256": sha256(PRELOAD),
            "affinity_cpus": [24, 25],
            "claim_boundary": (
                "unchanged SmartSpeaker machine code under zero-backed MMIO; "
                "not original wall-clock timing, Cortex-M, or FastDyn"
            ),
        },
        "frozen_binaries": {
            name: {"path": str(path), "sha256": expected_hashes[name]}
            for name, path in binaries.items()
        },
        "training": {
            "reference": "full_fop",
            "candidates": frozen_ids,
            "paired_blocks": 12,
            "duration_seconds": 10,
            "seed": 20261211,
            "selection": "largest lower 95% paired-bootstrap CPU-per-marker bound",
            "minimum_useful_ratio": MINIMUM_USEFUL_RATIO,
        },
        "held_out": {
            "reference": "stock_bolt",
            "paired_blocks": 30,
            "duration_seconds": 20,
            "seed": 20261212,
            "acceptance": (
                "matched behavior and lower 95% stock/candidate CPU-per-marker "
                f"bound above {MINIMUM_USEFUL_RATIO}"
            ),
        },
        "retuning_after_training_or_held_out": False,
    }
    if plan_path.exists():
        if json.loads(plan_path.read_text()) != plan:
            raise RuntimeError("existing measurement plan differs from frozen plan")
    else:
        write_json(plan_path, plan)

    training = run_benchmark(
        "training",
        samples=plan["training"]["paired_blocks"],
        duration=plan["training"]["duration_seconds"],
        seed=plan["training"]["seed"],
        variants=[("full_fop", binaries["full_fop"])]
        + [(name, binaries[name]) for name in frozen_ids],
    )
    ranked = sorted(
        (
            (
                training["paired_comparisons"][name]["bootstrap_95_percent"][0],
                name,
            )
            for name in frozen_ids
        ),
        key=lambda item: (-item[0], item[1]),
    )
    lower_bound, selected = ranked[0]
    selection = {
        "schema_version": 1,
        "status": (
            "selected_for_held_out"
            if lower_bound > MINIMUM_USEFUL_RATIO
            else "no_candidate_cleared_training_gate"
        ),
        "training_sha256": sha256(OUTPUT / "training.json"),
        "ranked_lower_bounds": [
            {"candidate": name, "lower_95_percent": lower}
            for lower, name in ranked
        ],
        "selected": selected if lower_bound > MINIMUM_USEFUL_RATIO else None,
        "selection_gate": MINIMUM_USEFUL_RATIO,
    }
    write_json(OUTPUT / "selection.json", selection)
    if selection["selected"] is None:
        print(json.dumps(selection, indent=2, sort_keys=True))
        return 2

    held_out = run_benchmark(
        "held-out",
        samples=plan["held_out"]["paired_blocks"],
        duration=plan["held_out"]["duration_seconds"],
        seed=plan["held_out"]["seed"],
        variants=[("stock_bolt", binaries["stock_bolt"]), (selected, binaries[selected])],
    )
    comparison = held_out["paired_comparisons"][selected]
    all_ok = held_out["validation"]["all_executions_ok"]
    accepted = all_ok and comparison["bootstrap_95_percent"][0] > MINIMUM_USEFUL_RATIO
    report = {
        "schema_version": 1,
        "status": "accepted" if accepted else "not_profitable",
        "selected": selected,
        "selection": selection,
        "held_out_sha256": sha256(OUTPUT / "held-out.json"),
        "all_behavior_checks_match": all_ok,
        "stock_bolt_over_candidate_cpu_per_marker": comparison,
        "advance_rule": plan["held_out"]["acceptance"],
        "claim_boundary": plan["environment"]["claim_boundary"],
    }
    write_json(OUTPUT / "report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 3


if __name__ == "__main__":
    raise SystemExit(main())
