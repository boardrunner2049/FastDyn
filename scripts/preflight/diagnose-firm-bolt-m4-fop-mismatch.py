#!/usr/bin/env python3
"""Bisect the real FreeRTOS modeled-state mismatch over FOP functions."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BOLT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bolt"
DEFAULT_FASTDYN = ROOT / "fastdyn-env/bin/fastdyn"
M4_IMAGE = ROOT / "tests/sample_binaries/RTOS/RTOSDemo.axf"


def load_port_runner():
    path = ROOT / "scripts/preflight/run-firm-bolt-cortex-m-port.py"
    spec = importlib.util.spec_from_file_location("firm_bolt_port", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bolt", type=Path, default=DEFAULT_BOLT)
    parser.add_argument("--fastdyn", type=Path, default=DEFAULT_FASTDYN)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = json.loads(args.source_report.read_text())
    functions = sorted(
        function["name"]
        for function in report["profiles"]["m4"]["native_pass_report"][
            "functions"
        ]
        if "frame_load_forwarding"
        in function.get("firmware_modification_reasons", [])
    )
    runner = load_port_runner()
    steps: list[dict[str, object]] = []

    def evaluate(names: list[str], tag: str) -> dict[str, object]:
        candidate = output / f"RTOSDemo.{tag}.axf"
        command = [
            str(args.bolt.resolve()),
            str(M4_IMAGE),
            "--firmbolt-cortex-m-cpu=cortex-m4",
            "--firmbolt-arm-full-pipeline",
            "--eliminate-unreachable=false",
            "--frame-opt=all",
            f"--funcs-no-regex={','.join(names)}",
            "--keep-tmp",
            "-o",
            str(candidate),
        ]
        process = subprocess.run(command, capture_output=True, text=True)
        if process.returncode != 0:
            raise RuntimeError(
                f"Firm-BOLT failed for {tag}:\n{process.stdout}\n{process.stderr}"
            )
        execution_output = output / f"execution-{tag}"
        execution_output.mkdir(parents=True, exist_ok=True)
        result = runner.validate_m4_execution(
            candidate, execution_output, args.fastdyn.resolve()
        )
        step: dict[str, object] = {
            "tag": tag,
            "function_count": len(names),
            "functions": names,
            "candidate": str(candidate),
            "candidate_sha256": runner.sha256(candidate),
            "execution": result,
        }
        steps.append(step)
        (output / "diagnosis.json").write_text(json.dumps({
            "schema_version": 1,
            "source_report": str(args.source_report.resolve()),
            "initial_function_count": len(functions),
            "steps": steps,
            "status": "running",
        }, indent=2, sort_keys=True) + "\n")
        return result

    cohort = functions
    level = 0
    conclusion: dict[str, object]
    while len(cohort) > 1:
        midpoint = len(cohort) // 2
        left, right = cohort[:midpoint], cohort[midpoint:]
        left_result = evaluate(left, f"level-{level}-left")
        if left_result["status"] == "rejected":
            cohort = left
            level += 1
            continue
        right_result = evaluate(right, f"level-{level}-right")
        if right_result["status"] == "rejected":
            cohort = right
            level += 1
            continue
        conclusion = {
            "classification": "interaction_or_non_monotonic",
            "cohort": cohort,
            "left_status": left_result["status"],
            "right_status": right_result["status"],
        }
        break
    else:
        final_result = evaluate(cohort, f"level-{level}-single")
        conclusion = {
            "classification": (
                "single_function_mismatch"
                if final_result["status"] == "rejected"
                else "full_cohort_only_or_non_monotonic"
            ),
            "cohort": cohort,
            "final_status": final_result["status"],
        }

    final = {
        "schema_version": 1,
        "source_report": str(args.source_report.resolve()),
        "initial_function_count": len(functions),
        "steps": steps,
        "status": "complete",
        "conclusion": conclusion,
    }
    (output / "diagnosis.json").write_text(
        json.dumps(final, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({
        "status": "complete",
        "report": str(output / "diagnosis.json"),
        "conclusion": conclusion,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
