#!/usr/bin/env python3
"""Build the frozen exact-profile SmartSpeaker BOLT frame-opt=hot arm."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
REFERENCE = Path(
    "/data/fastdyn/third_party/preflight/lemix/artifact/Lemix/Ported_Applications/"
    "dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/"
    "SmartSpeaker_Ported/build/SmartSpeaker"
)
PROFILE = RUN / "marker-training.fdata"
BOLT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bolt"
OUTPUT = RUN / "SmartSpeaker.marker_fop_hot"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if OUTPUT.exists():
        raise SystemExit(f"output exists: {OUTPUT}")
    command = [
        str(BOLT), str(REFERENCE), f"--data={PROFILE}", "--lite=false",
        "--icf=none", "--no-inline", "--indirect-call-promotion=none",
        "--tail-duplication=none", "--peepholes=none",
        "--reorder-blocks=ext-tsp", "--reorder-functions=cdsort",
        "--split-functions", "--split-all-cold", "--frame-opt=hot",
        "--enable-bat", "-o", str(OUTPUT),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    (RUN / "marker_fop_hot.stdout").write_text(result.stdout)
    (RUN / "marker_fop_hot.stderr").write_text(result.stderr)
    manifest = {
        "schema_version": 1,
        "command": command,
        "returncode": result.returncode,
        "reference_sha256": sha256(REFERENCE),
        "profile_sha256": sha256(PROFILE),
        "bolt_sha256": sha256(BOLT),
        "output": str(OUTPUT),
        "output_sha256": sha256(OUTPUT) if OUTPUT.is_file() else None,
    }
    (RUN / "marker_fop_hot-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if result.returncode == 0 and OUTPUT.is_file() else 1


if __name__ == "__main__":
    raise SystemExit(main())
