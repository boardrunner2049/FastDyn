#!/usr/bin/env python3
"""Convert relocatable BOLT policy decisions to FirmwareCandidate v1."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from fastdyn.preflight.bolt_policy import (
    load_bolt_deployment,
    publish_fixed_layout_firmware_candidate,
    publish_relocatable_firmware_candidate,
)
from fastdyn.preflight.candidate import load_firmware_candidate
from fastdyn.preflight.model import (
    TargetDescription,
    canonical_json_bytes,
    hash_file,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = (
    ROOT
    / "build/preflight/recovered-firmware-corpus-2026-11-16"
    / "bolt-firmware-wrapper/published-policy"
)
DEFAULT_BAT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bat-dump"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy-root", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--native-report", type=Path)
    parser.add_argument("--bat-dump", type=Path, default=DEFAULT_BAT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cpu", default="x86-64-v1")
    args = parser.parse_args()
    policy_root = args.policy_root.resolve()
    native_report = (
        args.native_report or policy_root.parent / "bolt-post-optimization-report.json"
    ).resolve()
    output = (
        args.output or policy_root.parent / "relocatable-firmware-candidates"
    ).resolve()
    if output.exists():
        raise SystemExit(f"output exists: {output}")
    policy_index_path = policy_root / "index.json"
    policy_index = json.loads(policy_index_path.read_text())
    decisions = [
        item for item in policy_index["deployments"]
        if item["layout_mode"] == "relocatable"
    ]
    target = TargetDescription("x86_64", "long", "little", args.cpu, ())

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    records = []
    try:
        for decision in decisions:
            workload = str(decision["workload_id"])
            deployment_path = policy_root / decision["relative_path"]
            deployment = load_bolt_deployment(deployment_path)
            destination = staging / workload
            if deployment.choice in {"reference", "compact_fop"}:
                candidate = publish_fixed_layout_firmware_candidate(
                    deployment_path, destination, target=target
                )
                origin_policy = "identity_address_preserving_fallback"
            else:
                candidate = publish_relocatable_firmware_candidate(
                    deployment_path,
                    destination,
                    target=target,
                    bat_dump=args.bat_dump,
                    native_report=native_report,
                )
                origin_policy = "bolt_bat_instruction_origins"
            records.append({
                "workload_id": workload,
                "choice": deployment.choice,
                "origin_policy": origin_policy,
                "candidate_id": candidate.candidate_id,
                "image_hash": candidate.image_hash,
                "relative_path": workload,
            })
        index = {
            "schema_version": 1,
            "status": "atomically_published",
            "policy_index_sha256": hash_file(policy_index_path),
            "native_report_sha256": hash_file(native_report),
            "bat_tool_sha256": hash_file(args.bat_dump),
            "target": target.to_dict(),
            "candidate_count": len(records),
            "candidates": records,
        }
        (staging / "index.json").write_bytes(canonical_json_bytes(index) + b"\n")
        for record in records:
            load_firmware_candidate(staging / record["relative_path"])
        os.replace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps({
        "output": str(output), "candidate_count": len(records),
        "index_sha256": hash_file(output / "index.json"),
        "bat_candidate_count": sum(
            item["origin_policy"] == "bolt_bat_instruction_origins"
            for item in records
        ),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
