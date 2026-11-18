#!/usr/bin/env python3
"""Freeze and apply the diagnostic screen for the SmartSpeaker layout matrix.

Callgrind cannot run this fixture because Valgrind drops the zero-backed MMIO
mapping.  This script records that limitation and applies the replacement rule
without using the one-sample native smoke timings: rank distinct executable
images by BOLT's profile-weighted final dynostats, then executable bytes, then
candidate id.  Only the frozen top three advance to native training.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from elftools.elf.elffile import ELFFile


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "build/preflight/lemix-smartspeaker-layout-matrix-2026-11-17"
EXECUTED_RE = re.compile(r"^\s*(\d+)\s+: executed instructions", re.MULTILINE)
BRANCHES_RE = re.compile(r"^\s*(\d+)\s+: total branches", re.MULTILINE)


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode() + b"\n"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def executable_inventory(path: Path) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        for section in elf.iter_sections():
            flags = int(section["sh_flags"])
            # ELF SHF_ALLOC=2 and SHF_EXECINSTR=4.
            if flags & 0x2 and flags & 0x4:
                data = section.data()
                item = {
                    "name": section.name,
                    "address": int(section["sh_addr"]),
                    "bytes": int(section["sh_size"]),
                    "alignment": int(section["sh_addralign"]),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                sections.append(item)
                digest.update(canonical(item))
    return {
        "allocated_executable_bytes": sum(item["bytes"] for item in sections),
        "executable_identity_sha256": digest.hexdigest(),
        "sections": sections,
    }


def last_metric(pattern: re.Pattern[str], text: str, label: str) -> int:
    matches = pattern.findall(text)
    if not matches:
        raise SystemExit(f"missing {label} dynostat")
    return int(matches[-1])


def main() -> int:
    manifest_path = OUTPUT / "manifest.json"
    result_path = OUTPUT / "diagnostic-screen.json"
    if result_path.exists():
        raise SystemExit(f"diagnostic screen already exists: {result_path}")
    manifest = json.loads(manifest_path.read_text())
    records = []
    for candidate in manifest["candidates"]:
        identifier = candidate["id"]
        image = Path(candidate["image"])
        stdout = OUTPUT / f"{identifier}.stdout"
        text = stdout.read_text()
        inventory = executable_inventory(image)
        records.append({
            "id": identifier,
            "image": str(image),
            "image_sha256": sha256(image),
            "profile_weighted_executed_instructions": last_metric(
                EXECUTED_RE, text, "executed instructions"
            ),
            "profile_weighted_total_branches": last_metric(
                BRANCHES_RE, text, "total branches"
            ),
            **inventory,
        })

    ranked = sorted(
        records,
        key=lambda item: (
            item["profile_weighted_executed_instructions"],
            item["profile_weighted_total_branches"],
            item["allocated_executable_bytes"],
            item["id"],
        ),
    )
    distinct: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates: list[dict[str, str]] = []
    for item in ranked:
        identity = item["executable_identity_sha256"]
        if identity in seen:
            representative = next(
                row["id"] for row in distinct
                if row["executable_identity_sha256"] == identity
            )
            duplicates.append({"id": item["id"], "same_executable_as": representative})
            continue
        seen.add(identity)
        distinct.append(item)

    callgrind_log = OUTPUT / "callgrind.stock.log"
    selection = [item["id"] for item in distinct[:3]]
    document = {
        "schema_version": 1,
        "status": "frozen_before_native_training",
        "matrix_manifest_sha256": sha256(manifest_path),
        "diagnostic_revision": 2,
        "callgrind": {
            "status": "unsupported_environment",
            "reason": (
                "Valgrind did not retain the fixture's zero-backed MMIO mapping "
                "and the stock executable faulted in RCC_LSICmd; this is not a "
                "candidate semantic failure"
            ),
            "log": str(callgrind_log),
            "log_sha256": sha256(callgrind_log),
        },
        "ranking_rule": [
            "lower BOLT profile-weighted executed instructions",
            "lower BOLT profile-weighted total branches",
            "lower allocated executable bytes",
            "lexical candidate id",
        ],
        "ranking_scope": (
            "distinct executable-section identities; smoke CPU values excluded"
        ),
        "ranked_candidates": ranked,
        "duplicate_executable_images": duplicates,
        "native_training": {
            "selected_ids": selection,
            "paired_blocks": 10,
            "duration_seconds": 15.0,
            "seed": 20261127,
            "affinity_cpus": "24,25",
            "baseline": "stock_bolt",
            "advance_rule": "largest lower 95% paired-bootstrap bound above 1.0",
            "held_out_minimum_useful_ratio": 1.005,
            "held_out_retuning": False,
        },
        "claim_boundaries": {
            "diagnostic_metrics_are_not_performance": True,
            "paced_cpu_cost_only": True,
            "whole_application_capacity": False,
            "original_mcu_or_fastdyn_speedup": False,
        },
    }
    result_path.write_bytes(canonical(document))
    print(json.dumps({
        "selected_ids": selection,
        "distinct_executable_images": len(distinct),
        "duplicates": duplicates,
        "output": str(result_path),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
