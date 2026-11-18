#!/usr/bin/env python3
"""Validate and summarize the checked SmartSpeaker stock-BOLT FOP overlay."""

from __future__ import annotations

import hashlib
import json
import argparse
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "build/preflight/lemix-smartspeaker-stock-fop-overlay-2026-11-17"
ACCEL = ROOT / "build/preflight/lemix-smartspeaker-accelerated-overlay-2026-11-17"
RERUN = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode() + b"\n"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_daemon_run(path: Path, expected_samples: int) -> tuple[dict[str, Any], str]:
    document = json.loads(path.read_text())
    if document["method"]["samples_per_variant"] != expected_samples:
        raise SystemExit(f"unexpected sample count: {path}")
    traces: set[str] = set()
    for identifier, variant in document["variants"].items():
        if not variant["all_ok"]:
            raise SystemExit(f"invalid execution in {path}: {identifier}")
        for sample in variant["samples"]:
            if sample["interrupt_trace_prefix_events"] != 400:
                raise SystemExit(f"short trace in {path}: {identifier}")
            traces.add(sample["interrupt_trace_prefix_sha256"])
    if len(traces) != 1:
        raise SystemExit(f"trace mismatch: {path}")
    return document, next(iter(traces))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replace-report", action="store_true")
    args = parser.parse_args()
    result_path = OVERLAY / "report.json"
    if result_path.exists() and not args.replace_report:
        raise SystemExit(f"report already exists: {result_path}")
    selection_path = OVERLAY / "selection.json"
    training_path = OVERLAY / "training.json"
    smoke_path = OVERLAY / "smoke.json"
    selection = json.loads(selection_path.read_text())
    smoke, smoke_trace = validate_daemon_run(smoke_path, 1)
    training, training_trace = validate_daemon_run(training_path, 10)
    if smoke_trace != training_trace:
        raise SystemExit("smoke/training traces differ")
    plan = selection["measurement_plan"]["training"]
    method = training["method"]
    if (
        method["samples_per_variant"] != plan["paired_blocks"]
        or method["duration_seconds"] != plan["duration_seconds"]
        or method["order_seed"] != plan["seed"]
        or method["reference"] != "stock_bolt"
    ):
        raise SystemExit("training does not match frozen plan")
    paired = training["paired_comparisons"]["checked_hot_fop_overlay"]
    advances = paired["bootstrap_95_percent"][0] > 1.0

    provenance_path = RERUN / "accessor-harness/body-provenance-frs.json"
    performance_path = RERUN / "accessor-harness/paired-results.json"
    provenance = json.loads(provenance_path.read_text())
    performance = json.loads(performance_path.read_text())
    accessor_edit = next(
        item for item in selection["edits"]
        if item["symbol"] == "prvGetThreadFromTask"
    )
    if (
        accessor_edit["candidate_body_sha256"]
        != provenance["comparisons"]["fop"]["production_sha256"]
    ):
        raise SystemExit("overlay accessor differs from measured FOP body")

    calibration_10x_path = ACCEL / "calibration-10x.json"
    calibration_100x_path = ACCEL / "calibration-100x.json"
    calibration_10x, accelerated_trace = validate_daemon_run(
        calibration_10x_path, 1
    )
    calibration_100x = json.loads(calibration_100x_path.read_text())
    if accelerated_trace != training_trace:
        raise SystemExit("accelerated diagnostic changed the trace prefix")

    confirmatory_path = ACCEL / "confirmatory-10x.json"
    confirmatory_plan_path = ACCEL / "confirmatory-plan.json"
    confirmatory = None
    confirmatory_paired = None
    confirmatory_advances = False
    if confirmatory_path.is_file():
        confirmatory, confirmatory_trace = validate_daemon_run(
            confirmatory_path, 30
        )
        if confirmatory_trace != training_trace:
            raise SystemExit("10x confirmatory trace differs from original schedule")
        plan_10x = json.loads(confirmatory_plan_path.read_text())
        method_10x = confirmatory["method"]
        if (
            method_10x["samples_per_variant"] != plan_10x["paired_blocks"]
            or method_10x["duration_seconds"] != plan_10x["duration_seconds"]
            or method_10x["order_seed"] != plan_10x["order_seed"]
            or sha256(Path(confirmatory["preload"]["path"]))
            != plan_10x["preload_sha256"]
        ):
            raise SystemExit("10x confirmatory run does not match frozen plan")
        confirmatory_paired = confirmatory["paired_comparisons"][
            "checked_hot_fop_overlay"
        ]
        confirmatory_advances = (
            confirmatory_paired["bootstrap_95_percent"][0] > 1.005
        )

    report = {
        "schema_version": 1,
        "status": "checked_overlay_evaluation_complete",
        "decision": (
            "advance_overlay_to_held_out" if advances
            else "retain_stock_bolt_for_paced_application"
        ),
        "selection_sha256": sha256(selection_path),
        "smoke_sha256": sha256(smoke_path),
        "training_sha256": sha256(training_path),
        "overlay": {
            "sha256": selection["output"]["sha256"],
            "selected_symbols": [item["symbol"] for item in selection["edits"]],
            "selected_profile_entries": selection["cohort"][
                "selected_profile_entry_count"
            ],
            "live_bytes_saved_in_place": sum(
                item["padding_bytes"] for item in selection["edits"]
            ),
            "changed_instruction_origin_mappings": sum(
                item["origin_mapping_count"] for item in selection["edits"]
            ),
            "same_file_size_as_stock_bolt": selection["output"][
                "same_file_size_as_stock_bolt"
            ],
        },
        "paced_application": {
            "stock_bolt_over_overlay_cpu_per_marker": paired,
            "advance_rule": selection["measurement_plan"]["advance_rule"],
            "matched_executions": 20,
            "markers_per_execution": 3000,
            "trace_prefix_events": 400,
            "trace_prefix_sha256": training_trace,
            "conclusion": (
                "inconclusive and does not advance; this is not proof of equality"
            ),
        },
        "function_level": {
            "symbol": "prvGetThreadFromTask",
            "overlay_body_matches_measured_fop_body": True,
            "body_sha256": accessor_edit["candidate_body_sha256"],
            "stock_bytes": accessor_edit["capacity_bytes"],
            "overlay_live_bytes": accessor_edit["live_candidate_bytes"],
            "stock_over_fop_cpu_per_call_median": performance[
                "median_reference_over_fop"
            ],
            "bootstrap_95_percent": performance["bootstrap_95_percent"],
            "cpu_cost_reduction_percent": (
                1.0 - 1.0 / performance["median_reference_over_fop"]
            ) * 100.0,
            "scope": performance["scope"],
            "provenance_sha256": sha256(provenance_path),
            "performance_sha256": sha256(performance_path),
        },
        "accelerated_schedule_diagnostics": {
            "ten_x": {
                "status": "calibration_valid_not_final_timing",
                "samples_sha256": sha256(calibration_10x_path),
                "preload": calibration_10x["preload"],
                "markers": {
                    name: value["median_interrupt_dispatches"]
                    for name, value in calibration_10x["variants"].items()
                },
                "cpu_seconds": {
                    name: value["median_cpu_seconds"]
                    for name, value in calibration_10x["variants"].items()
                },
                "confirmatory": (
                    {
                        "status": (
                            "strict_accelerated_environment_win"
                            if confirmatory_advances else
                            "did_not_clear_accelerated_environment_gate"
                        ),
                        "plan_sha256": sha256(confirmatory_plan_path),
                        "samples_sha256": sha256(confirmatory_path),
                        "paired_result": confirmatory_paired,
                        "matched_executions": 60,
                        "markers_per_execution": 20_000,
                        "advance_rule": (
                            "lower 95% paired-bootstrap bound above 1.005"
                        ),
                    }
                    if confirmatory is not None else None
                ),
            },
            "hundred_x": {
                "status": "invalid_capacity_calibration",
                "reason": (
                    "stock process did not terminate after SIGINT and was killed; "
                    "its apparent CPU ratio includes five extra seconds and is invalid"
                ),
                "samples_sha256": sha256(calibration_100x_path),
                "stock_returncode": calibration_100x["variants"]["stock_bolt"]
                    ["samples"][0]["returncode"],
            },
            "claim_boundary": (
                "accelerated schedules are distinct environments and do not establish "
                "original host-timed, MCU, or FastDyn capacity"
            ),
        },
        "callgrind": {
            "status": "unsupported_for_daemon_fixture",
            "reason": (
                "ordinary MMIO preload is not visible in Valgrind client space; "
                "the diagnostic MAP_FIXED shim runs the workload but the threaded "
                "daemon cannot be cleanly stopped into a nonempty Callgrind dump"
            ),
        },
        "claim_boundaries": selection["claim_boundaries"],
    }
    result_path.write_bytes(canonical(report))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
