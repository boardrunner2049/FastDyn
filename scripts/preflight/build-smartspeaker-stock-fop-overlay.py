#!/usr/bin/env python3
"""Overlay checked hot FOP bodies onto the exact SmartSpeaker stock-BOLT image.

The global layout and every unselected body come from the historical L1 image.
Selected bodies come from the code-identical BAT-enabled F1 rebuild, fit their
existing L1 symbol capacities, contain no PC-relative/control-flow references,
and already passed the wrapper's explicit memory-effect qualification.  The
profile prioritizes bodies; it is not used as a correctness argument.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import capstone


ROOT = Path(__file__).resolve().parents[2]
RERUN = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
BASELINE = RERUN / "binaries/SmartSpeaker.L1"
HISTORICAL_F1 = RERUN / "binaries/SmartSpeaker.F1"
ORIGINAL = (
    ROOT / "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/"
    "dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/"
    "SmartSpeaker_Ported/build/SmartSpeaker"
)
CANDIDATE = (
    ROOT / "build/preflight/lemix-smartspeaker-layout-matrix-2026-11-17/"
    "SmartSpeaker.ext_cdsort_split"
)
WRAPPER_RUN = (
    ROOT / "build/preflight/"
    "bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16"
)
STOCK_BAT_RUN = ROOT / "build/preflight/lemix-smartspeaker-stock-bat-2026-11-17"
OUTPUT = ROOT / "build/preflight/lemix-smartspeaker-stock-fop-overlay-2026-11-17"
BAT_DUMP = ROOT / "third_party/preflight/llvm-build/bin/llvm-bat-dump"


def load_wrapper() -> Any:
    path = ROOT / "scripts/preflight/run-bolt-firmware-wrapper.py"
    spec = importlib.util.spec_from_file_location("fastdyn_bolt_wrapper", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load wrapper helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode() + b"\n"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main() -> int:
    if OUTPUT.exists():
        raise SystemExit(f"output exists: {OUTPUT}")
    OUTPUT.mkdir(parents=True)
    wrapper = load_wrapper()
    result_path = WRAPPER_RUN / "wrapper-result.json"
    native_path = WRAPPER_RUN / "bolt-post-optimization-report.json"
    result = json.loads(result_path.read_text())
    native = json.loads(native_path.read_text())
    qualified = {
        item["symbol"]: item
        for item in result["memory_effect_qualified_adoption"]["edits"]
    }
    execution_counts = {
        wrapper.restored_bolt_name(str(item["name"])):
            int(item.get("known_execution_count", 0))
        for item in native["functions"]
    }
    native_by_name = {
        wrapper.restored_bolt_name(str(item["name"])): item
        for item in native["functions"]
    }

    baseline = wrapper.function_records(BASELINE)
    historical_f1 = wrapper.function_records(HISTORICAL_F1)
    candidate = wrapper.function_records(CANDIDATE)
    original = wrapper.function_records(ORIGINAL)
    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    decoder.detail = True
    bat = wrapper.BatIdentityResolver(BAT_DUMP, CANDIDATE)

    rejection_counts: dict[str, int] = {}
    eligible: list[dict[str, Any]] = []

    def reject(reason: str) -> None:
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    for name, effect in sorted(qualified.items()):
        if name not in baseline or name not in historical_f1 or name not in candidate:
            reject("missing_or_ambiguous_emitted_symbol")
            continue
        if name not in original or name not in native_by_name:
            reject("missing_original_identity")
            continue
        baseline_body = bytes(baseline[name]["bytes"])
        f1_body = bytes(historical_f1[name]["bytes"])
        candidate_body = bytes(candidate[name]["bytes"])
        if candidate_body != f1_body:
            reject("bat_rebuild_body_differs_from_historical_f1")
            continue
        if candidate_body == baseline_body:
            reject("unchanged_by_fop")
            continue
        if len(candidate_body) > len(baseline_body):
            reject("does_not_fit_stock_bolt_symbol_capacity")
            continue
        instructions = list(
            decoder.disasm(candidate_body, int(candidate[name]["address"]))
        )
        if sum(item.size for item in instructions) != len(candidate_body):
            reject("candidate_body_does_not_decode_exactly")
            continue
        if any(
            item.group(capstone.CS_GRP_JUMP)
            or item.group(capstone.CS_GRP_CALL)
            or item.mnemonic.startswith("loop")
            for item in instructions
        ):
            reject("control_reference_requires_repair")
            continue
        if any("rip" in item.op_str for item in instructions):
            reject("pc_relative_reference_requires_repair")
            continue
        protected = baseline_body[:4] == bytes.fromhex("f30f1efa")
        if protected and candidate_body[:4] != baseline_body[:4]:
            reject("protected_entry_changed")
            continue
        if native_by_name[name]["multi_entry"] or native_by_name[name]["split"]:
            reject("multi_entry_or_split_region")
            continue
        eligible.append({
            "symbol": name,
            "known_execution_count": execution_counts.get(name, 0),
            "baseline": baseline[name],
            "candidate": candidate[name],
            "original": original[name],
            "candidate_body": candidate_body,
            "effect": effect,
            "protected_entry": protected,
        })

    # Profitability policy: only overlay structurally qualified bodies observed
    # in the frozen training profile.  Unobserved paths retain stock BOLT bytes.
    selected = [item for item in eligible if item["known_execution_count"] > 0]
    if not selected:
        raise SystemExit("no hot checked body qualified")

    image = bytearray(BASELINE.read_bytes())
    edits: list[dict[str, Any]] = []
    origin_overlays: list[dict[str, Any]] = []
    for item in selected:
        name = item["symbol"]
        baseline_record = item["baseline"]
        candidate_record = item["candidate"]
        original_record = item["original"]
        before = bytes(baseline_record["bytes"])
        body = bytes(item["candidate_body"])
        padding = b"\x90" * (len(before) - len(body))
        start = int(baseline_record["file_offset"])
        image[start : start + len(before)] = body + padding

        translations = bat.instructions(
            name, original_record, candidate_record
        )
        mappings = []
        for instruction, original_address, bat_name in translations:
            output_offset = instruction.address - int(candidate_record["address"])
            emitted_address = int(baseline_record["address"]) + output_offset
            mappings.append({
                "emitted_address": emitted_address,
                "emitted_offset": output_offset,
                "emitted_bytes": instruction.bytes.hex(),
                "text": f"{instruction.mnemonic} {instruction.op_str}".strip(),
                "original_address": original_address,
                "original_identity": f"{bat_name}+0x{original_address - int(original_record['address']):x}",
                "potential_memory_effect": any(
                    operand.type == capstone.x86.X86_OP_MEM
                    for operand in instruction.operands
                ),
            })
        if sum(len(bytes.fromhex(row["emitted_bytes"])) for row in mappings) != len(body):
            raise RuntimeError(f"incomplete emitted instruction map: {name}")
        origin_overlays.extend(
            {"symbol": name, **mapping} for mapping in mappings
        )
        effect = item["effect"]
        edits.append({
            "symbol": name,
            "baseline_address": int(baseline_record["address"]),
            "original_address": int(original_record["address"]),
            "file_offset": start,
            "capacity_bytes": len(before),
            "live_candidate_bytes": len(body),
            "padding_bytes": len(padding),
            "known_execution_count": item["known_execution_count"],
            "baseline_body_sha256": sha256_bytes(before),
            "candidate_body_sha256": sha256_bytes(body),
            "protected_entry_preserved": item["protected_entry"],
            "effect_classification": effect["classification"],
            "forwarding_dependencies": effect["forwarding_dependencies"],
            "origin_mapping_count": len(mappings),
        })

    output_image = OUTPUT / "SmartSpeaker.L1.checked-hot-fop-overlay"
    temporary = output_image.with_name(f".{output_image.name}.tmp")
    temporary.write_bytes(image)
    temporary.chmod(BASELINE.stat().st_mode)
    os.replace(temporary, output_image)
    origins_path = OUTPUT / "origin-overlays.json"
    origins_path.write_bytes(canonical({
        "schema_version": 1,
        "unchanged_code_origin_source": str(
            STOCK_BAT_RUN / "SmartSpeaker.L1.bat"
        ),
        "changed_instruction_origins": origin_overlays,
    }))

    document = {
        "schema_version": 1,
        "status": "frozen_before_native_timing",
        "classification": "stock_bolt_plus_checked_hot_fop_body_overlay",
        "inputs": {
            "original": {"path": str(ORIGINAL), "sha256": sha256(ORIGINAL)},
            "stock_bolt": {"path": str(BASELINE), "sha256": sha256(BASELINE)},
            "historical_f1": {
                "path": str(HISTORICAL_F1), "sha256": sha256(HISTORICAL_F1),
            },
            "bat_f1_candidate": {
                "path": str(CANDIDATE), "sha256": sha256(CANDIDATE),
            },
            "wrapper_result_sha256": sha256(result_path),
            "native_report_sha256": sha256(native_path),
        },
        "selection_policy": {
            "rule": (
                "overlay every structurally and effect-qualified, in-place, "
                "position-independent FOP body with nonzero frozen profile count"
            ),
            "profile_is_correctness_evidence": False,
            "unselected_and_unknown_paths": "retain exact stock-BOLT bytes",
        },
        "cohort": {
            "effect_qualified_input_count": len(qualified),
            "structurally_eligible_count": len(eligible),
            "selected_hot_count": len(selected),
            "selected_profile_entry_count": sum(
                item["known_execution_count"] for item in selected
            ),
            "rejection_counts": rejection_counts,
        },
        "edits": edits,
        "output": {
            "path": str(output_image),
            "sha256": sha256(output_image),
            "file_bytes": output_image.stat().st_size,
            "same_file_size_as_stock_bolt": (
                output_image.stat().st_size == BASELINE.stat().st_size
            ),
            "origin_overlays": str(origins_path),
            "origin_overlays_sha256": sha256(origins_path),
        },
        "measurement_plan": {
            "training": {
                "paired_blocks": 10,
                "duration_seconds": 30.0,
                "seed": 20261129,
                "affinity_cpus": "24,25",
            },
            "advance_rule": "lower 95% paired-bootstrap bound above 1.0",
            "held_out_minimum_useful_ratio": 1.005,
            "held_out_retuning": False,
        },
        "claim_boundaries": {
            "patch_contract": "empty_phase_one_contract",
            "finite_behavior_check_not_proof": True,
            "paced_cpu_cost_only": True,
            "whole_application_capacity": False,
            "original_mcu_or_fastdyn_speedup": False,
        },
    }
    manifest = OUTPUT / "selection.json"
    manifest.write_bytes(canonical(document))
    print(json.dumps({
        "selected": [item["symbol"] for item in edits],
        "profile_entry_count": document["cohort"]["selected_profile_entry_count"],
        "output": str(output_image),
        "output_sha256": document["output"]["sha256"],
        "selection": str(manifest),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
