#!/usr/bin/env python3
"""Build a frozen BOLT layout matrix for the unchanged SmartSpeaker ELF."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (
    ROOT / "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/"
    "dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/"
    "SmartSpeaker_Ported"
)
REFERENCE = APP / "build/SmartSpeaker"
PROFILE = APP / "build/smartspeaker-training.fdata"
BOLT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bolt"
OUTPUT = ROOT / "build/preflight/lemix-smartspeaker-layout-matrix-2026-11-17"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if OUTPUT.exists():
        raise SystemExit(f"output exists: {OUTPUT}")
    OUTPUT.mkdir(parents=True)
    common = [
        str(BOLT), str(REFERENCE), f"--data={PROFILE}", "--lite=false",
        "--icf=none", "--no-inline", "--shorten-instructions=true",
        "--eliminate-unreachable=true", "--tail-duplication=none",
        "--peepholes=none", "--dyno-stats", "--time-opts",
        "--frame-opt=all", "--enable-bat",
    ]
    configurations = {
        "ext_cdsort_split": [
            "--reorder-blocks=ext-tsp", "--reorder-functions=cdsort",
            "--split-functions", "--split-all-cold",
        ],
        "normal_cdsort_split": [
            "--reorder-blocks=normal", "--reorder-functions=cdsort",
            "--split-functions", "--split-all-cold",
        ],
        "branch_cdsort_split": [
            "--reorder-blocks=branch-predictor", "--reorder-functions=cdsort",
            "--split-functions", "--split-all-cold",
        ],
        "ext_hfsort_split": [
            "--reorder-blocks=ext-tsp", "--reorder-functions=hfsort",
            "--split-functions", "--split-all-cold",
        ],
        "ext_pettis_split": [
            "--reorder-blocks=ext-tsp", "--reorder-functions=pettis-hansen",
            "--split-functions", "--split-all-cold",
        ],
        "ext_exec_split": [
            "--reorder-blocks=ext-tsp", "--reorder-functions=exec-count",
            "--split-functions", "--split-all-cold",
        ],
        "ext_cdsort_nosplit": [
            "--reorder-blocks=ext-tsp", "--reorder-functions=cdsort",
        ],
        "ext_cdsort_cdsplit": [
            "--reorder-blocks=ext-tsp", "--reorder-functions=cdsort",
            "--split-functions", "--split-strategy=cdsplit",
        ],
    }
    records = []
    for name, options in configurations.items():
        image = OUTPUT / f"SmartSpeaker.{name}"
        command = [*common, *options, "-o", str(image)]
        started = time.monotonic()
        result = subprocess.run(command, capture_output=True, text=True)
        elapsed = time.monotonic() - started
        stdout = OUTPUT / f"{name}.stdout"
        stderr = OUTPUT / f"{name}.stderr"
        stdout.write_text(result.stdout)
        stderr.write_text(result.stderr)
        (OUTPUT / f"{name}.command.json").write_text(
            json.dumps(command, indent=2) + "\n"
        )
        if result.returncode != 0 or not image.is_file():
            raise SystemExit(f"layout build {name} failed: {result.returncode}")
        records.append({
            "id": name,
            "options": options,
            "image": str(image),
            "sha256": sha256(image),
            "file_bytes": image.stat().st_size,
            "elapsed_seconds": elapsed,
            "stdout_sha256": sha256(stdout),
            "stderr_sha256": sha256(stderr),
        })
    manifest = {
        "schema_version": 1,
        "status": "frozen_before_diagnostic_selection",
        "reference": {"path": str(REFERENCE), "sha256": sha256(REFERENCE)},
        "profile": {"path": str(PROFILE), "sha256": sha256(PROFILE)},
        "bolt": {"path": str(BOLT), "sha256": sha256(BOLT)},
        "candidate_ids": list(configurations),
        "selection_rule": {
            "training": "lowest completed-work Callgrind instruction count",
            "tie_break": "smaller allocated executable bytes then lexical id",
            "native_held_out_retuning": False,
            "minimum_useful_native_ratio": 1.005,
        },
        "candidates": records,
        "claim_boundaries": {
            "diagnostic_not_performance": True,
            "supported_host_only": True,
            "firmware_acceptance_pending": True,
        },
    }
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({"candidate_count": len(records), "output": str(OUTPUT)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
