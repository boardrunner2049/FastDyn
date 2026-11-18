#!/usr/bin/env python3
"""Build an address-stable SmartSpeaker overlay for checked frame rewrites.

The full Firm-BOLT image is useful integration evidence, but its smaller
functions move later BOLT output and therefore do not isolate the execution
effect of the new frame rules.  This builder starts from the exact stock-BOLT
image used by the fixed-work marker experiment and replaces only profile-
observed functions carrying one of the two checked ABI frame classifications.

The selected functions are branchless, single-entry leaf regions.  Any
RIP-relative data displacement is repaired so that the instruction continues
to name the exact same object after being moved back to the stock address.
All unused capacity is filled with NOPs; every byte outside the selected symbol
ranges must remain byte-identical to stock BOLT.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import capstone


ROOT = Path(__file__).resolve().parents[2]
MARKER_RUN = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
WRAPPER_RUN = ROOT / "build/preflight/bolt-firmware-wrapper-private-spill-abi-2026-11-17"
BASELINE = MARKER_RUN / "SmartSpeaker.marker_stock"
OUTPUT = ROOT / "build/preflight/lemix-smartspeaker-stock-frame-overlay-2026-11-17"

ALLOWED_CLASSIFICATIONS = {
    "checked_trivial_frame_elimination",
    "checked_private_return_spill_elimination",
}


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


def signed_bounds(byte_count: int) -> tuple[int, int]:
    bits = byte_count * 8
    return -(1 << (bits - 1)), (1 << (bits - 1)) - 1


def repaired_body(
    decoder: capstone.Cs,
    body: bytes,
    source_address: int,
    destination_address: int,
) -> tuple[bytes, list[dict[str, Any]]]:
    """Relocate only exact RIP-relative memory displacements."""

    instructions = list(decoder.disasm(body, source_address))
    if sum(item.size for item in instructions) != len(body):
        raise RuntimeError("candidate body does not decode exactly")
    if any(
        item.group(capstone.CS_GRP_JUMP)
        or item.group(capstone.CS_GRP_CALL)
        or item.mnemonic.startswith("loop")
        for item in instructions
    ):
        raise RuntimeError("selected frame body unexpectedly contains control flow")

    result = bytearray(body)
    repairs: list[dict[str, Any]] = []
    for instruction in instructions:
        rip_operands = [
            operand
            for operand in instruction.operands
            if operand.type == capstone.x86.X86_OP_MEM
            and operand.mem.base == capstone.x86.X86_REG_RIP
        ]
        if not rip_operands:
            continue
        if len(rip_operands) != 1:
            raise RuntimeError(
                f"ambiguous RIP-relative operands at 0x{instruction.address:x}"
            )
        displacement_offset = int(instruction.disp_offset)
        displacement_size = int(instruction.disp_size)
        if displacement_size <= 0:
            raise RuntimeError(
                f"missing displacement encoding at 0x{instruction.address:x}"
            )
        target = int(instruction.address + instruction.size + rip_operands[0].mem.disp)
        instruction_offset = int(instruction.address - source_address)
        emitted_address = destination_address + instruction_offset
        new_displacement = target - (emitted_address + instruction.size)
        minimum, maximum = signed_bounds(displacement_size)
        if not minimum <= new_displacement <= maximum:
            raise RuntimeError(
                f"RIP-relative target 0x{target:x} is out of range at "
                f"0x{emitted_address:x}"
            )
        patch_start = instruction_offset + displacement_offset
        patch_end = patch_start + displacement_size
        result[patch_start:patch_end] = int(new_displacement).to_bytes(
            displacement_size, "little", signed=True
        )
        repairs.append({
            "source_instruction_address": int(instruction.address),
            "emitted_instruction_address": emitted_address,
            "instruction_offset": instruction_offset,
            "displacement_offset": displacement_offset,
            "displacement_size": displacement_size,
            "source_displacement": int(rip_operands[0].mem.disp),
            "emitted_displacement": new_displacement,
            "target_address": target,
        })

    # Decode at the destination and prove that every repaired reference still
    # reaches the source instruction's exact target.
    emitted = list(decoder.disasm(bytes(result), destination_address))
    if sum(item.size for item in emitted) != len(result):
        raise RuntimeError("repaired body does not decode exactly")
    emitted_by_offset = {
        int(item.address - destination_address): item for item in emitted
    }
    for repair in repairs:
        instruction = emitted_by_offset[repair["instruction_offset"]]
        targets = [
            int(instruction.address + instruction.size + operand.mem.disp)
            for operand in instruction.operands
            if operand.type == capstone.x86.X86_OP_MEM
            and operand.mem.base == capstone.x86.X86_REG_RIP
        ]
        if targets != [repair["target_address"]]:
            raise RuntimeError(
                f"RIP repair changed target at 0x{instruction.address:x}: {targets}"
            )
    return bytes(result), repairs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wrapper-run", type=Path, default=WRAPPER_RUN)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    wrapper_run = args.wrapper_run.resolve()
    output = args.output.resolve()
    candidate_path = wrapper_run / "SmartSpeaker.bolt-firmware-candidate"
    if output.exists():
        raise SystemExit(f"output exists: {output}")
    for path in (BASELINE, candidate_path):
        if not path.is_file():
            raise SystemExit(f"missing input: {path}")

    wrapper = load_wrapper()
    wrapper_result_path = wrapper_run / "wrapper-result.json"
    native_report_path = wrapper_run / "bolt-post-optimization-report.json"
    wrapper_result = json.loads(wrapper_result_path.read_text())
    native_report = json.loads(native_report_path.read_text())
    baseline_records = wrapper.function_records(BASELINE)
    candidate_records = wrapper.function_records(candidate_path)
    native_by_name = {
        wrapper.restored_bolt_name(str(item["name"])): item
        for item in native_report["functions"]
    }
    effect_edits = {
        str(item["symbol"]): item
        for item in wrapper_result["memory_effect_qualified_adoption"]["edits"]
        if item.get("classification") in ALLOWED_CLASSIFICATIONS
    }

    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    decoder.detail = True
    eligible: list[dict[str, Any]] = []
    rejection_counts: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    for name, effect in sorted(effect_edits.items()):
        native = native_by_name.get(name)
        baseline = baseline_records.get(name)
        candidate = candidate_records.get(name)
        if native is None or baseline is None or candidate is None:
            reject("missing_or_ambiguous_symbol_or_identity")
            continue
        if int(native.get("known_execution_count", 0)) <= 0:
            reject("unobserved_in_frozen_training_profile")
            continue
        if not native.get("simple") or native.get("multi_entry") or native.get("split"):
            reject("not_single_entry_unsplit_simple_region")
            continue
        before = bytes(baseline["bytes"])
        source = bytes(candidate["bytes"])
        if len(source) > len(before):
            reject("does_not_fit_exact_stock_symbol_capacity")
            continue
        if source == before:
            reject("no_byte_change_relative_to_stock_bolt")
            continue
        protected = before.startswith(bytes.fromhex("f30f1efa"))
        if protected and not source.startswith(bytes.fromhex("f30f1efa")):
            reject("protected_entry_changed")
            continue
        try:
            body, repairs = repaired_body(
                decoder,
                source,
                int(candidate["address"]),
                int(baseline["address"]),
            )
        except RuntimeError as error:
            reject(f"relocation_rejected:{error}")
            continue
        eligible.append({
            "symbol": name,
            "effect": effect,
            "native": native,
            "baseline": baseline,
            "candidate": candidate,
            "body": body,
            "source_body": source,
            "repairs": repairs,
            "protected_entry": protected,
        })

    if not eligible:
        raise SystemExit("no observed checked frame body qualified")

    # Make the manifest deterministic and make profile concentration obvious.
    eligible.sort(
        key=lambda item: (
            -int(item["native"]["known_execution_count"]), item["symbol"]
        )
    )
    output.mkdir(parents=True)
    stock_bytes = BASELINE.read_bytes()
    image = bytearray(stock_bytes)
    edits: list[dict[str, Any]] = []
    origin_mappings: list[dict[str, Any]] = []
    selected_ranges: list[tuple[int, int]] = []

    for item in eligible:
        name = item["symbol"]
        baseline = item["baseline"]
        candidate = item["candidate"]
        native = item["native"]
        before = bytes(baseline["bytes"])
        body = bytes(item["body"])
        start = int(baseline["file_offset"])
        end = start + len(before)
        if any(start < prior_end and prior_start < end for prior_start, prior_end in selected_ranges):
            raise RuntimeError(f"overlapping selected symbol range: {name}")
        selected_ranges.append((start, end))
        image[start:end] = body + b"\x90" * (len(before) - len(body))

        native_instructions = {
            int(row["estimated_output_offset"]): row
            for row in native["instructions"]
        }
        emitted_instructions = list(
            decoder.disasm(body, int(baseline["address"]))
        )
        if sum(row.size for row in emitted_instructions) != len(body):
            raise RuntimeError(f"incomplete emitted decode: {name}")
        mappings: list[dict[str, Any]] = []
        for instruction in emitted_instructions:
            offset = int(instruction.address - int(baseline["address"]))
            identity = native_instructions.get(offset)
            if identity is None:
                raise RuntimeError(
                    f"missing original identity for {name}+0x{offset:x}"
                )
            if int(identity["estimated_size"]) != instruction.size:
                raise RuntimeError(
                    f"identity size mismatch for {name}+0x{offset:x}"
                )
            mapping = {
                "symbol": name,
                "emitted_address": int(instruction.address),
                "emitted_offset": offset,
                "emitted_bytes": instruction.bytes.hex(),
                "text": f"{instruction.mnemonic} {instruction.op_str}".strip(),
                "original_address": int(identity["original_address"]),
                "provenance_kind": str(identity["provenance_kind"]),
                "potential_memory_effect": any(
                    operand.type == capstone.x86.X86_OP_MEM
                    for operand in instruction.operands
                ),
            }
            mappings.append(mapping)
            origin_mappings.append(mapping)

        effect = item["effect"]
        edits.append({
            "symbol": name,
            "classification": effect["classification"],
            "known_execution_count": int(native["known_execution_count"]),
            "stock_address": int(baseline["address"]),
            "source_candidate_address": int(candidate["address"]),
            "file_offset": start,
            "capacity_bytes": len(before),
            "live_bytes": len(body),
            "padding_bytes": len(before) - len(body),
            "bytes_saved_in_live_body": len(before) - len(body),
            "stock_body_sha256": sha256_bytes(before),
            "source_body_sha256": sha256_bytes(item["source_body"]),
            "emitted_body_sha256": sha256_bytes(body),
            "protected_entry_preserved": item["protected_entry"],
            "reference_repairs": item["repairs"],
            "forwarding_dependencies": effect["forwarding_dependencies"],
            "origin_mapping_count": len(mappings),
        })

    # Explicitly prove the address-stability premise: all differing bytes are
    # inside one of the selected stock symbol ranges.
    changed_offsets = [
        index for index, (old, new) in enumerate(zip(stock_bytes, image)) if old != new
    ]
    outside = [
        offset
        for offset in changed_offsets
        if not any(start <= offset < end for start, end in selected_ranges)
    ]
    if outside:
        raise RuntimeError(f"changed {len(outside)} bytes outside selected ranges")

    output_image = output / "SmartSpeaker.marker_stock.checked-frame-overlay"
    temporary = output_image.with_name(f".{output_image.name}.tmp")
    temporary.write_bytes(image)
    temporary.chmod(BASELINE.stat().st_mode)
    os.replace(temporary, output_image)

    origins_path = output / "origin-overlays.json"
    origins_path.write_bytes(canonical({
        "schema_version": 1,
        "unchanged_instruction_identity": "exact stock-BOLT image and addresses",
        "changed_instruction_origins": origin_mappings,
    }))
    manifest = {
        "schema_version": 1,
        "status": "frozen_before_timing",
        "classification": "exact_stock_layout_plus_checked_abi_frame_rewrites",
        "inputs": {
            "stock_bolt": {"path": str(BASELINE), "sha256": sha256(BASELINE)},
            "full_firm_bolt_candidate": {
                "path": str(candidate_path), "sha256": sha256(candidate_path)
            },
            "wrapper_result": {
                "path": str(wrapper_result_path),
                "sha256": sha256(wrapper_result_path),
            },
            "native_report": {
                "path": str(native_report_path),
                "sha256": sha256(native_report_path),
            },
        },
        "selection_policy": {
            "classifications": sorted(ALLOWED_CLASSIFICATIONS),
            "requires_nonzero_frozen_profile_count": True,
            "profile_is_correctness_evidence": False,
            "unselected_and_unknown_paths": "exact stock-BOLT bytes",
            "layout_policy": "preserve every stock address and file offset",
        },
        "cohort": {
            "checked_frame_input_count": len(effect_edits),
            "selected_count": len(edits),
            "selected_profile_entry_count": sum(
                item["known_execution_count"] for item in edits
            ),
            "selected_live_bytes": sum(item["live_bytes"] for item in edits),
            "selected_capacity_bytes": sum(item["capacity_bytes"] for item in edits),
            "saved_live_bytes": sum(item["bytes_saved_in_live_body"] for item in edits),
            "changed_file_byte_count": len(changed_offsets),
            "outside_selected_range_change_count": len(outside),
            "rejection_counts": rejection_counts,
        },
        "edits": edits,
        "output": {
            "path": str(output_image),
            "sha256": sha256(output_image),
            "file_bytes": output_image.stat().st_size,
            "same_file_size_as_stock_bolt": output_image.stat().st_size
            == BASELINE.stat().st_size,
            "origin_overlays": str(origins_path),
            "origin_overlays_sha256": sha256(origins_path),
        },
        "measurement_plan": {
            "fixed_work_units_per_execution": 20000,
            "training_blocks": 100,
            "seed": 20261213,
            "minimum_useful_target_cpu_ratio": 1.005,
            "wall_non_regression_ratio": 1.0,
            "paired_bootstrap_confidence": 0.95,
            "frozen_before_timing": True,
            "outcome_dependent_extension": False,
        },
        "claim_boundaries": {
            "abi_contract": "explicit --assume-abi supported-host arm",
            "patch_contract": "empty_phase_one_contract",
            "finite_behavior_check_not_proof": True,
            "function_and_application_native_cost_only": True,
            "whole_application_capacity": False,
            "original_mcu_or_fastdyn_speedup": False,
        },
    }
    manifest_path = output / "selection.json"
    manifest_path.write_bytes(canonical(manifest))
    print(json.dumps({
        "output": str(output_image),
        "output_sha256": manifest["output"]["sha256"],
        "selected": [item["symbol"] for item in edits],
        "selected_count": len(edits),
        "selected_profile_entry_count": manifest["cohort"][
            "selected_profile_entry_count"
        ],
        "saved_live_bytes": manifest["cohort"]["saved_live_bytes"],
        "selection": str(manifest_path),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
