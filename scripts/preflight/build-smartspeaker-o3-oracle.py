#!/usr/bin/env python3
"""Build and inventory the frozen SmartSpeaker O3 mechanism oracle."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-o3-oracle-2026-11-17"
PLAN = RUN / "plan.json"
SOURCE_ROOT = RUN / "source"
APPLICATION = SOURCE_ROOT / "SmartSpeaker_Ported"
OUTPUT = APPLICATION / "build/SmartSpeaker"
MANIFEST = RUN / "manifest.json"
COMMANDS = RUN / "commands.json"
FUNCTIONS = RUN / "function-inventory.json"
O0 = (
    ROOT / "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/"
    "dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/"
    "SmartSpeaker_Ported/build/SmartSpeaker"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_logged(name: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=APPLICATION,
        text=True,
        capture_output=True,
        check=False,
    )
    (RUN / f"{name}.stdout").write_text(result.stdout)
    (RUN / f"{name}.stderr").write_text(result.stderr)
    return result


def tool_output(command: list[str]) -> str:
    return subprocess.run(
        command, text=True, capture_output=True, check=True
    ).stdout.strip()


def symbol_inventory(binary: Path, names: list[str]) -> dict[str, object]:
    nm = tool_output(["nm", "-S", "--defined-only", str(binary)])
    records: dict[str, object] = {}
    for line in nm.splitlines():
        fields = line.split()
        if len(fields) != 4 or fields[3] not in names:
            continue
        address, size, symbol_type, name = fields
        records[name] = {
            "address": int(address, 16),
            "size": int(size, 16),
            "type": symbol_type,
        }
    return records


def main() -> int:
    if MANIFEST.exists() or COMMANDS.exists() or FUNCTIONS.exists():
        raise RuntimeError("O3 oracle output already exists")
    plan = json.loads(PLAN.read_text())
    if plan.get("status") != "frozen_before_build":
        raise RuntimeError("O3 oracle plan is not frozen")
    if sha256(O0) != plan["reference_o0_sha256"]:
        raise RuntimeError("O0 reference differs from the frozen plan")
    if not APPLICATION.joinpath("Makefile").is_file():
        raise RuntimeError("copied SmartSpeaker source tree is missing")

    cflags = " ".join([
        "-DSTM32F40_41xxx", "-DUSE_STDPERIPH_DRIVER", "-DUSE_FULL_ASSERT",
        "-DARM_MATH_CM4", "-Wno-implicit-function-declaration",
        "-Wno-incompatible-pointer-types", "-g", "-O3",
    ])
    ldflags = "-g -O3 -pthread -no-pie -Wl,--emit-relocs"
    clean = ["make", "clean"]
    build = [
        "make", "-j8", "CC=gcc", f"CFLAGS={cflags}",
        f"LDFLAGS={ldflags}",
    ]
    COMMANDS.write_text(json.dumps({
        "schema_version": 1,
        "cwd": str(APPLICATION),
        "clean": clean,
        "build": build,
        "environment_overrides": {},
    }, indent=2, sort_keys=True) + "\n")

    clean_result = run_logged("make-clean", clean)
    if clean_result.returncode != 0:
        raise RuntimeError("make clean failed")
    build_result = run_logged("make-o3", build)
    if build_result.returncode != 0 or not OUTPUT.is_file():
        raise RuntimeError(
            f"O3 build failed ({build_result.returncode}); see make-o3.stderr"
        )

    frozen_names = list(plan["analysis_functions"])
    inventory = {
        "schema_version": 1,
        "functions": {
            name: {
                "o0": symbol_inventory(O0, frozen_names).get(name),
                "o3": symbol_inventory(OUTPUT, frozen_names).get(name),
            }
            for name in frozen_names
        },
    }
    FUNCTIONS.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")

    manifest = {
        "schema_version": 1,
        "status": "built_not_yet_semantically_qualified",
        "purpose": plan["purpose"],
        "source_revision": plan["source_revision"],
        "reference_o0": {
            "path": str(O0), "sha256": sha256(O0),
            "file_bytes": O0.stat().st_size,
        },
        "o3_oracle": {
            "path": str(OUTPUT), "sha256": sha256(OUTPUT),
            "file_bytes": OUTPUT.stat().st_size,
        },
        "toolchain": {
            "gcc": tool_output(["gcc", "--version"]).splitlines()[0],
            "ld": tool_output(["ld", "--version"]).splitlines()[0],
            "make": tool_output(["make", "--version"]).splitlines()[0],
            "python": sys.version.splitlines()[0],
        },
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
        },
        "commands_sha256": sha256(COMMANDS),
        "build_logs": {
            name: sha256(RUN / name)
            for name in (
                "make-clean.stdout", "make-clean.stderr",
                "make-o3.stdout", "make-o3.stderr",
            )
        },
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
