#!/usr/bin/env python3
"""Isolate newly adopted compact BOLT bodies that fail the installation smoke."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
WRAPPER_PATH = ROOT / "scripts/preflight/run-bolt-firmware-wrapper.py"


def load_wrapper() -> Any:
    spec = importlib.util.spec_from_file_location("fastdyn_bolt_wrapper", WRAPPER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load BOLT wrapper module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--safe-dir", type=Path, required=True)
    parser.add_argument("--failed-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=0.5)
    args = parser.parse_args()

    safe_dir = args.safe_dir.resolve()
    failed_dir = args.failed_dir.resolve()
    safe_result = json.loads((safe_dir / "wrapper-result.json").read_text())
    failed_result = json.loads((failed_dir / "wrapper-result.json").read_text())
    safe_names = {
        item["symbol"]
        for item in safe_result["memory_effect_qualified_adoption"]["edits"]
    }
    failed_names = {
        item["symbol"]
        for item in failed_result["memory_effect_qualified_adoption"]["edits"]
    }
    new_names = sorted(failed_names - safe_names)
    if not new_names:
        raise SystemExit("failed arm contains no newly adopted functions")

    wrapper = load_wrapper()
    safe_image = safe_dir / "SmartSpeaker.bolt-qualified-compact"
    failed_image = failed_dir / "SmartSpeaker.bolt-qualified-compact"
    probe = args.output.resolve().with_suffix(".probe")
    trials: list[dict[str, Any]] = []
    cache: dict[tuple[str, ...], bool] = {}

    def fails(names: list[str]) -> bool:
        key = tuple(sorted(names))
        if key in cache:
            return cache[key]
        wrapper.emit_compact_selected(
            safe_image, failed_image, probe, key
        )
        observation = wrapper.observe(probe, args.seconds)
        failed = (
            observation["returncode"] < 0
            or observation["dispatch_markers"] == 0
        )
        cache[key] = failed
        trials.append(
            {
                "functions": list(key),
                "function_count": len(key),
                "failed": failed,
                "observation": observation,
            }
        )
        return failed

    baseline = wrapper.observe(safe_image, args.seconds)
    complete_failed = fails(new_names)
    if baseline["dispatch_markers"] == 0 or baseline["returncode"] < 0:
        raise RuntimeError("known-good baseline failed the diagnostic smoke")
    if not complete_failed:
        raise RuntimeError("newly adopted cohort did not reproduce the failure")

    remaining = list(new_names)
    isolated: list[str] = []
    interaction_sets: list[list[str]] = []
    while remaining and fails(remaining):
        subset = list(remaining)
        while len(subset) > 1:
            midpoint = len(subset) // 2
            left, right = subset[:midpoint], subset[midpoint:]
            if fails(left):
                subset = left
            elif fails(right):
                subset = right
            else:
                individually_failing = [name for name in subset if fails([name])]
                if individually_failing:
                    subset = [individually_failing[0]]
                else:
                    interaction_sets.append(subset)
                    subset = []
                break
        if not subset:
            break
        isolated.append(subset[0])
        remaining.remove(subset[0])

    report = {
        "schema_version": 1,
        "safe_policy": safe_result.get("wrapper_policy"),
        "failed_policy": failed_result.get("wrapper_policy"),
        "safe_image": str(safe_image),
        "failed_image": str(failed_image),
        "baseline_observation": baseline,
        "new_function_count": len(new_names),
        "isolated_failing_functions": isolated,
        "interaction_sets": interaction_sets,
        "remaining_function_count": len(remaining),
        "trials": trials,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    probe.unlink(missing_ok=True)
    print(json.dumps({
        "isolated": isolated,
        "interaction_sets": len(interaction_sets),
        "new_functions": len(new_names),
        "trials": len(trials),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
