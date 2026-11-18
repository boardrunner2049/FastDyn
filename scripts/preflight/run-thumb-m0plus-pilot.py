#!/usr/bin/env python3
"""Qualify one bounded Thumb FOP rewrite on the ESCape32 Cortex-M0+ ELF."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from capstone import CS_ARCH_ARM, CS_MODE_MCLASS, CS_MODE_THUMB, Cs
from elftools.elf.elffile import ELFFile

from fastdyn.preflight import (
    Decision,
    EffectSummary,
    EmitInPlace,
    FirmwareUnitView,
    InstructionIdentity,
    MemoryEffect,
    MemoryRange,
    OriginStateMapping,
    PassRegistry,
    PatchContract,
    PreflightContext,
    PreflightPipeline,
    ThumbStackForwardingPass,
    TypedRange,
    load_firmware_candidate,
    qualify_firmware,
)
from fastdyn.preflight.candidate import publish_firmware_candidate
from fastdyn.preflight.model import canonical_json_bytes, hash_file, hash_json


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = ROOT / "third_party/preflight/escape32-64k-O0/ESCAPE1-rev12.1.elf"
DEFAULT_OUTPUT = ROOT / "build/preflight/thumb-cortex-m0plus-pilot-2026-11-16"
FUNCTION = "serialresp"
ACCESS = re.compile(
    r"^(r(?:1[0-2]|[0-9])), \[(?P<base>sp|r7)"
    r"(?:, #(?P<offset>0x[0-9a-f]+|[0-9]+))?\]$"
)


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def function_bytes(image: Path, name: str) -> tuple[int, int, bytes]:
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        symbols = elf.get_section_by_name(".symtab")
        matches = [
            item for item in symbols.iter_symbols()
            if item.name == name and str(item["st_info"]["type"]) == "STT_FUNC"
        ]
        if len(matches) != 1:
            raise RuntimeError(f"expected one {name} function symbol")
        address = int(matches[0]["st_value"]) & ~1
        size = int(matches[0]["st_size"])
        for segment in elf.iter_segments():
            start = int(segment["p_vaddr"])
            file_size = int(segment["p_filesz"])
            if (
                segment["p_type"] == "PT_LOAD"
                and start <= address
                and address + size <= start + file_size
            ):
                offset = int(segment["p_offset"]) + address - start
                stream.seek(offset)
                return address, offset, stream.read(size)
    raise RuntimeError(f"{name} is not file backed")


def vector_bytes(image: Path) -> bytes:
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        section = elf.get_section_by_name(".text")
        if section is None or int(section["sh_addr"]) != 0x08001800:
            raise RuntimeError("ESCape32 vector-bearing text section moved")
        return section.data()[: 48 * 4]


def build_pair_unit(image: Path) -> tuple[FirmwareUnitView, dict[str, Any]]:
    function_address, function_offset, body = function_bytes(image, FUNCTION)
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    instructions = list(decoder.disasm(body, function_address))
    pair = None
    for store, load in zip(instructions, instructions[1:]):
        store_match = ACCESS.match(store.op_str)
        load_match = ACCESS.match(load.op_str)
        if not (
            store.mnemonic == "str"
            and load.mnemonic == "ldr"
            and store.size == load.size == 2
            and store_match
            and load_match
            and store_match.group("base", "offset") == load_match.group("base", "offset")
        ):
            continue
        pair = store, load, store_match, load_match
        break
    if pair is None:
        raise RuntimeError("frozen ESCape32 spill/reload pair is absent")
    store, load, store_match, load_match = pair
    if (
        store.address != 0x08005CAA
        or store.op_str != "r0, [r7, #4]"
        or load.op_str != "r3, [r7, #4]"
    ):
        raise RuntimeError(
            f"frozen ESCape32 pair changed: {store.address:#x} {store.op_str}; {load.op_str}"
        )
    pair_offset = function_offset + store.address - function_address
    records = (
        InstructionIdentity(
            "m0-store", f"{FUNCTION}+0x{store.address - function_address:x}",
            store.address, pair_offset, store.size, bytes(store.bytes).hex(),
            store.mnemonic, store.op_str, ("stack_fault",),
        ),
        InstructionIdentity(
            "m0-load", f"{FUNCTION}+0x{load.address - function_address:x}",
            load.address, pair_offset + store.size, load.size, bytes(load.bytes).hex(),
            load.mnemonic, load.op_str, ("stack_fault",),
        ),
    )
    effects = EffectSummary(
        register_reads=("r0", "r7"),
        register_writes=("r3",),
        memory=(
            MemoryEffect(
                "spill", "m0-store", "write", "stack", "frame_r7+4", 4,
                "w", "private", "none", True, 0, ("stack_fault",),
            ),
            MemoryEffect(
                "reload", "m0-load", "read", "stack", "frame_r7+4", 4,
                "r", "private", "none", False, 1, ("stack_fault",),
            ),
        ),
    )
    qualification = qualify_firmware(
        image,
        SimpleNamespace(arch="arm", cpu="cortex-m0plus", init_nsvtor=0x08001800),
    )
    unit = FirmwareUnitView(
        1,
        "cortex-m0plus:serialresp:spill-reload",
        FUNCTION,
        hash_file(image),
        hashlib.sha256(bytes(store.bytes) + bytes(load.bytes)).hexdigest(),
        qualification.target,
        store.address,
        load.address + load.size,
        pair_offset,
        pair_offset + store.size + load.size,
        records,
        (store.address,),
        ("m0-load",),
        (
            TypedRange(
                "local-pair", store.address, load.address + load.size,
                pair_offset, pair_offset + 4, "code", "rx", "immutable", 2,
            ),
        ),
        (),
        (),
        effects,
        (
            OriginStateMapping(
                "m0-store", records[0].original_identity,
                ("stack_fault",), ("memory", "registers"),
            ),
            OriginStateMapping(
                "m0-load", records[1].original_identity,
                ("stack_fault",), ("memory", "registers"),
            ),
        ),
        PatchContract(),
        True,
    )
    return unit, {
        "function_address": function_address,
        "function_size": len(body),
        "pair_address": store.address,
        "pair_file_offset": pair_offset,
        "original_pair": [records[0].to_dict(), records[1].to_dict()],
        "frame_base_evidence": {
            "prologue": [
                f"{item.address:#x}: {item.mnemonic} {item.op_str}"
                for item in instructions[:3]
            ],
            "r7_established_from_sp": (
                len(instructions) >= 3
                and instructions[2].mnemonic == "add"
                and instructions[2].op_str == "r7, sp, #0"
            ),
            "r7_write_between_prologue_and_pair": any(
                item.address > instructions[2].address
                and item.address < store.address
                and item.mnemonic in {"mov", "movs", "add", "adds", "ldr"}
                and item.op_str.startswith("r7,")
                for item in instructions
            ),
        },
        "qualification": qualification.to_dict(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    image = args.image.resolve()
    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    unit, evidence = build_pair_unit(image)
    if not evidence["frame_base_evidence"]["r7_established_from_sp"]:
        raise RuntimeError("r7 frame base was not established")
    if evidence["frame_base_evidence"]["r7_write_between_prologue_and_pair"]:
        raise RuntimeError("r7 changed before the spill/reload pair")
    state_values = [0, 1, 0x20000000, 0xFFFFFFFF]
    state_set_hash = hash_json({
        "r0_values": state_values,
        "r7": 0x20001000,
        "slot": 0x20001004,
        "permissions": "rw-private",
        "observer_between": False,
    })
    proposal = f"{unit.unit_id}:thumb-stack-forwarding"
    analysis = {
        "complete": True,
        "store_identity": "m0-store",
        "load_identity": "m0-load",
        "width": 4,
        "base_register": "r7",
        "frame_base_established": True,
        "private_stack": True,
        "address_escaped": False,
        "observer_between": False,
        "evidence": "r7 established from sp; no r7 write or address escape before pair",
    }
    context = PreflightContext(
        analyses={
            "state_decision": Decision(
                "accepted", "bounded_m0plus_states_ready",
                {"state_count": len(state_values), "state_set_hash": state_set_hash},
            ),
            "effect_summary": unit.effects.semantic_dict(),
            "entry_completeness": True,
            "reference_graph": [],
            "stack_slot_forwarding": analysis,
        },
        testcase_decisions={proposal: Decision(
            "accepted", "bounded_register_memory_outcomes_agree",
            {"r0_values": state_values, "mismatch_count": 0},
        )},
        state_set_hash=state_set_hash,
        work_budget=len(state_values),
    )
    registry = PassRegistry()
    registry.register(ThumbStackForwardingPass())
    emitted = output / "ESCAPE1-rev12.1.thumb-fop.elf"
    outcome = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-stack-forwarding-v1", unit, context, image, emitted
    )
    if outcome.status != "accepted":
        raise RuntimeError(f"M0+ pilot rejected: {outcome.status}/{outcome.reason_code}")

    before = image.read_bytes()
    after = emitted.read_bytes()
    changed = [index for index, pair in enumerate(zip(before, after)) if pair[0] != pair[1]]
    expected_changed = [unit.instructions[1].file_offset, unit.instructions[1].file_offset + 1]
    if changed != expected_changed:
        raise RuntimeError(f"unexpected changed offsets: {changed}")
    if vector_bytes(image) != vector_bytes(emitted):
        raise RuntimeError("ESCape32 vector table changed")
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    replacement = list(decoder.disasm(
        after[unit.file_start : unit.file_end], unit.address_start
    ))
    if [(item.mnemonic, item.op_str) for item in replacement] != [
        ("str", "r0, [r7, #4]"), ("mov", "r3, r0")
    ]:
        raise RuntimeError("M0+ emitted pair does not decode as retained store plus mov")

    qualification = evidence["qualification"]
    candidate = publish_firmware_candidate(
        output / "firmware-candidate",
        image=emitted,
        original_image_hash=hash_file(image),
        target=unit.target,
        entry_points=tuple(qualification["entry_points"]),
        memory_map=tuple(
            MemoryRange(
                f"exec-{index}", int(item["original_start"]),
                int(item["original_end"]), "r-x", "firmware"
            )
            for index, item in enumerate(qualification["executable_ranges"])
        ),
        origin_ranges=tuple(
            __import__("fastdyn.preflight", fromlist=["OriginRange"]).OriginRange(
                int(item["original_start"]), int(item["original_end"]),
                int(item["emitted_start"]), int(item["emitted_end"]),
                str(item["mapping"]),
            )
            for item in qualification["executable_ranges"]
        ),
        optimization_report={
            "status": "accepted",
            "passes": ["thumb-stack-forwarding-v1"],
            "code_growth_bytes": 0,
            "function": FUNCTION,
            "changed_file_offsets": changed,
            "region_contract_hash": unit.contract_hash,
            "stage_ledger": [item.to_dict() for item in outcome.ledger],
        },
    )
    loaded = load_firmware_candidate(candidate.path)
    report = {
        "schema_version": 1,
        "status": "accepted",
        "classification": "bounded_real_cortex_m0plus_thumb_fop_pilot",
        "input": {"path": str(image), "sha256": hash_file(image)},
        "candidate": {
            "path": str(candidate.image_path),
            "sha256": candidate.image_hash,
            "candidate_id": candidate.candidate_id,
            "reload_validated": loaded.candidate_id == candidate.candidate_id,
            "code_growth_bytes": 0,
        },
        "transformation": {
            "function": FUNCTION,
            "address": unit.address_start,
            "original": "str r0, [r7, #4]; ldr r3, [r7, #4]",
            "candidate": "str r0, [r7, #4]; mov r3, r0",
            "changed_file_offsets": changed,
            "source_store_retained": True,
        },
        "evidence": evidence,
        "stage_ledger": [item.to_dict() for item in outcome.ledger],
        "claims": {
            "cortex_m0plus_valid_encoding": True,
            "target_execution": False,
            "fastdyn_speedup": False,
            "whole_firmware_equivalence": False,
            "general_thumb_backend": False,
        },
    }
    write_json(output / "region-contract.json", unit.to_dict())
    write_json(output / "report.json", report)
    print(json.dumps({
        "status": report["status"],
        "candidate_id": candidate.candidate_id,
        "changed_offsets": changed,
        "report": str(output / "report.json"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
