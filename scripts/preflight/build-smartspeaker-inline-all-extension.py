#!/usr/bin/env python3
"""Build the bounded BOLT inline-all extension after the no-op mechanism screen."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "build/preflight/lemix-smartspeaker-bolt-mechanisms-2026-11-17"
APP = (
    ROOT / "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/"
    "dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/"
    "SmartSpeaker_Ported"
)
REFERENCE = APP / "build/SmartSpeaker"
PROFILE = APP / "build/smartspeaker-training.fdata"
BOLT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bolt"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    image = BASE / "SmartSpeaker.inline_all"
    manifest = BASE / "inline-all-extension.json"
    if image.exists() or manifest.exists():
        raise SystemExit("inline-all extension already exists")
    command = [
        str(BOLT), str(REFERENCE), f"--data={PROFILE}", "--lite=false",
        "--icf=none", "--shorten-instructions=true",
        "--eliminate-unreachable=true", "--tail-duplication=none",
        "--peepholes=none", "--dyno-stats", "--time-opts",
        "--reorder-blocks=ext-tsp", "--reorder-functions=cdsort",
        "--split-functions", "--split-all-cold", "--frame-opt=all",
        "--inline-all", "--inline-limit=1000", "--inline-ap",
        "-o", str(image),
    ]
    started = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True)
    elapsed = time.monotonic() - started
    stdout = BASE / "inline_all.stdout"
    stderr = BASE / "inline_all.stderr"
    stdout.write_text(result.stdout)
    stderr.write_text(result.stderr)
    if result.returncode != 0 or not image.is_file():
        raise SystemExit(f"BOLT inline-all failed with status {result.returncode}")
    document = {
        "schema_version": 1,
        "status": "bounded_extension_built",
        "reason": (
            "frozen ICP and small-function candidates performed zero target "
            "transformations; test BOLT's broader existing inliner"
        ),
        "command": command,
        "reference_sha256": digest(REFERENCE),
        "profile_sha256": digest(PROFILE),
        "bolt_sha256": digest(BOLT),
        "image": str(image),
        "image_sha256": digest(image),
        "file_bytes": image.stat().st_size,
        "stdout_sha256": digest(stdout),
        "stderr_sha256": digest(stderr),
        "elapsed_seconds": elapsed,
        "limits": {"inline_callsites": 1000, "patch_contract": "empty_phase_one"},
        "claim_boundaries": {
            "screening_only": True,
            "firmware_acceptance_pending": True,
            "performance_unmeasured": True,
        },
    }
    manifest.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
