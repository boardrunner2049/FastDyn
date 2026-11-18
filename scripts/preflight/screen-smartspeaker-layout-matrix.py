#!/usr/bin/env python3
"""Apply the frozen model-based screen to SmartSpeaker BOLT layouts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from elftools.elf.elffile import ELFFile


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "build/preflight/lemix-smartspeaker-layout-matrix-2026-11-17"
MANIFEST = MATRIX / "manifest.json"
CALLGRIND_LOG = MATRIX / "callgrind.stock.log"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def executable_inventory(path: Path) -> tuple[int, str]:
    records: list[tuple[str, int, bytes]] = []
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        for section in elf.iter_sections():
            if int(section["sh_flags"]) & 0x4:
                records.append(
                    (section.name, int(section["sh_addr"]), section.data())
                )
    payload = b"".join(
        name.encode() + b"\0" + address.to_bytes(8, "little") + data
        for name, address, data in records
    )
    return sum(len(data) for _name, _address, data in records), hashlib.sha256(
        payload
    ).hexdigest()


def metric(text: str, label: str) -> int:
    matches = re.findall(rf"^\s*(\d+)\s+:\s+{re.escape(label)}(?:\s|$)", text, re.M)
    if len(matches) < 2:
        raise SystemExit(f"missing before/after metric: {label}")
    return int(matches[-1])


def main() -> int:
    output = MATRIX / "diagnostic-screen.json"
    if output.exists():
        raise SystemExit(f"output exists: {output}")
    manifest = json.loads(MANIFEST.read_text())
    plan = {
        "version": 2,
        "frozen_before_candidate_statistics_loaded": True,
        "reason_for_v1_replacement": "Callgrind cannot preserve the mapped-MMIO environment",
        "ranking_tuple": [
            "profile_weighted_executed_instructions",
            "profile_weighted_total_branches",
            "allocated_executable_section_bytes",
            "candidate_id",
        ],
        "distinctness": "one candidate per executable-section identity",
        "training_candidates": 3,
        "training_protocol": {
            "paired_blocks": 10,
            "duration_seconds": 15,
            "seed": 20261127,
            "affinity_cpus": "24,25",
        },
        "held_out_retuning": False,
    }
    rows: list[dict[str, Any]] = []
    for candidate in manifest["candidates"]:
        name = candidate["id"]
        image = Path(candidate["image"])
        text = (MATRIX / f"{name}.stdout").read_text()
        executable_bytes, executable_hash = executable_inventory(image)
        rows.append({
            "id": name,
            "image": str(image),
            "sha256": sha256(image),
            "profile_weighted_executed_instructions": metric(
                text, "executed instructions"
            ),
            "profile_weighted_total_branches": metric(text, "total branches"),
            "allocated_executable_section_bytes": executable_bytes,
            "executable_section_identity": executable_hash,
        })
    rows.sort(key=lambda item: (
        item["profile_weighted_executed_instructions"],
        item["profile_weighted_total_branches"],
        item["allocated_executable_section_bytes"],
        item["id"],
    ))
    distinct = []
    seen: set[str] = set()
    for row in rows:
        if row["executable_section_identity"] in seen:
            row["screen_status"] = "duplicate_executable_image"
            continue
        seen.add(row["executable_section_identity"])
        distinct.append(row)
    selected = distinct[: plan["training_candidates"]]
    for row in rows:
        if "screen_status" not in row:
            row["screen_status"] = (
                "selected_for_native_training" if row in selected
                else "model_rank_below_training_budget"
            )
    callgrind = {
        "status": "unsupported_environment",
        "returncode": 139,
        "failure": "Valgrind did not retain the zero-backed MMIO mapping; RCC_LSICmd faulted",
        "log_sha256": sha256(CALLGRIND_LOG),
    }
    report = {
        "schema_version": 1,
        "status": "diagnostic_screen_complete",
        "manifest_sha256": sha256(MANIFEST),
        "plan": plan,
        "callgrind": callgrind,
        "selected_ids": [item["id"] for item in selected],
        "candidates": rows,
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
