#!/usr/bin/env python3
"""Build the machine-readable result for the bounded queue BOLT policy study."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import statistics
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
QUEUE = ROOT / "build/preflight/lemix-queue"


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def paired_ratio(
    timing: dict[str, Any], numerator: str, denominator: str, metric: str
) -> dict[str, Any]:
    left = {
        int(item["block"]): float(item[metric])
        for item in timing["variants"][numerator]["samples"]
    }
    right = {
        int(item["block"]): float(item[metric])
        for item in timing["variants"][denominator]["samples"]
    }
    blocks = sorted(set(left) & set(right))
    ratios = [left[block] / right[block] for block in blocks]
    randomizer = random.Random(20261116)
    bootstrap = [
        statistics.median(
            ratios[randomizer.randrange(len(ratios))] for _ in ratios
        )
        for _ in range(100_000)
    ]
    return {
        "definition": f"{numerator}_{metric}_over_{denominator}_{metric}",
        "paired_block_count": len(blocks),
        "median_ratio": statistics.median(ratios),
        "bootstrap_95_percent": [
            percentile(bootstrap, 0.025),
            percentile(bootstrap, 0.975),
        ],
        "bootstrap_resamples": 100_000,
    }


def valgrind_count(path: Path, label: str) -> int:
    text = path.read_text()
    match = re.search(rf"{re.escape(label)}:\s+([0-9,]+)", text)
    if match is None:
        raise RuntimeError(f"missing {label!r} in {path}")
    return int(match.group(1).replace(",", ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=QUEUE / "bolt-policy-matrix-2026-11-16/report.json",
    )
    args = parser.parse_args()

    arm_paths = {
        "ordinary_fop": "bolt-policy-baseline-1k",
        "peepholes": "bolt-policy-peepholes",
        "register_reassignment": "bolt-policy-regreassign",
        "aggressive_register_reassignment": "bolt-policy-regreassign-aggressive",
        "experimental_store_removal": "bolt-policy-frame-store-removal",
    }
    arms: dict[str, Any] = {}
    for name, directory_name in arm_paths.items():
        directory = QUEUE / directory_name
        result = load(directory / "wrapper-result.json")
        compact = directory / "lemix_queue_workload.bolt-qualified-compact"
        full = directory / "lemix_queue_workload.bolt-fop"
        stdout = (directory / "bolt.stdout").read_text()
        arms[name] = {
            "directory": str(directory),
            "optimization_policy": result["optimization_policy"],
            "wrapper_status": result["status"],
            "qualified_function_count": result["qualified"]["function_count"],
            "saved_live_bytes": result["qualified"]["saved_live_bytes"],
            "effect_contract_rejections": result["rejections"]["effect_contract"],
            "fop": result["fop"],
            "compact_sha256": sha256(compact),
            "full_sha256": sha256(full),
            "bolt_reported_no_register_reassignment_changes": (
                "Reg Reassignment Pass: no changes were made" in stdout
            ),
            "bolt_reported_zero_useless_conditional_branches": (
                "Peephole: 0 useless conditional branches removed" in stdout
            ),
        }

    timing_path = QUEUE / "bolt-policy-matrix-timing-2026-11-16/report.json"
    timing = load(timing_path)
    composition_path = QUEUE / "bolt-policy-composed-2026-11-16/report.json"
    composition = load(composition_path)
    diagnostic_dir = QUEUE / "bolt-policy-matrix-timing-2026-11-16"
    diagnostics: dict[str, Any] = {}
    for name in ("reference", "baseline", "store_removal"):
        callgrind_log = diagnostic_dir / "callgrind" / f"{name}.log"
        cachegrind_log = diagnostic_dir / "cachegrind" / f"{name}.log"
        diagnostics[name] = {
            "callgrind_instruction_refs": valgrind_count(callgrind_log, "I   refs"),
            "cachegrind_instruction_refs": valgrind_count(cachegrind_log, "I refs"),
            "cachegrind_branches": valgrind_count(cachegrind_log, "Branches"),
            "cachegrind_mispredicts": valgrind_count(cachegrind_log, "Mispredicts"),
            "callgrind_log": str(callgrind_log),
            "cachegrind_log": str(cachegrind_log),
        }

    baseline_ir = diagnostics["baseline"]["callgrind_instruction_refs"]
    reference_ir = diagnostics["reference"]["callgrind_instruction_refs"]
    store_ir = diagnostics["store_removal"]["callgrind_instruction_refs"]
    baseline_hash = arms["ordinary_fop"]["compact_sha256"]
    report = {
        "schema_version": 1,
        "status": "complete_bounded_policy_comparison",
        "scope": (
            "supported-host fixed-work LEMIX queue fixture; not Cortex-M or "
            "FastDyn throughput"
        ),
        "arms": arms,
        "byte_identity_findings": {
            "peepholes_compact_equals_ordinary_fop": (
                arms["peepholes"]["compact_sha256"] == baseline_hash
            ),
            "register_reassignment_compact_equals_ordinary_fop": (
                arms["register_reassignment"]["compact_sha256"] == baseline_hash
            ),
            "aggressive_register_reassignment_compact_equals_ordinary_fop": (
                arms["aggressive_register_reassignment"]["compact_sha256"]
                == baseline_hash
            ),
            "composed_equals_ordinary_fop": (
                composition["output"]["sha256"] == baseline_hash
            ),
            "composition_selected_experimental_body_count": composition[
                "selected_experimental_body_count"
            ],
        },
        "timing": {
            "raw_report": str(timing_path),
            "raw_report_sha256": sha256(timing_path),
            "reference_over_ordinary_fop": timing["paired_comparisons"][
                "baseline_compact"
            ],
            "reference_over_store_removal": timing["paired_comparisons"][
                "store_removal_compact"
            ],
            "ordinary_fop_over_store_removal": {
                "cpu_ns": paired_ratio(
                    timing, "baseline_compact", "store_removal_compact", "cpu_ns"
                ),
                "wall_ns": paired_ratio(
                    timing, "baseline_compact", "store_removal_compact", "wall_ns"
                ),
            },
        },
        "deterministic_work_diagnostics": {
            "variants": diagnostics,
            "reference_over_ordinary_fop_instruction_refs": reference_ir / baseline_ir,
            "ordinary_fop_instruction_ref_reduction": reference_ir - baseline_ir,
            "ordinary_fop_instruction_ref_reduction_percent": (
                (reference_ir - baseline_ir) / reference_ir * 100
            ),
            "store_removal_minus_ordinary_fop_instruction_refs": store_ir - baseline_ir,
            "measurement_note": (
                "Valgrind instruction references and simulated branch counts explain "
                "mechanism; they are not native hardware retired-instruction counters"
            ),
        },
        "observed_conclusions": [
            "ordinary FOP reduces deterministic dynamic instruction work",
            "the new native timing block does not reproduce a resolved speedup",
            "peepholes and register reassignment expose no compact-body opportunity",
            "experimental store removal changes 24 stores but 13 affected functions fail the declared memory-effect contract",
            "accepted experimental bodies add no bytes beyond ordinary FOP after in-place reference repair",
        ],
        "decision": (
            "retain ordinary FOP behind the firmware wrapper; reject store-removal "
            "bodies that alter explicit stores; do not expand these x86 policies further "
            "for this workload"
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "sha256": sha256(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
