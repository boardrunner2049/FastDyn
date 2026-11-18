#!/usr/bin/env python3
"""Extract one guest TB's optimized TCG and host-code inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(path: Path, address: int) -> dict[str, object]:
    text = path.read_text(errors="replace")
    marker = f"0x{address:08x}:"
    position = text.find(marker)
    if position < 0:
        raise RuntimeError(f"{marker} is absent from {path}")
    start = text.rfind("----------------", 0, position)
    end = text.find("----------------", position)
    section = text[start:end if end >= 0 else len(text)]
    size_match = re.search(r"OUT: \[size=(\d+)\]", section)
    if size_match is None:
        raise RuntimeError(f"host output size is absent for {marker} in {path}")
    optimized = section.split("OP after optimization and liveness analysis:", 1)
    op_text = optimized[1].split("OUT:", 1)[0] if len(optimized) == 2 else ""
    operations = [
        line.strip() for line in op_text.splitlines()
        if line.startswith(" ") and line.strip() and not line.strip().startswith("----")
    ]
    return {
        "path": str(path),
        "sha256": sha256(path),
        "guest_address": address,
        "host_output_bytes": int(size_match.group(1)),
        "optimized_tcg_operation_count": len(operations),
        "optimized_tcg_operations": operations,
        "section_sha256": hashlib.sha256(section.encode()).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x080066F8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference = extract(args.reference.resolve(), args.address)
    candidate = extract(args.candidate.resolve(), args.address)
    report = {
        "schema_version": 1,
        "classification": "diagnostic_generated_host_block_inventory",
        "reference": reference,
        "candidate": candidate,
        "delta": {
            "host_output_bytes": candidate["host_output_bytes"] - reference["host_output_bytes"],
            "optimized_tcg_operation_count": (
                candidate["optimized_tcg_operation_count"]
                - reference["optimized_tcg_operation_count"]
            ),
        },
        "claims": {
            "dynamic_execution_cost": False,
            "full_harness_profitability": False,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    print(json.dumps(report["delta"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
