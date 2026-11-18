#!/usr/bin/env python3
"""Run real BOLT first, then apply the bounded firmware acceptance wrapper.

This supported-host path deliberately keeps optimization in BOLT.  The wrapper
records BOLT's post-optimization native-pass report, checks emitted ELF and
protected-entry obligations, runs a finite installation smoke, and retains the
earlier custom-pass results as a separately labelled baseline.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import capstone
from elftools.elf.constants import P_FLAGS, SH_FLAGS
from elftools.elf.elffile import ELFFile


ROOT = Path(__file__).resolve().parents[2]
RERUN = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
PROTOTYPE = ROOT / "build/preflight/preflight-sdk-v1-2026-11-16"
DEFAULT_OUTPUT = ROOT / "build/preflight/bolt-firmware-wrapper-2026-11-16"
DEFAULT_BOLT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bolt"
DEFAULT_BAT_DUMP = ROOT / "third_party/preflight/llvm-build/bin/llvm-bat-dump"
PRELOAD = ROOT / "build/preflight/lemix-smartspeaker/lemix_mmio_map.so"
FIXED_PRELOAD = (
    ROOT
    / "build/preflight/lemix-smartspeaker-marker-schedule-2026-11-17"
    / "lemix_mmio_marker_schedule.so"
)
EVENT = b"[JS:Debug] Interrupt_Handler_Task running"
INTERRUPT_EVENTS = frozenset({
    b"WWDG_IRQ", b"RTC_WKUP_IRQ", b"DMA1_Stream3_IRQ",
    b"DMA1_Stream4_IRQ", b"DMA1_Stream5_IRQ", b"DMA1_Stream6_IRQ",
    b"USART1_IRQ", b"USART2_IRQ", b"RTC_Alarm_IRQ", b"SDIO_IRQ",
})
FIXED_DRIVER_RE = re.compile(
    rb"FAST_FIXED_WORK start_tick=(\d+) end_tick=(\d+) completed_ticks=(\d+) "
    rb"start_markers=(\d+) end_markers=(\d+) completed_units=(\d+) "
    rb"driver_cpu_ns=(\d+)"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def elf_inventory(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        executable_sections = [
            {
                "name": section.name,
                "address": int(section["sh_addr"]),
                "size": int(section["sh_size"]),
            }
            for section in elf.iter_sections()
            if int(section["sh_flags"]) & SH_FLAGS.SHF_ALLOC
            and int(section["sh_flags"]) & SH_FLAGS.SHF_EXECINSTR
        ]
        executable_segments = [
            {
                "address": int(segment["p_vaddr"]),
                "file_size": int(segment["p_filesz"]),
                "memory_size": int(segment["p_memsz"]),
            }
            for segment in elf.iter_segments()
            if segment["p_type"] == "PT_LOAD"
            and int(segment["p_flags"]) & P_FLAGS.PF_X
        ]
        return {
            "sha256": sha256(path),
            "file_bytes": path.stat().st_size,
            "machine": str(elf.header["e_machine"]),
            "entry": int(elf.header["e_entry"]),
            "executable_section_bytes": sum(item["size"] for item in executable_sections),
            "executable_sections": executable_sections,
            "executable_segments": executable_segments,
            "has_bat": elf.get_section_by_name(".note.bolt_bat") is not None,
        }


def symbol_record(path: Path, name: str) -> dict[str, Any] | None:
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        matches = []
        for section in elf.iter_sections():
            if section["sh_type"] not in {"SHT_SYMTAB", "SHT_DYNSYM"}:
                continue
            matches.extend(symbol for symbol in section.iter_symbols() if symbol.name == name)
        if not matches:
            return None
        symbol = max(matches, key=lambda item: int(item["st_size"]))
        address = int(symbol["st_value"])
        size = int(symbol["st_size"])
        for segment in elf.iter_segments():
            start = int(segment["p_vaddr"])
            file_size = int(segment["p_filesz"])
            if segment["p_type"] == "PT_LOAD" and start <= address < start + file_size:
                available = start + file_size - address
                stream.seek(int(segment["p_offset"]) + address - start)
                file_offset = int(segment["p_offset"]) + address - start
                data = stream.read(min(size or available, available))
                return {
                    "address": address,
                    "file_offset": file_offset,
                    "declared_size": size,
                    "bytes": data,
                }
    return None


def symbol_bytes(path: Path, name: str) -> tuple[int, bytes] | None:
    record = symbol_record(path, name)
    if record is None:
        return None
    return int(record["address"]), bytes(record["bytes"])


def function_records(path: Path) -> dict[str, dict[str, Any]]:
    """Return unambiguous, sized ELF function symbols with exact file bytes."""

    with path.open("rb") as stream:
        elf = ELFFile(stream)
        raw: dict[str, list[tuple[int, int]]] = {}
        for section in elf.iter_sections():
            if section["sh_type"] not in {"SHT_SYMTAB", "SHT_DYNSYM"}:
                continue
            for symbol in section.iter_symbols():
                if str(symbol["st_info"]["type"]) != "STT_FUNC":
                    continue
                address = int(symbol["st_value"])
                size = int(symbol["st_size"])
                if symbol.name and address and size:
                    raw.setdefault(symbol.name, []).append((address, size))
        segments = [
            segment for segment in elf.iter_segments()
            if segment["p_type"] == "PT_LOAD"
        ]
        records = {}
        for name, candidates in raw.items():
            distinct = sorted(set(candidates))
            if len(distinct) != 1:
                continue
            address, size = distinct[0]
            for segment in segments:
                start = int(segment["p_vaddr"])
                file_size = int(segment["p_filesz"])
                if start <= address and address + size <= start + file_size:
                    file_offset = int(segment["p_offset"]) + address - start
                    stream.seek(file_offset)
                    records[name] = {
                        "address": address,
                        "file_offset": file_offset,
                        "declared_size": size,
                        "bytes": stream.read(size),
                    }
                    break
        return records


class BatIdentityResolver:
    """Resolve final emitted instruction addresses through BOLT's BAT table.

    Native pass offsets are recorded before final lowering and instruction
    shortening.  BAT is emitted after those phases, so it is authoritative for
    matching an actual candidate instruction/target back to an input offset.
    """

    _TRANSLATION = re.compile(
        r"^0x([0-9a-fA-F]+) -> (.+) \+ 0x([0-9a-fA-F]+)$"
    )

    def __init__(self, tool: Path, candidate: Path) -> None:
        self.tool = tool.resolve()
        self.candidate = candidate.resolve()
        if not self.tool.is_file():
            raise FileNotFoundError(self.tool)
        self.decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        self.decoder.detail = True
        self._cache: dict[
            tuple[str, int, int], list[tuple[capstone.CsInsn, int, str]]
        ] = {}

    def instructions(
        self,
        name: str,
        original: dict[str, Any],
        optimized: dict[str, Any],
    ) -> list[tuple[capstone.CsInsn, int, str]]:
        key = (name, int(optimized["address"]), len(optimized["bytes"]))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        decoded = list(
            self.decoder.disasm(optimized["bytes"], int(optimized["address"]))
        )
        if sum(item.size for item in decoded) != len(optimized["bytes"]):
            raise RuntimeError(f"final candidate body does not decode exactly: {name}")
        command = [str(self.tool), str(self.candidate)] + [
            f"--translate=0x{item.address:x}" for item in decoded
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        translations: dict[int, tuple[str, int]] = {}
        for line in result.stdout.splitlines():
            match = self._TRANSLATION.match(line.strip())
            if match is None:
                continue
            translations[int(match.group(1), 16)] = (
                match.group(2), int(match.group(3), 16)
            )
        if len(translations) != len(decoded):
            raise RuntimeError(
                f"BAT translated {len(translations)}/{len(decoded)} instructions in {name}"
            )
        records = []
        for instruction in decoded:
            bat_name, original_offset = translations[instruction.address]
            original_address = int(original["address"]) + original_offset
            if not (
                int(original["address"])
                <= original_address
                <= int(original["address"]) + len(original["bytes"])
            ):
                raise RuntimeError(f"BAT target escapes original function {name}")
            records.append((instruction, original_address, bat_name))
        self._cache[key] = records
        return records


def restored_bolt_name(name: str) -> str:
    return re.sub(r"(?:/\d+)?(?:\(\*\d+\))?$", "", name)


def automatic_compact_cohort(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    max_region_bytes: int,
) -> tuple[list[str], dict[str, int]]:
    """Select a conservative address-independent in-place adoption cohort."""

    original_symbols = function_records(reference)
    candidate_symbols = function_records(bolt_candidate)
    selected = []
    reasons: dict[str, int] = {}
    occupied: list[tuple[int, int]] = []

    def reject(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for function in native_report["functions"]:
        name = restored_bolt_name(str(function["name"]))
        instructions = function["instructions"]
        if function["ignored"] or function["injected"]:
            reject("ignored_or_injected")
            continue
        if not function["simple"] or function["multi_entry"] or function["split"]:
            reject("non_simple_multi_entry_or_split")
            continue
        if not instructions or any(item["original_address"] is None for item in instructions):
            reject("incomplete_instruction_origins")
            continue
        if any(item["is_call"] or item["is_branch"] for item in instructions):
            reject("call_or_branch")
            continue
        if any("%rip" in item["text"] for item in instructions):
            reject("pc_relative_reference")
            continue
        original = original_symbols.get(name)
        optimized = candidate_symbols.get(name)
        if original is None or optimized is None:
            reject("symbol_not_unambiguous_in_both_images")
            continue
        original_size = len(original["bytes"])
        optimized_size = len(optimized["bytes"])
        if original_size > max_region_bytes:
            reject("region_budget_exceeded")
            continue
        if optimized_size >= original_size:
            reject("not_smaller")
            continue
        if original["bytes"][:4] == bytes.fromhex("f30f1efa") and (
            optimized["bytes"][:4] != original["bytes"][:4]
        ):
            reject("protected_prefix_changed")
            continue
        start = int(original["file_offset"])
        end = start + original_size
        if any(start < other_end and other_start < end for other_start, other_end in occupied):
            reject("overlapping_symbol_range")
            continue
        occupied.append((start, end))
        selected.append(name)
    return selected, reasons


def direct_call_compact_cohort(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    max_region_bytes: int,
) -> tuple[list[str], dict[str, int]]:
    """Select bounded bodies needing only direct-call displacement repair."""

    original_symbols = function_records(reference)
    candidate_symbols = function_records(bolt_candidate)
    selected = []
    reasons: dict[str, int] = {}
    occupied: list[tuple[int, int]] = []

    def reject(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for function in native_report["functions"]:
        name = restored_bolt_name(str(function["name"]))
        instructions = function["instructions"]
        if function["ignored"] or function["injected"]:
            continue
        if not function["simple"] or function["multi_entry"] or function["split"]:
            continue
        if not instructions or any(item["original_address"] is None for item in instructions):
            continue
        if not any(item["is_call"] for item in instructions):
            continue
        if any(item["is_branch"] for item in instructions):
            reject("non_call_branch")
            continue
        if any("%rip" in item["text"] for item in instructions):
            reject("pc_relative_data_reference")
            continue
        original_calls = [
            item["original_address"]
            for item in function.get("original_instructions", [])
            if item["is_call"]
        ]
        post_calls = [
            item["original_address"] for item in instructions if item["is_call"]
        ]
        if original_calls != post_calls:
            reject("call_identity_or_order_changed")
            continue
        original = original_symbols.get(name)
        optimized = candidate_symbols.get(name)
        if original is None or optimized is None:
            reject("symbol_not_unambiguous_in_both_images")
            continue
        original_size = len(original["bytes"])
        optimized_size = len(optimized["bytes"])
        if original_size > max_region_bytes:
            reject("region_budget_exceeded")
            continue
        if optimized_size >= original_size:
            reject("not_smaller")
            continue
        if original["bytes"][:4] == bytes.fromhex("f30f1efa") and (
            optimized["bytes"][:4] != original["bytes"][:4]
        ):
            reject("protected_prefix_changed")
            continue
        start = int(original["file_offset"])
        end = start + original_size
        if any(start < other_end and other_start < end for other_start, other_end in occupied):
            reject("overlapping_symbol_range")
            continue
        occupied.append((start, end))
        selected.append(name)
    return selected, reasons


def repair_direct_calls_for_in_place(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    names: list[str],
    bat: BatIdentityResolver | None = None,
) -> tuple[dict[str, bytes], dict[str, list[dict[str, int]]]]:
    """Retarget exact x86 E8 rel32 calls from BOLT layout to original targets."""

    functions = {
        restored_bolt_name(str(item["name"])): item
        for item in native_report["functions"]
    }
    overrides: dict[str, bytes] = {}
    repairs: dict[str, list[dict[str, int]]] = {}
    for name in names:
        original = symbol_record(reference, name)
        optimized = symbol_record(bolt_candidate, name)
        if original is None or optimized is None:
            raise RuntimeError(f"missing direct-call body for {name}")
        body = bytearray(optimized["bytes"])
        function_repairs = []
        if bat is not None:
            actual_calls = [
                (instruction, original_address)
                for instruction, original_address, _bat_name
                in bat.instructions(name, original, optimized)
                if instruction.group(capstone.CS_GRP_CALL)
            ]
            expected_calls = [
                int(item["original_address"])
                for item in functions[name]["instructions"]
                if item["is_call"]
            ]
            if len(actual_calls) != len(expected_calls):
                raise RuntimeError(f"final call count changed in {name}")
            call_work = [
                {
                    "output_offset": instruction.address - int(optimized["address"]),
                    "original_address": expected_original,
                    "bat_translated_address": bat_original,
                    "instruction_size": instruction.size,
                }
                for (instruction, bat_original), expected_original
                in zip(actual_calls, expected_calls)
            ]
        else:
            call_work = [
                {
                    "output_offset": int(item["estimated_output_offset"]),
                    "original_address": int(item["original_address"]),
                    "instruction_size": int(item["estimated_size"]),
                }
                for item in functions[name]["instructions"]
                if item["is_call"]
            ]
        for item in call_work:
            output_offset = int(item["output_offset"])
            original_address = int(item["original_address"])
            original_offset = original_address - int(original["address"])
            instruction_size = int(item["instruction_size"])
            if (
                original_offset < 0
                or original_offset + instruction_size > len(original["bytes"])
                or output_offset < 0
                or output_offset + instruction_size > len(body)
            ):
                raise RuntimeError(f"call instruction is outside {name}")
            if (
                instruction_size != 5
                or output_offset + 5 > len(body)
                or body[output_offset] != 0xE8
                or original_offset < 0
                or original_offset + 5 > len(original["bytes"])
                or original["bytes"][original_offset] != 0xE8
            ):
                original_encoding = original["bytes"][
                    original_offset : original_offset + instruction_size
                ]
                candidate_encoding = bytes(
                    body[output_offset : output_offset + instruction_size]
                )
                if (
                    instruction_size >= 2
                    and original_encoding == candidate_encoding
                    and candidate_encoding[0] == 0xFF
                ):
                    # Register/memory-indirect calls have no placement-relative
                    # displacement. Their exact encoding and original identity
                    # are already checked by the cohort gate.
                    continue
                raise RuntimeError(f"unsupported non-E8 direct call in {name}")
            old_disp = int.from_bytes(
                original["bytes"][original_offset + 1 : original_offset + 5],
                "little",
                signed=True,
            )
            target = original_address + 5 + old_disp
            emitted_address = int(original["address"]) + output_offset
            new_disp = target - (emitted_address + 5)
            if not -(1 << 31) <= new_disp < (1 << 31):
                raise RuntimeError(f"direct call target is out of rel32 range in {name}")
            body[output_offset + 1 : output_offset + 5] = int(new_disp).to_bytes(
                4, "little", signed=True
            )
            repaired_target = emitted_address + 5 + int.from_bytes(
                body[output_offset + 1 : output_offset + 5], "little", signed=True
            )
            if repaired_target != target:
                raise RuntimeError(f"direct call repair verification failed in {name}")
            function_repairs.append(
                {
                    "call_original_address": original_address,
                    "emitted_call_address": emitted_address,
                    "target_original_address": target,
                    "output_offset": output_offset,
                    "identity_source": (
                        "native-order-plus-final-ELF-offset"
                        if bat is not None else "native-report"
                    ),
                    "bat_translated_address": item.get("bat_translated_address"),
                }
            )
        overrides[name] = bytes(body)
        repairs[name] = function_repairs
    return overrides, repairs


def _decode_x86_relative_branch(
    body: bytes, offset: int, instruction_address: int
) -> tuple[int, int] | None:
    """Return (absolute target, instruction size) for bounded rel8/rel32 jumps."""

    if offset >= len(body):
        return None
    opcode = body[offset]
    if opcode in range(0x70, 0x80) or opcode in {0xE0, 0xE1, 0xE2, 0xE3, 0xEB}:
        if offset + 2 > len(body):
            return None
        displacement = int.from_bytes(body[offset + 1 : offset + 2], "little", signed=True)
        return instruction_address + 2 + displacement, 2
    if opcode == 0xE9:
        if offset + 5 > len(body):
            return None
        displacement = int.from_bytes(body[offset + 1 : offset + 5], "little", signed=True)
        return instruction_address + 5 + displacement, 5
    if opcode == 0x0F and offset + 6 <= len(body) and 0x80 <= body[offset + 1] <= 0x8F:
        displacement = int.from_bytes(body[offset + 2 : offset + 6], "little", signed=True)
        return instruction_address + 6 + displacement, 6
    return None


def internal_branch_compact_cohort(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    max_region_bytes: int,
) -> tuple[list[str], dict[str, list[dict[str, int]]], dict[str, int]]:
    """Admit functions whose only control references are invariant local branches."""

    original_symbols = function_records(reference)
    candidate_symbols = function_records(bolt_candidate)
    selected: list[str] = []
    branch_records: dict[str, list[dict[str, int]]] = {}
    reasons: dict[str, int] = {}

    def reject(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for function in native_report["functions"]:
        name = restored_bolt_name(str(function["name"]))
        instructions = function["instructions"]
        if function["ignored"] or function["injected"]:
            continue
        if not function["simple"] or function["multi_entry"] or function["split"]:
            continue
        if not instructions or any(item["original_address"] is None for item in instructions):
            continue
        if any(item["is_call"] for item in instructions):
            continue
        if not any(item["is_branch"] for item in instructions):
            continue
        if any("%rip" in item["text"] for item in instructions):
            reject("pc_relative_data_reference")
            continue
        original = original_symbols.get(name)
        optimized = candidate_symbols.get(name)
        if original is None or optimized is None:
            reject("symbol_not_unambiguous_in_both_images")
            continue
        if len(original["bytes"]) > max_region_bytes:
            reject("region_budget_exceeded")
            continue
        if len(optimized["bytes"]) >= len(original["bytes"]):
            reject("not_smaller")
            continue
        if original["bytes"][:4] == bytes.fromhex("f30f1efa") and (
            optimized["bytes"][:4] != original["bytes"][:4]
        ):
            reject("protected_prefix_changed")
            continue
        original_branches = [
            item["original_address"]
            for item in function.get("original_instructions", [])
            if item["is_branch"]
        ]
        post_branches = [
            item["original_address"] for item in instructions if item["is_branch"]
        ]
        if original_branches != post_branches:
            reject("branch_identity_or_order_changed")
            continue
        post_by_offset = {
            int(item["estimated_output_offset"]): item for item in instructions
        }
        records = []
        failure = None
        for item in instructions:
            if not item["is_branch"]:
                continue
            output_offset = int(item["estimated_output_offset"])
            original_address = int(item["original_address"])
            original_offset = original_address - int(original["address"])
            candidate_branch = _decode_x86_relative_branch(
                optimized["bytes"],
                output_offset,
                int(optimized["address"]) + output_offset,
            )
            original_branch = _decode_x86_relative_branch(
                original["bytes"], original_offset, original_address
            )
            if candidate_branch is None or original_branch is None:
                failure = "unsupported_branch_encoding"
                break
            candidate_target, candidate_size = candidate_branch
            original_target, _ = original_branch
            target_offset = candidate_target - int(optimized["address"])
            target_instruction = post_by_offset.get(target_offset)
            if (
                candidate_size != int(item["estimated_size"])
                or target_offset < 0
                or target_offset >= len(optimized["bytes"])
                or target_instruction is None
                or int(target_instruction["original_address"]) != original_target
            ):
                failure = "branch_target_identity_changed_or_external"
                break
            emitted_branch = int(original["address"]) + output_offset
            emitted_target = int(original["address"]) + target_offset
            if emitted_target - (emitted_branch + candidate_size) != (
                candidate_target
                - (int(optimized["address"]) + output_offset + candidate_size)
            ):
                failure = "branch_displacement_not_position_invariant"
                break
            records.append(
                {
                    "branch_original_address": original_address,
                    "target_original_address": original_target,
                    "emitted_branch_address": emitted_branch,
                    "emitted_target_address": emitted_target,
                    "output_offset": output_offset,
                    "target_output_offset": target_offset,
                }
            )
        if failure is not None:
            reject(failure)
            continue
        selected.append(name)
        branch_records[name] = records
    return selected, branch_records, reasons


def branch_heavy_compact_cohort(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    excluded_names: set[str],
    max_region_bytes: int,
    bat: BatIdentityResolver | None = None,
) -> tuple[
    list[str],
    dict[str, list[dict[str, int]]],
    dict[str, int],
    list[dict[str, Any]],
]:
    """Combine strict local branches with the existing call/data repair rules."""

    original_symbols = function_records(reference)
    candidate_symbols = function_records(bolt_candidate)
    selected: list[str] = []
    branch_records: dict[str, list[dict[str, int]]] = {}
    reasons: dict[str, int] = {}
    diagnostics: list[dict[str, Any]] = []

    def reject(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for function in native_report["functions"]:
        name = restored_bolt_name(str(function["name"]))
        instructions = function["instructions"]
        if name in excluded_names:
            continue
        if function["ignored"] or function["injected"]:
            continue
        if not function["simple"] or function["multi_entry"] or function["split"]:
            continue
        if not instructions:
            continue
        if any(
            item["original_address"] is None
            and not (
                item.get("provenance_kind") == "synthesized_control_edge"
                and item.get("control_edge_was_original") is True
            )
            for item in instructions
        ):
            reject("unproven_instruction_provenance")
            continue
        if function.get("control_edge_set_preserved") is not True:
            reject("control_edge_set_changed")
            continue
        if not any(item["is_branch"] for item in instructions):
            continue
        original = original_symbols.get(name)
        optimized = candidate_symbols.get(name)
        if original is None or optimized is None:
            reject("symbol_not_unambiguous_in_both_images")
            continue
        if len(original["bytes"]) > max_region_bytes:
            reject("region_budget_exceeded")
            continue
        if len(optimized["bytes"]) >= len(original["bytes"]):
            reject("not_smaller")
            continue
        if original["bytes"][:4] == bytes.fromhex("f30f1efa") and (
            optimized["bytes"][:4] != original["bytes"][:4]
        ):
            reject("protected_prefix_changed")
            continue

        def identities(source: list[dict[str, Any]], field: str) -> list[int]:
            if field == "rip":
                return [
                    int(item["original_address"])
                    for item in source
                    if "%rip" in item["text"]
                ]
            return [
                int(item["original_address"])
                for item in source
                if item[field]
            ]

        originals = function.get("original_instructions", [])
        if identities(originals, "is_call") != identities(instructions, "is_call"):
            reject("call_identity_or_order_changed")
            continue
        if identities(originals, "rip") != identities(instructions, "rip"):
            reject("rip_reference_identity_or_order_changed")
            continue

        original_branch_identities = [
            int(item["original_address"])
            for item in originals
            if item["is_branch"] and not item.get("is_unconditional_branch", False)
        ]
        native_branch_descriptors = []
        for instruction in instructions:
            if not instruction["is_branch"]:
                continue
            if instruction.get("is_unconditional_branch", False):
                if (
                    instruction.get("control_edge_was_original") is not True
                    or instruction.get("source_block_original_address") is None
                    or instruction.get("target_block_original_address") is None
                ):
                    reject("unproven_unconditional_control_edge")
                    native_branch_descriptors = []
                    break
                native_branch_descriptors.append(
                    {
                        "provenance_kind": "control_edge",
                        "original_address": instruction["original_address"],
                        "source_block_original_address": int(
                            instruction["source_block_original_address"]
                        ),
                        "target_block_original_address": int(
                            instruction["target_block_original_address"]
                        ),
                    }
                )
            elif instruction["original_address"] is not None:
                native_branch_descriptors.append(
                    {
                        "provenance_kind": "original_conditional_branch",
                        "original_address": int(instruction["original_address"]),
                    }
                )
            else:
                reject("unproven_conditional_branch_provenance")
                native_branch_descriptors = []
                break
        if not native_branch_descriptors:
            continue
        native_branch_identities = [
            item["original_address"]
            for item in native_branch_descriptors
            if item["provenance_kind"] == "original_conditional_branch"
        ]
        if original_branch_identities != native_branch_identities:
            reject("branch_identity_or_order_changed")
            diagnostics.append(
                {
                    "symbol": name,
                    "reason": "branch_identity_or_order_changed",
                    "original_branch_identities": original_branch_identities,
                    "candidate_branch_identities": native_branch_identities,
                    "identity_source": "native-report",
                }
            )
            continue

        if bat is None:
            actual_instructions = None
        else:
            actual_instructions = bat.instructions(name, original, optimized)
        if actual_instructions is not None:
            actual_branches = [
                (item, original_address)
                for item, original_address, _bat_name in actual_instructions
                if item.group(capstone.CS_GRP_JUMP)
                and len(item.operands) == 1
                and item.operands[0].type == capstone.x86.X86_OP_IMM
            ]
            if len(actual_branches) != len(native_branch_descriptors):
                reject("final_branch_count_changed")
                diagnostics.append(
                    {
                        "symbol": name,
                        "reason": "final_branch_count_changed",
                        "native_branch_count": len(native_branch_descriptors),
                        "final_branch_count": len(actual_branches),
                    }
                )
                continue
            candidate_branch_identities = native_branch_identities
        else:
            actual_branches = []
            candidate_branch_identities = native_branch_identities

        records = []
        failure = None
        if actual_instructions is not None:
            translated_by_output = {
                item.address: original_address
                for item, original_address, _bat_name in actual_instructions
            }
            branch_work = [
                {
                    "output_offset": item.address - int(optimized["address"]),
                    **native_descriptor,
                    "bat_translated_source_address": bat_original_address,
                    "candidate_target": int(item.operands[0].imm),
                    "candidate_size": item.size,
                }
                for (item, bat_original_address), native_descriptor
                in zip(actual_branches, native_branch_descriptors)
            ]
        else:
            translated_by_output = {}
            branch_work = []
            for item in instructions:
                if not item["is_branch"]:
                    continue
                output_offset = int(item["estimated_output_offset"])
                candidate_branch = _decode_x86_relative_branch(
                    optimized["bytes"],
                    output_offset,
                    int(optimized["address"]) + output_offset,
                )
                if candidate_branch is None:
                    failure = "unsupported_branch_encoding"
                    break
                branch_work.append(
                    {
                        "output_offset": output_offset,
                        "provenance_kind": (
                            "control_edge"
                            if item.get("is_unconditional_branch", False)
                            else "original_conditional_branch"
                        ),
                        "original_address": item["original_address"],
                        "source_block_original_address": item.get(
                            "source_block_original_address"
                        ),
                        "target_block_original_address": item.get(
                            "target_block_original_address"
                        ),
                        "candidate_target": candidate_branch[0],
                        "candidate_size": candidate_branch[1],
                    }
                )

        for item in branch_work:
            output_offset = int(item["output_offset"])
            provenance_kind = item.get("provenance_kind", "original_instruction")
            if provenance_kind == "original_conditional_branch":
                original_address = int(item["original_address"])
                original_offset = original_address - int(original["address"])
                original_branch = _decode_x86_relative_branch(
                    original["bytes"], original_offset, original_address
                )
                if original_branch is None:
                    failure = "unsupported_branch_encoding"
                    diagnostics.append(
                        {
                            "symbol": name,
                            "reason": failure,
                            "branch_original_address": original_address,
                        }
                    )
                    break
                original_target, _ = original_branch
            elif provenance_kind == "control_edge":
                original_address = item.get("original_address")
                original_target = int(item["target_block_original_address"])
            else:
                failure = "unproven_branch_provenance"
                break
            candidate_target = int(item["candidate_target"])
            candidate_size = int(item["candidate_size"])
            target_offset = candidate_target - int(optimized["address"])
            if target_offset < 0 or target_offset >= len(optimized["bytes"]):
                failure = "external_branch_target"
                diagnostics.append(
                    {
                        "symbol": name,
                        "reason": failure,
                        "branch_original_address": original_address,
                        "original_target_address": original_target,
                        "candidate_target_address": candidate_target,
                    }
                )
                break
            if actual_instructions is not None:
                target_original_address = translated_by_output.get(candidate_target)
            else:
                post_by_offset = {
                    int(candidate["estimated_output_offset"]): candidate
                    for candidate in instructions
                }
                target_instruction = post_by_offset.get(target_offset)
                target_original_address = (
                    None
                    if target_instruction is None
                    or target_instruction.get("original_address") is None
                    else int(target_instruction["original_address"])
                )
            if target_original_address is None:
                failure = "branch_target_not_instruction_boundary"
                diagnostics.append(
                    {
                        "symbol": name,
                        "reason": failure,
                        "branch_original_address": original_address,
                        "original_target_address": original_target,
                        "candidate_target_output_offset": target_offset,
                    }
                )
                break
            if target_original_address != original_target:
                failure = "branch_target_identity_changed"
                diagnostics.append(
                    {
                        "symbol": name,
                        "reason": failure,
                        "branch_original_address": original_address,
                        "original_target_address": original_target,
                        "candidate_target_output_offset": target_offset,
                        "candidate_target_original_address": target_original_address,
                        "identity_source": (
                            "BAT-final-ELF" if bat is not None else "native-report"
                        ),
                    }
                )
                break
            emitted_branch = int(original["address"]) + output_offset
            emitted_target = int(original["address"]) + target_offset
            records.append(
                {
                    "branch_original_address": original_address,
                    "provenance_kind": provenance_kind,
                    "source_block_original_address": item.get(
                        "source_block_original_address"
                    ),
                    "target_original_address": original_target,
                    "emitted_branch_address": emitted_branch,
                    "emitted_target_address": emitted_target,
                    "output_offset": output_offset,
                    "target_output_offset": target_offset,
                    "identity_source": (
                        "native-source-final-offset-BAT-target"
                        if bat is not None else "native-report"
                    ),
                    "bat_translated_source_address": item.get(
                        "bat_translated_source_address"
                    ),
                }
            )
        if failure is not None:
            reject(failure)
            continue
        selected.append(name)
        branch_records[name] = records
    return selected, branch_records, reasons, diagnostics


def rip_relative_compact_cohort(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    max_region_bytes: int,
) -> tuple[list[str], dict[str, int]]:
    """Select leaf bodies whose only remaining references are RIP-relative data."""

    original_symbols = function_records(reference)
    candidate_symbols = function_records(bolt_candidate)
    selected = []
    reasons: dict[str, int] = {}

    def reject(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for function in native_report["functions"]:
        name = restored_bolt_name(str(function["name"]))
        instructions = function["instructions"]
        if function["ignored"] or function["injected"]:
            continue
        if not function["simple"] or function["multi_entry"] or function["split"]:
            continue
        if not instructions or any(item["original_address"] is None for item in instructions):
            continue
        if any(item["is_call"] or item["is_branch"] for item in instructions):
            continue
        if not any("%rip" in item["text"] for item in instructions):
            continue
        original_references = [
            item["original_address"]
            for item in function.get("original_instructions", [])
            if "%rip" in item["text"]
        ]
        post_references = [
            item["original_address"]
            for item in instructions
            if "%rip" in item["text"]
        ]
        if original_references != post_references:
            reject("rip_reference_identity_or_order_changed")
            continue
        original = original_symbols.get(name)
        optimized = candidate_symbols.get(name)
        if original is None or optimized is None:
            reject("symbol_not_unambiguous_in_both_images")
            continue
        if len(original["bytes"]) > max_region_bytes:
            reject("region_budget_exceeded")
            continue
        if len(optimized["bytes"]) >= len(original["bytes"]):
            reject("not_smaller")
            continue
        if original["bytes"][:4] == bytes.fromhex("f30f1efa") and (
            optimized["bytes"][:4] != original["bytes"][:4]
        ):
            reject("protected_prefix_changed")
            continue
        selected.append(name)
    return selected, reasons


def call_rip_compact_cohort(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    max_region_bytes: int,
) -> tuple[list[str], dict[str, int]]:
    """Select bodies combining already-supported direct-call and RIP references."""

    original_symbols = function_records(reference)
    candidate_symbols = function_records(bolt_candidate)
    selected = []
    reasons: dict[str, int] = {}

    def reject(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for function in native_report["functions"]:
        name = restored_bolt_name(str(function["name"]))
        instructions = function["instructions"]
        if function["ignored"] or function["injected"]:
            continue
        if not function["simple"] or function["multi_entry"] or function["split"]:
            continue
        if not instructions or any(item["original_address"] is None for item in instructions):
            continue
        if not any(item["is_call"] for item in instructions):
            continue
        if any(item["is_branch"] for item in instructions):
            continue
        if not any("%rip" in item["text"] for item in instructions):
            continue
        original_calls = [
            item["original_address"]
            for item in function.get("original_instructions", [])
            if item["is_call"]
        ]
        post_calls = [
            item["original_address"] for item in instructions if item["is_call"]
        ]
        original_references = [
            item["original_address"]
            for item in function.get("original_instructions", [])
            if "%rip" in item["text"]
        ]
        post_references = [
            item["original_address"]
            for item in instructions
            if "%rip" in item["text"]
        ]
        if original_calls != post_calls:
            reject("call_identity_or_order_changed")
            continue
        if original_references != post_references:
            reject("rip_reference_identity_or_order_changed")
            continue
        original = original_symbols.get(name)
        optimized = candidate_symbols.get(name)
        if original is None or optimized is None:
            reject("symbol_not_unambiguous_in_both_images")
            continue
        if len(original["bytes"]) > max_region_bytes:
            reject("region_budget_exceeded")
            continue
        if len(optimized["bytes"]) >= len(original["bytes"]):
            reject("not_smaller")
            continue
        if original["bytes"][:4] == bytes.fromhex("f30f1efa") and (
            optimized["bytes"][:4] != original["bytes"][:4]
        ):
            reject("protected_prefix_changed")
            continue
        selected.append(name)
    return selected, reasons


def exact_leaf_call_inline_compact_cohort(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    max_region_bytes: int,
) -> tuple[list[str], dict[str, int]]:
    """Select explicitly ledgered leaf-call inlines that fit in place.

    Unlike the ordinary compact cohorts, this cohort intentionally admits a
    same-size body: removing a hot call/return boundary can be profitable even
    when the exact replacement load occupies the reclaimed bytes. Semantic
    admission remains the responsibility of ``qualify_memory_effect_contract``;
    this function only establishes bounded structural and placement support.
    """

    original_symbols = function_records(reference)
    candidate_symbols = function_records(bolt_candidate)
    inline_functions = {
        int(item["function_original_address"])
        for item in native_report.get("arithmetic_fusions", [])
        if item.get("kind") in {
            "x86_exact_leaf_call_inline",
            "x86_exact_pointer_accessor_call_inline",
            "x86_exact_empty_call_elimination",
        }
    }
    selected: list[str] = []
    reasons: dict[str, int] = {}

    def reject(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for function in native_report["functions"]:
        if int(function["original_address"]) not in inline_functions:
            continue
        name = restored_bolt_name(str(function["name"]))
        instructions = function["instructions"]
        if function["ignored"] or function["injected"]:
            reject("ignored_or_injected")
            continue
        if not function["simple"] or function["multi_entry"] or function["split"]:
            reject("non_simple_multi_entry_or_split")
            continue
        if not instructions or any(
            item["original_address"] is None for item in instructions
        ):
            reject("incomplete_instruction_origins")
            continue
        if any(item["is_branch"] for item in instructions):
            reject("branch_repair_not_supported")
            continue
        original = original_symbols.get(name)
        optimized = candidate_symbols.get(name)
        if original is None or optimized is None:
            reject("symbol_not_unambiguous_in_both_images")
            continue
        if len(original["bytes"]) > max_region_bytes:
            reject("region_budget_exceeded")
            continue
        if len(optimized["bytes"]) > len(original["bytes"]):
            reject("does_not_fit_original_range")
            continue
        if original["bytes"][:4] == bytes.fromhex("f30f1efa") and (
            optimized["bytes"][:4] != original["bytes"][:4]
        ):
            reject("protected_prefix_changed")
            continue
        selected.append(name)
    return selected, reasons


def _single_pc_relative_target(instruction: dict[str, Any], name: str) -> int:
    references = [
        item
        for item in instruction.get("symbol_references", [])
        if item.get("kind") == "pc_relative"
        and item.get("target_original_address") is not None
    ]
    if len(references) != 1:
        raise RuntimeError(f"ambiguous RIP-relative reference graph in {name}")
    return int(references[0]["target_original_address"])


def repair_rip_relative_for_in_place(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    names: list[str],
    body_overrides: dict[str, bytes] | None = None,
    bat: BatIdentityResolver | None = None,
) -> tuple[dict[str, bytes], dict[str, list[dict[str, int]]]]:
    """Repair x86 RIP-relative data displacements to exact original targets."""

    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    decoder.detail = True
    functions = {
        restored_bolt_name(str(item["name"])): item
        for item in native_report["functions"]
    }
    overrides: dict[str, bytes] = {}
    repairs: dict[str, list[dict[str, int]]] = {}
    for name in names:
        original = symbol_record(reference, name)
        optimized = symbol_record(bolt_candidate, name)
        if original is None or optimized is None:
            raise RuntimeError(f"missing RIP-relative body for {name}")
        body = bytearray(
            body_overrides[name]
            if body_overrides is not None and name in body_overrides
            else optimized["bytes"]
        )
        records = []
        if bat is not None:
            actual_rip = []
            for instruction, original_address, _bat_name in bat.instructions(
                name, original, optimized
            ):
                operands = [
                    operand
                    for operand in instruction.operands
                    if operand.type == capstone.x86.X86_OP_MEM
                    and operand.mem.base == capstone.x86.X86_REG_RIP
                ]
                if operands:
                    actual_rip.append((instruction, original_address))
            expected_rip = [
                item
                for item in functions[name]["instructions"]
                if "%rip" in item["text"]
            ]
            if len(actual_rip) != len(expected_rip):
                raise RuntimeError(f"final RIP-relative count changed in {name}")
            rip_work = [
                {
                    "output_offset": instruction.address - int(optimized["address"]),
                    "original_address": int(expected["original_address"]),
                    "target_original_address": _single_pc_relative_target(
                        expected, name
                    ),
                    "bat_translated_address": bat_original,
                    "candidate_instruction": instruction,
                    "estimated_size": instruction.size,
                }
                for (instruction, bat_original), expected
                in zip(actual_rip, expected_rip)
            ]
        else:
            rip_work = [
                {
                    "output_offset": int(item["estimated_output_offset"]),
                    "original_address": int(item["original_address"]),
                    "target_original_address": _single_pc_relative_target(
                        item, name
                    ),
                    "candidate_instruction": None,
                    "estimated_size": int(item["estimated_size"]),
                }
                for item in functions[name]["instructions"]
                if "%rip" in item["text"]
            ]
        for item in rip_work:
            output_offset = int(item["output_offset"])
            original_address = int(item["original_address"])
            original_offset = original_address - int(original["address"])
            candidate_instruction = item["candidate_instruction"] or next(
                decoder.disasm(
                    optimized["bytes"][output_offset:],
                    int(optimized["address"]) + output_offset,
                    count=1,
                ),
                None,
            )
            if candidate_instruction is None:
                raise RuntimeError(f"cannot decode RIP-relative instruction in {name}")

            def rip_operand(instruction: capstone.CsInsn) -> Any:
                operands = [
                    operand
                    for operand in instruction.operands
                    if operand.type == capstone.x86.X86_OP_MEM
                    and operand.mem.base == capstone.x86.X86_REG_RIP
                ]
                return operands[0] if len(operands) == 1 else None

            candidate_operand = rip_operand(candidate_instruction)
            if (
                candidate_operand is None
                or candidate_instruction.disp_size != 4
                or candidate_instruction.size != int(item["estimated_size"])
            ):
                raise RuntimeError(f"unsupported RIP-relative encoding in {name}")
            target = int(item["target_original_address"])
            candidate_target = (
                candidate_instruction.address
                + candidate_instruction.size
                + candidate_operand.mem.disp
            )
            if candidate_target != target:
                raise RuntimeError(
                    f"RIP-relative reference target identity changed in {name}"
                )
            emitted_address = int(original["address"]) + output_offset
            new_disp = target - (emitted_address + candidate_instruction.size)
            if not -(1 << 31) <= new_disp < (1 << 31):
                raise RuntimeError(f"RIP-relative target is out of range in {name}")
            displacement_offset = output_offset + candidate_instruction.disp_offset
            body[displacement_offset : displacement_offset + 4] = int(new_disp).to_bytes(
                4, "little", signed=True
            )
            repaired_target = (
                emitted_address
                + candidate_instruction.size
                + int.from_bytes(
                    body[displacement_offset : displacement_offset + 4],
                    "little",
                    signed=True,
                )
            )
            if repaired_target != target:
                raise RuntimeError(f"RIP-relative repair verification failed in {name}")
            records.append(
                {
                    "instruction_original_address": original_address,
                    "emitted_instruction_address": emitted_address,
                    "target_original_address": target,
                    "output_offset": output_offset,
                    "displacement_offset": candidate_instruction.disp_offset,
                    "identity_source": (
                        "native-order-plus-final-ELF-offset"
                        if bat is not None else "native-report"
                    ),
                    "bat_translated_address": item.get("bat_translated_address"),
                }
            )
        if not records:
            raise RuntimeError(f"RIP-relative cohort member {name} has no references")
        overrides[name] = bytes(body)
        repairs[name] = records
    return overrides, repairs


def _repair_reason(error: RuntimeError) -> str:
    message = str(error)
    for suffix in (" in ", " for "):
        if suffix in message:
            message = message.split(suffix, 1)[0]
    return re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")


def repair_direct_calls_bounded(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    names: list[str],
    bat: BatIdentityResolver,
) -> tuple[
    list[str],
    dict[str, bytes],
    dict[str, list[dict[str, int]]],
    dict[str, int],
    list[dict[str, str]],
]:
    accepted = []
    overrides: dict[str, bytes] = {}
    repairs: dict[str, list[dict[str, int]]] = {}
    rejection_counts: dict[str, int] = {}
    diagnostics = []
    for name in names:
        try:
            one_override, one_repair = repair_direct_calls_for_in_place(
                native_report, reference, bolt_candidate, [name], bat
            )
        except RuntimeError as error:
            reason = _repair_reason(error)
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            diagnostics.append({"symbol": name, "reason": reason, "detail": str(error)})
            continue
        accepted.append(name)
        overrides.update(one_override)
        repairs.update(one_repair)
    return accepted, overrides, repairs, rejection_counts, diagnostics


def repair_rip_relative_bounded(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    names: list[str],
    bat: BatIdentityResolver,
    body_overrides: dict[str, bytes] | None = None,
) -> tuple[
    list[str],
    dict[str, bytes],
    dict[str, list[dict[str, int]]],
    dict[str, int],
    list[dict[str, str]],
]:
    accepted = []
    overrides: dict[str, bytes] = {}
    repairs: dict[str, list[dict[str, int]]] = {}
    rejection_counts: dict[str, int] = {}
    diagnostics = []
    for name in names:
        one_body = (
            {name: body_overrides[name]}
            if body_overrides is not None and name in body_overrides
            else None
        )
        try:
            one_override, one_repair = repair_rip_relative_for_in_place(
                native_report,
                reference,
                bolt_candidate,
                [name],
                body_overrides=one_body,
                bat=bat,
            )
        except RuntimeError as error:
            reason = _repair_reason(error)
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            diagnostics.append({"symbol": name, "reason": reason, "detail": str(error)})
            continue
        accepted.append(name)
        overrides.update(one_override)
        repairs.update(one_repair)
    return accepted, overrides, repairs, rejection_counts, diagnostics


def repair_absolute_symbol_references_for_in_place(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    name: str,
    bat: BatIdentityResolver,
    body: bytes,
) -> tuple[bytes, list[dict[str, Any]]]:
    """Restore address-valued immediates after importing a relocated BOLT body."""

    functions = {
        restored_bolt_name(str(item["name"])): item
        for item in native_report["functions"]
    }
    original = symbol_record(reference, name)
    optimized = symbol_record(bolt_candidate, name)
    if original is None or optimized is None:
        raise RuntimeError(f"missing absolute-reference body for {name}")
    native_references = [
        (instruction, reference_item)
        for instruction in functions[name]["instructions"]
        for reference_item in instruction.get("symbol_references", [])
        if reference_item.get("kind") == "absolute_immediate"
    ]
    if not native_references:
        return body, []
    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    decoder.detail = True
    actual = bat.instructions(name, original, optimized)
    cursor = 0
    repaired = bytearray(body)
    records = []

    for native_instruction, reference_item in native_references:
        symbol_name = restored_bolt_name(str(reference_item["symbol"]))
        candidate_symbol = symbol_record(bolt_candidate, symbol_name)
        if candidate_symbol is None:
            raise RuntimeError(f"unresolved absolute symbol {symbol_name} in {name}")
        candidate_target = (
            int(candidate_symbol["address"]) + int(reference_item.get("addend", 0))
        )
        match = None
        while cursor < len(actual):
            instruction, bat_original, _bat_name = actual[cursor]
            cursor += 1
            if instruction.group(capstone.CS_GRP_CALL) or instruction.group(
                capstone.CS_GRP_JUMP
            ):
                continue
            immediates = [
                operand.imm
                for operand in instruction.operands
                if operand.type == capstone.x86.X86_OP_IMM
            ]
            if (
                len(immediates) == 1
                and int(immediates[0]) == candidate_target
                and instruction.imm_size in {1, 2, 4, 8}
            ):
                match = (instruction, bat_original)
                break
        if match is None:
            raise RuntimeError(f"absolute symbol encoding not found in {name}")
        instruction, bat_original = match
        original_address = int(native_instruction["original_address"])
        original_offset = original_address - int(original["address"])
        original_instruction = next(
            decoder.disasm(
                original["bytes"][original_offset:], original_address, count=1
            ),
            None,
        )
        if original_instruction is None:
            raise RuntimeError(f"cannot decode absolute symbol source in {name}")
        original_immediates = [
            operand.imm
            for operand in original_instruction.operands
            if operand.type == capstone.x86.X86_OP_IMM
        ]
        target_original = int(reference_item["target_original_address"])
        if (
            len(original_immediates) != 1
            or int(original_immediates[0]) != target_original
            or original_instruction.imm_size != instruction.imm_size
        ):
            raise RuntimeError(f"absolute symbol source mismatch in {name}")
        width = int(instruction.imm_size)
        if target_original < 0 or target_original >= 1 << (8 * width):
            raise RuntimeError(f"absolute symbol target does not fit in {name}")
        output_offset = instruction.address - int(optimized["address"])
        immediate_offset = output_offset + int(instruction.imm_offset)
        if immediate_offset < 0 or immediate_offset + width > len(repaired):
            raise RuntimeError(f"absolute symbol repair escapes {name}")
        repaired[immediate_offset : immediate_offset + width] = target_original.to_bytes(
            width, "little", signed=False
        )
        emitted_address = int(original["address"]) + output_offset
        verified = next(
            decoder.disasm(bytes(repaired[output_offset:]), emitted_address, count=1),
            None,
        )
        verified_immediates = [] if verified is None else [
            operand.imm
            for operand in verified.operands
            if operand.type == capstone.x86.X86_OP_IMM
        ]
        if len(verified_immediates) != 1 or int(verified_immediates[0]) != target_original:
            raise RuntimeError(f"absolute symbol repair verification failed in {name}")
        records.append(
            {
                "instruction_original_address": original_address,
                "emitted_instruction_address": emitted_address,
                "symbol": symbol_name,
                "target_original_address": target_original,
                "candidate_target_address": candidate_target,
                "output_offset": output_offset,
                "immediate_offset": int(instruction.imm_offset),
                "immediate_size": width,
                "bat_translated_address": bat_original,
            }
        )
    return bytes(repaired), records


def repair_absolute_symbol_references_bounded(
    native_report: dict[str, Any],
    reference: Path,
    bolt_candidate: Path,
    names: list[str],
    bat: BatIdentityResolver,
    body_overrides: dict[str, bytes],
) -> tuple[
    list[str],
    dict[str, bytes],
    dict[str, list[dict[str, Any]]],
    dict[str, int],
    list[dict[str, str]],
]:
    accepted = []
    overrides: dict[str, bytes] = {}
    repairs: dict[str, list[dict[str, Any]]] = {}
    rejection_counts: dict[str, int] = {}
    diagnostics = []
    for name in names:
        original = symbol_record(bolt_candidate, name)
        if original is None:
            error = RuntimeError(f"missing absolute-reference body for {name}")
        else:
            try:
                repaired, records = repair_absolute_symbol_references_for_in_place(
                    native_report,
                    reference,
                    bolt_candidate,
                    name,
                    bat,
                    body_overrides.get(name, bytes(original["bytes"])),
                )
            except RuntimeError as caught:
                error = caught
            else:
                accepted.append(name)
                overrides[name] = repaired
                repairs[name] = records
                continue
        reason = _repair_reason(error)
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        diagnostics.append({"symbol": name, "reason": reason, "detail": str(error)})
    return accepted, overrides, repairs, rejection_counts, diagnostics


def _stack_slot(text: str) -> str | None:
    match = re.search(r"(-?0x[0-9a-f]+)?\(%(rbp|rsp)\)", text)
    return match.group(0) if match else None


def qualify_memory_effect_contract(
    native_report: dict[str, Any], names: list[str]
) -> tuple[list[str], dict[str, Any], dict[str, int]]:
    """Qualify explicit memory effects or exact BOLT FOP dependencies.

    This does not claim whole-machine equivalence.  It establishes that each
    explicit memory instruction is unchanged, or that an original stack load
    is linked to the exact earlier original store used by BOLT's available-
    expression analysis.  Stores and all non-forwarded memory operations must
    remain byte-for-byte identical in the native instruction report.
    """

    wanted = set(names)
    forwarding_by_load = {
        int(item["load_original_address"]): item
        for item in native_report.get("frame_forwardings", [])
    }
    memory_test_by_load = {
        int(item["replacement_original_address"]): item
        for item in native_report.get("arithmetic_fusions", [])
        if item.get("kind") == "x86_dead_load_self_test_to_memory_test"
    }
    trivial_frame_by_consumed_address = {
        int(address): item
        for item in native_report.get("arithmetic_fusions", [])
        if item.get("kind") == "x86_trivial_frame_elimination"
        for address in item.get("consumed_original_addresses", [])
    }
    private_return_spill_by_consumed_address = {
        int(address): item
        for item in native_report.get("arithmetic_fusions", [])
        if item.get("kind") == "x86_private_return_spill_frame_elimination"
        for address in item.get("consumed_original_addresses", [])
    }
    leaf_call_inlines_by_function: dict[int, list[dict[str, Any]]] = {}
    accessor_call_inlines_by_function: dict[int, list[dict[str, Any]]] = {}
    empty_call_eliminations_by_function: dict[int, list[dict[str, Any]]] = {}
    for item in native_report.get("arithmetic_fusions", []):
        if item.get("kind") == "x86_exact_leaf_call_inline":
            leaf_call_inlines_by_function.setdefault(
                int(item["function_original_address"]), []
            ).append(item)
        elif item.get("kind") == "x86_exact_pointer_accessor_call_inline":
            accessor_call_inlines_by_function.setdefault(
                int(item["function_original_address"]), []
            ).append(item)
        elif item.get("kind") == "x86_exact_empty_call_elimination":
            empty_call_eliminations_by_function.setdefault(
                int(item["function_original_address"]), []
            ).append(item)
    global_original = {
        int(item["original_address"]): item
        for function in native_report["functions"]
        for item in function.get("original_instructions", [])
    }
    qualified: list[str] = []
    details: dict[str, Any] = {}
    rejections: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejections[reason] = rejections.get(reason, 0) + 1

    for function in native_report["functions"]:
        name = restored_bolt_name(str(function["name"]))
        if name not in wanted:
            continue
        original = {
            int(item["original_address"]): item
            for item in function.get("original_instructions", [])
        }
        post = {
            int(item["original_address"]): item
            for item in function["instructions"]
            if item["original_address"] is not None
        }
        dependencies = []
        failure: str | None = None

        inline_fusions = leaf_call_inlines_by_function.get(
            int(function["original_address"]), []
        )
        if inline_fusions:
            expected_replacements: dict[int, int] = {}
            consumed_calls: list[int] = []
            for fusion in inline_fusions:
                consumed = [
                    int(value)
                    for value in fusion.get("consumed_original_addresses", [])
                ]
                replacement = int(fusion.get("replacement_original_address", -1))
                call = original.get(consumed[0]) if len(consumed) == 1 else None
                load = global_original.get(replacement)
                if (
                    call is None
                    or not call["is_call"]
                    or call["may_load"]
                    or call["may_store"]
                    or post.get(consumed[0]) is not None
                    or load is None
                    or not load["may_load"]
                    or load["may_store"]
                ):
                    failure = "invalid_exact_leaf_call_inline_ledger"
                    break
                consumed_calls.append(consumed[0])
                expected_replacements[replacement] = (
                    expected_replacements.get(replacement, 0) + 1
                )
            if failure is None:
                for replacement, expected_count in expected_replacements.items():
                    load = global_original[replacement]
                    emitted = [
                        item
                        for item in function["instructions"]
                        if item.get("original_address") == replacement
                    ]
                    if len(emitted) != expected_count or any(
                        (
                            item["text"], item["may_load"], item["may_store"]
                        )
                        != (load["text"], load["may_load"], load["may_store"])
                        for item in emitted
                    ):
                        failure = "invalid_exact_leaf_call_inline_replacement"
                        break
            if failure is None:
                dependencies.append(
                    {
                        "kind": "x86_exact_leaf_call_inline",
                        "consumed_call_original_addresses": consumed_calls,
                        "replacement_original_addresses": sorted(
                            expected_replacements
                        ),
                        "clone_multiplicity_by_original_address": {
                            str(address): count
                            for address, count in sorted(
                                expected_replacements.items()
                            )
                        },
                        "contract": (
                            "System V ABI valid private call stack, no call "
                            "observer and empty patch contract under explicit "
                            "--assume-abi"
                        ),
                    }
                )

        accessor_fusions = accessor_call_inlines_by_function.get(
            int(function["original_address"]), []
        )
        if accessor_fusions and failure is None:
            expected_replacements: dict[int, int] = {}
            consumed_pairs: list[list[int]] = []
            for fusion in accessor_fusions:
                consumed = [
                    int(value)
                    for value in fusion.get("consumed_original_addresses", [])
                ]
                replacements = [
                    int(value)
                    for value in fusion.get("replacement_original_addresses", [])
                ]
                move = original.get(consumed[0]) if len(consumed) == 2 else None
                call = original.get(consumed[1]) if len(consumed) == 2 else None
                load = global_original.get(replacements[0]) \
                    if len(replacements) == 2 else None
                add = global_original.get(replacements[1]) \
                    if len(replacements) == 2 else None
                if (
                    move is None
                    or call is None
                    or move["is_call"]
                    or move["may_load"]
                    or move["may_store"]
                    or re.sub(r"\s+", "", str(move["text"]))
                    != "movq%rax,%rdi"
                    or not call["is_call"]
                    or call["may_load"]
                    or call["may_store"]
                    or post.get(consumed[0]) is not None
                    or post.get(consumed[1]) is not None
                    or load is None
                    or not load["may_load"]
                    or load["may_store"]
                    or re.sub(r"\s+", "", str(load["text"]))
                    != "movq(%rax),%rax"
                    or add is None
                    or add["may_load"]
                    or add["may_store"]
                    or re.sub(r"\s+", "", str(add["text"]))
                    not in {"addq$0x8,%rax", "addq$8,%rax"}
                ):
                    failure = "invalid_exact_accessor_call_inline_ledger"
                    break
                consumed_pairs.append(consumed)
                for replacement in replacements:
                    expected_replacements[replacement] = (
                        expected_replacements.get(replacement, 0) + 1
                    )
            if failure is None:
                for replacement, expected_count in expected_replacements.items():
                    before = global_original[replacement]
                    emitted = [
                        item
                        for item in function["instructions"]
                        if item.get("original_address") == replacement
                    ]
                    if replacement == int(
                        accessor_fusions[0]["replacement_original_address"]
                    ):
                        valid = all(
                            item["may_load"]
                            and not item["may_store"]
                            and re.sub(r"\s+", "", str(item["text"]))
                            == "movq(%rax),%rax"
                            for item in emitted
                        )
                    else:
                        valid = all(
                            (
                                item["text"], item["may_load"], item["may_store"]
                            )
                            == (
                                before["text"], before["may_load"],
                                before["may_store"],
                            )
                            for item in emitted
                        )
                    if len(emitted) != expected_count or not valid:
                        failure = "invalid_exact_accessor_call_inline_replacement"
                        break
            if failure is None:
                dependencies.append(
                    {
                        "kind": "x86_exact_pointer_accessor_call_inline",
                        "consumed_move_call_pairs": consumed_pairs,
                        "replacement_original_addresses": sorted(
                            expected_replacements
                        ),
                        "clone_multiplicity_by_original_address": {
                            str(address): count
                            for address, count in sorted(
                                expected_replacements.items()
                            )
                        },
                        "contract": (
                            "System V ABI valid private call stacks, no call "
                            "observer and empty patch contract under explicit "
                            "--assume-abi"
                        ),
                    }
                )

        empty_fusions = empty_call_eliminations_by_function.get(
            int(function["original_address"]), []
        )
        if empty_fusions and failure is None:
            consumed_calls: list[int] = []
            retained_returns: list[int] = []
            for fusion in empty_fusions:
                consumed = [
                    int(value)
                    for value in fusion.get("consumed_original_addresses", [])
                ]
                retained_return = int(
                    fusion.get("replacement_original_address", -1)
                )
                call = original.get(consumed[0]) if len(consumed) == 1 else None
                callee_return = global_original.get(retained_return)
                if (
                    call is None
                    or not call["is_call"]
                    or call["may_load"]
                    or call["may_store"]
                    or post.get(consumed[0]) is not None
                    or callee_return is None
                    or not callee_return["is_return"]
                    or callee_return["may_load"]
                    or callee_return["may_store"]
                ):
                    failure = "invalid_exact_empty_call_elimination_ledger"
                    break
                consumed_calls.append(consumed[0])
                retained_returns.append(retained_return)
            if failure is None:
                dependencies.append(
                    {
                        "kind": "x86_exact_empty_call_elimination",
                        "consumed_call_original_addresses": consumed_calls,
                        "retained_callee_return_original_addresses": sorted(
                            set(retained_returns)
                        ),
                        "contract": (
                            "System V ABI valid private call and shadow stacks, "
                            "no call observer and empty patch contract under "
                            "explicit --assume-abi"
                        ),
                    }
                )

        allowed_inline_replacements = {
            int(address)
            for dependency in dependencies
            if dependency.get("kind") in {
                "x86_exact_leaf_call_inline",
                "x86_exact_pointer_accessor_call_inline",
            }
            for address in dependency.get("replacement_original_addresses", [])
        }
        if failure is None:
            for address, item in post.items():
                before = original.get(address)
                if before is None and (item["may_load"] or item["may_store"]):
                    if address in allowed_inline_replacements:
                        continue
                    failure = "introduced_unmapped_memory_effect"
                    break

        if failure is None:
            for address, before in original.items():
                if not (before["may_load"] or before["may_store"]):
                    continue
                after = post.get(address)
                if after is not None and (
                    before["text"], before["may_load"], before["may_store"]
                ) == (after["text"], after["may_load"], after["may_store"]):
                    continue
                private_return_spill = (
                    private_return_spill_by_consumed_address.get(address)
                )
                if private_return_spill is not None:
                    consumed = [
                        int(value)
                        for value in private_return_spill.get(
                            "consumed_original_addresses", []
                        )
                    ]
                    consumed_items = [original.get(value) for value in consumed]
                    replacement_address = int(
                        private_return_spill.get(
                            "replacement_original_address", -1
                        )
                    )
                    replacement_before = original.get(replacement_address)
                    replacement_after = post.get(replacement_address)
                    if (
                        len(consumed) != 4
                        or any(item is None for item in consumed_items)
                        or str(consumed_items[0]["text"]).strip()
                        != "pushq\t%rbp"
                        or str(consumed_items[1]["text"]).strip()
                        != "movq\t%rsp, %rbp"
                        or str(consumed_items[2]["text"]).strip()
                        != "movq\t%rax, -0x8(%rbp)"
                        or str(consumed_items[3]["text"]).strip()
                        != "popq\t%rbp"
                        or any(post.get(value) is not None for value in consumed)
                        or replacement_before is None
                        or replacement_after is None
                        or not replacement_before["is_return"]
                        or (
                            replacement_before["text"],
                            replacement_before["may_load"],
                            replacement_before["may_store"],
                        )
                        != (
                            replacement_after["text"],
                            replacement_after["may_load"],
                            replacement_after["may_store"],
                        )
                    ):
                        failure = "invalid_private_return_spill_ledger"
                        break
                    if not any(
                        item.get("kind")
                        == "x86_private_return_spill_frame_elimination"
                        for item in dependencies
                    ):
                        dependencies.append(
                            {
                                "kind": (
                                    "x86_private_return_spill_frame_elimination"
                                ),
                                "consumed_original_addresses": consumed,
                                "retained_return_original_address": (
                                    replacement_address
                                ),
                                "contract": (
                                    "System V ABI private nonescaped local "
                                    "return spill under explicit --assume-abi"
                                ),
                            }
                        )
                    continue
                trivial_frame = trivial_frame_by_consumed_address.get(address)
                if trivial_frame is not None:
                    consumed = [
                        int(value)
                        for value in trivial_frame.get(
                            "consumed_original_addresses", []
                        )
                    ]
                    consumed_items = [original.get(value) for value in consumed]
                    replacement_address = int(
                        trivial_frame.get("replacement_original_address", -1)
                    )
                    replacement_before = original.get(replacement_address)
                    replacement_after = post.get(replacement_address)
                    if (
                        len(consumed) != 3
                        or consumed_items[0] is None
                        or consumed_items[1] is None
                        or consumed_items[2] is None
                        or str(consumed_items[0]["text"]).strip()
                        != "pushq\t%rbp"
                        or str(consumed_items[1]["text"]).strip()
                        != "movq\t%rsp, %rbp"
                        or str(consumed_items[2]["text"]).strip()
                        != "popq\t%rbp"
                        or any(post.get(value) is not None for value in consumed)
                        or replacement_before is None
                        or replacement_after is None
                        or not replacement_before["is_return"]
                        or (
                            replacement_before["text"],
                            replacement_before["may_load"],
                            replacement_before["may_store"],
                        )
                        != (
                            replacement_after["text"],
                            replacement_after["may_load"],
                            replacement_after["may_store"],
                        )
                    ):
                        failure = "invalid_trivial_frame_elimination_ledger"
                        break
                    if not any(
                        item.get("kind") == "x86_trivial_frame_elimination"
                        for item in dependencies
                    ):
                        dependencies.append(
                            {
                                "kind": "x86_trivial_frame_elimination",
                                "consumed_original_addresses": consumed,
                                "retained_return_original_address": (
                                    replacement_address
                                ),
                                "contract": (
                                    "System V ABI call-frame effects under "
                                    "explicit --assume-abi"
                                ),
                            }
                        )
                    continue
                if before["may_store"] or not before["may_load"]:
                    failure = "changed_nonload_or_store_effect"
                    break
                memory_test = memory_test_by_load.get(address)
                if memory_test is not None:
                    consumed = [
                        int(value)
                        for value in memory_test.get(
                            "consumed_original_addresses", []
                        )
                    ]
                    test_address = consumed[1] if len(consumed) == 2 else None
                    original_test = (
                        original.get(test_address)
                        if test_address is not None
                        else None
                    )
                    before_parts = str(before["text"]).strip().split(None, 1)
                    after_parts = str(after["text"]).strip().split(None, 1) \
                        if after is not None else []
                    before_operands = (
                        before_parts[1].rsplit(",", 1)
                        if len(before_parts) == 2 else []
                    )
                    after_operands = (
                        after_parts[1].split(",", 1)
                        if len(after_parts) == 2 else []
                    )
                    original_test_text = (
                        str(original_test["text"]).strip()
                        if original_test is not None else ""
                    )
                    if (
                        after is None
                        or not after["may_load"]
                        or after["may_store"]
                        or after_parts[0] != "testq"
                        or len(after_operands) != 2
                        or after_operands[0].strip() not in {"$-0x1", "$-1"}
                        or len(before_operands) != 2
                        or before_operands[0].strip()
                        != after_operands[1].strip()
                        or original_test is None
                        or not re.fullmatch(
                            r"testq\s+%([a-z0-9]+),\s*%\1",
                            original_test_text,
                        )
                        or post.get(test_address) is not None
                    ):
                        failure = "invalid_memory_test_fusion_ledger"
                        break
                    dependencies.append(
                        {
                            "kind": "x86_dead_load_self_test_to_memory_test",
                            "load_original_address": address,
                            "test_original_address": test_address,
                            "memory_operand": before_operands[0].strip(),
                            "replacement_original_address": address,
                        }
                    )
                    continue
                slot = _stack_slot(str(before["text"]))
                edge = forwarding_by_load.get(address)
                if slot is None or edge is None:
                    failure = "changed_load_without_fop_dependency"
                    break
                source_address = int(edge["store_original_address"])
                source_before = original.get(source_address)
                source_after = post.get(source_address)
                source_private_elimination = (
                    private_return_spill_by_consumed_address.get(source_address)
                )
                source_eliminated_by_same_private_contract = bool(
                    source_after is None
                    and source_private_elimination is not None
                    and int(
                        source_private_elimination.get(
                            "function_original_address", -1
                        )
                    )
                    == int(function["original_address"])
                    and any(
                        item.get("kind")
                        == "x86_private_return_spill_frame_elimination"
                        for item in dependencies
                    )
                )
                if (
                    source_before is None
                    or (
                        source_after is None
                        and not source_eliminated_by_same_private_contract
                    )
                    or source_address >= address
                    or not source_before["may_store"]
                    or _stack_slot(str(source_before["text"])) != slot
                    or (source_after is not None and (
                        source_before["text"],
                        source_before["may_load"],
                        source_before["may_store"],
                    )
                    != (
                        source_after["text"],
                        source_after["may_load"],
                        source_after["may_store"],
                    ))
                ):
                    failure = "invalid_or_changed_fop_source_store"
                    break
                if bool(edge["replacement_deleted"]) != (after is None):
                    failure = "fop_deletion_ledger_mismatch"
                    break
                if after is not None and (after["may_load"] or after["may_store"]):
                    failure = "fop_replacement_remains_memory_effect"
                    break
                intervening_stores = [
                    candidate_address
                    for candidate_address, candidate in original.items()
                    if source_address < candidate_address < address
                    and candidate["may_store"]
                ]
                if intervening_stores:
                    failure = "fop_dependency_crosses_intervening_store"
                    break
                dependencies.append(
                    {
                        "load_original_address": address,
                        "store_original_address": source_address,
                        "stack_slot": slot,
                        "replacement_deleted": bool(edge["replacement_deleted"]),
                        "source_store_eliminated_by_private_contract": (
                            source_eliminated_by_same_private_contract
                        ),
                    }
                )

        if failure is not None:
            reject(failure)
            continue
        classification = (
            "exact_explicit_memory_sequence" if not dependencies
            else (
                "checked_exact_accessor_chain_inline"
                if any(
                    item.get("kind")
                    == "x86_exact_pointer_accessor_call_inline"
                    for item in dependencies
                )
                else "checked_exact_empty_call_elimination"
                if any(
                    item.get("kind") == "x86_exact_empty_call_elimination"
                    for item in dependencies
                )
                else "checked_exact_leaf_call_inline"
                if any(
                    item.get("kind") == "x86_exact_leaf_call_inline"
                    for item in dependencies
                )
                else "checked_private_return_spill_elimination"
                if any(
                    item.get("kind")
                    == "x86_private_return_spill_frame_elimination"
                    for item in dependencies
                )
                else "checked_trivial_frame_elimination"
                if any(
                    item.get("kind") == "x86_trivial_frame_elimination"
                    for item in dependencies
                )
                else "checked_memory_test_fusion"
                if any(
                    item.get("kind")
                    == "x86_dead_load_self_test_to_memory_test"
                    for item in dependencies
                )
                else "bolt_fop_store_to_load_dependency"
            )
        )
        qualified.append(name)
        details[name] = {
            "classification": classification,
            "forwarding_dependencies": dependencies,
            "forwarding_dependency_count": len(dependencies),
        }
    return qualified, details, rejections


def emit_compact_selected(
    reference: Path,
    bolt_candidate: Path,
    output: Path,
    names: tuple[str, ...],
    body_overrides: dict[str, bytes] | None = None,
) -> list[dict[str, Any]]:
    """Adopt exact BOLT bodies in place for bounded reference-complete regions."""

    image = bytearray(reference.read_bytes())
    edits = []
    for name in names:
        original = symbol_record(reference, name)
        optimized = symbol_record(bolt_candidate, name)
        if original is None or optimized is None:
            raise RuntimeError(f"cannot import BOLT body for {name}")
        original_bytes = bytes(original["bytes"])
        optimized_bytes = (
            body_overrides[name]
            if body_overrides is not None and name in body_overrides
            else bytes(optimized["bytes"])
        )
        if len(optimized_bytes) > len(original_bytes):
            raise RuntimeError(f"BOLT body for {name} does not fit the original range")
        protected_prefix = original_bytes[:4] == bytes.fromhex("f30f1efa")
        if protected_prefix and optimized_bytes[:4] != original_bytes[:4]:
            raise RuntimeError(f"BOLT body for {name} changed the protected entry")
        start = int(original["file_offset"])
        capacity = len(original_bytes)
        padding = bytes.fromhex("90") * (capacity - len(optimized_bytes))
        image[start : start + capacity] = optimized_bytes + padding
        edits.append(
            {
                "symbol": name,
                "original_address": original["address"],
                "bolt_output_address": optimized["address"],
                "file_offset": start,
                "qualified_capacity_bytes": capacity,
                "live_bolt_bytes": len(optimized_bytes),
                "padding_bytes": len(padding),
                "protected_prefix_preserved": protected_prefix,
                "original_body_sha256": hashlib.sha256(original_bytes).hexdigest(),
                "bolt_body_sha256": hashlib.sha256(optimized_bytes).hexdigest(),
                "origin_scope": "function identity; per-instruction mapping remains a gate",
            }
        )
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_bytes(image)
    temporary.chmod(reference.stat().st_mode)
    os.replace(temporary, output)
    return edits


def protected_symbols() -> list[str]:
    symbols = set()
    with (RERUN / "candidate-universe.csv").open(newline="") as stream:
        for row in csv.DictReader(stream):
            if int(row.get("ProtectedPrefixBytes") or 0) != 4:
                continue
            region = json.loads((Path(row["CanonicalPath"]) / "region.json").read_text())
            symbols.add(str(region["ActualSymbol"]))
    return sorted(symbols)


def observe(path: Path, seconds: float = 1.0) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["LD_PRELOAD"] = str(PRELOAD)
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            [str(path)], stdout=output, stderr=subprocess.STDOUT,
            env=environment, start_new_session=True,
        )
        time.sleep(seconds)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=5)
        output.seek(0)
        content = output.read()
    return {
        "returncode": process.returncode,
        "dispatch_markers": content.count(EVENT),
        "output_bytes": len(content),
        "seconds": seconds,
    }


def observe_fixed_work(path: Path, work_units: int = 100) -> dict[str, Any]:
    """Run the existing closed-loop SmartSpeaker completion oracle.

    Unlike the historical one-second smoke, this supplies exactly ten
    acknowledged ticks per completed interrupt-task marker and stops after a
    fixed amount of work. It is an acceptance oracle, not a timing sample.
    """

    environment = dict(os.environ)
    environment.update({
        "LD_PRELOAD": str(FIXED_PRELOAD),
        "FASTDYN_FIXED_WORK_UNITS": str(work_units),
        "FASTDYN_TICK_COUNTER_ADDRESS": "0x495670",
        "FASTDYN_MARKER_SETTLE_NS": "100000",
    })
    result = subprocess.run(
        [str(path)], env=environment, capture_output=True,
        timeout=30, check=False,
    )
    match = FIXED_DRIVER_RE.search(result.stderr)
    driver = None
    if match is not None:
        driver = {
            "start_tick": int(match.group(1)),
            "end_tick": int(match.group(2)),
            "completed_ticks": int(match.group(3)),
            "start_markers": int(match.group(4)),
            "end_markers": int(match.group(5)),
            "completed_units": int(match.group(6)),
            "driver_cpu_ns": int(match.group(7)),
        }
    trace = [
        line for line in result.stdout.splitlines()
        if line in INTERRUPT_EVENTS
    ]
    prefix = trace[:400]
    ok = bool(
        result.returncode == 2
        and driver is not None
        and driver["completed_units"] == work_units
        and driver["completed_ticks"] == work_units * 10
        and driver["end_tick"] - driver["start_tick"]
        == driver["completed_ticks"]
        and driver["end_markers"] - driver["start_markers"] == work_units
        and result.stdout.count(EVENT) == work_units + 1
        and len(prefix) == min(400, len(trace))
        and len(prefix) > 0
    )
    return {
        "ok": ok,
        "returncode": result.returncode,
        "work_units": work_units,
        "completed_units": driver["completed_units"] if driver else None,
        "completed_ticks": driver["completed_ticks"] if driver else None,
        "dispatch_markers": result.stdout.count(EVENT),
        "interrupt_trace_events": len(trace),
        "interrupt_trace_prefix_events": len(prefix),
        "interrupt_trace_prefix_sha256": hashlib.sha256(
            b"\n".join(prefix) + b"\n"
        ).hexdigest(),
        "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
    }


def fixed_work_contract(observation: dict[str, Any]) -> tuple[Any, ...]:
    return (
        observation["returncode"],
        observation["completed_units"],
        observation["completed_ticks"],
        observation["dispatch_markers"],
        observation["interrupt_trace_prefix_events"],
        observation["interrupt_trace_prefix_sha256"],
    )


def parse_fop(stdout: str) -> dict[str, int]:
    optimized = re.search(
        r"FOP optimized (\d+) redundant load\(s\) and (\d+) unused store\(s\)", stdout
    )
    deleted = re.search(
        r"FOP deleted (\d+) load\(s\) \(dyn count: (\d+)\) and (\d+) store\(s\)",
        stdout,
    )
    if not optimized or not deleted:
        raise RuntimeError("could not parse BOLT FOP diagnostics")
    return {
        "optimized_redundant_loads": int(optimized.group(1)),
        "optimized_unused_stores": int(optimized.group(2)),
        "deleted_loads": int(deleted.group(1)),
        "deleted_load_dynamic_count": int(deleted.group(2)),
        "deleted_stores": int(deleted.group(3)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bolt", type=Path, default=DEFAULT_BOLT)
    parser.add_argument("--bat-dump", type=Path, default=DEFAULT_BAT_DUMP)
    parser.add_argument("--max-region-bytes", type=int, default=256)
    parser.add_argument(
        "--profile", type=Path,
        help="override the retained BOLT profile while preserving all other arms",
    )
    parser.add_argument(
        "--arithmetic-strength-reduction", action="store_true",
        help="enable the checked native BOLT x86 arithmetic-folding pass",
    )
    parser.add_argument(
        "--copy-cleanup", action="store_true",
        help="enable checked inverse-copy cleanup after BOLT frame optimization",
    )
    parser.add_argument(
        "--copy-cleanup-hot-only", action="store_true",
        help="emit checked copy cleanup only in profile-observed blocks",
    )
    parser.add_argument(
        "--copy-cleanup-adjacent-only", action="store_true",
        help="limit copy equality to immediately adjacent register moves",
    )
    parser.add_argument(
        "--memory-test-folding", action="store_true",
        help="fold checked dead x86-64 load/self-test pairs",
    )
    parser.add_argument(
        "--trivial-frame-elimination", action="store_true",
        help="remove exact empty x86-64 frames under an explicit ABI contract",
    )
    parser.add_argument(
        "--exact-leaf-call-inlining", action="store_true",
        help="replace observed calls to exact qualified leaf loads",
    )
    parser.add_argument(
        "--exact-accessor-call-fusion", action="store_true",
        help="fuse observed calls to the exact qualified pointer accessor",
    )
    parser.add_argument(
        "--exact-empty-call-elimination", action="store_true",
        help="remove observed calls to exact qualified empty leaves",
    )
    parser.add_argument(
        "--layout", choices=("on", "off"), default="on",
        help=(
            "select the retained trained BOLT layout policy or keep original "
            "function placement for contiguous compact import"
        ),
    )
    parser.add_argument(
        "--inline-all", action="store_true",
        help=(
            "enable BOLT's existing bounded inline-all policy after removing "
            "the retained no-inline control"
        ),
    )
    parser.add_argument(
        "--bolt-extra-arg", action="append", default=[],
        help="append one frozen BOLT option for a bounded policy experiment",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if args.max_region_bytes <= 0:
        raise SystemExit("--max-region-bytes must be positive")
    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    options = json.loads((RERUN / "bolt-arm-options.json").read_text())
    reference = Path(options["reference"]["path"]).resolve()
    profile = (
        args.profile.resolve() if args.profile is not None
        else Path(options["profile"]).resolve()
    )
    candidate = output / "SmartSpeaker.bolt-firmware-candidate"
    native_report_path = output / "bolt-post-optimization-report.json"
    common_options = list(options["common"])
    if args.inline_all:
        common_options = [item for item in common_options if item != "--no-inline"]
    command = [
        str(args.bolt.resolve()), str(reference),
        f"--data={profile}", "--frame-opt=all",
        *common_options, *options[f"layout_{args.layout}"],
        "--enable-bat",
        f"--fastdyn-firmware-report={native_report_path}",
        "-o", str(candidate),
    ]
    if args.inline_all:
        command[2:2] = ["--inline-all", "--inline-limit=1000", "--inline-ap"]
    if args.arithmetic_strength_reduction:
        command.insert(-2, "--fastdyn-arithmetic-strength-reduction")
    if args.copy_cleanup:
        command.insert(-2, "--fastdyn-copy-cleanup")
    if args.copy_cleanup_hot_only:
        if not args.copy_cleanup:
            raise SystemExit("--copy-cleanup-hot-only requires --copy-cleanup")
        command.insert(-2, "--fastdyn-copy-cleanup-hot-only")
    if args.copy_cleanup_adjacent_only:
        if not args.copy_cleanup:
            raise SystemExit("--copy-cleanup-adjacent-only requires --copy-cleanup")
        command.insert(-2, "--fastdyn-copy-cleanup-adjacent-only")
    if args.memory_test_folding:
        command.insert(-2, "--fastdyn-memory-test-folding")
    if args.trivial_frame_elimination:
        if "--assume-abi" not in args.bolt_extra_arg:
            raise SystemExit(
                "--trivial-frame-elimination requires "
                "--bolt-extra-arg=--assume-abi"
            )
        command.insert(-2, "--fastdyn-trivial-frame-elimination")
    if args.exact_leaf_call_inlining:
        if not args.trivial_frame_elimination:
            raise SystemExit(
                "--exact-leaf-call-inlining requires "
                "--trivial-frame-elimination"
            )
        if "--assume-abi" not in args.bolt_extra_arg:
            raise SystemExit(
                "--exact-leaf-call-inlining requires "
                "--bolt-extra-arg=--assume-abi"
            )
        command.insert(-2, "--fastdyn-exact-leaf-call-inlining")
    if args.exact_accessor_call_fusion:
        if not args.exact_leaf_call_inlining:
            raise SystemExit(
                "--exact-accessor-call-fusion requires "
                "--exact-leaf-call-inlining"
            )
        if "--assume-abi" not in args.bolt_extra_arg:
            raise SystemExit(
                "--exact-accessor-call-fusion requires "
                "--bolt-extra-arg=--assume-abi"
            )
        command.insert(-2, "--fastdyn-exact-accessor-call-fusion")
    if args.exact_empty_call_elimination:
        if not args.trivial_frame_elimination:
            raise SystemExit(
                "--exact-empty-call-elimination requires "
                "--trivial-frame-elimination"
            )
        if "--assume-abi" not in args.bolt_extra_arg:
            raise SystemExit(
                "--exact-empty-call-elimination requires "
                "--bolt-extra-arg=--assume-abi"
            )
        command.insert(-2, "--fastdyn-exact-empty-call-elimination")
    for extra in args.bolt_extra_arg:
        if not extra.startswith("--"):
            raise SystemExit("--bolt-extra-arg values must be long BOLT options")
        command.insert(-2, extra)
    run = subprocess.run(command, capture_output=True, text=True)
    (output / "bolt.stdout").write_text(run.stdout)
    (output / "bolt.stderr").write_text(run.stderr)
    write_json(output / "command.json", {"argv": command, "returncode": run.returncode})
    if run.returncode != 0:
        raise SystemExit(f"BOLT failed; see {output / 'bolt.stderr'}")
    native_report = json.loads(native_report_path.read_text())
    if native_report.get("phase") != "post-frame-optimizer-and-alloc-combiner":
        raise RuntimeError("native BOLT report is not post-optimization")
    bat = BatIdentityResolver(args.bat_dump, candidate)
    selected_names = ("prvGetThreadFromTask", "vListInitialiseItem")
    selected_native = {}
    for name in selected_names:
        matches = [
            item for item in native_report["functions"]
            if str(item["name"]).startswith(name)
        ]
        if len(matches) != 1:
            raise RuntimeError(f"native report did not identify {name} exactly once")
        selected_native[name] = matches[0]

    reference_inventory = elf_inventory(reference)
    candidate_inventory = elf_inventory(candidate)
    protected = []
    for name in protected_symbols():
        located = symbol_bytes(candidate, name)
        prefix = located[1][:4].hex() if located else None
        protected.append(
            {
                "symbol": name,
                "status": "preserved" if prefix == "f30f1efa" else "missing_or_changed",
                "output_address": located[0] if located else None,
                "prefix": prefix,
            }
        )

    accessor = symbol_bytes(candidate, "prvGetThreadFromTask")
    expected_body = bytes.fromhex("554889e548897de84889f8488b00488945f84883c0085dc3")
    expected_address_fold_body = bytes.fromhex(
        "554889e548897de8488b07488945f84883c0085dc3"
    )
    accessor_variant = None
    if accessor and accessor[1][: len(expected_body)] == expected_body:
        accessor_variant = "ordinary_fop"
    elif (
        accessor
        and accessor[1][: len(expected_address_fold_body)]
        == expected_address_fold_body
        and any(
            item.get("kind")
            == "x86_mov64_copy_folded_into_zero_disp_load"
            and int(item["function_original_address"])
            == int(selected_native["prvGetThreadFromTask"]["original_address"])
            for item in native_report.get("copy_cleanups", [])
        )
    ):
        accessor_variant = "ordinary_fop_plus_checked_address_fold"
    accessor_match = accessor_variant is not None
    observations = {
        "reference": observe(reference),
        "candidate": observe(candidate),
    }
    compact_candidate = output / "SmartSpeaker.bolt-selected-compact"
    compact_edits = emit_compact_selected(
        reference,
        candidate,
        compact_candidate,
        selected_names,
    )
    for edit in compact_edits:
        native_function = selected_native[edit["symbol"]]
        mappings = [
            {
                "estimated_output_offset": item["estimated_output_offset"],
                "original_address": item["original_address"],
                "text": item["text"],
            }
            for item in native_function["instructions"]
        ]
        edit["origin_mappings"] = mappings
        edit["per_instruction_origin_complete"] = all(
            item["original_address"] is not None for item in mappings
        )
    compact_inventory = elf_inventory(compact_candidate)
    compact_observation = observe(compact_candidate)
    automatic_names, automatic_rejections = automatic_compact_cohort(
        native_report, reference, candidate, args.max_region_bytes
    )
    automatic_candidate = output / "SmartSpeaker.bolt-auto-compact"
    automatic_edits = emit_compact_selected(
        reference, candidate, automatic_candidate, tuple(automatic_names)
    )
    native_by_restored_name = {
        restored_bolt_name(str(item["name"])): item
        for item in native_report["functions"]
    }
    for edit in automatic_edits:
        function = native_by_restored_name[edit["symbol"]]
        edit["origin_mappings"] = [
            {
                "estimated_output_offset": item["estimated_output_offset"],
                "original_address": item["original_address"],
                "text": item["text"],
            }
            for item in function["instructions"]
        ]
        edit["per_instruction_origin_complete"] = True
    automatic_inventory = elf_inventory(automatic_candidate)
    automatic_observation = observe(automatic_candidate)
    base_qualified_names, base_qualification_details, base_qualification_rejections = (
        qualify_memory_effect_contract(native_report, automatic_names)
    )
    direct_call_names, direct_call_rejections = direct_call_compact_cohort(
        native_report, reference, candidate, args.max_region_bytes
    )
    (
        direct_qualified_names,
        direct_qualification_details,
        direct_qualification_rejections,
    ) = qualify_memory_effect_contract(native_report, direct_call_names)
    (
        direct_qualified_names,
        direct_call_overrides,
        direct_call_repairs,
        direct_repair_rejections,
        direct_repair_diagnostics,
    ) = repair_direct_calls_bounded(
        native_report, reference, candidate, direct_qualified_names, bat
    )
    (
        internal_branch_names,
        internal_branch_records,
        internal_branch_rejections,
    ) = internal_branch_compact_cohort(
        native_report, reference, candidate, args.max_region_bytes
    )
    (
        internal_branch_qualified_names,
        internal_branch_qualification_details,
        internal_branch_qualification_rejections,
    ) = qualify_memory_effect_contract(native_report, internal_branch_names)
    internal_branch_records = {
        name: internal_branch_records[name]
        for name in internal_branch_qualified_names
    }
    rip_relative_names, rip_relative_rejections = rip_relative_compact_cohort(
        native_report, reference, candidate, args.max_region_bytes
    )
    (
        rip_relative_qualified_names,
        rip_relative_qualification_details,
        rip_relative_qualification_rejections,
    ) = qualify_memory_effect_contract(native_report, rip_relative_names)
    (
        rip_relative_qualified_names,
        rip_relative_overrides,
        rip_relative_repairs,
        rip_repair_rejections,
        rip_repair_diagnostics,
    ) = repair_rip_relative_bounded(
        native_report, reference, candidate, rip_relative_qualified_names, bat
    )
    call_rip_names, call_rip_rejections = call_rip_compact_cohort(
        native_report, reference, candidate, args.max_region_bytes
    )
    (
        call_rip_qualified_names,
        call_rip_qualification_details,
        call_rip_qualification_rejections,
    ) = qualify_memory_effect_contract(native_report, call_rip_names)
    (
        call_rip_call_qualified_names,
        call_rip_direct_overrides,
        call_rip_call_repairs,
        call_rip_call_repair_rejections,
        call_rip_call_repair_diagnostics,
    ) = repair_direct_calls_bounded(
        native_report, reference, candidate, call_rip_qualified_names, bat
    )
    (
        call_rip_qualified_names,
        call_rip_overrides,
        call_rip_data_repairs,
        call_rip_data_repair_rejections,
        call_rip_data_repair_diagnostics,
    ) = repair_rip_relative_bounded(
        native_report,
        reference,
        candidate,
        call_rip_call_qualified_names,
        bat,
        body_overrides=call_rip_direct_overrides,
    )
    call_rip_call_repairs = {
        name: call_rip_call_repairs[name] for name in call_rip_qualified_names
    }
    leaf_inline_names, leaf_inline_rejections = (
        exact_leaf_call_inline_compact_cohort(
            native_report, reference, candidate, args.max_region_bytes
        )
    )
    (
        leaf_inline_qualified_names,
        leaf_inline_qualification_details,
        leaf_inline_qualification_rejections,
    ) = qualify_memory_effect_contract(native_report, leaf_inline_names)
    (
        leaf_inline_call_qualified_names,
        leaf_inline_direct_overrides,
        leaf_inline_call_repairs,
        leaf_inline_call_repair_rejections,
        leaf_inline_call_repair_diagnostics,
    ) = repair_direct_calls_bounded(
        native_report,
        reference,
        candidate,
        leaf_inline_qualified_names,
        bat,
    )
    (
        leaf_inline_qualified_names,
        leaf_inline_overrides,
        leaf_inline_data_repairs,
        leaf_inline_data_repair_rejections,
        leaf_inline_data_repair_diagnostics,
    ) = repair_rip_relative_bounded(
        native_report,
        reference,
        candidate,
        leaf_inline_call_qualified_names,
        bat,
        body_overrides=leaf_inline_direct_overrides,
    )
    leaf_inline_call_repairs = {
        name: leaf_inline_call_repairs[name]
        for name in leaf_inline_qualified_names
    }
    previously_qualified_names = set(
        base_qualified_names
        + direct_qualified_names
        + internal_branch_qualified_names
        + rip_relative_qualified_names
        + call_rip_qualified_names
        + leaf_inline_qualified_names
    )
    (
        branch_heavy_names,
        branch_heavy_records,
        branch_heavy_rejections,
        branch_heavy_rejection_diagnostics,
    ) = branch_heavy_compact_cohort(
        native_report,
        reference,
        candidate,
        previously_qualified_names,
        args.max_region_bytes,
        bat,
    )
    (
        branch_heavy_qualified_names,
        branch_heavy_qualification_details,
        branch_heavy_qualification_rejections,
    ) = qualify_memory_effect_contract(native_report, branch_heavy_names)
    branch_heavy_records = {
        name: branch_heavy_records[name]
        for name in branch_heavy_qualified_names
    }
    branch_heavy_call_names = [
        name
        for name in branch_heavy_qualified_names
        if any(item["is_call"] for item in native_by_restored_name[name]["instructions"])
    ]
    branch_heavy_rip_names = [
        name
        for name in branch_heavy_qualified_names
        if any("%rip" in item["text"] for item in native_by_restored_name[name]["instructions"])
    ]
    (
        branch_heavy_call_accepted,
        branch_heavy_direct_overrides,
        branch_heavy_call_repairs,
        branch_heavy_call_repair_rejections,
        branch_heavy_call_repair_diagnostics,
    ) = repair_direct_calls_bounded(
        native_report, reference, candidate, branch_heavy_call_names, bat
    )
    branch_heavy_call_failed = set(branch_heavy_call_names) - set(
        branch_heavy_call_accepted
    )
    branch_heavy_after_calls = [
        name
        for name in branch_heavy_qualified_names
        if name not in branch_heavy_call_failed
    ]
    branch_heavy_rip_names = [
        name for name in branch_heavy_rip_names if name in branch_heavy_after_calls
    ]
    (
        branch_heavy_rip_accepted,
        branch_heavy_rip_overrides,
        branch_heavy_data_repairs,
        branch_heavy_data_repair_rejections,
        branch_heavy_data_repair_diagnostics,
    ) = repair_rip_relative_bounded(
        native_report,
        reference,
        candidate,
        branch_heavy_rip_names,
        bat,
        body_overrides=branch_heavy_direct_overrides,
    )
    branch_heavy_rip_failed = set(branch_heavy_rip_names) - set(
        branch_heavy_rip_accepted
    )
    branch_heavy_qualified_names = [
        name
        for name in branch_heavy_after_calls
        if name not in branch_heavy_rip_failed
    ]
    branch_heavy_records = {
        name: branch_heavy_records[name]
        for name in branch_heavy_qualified_names
    }
    branch_heavy_call_repairs = {
        name: branch_heavy_call_repairs[name]
        for name in branch_heavy_qualified_names
        if name in branch_heavy_call_repairs
    }
    branch_heavy_overrides = {
        **branch_heavy_direct_overrides,
        **branch_heavy_rip_overrides,
    }
    all_direct_call_repairs = {
        **direct_call_repairs,
        **call_rip_call_repairs,
        **leaf_inline_call_repairs,
        **branch_heavy_call_repairs,
    }
    all_rip_relative_repairs = {
        **rip_relative_repairs,
        **call_rip_data_repairs,
        **leaf_inline_data_repairs,
        **branch_heavy_data_repairs,
    }
    all_internal_branch_records = {
        **internal_branch_records,
        **branch_heavy_records,
    }
    qualified_names = (
        base_qualified_names
        + direct_qualified_names
        + internal_branch_qualified_names
        + rip_relative_qualified_names
        + call_rip_qualified_names
        + leaf_inline_qualified_names
        + branch_heavy_qualified_names
    )
    qualification_details = {
        **base_qualification_details,
        **direct_qualification_details,
        **internal_branch_qualification_details,
        **rip_relative_qualification_details,
        **call_rip_qualification_details,
        **leaf_inline_qualification_details,
        **branch_heavy_qualification_details,
    }
    qualification_rejections = {
        f"base:{key}": value
        for key, value in base_qualification_rejections.items()
    }
    qualification_rejections.update(
        {
            f"direct_call:{key}": value
            for key, value in direct_qualification_rejections.items()
        }
    )
    qualification_rejections.update(
        {
            f"branch_heavy:{key}": value
            for key, value in branch_heavy_qualification_rejections.items()
        }
    )
    qualification_rejections.update(
        {
            f"rip_relative:{key}": value
            for key, value in rip_relative_qualification_rejections.items()
        }
    )
    qualification_rejections.update(
        {
            f"call_rip:{key}": value
            for key, value in call_rip_qualification_rejections.items()
        }
    )
    qualification_rejections.update(
        {
            f"leaf_inline:{key}": value
            for key, value in leaf_inline_qualification_rejections.items()
        }
    )
    for prefix, counts in (
        ("direct_call_repair", direct_repair_rejections),
        ("rip_relative_repair", rip_repair_rejections),
        ("call_rip_call_repair", call_rip_call_repair_rejections),
        ("call_rip_data_repair", call_rip_data_repair_rejections),
        ("leaf_inline_call_repair", leaf_inline_call_repair_rejections),
        ("leaf_inline_data_repair", leaf_inline_data_repair_rejections),
        ("branch_heavy_call_repair", branch_heavy_call_repair_rejections),
        ("branch_heavy_data_repair", branch_heavy_data_repair_rejections),
    ):
        qualification_rejections.update(
            {f"{prefix}:{key}": value for key, value in counts.items()}
        )
    body_overrides = {
        **direct_call_overrides,
        **rip_relative_overrides,
        **call_rip_overrides,
        **leaf_inline_overrides,
        **branch_heavy_overrides,
    }
    absolute_reference_names = [
        name
        for name in qualified_names
        if any(
            reference_item.get("kind") == "absolute_immediate"
            for instruction in native_by_restored_name[name]["instructions"]
            for reference_item in instruction.get("symbol_references", [])
        )
    ]
    (
        absolute_reference_accepted,
        absolute_reference_overrides,
        absolute_reference_repairs,
        absolute_reference_rejections,
        absolute_reference_diagnostics,
    ) = repair_absolute_symbol_references_bounded(
        native_report,
        reference,
        candidate,
        absolute_reference_names,
        bat,
        body_overrides,
    )
    absolute_reference_failed = set(absolute_reference_names) - set(
        absolute_reference_accepted
    )
    qualified_names = [
        name for name in qualified_names if name not in absolute_reference_failed
    ]
    accepted_name_set = set(qualified_names)
    base_qualified_names = [n for n in base_qualified_names if n in accepted_name_set]
    direct_qualified_names = [n for n in direct_qualified_names if n in accepted_name_set]
    internal_branch_qualified_names = [
        n for n in internal_branch_qualified_names if n in accepted_name_set
    ]
    rip_relative_qualified_names = [
        n for n in rip_relative_qualified_names if n in accepted_name_set
    ]
    call_rip_qualified_names = [
        n for n in call_rip_qualified_names if n in accepted_name_set
    ]
    leaf_inline_qualified_names = [
        n for n in leaf_inline_qualified_names if n in accepted_name_set
    ]
    branch_heavy_qualified_names = [
        n for n in branch_heavy_qualified_names if n in accepted_name_set
    ]
    all_direct_call_repairs = {
        name: records
        for name, records in all_direct_call_repairs.items()
        if name in accepted_name_set
    }
    all_rip_relative_repairs = {
        name: records
        for name, records in all_rip_relative_repairs.items()
        if name in accepted_name_set
    }
    all_internal_branch_records = {
        name: records
        for name, records in all_internal_branch_records.items()
        if name in accepted_name_set
    }
    branch_heavy_records = {
        name: records
        for name, records in branch_heavy_records.items()
        if name in accepted_name_set
    }
    branch_heavy_call_repairs = {
        name: records
        for name, records in branch_heavy_call_repairs.items()
        if name in accepted_name_set
    }
    branch_heavy_data_repairs = {
        name: records
        for name, records in branch_heavy_data_repairs.items()
        if name in accepted_name_set
    }
    body_overrides.update(absolute_reference_overrides)
    qualification_rejections.update(
        {
            f"absolute_symbol_repair:{key}": value
            for key, value in absolute_reference_rejections.items()
        }
    )
    qualification_rejections.update(
        {
            f"internal_branch:{key}": value
            for key, value in internal_branch_qualification_rejections.items()
        }
    )
    qualified_candidate = output / "SmartSpeaker.bolt-qualified-compact"
    qualified_edits = emit_compact_selected(
        reference,
        candidate,
        qualified_candidate,
        tuple(qualified_names),
        body_overrides=body_overrides,
    )
    for edit in qualified_edits:
        edit.update(qualification_details[edit["symbol"]])
        if edit["symbol"] in all_direct_call_repairs:
            edit["reference_repairs"] = all_direct_call_repairs[edit["symbol"]]
            edit["reference_repair_class"] = "x86_direct_call_rel32"
        else:
            edit["reference_repairs"] = []
            edit["reference_repair_class"] = "none"
        if edit["symbol"] in all_internal_branch_records:
            edit["branch_identity_records"] = all_internal_branch_records[edit["symbol"]]
            edit["branch_reference_class"] = "position_invariant_internal_relative"
        else:
            edit["branch_identity_records"] = []
            edit["branch_reference_class"] = "none"
        if edit["symbol"] in all_rip_relative_repairs:
            edit["data_reference_repairs"] = all_rip_relative_repairs[edit["symbol"]]
            edit["data_reference_class"] = "x86_rip_relative_exact_original_target"
        else:
            edit["data_reference_repairs"] = []
            edit["data_reference_class"] = "none"
        if edit["symbol"] in absolute_reference_repairs:
            edit["absolute_reference_repairs"] = absolute_reference_repairs[
                edit["symbol"]
            ]
            edit["absolute_reference_class"] = (
                "x86_absolute_immediate_exact_original_target"
            )
        else:
            edit["absolute_reference_repairs"] = []
            edit["absolute_reference_class"] = "none"
    qualified_inventory = elf_inventory(qualified_candidate)
    qualified_observation = observe(qualified_candidate)
    hot_qualified_names = [
        name
        for name in qualified_names
        if int(native_by_restored_name[name].get("known_execution_count", 0)) > 0
    ]
    hot_qualified_candidate = output / "SmartSpeaker.bolt-hot-qualified-compact"
    hot_qualified_edits = emit_compact_selected(
        reference,
        candidate,
        hot_qualified_candidate,
        tuple(hot_qualified_names),
        body_overrides=body_overrides,
    )
    hot_qualified_inventory = elf_inventory(hot_qualified_candidate)
    hot_qualified_observation = observe(hot_qualified_candidate)
    fixed_work_observations = {
        "reference": observe_fixed_work(reference),
        "candidate": observe_fixed_work(candidate),
        "compact_selected": observe_fixed_work(compact_candidate),
        "automatic_compact": observe_fixed_work(automatic_candidate),
        "qualified_compact": observe_fixed_work(qualified_candidate),
        "hot_qualified_compact": observe_fixed_work(hot_qualified_candidate),
    }
    prototype_outcomes_path = PROTOTYPE / "pass-outcomes.json"
    prototype_outcomes = json.loads(prototype_outcomes_path.read_text())
    prototype_baseline = {
        "classification": "custom_bounded_prototype_baseline_not_production_optimizer",
        "artifact": str(prototype_outcomes_path.relative_to(ROOT)),
        "artifact_sha256": sha256(prototype_outcomes_path),
        "outcomes": [
            {
                "unit": item["unit"],
                "pass_id": item["pass_id"],
                "status": item["status"],
                "reason_code": item["reason_code"],
                "live_replacement_bytes": item["emission"].get("live_replacement_bytes"),
                "qualified_capacity_bytes": item["emission"].get("qualified_capacity_bytes"),
            }
            for item in prototype_outcomes["outcomes"]
        ],
    }
    write_json(output / "custom-pass-baseline.json", prototype_baseline)

    failures = []
    if reference_inventory["machine"] != candidate_inventory["machine"]:
        failures.append("elf_machine_changed")
    if not candidate_inventory["has_bat"]:
        failures.append("bolt_address_translation_missing")
    if any(item["status"] != "preserved" for item in protected):
        failures.append("protected_entry_changed")
    if not accessor_match:
        failures.append("accessor_does_not_match_measured_bolt_fop_body")
    if compact_inventory["file_bytes"] != reference_inventory["file_bytes"]:
        failures.append("compact_selected_image_size_changed")
    if not all(item["per_instruction_origin_complete"] for item in compact_edits):
        failures.append("compact_selected_origin_mapping_incomplete")
    if automatic_inventory["file_bytes"] != reference_inventory["file_bytes"]:
        failures.append("automatic_compact_image_size_changed")
    if qualified_inventory["file_bytes"] != reference_inventory["file_bytes"]:
        failures.append("qualified_compact_image_size_changed")
    if not qualified_edits:
        failures.append("no_memory_effect_qualified_compact_edits")
    if hot_qualified_inventory["file_bytes"] != reference_inventory["file_bytes"]:
        failures.append("hot_qualified_compact_image_size_changed")
    reference_contract = fixed_work_contract(fixed_work_observations["reference"])
    for name, observation in fixed_work_observations.items():
        if not observation["ok"]:
            failures.append(f"{name}_fixed_work_invalid")
        elif fixed_work_contract(observation) != reference_contract:
            failures.append(f"{name}_fixed_work_contract_mismatch")

    result = {
        "schema_version": 1,
        "status": "integration_accepted" if not failures else "rejected",
        "reason_codes": failures or ["bounded_supported_host_checks_passed"],
        "architecture": "real BOLT optimization followed by firmware wrapper",
        "wrapper_policy": {"max_region_bytes": args.max_region_bytes},
        "bolt_revision": "7864c0736a8a09670dfbfc513f1e9649799f2486",
        "bolt_binary_sha256": sha256(args.bolt.resolve()),
        "bat_dump_binary_sha256": sha256(args.bat_dump.resolve()),
        "identity_sources": {
            "source_instruction_identity_and_order": "native post-optimization report",
            "final_instruction_offset_and_size": "Capstone decode of final BOLT ELF",
            "final_branch_target_identity": "BOLT BAT translation",
        },
        "reference": reference_inventory,
        "candidate": candidate_inventory,
        "fop": parse_fop(run.stdout),
        "native_post_optimization_summary": native_report["summary"],
        "protected_entries": protected,
        "accessor": {
            "output_address": accessor[0] if accessor else None,
            "matches_measured_fop_body": accessor_match,
            "variant": accessor_variant,
            "measured_body_bytes": (
                len(expected_address_fold_body)
                if accessor_variant == "ordinary_fop_plus_checked_address_fold"
                else len(expected_body)
            ),
        },
        "paced_installation_smoke": observations,
        "fixed_work_acceptance": {
            "oracle": (
                "closed-loop exact completed units, acknowledged tick count, "
                "marker multiplicity, return status and ordered interrupt prefix"
            ),
            "paced_marker_counts_are_diagnostic_only": True,
            "observations": fixed_work_observations,
        },
        "compact_selected_adoption": {
            "classification": "exact BOLT bodies imported after optimization",
            "candidate": compact_inventory,
            "edits": compact_edits,
            "paced_installation_smoke": compact_observation,
            "full_reference_completeness": False,
            "per_instruction_origin_complete": all(
                item["per_instruction_origin_complete"] for item in compact_edits
            ),
        },
        "automatic_compact_adoption": {
            "policy": (
                "simple single-entry unsplit functions; complete origins; no calls, "
                "branches, or RIP-relative operands; "
                f"<={args.max_region_bytes} bytes; strictly smaller; "
                "non-overlapping original ranges; protected prefix retained"
            ),
            "selected_function_count": len(automatic_edits),
            "original_capacity_bytes": sum(
                item["qualified_capacity_bytes"] for item in automatic_edits
            ),
            "live_bolt_bytes": sum(item["live_bolt_bytes"] for item in automatic_edits),
            "saved_live_bytes": sum(
                item["qualified_capacity_bytes"] - item["live_bolt_bytes"]
                for item in automatic_edits
            ),
            "rejection_counts": automatic_rejections,
            "candidate": automatic_inventory,
            "edits": automatic_edits,
            "paced_installation_smoke": automatic_observation,
            "full_semantic_contract": False,
        },
        "memory_effect_qualified_adoption": {
            "policy": (
                "all explicit memory instructions remain identical and ordered, or an "
                "original stack load is replaced through BOLT FOP with an exported "
                "dependency on the exact earlier, unchanged source store; direct calls "
                "retain their exact original identity/order and are repaired to the "
                "target decoded from the original E8 rel32 instruction"
            ),
            "contract_scope": (
                "x86 explicit memory events, BOLT frame-available-expression "
                "dependencies, and exact direct-call targets; not whole-machine or "
                "firmware-device equivalence"
            ),
            "selected_function_count": len(qualified_edits),
            "exact_memory_sequence_function_count": sum(
                item["classification"] == "exact_explicit_memory_sequence"
                for item in qualified_edits
            ),
            "fop_dependency_function_count": sum(
                item["classification"] == "bolt_fop_store_to_load_dependency"
                for item in qualified_edits
            ),
            "forwarding_dependency_count": sum(
                item["forwarding_dependency_count"] for item in qualified_edits
            ),
            "original_capacity_bytes": sum(
                item["qualified_capacity_bytes"] for item in qualified_edits
            ),
            "live_bolt_bytes": sum(item["live_bolt_bytes"] for item in qualified_edits),
            "saved_live_bytes": sum(
                item["qualified_capacity_bytes"] - item["live_bolt_bytes"]
                for item in qualified_edits
            ),
            "rejection_counts": qualification_rejections,
            "direct_call_extension": {
                "structurally_selected_function_count": len(direct_call_names),
                "qualified_function_count": len(direct_qualified_names),
                "repaired_call_count": sum(
                    len(items) for items in direct_call_repairs.values()
                ),
                "saved_live_bytes": sum(
                    item["qualified_capacity_bytes"] - item["live_bolt_bytes"]
                    for item in qualified_edits
                    if item["symbol"] in direct_qualified_names
                ),
                "selection_rejection_counts": direct_call_rejections,
                "qualification_rejection_counts": direct_qualification_rejections,
                "repair_rejection_counts": direct_repair_rejections,
                "repair_rejection_diagnostics": direct_repair_diagnostics,
            },
            "internal_branch_extension": {
                "structurally_selected_function_count": len(internal_branch_names),
                "qualified_function_count": len(internal_branch_qualified_names),
                "validated_branch_count": sum(
                    len(items) for items in internal_branch_records.values()
                ),
                "saved_live_bytes": sum(
                    item["qualified_capacity_bytes"] - item["live_bolt_bytes"]
                    for item in qualified_edits
                    if item["symbol"] in internal_branch_qualified_names
                ),
                "selection_rejection_counts": internal_branch_rejections,
                "qualification_rejection_counts": (
                    internal_branch_qualification_rejections
                ),
            },
            "rip_relative_extension": {
                "decoder": f"capstone-{capstone.__version__}",
                "structurally_selected_function_count": len(rip_relative_names),
                "qualified_function_count": len(rip_relative_qualified_names),
                "repaired_reference_count": sum(
                    len(items) for items in rip_relative_repairs.values()
                ),
                "saved_live_bytes": sum(
                    item["qualified_capacity_bytes"] - item["live_bolt_bytes"]
                    for item in qualified_edits
                    if item["symbol"] in rip_relative_qualified_names
                ),
                "selection_rejection_counts": rip_relative_rejections,
                "qualification_rejection_counts": (
                    rip_relative_qualification_rejections
                ),
                "repair_rejection_counts": rip_repair_rejections,
                "repair_rejection_diagnostics": rip_repair_diagnostics,
            },
            "combined_call_rip_extension": {
                "decoder": f"capstone-{capstone.__version__}",
                "structurally_selected_function_count": len(call_rip_names),
                "qualified_function_count": len(call_rip_qualified_names),
                "repaired_call_count": sum(
                    len(items) for items in call_rip_call_repairs.values()
                ),
                "repaired_data_reference_count": sum(
                    len(items) for items in call_rip_data_repairs.values()
                ),
                "saved_live_bytes": sum(
                    item["qualified_capacity_bytes"] - item["live_bolt_bytes"]
                    for item in qualified_edits
                    if item["symbol"] in call_rip_qualified_names
                ),
                "selection_rejection_counts": call_rip_rejections,
                "qualification_rejection_counts": call_rip_qualification_rejections,
                "call_repair_rejection_counts": call_rip_call_repair_rejections,
                "call_repair_rejection_diagnostics": (
                    call_rip_call_repair_diagnostics
                ),
                "data_repair_rejection_counts": call_rip_data_repair_rejections,
                "data_repair_rejection_diagnostics": (
                    call_rip_data_repair_diagnostics
                ),
            },
            "exact_leaf_call_inline_extension": {
                "contract": (
                    "System V ABI valid private call stack, no call observer, "
                    "empty patch contract and explicit --assume-abi"
                ),
                "same_size_replacements_allowed": True,
                "structurally_selected_function_count": len(leaf_inline_names),
                "qualified_function_count": len(leaf_inline_qualified_names),
                "repaired_call_count": sum(
                    len(items) for items in leaf_inline_call_repairs.values()
                ),
                "repaired_data_reference_count": sum(
                    len(items) for items in leaf_inline_data_repairs.values()
                ),
                "saved_live_bytes": sum(
                    item["qualified_capacity_bytes"] - item["live_bolt_bytes"]
                    for item in qualified_edits
                    if item["symbol"] in leaf_inline_qualified_names
                ),
                "selection_rejection_counts": leaf_inline_rejections,
                "qualification_rejection_counts": (
                    leaf_inline_qualification_rejections
                ),
                "call_repair_rejection_counts": (
                    leaf_inline_call_repair_rejections
                ),
                "call_repair_rejection_diagnostics": (
                    leaf_inline_call_repair_diagnostics
                ),
                "data_repair_rejection_counts": (
                    leaf_inline_data_repair_rejections
                ),
                "data_repair_rejection_diagnostics": (
                    leaf_inline_data_repair_diagnostics
                ),
            },
            "branch_heavy_extension": {
                "decoder": f"capstone-{capstone.__version__}",
                "structurally_selected_function_count": len(branch_heavy_names),
                "qualified_function_count": len(branch_heavy_qualified_names),
                "validated_branch_count": sum(
                    len(items) for items in branch_heavy_records.values()
                ),
                "repaired_call_count": sum(
                    len(items) for items in branch_heavy_call_repairs.values()
                ),
                "repaired_data_reference_count": sum(
                    len(items) for items in branch_heavy_data_repairs.values()
                ),
                "saved_live_bytes": sum(
                    item["qualified_capacity_bytes"] - item["live_bolt_bytes"]
                    for item in qualified_edits
                    if item["symbol"] in branch_heavy_qualified_names
                ),
                "selection_rejection_counts": branch_heavy_rejections,
                "branch_rejection_diagnostics": branch_heavy_rejection_diagnostics,
                "qualification_rejection_counts": branch_heavy_qualification_rejections,
                "call_repair_rejection_counts": (
                    branch_heavy_call_repair_rejections
                ),
                "call_repair_rejection_diagnostics": (
                    branch_heavy_call_repair_diagnostics
                ),
                "data_repair_rejection_counts": (
                    branch_heavy_data_repair_rejections
                ),
                "data_repair_rejection_diagnostics": (
                    branch_heavy_data_repair_diagnostics
                ),
            },
            "absolute_symbol_reference_extension": {
                "structurally_selected_function_count": len(
                    absolute_reference_names
                ),
                "qualified_function_count": len(absolute_reference_accepted),
                "repaired_reference_count": sum(
                    len(items) for items in absolute_reference_repairs.values()
                ),
                "repair_rejection_counts": absolute_reference_rejections,
                "repair_rejection_diagnostics": absolute_reference_diagnostics,
            },
            "candidate": qualified_inventory,
            "edits": qualified_edits,
            "paced_installation_smoke": qualified_observation,
            "full_semantic_contract": False,
        },
        "hot_qualified_adoption": {
            "policy": (
                "publish the same correctness-qualified BOLT bodies only when the "
                "frozen training profile reports a nonzero function execution count"
            ),
            "selected_function_count": len(hot_qualified_edits),
            "original_capacity_bytes": sum(
                item["qualified_capacity_bytes"] for item in hot_qualified_edits
            ),
            "live_bolt_bytes": sum(
                item["live_bolt_bytes"] for item in hot_qualified_edits
            ),
            "saved_live_bytes": sum(
                item["qualified_capacity_bytes"] - item["live_bolt_bytes"]
                for item in hot_qualified_edits
            ),
            "candidate": hot_qualified_inventory,
            "paced_installation_smoke": hot_qualified_observation,
            "profile_authorizes_correctness": False,
        },
        "custom_pass_baseline": prototype_baseline,
        "claim_limits": {
            "full_firmware_semantic_equivalence": False,
            "whole_application_capacity": False,
            "fastdyn_speedup": False,
            "cortex_m_optimization": False,
        },
    }
    write_json(output / "wrapper-result.json", result)
    write_json(
        output / "bolt-pass-results.json",
        {
            "schema_version": 1,
            "status": result["status"],
            "architecture": result["architecture"],
            "bolt_revision": result["bolt_revision"],
            "bolt_binary_sha256": result["bolt_binary_sha256"],
            "fop": result["fop"],
            "native_post_optimization_summary": result[
                "native_post_optimization_summary"
            ],
            "exported_frame_forwarding_count": len(
                native_report.get("frame_forwardings", [])
            ),
            "exported_arithmetic_fusion_count": len(
                native_report.get("arithmetic_fusions", [])
            ),
            "exported_copy_cleanup_count": len(
                native_report.get("copy_cleanups", [])
            ),
            "protected_entry_count": len(protected),
            "compact_installation": {
                key: result["memory_effect_qualified_adoption"][key]
                for key in (
                    "contract_scope",
                    "selected_function_count",
                    "exact_memory_sequence_function_count",
                    "fop_dependency_function_count",
                    "forwarding_dependency_count",
                    "original_capacity_bytes",
                    "live_bolt_bytes",
                    "saved_live_bytes",
                    "rejection_counts",
                    "direct_call_extension",
                    "internal_branch_extension",
                    "rip_relative_extension",
                    "combined_call_rip_extension",
                    "branch_heavy_extension",
                )
            },
            "hot_compact_installation": result["hot_qualified_adoption"],
            "reference_file_bytes": reference_inventory["file_bytes"],
            "full_bolt_file_bytes": candidate_inventory["file_bytes"],
            "compact_file_bytes": qualified_inventory["file_bytes"],
            "claim_limits": result["claim_limits"],
        },
    )
    print(json.dumps({
        "status": result["status"],
        "protected_entries": len(protected),
        "fop": result["fop"],
        "output": str(output),
    }, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
