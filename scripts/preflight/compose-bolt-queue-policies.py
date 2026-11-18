#!/usr/bin/env python3
"""Compose independently accepted BOLT bodies in the original queue layout.

The ordinary-FOP compact image is the fallback.  A body from an experimental
policy replaces it only when both wrapper runs accepted the same original
function identity and the experimental wrapper retained the explicit memory
effect contract.  This is deliberately narrower than publishing either full
relocated BOLT image.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TICK_RE = re.compile(r" tick=\d+$")


def load_wrapper() -> Any:
    path = ROOT / "scripts/preflight/run-bolt-firmware-wrapper.py"
    spec = importlib.util.spec_from_file_location("fastdyn_bolt_wrapper", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load wrapper helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def observe(path: Path) -> dict[str, object]:
    result = subprocess.run([str(path)], capture_output=True, text=True, check=False)
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "normalized_output": TICK_RE.sub("", result.stdout.strip()),
        "stderr": result.stderr.strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--baseline-result", type=Path, required=True)
    parser.add_argument("--baseline-image", type=Path, required=True)
    parser.add_argument("--experimental-result", type=Path, required=True)
    parser.add_argument("--experimental-image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    wrapper = load_wrapper()
    reference = args.reference.resolve()
    baseline_image = args.baseline_image.resolve()
    experimental_image = args.experimental_image.resolve()
    baseline = json.loads(args.baseline_result.read_text())
    experimental = json.loads(args.experimental_result.read_text())

    for label, result in (("baseline", baseline), ("experimental", experimental)):
        if result.get("status") != "integration_accepted":
            raise RuntimeError(f"{label} wrapper result was not accepted")
        if result["inputs"]["reference_sha256"] != sha256(reference):
            raise RuntimeError(f"{label} wrapper used a different reference")

    baseline_edits = {
        item["symbol"]: item for item in baseline["qualified"]["edits"]
    }
    experimental_edits = {
        item["symbol"]: item for item in experimental["qualified"]["edits"]
    }
    reference_functions = wrapper.function_records(reference)
    baseline_functions = wrapper.function_records(baseline_image)
    experimental_functions = wrapper.function_records(experimental_image)

    image = bytearray(baseline_image.read_bytes())
    selected: list[dict[str, object]] = []
    for name in sorted(set(baseline_edits) & set(experimental_edits)):
        contract = experimental_edits[name]
        if contract.get("classification") not in {
            "exact_explicit_memory_sequence",
            "bolt_fop_store_to_load_dependency",
        }:
            raise RuntimeError(f"{name}: unsupported experimental effect contract")
        original = reference_functions.get(name)
        baseline_body = baseline_functions.get(name)
        experimental_body = experimental_functions.get(name)
        if original is None or baseline_body is None or experimental_body is None:
            raise RuntimeError(f"{name}: ambiguous or missing function identity")
        capacity = len(bytes(original["bytes"]))
        before = bytes(baseline_body["bytes"])
        after = bytes(experimental_body["bytes"])
        if len(before) != capacity or len(after) != capacity:
            raise RuntimeError(f"{name}: compact symbol capacity changed")
        if before == after:
            continue
        if before[:4] == bytes.fromhex("f30f1efa") and after[:4] != before[:4]:
            raise RuntimeError(f"{name}: protected entry changed")
        start = int(original["file_offset"])
        image[start : start + capacity] = after
        selected.append(
            {
                "symbol": name,
                "original_address": int(original["address"]),
                "file_offset": start,
                "capacity_bytes": capacity,
                "baseline_body_sha256": sha256_bytes(before),
                "experimental_body_sha256": sha256_bytes(after),
                "effect_contract": contract["classification"],
                "forwarding_dependencies": contract.get(
                    "forwarding_dependencies", []
                ),
                "known_execution_count": int(
                    contract.get("known_execution_count", 0)
                ),
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_bytes(image)
    temporary.chmod(baseline_image.stat().st_mode)
    os.replace(temporary, args.output)

    observations = {
        "reference": observe(reference),
        "baseline": observe(baseline_image),
        "experimental": observe(experimental_image),
        "composed": observe(args.output),
    }
    expected = observations["reference"]
    mismatches = [
        name
        for name, item in observations.items()
        if item["returncode"] != expected["returncode"]
        or item["normalized_output"] != expected["normalized_output"]
    ]
    report = {
        "schema_version": 1,
        "status": "integration_accepted" if not mismatches else "rejected",
        "reason_codes": mismatches or [
            "per_function_contracts_and_fixed_work_output_passed"
        ],
        "policy": {
            "kind": "accepted_per_function_bolt_policy_composition",
            "fallback": baseline["optimization_policy"],
            "experimental": experimental["optimization_policy"],
            "selection_rule": (
                "use the experimental body only when both wrappers accepted the "
                "same original function identity and the experimental memory-effect "
                "contract is explicit; otherwise retain ordinary FOP"
            ),
        },
        "inputs": {
            "reference": str(reference),
            "reference_sha256": sha256(reference),
            "baseline_image": str(baseline_image),
            "baseline_sha256": sha256(baseline_image),
            "experimental_image": str(experimental_image),
            "experimental_sha256": sha256(experimental_image),
        },
        "selected_experimental_bodies": selected,
        "selected_experimental_body_count": len(selected),
        "retained_baseline_body_count": len(baseline_edits) - len(selected),
        "output": {
            "path": str(args.output.resolve()),
            "sha256": sha256(args.output),
            "file_bytes": args.output.stat().st_size,
        },
        "fixed_work_observations": observations,
        "limitations": {
            "whole_machine_equivalence_proved": False,
            "patch_contract": "empty_phase_one_contract",
            "publication_scope": "supported-host fixed-layout integration fixture",
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "selected_experimental_body_count": len(selected),
                "output_sha256": report["output"]["sha256"],
                "report": str(args.report.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
