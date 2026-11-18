#!/usr/bin/env python3
"""Recreate the rejected relocated-callback negative from the repaired image."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-frs-wrapper-2026-11-17"
WRAPPER = RUN / "wrapper-fop-inplace"
LEDGER = WRAPPER / "wrapper-result.json"
ACCEPTED = WRAPPER / "SmartSpeaker.bolt-qualified-compact"
RELOCATED = WRAPPER / "SmartSpeaker.bolt-fop"
PRELOAD = (
    ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17/"
    "lemix_mmio_marker_schedule.so"
)
NEGATIVE = RUN / "SmartSpeaker.absolute-callback-negative"
REPORT = RUN / "absolute-reference-counterexample.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if NEGATIVE.exists() or REPORT.exists():
        raise SystemExit("negative-control output already exists")
    ledger = json.loads(LEDGER.read_text())
    edit = next(item for item in ledger["qualified"]["edits"] if item["symbol"] == "vAudio_Task")
    repair = next(
        item for item in edit["absolute_reference_repairs"]
        if item["symbol"] == "vRecord_Task"
    )
    offset = int(edit["file_offset"]) + int(repair["output_offset"]) + int(repair["immediate_offset"])
    width = int(repair["immediate_size"])
    image = bytearray(ACCEPTED.read_bytes())
    repaired = int.from_bytes(image[offset : offset + width], "little")
    if repaired != int(repair["target_original_address"]):
        raise RuntimeError("accepted image does not contain the repaired callback")
    image[offset : offset + width] = int(repair["candidate_target_address"]).to_bytes(
        width, "little"
    )
    NEGATIVE.write_bytes(image)
    NEGATIVE.chmod(ACCEPTED.stat().st_mode)

    environment = dict(os.environ)
    environment.update({
        "LD_PRELOAD": str(PRELOAD),
        "FASTDYN_FIXED_WORK_UNITS": "50",
        "FASTDYN_TICK_COUNTER_ADDRESS": "0x495670",
        "FASTDYN_FIXED_TICK_DRIVER_CPU": "25",
        "FASTDYN_MARKER_SETTLE_NS": "100000",
    })
    result = subprocess.run(
        [str(NEGATIVE)], env=environment, capture_output=True, timeout=30,
        check=False,
    )
    symbols = subprocess.run(
        ["llvm-nm", "-n", str(RELOCATED)], capture_output=True, text=True,
        check=True,
    ).stdout.splitlines()
    target_hex = f"{int(repair['candidate_target_address']):016x}"
    target_symbol = next(
        (line.split(maxsplit=2)[2] for line in symbols if line.startswith(target_hex)),
        None,
    )
    report = {
        "schema_version": 1,
        "status": "counterexample_reproduced",
        "accepted_image": str(ACCEPTED),
        "accepted_sha256": sha256(ACCEPTED),
        "negative_image": str(NEGATIVE),
        "negative_sha256": sha256(NEGATIVE),
        "changed_file_offset": offset,
        "immediate_size": width,
        "repaired_original_target": repaired,
        "reintroduced_relocated_target": int(repair["candidate_target_address"]),
        "relocated_target_symbol": target_symbol,
        "source_function": "vAudio_Task",
        "reference_symbol": "vRecord_Task",
        "observation": {
            "returncode": result.returncode,
            "dispatch_markers": result.stdout.count(
                b"[JS:Debug] Interrupt_Handler_Task running"
            ),
            "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
        },
        "acceptance_rule": (
            "address-valued immediates must be rebound to the exact original symbol "
            "identity before an in-place BOLT body can be installed"
        ),
    }
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if result.returncode == -11 and target_symbol == "vRecord_Task" else 1


if __name__ == "__main__":
    raise SystemExit(main())
