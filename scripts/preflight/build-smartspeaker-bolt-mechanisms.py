#!/usr/bin/env python3
"""Build a frozen, bounded set of BOLT-native SmartSpeaker mechanisms."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
APP = (
    ROOT / "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/"
    "dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/"
    "SmartSpeaker_Ported"
)
REFERENCE = APP / "build/SmartSpeaker"
PROFILE = APP / "build/smartspeaker-training.fdata"
DEFAULT_BOLT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bolt"
DEFAULT_OUTPUT = ROOT / "build/preflight/lemix-smartspeaker-bolt-mechanisms-2026-11-17"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bolt", type=Path, default=DEFAULT_BOLT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    bolt = args.bolt.resolve()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"output exists: {output}")
    output.mkdir(parents=True)
    common = [
        str(bolt), str(REFERENCE), f"--data={PROFILE}", "--lite=false",
        "--icf=none", "--shorten-instructions=true",
        "--eliminate-unreachable=true", "--tail-duplication=none",
        "--peepholes=none", "--dyno-stats", "--time-opts",
        "--reorder-blocks=ext-tsp", "--reorder-functions=cdsort",
        "--split-functions", "--split-all-cold", "--frame-opt=all",
    ]
    configurations = {
        "icp": ["--no-inline", "--indirect-call-promotion=calls"],
        "inline_small": ["--inline-small-functions"],
        "icp_inline_small": [
            "--inline-small-functions", "--indirect-call-promotion=calls",
        ],
    }
    records = []
    for name, mechanism_args in configurations.items():
        image = output / f"SmartSpeaker.{name}"
        command = [*common, *mechanism_args, "-o", str(image)]
        started = time.monotonic()
        result = subprocess.run(command, capture_output=True, text=True)
        elapsed = time.monotonic() - started
        (output / f"{name}.stdout").write_text(result.stdout)
        (output / f"{name}.stderr").write_text(result.stderr)
        write(output / f"{name}.command.json", command)
        if result.returncode != 0 or not image.is_file():
            raise SystemExit(f"BOLT {name} failed with status {result.returncode}")
        records.append({
            "id": name,
            "mechanism_args": mechanism_args,
            "image": str(image),
            "sha256": sha256(image),
            "file_bytes": image.stat().st_size,
            "elapsed_seconds": elapsed,
            "stdout_sha256": sha256(output / f"{name}.stdout"),
            "stderr_sha256": sha256(output / f"{name}.stderr"),
        })
    manifest = {
        "schema_version": 1,
        "status": "bounded_candidate_set_built_before_training",
        "bolt": {"path": str(bolt), "sha256": sha256(bolt)},
        "reference": {"path": str(REFERENCE), "sha256": sha256(REFERENCE)},
        "profile": {"path": str(PROFILE), "sha256": sha256(PROFILE)},
        "baseline": {
            "id": "full_fop",
            "path": str(BASELINE / "binaries/SmartSpeaker.F1"),
            "sha256": sha256(BASELINE / "binaries/SmartSpeaker.F1"),
        },
        "candidate_policy": {
            "frozen_ids": list(configurations),
            "minimum_useful_training_ratio": 1.005,
            "selection": "largest lower confidence bound versus full_fop",
            "held_out_retuning": False,
        },
        "candidates": records,
        "claim_boundaries": {
            "supported_host_only": True,
            "firmware_acceptance_pending": True,
            "whole_application_capacity": False,
        },
    }
    write(output / "manifest.json", manifest)
    print(json.dumps(records, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
