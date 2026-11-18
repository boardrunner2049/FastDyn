#!/usr/bin/env python3
"""Validate and decide the frozen SmartSpeaker layout training experiment."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "build/preflight/lemix-smartspeaker-layout-matrix-2026-11-17"


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode() + b"\n"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    screen_path = OUTPUT / "diagnostic-screen.json"
    training_path = OUTPUT / "layout-training.json"
    result_path = OUTPUT / "layout-result.json"
    if result_path.exists():
        raise SystemExit(f"result already exists: {result_path}")
    screen = json.loads(screen_path.read_text())
    training = json.loads(training_path.read_text())
    plan = screen["native_training"]
    method = training["method"]
    if (
        method["reference"] != plan["baseline"]
        or method["samples_per_variant"] != plan["paired_blocks"]
        or method["duration_seconds"] != plan["duration_seconds"]
        or method["order_seed"] != plan["seed"]
        or not method["paired_blocks"]
    ):
        raise SystemExit("training method does not match frozen plan")
    expected = {plan["baseline"], *plan["selected_ids"]}
    if set(training["variants"]) != expected:
        raise SystemExit("measured variants do not match frozen selection")

    trace_hashes: set[str] = set()
    for identifier, variant in training["variants"].items():
        if not variant["all_ok"]:
            raise SystemExit(f"failed execution: {identifier}")
        for sample in variant["samples"]:
            if sample["interrupt_dispatches"] != 1500:
                raise SystemExit(f"unexpected completed markers: {identifier}")
            if sample["interrupt_trace_prefix_events"] != 400:
                raise SystemExit(f"short trace prefix: {identifier}")
            trace_hashes.add(sample["interrupt_trace_prefix_sha256"])
    if len(trace_hashes) != 1:
        raise SystemExit("interrupt trace differs across variants or samples")

    comparisons = training["paired_comparisons"]
    qualified = [
        (identifier, result)
        for identifier, result in comparisons.items()
        if result["bootstrap_95_percent"][0] > 1.0
    ]
    winner = max(
        qualified,
        key=lambda item: (item[1]["bootstrap_95_percent"][0], item[0]),
        default=None,
    )
    report = {
        "schema_version": 1,
        "status": "native_training_complete",
        "diagnostic_screen_sha256": sha256(screen_path),
        "layout_training_sha256": sha256(training_path),
        "decision": "advance_to_held_out" if winner else "no_layout_candidate_advances",
        "winner": winner[0] if winner else None,
        "reason": (
            "largest lower 95% paired-bootstrap bound is above 1.0"
            if winner else
            "no candidate's lower 95% paired-bootstrap bound is above 1.0"
        ),
        "paired_results": comparisons,
        "matched_execution": {
            "executions": sum(
                len(item["samples"]) for item in training["variants"].values()
            ),
            "markers_per_execution": 1500,
            "trace_prefix_events": 400,
            "trace_prefix_sha256": next(iter(trace_hashes)),
        },
        "claims": screen["claim_boundaries"],
    }
    result_path.write_bytes(canonical(report))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
