#!/usr/bin/env python3
"""Rebuild historical SmartSpeaker L1 with BAT and prove code identity."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from elftools.elf.elffile import ELFFile


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
APP = (
    ROOT / "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/"
    "dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/"
    "SmartSpeaker_Ported"
)
REFERENCE = APP / "build/SmartSpeaker"
PROFILE = APP / "build/smartspeaker-training.fdata"
HISTORICAL = RUN / "binaries/SmartSpeaker.L1"
BOLT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bolt"
OUTPUT = ROOT / "build/preflight/lemix-smartspeaker-stock-bat-2026-11-17"


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def executable_sections(path: Path) -> list[dict[str, Any]]:
    result = []
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        entry = int(elf.header["e_entry"])
        for section in elf.iter_sections():
            if not int(section["sh_flags"]) & 0x4:
                continue
            data = section.data()
            result.append({
                "name": section.name,
                "address": int(section["sh_addr"]),
                "size": int(section["sh_size"]),
                "alignment": int(section["sh_addralign"]),
                "sha256": digest_bytes(data),
            })
    return [{"entry": entry}, *result]


def main() -> int:
    if OUTPUT.exists():
        raise SystemExit(f"output exists: {OUTPUT}")
    OUTPUT.mkdir(parents=True)
    image = OUTPUT / "SmartSpeaker.L1.bat"
    command = [
        str(BOLT), str(REFERENCE), f"--data={PROFILE}", "--lite=false",
        "--icf=none", "--no-inline", "--shorten-instructions=true",
        "--eliminate-unreachable=true", "--tail-duplication=none",
        "--peepholes=none", "--dyno-stats", "--time-opts",
        "--reorder-blocks=ext-tsp", "--reorder-functions=cdsort",
        "--split-functions", "--split-all-cold", "--frame-opt=none",
        "--enable-bat", "-o", str(image),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    (OUTPUT / "stdout.log").write_text(result.stdout)
    (OUTPUT / "stderr.log").write_text(result.stderr)
    if result.returncode != 0 or not image.is_file():
        raise SystemExit(f"BAT rebuild failed with status {result.returncode}")
    historical_sections = executable_sections(HISTORICAL)
    bat_sections = executable_sections(image)
    if historical_sections != bat_sections:
        raise SystemExit("BAT rebuild changed executable sections or entry point")
    manifest = {
        "schema_version": 1,
        "status": "bat_metadata_enrichment_code_identical",
        "command": command,
        "bolt_sha256": digest(BOLT),
        "reference_sha256": digest(REFERENCE),
        "profile_sha256": digest(PROFILE),
        "historical_stock": {"path": str(HISTORICAL), "sha256": digest(HISTORICAL)},
        "bat_source": {"path": str(image), "sha256": digest(image)},
        "entry_and_executable_sections": historical_sections,
        "executable_sections_identical": True,
        "deployment_image_remains_historical_stock": True,
    }
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
