#!/usr/bin/env python3
"""Publish the held-out SmartSpeaker stock-BOLT fallback and BAT candidate."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from fastdyn.preflight.bolt_policy import (
    load_bolt_deployment,
    publish_hot_fop_fallback_deployment,
    publish_relocatable_firmware_candidate,
)
from fastdyn.preflight.candidate import load_firmware_candidate
from fastdyn.preflight.model import TargetDescription, canonical_json_bytes, hash_file


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "build/preflight/lemix-smartspeaker-hot-fop-2026-11-17"
BASELINE = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
APP = (
    ROOT / "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/"
    "dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/"
    "SmartSpeaker_Ported"
)
REFERENCE = APP / "build/SmartSpeaker"
STOCK = BASELINE / "binaries/SmartSpeaker.L1"
NATIVE_REPORT = (
    ROOT / "build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16/"
    "bolt-post-optimization-report.json"
)
BAT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bat-dump"
BAT_SOURCE = (
    ROOT / "build/preflight/lemix-smartspeaker-stock-bat-2026-11-17/"
    "SmartSpeaker.L1.bat"
)
WORKLOAD = "lemix_smartspeaker_paced"


def write(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def main() -> int:
    selection_path = SOURCE / "selection.json"
    raw_report_path = SOURCE / "report.json"
    evidence_path = SOURCE / "deployment-evidence.json"
    policy_root = SOURCE / "published-policy"
    candidate_root = SOURCE / "relocatable-firmware-candidates"
    if candidate_root.exists():
        raise SystemExit(f"output exists: {candidate_root}")
    selection = json.loads(selection_path.read_text())
    raw = json.loads(raw_report_path.read_text())
    if raw["selection_sha256"] != hash_file(selection_path):
        raise SystemExit("hot-FOP report does not bind selection")
    trace = raw["interrupt_trace_prefix"]
    evidence = {
        "schema_version": 1,
        "status": "held_out_hot_fop_evaluation_complete",
        "workload_id": WORKLOAD,
        "selection": {
            "sha256": hash_file(selection_path),
            "frozen_before_held_out_measurement": True,
        },
        "decision": raw["decision"],
        "all_outputs_match": trace["equal_across_all_samples"],
        "observation_contract": {
            "interrupt_dispatches_per_run": 3000,
            "interrupt_trace_prefix_events": trace["events"],
            "interrupt_trace_prefix_sha256": trace["sha256"],
            "termination_and_work_checks": True,
        },
        "artifacts": {
            "reference": {
                "path": str(REFERENCE), "sha256": hash_file(REFERENCE),
            },
            "stock_bolt": {"path": str(STOCK), "sha256": hash_file(STOCK)},
        },
        "raw_report": {
            "path": str(raw_report_path), "sha256": hash_file(raw_report_path),
        },
        "claims": selection["claim_boundaries"],
    }
    expected_evidence = canonical_json_bytes(evidence) + b"\n"
    if evidence_path.exists():
        if evidence_path.read_bytes() != expected_evidence:
            raise SystemExit("existing deployment evidence differs")
    else:
        write(evidence_path, evidence)

    relative = Path(WORKLOAD) / "relocatable"
    if not policy_root.exists():
        policy_root.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=".published-policy.", dir=policy_root.parent)
        )
        try:
            deployment = publish_hot_fop_fallback_deployment(
                staging / relative,
                selection_path=selection_path,
                evidence_path=evidence_path,
                workload_id=WORKLOAD,
            )
            index = {
                "schema_version": 1,
                "status": "atomically_published",
                "selection_sha256": hash_file(selection_path),
                "evidence_sha256": hash_file(evidence_path),
                "deployment_count": 1,
                "deployments": [{
                    "workload_id": WORKLOAD,
                    "layout_mode": "relocatable",
                    "choice": deployment.choice,
                    "decision_id": deployment.decision_id,
                    "image_hash": deployment.image_hash,
                    "relative_path": str(relative),
                }],
            }
            write(staging / "index.json", index)
            load_bolt_deployment(staging / relative)
            os.replace(staging, policy_root)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    else:
        deployment = load_bolt_deployment(policy_root / relative)
        if deployment.choice != "stock_bolt":
            raise SystemExit("existing SmartSpeaker deployment is not stock BOLT")

    target = TargetDescription("x86_64", "long", "little", "x86-64-v1", ())
    candidate_staging = Path(
        tempfile.mkdtemp(prefix=".relocatable-firmware-candidates.", dir=SOURCE)
    )
    try:
        candidate = publish_relocatable_firmware_candidate(
            policy_root / WORKLOAD / "relocatable",
            candidate_staging / WORKLOAD,
            target=target,
            bat_dump=BAT,
            native_report=NATIVE_REPORT,
            bat_source_image=BAT_SOURCE,
        )
        candidate_index = {
            "schema_version": 1,
            "status": "atomically_published",
            "policy_index_sha256": hash_file(policy_root / "index.json"),
            "candidate_count": 1,
            "candidates": [{
                "workload_id": WORKLOAD,
                "choice": "stock_bolt",
                "origin_policy": "bolt_bat_instruction_origins",
                "candidate_id": candidate.candidate_id,
                "image_hash": candidate.image_hash,
                "relative_path": WORKLOAD,
            }],
        }
        write(candidate_staging / "index.json", candidate_index)
        load_firmware_candidate(candidate_staging / WORKLOAD)
        os.replace(candidate_staging, candidate_root)
    except BaseException:
        shutil.rmtree(candidate_staging, ignore_errors=True)
        raise
    result = {
        "policy_index_sha256": hash_file(policy_root / "index.json"),
        "candidate_index_sha256": hash_file(candidate_root / "index.json"),
        "candidate_id": candidate.candidate_id,
        "image_sha256": candidate.image_hash,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
