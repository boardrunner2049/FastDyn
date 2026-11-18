#!/usr/bin/env python3
"""Publish hash-bound executables selected by the recovered-corpus policy."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from fastdyn.preflight.bolt_policy import (
    load_bolt_deployment,
    publish_bolt_deployment,
)
from fastdyn.preflight.model import canonical_json_bytes, hash_file


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = ROOT / "build/preflight/recovered-firmware-corpus-2026-11-16"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--workload", action="append")
    parser.add_argument(
        "--layout-mode", action="append", choices=("relocatable", "fixed_layout")
    )
    args = parser.parse_args()

    root = args.root.resolve()
    wrapper = root / "bolt-firmware-wrapper"
    selection = (args.selection or wrapper / "policy-selection.json").resolve()
    evidence = (args.evidence or wrapper / "held-out-policy-report.json").resolve()
    selection_doc = json.loads(selection.read_text())
    workloads = tuple(args.workload or sorted(selection_doc["choices"]))
    layout_modes = tuple(args.layout_mode or ("relocatable", "fixed_layout"))
    output = (args.output or wrapper / "published-policy").resolve()
    if output.exists():
        raise SystemExit(f"output exists: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    records = []
    try:
        for workload in workloads:
            for layout_mode in layout_modes:
                destination = staging / workload / layout_mode
                artifact = publish_bolt_deployment(
                    destination,
                    selection_path=selection,
                    evidence_path=evidence,
                    workload_id=workload,
                    layout_mode=layout_mode,
                )
                records.append({
                    "workload_id": workload,
                    "layout_mode": layout_mode,
                    "choice": artifact.choice,
                    "decision_id": artifact.decision_id,
                    "image_hash": artifact.image_hash,
                    "relative_path": str(destination.relative_to(staging)),
                })
        index = {
            "schema_version": 1,
            "status": "atomically_published",
            "selection_sha256": hash_file(selection),
            "evidence_sha256": hash_file(evidence),
            "deployment_count": len(records),
            "deployments": records,
        }
        (staging / "index.json").write_bytes(canonical_json_bytes(index) + b"\n")
        for record in records:
            load_bolt_deployment(staging / record["relative_path"])
        os.replace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(json.dumps({
        "output": str(output),
        "deployment_count": len(records),
        "index_sha256": hash_file(output / "index.json"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
