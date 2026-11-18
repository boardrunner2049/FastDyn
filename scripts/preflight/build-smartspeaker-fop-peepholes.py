#!/usr/bin/env python3
"""Build and smoke the frozen SmartSpeaker FOP plus peepholes candidate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
SCHEDULE = ROOT / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17"
PLAN = RUN / "fop-peepholes-plan.json"
REFERENCE = (
    ROOT / "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/"
    "dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/"
    "SmartSpeaker_Ported/build/SmartSpeaker"
)
PROFILE = RUN / "marker-training.fdata"
BOLT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bolt"
BASELINE = RUN / "SmartSpeaker.marker_fop"
OUTPUT = RUN / "SmartSpeaker.marker_fop_peepholes"
PRELOAD = SCHEDULE / "lemix_mmio_marker_schedule.so"
BENCHMARK = ROOT / "scripts/preflight/benchmark-smartspeaker-fixed-ticks.py"
MANIFEST = RUN / "fop-peepholes-manifest.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    plan = json.loads(PLAN.read_text())
    if plan["status"] != "frozen_before_candidate_build":
        raise RuntimeError("candidate plan is not frozen")
    if sha256(PROFILE) != plan["profile_sha256"]:
        raise RuntimeError("profile differs from frozen plan")
    if sha256(BASELINE) != plan["baseline_candidate_sha256"]:
        raise RuntimeError("ordinary FOP baseline differs from frozen plan")
    paths = (
        OUTPUT, MANIFEST, RUN / "fop-peepholes.stdout",
        RUN / "fop-peepholes.stderr", RUN / "fop-peepholes-smoke.json",
        RUN / "fop-peepholes-smoke.log",
    )
    if any(path.exists() for path in paths):
        raise RuntimeError("candidate output already exists")
    command = [
        str(BOLT), str(REFERENCE), f"--data={PROFILE}", "--lite=false",
        "--icf=none", "--no-inline", "--indirect-call-promotion=none",
        "--tail-duplication=none", "--peepholes=all",
        "--reorder-blocks=ext-tsp", "--reorder-functions=cdsort",
        "--split-functions", "--split-all-cold", "--frame-opt=all",
        "--enable-bat", "-o", str(OUTPUT),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    (RUN / "fop-peepholes.stdout").write_text(result.stdout)
    (RUN / "fop-peepholes.stderr").write_text(result.stderr)
    if result.returncode != 0 or not OUTPUT.is_file():
        raise RuntimeError(f"BOLT candidate build failed: {result.returncode}")
    patches = re.search(r"Peephole: (\d+) double jumps patched", result.stdout)
    patched_double_jumps = int(patches.group(1)) if patches else 0
    distinct = sha256(OUTPUT) != sha256(BASELINE)

    smoke_output = RUN / "fop-peepholes-smoke.json"
    smoke_log = RUN / "fop-peepholes-smoke.log"
    smoke_command = [
        "taskset", "-c", "24,25", sys.executable, str(BENCHMARK),
        "--variant", f"stock_bolt={BASELINE}",
        "--variant", f"fop_peepholes={OUTPUT}",
        "--preload", str(PRELOAD), "--work-units", "1000",
        "--tick-address", "0x495670", "--samples", "1",
        "--seed", "20261711", "--output", str(smoke_output),
    ]
    environment = dict(os.environ)
    environment["FASTDYN_MARKER_SETTLE_NS"] = "100000"
    with smoke_log.open("w") as stream:
        smoke_result = subprocess.run(
            smoke_command, cwd=ROOT, env=environment, stdout=stream,
            stderr=subprocess.STDOUT, check=False,
        )
    smoke = json.loads(smoke_output.read_text()) if smoke_output.is_file() else None
    valid = bool(
        distinct and patched_double_jumps > 0 and smoke_result.returncode == 0
        and smoke is not None and smoke["validation"]["all_executions_ok"]
    )
    manifest = {
        "schema_version": 1,
        "status": "screen_passed" if valid else "screen_failed",
        "command": command,
        "reference_sha256": sha256(REFERENCE),
        "profile_sha256": sha256(PROFILE),
        "bolt_sha256": sha256(BOLT),
        "ordinary_fop_sha256": sha256(BASELINE),
        "candidate_sha256": sha256(OUTPUT),
        "candidate_differs_from_ordinary_fop": distinct,
        "patched_double_jumps": patched_double_jumps,
        "exact_work_smoke_valid": bool(
            smoke and smoke["validation"]["all_executions_ok"]
        ),
        "smoke_sha256": sha256(smoke_output) if smoke_output.is_file() else None,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
