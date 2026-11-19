#!/usr/bin/env python3
"""Build and validate the Firm-BOLT Cortex-M backend.

This is the reproducible qualification command for the direct BOLT port.
It independently qualifies a real Cortex-M0+ ESCape32 O0 image, bounded
Cortex-M0+ architectural/reference/veneer fixtures, a relocation- and
execution-checked Cortex-M3 backend fixture, and the real Cortex-M4 FreeRTOS
image plus a fixed-work FastDyn smoke.

The default production policy is compact in-place publication. Separately
qualified relocation-complete fixtures exercise whole-image function ordering
and hot/cold splitting inside original linker-owned .text flash extents.
Unknown or unqualified compact functions retain their original implementation;
relocation-mode rejection is atomic, and any new loadable range is a hard
failure. The report names all remaining capability limits.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from capstone import CS_ARCH_ARM, CS_MODE_MCLASS, CS_MODE_THUMB, Cs
from capstone.arm import ARM_OP_IMM, ARM_OP_MEM, ARM_REG_PC
from elftools.elf.elffile import ELFFile


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
LLVM_SOURCE = ROOT / "third_party/preflight/llvm-project"
LLVM_BUILD = ROOT / "third_party/preflight/llvm-build"
DEFAULT_BOLT = LLVM_BUILD / "bin/llvm-bolt"
DEFAULT_FASTDYN = ROOT / "fastdyn-env/bin/fastdyn"
DEFAULT_OUTPUT = ROOT / "build/preflight/firm-bolt-cortex-m-port"
YAML2OBJ = LLVM_BUILD / "bin/yaml2obj"
LLVM_MC = LLVM_BUILD / "bin/llvm-mc"
LD_LLD = Path("/usr/bin/ld.lld-18")
M0_IMAGE = ROOT / "third_party/preflight/escape32-64k-O0/ESCAPE1-rev12.1.elf"
M4_IMAGE = ROOT / "tests/sample_binaries/RTOS/RTOSDemo.axf"
M3_SOURCE = ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3.s"
M3_LINKER = ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3.ld"
M3_JUMP6_YAML = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_jump6.yaml"
)
M3_NARROW_DATA_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_narrow_data.s"
)
M3_INLINE_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_inline.s"
)
M3_TAILDUP_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_taildup.s"
)
M3_TAILDUP_PROPAGATION_SOURCE = (
    ROOT
    / "tests/preflight/fixtures/firm_bolt_cortex_m3_taildup_propagation.s"
)
M3_RODATA_LOAD_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_rodata_load.s"
)
M3_RODATA_LOAD_LINKER = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_rodata_load.ld"
)
M3_FUNCTION_REORDER_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_function_reorder.s"
)
M3_FUNCTION_REORDER_LINKER = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_function_reorder.ld"
)
M3_FUNCTION_REORDER_ORDER = (
    ROOT
    / "tests/preflight/fixtures/firm_bolt_cortex_m3_function_reorder.order"
)
M3_FUNCTION_REORDER_PROFILE = (
    ROOT
    / "tests/preflight/fixtures/firm_bolt_cortex_m3_function_reorder.fdata"
)
M3_FUNCTION_REORDER_CALLGRAPH_PROFILE = (
    ROOT
    / "tests/preflight/fixtures/firm_bolt_cortex_m3_function_reorder_callgraph.fdata"
)
M3_FUNCTION_SPLIT_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_function_split.s"
)
M3_FUNCTION_SPLIT_LINKER = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_function_split.ld"
)
M3_FUNCTION_SPLIT_PROFILE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_function_split.fdata"
)
M3_CDSPLIT_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_cdsplit.s"
)
M3_CDSPLIT_LINKER = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_cdsplit.ld"
)
M3_CDSPLIT_PROFILE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_cdsplit.fdata"
)
M3_LOOP_INVERSION_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_loop_inversion.s"
)
M3_PEEPHOLE_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_peephole.s"
)
M3_ICP_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_icp.s"
)
M3_ICP_R12_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_icp_r12.s"
)
M3_ICP_METHOD_LOAD_SOURCE = (
    ROOT
    / "tests/preflight/fixtures/firm_bolt_cortex_m3_icp_method_load.s"
)
M3_ICP_TAIL_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m3_icp_tail.s"
)
M3_JUMP_TABLE_ICP_SOURCE = (
    ROOT
    / "tests/preflight/fixtures/firm_bolt_cortex_m3_jump_table_icp.s"
)
M3_ABS32_JUMP_TABLE_ICP_SOURCE = (
    ROOT
    / "tests/preflight/fixtures/firm_bolt_cortex_m3_abs32_jump_table_icp.s"
)
M3_DETACHED_ABS32_JUMP_TABLE_ICP_SOURCE = (
    ROOT / "tests/preflight/fixtures/"
    "firm_bolt_cortex_m3_detached_abs32_jump_table_icp.s"
)
M3_DETACHED_ABS32_JUMP_TABLE_ICP_LINKER = (
    ROOT / "tests/preflight/fixtures/"
    "firm_bolt_cortex_m3_detached_abs32_jump_table_icp.ld"
)
M0_LITERAL_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m0plus_literal.s"
)
M0_MOVABLE_LITERALS_SOURCE = (
    ROOT
    / "tests/preflight/fixtures/firm_bolt_cortex_m0plus_movable_literals.s"
)
M0_MOVED_RELOCATION_SOURCE = (
    ROOT
    / "tests/preflight/fixtures/firm_bolt_cortex_m0plus_moved_relocation.s"
)
M0_RELOCATION_CLONE_SOURCE = (
    ROOT
    / "tests/preflight/fixtures/firm_bolt_cortex_m0plus_relocation_clone.s"
)
M0_BRANCH_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m0plus_branch_relaxation.s"
)
M0_CALL_VENEER_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m0plus_call_veneer.s"
)
M0_RESERVED_VENEER_SOURCE = (
    ROOT
    / "tests/preflight/fixtures/firm_bolt_cortex_m0plus_reserved_veneer.s"
)
M0_ICP_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m0plus_icp.s"
)
M0_ICP_TAIL_SOURCE = (
    ROOT / "tests/preflight/fixtures/firm_bolt_cortex_m0plus_icp_tail.s"
)
M0_ICP_METHOD_LOAD_SOURCE = (
    ROOT
    / "tests/preflight/fixtures/firm_bolt_cortex_m0plus_icp_method_load.s"
)
ARM_GCC = Path(
    "/data/arm-gnu-toolchain-13.3.rel1-x86_64-arm-none-eabi/bin/arm-none-eabi-gcc"
)
PINNED_BOLT = "7864c0736a8a09670dfbfc513f1e9649799f2486"

LITERAL_POOL_SUMMARY_RE = re.compile(
    r"Thumb literal-pool placement qualified (?P<qualified>\d+) "
    r"function\(s\), attached (?P<owners>\d+) owner block\(s\), rebound "
    r"(?P<references>\d+) PC-relative reference\(s\), and retained "
    r"(?P<retained>\d+) function\(s\); skipped (?P<unreferenced>\d+) "
    r"unreferenced island function\(s\)"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_literal_pool_summary(output: str) -> dict[str, Any]:
    """Return the pass ledger without conflating rejection and no references."""
    matches = list(LITERAL_POOL_SUMMARY_RE.finditer(output))
    if len(matches) != 1:
        raise RuntimeError(
            "expected exactly one structured Thumb literal-pool summary, "
            f"found {len(matches)}"
        )
    counts = {key: int(value) for key, value in matches[0].groupdict().items()}
    return {"status": "accepted", **counts}


def run(command: list[str], log: Path, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "$ " + " ".join(command) + "\n\n[stdout]\n" + process.stdout
        + "\n[stderr]\n" + process.stderr
        + f"\n[returncode]\n{process.returncode}\n"
    )
    return process


def require_success(process: subprocess.CompletedProcess[str], label: str) -> None:
    if process.returncode:
        raise RuntimeError(
            f"{label} failed with status {process.returncode}:\n"
            f"{process.stderr[-4000:]}"
        )


def elf_inventory(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [
            {
                "type": segment["p_type"],
                "offset": int(segment["p_offset"]),
                "vaddr": int(segment["p_vaddr"]),
                "paddr": int(segment["p_paddr"]),
                "file_size": int(segment["p_filesz"]),
                "memory_size": int(segment["p_memsz"]),
                "flags": int(segment["p_flags"]),
                "alignment": int(segment["p_align"]),
            }
            for segment in elf.iter_segments()
        ]
        sections = {
            section.name: {
                "type": section["sh_type"],
                "address": int(section["sh_addr"]),
                "offset": int(section["sh_offset"]),
                "size": int(section["sh_size"]),
                "flags": int(section["sh_flags"]),
                "alignment": int(section["sh_addralign"]),
                "sha256": hashlib.sha256(section.data()).hexdigest()
                if section["sh_type"] != "SHT_NOBITS"
                else None,
            }
            for section in elf.iter_sections()
        }
        return {
            "path": str(path),
            "sha256": sha256(path),
            "file_bytes": path.stat().st_size,
            "entry": int(elf.header["e_entry"]),
            "segments": segments,
            "sections": sections,
        }


def section_bytes(path: Path, name: str) -> bytes:
    with path.open("rb") as stream:
        section = ELFFile(stream).get_section_by_name(name)
        if section is None:
            raise RuntimeError(f"{path} has no {name} section")
        return section.data()


def virtual_bytes(path: Path, address: int, size: int) -> bytes:
    """Read an initialized allocatable byte range by its linked address."""
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        for section in elf.iter_sections():
            start = int(section["sh_addr"])
            end = start + int(section["sh_size"])
            if (
                section["sh_type"] != "SHT_NOBITS"
                and start <= address and address + size <= end
            ):
                offset = address - start
                return section.data()[offset:offset + size]
    raise RuntimeError(
        f"{path} has no initialized section covering "
        f"0x{address:x}..0x{address + size:x}"
    )


def summarize_pass_publication(
    reference: Path, candidate: Path, native_report: dict[str, Any],
    final_layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attribute installed function-byte deltas to the audited pass ledger.

    A function can carry multiple reasons, so per-reason byte totals are
    affected-byte counts and are intentionally not additive. The unique total
    counts each compared firmware byte once.
    """
    functions: list[dict[str, Any]] = []
    reason_totals: dict[str, dict[str, int]] = {}
    unique_changed_bytes = 0
    selected_functions = 0
    byte_changed_functions = 0
    rejected_functions = 0
    rejection_totals: dict[str, int] = {}
    final_by_address = {
        int(function["original_address"]): function
        for function in (final_layout or {}).get("functions", [])
    }
    for function in native_report["functions"]:
        address = int(function["original_address"])
        final_function = final_by_address.get(address, function)
        rejections = list(
            final_function.get("firmware_publication_rejections", [])
        )
        if rejections:
            rejected_functions += 1
            for rejection in rejections:
                rejection_totals[rejection] = (
                    rejection_totals.get(rejection, 0) + 1
                )
        if not final_function.get("firmware_modified", False):
            continue
        selected_functions += 1
        size = int(function["original_size"])
        original = virtual_bytes(reference, address, size)
        fragments = final_function.get("fragments", [])
        candidate_ranges = [
            {
                "address": int(fragment["output_address"]),
                "size": int(fragment["output_size"]),
                "kind": fragment["kind"],
            }
            for fragment in fragments
            if int(fragment["output_size"])
        ]
        if candidate_ranges:
            emitted = b"".join(
                virtual_bytes(candidate, item["address"], item["size"])
                for item in candidate_ranges
            )
        else:
            candidate_address = int(
                final_function.get("output_address", address)
            )
            candidate_size = int(
                final_function.get("output_size", size)
            )
            candidate_ranges = [{
                "address": candidate_address,
                "size": candidate_size,
                "kind": "main",
            }]
            emitted = virtual_bytes(
                candidate, candidate_address, candidate_size
            )
        changed = (
            sum(left != right for left, right in zip(original, emitted))
            + abs(len(original) - len(emitted))
        )
        unique_changed_bytes += changed
        byte_changed_functions += changed != 0
        reasons = list(
            final_function.get("firmware_modification_reasons", [])
        )
        functions.append({
            "name": function["name"],
            "original_address": address,
            "original_size": size,
            "emitted_fragment_bytes": len(emitted),
            "candidate_ranges": candidate_ranges,
            "changed_executable_bytes": changed,
            "reasons": reasons,
        })
        for reason in reasons:
            totals = reason_totals.setdefault(reason, {
                "selected_function_count": 0,
                "byte_changed_function_count": 0,
                "affected_executable_bytes": 0,
            })
            totals["selected_function_count"] += 1
            totals["byte_changed_function_count"] += changed != 0
            totals["affected_executable_bytes"] += changed
    return {
        "selected_function_count": selected_functions,
        "byte_changed_function_count": byte_changed_functions,
        "rejected_function_count": rejected_functions,
        "rejections_by_reason": rejection_totals,
        "unique_changed_executable_bytes_in_selected_ranges":
            unique_changed_bytes,
        "reason_counts_are_non_additive": True,
        "by_reason": reason_totals,
        "functions": functions,
    }


def validate_inline_jump_tables(reference: Path, candidate: Path,
                                final_layout: dict[str, Any]) -> dict[str, Any]:
    """Check emitted Thumb jump tables against original-identity CFG targets.

    The pass report maps each original basic-block identity to its estimated
    post-optimization offset.  This validator then reads the final ELF bytes
    and proves that every compact table entry reaches that relocated block.
    It deliberately does not use stale raw PCs or disassembler heuristics.
    """
    with candidate.open("rb") as stream:
        elf = ELFFile(stream)
        symtab = elf.get_section_by_name(".symtab")
        mapping_symbols = {
            (symbol.name.split(".", 1)[0], int(symbol["st_value"]) & ~1)
            for symbol in symtab.iter_symbols()
            if symbol.name == "$d" or symbol.name.startswith("$d.")
            or symbol.name == "$t" or symbol.name.startswith("$t.")
        } if symtab is not None else set()
        mapping_transitions = sorted(
            (address, kind) for kind, address in mapping_symbols
        )

    def mapping_kind_at(address: int) -> str | None:
        """Return the ARM mapping state covering an address.

        Mapping symbols are state transitions, not per-object labels.  A
        sequence of adjacent absolute switch tables therefore needs only the
        first `$d`, whereas an inline TBB/TBH island must also transition back
        to `$t` at its exact end.
        """
        state = None
        for transition_address, kind in mapping_transitions:
            if transition_address > address:
                break
            state = kind
        return state
    tables: list[dict[str, Any]] = []
    for function in final_layout["functions"]:
        reported_tables = function.get("inline_jump_tables", [])
        if not reported_tables:
            continue
        function_address = int(function["original_address"])
        function_size = int(function["original_size"])
        unchanged = (
            virtual_bytes(reference, function_address, function_size)
            == virtual_bytes(candidate, function_address, function_size)
        )
        for table in reported_tables:
            table_address = int(table["table_output_address"])
            kind = table["kind"]
            branch_base_value = table.get("branch_base_output_address")
            branch_base = (
                int(branch_base_value)
                if branch_base_value is not None else None
            )
            entry_size = int(table["entry_size"])
            entry_count = int(table["entry_count"])
            encoded = virtual_bytes(
                candidate, table_address, entry_size * entry_count
            )
            encoded_entries = [
                int.from_bytes(
                    encoded[index * entry_size:(index + 1) * entry_size],
                    "little", signed=False,
                )
                for index in range(entry_count)
            ]
            if kind == "thumb_abs32":
                # Absolute switch entries are Thumb code pointers.  Compare
                # their architectural address identities after clearing the
                # state bit; unlike TBB/TBH they have no PC-relative base.
                actual_targets = [entry & ~1 for entry in encoded_entries]
            else:
                if branch_base is None:
                    raise RuntimeError(
                        f"{function['name']} emitted {kind} without a branch base"
                    )
                actual_targets = [
                    branch_base + entry * 2 for entry in encoded_entries
                ]
            target_identities = table["target_block_original_addresses"]
            expected_targets = (
                target_identities if unchanged
                else table["target_block_output_addresses"]
            )
            exact = actual_targets == expected_targets
            code_mark = None
            if kind == "thumb_abs32":
                # These compiler switch tables are separate data objects, not
                # inline islands followed immediately by executable bytes.
                mapping_exact = mapping_kind_at(table_address) == "$d"
            else:
                code_mark = (
                    table_address + entry_size * entry_count
                    + int(table["trailing_byte_count"])
                )
                mapping_exact = (
                    ("$d", table_address) in mapping_symbols
                    and ("$t", code_mark) in mapping_symbols
                )
            result = {
                "function": function["name"],
                "dispatch_original_address": table["dispatch_original_address"],
                "kind": kind,
                "entry_size": entry_size,
                "entry_count": entry_count,
                "emitted_table_address": table_address,
                "emitted_branch_base_address": branch_base,
                "encoded_hex": encoded.hex(),
                "target_original_identities": target_identities,
                "expected_emitted_targets": expected_targets,
                "actual_emitted_targets": actual_targets,
                "data_mapping_symbol_address": table_address,
                "thumb_mapping_symbol_address": code_mark,
                "mapping_symbols_exact": mapping_exact,
                "validation_basis": (
                    "unchanged_original_body" if unchanged
                    else "installed_linker_resolved_body"
                ),
                "exact": exact,
            }
            tables.append(result)
            if not exact:
                raise RuntimeError(
                    f"{function['name']} emitted {table['kind']} targets do not "
                    "match the original-identity CFG"
                )
            if not mapping_exact:
                raise RuntimeError(
                    f"{function['name']} emitted {table['kind']} without exact "
                    "$d/$t mapping symbols"
                )
    return {
        "status": "accepted",
        "table_count": len(tables),
        "entry_count": sum(table["entry_count"] for table in tables),
        "tables": tables,
    }


def validate_arm_mapping_symbols(candidate: Path) -> dict[str, Any]:
    """Require final ARM mapping symbols to describe loaded executable bytes.

    The intermediate MC object is not the published artifact.  This check
    specifically guards BOLT's final symtab reconstruction, including the
    non-relocation-mode constant-island path that previously emitted `$d/$t`
    at address zero.
    """
    records: list[dict[str, Any]] = []
    with candidate.open("rb") as stream:
        elf = ELFFile(stream)
        symtab = elf.get_section_by_name(".symtab")
        if symtab is None:
            raise RuntimeError(f"{candidate} has no final symbol table")
        for symbol in symtab.iter_symbols():
            kind = symbol.name.split(".", 1)[0]
            if kind not in {"$a", "$d", "$t"}:
                continue
            address = int(symbol["st_value"]) & ~1
            section_index = symbol["st_shndx"]
            if not isinstance(section_index, int):
                raise RuntimeError(
                    f"{candidate} mapping symbol {symbol.name} has special "
                    f"section index {section_index}"
                )
            section = elf.get_section(section_index)
            start = int(section["sh_addr"])
            end = start + int(section["sh_size"])
            allocated = bool(int(section["sh_flags"]) & 0x2)
            executable = bool(int(section["sh_flags"]) & 0x4)
            section_kind_valid = allocated and (
                kind == "$d" or executable
            )
            valid = (
                address != 0 and section_kind_valid
                and start <= address <= end
            )
            records.append({
                "name": symbol.name,
                "kind": kind,
                "address": address,
                "section": section.name,
                "valid": valid,
            })
            if not valid:
                raise RuntimeError(
                    f"{candidate} has invalid final mapping symbol "
                    f"{symbol.name} at 0x{address:x} in {section.name}"
                )
    return {
        "status": "accepted",
        "mapping_symbol_count": len(records),
        "kind_counts": {
            kind: sum(record["kind"] == kind for record in records)
            for kind in ("$a", "$d", "$t")
        },
        "records": records,
    }


def static_relocation_records(candidate: Path, section_name: str) -> list[dict[str, Any]]:
    """Decode retained static relocations against the rebuilt output symtab."""
    records: list[dict[str, Any]] = []
    with candidate.open("rb") as stream:
        elf = ELFFile(stream)
        section = elf.get_section_by_name(section_name)
        if section is None or not hasattr(section, "iter_relocations"):
            raise RuntimeError(
                f"{candidate} has no relocation section {section_name}"
            )
        symbols = elf.get_section(int(section["sh_link"]))
        for relocation in section.iter_relocations():
            symbol_index = int(relocation["r_info_sym"])
            symbol = symbols.get_symbol(symbol_index)
            records.append({
                "offset": int(relocation["r_offset"]),
                "type": int(relocation["r_info_type"]),
                "symbol_index": symbol_index,
                "symbol": symbol.name,
                "symbol_value": int(symbol["st_value"]),
            })
    return records


def thumb_pc_relative_target(instruction: Any) -> int | None:
    """Return the architectural data target of ADR/literal-load instructions."""
    mnemonic = instruction.mnemonic.removesuffix(".w")
    if mnemonic == "adr" and len(instruction.operands) >= 2:
        operand = instruction.operands[1]
        if operand.type != ARM_OP_IMM:
            return None
        # Capstone 4 exposes Thumb ADR's encoded PC-relative displacement,
        # not the architectural result address.  Validate the actual address
        # so two equally wrong encodings cannot pass by sharing an immediate.
        return ((int(instruction.address) + 4) & ~3) + int(operand.imm)
    if mnemonic not in {"ldr", "ldrb", "ldrh", "ldrsb", "ldrsh"}:
        return None
    if len(instruction.operands) < 2:
        return None
    operand = instruction.operands[1]
    if operand.type != ARM_OP_MEM or operand.mem.base != ARM_REG_PC:
        return None
    return ((int(instruction.address) + 4) & ~3) + int(operand.mem.disp)


def decode_one_thumb(decoder: Cs, image: Path, address: int) -> Any:
    # A 16-bit instruction at the exact end of an initialized section is
    # complete and decodable; requiring four bytes incorrectly rejects it.
    # Thumb-2 prefixes do not decode from the first halfword alone, so retry
    # with four bytes only when the narrow attempt produces no instruction.
    data = virtual_bytes(image, address, 2)
    decoded = list(decoder.disasm(data, address, count=1))
    if not decoded:
        data = virtual_bytes(image, address, 4)
        decoded = list(decoder.disasm(data, address, count=1))
    if not decoded:
        raise RuntimeError(f"cannot decode Thumb instruction at 0x{address:x}")
    return decoded[0]


def validate_pc_relative_data_references(
    reference: Path, candidate: Path, final_layout: dict[str, Any],
    allow_owned_literal_movement: bool = False,
) -> dict[str, Any]:
    """Check final ADR/literal targets through exact original identities."""
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoder.detail = True
    checked: list[dict[str, Any]] = []
    mapped_origins = 0
    synthesized_origins = 0
    owned_pool_translations = [
        (int(pool["original_address"]), int(pool["output_address"]),
         int(pool["size"]))
        for function in final_layout["functions"]
        for pool in function.get("owned_literal_pools", [])
    ]
    for function in final_layout["functions"]:
        address = int(function["original_address"])
        size = int(function["original_size"])
        unchanged = (
            virtual_bytes(reference, address, size)
            == virtual_bytes(candidate, address, size)
        )
        for origin in function.get("instruction_origins", []):
            if origin.get("origin_kind") != "original_instruction":
                synthesized_origins += 1
                continue
            original_address = int(origin["original_address"])
            output_address = (
                original_address if unchanged else int(origin["output_address"])
            )
            mapped_origins += 1
            original = decode_one_thumb(decoder, reference, original_address)
            original_target = thumb_pc_relative_target(original)
            if original_target is None:
                continue
            emitted = decode_one_thumb(decoder, candidate, output_address)
            emitted_target = thumb_pc_relative_target(emitted)
            exact = emitted_target == original_target
            accepted_by = "exact_address"
            if not exact and allow_owned_literal_movement:
                owned_pool_targets = [
                    output + (original_target - original)
                    for original, output, size in owned_pool_translations
                    if original <= original_target < original + size
                ]
                abs32_pointer_targets = [
                    int(table["table_pointer_output_address"])
                    for owner in final_layout["functions"]
                    for table in owner.get("inline_jump_tables", [])
                    if table.get("kind") == "thumb_abs32"
                    and table.get("table_pointer_output_address") is not None
                    and original_target
                    == int(table["table_pointer_original_address"])
                ]
                if emitted_target in owned_pool_targets:
                    accepted_by = "declared_owned_literal_translation"
                elif emitted_target in abs32_pointer_targets:
                    accepted_by = "declared_abs32_table_pointer_translation"
            item = {
                "function": function["name"],
                "original_identity": original_address,
                "output_address": output_address,
                "original_instruction": f"{original.mnemonic} {original.op_str}",
                "emitted_instruction": f"{emitted.mnemonic} {emitted.op_str}",
                "original_target": original_target,
                "emitted_target": emitted_target,
                "installed_original_body": unchanged,
                "exact": exact,
                "accepted_by": accepted_by,
            }
            checked.append(item)
            if not exact and accepted_by == "exact_address":
                raise RuntimeError(
                    f"{function['name']} PC-relative reference at original "
                    f"identity 0x{original_address:x} changed target from "
                    f"0x{original_target:x} to {emitted_target!r}"
                )
    return {
        "status": "accepted",
        "mapped_instruction_count": mapped_origins,
        "synthesized_instruction_count": synthesized_origins,
        "pc_relative_reference_count": len(checked),
        "contract": (
            "declared_owned_literal_translation"
            if allow_owned_literal_movement else "exact_address"
        ),
        "references": checked,
    }


def validate_in_place(reference: Path, candidate: Path,
                      protected: dict[str, int | None]) -> dict[str, Any]:
    ref = elf_inventory(reference)
    out = elf_inventory(candidate)
    if ref["entry"] != out["entry"]:
        raise RuntimeError("entry point changed")
    if ref["segments"] != out["segments"]:
        raise RuntimeError("program headers or loaded ranges changed")

    ref_alloc = {
        name: value for name, value in ref["sections"].items()
        if value["flags"] & 0x2
    }
    out_alloc = {
        name: value for name, value in out["sections"].items()
        if value["flags"] & 0x2
    }
    if set(ref_alloc) != set(out_alloc):
        raise RuntimeError(
            f"allocated section set changed: {set(ref_alloc) ^ set(out_alloc)}"
        )
    metadata_fields = ("type", "address", "offset", "size", "flags", "alignment")
    for name in ref_alloc:
        for field in metadata_fields:
            if ref_alloc[name][field] != out_alloc[name][field]:
                raise RuntimeError(f"allocated section {name} changed {field}")

    protected_results = {}
    for name, prefix_size in protected.items():
        left = section_bytes(reference, name)
        right = section_bytes(candidate, name)
        if prefix_size is not None:
            left = left[:prefix_size]
            right = right[:prefix_size]
        if left != right:
            raise RuntimeError(f"protected bytes changed in {name}")
        protected_results[name] = {
            "bytes": len(left),
            "sha256": hashlib.sha256(left).hexdigest(),
        }

    changed_exec_bytes = 0
    for name, value in ref_alloc.items():
        if not value["flags"] & 0x4 or value["type"] == "SHT_NOBITS":
            continue
        left = section_bytes(reference, name)
        right = section_bytes(candidate, name)
        changed_exec_bytes += sum(a != b for a, b in zip(left, right))

    return {
        "reference": ref,
        "candidate": out,
        "program_headers_exact": True,
        "allocated_section_layout_exact": True,
        "protected": protected_results,
        "changed_executable_bytes": changed_exec_bytes,
    }


def validate_relocated_function_order(
    reference: Path, candidate: Path,
    final_layout: dict[str, Any] | None = None,
    *, order_policy: str = "explicit_user",
) -> dict[str, Any]:
    """Validate a relocation-mode Cortex-M image as one atomic artifact.

    This is deliberately stronger than comparing ordinary function output.
    It proves that code still occupies the linker-qualified input flash span,
    the reset vector and ELF entry carry the Thumb state bit, every retained
    code relocation names the moved identity, the alternate completion label
    moved with its containing function, and the final origin map is exact.
    """
    reference_inventory = elf_inventory(reference)
    candidate_inventory = elf_inventory(candidate)
    if reference_inventory["segments"] != candidate_inventory["segments"]:
        raise RuntimeError(
            "relocation-mode candidate changed the loadable segment map"
        )

    reference_text = reference_inventory["sections"][".text"]
    candidate_text = candidate_inventory["sections"][".text"]
    for field in ("address", "offset", "size", "flags"):
        if reference_text[field] != candidate_text[field]:
            raise RuntimeError(
                f"relocation-mode .text changed qualified {field}"
            )
    old_text = candidate_inventory["sections"].get(".bolt.org.text")
    if old_text is None or int(old_text["size"]) != 0:
        raise RuntimeError(
            "relocation-mode candidate retained a live original text body"
        )

    moved = {
        name: symbol_address(candidate, name)
        for name in ("Reset_Handler", "worker_a", "worker_b", "fixture_halt")
    }
    if order_policy not in {"explicit_user", "profile_exec_count", "cdsort"}:
        raise ValueError(f"unknown function-order policy {order_policy}")
    expected_moved = (
        {
            "worker_a": 0x08000100,
            "Reset_Handler": 0x08000104,
            "fixture_halt": 0x08000118,
            "worker_b": 0x0800011C,
        }
        if order_policy in {"profile_exec_count", "cdsort"} else
        {
            "worker_b": 0x08000100,
            "Reset_Handler": 0x08000104,
            "fixture_halt": 0x08000118,
            "worker_a": 0x0800011C,
        }
    )
    if moved != expected_moved:
        raise RuntimeError(
            f"unexpected relocation-mode function order: {moved}"
        )
    reset_pointer = moved["Reset_Handler"] | 1
    if candidate_inventory["entry"] != reset_pointer:
        raise RuntimeError("relocated ELF entry lost its Thumb-state identity")
    reference_vector = struct.unpack(
        "<II", virtual_bytes(reference, 0x08000000, 8)
    )
    candidate_vector = struct.unpack(
        "<II", virtual_bytes(candidate, 0x08000000, 8)
    )
    if (
        candidate_vector[0] != reference_vector[0]
        or candidate_vector[1] != reset_pointer
    ):
        raise RuntimeError(
            "relocated vector does not name the moved Thumb reset entry"
        )

    text_relocations = static_relocation_records(candidate, ".rel.text")
    vector_relocations = static_relocation_records(
        candidate, ".rel.isr_vector"
    )
    expected_text_relocations = [
        (0x0800010C, 10, "worker_a", moved["worker_a"] | 1),
        (0x08000112, 10, "worker_b", moved["worker_b"] | 1),
        (0x08000118, 30, "fixture_halt", 0x08000118),
    ]
    observed_text_relocations = [
        (
            record["offset"], record["type"], record["symbol"],
            record["symbol_value"],
        )
        for record in text_relocations
    ]
    if observed_text_relocations != expected_text_relocations:
        raise RuntimeError(
            "relocated .rel.text records do not match moved code identities"
        )
    if [
        (
            record["offset"], record["type"], record["symbol"],
            record["symbol_value"],
        )
        for record in vector_relocations
    ] != [(0x08000004, 2, "Reset_Handler", reset_pointer)]:
        raise RuntimeError(
            "relocated vector relocation lost the moved Thumb entry"
        )

    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoder.detail = True
    repaired_control_transfers = []
    for address, expected_mnemonic, expected_target in (
        (0x0800010C, "bl", moved["worker_a"]),
        (0x08000112, "bl", moved["worker_b"]),
        (0x08000118, "b.w", moved["fixture_halt"]),
    ):
        instruction = decode_one_thumb(decoder, candidate, address)
        target = (
            int(instruction.operands[0].imm)
            if instruction.operands
            and instruction.operands[0].type == ARM_OP_IMM
            else None
        )
        repaired_control_transfers.append({
            "address": address,
            "mnemonic": instruction.mnemonic,
            "target": target,
        })
        if (
            instruction.mnemonic != expected_mnemonic
            or target != expected_target
        ):
            raise RuntimeError(
                f"relocated control transfer at 0x{address:x} does not "
                f"target 0x{expected_target:x}"
            )
    with candidate.open("rb") as stream:
        elf = ELFFile(stream)
        text_relocation_section = elf.get_section_by_name(".rel.text")
        if text_relocation_section is None:
            raise RuntimeError("relocation-mode candidate has no .rel.text")
        target_section = elf.get_section(
            int(text_relocation_section["sh_info"])
        )
        if target_section.name != ".text":
            raise RuntimeError(
                ".rel.text still targets the retired original text section"
            )

    origin_summary = None
    if final_layout is not None:
        expected_origins = {
            "Reset_Handler": (0x08000100, 0x08000104, 7),
            "worker_a": (
                0x08000118, expected_moved["worker_a"], 2
            ),
            "worker_b": (
                0x0800011C, expected_moved["worker_b"], 2
            ),
        }
        origin_summary = {}
        for function in final_layout["functions"]:
            name = function["name"]
            if name not in expected_origins:
                continue
            observed = (
                int(function["original_address"]),
                int(function["output_address"]),
                len(function["instruction_origins"]),
            )
            if observed != expected_origins[name]:
                raise RuntimeError(
                    f"relocated origin map for {name} is not exact: {observed}"
                )
            if any(
                origin["origin_kind"] != "original_instruction"
                for origin in function["instruction_origins"]
            ):
                raise RuntimeError(
                    f"relocated origin map for {name} has synthetic entries"
                )
            origin_summary[name] = {
                "original_address": observed[0],
                "output_address": observed[1],
                "instruction_origin_count": observed[2],
            }
        if set(origin_summary) != set(expected_origins):
            raise RuntimeError("relocation-mode layout omitted a function map")

    return {
        "status": "accepted",
        "placement_policy": "reuse_linker_qualified_input_text",
        "order_policy": order_policy,
        "program_headers_exact": True,
        "text_range": {
            "address": candidate_text["address"],
            "size": candidate_text["size"],
        },
        "retained_original_text_bytes": int(old_text["size"]),
        "entry": candidate_inventory["entry"],
        "vector_words": list(candidate_vector),
        "moved_symbols": moved,
        "text_relocations": text_relocations,
        "vector_relocations": vector_relocations,
        "repaired_control_transfers": repaired_control_transfers,
        "text_relocation_target_section": ".text",
        "origin_summary": origin_summary,
    }


def validate_relocated_function_split(
    reference: Path, candidate: Path,
    final_layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate an admitted Cortex-M hot/cold split as one firmware image."""
    reference_inventory = elf_inventory(reference)
    candidate_inventory = elf_inventory(candidate)
    if reference_inventory["segments"] != candidate_inventory["segments"]:
        raise RuntimeError(
            "function-split candidate changed the loadable segment map"
        )
    reference_text = reference_inventory["sections"][".text"]
    candidate_text = candidate_inventory["sections"][".text"]
    for field in ("address", "offset", "size", "flags", "alignment"):
        if reference_text[field] != candidate_text[field]:
            raise RuntimeError(
                f"function-split .text changed qualified {field}"
            )
    old_text = candidate_inventory["sections"].get(".bolt.org.text")
    if old_text is None or int(old_text["size"]) != 0:
        raise RuntimeError(
            "function-split candidate retained a live original text body"
        )

    moved = {
        name: symbol_address(candidate, name)
        for name in (
            "split_worker", "split_worker.cold.0", "Reset_Handler",
            "fixture_halt",
        )
    }
    expected_moved = {
        "split_worker": 0x08000100,
        "split_worker.cold.0": 0x0800010C,
        "Reset_Handler": 0x08000110,
        "fixture_halt": 0x08000128,
    }
    if moved != expected_moved:
        raise RuntimeError(f"unexpected function-split placement: {moved}")
    with candidate.open("rb") as stream:
        symtab = ELFFile(stream).get_section_by_name(".symtab")
        names = (
            {symbol.name for symbol in symtab.iter_symbols()}
            if symtab is not None else set()
        )
    if "Reset_Handler.cold.0" in names:
        raise RuntimeError("vector-reachable Reset_Handler was split")

    reset_pointer = moved["Reset_Handler"] | 1
    if candidate_inventory["entry"] != reset_pointer:
        raise RuntimeError("split candidate ELF entry lost Thumb state")
    reference_vector = struct.unpack(
        "<II", virtual_bytes(reference, 0x08000000, 8)
    )
    candidate_vector = struct.unpack(
        "<II", virtual_bytes(candidate, 0x08000000, 8)
    )
    if (
        candidate_vector[0] != reference_vector[0]
        or candidate_vector[1] != reset_pointer
    ):
        raise RuntimeError("split candidate vector did not follow reset")

    text_relocations = static_relocation_records(candidate, ".rel.text")
    vector_relocations = static_relocation_records(
        candidate, ".rel.isr_vector"
    )
    expected_text_relocations = [
        (0x0800011A, 10, "split_worker", 0x08000101),
        (0x08000122, 10, "split_worker", 0x08000101),
        (0x08000128, 30, "fixture_halt", 0x08000128),
    ]
    observed_text_relocations = [
        (
            record["offset"], record["type"], record["symbol"],
            record["symbol_value"],
        )
        for record in text_relocations
    ]
    if observed_text_relocations != expected_text_relocations:
        raise RuntimeError(
            "function-split .rel.text records do not match moved identities"
        )
    if [
        (
            record["offset"], record["type"], record["symbol"],
            record["symbol_value"],
        )
        for record in vector_relocations
    ] != [(0x08000004, 2, "Reset_Handler", reset_pointer)]:
        raise RuntimeError("split vector relocation lost the reset identity")

    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoder.detail = True
    repaired_control_transfers = []
    for address, expected_mnemonic, expected_target in (
        (0x08000102, "bne.w", moved["split_worker.cold.0"]),
        (0x0800011A, "bl", moved["split_worker"]),
        (0x08000122, "bl", moved["split_worker"]),
        (0x08000128, "b.w", moved["fixture_halt"]),
    ):
        instruction = decode_one_thumb(decoder, candidate, address)
        target = (
            int(instruction.operands[0].imm)
            if instruction.operands
            and instruction.operands[0].type == ARM_OP_IMM else None
        )
        repaired_control_transfers.append({
            "address": address,
            "mnemonic": instruction.mnemonic,
            "target": target,
        })
        if (
            instruction.mnemonic != expected_mnemonic
            or target != expected_target
        ):
            raise RuntimeError(
                f"split control transfer at 0x{address:x} does not target "
                f"0x{expected_target:x}"
            )

    origin_summary = None
    fragment_summary = None
    if final_layout is not None:
        if int(final_layout.get("split_function_count", -1)) != 1:
            raise RuntimeError("final layout did not record one split function")
        worker = next(
            function for function in final_layout["functions"]
            if function["name"] == "split_worker"
        )
        reset = next(
            function for function in final_layout["functions"]
            if function["name"] == "Reset_Handler"
        )
        expected_fragments = [
            {
                "fragment_number": 0,
                "kind": "main",
                "output_address": 0x08000100,
                "output_size": 10,
                "block_count": 2,
                "block_original_addresses": [0x0800011C, 0x08000120],
                "block_output_addresses": [0x08000100, 0x08000106],
            },
            {
                "fragment_number": 1,
                "kind": "cold",
                "output_address": 0x0800010C,
                "output_size": 4,
                "block_count": 1,
                "block_original_addresses": [0x08000124],
                "block_output_addresses": [0x0800010C],
            },
        ]
        if worker.get("fragments") != expected_fragments:
            raise RuntimeError(
                f"split fragment ledger is not exact: {worker.get('fragments')}"
            )
        expected_origins = [
            (0x0800011C, 0x08000100, 2, "original_instruction"),
            (0x0800011E, 0x08000102, 4, "original_instruction"),
            (0x08000120, 0x08000106, 2, "original_instruction"),
            (0x08000122, 0x08000108, 2, "original_instruction"),
            (0x08000124, 0x0800010C, 2, "original_instruction"),
            (0x08000126, 0x0800010E, 2, "original_instruction"),
        ]
        observed_origins = [
            (
                int(origin["original_address"]),
                int(origin["output_address"]),
                int(origin["output_size"]), origin["origin_kind"],
            )
            for origin in worker["instruction_origins"]
        ]
        if observed_origins != expected_origins:
            raise RuntimeError(
                f"split instruction-origin map is not exact: "
                f"{observed_origins}"
            )
        if (
            worker["firmware_modification_reasons"]
            != ["function_splitting"]
            or worker["firmware_publication_rejections"]
            or not worker["firmware_modified"]
        ):
            raise RuntimeError("split pass result was not recorded exactly")
        reset_fragments = reset.get("fragments", [])
        if (
            reset["firmware_modification_reasons"]
            or len(reset_fragments) != 1
            or reset_fragments[0]["kind"] != "main"
            or int(reset_fragments[0]["output_address"]) != 0x08000110
            or int(reset_fragments[0]["output_size"]) != 28
        ):
            raise RuntimeError("protected reset fragment ledger is invalid")
        origin_summary = observed_origins
        fragment_summary = expected_fragments

    return {
        "status": "accepted",
        "placement_policy": "reuse_linker_qualified_input_text",
        "split_policy": "profile2_hot_cold_with_explicit_contract",
        "program_headers_exact": True,
        "text_range": {
            "address": candidate_text["address"],
            "size": candidate_text["size"],
        },
        "retained_original_text_bytes": int(old_text["size"]),
        "entry": candidate_inventory["entry"],
        "vector_words": list(candidate_vector),
        "moved_symbols": moved,
        "text_relocations": text_relocations,
        "vector_relocations": vector_relocations,
        "repaired_control_transfers": repaired_control_transfers,
        "fragment_summary": fragment_summary,
        "origin_summary": origin_summary,
    }


def validate_relocated_cdsplit(
    reference: Path, candidate: Path,
    final_layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate cache-directed three-way splitting as one firmware image."""
    reference_inventory = elf_inventory(reference)
    candidate_inventory = elf_inventory(candidate)
    if reference_inventory["segments"] != candidate_inventory["segments"]:
        raise RuntimeError("CDSplit candidate changed the loadable segment map")
    reference_text = reference_inventory["sections"][".text"]
    candidate_text = candidate_inventory["sections"][".text"]
    for field in ("address", "offset", "size", "flags", "alignment"):
        if reference_text[field] != candidate_text[field]:
            raise RuntimeError(f"CDSplit .text changed qualified {field}")
    old_text = candidate_inventory["sections"].get(".bolt.org.text")
    if old_text is None or int(old_text["size"]) != 0:
        raise RuntimeError("CDSplit candidate retained a live original body")

    moved = {
        name: symbol_address(candidate, name)
        for name in (
            "cdsplit_worker", "cdsplit_worker.warm",
            "cdsplit_worker.cold", "Reset_Handler", "fixture_halt",
        )
    }
    expected_moved = {
        "cdsplit_worker": 0x08000100,
        "cdsplit_worker.warm": 0x08000130,
        "cdsplit_worker.cold": 0x08000184,
        "Reset_Handler": 0x0800010C,
        "fixture_halt": 0x0800012C,
    }
    if moved != expected_moved:
        raise RuntimeError(f"unexpected CDSplit placement: {moved}")
    with candidate.open("rb") as stream:
        symtab = ELFFile(stream).get_section_by_name(".symtab")
        names = (
            {symbol.name for symbol in symtab.iter_symbols()}
            if symtab is not None else set()
        )
    if any(
        name in names
        for name in ("Reset_Handler.warm", "Reset_Handler.cold")
    ):
        raise RuntimeError("vector-reachable Reset_Handler was fragmented")

    reset_pointer = moved["Reset_Handler"] | 1
    if candidate_inventory["entry"] != reset_pointer:
        raise RuntimeError("CDSplit candidate ELF entry lost Thumb state")
    reference_vector = struct.unpack(
        "<II", virtual_bytes(reference, 0x08000000, 8)
    )
    candidate_vector = struct.unpack(
        "<II", virtual_bytes(candidate, 0x08000000, 8)
    )
    if (
        candidate_vector[0] != reference_vector[0]
        or candidate_vector[1] != reset_pointer
    ):
        raise RuntimeError("CDSplit vector did not follow the reset handler")

    text_relocations = static_relocation_records(candidate, ".rel.text")
    vector_relocations = static_relocation_records(
        candidate, ".rel.isr_vector"
    )
    observed_text_relocations = [
        (
            record["offset"], record["type"], record["symbol"],
            record["symbol_value"],
        )
        for record in text_relocations
    ]
    expected_text_relocations = [
        (0x08000116, 10, "cdsplit_worker", 0x08000101),
        (0x0800011E, 10, "cdsplit_worker", 0x08000101),
        (0x08000126, 10, "cdsplit_worker", 0x08000101),
        (0x0800012C, 30, "fixture_halt", 0x0800012C),
    ]
    if observed_text_relocations != expected_text_relocations:
        raise RuntimeError(
            "CDSplit .rel.text does not describe the live moved identities"
        )
    if [
        (
            record["offset"], record["type"], record["symbol"],
            record["symbol_value"],
        )
        for record in vector_relocations
    ] != [(0x08000004, 2, "Reset_Handler", reset_pointer)]:
        raise RuntimeError("CDSplit vector relocation lost reset identity")

    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoder.detail = True
    repaired_control_transfers = []
    for address, expected_mnemonic, expected_target in (
        (0x08000102, "bge.w", moved["cdsplit_worker.warm"]),
        (0x08000134, "ble.w", moved["cdsplit_worker.cold"]),
        (0x08000180, "b.w", 0x0800010A),
        (0x0800018C, "b.w", 0x0800010A),
        (0x08000116, "bl", moved["cdsplit_worker"]),
        (0x0800011E, "bl", moved["cdsplit_worker"]),
        (0x08000126, "bl", moved["cdsplit_worker"]),
        (0x0800012C, "b.w", moved["fixture_halt"]),
    ):
        instruction = decode_one_thumb(decoder, candidate, address)
        target = (
            int(instruction.operands[0].imm)
            if instruction.operands
            and instruction.operands[0].type == ARM_OP_IMM else None
        )
        repaired_control_transfers.append({
            "address": address,
            "mnemonic": instruction.mnemonic,
            "target": target,
        })
        if instruction.mnemonic != expected_mnemonic or target != expected_target:
            raise RuntimeError(
                f"CDSplit transfer at 0x{address:x} does not target "
                f"0x{expected_target:x}"
            )

    fragment_summary = None
    origin_summary = None
    if final_layout is not None:
        if int(final_layout.get("split_function_count", -1)) != 1:
            raise RuntimeError("CDSplit layout did not record one split function")
        worker = next(
            function for function in final_layout["functions"]
            if function["name"] == "cdsplit_worker"
        )
        reset = next(
            function for function in final_layout["functions"]
            if function["name"] == "Reset_Handler"
        )
        expected_fragments = [
            {
                "fragment_number": 0,
                "kind": "main",
                "output_address": 0x08000100,
                "output_size": 12,
                "block_count": 3,
                "block_original_addresses": [
                    0x08000100, 0x08000104, 0x08000162,
                ],
                "block_output_addresses": [
                    0x08000100, 0x08000106, 0x0800010A,
                ],
            },
            {
                "fragment_number": 1,
                "kind": "cold",
                "output_address": 0x08000184,
                "output_size": 12,
                "block_count": 1,
                "block_original_addresses": [0x0800010E],
                "block_output_addresses": [0x08000184],
            },
            {
                "fragment_number": 2,
                "kind": "warm",
                "output_address": 0x08000130,
                "output_size": 84,
                "block_count": 5,
                "block_original_addresses": [
                    0x08000108, 0x08000118, 0x0800012A,
                    0x0800013C, 0x0800014E,
                ],
                "block_output_addresses": [
                    0x08000130, 0x08000138, 0x0800014A,
                    0x0800015C, 0x0800016E,
                ],
            },
        ]
        if worker.get("fragments") != expected_fragments:
            raise RuntimeError(
                f"CDSplit fragment ledger is not exact: "
                f"{worker.get('fragments')}"
            )
        origins = worker["instruction_origins"]
        reference_instructions = list(decoder.disasm(
            virtual_bytes(reference, 0x08000100, 100), 0x08000100,
        ))
        original_addresses = sorted(
            int(origin["original_address"]) for origin in origins
        )
        expected_original_addresses = sorted(
            int(instruction.address) for instruction in reference_instructions
        )
        output_addresses = [int(origin["output_address"]) for origin in origins]
        if (
            len(origins) != 49
            or original_addresses != expected_original_addresses
            or len(set(output_addresses)) != 49
            or any(
                origin["origin_kind"] != "original_instruction"
                for origin in origins
            )
            or any(
                decode_one_thumb(
                    decoder, candidate, int(origin["output_address"])
                ).size != int(origin["output_size"])
                for origin in origins
            )
        ):
            raise RuntimeError(
                "CDSplit did not retain a complete bijective instruction map"
            )
        if (
            worker["firmware_modification_reasons"]
            != ["block_reordering", "function_splitting"]
            or worker["firmware_publication_rejections"]
            or not worker["firmware_modified"]
        ):
            raise RuntimeError("CDSplit pass result was not recorded exactly")
        reset_fragments = reset.get("fragments", [])
        if (
            reset["firmware_modification_reasons"]
            or len(reset_fragments) != 1
            or reset_fragments[0]["kind"] != "main"
            or int(reset_fragments[0]["output_address"]) != 0x0800010C
            or int(reset_fragments[0]["output_size"]) != 36
        ):
            raise RuntimeError("protected reset fragment ledger is invalid")
        fragment_summary = expected_fragments
        origin_summary = {
            "count": len(origins),
            "original_addresses_exact": True,
            "output_addresses_unique": True,
        }

    return {
        "status": "accepted",
        "placement_policy": "reuse_linker_qualified_input_text",
        "split_policy": "cdsplit_hot_warm_cold_with_compact_branch_model",
        "program_headers_exact": True,
        "text_range": {
            "address": candidate_text["address"],
            "size": candidate_text["size"],
        },
        "retained_original_text_bytes": int(old_text["size"]),
        "entry": candidate_inventory["entry"],
        "vector_words": list(candidate_vector),
        "moved_symbols": moved,
        "text_relocations": text_relocations,
        "vector_relocations": vector_relocations,
        "repaired_control_transfers": repaired_control_transfers,
        "fragment_summary": fragment_summary,
        "origin_summary": origin_summary,
    }


def build_m3_fixture(output: Path, logs: Path) -> Path:
    image = output / "firm-bolt-cortex-m3-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}", str(M3_SOURCE),
        "-o", str(image),
    ]
    require_success(run(command, logs / "build-m3.log"), "M3 fixture build")
    return image


def build_m3_function_reorder_fixture(
    output: Path, logs: Path, *, variant: str = "user",
) -> Path:
    """Build the relocation-complete whole-image ordering fixture."""
    variant_names = {
        "user": "function-reorder",
        "exec-count": "profile-function-reorder",
        "cdsort": "callgraph-function-reorder",
    }
    if variant not in variant_names:
        raise ValueError(f"unknown function-reorder fixture variant {variant}")
    stem = variant_names[variant]
    object_path = output / f"firm-bolt-cortex-m3-{stem}.o"
    image = output / f"firm-bolt-cortex-m3-{stem}-reference.elf"
    require_success(run([
        str(LLVM_MC), "-filetype=obj", "-triple=thumbv7m-none-eabi",
        "-mcpu=cortex-m3", str(M3_FUNCTION_REORDER_SOURCE),
        "-o", str(object_path),
    ], logs / f"build-m3-{stem}-object.log"),
        "M3 function-reorder object build")
    require_success(run([
        str(LD_LLD), "--emit-relocs", "-T",
        str(M3_FUNCTION_REORDER_LINKER), str(object_path), "-o", str(image),
    ], logs / f"build-m3-{stem}-image.log"),
        "M3 function-reorder image build")
    return image


def build_m3_function_split_fixture(output: Path, logs: Path) -> Path:
    """Build the relocation-complete hot/cold splitting fixture."""
    object_path = output / "firm-bolt-cortex-m3-function-split.o"
    image = output / "firm-bolt-cortex-m3-function-split-reference.elf"
    require_success(run([
        str(LLVM_MC), "-filetype=obj", "-triple=thumbv7m-none-eabi",
        "-mcpu=cortex-m3", str(M3_FUNCTION_SPLIT_SOURCE),
        "-o", str(object_path),
    ], logs / "build-m3-function-split-object.log"),
        "M3 function-split object build")
    require_success(run([
        str(LD_LLD), "--emit-relocs", "-T",
        str(M3_FUNCTION_SPLIT_LINKER), str(object_path), "-o", str(image),
    ], logs / "build-m3-function-split-image.log"),
        "M3 function-split image build")
    return image


def build_m3_cdsplit_fixture(output: Path, logs: Path) -> Path:
    """Build the relocation-complete hot/warm/cold CDSplit fixture."""
    object_path = output / "firm-bolt-cortex-m3-cdsplit.o"
    image = output / "firm-bolt-cortex-m3-cdsplit-reference.elf"
    require_success(run([
        str(LLVM_MC), "-filetype=obj", "-triple=thumbv7m-none-eabi",
        "-mcpu=cortex-m3", str(M3_CDSPLIT_SOURCE),
        "-o", str(object_path),
    ], logs / "build-m3-cdsplit-object.log"),
        "M3 CDSplit object build")
    require_success(run([
        str(LD_LLD), "--emit-relocs", "-T", str(M3_CDSPLIT_LINKER),
        str(object_path), "-o", str(image),
    ], logs / "build-m3-cdsplit-image.log"),
        "M3 CDSplit image build")
    return image


def write_m3_profile(output: Path) -> Path:
    """Freeze a no-LBR profile that makes profiled_worker's taken arm hot.

    The profile is intentionally function-relative and tied to the checked-in
    fixture.  It exercises BOLT's ordinary ext-tsp block layout through the
    same compact firmware publication and architectural execution gates as
    the rest of the Cortex-M3 backend qualification.
    """
    profile = output / "firm-bolt-cortex-m3-training.fdata"
    profile.write_text(
        "no_lbr\n"
        "1 profiled_worker 0 100\n"  # cmp
        "1 profiled_worker 2 100\n"  # beq (taken in fixture execution)
        "1 profiled_worker 4 1\n"    # cold movs
        "1 profiled_worker 6 1\n"    # cold return
        "1 profiled_worker 8 100\n"  # hot movs
        "1 profiled_worker a 100\n"  # hot return
    )
    return profile


def build_m3_jump6_fixture(output: Path, logs: Path) -> Path:
    """Materialize a conforming retained R_ARM_THM_JUMP6 executable.

    LLVM MC intentionally relaxes an unresolved CBZ/CBNZ to a hint, and LLD
    does not implement relocation 52 in the pinned revision.  The checked-in
    ELF schema therefore captures the resolved post-link form directly rather
    than hiding that independent producer limitation.
    """
    image = output / "firm-bolt-cortex-m3-jump6-reference.elf"
    command = [str(YAML2OBJ), str(M3_JUMP6_YAML), "-o", str(image)]
    require_success(
        run(command, logs / "build-m3-jump6.log"),
        "M3 R_ARM_THM_JUMP6 fixture build",
    )
    return image


def build_m3_narrow_data_fixture(output: Path, logs: Path) -> Path:
    """Build an executable-flash ABS8/ABS16 relocation fixture."""
    image = output / "firm-bolt-cortex-m3-narrow-data-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", "-Wl,--defsym,answer8=21",
        "-Wl,--defsym,answer16=21", f"-Wl,-T,{M3_LINKER}",
        str(M3_NARROW_DATA_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-narrow-data.log"),
        "M3 R_ARM_ABS8/R_ARM_ABS16 fixture build",
    )
    return image


def build_m3_inline_fixture(output: Path, logs: Path) -> Path:
    """Build the executed native-BOLT Thumb inlining fixture."""
    image = output / "firm-bolt-cortex-m3-inline-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M3_INLINE_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-inline.log"),
        "M3 native inlining fixture build",
    )
    return image


def build_m3_taildup_fixture(output: Path, logs: Path) -> Path:
    """Build the executed native-BOLT Thumb tail-duplication fixture."""
    image = output / "firm-bolt-cortex-m3-taildup-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M3_TAILDUP_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-taildup.log"),
        "M3 native tail-duplication fixture build",
    )
    return image


def write_m3_taildup_profile(output: Path) -> Path:
    """Freeze exact function-relative counts for the tail-dup fixture."""
    profile = output / "firm-bolt-cortex-m3-taildup-training.fdata"
    profile.write_text(
        "no_lbr\n"
        "1 taildup_worker 0 100\n"
        "1 taildup_worker 4 100\n"
        "1 taildup_worker a 1\n"
        "1 taildup_worker e 100\n"
    )
    return profile


def build_m3_taildup_propagation_fixture(output: Path, logs: Path) -> Path:
    """Build the checked register-only tail-dup propagation fixture."""
    image = output / "firm-bolt-cortex-m3-taildup-propagation-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M3_TAILDUP_PROPAGATION_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-taildup-propagation.log"),
        "M3 tail-duplication propagation fixture build",
    )
    return image


def build_m3_rodata_load_fixture(output: Path, logs: Path) -> Path:
    """Build linked-rodata and unresolved-local-pool execution controls."""
    object_file = output / "firm-bolt-cortex-m3-rodata-load.o"
    image = output / "firm-bolt-cortex-m3-rodata-load-reference.elf"
    assemble = [
        str(LLVM_MC), "-filetype=obj", "-triple=thumbv7m-none-eabi",
        "-mcpu=cortex-m3", str(M3_RODATA_LOAD_SOURCE),
        "-o", str(object_file),
    ]
    require_success(
        run(assemble, logs / "build-m3-rodata-load-object.log"),
        "M3 read-only-load fixture assembly",
    )
    link = [
        str(LD_LLD), "--emit-relocs", "-T", str(M3_RODATA_LOAD_LINKER),
        "-e", "Reset_Handler", str(object_file), "-o", str(image),
    ]
    require_success(
        run(link, logs / "build-m3-rodata-load-link.log"),
        "M3 read-only-load fixture link",
    )
    return image


def write_m3_taildup_propagation_profile(output: Path) -> Path:
    """Freeze exact counts for checked tail-dup propagation."""
    profile = output / "firm-bolt-cortex-m3-taildup-propagation-training.fdata"
    profile.write_text(
        "no_lbr\n"
        "1 taildup_propagate 0 100\n"
        "1 taildup_propagate 4 100\n"
        "1 taildup_propagate c 1\n"
        "1 taildup_propagate 10 100\n"
    )
    return profile


def build_m3_loop_inversion_fixture(output: Path, logs: Path) -> Path:
    """Build the executed native-BOLT Thumb loop-inversion fixture."""
    image = output / "firm-bolt-cortex-m3-loop-inversion-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M3_LOOP_INVERSION_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-loop-inversion.log"),
        "M3 native loop-inversion fixture build",
    )
    return image


def write_m3_loop_inversion_profile(output: Path) -> Path:
    """Freeze exact function-relative counts for the loop fixture."""
    profile = output / "firm-bolt-cortex-m3-loop-inversion-training.fdata"
    profile.write_text(
        "no_lbr\n"
        "1 loop_worker 0 100\n"
        "1 loop_worker 4 500\n"
        "1 loop_worker 8 400\n"
        "1 loop_worker c 100\n"
    )
    return profile


def build_m3_peephole_fixture(output: Path, logs: Path) -> Path:
    """Build the executed native-BOLT Thumb control-flow peephole fixture."""
    image = output / "firm-bolt-cortex-m3-peephole-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M3_PEEPHOLE_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-peephole.log"),
        "M3 native control-flow peephole fixture build",
    )
    return image


def build_m3_icp_fixture(output: Path, logs: Path) -> Path:
    """Build the executed native-BOLT Thumb indirect-call fixture."""
    image = output / "firm-bolt-cortex-m3-icp-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M3_ICP_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-icp.log"),
        "M3 native indirect-call promotion fixture build",
    )
    return image


def build_m0_icp_fixture(output: Path, logs: Path) -> Path:
    """Build the executed Thumb-1 indirect-call promotion fixture."""
    image = output / "firm-bolt-cortex-m0plus-icp-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m0plus", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M0_ICP_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m0plus-icp.log"),
        "M0+ native indirect-call promotion fixture build",
    )
    return image


def write_m0_icp_profile(output: Path) -> Path:
    """Freeze the exact hot target for the Thumb-1 callsite."""
    profile = output / "firm-bolt-cortex-m0plus-icp-training.fdata"
    profile.write_text(
        "1 indirect_caller 2 1 promoted_target 0 0 100\n"
    )
    return profile


def build_m0_icp_tail_fixture(output: Path, logs: Path) -> Path:
    """Build the executed Thumb-1 indirect-tail-call fixture."""
    image = output / "firm-bolt-cortex-m0plus-icp-tail-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m0plus", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M0_ICP_TAIL_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m0plus-icp-tail.log"),
        "M0+ native indirect-tail-call promotion fixture build",
    )
    return image


def write_m0_icp_tail_profile(output: Path) -> Path:
    """Freeze the exact hot target for the Thumb-1 tail callsite."""
    profile = output / "firm-bolt-cortex-m0plus-icp-tail-training.fdata"
    profile.write_text(
        "1 tail_dispatch 4 1 promoted_target 0 0 100\n"
    )
    return profile


def build_m0_icp_method_load_fixture(output: Path, logs: Path) -> Path:
    """Build the executed Thumb-1 immutable-vtable ICP fixture."""
    image = output / "firm-bolt-cortex-m0plus-icp-method-load-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m0plus", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M0_ICP_METHOD_LOAD_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m0plus-icp-method-load.log"),
        "M0+ native method-load-elimination fixture build",
    )
    return image


def write_m0_icp_method_load_profile(output: Path) -> Path:
    """Freeze one hot target and exact Thumb-1 immutable method slot."""
    profile = output / "firm-bolt-cortex-m0plus-icp-method-load-training.fdata"
    profile.write_text(
        "1 virtual_caller 6 1 promoted_target 0 0 100\n"
        "4 virtual_caller 4 4 immutable_hot_vtable 0 100\n"
    )
    return profile


def write_m3_icp_profile(output: Path) -> Path:
    """Freeze the exact profiled target for the indirect callsite."""
    profile = output / "firm-bolt-cortex-m3-icp-training.fdata"
    profile.write_text(
        "1 indirect_caller 2 1 promoted_target 0 0 100\n"
    )
    return profile


def build_m3_icp_r12_fixture(output: Path, logs: Path) -> Path:
    """Build the executed native-BOLT r12-target indirect-call fixture."""
    image = output / "firm-bolt-cortex-m3-icp-r12-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M3_ICP_R12_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-icp-r12.log"),
        "M3 native r12-target indirect-call promotion fixture build",
    )
    return image


def write_m3_icp_r12_profile(output: Path) -> Path:
    """Freeze the exact target for the r12/IP indirect callsite."""
    profile = output / "firm-bolt-cortex-m3-icp-r12-training.fdata"
    profile.write_text(
        "1 indirect_caller 2 1 promoted_target 0 0 100\n"
    )
    return profile


def build_m3_icp_method_load_fixture(output: Path, logs: Path) -> Path:
    """Build the immutable-vtable method-load-elimination fixture."""
    image = output / "firm-bolt-cortex-m3-icp-method-load-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M3_ICP_METHOD_LOAD_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-icp-method-load.log"),
        "M3 native method-load-elimination fixture build",
    )
    return image


def write_m3_icp_method_load_profile(output: Path) -> Path:
    """Freeze one hot target and its exact immutable method slot."""
    profile = output / "firm-bolt-cortex-m3-icp-method-load-training.fdata"
    profile.write_text(
        "1 virtual_caller 6 1 promoted_target 0 0 100\n"
        "4 virtual_caller 4 4 immutable_hot_vtable 0 100\n"
    )
    return profile


def build_m3_icp_tail_fixture(output: Path, logs: Path) -> Path:
    """Build the executed native-BOLT Thumb indirect-tail-call fixture."""
    image = output / "firm-bolt-cortex-m3-icp-tail-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M3_ICP_TAIL_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-icp-tail.log"),
        "M3 native indirect-tail-call promotion fixture build",
    )
    return image


def write_m3_icp_tail_profile(output: Path) -> Path:
    """Freeze the exact profiled target for the indirect tail callsite."""
    profile = output / "firm-bolt-cortex-m3-icp-tail-training.fdata"
    profile.write_text(
        "1 tail_dispatch 4 1 promoted_tail_target 0 0 100\n"
    )
    return profile


def build_m3_jump_table_icp_fixture(output: Path, logs: Path) -> Path:
    """Build the executed native-BOLT Thumb TBB promotion fixture."""
    image = output / "firm-bolt-cortex-m3-jump-table-icp-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M3_JUMP_TABLE_ICP_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-jump-table-icp.log"),
        "M3 native jump-table promotion fixture build",
    )
    return image


def write_m3_jump_table_icp_profile(output: Path) -> Path:
    """Freeze one hot TBB index and retain two cold fallback indices."""
    profile = output / "firm-bolt-cortex-m3-jump-table-icp-training.fdata"
    profile.write_text(
        "1 jump_table_worker 4 1 jump_table_worker c 0 90\n"
        "1 jump_table_worker 4 1 jump_table_worker 10 5 5\n"
        "1 jump_table_worker 4 1 jump_table_worker 14 5 5\n"
    )
    return profile


def build_m3_abs32_jump_table_icp_fixture(
    output: Path, logs: Path
) -> Path:
    """Build the executed relocation-safe Thumb ABS32 promotion fixture."""
    image = output / "firm-bolt-cortex-m3-abs32-jump-table-icp-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        f"-Wl,-T,{M3_LINKER}",
        str(M3_ABS32_JUMP_TABLE_ICP_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-abs32-jump-table-icp.log"),
        "M3 native ABS32 jump-table promotion fixture build",
    )
    return image


def write_m3_abs32_jump_table_icp_profile(output: Path) -> Path:
    """Freeze one hot ABS32 index and retain two cold fallback indices."""
    profile = (
        output / "firm-bolt-cortex-m3-abs32-jump-table-icp-training.fdata"
    )
    profile.write_text(
        "1 abs32_jump_table_worker 10 1 abs32_jump_table_worker 12 0 90\n"
        "1 abs32_jump_table_worker 10 1 abs32_jump_table_worker 16 5 5\n"
        "1 abs32_jump_table_worker 10 1 abs32_jump_table_worker 1a 5 5\n"
    )
    return profile


def build_m3_detached_abs32_jump_table_icp_fixture(
    output: Path, logs: Path
) -> Path:
    """Build the relocation-complete detached ABS32 execution fixture."""
    image = (
        output
        / "firm-bolt-cortex-m3-detached-abs32-jump-table-icp-reference.elf"
    )
    command = [
        str(ARM_GCC), "-mcpu=cortex-m3", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs",
        f"-Wl,-T,{M3_DETACHED_ABS32_JUMP_TABLE_ICP_LINKER}",
        str(M3_DETACHED_ABS32_JUMP_TABLE_ICP_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m3-detached-abs32-jump-table-icp.log"),
        "M3 detached ABS32 jump-table promotion fixture build",
    )
    return image


def write_m3_detached_abs32_jump_table_icp_profile(output: Path) -> Path:
    """Freeze one hot detached ABS32 index and two cold indices."""
    profile = (
        output
        / "firm-bolt-cortex-m3-detached-abs32-jump-table-icp-training.fdata"
    )
    profile.write_text(
        "1 detached_abs32_jump_table_worker 10 1 "
        "detached_abs32_jump_table_worker 12 0 90\n"
        "1 detached_abs32_jump_table_worker 10 1 "
        "detached_abs32_jump_table_worker 16 5 5\n"
        "1 detached_abs32_jump_table_worker 10 1 "
        "detached_abs32_jump_table_worker 1a 5 5\n"
    )
    return profile


def build_m0_literal_fixture(output: Path, logs: Path) -> Path:
    image = output / "firm-bolt-cortex-m0plus-literal-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m0plus", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M0_LITERAL_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m0plus-literal.log"),
        "M0+ literal fixture build",
    )
    return image


def build_m0_movable_literals_fixture(output: Path, logs: Path) -> Path:
    image = output / "firm-bolt-cortex-m0plus-movable-literals-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m0plus", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M0_MOVABLE_LITERALS_SOURCE),
        "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m0plus-movable-literals.log"),
        "M0+ movable-literal fixture build",
    )
    return image


def build_m0_moved_relocation_fixture(output: Path, logs: Path) -> Path:
    image = output / "firm-bolt-cortex-m0plus-moved-relocation-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m0plus", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M0_MOVED_RELOCATION_SOURCE),
        "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m0plus-moved-relocation.log"),
        "M0+ moved-relocation fixture build",
    )
    return image


def build_m0_relocation_clone_fixture(output: Path, logs: Path) -> Path:
    image = output / "firm-bolt-cortex-m0plus-relocation-clone-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m0plus", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M0_RELOCATION_CLONE_SOURCE),
        "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m0plus-relocation-clone.log"),
        "M0+ relocation-clone fixture build",
    )
    return image


def build_m0_branch_fixture(output: Path, logs: Path) -> Path:
    image = output / "firm-bolt-cortex-m0plus-branch-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m0plus", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M0_BRANCH_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m0plus-branch.log"),
        "M0+ branch-relaxation fixture build",
    )
    return image


def build_m0_call_veneer_fixture(output: Path, logs: Path) -> Path:
    image = output / "firm-bolt-cortex-m0plus-call-veneer-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m0plus", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M0_CALL_VENEER_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m0plus-call-veneer.log"),
        "M0+ call-veneer fixture build",
    )
    return image


def build_m0_reserved_veneer_fixture(output: Path, logs: Path) -> Path:
    image = output / "firm-bolt-cortex-m0plus-reserved-veneer-reference.elf"
    command = [
        str(ARM_GCC), "-mcpu=cortex-m0plus", "-mthumb", "-nostdlib",
        "-Wl,--emit-relocs", f"-Wl,-T,{M3_LINKER}",
        str(M0_RESERVED_VENEER_SOURCE), "-o", str(image),
    ]
    require_success(
        run(command, logs / "build-m0plus-reserved-veneer.log"),
        "M0+ reserved-veneer fixture build",
    )
    return image


def run_bolt(bolt: Path, image: Path, cpu: str, output: Path,
             logs: Path, full_pipeline: bool = False,
             frame_opt: str = "none",
             report_tag: str | None = None,
             extra_args: tuple[str, ...] = (),
             ) -> tuple[Path, subprocess.CompletedProcess[str]]:
    candidate = output / f"{image.stem}.firm-bolt-{cpu}{image.suffix}"
    pass_report = output / f"bolt-pass-report-{report_tag or cpu}.json"
    command = [str(bolt), str(image), f"--firmbolt-cortex-m-cpu={cpu}"]
    if full_pipeline:
        command.append("--firmbolt-arm-full-pipeline")
        # Reachability under a reconstructed CFG is not permission to remove
        # firmware bytes: alternate entries, asynchronous transfers and
        # instruction-count-driven schedules remain observable. Keep UCE as a
        # separately audited arm instead of enabling BOLT's host default in
        # the production firmware policy.
        command.append("--eliminate-unreachable=false")
    command.append(f"--frame-opt={frame_opt}")
    if full_pipeline:
        command.append(f"--fastdyn-firmware-report={pass_report}")
    command.extend(extra_args)
    command.extend(["--keep-tmp", "-o", str(candidate)])
    process = run(
        command,
        logs / f"bolt-{report_tag or cpu}.log",
    )
    require_success(process, f"Firm-BOLT {cpu}")
    if not candidate.is_file():
        raise RuntimeError(f"Firm-BOLT returned success without {candidate}")
    if full_pipeline and not pass_report.is_file():
        raise RuntimeError(f"Firm-BOLT returned success without {pass_report}")
    if full_pipeline and not Path(f"{pass_report}.layout.json").is_file():
        raise RuntimeError(
            "Firm-BOLT returned success without linker-resolved layout report"
        )
    return candidate, process


def load_runner():
    path = ROOT / "scripts/preflight/run-thumb-hot-fop-pilot.py"
    spec = importlib.util.spec_from_file_location("thumb_hot_fop", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def symbol_address(image: Path, name: str) -> int:
    process = subprocess.run(
        [str(ARM_GCC.parent / "arm-none-eabi-nm"), "--defined-only", str(image)],
        capture_output=True, text=True, check=True,
    )
    for line in process.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[-1] == name:
            return int(fields[0], 16) & ~1
    raise RuntimeError(f"missing symbol {name} in {image}")


def m3_config_text(source: Path, image: Path, ram: Path, qmp: Path,
                   halt: int) -> str:
    start = symbol_address(image, "Reset_Handler")
    text = source.read_text()
    text = text.replace(
        'binary = "tests/sample_binaries/RTOS/RTOSDemo.axf"',
        f'binary = "{image}"', 1,
    )
    text = text.replace('memory_size  = "512M"', 'memory_size  = "64K"', 1)
    text = text.replace(
        'memory_file  = "/tmp/fastdyn-minimal-ram0.ram"',
        f'memory_file  = "{ram}"', 1,
    )
    text = re.sub(
        r"\n\[\[Memory\.ram1\]\].*?(?=\n# ={10,}\n#\s+Machine Configuration)",
        "", text, count=1, flags=re.DOTALL,
    )
    text = re.sub(r"monitor_port = 5555", "monitor_port = 0", text, count=1)
    text = re.sub(
        r'qmp_socket = "/tmp/fastdyn-minimal\.qmp"',
        f'qmp_socket = "{qmp}"', text, count=1,
    )
    text = re.sub(
        r"\n\[\[CPU\.cpu0\.(?:virtuals|modifiers)\]\].*?"
        r"(?=\n\[\[CPU\.cpu0\.(?:virtuals|modifiers)\]\]|"
        r"\n# ={10,}\n#\s+Device Configuration)",
        "", text, flags=re.DOTALL,
    )
    text += (
        "\n[[CPU.cpu0.virtuals]]\n"
        f'at = "0x{start:08x}"\n'
        'instruction = "benchmark_start"\n'
        "\n[[CPU.cpu0.virtuals]]\n"
        f'at = "0x{halt:08x}"\n'
        'instruction = "benchmark_end"\n'
        'args = ["firm-bolt-m3-semantic"]\n'
    )
    return text


def validate_m3_execution(reference: Path, candidate: Path, output: Path,
                          fastdyn: Path) -> dict[str, Any]:
    run_root = Path(tempfile.mkdtemp(prefix="m3-fastdyn-semantic-", dir=output))
    halt = symbol_address(reference, "fixture_halt")
    expected_words = (
        43, 14, 20, 22, 43, 31, 52,
        0x80, 0xFFFFFFFE, 0x8001, 0xFFFFFF80,
        0x11223344, 2,
    )
    expected = struct.pack("<IIIIIIIIIIIII", *expected_words)
    results = []
    short_parent = Path(tempfile.mkdtemp(prefix="fbm3-", dir="/tmp"))
    short_root = short_parent / "runs"
    short_root.symlink_to(run_root, target_is_directory=True)
    try:
        for name, image in (("reference", reference), ("candidate", candidate)):
            directory = short_root / name
            directory.mkdir()
            ram = directory / "ram0.ram"
            config = directory / "run.toml"
            config.write_text(m3_config_text(
                ROOT / "configs/minimal.toml", image, ram,
                directory / "qmp.sock", halt,
            ))
            process = subprocess.run(
                [str(fastdyn), "-q", "run", "-c", str(config),
                 "-o", str(directory / "fastdyn")],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            observed = ram.read_bytes()[:52] if ram.is_file() else b""
            results.append({
                "variant": name,
                "returncode": process.returncode,
                "halt_pc": halt,
                "ram_words": list(struct.unpack("<IIIIIIIIIIIII", observed))
                if len(observed) == 52 else None,
                "ram_prefix_hex": observed.hex(),
                "stdout": process.stdout.splitlines(),
                "stderr": process.stderr.splitlines(),
            })
    finally:
        short_root.unlink(missing_ok=True)
        short_parent.rmdir()
    accepted = all(
        item["returncode"] == 0 and item["ram_prefix_hex"] == expected.hex()
        for item in results
    )
    return {
        "status": "accepted" if accepted else "rejected",
        "reason": None if accepted else "architectural_result_mismatch",
        "expected_ram_words": list(expected_words),
        "runs": results,
    }


def validate_m3_single_word_execution(
        reference: Path, candidate: Path, output: Path, fastdyn: Path,
        workload: str, expected_word: int = 42) -> dict[str, Any]:
    """Execute an Armv7-M fixture expected to write one exact word."""
    run_root = Path(tempfile.mkdtemp(
        prefix=f"m3-fastdyn-{workload}-", dir=output,
    ))
    expected_words = (expected_word,)
    expected = struct.pack("<I", *expected_words)
    results = []
    short_parent = Path(tempfile.mkdtemp(prefix="fbm3w-", dir="/tmp"))
    short_root = short_parent / "runs"
    short_root.symlink_to(run_root, target_is_directory=True)
    try:
        for name, image in (("reference", reference),
                            ("candidate", candidate)):
            # Whole-image relocation may move the exact completion label even
            # when the completed work and instruction identity are unchanged.
            # Bind the benchmark-end hook through each image's symbol rather
            # than reusing the reference PC for the candidate.
            halt = symbol_address(image, "fixture_halt")
            directory = short_root / name
            directory.mkdir()
            ram = directory / "ram0.ram"
            config = directory / "run.toml"
            text = m3_config_text(
                ROOT / "configs/minimal.toml", image, ram,
                directory / "qmp.sock", halt,
            ).replace(
                'args = ["firm-bolt-m3-semantic"]',
                f'args = ["firm-bolt-m3-{workload}"]', 1,
            )
            config.write_text(text)
            process = subprocess.run(
                [str(fastdyn), "-q", "run", "-c", str(config),
                 "-o", str(directory / "fastdyn")],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            observed = ram.read_bytes()[:4] if ram.is_file() else b""
            results.append({
                "variant": name,
                "returncode": process.returncode,
                "halt_pc": halt,
                "ram_words": list(struct.unpack("<I", observed))
                if len(observed) == 4 else None,
                "ram_prefix_hex": observed.hex(),
                "stdout": process.stdout.splitlines(),
                "stderr": process.stderr.splitlines(),
            })
    finally:
        short_root.unlink(missing_ok=True)
        short_parent.rmdir()
    accepted = all(
        item["returncode"] == 0 and item["ram_prefix_hex"] == expected.hex()
        for item in results
    )
    return {
        "status": "accepted" if accepted else "rejected",
        "reason": None if accepted else "architectural_result_mismatch",
        "cpu_model": "cortex-m3",
        "expected_ram_words": list(expected_words),
        "runs": results,
    }


def validate_m3_words_execution(
        reference: Path, candidate: Path, output: Path, fastdyn: Path,
        workload: str, expected_words: tuple[int, ...]) -> dict[str, Any]:
    """Execute an Armv7-M fixture and compare an exact RAM word sequence."""
    run_root = Path(tempfile.mkdtemp(
        prefix=f"m3-fastdyn-{workload}-", dir=output,
    ))
    expected = struct.pack(f"<{len(expected_words)}I", *expected_words)
    results = []
    short_parent = Path(tempfile.mkdtemp(prefix="fbm3ws-", dir="/tmp"))
    short_root = short_parent / "runs"
    short_root.symlink_to(run_root, target_is_directory=True)
    try:
        for name, image in (("reference", reference),
                            ("candidate", candidate)):
            halt = symbol_address(image, "fixture_halt")
            directory = short_root / name
            directory.mkdir()
            ram = directory / "ram0.ram"
            config = directory / "run.toml"
            text = m3_config_text(
                ROOT / "configs/minimal.toml", image, ram,
                directory / "qmp.sock", halt,
            ).replace(
                'args = ["firm-bolt-m3-semantic"]',
                f'args = ["firm-bolt-m3-{workload}"]', 1,
            )
            config.write_text(text)
            process = subprocess.run(
                [str(fastdyn), "-q", "run", "-c", str(config),
                 "-o", str(directory / "fastdyn")],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            observed = ram.read_bytes()[:len(expected)] if ram.is_file() else b""
            results.append({
                "variant": name,
                "returncode": process.returncode,
                "halt_pc": halt,
                "ram_words": list(struct.unpack(
                    f"<{len(expected_words)}I", observed
                )) if len(observed) == len(expected) else None,
                "ram_prefix_hex": observed.hex(),
                "stdout": process.stdout.splitlines(),
                "stderr": process.stderr.splitlines(),
            })
    finally:
        short_root.unlink(missing_ok=True)
        short_parent.rmdir()
    accepted = all(
        item["returncode"] == 0 and item["ram_prefix_hex"] == expected.hex()
        for item in results
    )
    return {
        "status": "accepted" if accepted else "rejected",
        "reason": None if accepted else "architectural_result_mismatch",
        "cpu_model": "cortex-m3",
        "expected_ram_words": list(expected_words),
        "runs": results,
    }


def validate_m3_method_load_fault_execution(
        reference: Path, candidate: Path, output: Path, fastdyn: Path,
        final_layout: dict[str, Any]) -> dict[str, Any]:
    """Compare an invalid-vtable fault through exact original identity.

    The fixture's HardFault handler records the stacked fault PC, CFSR, HFSR
    and MMFAR after two successful dispatches. Raw PCs may differ after compact
    publication; the candidate PC is therefore resolved through the final
    instruction-origin map before the fault state and ordered RAM prefix are
    compared.
    """
    run_root = Path(tempfile.mkdtemp(
        prefix="m3-fastdyn-method-load-fault-", dir=output,
    ))
    worker_start = symbol_address(reference, "virtual_caller")
    original_method_identity = worker_start + 4
    method_worker = next(
        function for function in final_layout["functions"]
        if function["name"] == "virtual_caller"
    )
    candidate_origins = {
        int(origin["output_address"]): origin
        for origin in method_worker["instruction_origins"]
    }
    halt = symbol_address(reference, "fixture_halt")
    runs = []
    short_parent = Path(tempfile.mkdtemp(prefix="fbm3mf-", dir="/tmp"))
    short_root = short_parent / "runs"
    short_root.symlink_to(run_root, target_is_directory=True)
    try:
        for name, image in (("reference", reference),
                            ("candidate", candidate)):
            directory = short_root / name
            directory.mkdir()
            ram = directory / "ram0.ram"
            config = directory / "run.toml"
            text = m3_config_text(
                ROOT / "configs/minimal.toml", image, ram,
                directory / "qmp.sock", halt,
            ).replace(
                'args = ["firm-bolt-m3-semantic"]',
                'args = ["firm-bolt-m3-method-load-fault"]', 1,
            )
            config.write_text(text)
            process = subprocess.run(
                [str(fastdyn), "-q", "run", "-c", str(config),
                 "-o", str(directory / "fastdyn")],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            observed = ram.read_bytes()[:24] if ram.is_file() else b""
            words = list(struct.unpack("<IIIIII", observed)) \
                if len(observed) == 24 else None
            raw_fault_pc = words[2] if words is not None else None
            if name == "reference":
                mapped_identity = raw_fault_pc
                origin_kind = "original_instruction"
            else:
                origin = candidate_origins.get(raw_fault_pc or -1)
                mapped_identity = (
                    origin.get("original_address") if origin else None
                )
                origin_kind = origin.get("origin_kind") if origin else None
            runs.append({
                "variant": name,
                "returncode": process.returncode,
                "halt_pc": halt,
                "ram_words": words,
                "ordered_result_prefix": words[:2] if words else None,
                "raw_stacked_fault_pc": raw_fault_pc,
                "mapped_original_identity": mapped_identity,
                "mapped_origin_kind": origin_kind,
                "cfsr": words[3] if words else None,
                "hfsr": words[4] if words else None,
                "mmfar": words[5] if words else None,
                "stdout": process.stdout.splitlines(),
                "stderr": process.stderr.splitlines(),
            })
    finally:
        short_root.unlink(missing_ok=True)
        short_parent.rmdir()
    reference_run, candidate_run = runs
    fault_state_fields = ("cfsr", "hfsr", "mmfar")
    fault_state_matches = all(
        reference_run[field] == candidate_run[field]
        for field in fault_state_fields
    )
    accepted = (
        all(run["returncode"] == 0 for run in runs)
        and all(run["ordered_result_prefix"] == [17, 19] for run in runs)
        and reference_run["raw_stacked_fault_pc"]
            == original_method_identity
        and all(
            run["mapped_original_identity"] == original_method_identity
            for run in runs
        )
        and candidate_run["mapped_origin_kind"] == "original_instruction"
        and fault_state_matches
        and bool(reference_run["cfsr"] or reference_run["hfsr"])
    )
    return {
        "status": "accepted" if accepted else "rejected",
        "reason": None if accepted else "fault_contract_mismatch",
        "cpu_model": "cortex-m3",
        "fault_contract": {
            "original_method_load_identity": original_method_identity,
            "access_address": 0x60000000,
            "access_width_bytes": 4,
            "ordered_result_prefix": [17, 19],
            "required_state": list(fault_state_fields),
        },
        "fault_state_matches": fault_state_matches,
        "runs": runs,
    }


def validate_m0_method_load_fault_execution(
        reference: Path, candidate: Path, output: Path, fastdyn: Path,
        final_layout: dict[str, Any]) -> dict[str, Any]:
    """Check Thumb-1 hot/cold dispatch and a precise cold-load HardFault.

    The configured 512 MiB SRAM ends at 0x3fffffff. A word load from
    0x3ffffffe crosses that exact mapped boundary and faults at the original
    method-load identity on Cortex-M0. The core does not expose the richer M3
    configurable-fault state, so this contract compares the ordered successful
    prefix, stacked PC through the origin map, access address/width and HFSR.
    """
    run_root = Path(tempfile.mkdtemp(
        prefix="m0-fastdyn-method-load-fault-", dir=output,
    ))
    worker_start = symbol_address(reference, "virtual_caller")
    original_method_identity = worker_start + 4
    method_worker = next(
        function for function in final_layout["functions"]
        if function["name"] == "virtual_caller"
    )
    candidate_origins = {
        int(origin["output_address"]): origin
        for origin in method_worker["instruction_origins"]
    }
    halt = symbol_address(reference, "fixture_halt")
    runs = []
    short_parent = Path(tempfile.mkdtemp(prefix="fbm0mf-", dir="/tmp"))
    short_root = short_parent / "runs"
    short_root.symlink_to(run_root, target_is_directory=True)
    try:
        for name, image in (("reference", reference),
                            ("candidate", candidate)):
            directory = short_root / name
            directory.mkdir()
            ram = directory / "ram0.ram"
            config = directory / "run.toml"
            text = m3_config_text(
                ROOT / "configs/minimal.toml", image, ram,
                directory / "qmp.sock", halt,
            ).replace(
                'cpu = "cortex-m3"', 'cpu = "cortex-m0"', 1,
            ).replace(
                'args = ["firm-bolt-m3-semantic"]',
                'args = ["firm-bolt-m0-method-load-fault"]', 1,
            )
            config.write_text(text)
            process = subprocess.run(
                [str(fastdyn), "-q", "run", "-c", str(config),
                 "-o", str(directory / "fastdyn")],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            observed = ram.read_bytes()[:16] if ram.is_file() else b""
            words = list(struct.unpack("<IIII", observed)) \
                if len(observed) == 16 else None
            raw_fault_pc = words[2] if words is not None else None
            if name == "reference":
                mapped_identity = raw_fault_pc
                origin_kind = "original_instruction"
            else:
                origin = candidate_origins.get(raw_fault_pc or -1)
                mapped_identity = (
                    origin.get("original_address") if origin else None
                )
                origin_kind = origin.get("origin_kind") if origin else None
            runs.append({
                "variant": name,
                "returncode": process.returncode,
                "halt_pc": halt,
                "ram_words": words,
                "ordered_result_prefix": words[:2] if words else None,
                "raw_stacked_fault_pc": raw_fault_pc,
                "mapped_original_identity": mapped_identity,
                "mapped_origin_kind": origin_kind,
                "hfsr": words[3] if words else None,
                "stdout": process.stdout.splitlines(),
                "stderr": process.stderr.splitlines(),
            })
    finally:
        short_root.unlink(missing_ok=True)
        short_parent.rmdir()
    reference_run, candidate_run = runs
    accepted = (
        all(run["returncode"] == 0 for run in runs)
        and all(run["ordered_result_prefix"] == [17, 19] for run in runs)
        and reference_run["raw_stacked_fault_pc"]
            == original_method_identity
        and all(
            run["mapped_original_identity"] == original_method_identity
            for run in runs
        )
        and candidate_run["mapped_origin_kind"] == "original_instruction"
        and reference_run["hfsr"] == candidate_run["hfsr"]
    )
    return {
        "status": "accepted" if accepted else "rejected",
        "reason": None if accepted else "fault_contract_mismatch",
        "cpu_model": "cortex-m0",
        "fault_contract": {
            "original_method_load_identity": original_method_identity,
            "access_address": 0x3FFFFFFE,
            "access_width_bytes": 4,
            "ordered_result_prefix": [17, 19],
            "required_state": ["stacked_pc", "hfsr"],
            "note": "Cortex-M0 lacks the M3 CFSR/MMFAR contract",
        },
        "fault_state_matches": (
            reference_run["hfsr"] == candidate_run["hfsr"]
        ),
        "runs": runs,
    }


def validate_m0_literal_execution(reference: Path, candidate: Path,
                                  output: Path, fastdyn: Path) -> dict[str, Any]:
    """Execute the Thumb-1 literal/ADR identity through the M0 CPU model."""
    run_root = Path(tempfile.mkdtemp(prefix="m0-fastdyn-literal-", dir=output))
    literal_address = symbol_address(reference, "literal_value")
    expected_words = (0x12345678, literal_address)
    expected = struct.pack("<II", *expected_words)
    halt = symbol_address(reference, "fixture_halt")
    results = []
    short_parent = Path(tempfile.mkdtemp(prefix="fbm0-", dir="/tmp"))
    short_root = short_parent / "runs"
    short_root.symlink_to(run_root, target_is_directory=True)
    try:
        for name, image in (("reference", reference), ("candidate", candidate)):
            directory = short_root / name
            directory.mkdir()
            ram = directory / "ram0.ram"
            config = directory / "run.toml"
            text = m3_config_text(
                ROOT / "configs/minimal.toml", image, ram,
                directory / "qmp.sock", halt,
            )
            text = text.replace('cpu = "cortex-m3"', 'cpu = "cortex-m0"', 1)
            text = text.replace(
                'args = ["firm-bolt-m3-semantic"]',
                'args = ["firm-bolt-m0-literal"]',
                1,
            )
            config.write_text(text)
            process = subprocess.run(
                [str(fastdyn), "-q", "run", "-c", str(config),
                 "-o", str(directory / "fastdyn")],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            observed = ram.read_bytes()[:8] if ram.is_file() else b""
            results.append({
                "variant": name,
                "returncode": process.returncode,
                "halt_pc": halt,
                "ram_words": list(struct.unpack("<II", observed))
                if len(observed) == 8 else None,
                "ram_prefix_hex": observed.hex(),
                "stdout": process.stdout.splitlines(),
                "stderr": process.stderr.splitlines(),
            })
    finally:
        short_root.unlink(missing_ok=True)
        short_parent.rmdir()
    accepted = all(
        item["returncode"] == 0 and item["ram_prefix_hex"] == expected.hex()
        for item in results
    )
    return {
        "status": "accepted" if accepted else "rejected",
        "reason": None if accepted else "architectural_result_mismatch",
        "cpu_model": "cortex-m0",
        "expected_ram_words": list(expected_words),
        "runs": results,
    }


def validate_m0_two_word_execution(
    reference: Path, candidate: Path, output: Path, fastdyn: Path,
    *, run_label: str, expected_words: tuple[int, int],
) -> dict[str, Any]:
    """Execute both paths of a Cortex-M0 fixture and compare two RAM words."""
    run_root = Path(tempfile.mkdtemp(
        prefix=f"m0-fastdyn-{run_label}-", dir=output
    ))
    expected = struct.pack("<" + "I" * len(expected_words), *expected_words)
    halt = symbol_address(reference, "fixture_halt")
    results = []
    short_parent = Path(tempfile.mkdtemp(prefix="fbm0w-", dir="/tmp"))
    short_root = short_parent / "runs"
    short_root.symlink_to(run_root, target_is_directory=True)
    try:
        for name, image in (("reference", reference), ("candidate", candidate)):
            directory = short_root / name
            directory.mkdir()
            ram = directory / "ram0.ram"
            config = directory / "run.toml"
            text = m3_config_text(
                ROOT / "configs/minimal.toml", image, ram,
                directory / "qmp.sock", halt,
            )
            text = text.replace('cpu = "cortex-m3"', 'cpu = "cortex-m0"', 1)
            text = text.replace(
                'args = ["firm-bolt-m3-semantic"]',
                f'args = ["firm-bolt-m0-{run_label}"]', 1,
            )
            config.write_text(text)
            process = subprocess.run(
                [str(fastdyn), "-q", "run", "-c", str(config),
                 "-o", str(directory / "fastdyn")],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            observed = ram.read_bytes()[:8] if ram.is_file() else b""
            results.append({
                "variant": name,
                "returncode": process.returncode,
                "halt_pc": halt,
                "ram_words": list(struct.unpack(
                    "<" + "I" * len(expected_words), observed
                )) if len(observed) == len(expected) else None,
                "ram_prefix_hex": observed.hex(),
                "stdout": process.stdout.splitlines(),
                "stderr": process.stderr.splitlines(),
            })
    finally:
        short_root.unlink(missing_ok=True)
        short_parent.rmdir()
    accepted = all(
        item["returncode"] == 0 and item["ram_prefix_hex"] == expected.hex()
        for item in results
    )
    return {
        "status": "accepted" if accepted else "rejected",
        "reason": None if accepted else "architectural_result_mismatch",
        "cpu_model": "cortex-m0",
        "expected_ram_words": list(expected_words),
        "runs": results,
    }


def validate_m0_branch_execution(reference: Path, candidate: Path,
                                 output: Path, fastdyn: Path) -> dict[str, Any]:
    """Execute both paths of the installed Thumb-1 trampoline candidate."""
    run_root = Path(tempfile.mkdtemp(prefix="m0-fastdyn-branch-", dir=output))
    expected_words = (1, 2)
    expected = struct.pack("<II", *expected_words)
    halt = symbol_address(reference, "fixture_halt")
    results = []
    short_parent = Path(tempfile.mkdtemp(prefix="fbm0b-", dir="/tmp"))
    short_root = short_parent / "runs"
    short_root.symlink_to(run_root, target_is_directory=True)
    try:
        for name, image in (("reference", reference), ("candidate", candidate)):
            directory = short_root / name
            directory.mkdir()
            ram = directory / "ram0.ram"
            config = directory / "run.toml"
            text = m3_config_text(
                ROOT / "configs/minimal.toml", image, ram,
                directory / "qmp.sock", halt,
            )
            text = text.replace('cpu = "cortex-m3"', 'cpu = "cortex-m0"', 1)
            text = text.replace(
                'args = ["firm-bolt-m3-semantic"]',
                'args = ["firm-bolt-m0-branch"]',
                1,
            )
            config.write_text(text)
            process = subprocess.run(
                [str(fastdyn), "-q", "run", "-c", str(config),
                 "-o", str(directory / "fastdyn")],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            observed = ram.read_bytes()[:8] if ram.is_file() else b""
            results.append({
                "variant": name,
                "returncode": process.returncode,
                "halt_pc": halt,
                "ram_words": list(struct.unpack("<II", observed))
                if len(observed) == 8 else None,
                "ram_prefix_hex": observed.hex(),
                "stdout": process.stdout.splitlines(),
                "stderr": process.stderr.splitlines(),
            })
    finally:
        short_root.unlink(missing_ok=True)
        short_parent.rmdir()
    accepted = all(
        item["returncode"] == 0 and item["ram_prefix_hex"] == expected.hex()
        for item in results
    )
    return {
        "status": "accepted" if accepted else "rejected",
        "reason": None if accepted else "architectural_result_mismatch",
        "cpu_model": "cortex-m0",
        "expected_ram_words": list(expected_words),
        "runs": results,
    }


def validate_m0_call_veneer_execution(
    reference: Path, candidate: Path, output: Path, fastdyn: Path,
) -> dict[str, Any]:
    """Execute a linked Thumb-1 literal-backed call veneer on Cortex-M0."""
    run_root = Path(tempfile.mkdtemp(prefix="m0-fastdyn-veneer-", dir=output))
    expected_words = (42,)
    expected = struct.pack("<I", *expected_words)
    halt = symbol_address(reference, "fixture_halt")
    results = []
    short_parent = Path(tempfile.mkdtemp(prefix="fbm0v-", dir="/tmp"))
    short_root = short_parent / "runs"
    short_root.symlink_to(run_root, target_is_directory=True)
    try:
        for name, image in (("reference", reference), ("candidate", candidate)):
            directory = short_root / name
            directory.mkdir()
            ram = directory / "ram0.ram"
            config = directory / "run.toml"
            text = m3_config_text(
                ROOT / "configs/minimal.toml", image, ram,
                directory / "qmp.sock", halt,
            )
            text = text.replace('cpu = "cortex-m3"', 'cpu = "cortex-m0"', 1)
            text = text.replace(
                'args = ["firm-bolt-m3-semantic"]',
                'args = ["firm-bolt-m0-call-veneer"]',
                1,
            )
            config.write_text(text)
            process = subprocess.run(
                [str(fastdyn), "-q", "run", "-c", str(config),
                 "-o", str(directory / "fastdyn")],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            observed = ram.read_bytes()[:4] if ram.is_file() else b""
            results.append({
                "variant": name,
                "returncode": process.returncode,
                "halt_pc": halt,
                "ram_words": list(struct.unpack("<I", observed))
                if len(observed) == 4 else None,
                "ram_prefix_hex": observed.hex(),
                "stdout": process.stdout.splitlines(),
                "stderr": process.stderr.splitlines(),
            })
    finally:
        short_root.unlink(missing_ok=True)
        short_parent.rmdir()
    accepted = all(
        item["returncode"] == 0 and item["ram_prefix_hex"] == expected.hex()
        for item in results
    )
    return {
        "status": "accepted" if accepted else "rejected",
        "reason": None if accepted else "architectural_result_mismatch",
        "cpu_model": "cortex-m0",
        "expected_ram_words": list(expected_words),
        "runs": results,
    }


def validate_m4_execution(candidate: Path, output: Path,
                          fastdyn: Path) -> dict[str, Any]:
    runner = load_runner()
    run_root = Path(tempfile.mkdtemp(prefix="m4-fastdyn-smoke-", dir=output))
    base = (ROOT / "configs/minimal.toml").read_text()
    base = base.replace('cpu = "cortex-m3"', 'cpu = "cortex-m4"', 1)
    base_path = run_root / "minimal-cortex-m4.toml"
    base_path.write_text(base)

    # QEMU's Unix socket path is limited to 107 visible bytes.  Keep the
    # durable artifacts under the selected output directory but reach them
    # through a short temporary symlink while the processes are running.
    short_parent = Path(tempfile.mkdtemp(prefix="fbcm-", dir="/tmp"))
    short_root = short_parent / "runs"
    short_root.symlink_to(run_root, target_is_directory=True)
    results = []
    try:
        # Run the immutable reference twice.  A reference/candidate RAM
        # mismatch is only actionable when the timed RTOS harness itself is
        # reproducible under the same admitted environment.
        for name, image in (
            ("reference_a", M4_IMAGE),
            ("reference_b", M4_IMAGE),
            ("candidate", candidate),
        ):
            results.append({
                "variant": name,
                **runner.run_one(
                    fastdyn, base_path, image, short_root / name, 10_000
                ),
            })
    finally:
        short_root.unlink(missing_ok=True)
        short_parent.rmdir()
    completed = all(
        item["returncode"] == 0 and item["completed_work_units"] == 10_000
        for item in results
    )
    reference_repeat_exact = (
        completed and
        results[0]["ram_prefix_sha256"] == results[1]["ram_prefix_sha256"]
    )
    candidate_exact = (
        completed and reference_repeat_exact and
        results[0]["ram_prefix_sha256"] == results[2]["ram_prefix_sha256"]
    )
    return {
        "status": "accepted" if candidate_exact else "rejected",
        "reason": (
            None if candidate_exact else
            "fixed_work_failed" if not completed else
            "reference_schedule_not_reproducible"
            if not reference_repeat_exact else
            "modeled_time_state_mismatch"
        ),
        "cpu_model": "cortex-m4",
        "completed_work_units": 10_000,
        "reference_repeat_exact": reference_repeat_exact,
        "candidate_ram_prefix_exact": candidate_exact,
        "runs": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bolt", type=Path, default=DEFAULT_BOLT)
    parser.add_argument("--fastdyn", type=Path, default=DEFAULT_FASTDYN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-fastdyn", action="store_true")
    parser.add_argument(
        "--full-pipeline", action="store_true",
        help="qualify the normal BOLT optimization pipeline on ARM/Thumb",
    )
    parser.add_argument(
        "--frame-opt", choices=("none", "hot", "all"), default="none",
        help="run BOLT frame optimization using qualified Thumb target hooks",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    logs = output / "commands-and-logs"
    output.mkdir(parents=True, exist_ok=True)

    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=LLVM_SOURCE,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if revision != PINNED_BOLT:
        raise RuntimeError(f"expected BOLT {PINNED_BOLT}, found {revision}")
    if not args.skip_build:
        require_success(run(
            ["cmake", "--build", str(LLVM_BUILD), "--target", "llvm-bolt", "-j8"],
            logs / "build-bolt.log",
        ), "BOLT build")

    m0_literal_image = build_m0_literal_fixture(output, logs)
    m0_movable_literals_image = build_m0_movable_literals_fixture(output, logs)
    m0_moved_relocation_image = build_m0_moved_relocation_fixture(output, logs)
    m0_relocation_clone_image = build_m0_relocation_clone_fixture(output, logs)
    m0_branch_image = build_m0_branch_fixture(output, logs)
    m0_call_veneer_image = build_m0_call_veneer_fixture(output, logs)
    m0_reserved_veneer_image = build_m0_reserved_veneer_fixture(output, logs)
    m0_icp_image = build_m0_icp_fixture(output, logs)
    m0_icp_profile = write_m0_icp_profile(output)
    m0_icp_tail_image = build_m0_icp_tail_fixture(output, logs)
    m0_icp_tail_profile = write_m0_icp_tail_profile(output)
    m0_icp_method_load_image = build_m0_icp_method_load_fixture(output, logs)
    m0_icp_method_load_profile = write_m0_icp_method_load_profile(output)
    m3_image = build_m3_fixture(output, logs)
    m3_profile = write_m3_profile(output)
    m3_jump6_image = build_m3_jump6_fixture(output, logs)
    m3_narrow_data_image = build_m3_narrow_data_fixture(output, logs)
    m3_inline_image = build_m3_inline_fixture(output, logs)
    m3_taildup_image = build_m3_taildup_fixture(output, logs)
    m3_taildup_profile = write_m3_taildup_profile(output)
    m3_taildup_propagation_image = (
        build_m3_taildup_propagation_fixture(output, logs)
    )
    m3_taildup_propagation_profile = (
        write_m3_taildup_propagation_profile(output)
    )
    m3_rodata_load_image = build_m3_rodata_load_fixture(output, logs)
    m3_function_reorder_image = build_m3_function_reorder_fixture(
        output, logs
    )
    m3_profile_function_reorder_image = build_m3_function_reorder_fixture(
        output, logs, variant="exec-count"
    )
    m3_callgraph_function_reorder_image = build_m3_function_reorder_fixture(
        output, logs, variant="cdsort"
    )
    m3_function_split_image = build_m3_function_split_fixture(output, logs)
    m3_cdsplit_image = build_m3_cdsplit_fixture(output, logs)
    m3_loop_inversion_image = build_m3_loop_inversion_fixture(output, logs)
    m3_loop_inversion_profile = write_m3_loop_inversion_profile(output)
    m3_peephole_image = build_m3_peephole_fixture(output, logs)
    m3_icp_image = build_m3_icp_fixture(output, logs)
    m3_icp_profile = write_m3_icp_profile(output)
    m3_icp_r12_image = build_m3_icp_r12_fixture(output, logs)
    m3_icp_r12_profile = write_m3_icp_r12_profile(output)
    m3_icp_method_load_image = build_m3_icp_method_load_fixture(output, logs)
    m3_icp_method_load_profile = write_m3_icp_method_load_profile(output)
    m3_icp_tail_image = build_m3_icp_tail_fixture(output, logs)
    m3_icp_tail_profile = write_m3_icp_tail_profile(output)
    m3_jump_table_icp_image = build_m3_jump_table_icp_fixture(output, logs)
    m3_jump_table_icp_profile = write_m3_jump_table_icp_profile(output)
    m3_abs32_jump_table_icp_image = (
        build_m3_abs32_jump_table_icp_fixture(output, logs)
    )
    m3_abs32_jump_table_icp_profile = (
        write_m3_abs32_jump_table_icp_profile(output)
    )
    m3_detached_abs32_jump_table_icp_image = (
        build_m3_detached_abs32_jump_table_icp_fixture(output, logs)
    )
    m3_detached_abs32_jump_table_icp_profile = (
        write_m3_detached_abs32_jump_table_icp_profile(output)
    )
    cases = [
        ("m0plus", "cortex-m0plus", M0_IMAGE,
         {".cfg": None, ".text": 0xC0}, []),
        ("m0plus_literal", "cortex-m0plus", m0_literal_image,
         {".isr_vector": None}, []),
        ("m0plus_movable_literals", "cortex-m0plus",
         m0_movable_literals_image, {".isr_vector": None}, []),
        ("m0plus_moved_relocation", "cortex-m0plus",
         m0_moved_relocation_image, {".isr_vector": None}, [
             "--firmbolt-allow-literal-address-changes",
         ]),
        ("m0plus_relocation_clone", "cortex-m0plus",
         m0_relocation_clone_image, {".isr_vector": None}, [
             "--firmbolt-preserve-guest-instruction-count=false",
             "--firmbolt-allow-literal-address-changes",
         ]),
        ("m0plus_branch", "cortex-m0plus", m0_branch_image,
         {".isr_vector": None}, [
             "--reorder-blocks=reverse",
             "--firmbolt-preserve-guest-instruction-count=false",
         ]),
        ("m0plus_call_veneer", "cortex-m0plus", m0_call_veneer_image,
         {".isr_vector": None}, [
             "--relax-exp",
             "--max-cluster-size=64",
             "--max-thunk-chain-length=0",
             "--eliminate-unreachable=true",
             "--firmbolt-preserve-guest-instruction-count=false",
         ]),
        ("m0plus_reserved_veneer", "cortex-m0plus",
         m0_reserved_veneer_image, {".isr_vector": None}, [
             "--relax-exp",
             "--max-cluster-size=64",
             "--max-thunk-chain-length=0",
         ]),
        ("m0plus_icp", "cortex-m0plus", m0_icp_image,
         {".isr_vector": None}, [
             f"--data={m0_icp_profile}",
             "--icp=calls",
             "--icp-calls-topn=1",
             "--icp-top-callsites=0",
             "--assume-abi",
             "--firmbolt-allow-indirect-call-promotion",
             "--firmbolt-allow-thumb1-icp-literal-load",
             "--firmbolt-preserve-guest-instruction-count=false",
             "--eliminate-unreachable=true",
         ]),
        ("m0plus_icp_tail", "cortex-m0plus", m0_icp_tail_image,
         {".isr_vector": None}, [
             f"--data={m0_icp_tail_profile}",
             "--icp=calls",
             "--icp-calls-topn=1",
             "--icp-top-callsites=0",
             "--assume-abi",
             "--firmbolt-allow-indirect-call-promotion",
             "--firmbolt-allow-indirect-tail-call-promotion",
             "--firmbolt-allow-thumb1-icp-literal-load",
             "--firmbolt-preserve-guest-instruction-count=false",
             "--eliminate-unreachable=true",
         ]),
        ("m0plus_icp_method_load", "cortex-m0plus",
         m0_icp_method_load_image, {".isr_vector": None}, [
             f"--data={m0_icp_method_load_profile}",
             "--icp=calls",
             "--icp-calls-topn=1",
             "--icp-top-callsites=0",
             "--assume-abi",
             "--firmbolt-allow-indirect-call-promotion",
             "--firmbolt-allow-method-load-elimination",
             "--firmbolt-allow-thumb1-icp-literal-load",
             "--firmbolt-preserve-guest-instruction-count=false",
             "--eliminate-unreachable=true",
         ]),
        ("m3", "cortex-m3", m3_image, {".isr_vector": None}, [
            f"--data={m3_profile}",
            "--reorder-blocks=ext-tsp",
        ] if args.full_pipeline else []),
        ("m3_jump6", "cortex-m3", m3_jump6_image,
         {".isr_vector": None}, []),
        ("m3_narrow_data", "cortex-m3", m3_narrow_data_image,
         {".isr_vector": None}, []),
        ("m3_inline", "cortex-m3", m3_inline_image,
         {".isr_vector": None}, [
             "--force-inline=inline_callee",
             "--firmbolt-allow-inlining",
         ]),
        ("m3_taildup", "cortex-m3", m3_taildup_image,
         {".isr_vector": None}, [
             f"--data={m3_taildup_profile}",
             "--tail-duplication=aggressive",
             "--tail-duplication-minimum-offset=0",
             "--firmbolt-allow-tail-duplication",
             "--firmbolt-preserve-guest-instruction-count=false",
         ]),
        ("m3_taildup_propagation", "cortex-m3",
         m3_taildup_propagation_image, {".isr_vector": None}, [
             f"--data={m3_taildup_propagation_profile}",
             "--tail-duplication=aggressive",
             "--tail-duplication-minimum-offset=0",
             "--firmbolt-allow-tail-duplication",
             "--firmbolt-allow-tail-duplication-propagation",
             "--tail-duplication-const-copy-propagation",
             "--firmbolt-preserve-guest-instruction-count=false",
         ]),
        ("m3_rodata_load", "cortex-m3", m3_rodata_load_image,
         {".isr_vector": None}, [
             "--simplify-rodata-loads",
             "--firmbolt-allow-read-only-load-elision",
         ]),
        ("m3_function_reorder", "cortex-m3", m3_function_reorder_image,
         {}, [
             "--relocs",
             "--use-old-text",
             "--reorder-functions=user",
             f"--function-order={M3_FUNCTION_REORDER_ORDER}",
         ]),
        ("m3_profile_function_reorder", "cortex-m3",
         m3_profile_function_reorder_image, {}, [
             f"--data={M3_FUNCTION_REORDER_PROFILE}",
             "--relocs",
             "--use-old-text",
             "--reorder-functions=exec-count",
         ]),
        ("m3_callgraph_function_reorder", "cortex-m3",
         m3_callgraph_function_reorder_image, {}, [
             f"--data={M3_FUNCTION_REORDER_CALLGRAPH_PROFILE}",
             "--relocs",
             "--use-old-text",
             "--reorder-functions=cdsort",
         ]),
        ("m3_function_split", "cortex-m3", m3_function_split_image, {}, [
             f"--data={M3_FUNCTION_SPLIT_PROFILE}",
             "--relocs",
             "--use-old-text",
             "--merge-text-sections",
             "--align-functions=4",
             "--align-text=4",
             "--split-functions",
             "--split-all-cold",
             "--firmbolt-allow-function-splitting",
         ]),
        ("m3_cdsplit", "cortex-m3", m3_cdsplit_image, {}, [
             f"--data={M3_CDSPLIT_PROFILE}",
             "--relocs",
             "--use-old-text",
             "--merge-text-sections",
             "--align-functions=4",
             "--align-text=4",
             "--reorder-blocks=ext-tsp",
             "--split-functions",
             "--split-strategy=cdsplit",
             "--call-scale=10",
             "--compact-code-model",
             "--firmbolt-allow-function-splitting",
         ]),
        ("m3_loop_inversion", "cortex-m3", m3_loop_inversion_image,
         {".isr_vector": None}, [
             f"--data={m3_loop_inversion_profile}",
             "--firmbolt-allow-loop-inversion",
             "--firmbolt-preserve-guest-instruction-count=false",
         ]),
        ("m3_peephole", "cortex-m3", m3_peephole_image,
         {".isr_vector": None}, [
             "--peepholes=double-jumps",
             "--firmbolt-allow-control-flow-peepholes",
         ]),
        ("m3_icp", "cortex-m3", m3_icp_image,
         {".isr_vector": None}, [
             f"--data={m3_icp_profile}",
             "--icp=calls",
             "--icp-calls-topn=1",
             "--icp-top-callsites=0",
             "--assume-abi",
             "--firmbolt-allow-indirect-call-promotion",
             "--firmbolt-preserve-guest-instruction-count=false",
             "--eliminate-unreachable=true",
         ]),
        ("m3_icp_r12", "cortex-m3", m3_icp_r12_image,
         {".isr_vector": None}, [
             f"--data={m3_icp_r12_profile}",
             "--icp=calls",
             "--icp-calls-topn=1",
             "--icp-top-callsites=0",
             "--assume-abi",
             "--firmbolt-allow-indirect-call-promotion",
             "--firmbolt-preserve-guest-instruction-count=false",
             "--eliminate-unreachable=true",
         ]),
        ("m3_icp_method_load", "cortex-m3", m3_icp_method_load_image,
         {".isr_vector": None}, [
             f"--data={m3_icp_method_load_profile}",
             "--icp=calls",
             "--icp-calls-topn=1",
             "--icp-top-callsites=0",
             "--assume-abi",
             "--firmbolt-allow-indirect-call-promotion",
             "--firmbolt-allow-method-load-elimination",
             "--firmbolt-preserve-guest-instruction-count=false",
             "--eliminate-unreachable=true",
         ]),
        ("m3_icp_tail", "cortex-m3", m3_icp_tail_image,
         {".isr_vector": None}, [
             f"--data={m3_icp_tail_profile}",
             "--icp=calls",
             "--icp-calls-topn=1",
             "--icp-top-callsites=0",
             "--assume-abi",
             "--firmbolt-allow-indirect-call-promotion",
             "--firmbolt-allow-indirect-tail-call-promotion",
             "--firmbolt-preserve-guest-instruction-count=false",
             "--eliminate-unreachable=true",
         ]),
        ("m3_jump_table_icp", "cortex-m3", m3_jump_table_icp_image,
         {".isr_vector": None}, [
             f"--data={m3_jump_table_icp_profile}",
             "--icp=jump-tables",
             "--icp-jt-topn=1",
             "--icp-top-callsites=0",
             "--firmbolt-allow-jump-table-promotion",
             "--firmbolt-preserve-guest-instruction-count=false",
             "--eliminate-unreachable=true",
         ]),
        ("m3_abs32_jump_table_icp", "cortex-m3",
         m3_abs32_jump_table_icp_image, {".isr_vector": None}, [
             f"--data={m3_abs32_jump_table_icp_profile}",
             "--icp=jump-tables",
             "--icp-jt-topn=1",
             "--icp-top-callsites=0",
             "--firmbolt-allow-jump-table-promotion",
             "--firmbolt-preserve-guest-instruction-count=false",
             "--eliminate-unreachable=true",
         ]),
        ("m3_detached_abs32_jump_table_icp", "cortex-m3",
         m3_detached_abs32_jump_table_icp_image,
         {".isr_vector": None}, [
             f"--data={m3_detached_abs32_jump_table_icp_profile}",
             "--icp=jump-tables",
             "--icp-jt-topn=1",
             "--icp-top-callsites=0",
             "--firmbolt-allow-jump-table-promotion",
             "--firmbolt-allow-detached-jump-table-update",
             "--firmbolt-preserve-guest-instruction-count=false",
             "--eliminate-unreachable=true",
         ]),
        ("m4", "cortex-m4", M4_IMAGE, {".isr_vector": None}, []),
    ]
    results: dict[str, Any] = {}
    candidates: dict[str, Path] = {}
    for label, cpu, image, protected, extra_args in cases:
        candidate, process = run_bolt(
            args.bolt.resolve(), image, cpu, output, logs, args.full_pipeline,
            args.frame_opt, label, extra_args,
        )
        candidates[label] = candidate
        pending_icp_validation = None
        native_pass_report = (
            json.loads(
                (output / f"bolt-pass-report-{label}.json").read_text()
            ) if args.full_pipeline else None
        )
        provenance_validation = None
        exclusive_access_validation = None
        if native_pass_report is not None:
            summary = native_pass_report["summary"]
            synthesized = int(
                summary["synthesized_control_edge_instruction_count"]
            )
            verified = int(
                summary[
                    "verified_synthesized_control_edge_instruction_count"
                ]
            )
            unmapped = int(summary["unmapped_instruction_count"])
            if unmapped != 0 or synthesized != verified:
                raise RuntimeError(
                    f"{label} has incomplete emitted provenance: "
                    f"unmapped={unmapped}, synthesized={synthesized}, "
                    f"verified={verified}"
                )
            provenance_validation = {
                "status": "accepted",
                "unmapped_instruction_count": unmapped,
                "synthesized_control_edge_instruction_count": synthesized,
                "verified_synthesized_control_edge_instruction_count":
                    verified,
            }
            exclusive_access_validation = {
                "status": "accepted",
                "exclusive_load_instruction_count": int(
                    summary["exclusive_load_instruction_count"]
                ),
                "exclusive_store_instruction_count": int(
                    summary["exclusive_store_instruction_count"]
                ),
                "exclusive_clear_instruction_count": int(
                    summary["exclusive_clear_instruction_count"]
                ),
            }
            if label == "m4" and exclusive_access_validation != {
                "status": "accepted",
                "exclusive_load_instruction_count": 60,
                "exclusive_store_instruction_count": 60,
                "exclusive_clear_instruction_count": 0,
            }:
                raise RuntimeError(
                    "M4 exclusive-access cohort changed unexpectedly: "
                    f"{exclusive_access_validation}"
                )
            if label == "m3_inline":
                if int(summary["cloned_original_instruction_count"]) != 1:
                    raise RuntimeError(
                        "M3 native inlining fixture did not retain exactly "
                        "one cloned original-instruction identity"
                    )
                if int(summary["firmware_modification_reason_counts"]
                       ["inlining"]) != 1:
                    raise RuntimeError(
                        "M3 native inlining fixture did not publish exactly "
                        "one inlining transformation"
                    )
            if label in {"m3_taildup", "m3_taildup_propagation"}:
                if int(summary["cloned_original_instruction_count"]) != 2:
                    raise RuntimeError(
                        f"{label} did not retain "
                        "exactly two cloned original-instruction identities"
                    )
                if int(summary["firmware_modification_reason_counts"]
                       ["tail_duplication"]) != 1:
                    raise RuntimeError(
                        f"{label} did not publish "
                        "exactly one tail-duplication transformation"
                    )
            if label == "m3_loop_inversion":
                if int(summary["cloned_original_instruction_count"]) != 0:
                    raise RuntimeError(
                        "M3 loop-inversion fixture unexpectedly cloned "
                        "original instruction identities"
                    )
                if int(summary["firmware_modification_reason_counts"]
                       ["loop_inversion"]) != 1:
                    raise RuntimeError(
                        "M3 loop-inversion fixture did not publish exactly "
                        "one loop-inversion transformation"
                    )
            if label == "m3_rodata_load":
                if int(summary["firmware_modification_reason_counts"]
                       ["read_only_load_elision"]) != 2:
                    raise RuntimeError(
                        "M3 read-only-load fixture did not record both the "
                        "linked-data candidate and unresolved-pool attempt"
                    )
                if int(summary[
                    "synthesized_from_original_instruction_count"
                ]) != 2:
                    raise RuntimeError(
                        "M3 read-only-load fixture lost transformed-source "
                        "attribution before publication"
                    )
            if label == "m3_peephole":
                if int(summary["firmware_modification_reason_counts"]
                       ["control_flow_peepholes"]) != 1:
                    raise RuntimeError(
                        "M3 peephole fixture did not publish exactly one "
                        "control-flow peephole transformation"
                    )
            if label in {"m3_function_split", "m3_cdsplit"}:
                if (
                    int(summary["firmware_modification_reason_counts"]
                        ["function_splitting"]) != 1
                    or int(summary["firmware_modified_function_count"]) != 1
                    or int(summary["cloned_original_instruction_count"]) != 0
                    or int(summary["unmapped_instruction_count"]) != 0
                ):
                    raise RuntimeError(
                        f"{label} did not record exactly "
                        "one split function with complete original identities"
                    )
            if label == "m0plus_icp":
                final_layout = json.loads(
                    Path(
                        f"{output / f'bolt-pass-report-{label}.json'}.layout.json"
                    ).read_text()
                )
                icp_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "indirect_caller"
                )
                icp_reasons = set(
                    icp_worker["firmware_modification_reasons"]
                )
                worker_start = symbol_address(
                    m0_icp_image, "indirect_caller"
                )
                call_identity = worker_start + 2
                origins = icp_worker["instruction_origins"]
                observed = [
                    (
                        int(origin["output_address"]) - worker_start,
                        origin["origin_kind"],
                        origin.get("original_address"),
                        origin.get("source_original_address"),
                    )
                    for origin in origins
                ]
                expected = [
                    (0, "original_instruction", worker_start, None),
                    (2, "synthesized_call_promotion", None,
                     call_identity),
                    (4, "synthesized_call_promotion", None,
                     call_identity),
                    (6, "synthesized_call_promotion", None,
                     call_identity),
                    (8, "synthesized_call_promotion", None,
                     call_identity),
                    (10, "synthesized_call_promotion", None,
                     call_identity),
                    (12, "synthesized_call_promotion", None,
                     call_identity),
                    (14, "synthesized_call_promotion", None,
                     call_identity),
                    (20, "cloned_original_instruction", call_identity,
                     None),
                    (24, "synthesized_call_promotion", None,
                     call_identity),
                    (26, "original_instruction", call_identity, None),
                    (28, "synthesized_call_promotion", None,
                     call_identity),
                    (30, "original_instruction", worker_start + 38, None),
                ]
                inline_data = icp_worker.get("synthesized_inline_data", [])
                target_output = symbol_address(
                    m0_icp_image, "promoted_target"
                ) + 1
                if (
                    not icp_worker["firmware_modified"]
                    or icp_reasons != {
                        "unreachable_code_elimination",
                        "indirect_call_promotion",
                    }
                    or icp_worker["firmware_publication_rejections"]
                    or observed != expected
                    or int(icp_worker["original_size"]) != 40
                    or int(icp_worker["output_size"]) != 32
                    or int(final_layout[
                        "synthesized_call_promotion_instruction_count"
                    ]) != 9
                    or len(inline_data) != 1
                    or int(inline_data[0]["output_address"])
                        != worker_start + 16
                    or int(inline_data[0]["target_output_address"])
                        != target_output
                ):
                    raise RuntimeError(
                        "M0+ indirect-call promotion did not survive compact "
                        "publication with aligned target data, argument "
                        "restoration, and exact direct/fallback origins"
                    )
                pending_icp_validation = {
                    "status": "accepted",
                    "profile": str(m0_icp_profile),
                    "profile_sha256": sha256(m0_icp_profile),
                    "call_target_register": "r3",
                    "temporary_argument_register": "r2",
                    "temporary_preservation_register": "lr",
                    "function": "indirect_caller",
                    "modification_reasons": sorted(icp_reasons),
                    "original_call_identity": call_identity,
                    "instruction_origins": observed,
                    "inline_target_data": inline_data,
                    "original_size": int(icp_worker["original_size"]),
                    "output_size": int(icp_worker["output_size"]),
                }
            if label in {"m3_icp", "m3_icp_r12"}:
                if int(summary["cloned_original_instruction_count"]) != 1:
                    raise RuntimeError(
                        "M3 native ICP fixture did not retain exactly one "
                        "cloned indirect-call identity"
                    )
                if int(summary["firmware_modification_reason_counts"]
                       ["indirect_call_promotion"]) != 1:
                    raise RuntimeError(
                        "M3 native ICP fixture did not publish exactly one "
                        "indirect-call promotion"
                    )
            if label in {"m0plus_icp_method_load", "m3_icp_method_load"}:
                if int(summary["cloned_original_instruction_count"]) != 1:
                    raise RuntimeError(
                        "method-load ICP fixture did not retain exactly "
                        "one cloned cold-fallback call identity"
                    )
                for reason in (
                    "indirect_call_promotion", "method_load_elimination",
                ):
                    if int(summary["firmware_modification_reason_counts"]
                           [reason]) != 1:
                        raise RuntimeError(
                            "method-load ICP fixture did not publish "
                            f"exactly one {reason} transformation"
                        )
            if label in {"m0plus_icp_tail", "m3_icp_tail"}:
                if int(summary["cloned_original_instruction_count"]) != 1:
                    raise RuntimeError(
                        "native tail-ICP fixture did not retain exactly "
                        "one cloned indirect-tail-call identity"
                    )
                if int(summary["firmware_modification_reason_counts"]
                       ["indirect_call_promotion"]) != 1:
                    raise RuntimeError(
                        "native tail-ICP fixture did not publish exactly "
                        "one indirect-tail-call promotion"
                    )
            if label == "m3_jump_table_icp":
                if int(summary["firmware_modification_reason_counts"]
                       ["jump_table_promotion"]) != 1:
                    raise RuntimeError(
                        "M3 native jump-table fixture did not publish exactly "
                        "one TBB hot-index promotion"
                    )
            if label in {
                "m3_abs32_jump_table_icp",
                "m3_detached_abs32_jump_table_icp",
            }:
                if int(summary["firmware_modification_reason_counts"]
                       ["jump_table_promotion"]) != 1:
                    raise RuntimeError(
                        "M3 native ABS32 jump-table fixture did not publish "
                        "exactly one relocation-safe hot-index promotion"
                    )
        literal_pool_placement_validation = parse_literal_pool_summary(
            process.stdout + process.stderr
        )
        if label == "m4":
            expected_pool_counts = {
                "qualified": 1,
                "owners": 1,
                "references": 1,
                "retained": 1,
                "unreferenced": 1,
            }
            observed_pool_counts = {
                key: literal_pool_placement_validation[key]
                for key in expected_pool_counts
            }
            if observed_pool_counts != expected_pool_counts:
                raise RuntimeError(
                    "M4 FreeRTOS literal-pool cohort changed unexpectedly: "
                    f"expected {expected_pool_counts}, observed "
                    f"{observed_pool_counts}"
                )
        relocation_order_policy = {
            "m3_function_reorder": "explicit_user",
            "m3_profile_function_reorder": "profile_exec_count",
            "m3_callgraph_function_reorder": "cdsort",
        }.get(label)
        split_validation = {
            "m3_function_split": validate_relocated_function_split,
            "m3_cdsplit": validate_relocated_cdsplit,
        }.get(label)
        results[label] = {
            "cpu": cpu,
            "bolt_summary": [
                line for line in (process.stdout + process.stderr).splitlines()
                if (
                    "out of" in line or "not adding new segments" in line
                    or "Cortex-M firmware constraints" in line
                    or "ARM-state mapping regions" in line
                    or "FOP optimized" in line or "FOP deleted" in line
                    or "FRAME ANALYSIS" in line
                    or "Thumb-1 local branch(es)" in line
                    or "literal-backed call veneers" in line
                    or "Thumb literal-pool placement" in line
                    or "tail duplication modified" in line
                    or "Functions were reordered by LoopInversionPass" in line
                    or "Peephole:" in line
                    or "ICP " in line
                )
            ],
            "validation": (
                validate_relocated_function_order(
                    image, candidate,
                    order_policy=relocation_order_policy,
                )
                if relocation_order_policy is not None
                else split_validation(image, candidate)
                if split_validation is not None
                else validate_in_place(image, candidate, protected)
            ),
            "mapping_symbol_validation": validate_arm_mapping_symbols(
                candidate
            ),
            "native_pass_report": native_pass_report,
            "pass_publication": (
                summarize_pass_publication(
                    image, candidate, native_pass_report
                ) if (
                    native_pass_report is not None
                    and split_validation is None
                ) else None
            ),
            "provenance_validation": provenance_validation,
            "exclusive_access_validation": exclusive_access_validation,
            "literal_pool_placement_validation":
                literal_pool_placement_validation,
        }
        if pending_icp_validation is not None:
            results[label]["indirect_call_promotion_validation"] = (
                pending_icp_validation
            )
        if args.full_pipeline:
            final_layout = json.loads(
                Path(
                    f"{output / f'bolt-pass-report-{label}.json'}.layout.json"
                ).read_text()
            )
            if native_pass_report is not None:
                results[label]["pass_publication"] = (
                    summarize_pass_publication(
                        image, candidate, native_pass_report, final_layout
                    )
                )
            results[label]["final_layout_report"] = final_layout
            if relocation_order_policy is not None:
                results[label]["validation"] = (
                    validate_relocated_function_order(
                        image, candidate, final_layout,
                        order_policy=relocation_order_policy,
                    )
                )
            elif split_validation is not None:
                results[label]["validation"] = (
                    split_validation(
                        image, candidate, final_layout,
                    )
                )
            results[label]["inline_jump_table_validation"] = (
                validate_inline_jump_tables(
                    image, candidate, final_layout
                )
            )
            results[label]["pc_relative_data_validation"] = (
                validate_pc_relative_data_references(
                    image, candidate, final_layout,
                    allow_owned_literal_movement=(
                        label in {
                            "m0plus_moved_relocation",
                            "m0plus_relocation_clone",
                            "m3_abs32_jump_table_icp",
                            "m3_detached_abs32_jump_table_icp",
                        }
                    ),
                )
            )
            table_validation = results[label]["inline_jump_table_validation"]
            kinds = [table["kind"] for table in table_validation["tables"]]
            if label == "m3_rodata_load":
                linked = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "load_linked_rodata"
                )
                unresolved = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "load_unresolved_pool"
                )
                linked_origins = linked["instruction_origins"]
                unresolved_origins = unresolved["instruction_origins"]
                publication = results[label]["pass_publication"]
                if (
                    not linked["firmware_modified"]
                    or linked["original_size"] != linked["output_size"]
                    or [origin["origin_kind"] for origin in linked_origins]
                    != ["synthesized_from_original_instruction",
                        "original_instruction"]
                    or unresolved["firmware_modified"]
                    or not unresolved["firmware_modification_attempted"]
                    or unresolved["firmware_publication_rejections"]
                    != ["missing_emitted_body"]
                    or len(unresolved_origins) != 2
                    or any(origin["origin_kind"] != "original_instruction"
                           for origin in unresolved_origins)
                    or publication["byte_changed_function_count"] != 1
                    or int(final_layout[
                        "synthesized_from_original_instruction_count"
                    ]) != 1
                ):
                    raise RuntimeError(
                        "M3 read-only-load fixture did not publish exactly "
                        "the linked immutable-data rewrite while retaining "
                        "the unresolved local-pool body and full origin map"
                    )
                results[label]["read_only_load_validation"] = {
                    "status": "accepted",
                    "linked_function": linked,
                    "unresolved_function": unresolved,
                }
            if label == "m3" and sorted(kinds) != [
                "thumb_tbb", "thumb_tbb", "thumb_tbb", "thumb_tbh"
            ]:
                raise RuntimeError(
                    "M3 qualification fixture did not retain PC-base TBB/TBH "
                    "and adjacent/detached register-base TBB tables"
                )
            if label == "m3":
                cfg_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "profiled_worker"
                )
                cfg_reasons = set(
                    cfg_worker["firmware_modification_reasons"]
                )
                if (
                    not cfg_worker["firmware_modified"]
                    or "block_reordering" not in cfg_reasons
                    or cfg_worker["firmware_publication_rejections"]
                ):
                    raise RuntimeError(
                        "M3 profile-driven ext-tsp layout did not survive "
                        "compact publication"
                    )
                results[label]["profiled_layout_validation"] = {
                    "status": "accepted",
                    "profile": str(m3_profile),
                    "profile_sha256": sha256(m3_profile),
                    "function": "profiled_worker",
                    "modification_reasons": sorted(cfg_reasons),
                    "emitted_instruction_count": len(
                        cfg_worker["instruction_origins"]
                    ),
                    "profile_block_count": 100,
                }
            if label == "m3_taildup":
                tail_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "taildup_worker"
                )
                tail_reasons = set(
                    tail_worker["firmware_modification_reasons"]
                )
                cloned_origins = [
                    origin for origin in tail_worker["instruction_origins"]
                    if origin["origin_kind"] ==
                    "cloned_original_instruction"
                ]
                worker_start = symbol_address(
                    m3_taildup_image, "taildup_worker"
                )
                expected_originals = [worker_start + 0xE, worker_start + 0x10]
                observed_originals = [
                    int(origin["original_address"])
                    for origin in cloned_origins
                ]
                output_addresses = [
                    int(origin["output_address"])
                    for origin in cloned_origins
                ]
                if (
                    not tail_worker["firmware_modified"]
                    or "tail_duplication" not in tail_reasons
                    or tail_worker["firmware_publication_rejections"]
                    or observed_originals != expected_originals
                    or len(output_addresses) != 2
                    or output_addresses[1] != output_addresses[0] + 2
                    or output_addresses == observed_originals
                    or int(tail_worker["output_size"]) >
                    int(tail_worker["original_size"])
                ):
                    raise RuntimeError(
                        "M3 profile-driven tail duplication did not survive "
                        "compact publication with exact clone origins"
                    )
                results[label]["tail_duplication_validation"] = {
                    "status": "accepted",
                    "profile": str(m3_taildup_profile),
                    "profile_sha256": sha256(m3_taildup_profile),
                    "function": "taildup_worker",
                    "modification_reasons": sorted(tail_reasons),
                    "cloned_original_addresses": observed_originals,
                    "cloned_output_addresses": output_addresses,
                    "original_size": int(tail_worker["original_size"]),
                    "output_size": int(tail_worker["output_size"]),
                }
            if label == "m3_taildup_propagation":
                propagation_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "taildup_propagate"
                )
                propagation_report = next(
                    function for function in native_pass_report["functions"]
                    if function["name"] == "taildup_propagate"
                )
                propagation_reasons = set(
                    propagation_worker["firmware_modification_reasons"]
                )
                worker_start = symbol_address(
                    m3_taildup_propagation_image, "taildup_propagate"
                )
                deleted_move_identity = worker_start + 0x4
                final_origins = propagation_worker["instruction_origins"]
                cloned_origins = [
                    origin for origin in final_origins
                    if origin["origin_kind"] ==
                    "cloned_original_instruction"
                ]
                observed_clone_originals = [
                    int(origin["original_address"])
                    for origin in cloned_origins
                ]
                expected_clone_originals = [
                    worker_start + 0x10, worker_start + 0x14
                ]
                final_text = [
                    instruction["text"]
                    for instruction in propagation_report["instructions"]
                ]
                if (
                    not propagation_worker["firmware_modified"]
                    or "tail_duplication" not in propagation_reasons
                    or propagation_worker["firmware_publication_rejections"]
                    or int(propagation_worker["original_size"]) != 22
                    or int(propagation_worker["output_size"]) != 20
                    or observed_clone_originals != expected_clone_originals
                    or any(
                        int(origin["original_address"]) ==
                        deleted_move_identity
                        for origin in final_origins
                    )
                    or not any(
                        "add.w\tr0, r0, #0x5" in text
                        for text in final_text
                    )
                    or any(
                        "mov" in text and "r2" in text and "#0x5" in text
                        for text in final_text
                    )
                ):
                    raise RuntimeError(
                        "M3 tail-duplication propagation did not publish "
                        "the checked immediate tail with exact deletion and "
                        "clone origins"
                    )
                results[label]["tail_duplication_propagation_validation"] = {
                    "status": "accepted",
                    "profile": str(m3_taildup_propagation_profile),
                    "profile_sha256": sha256(
                        m3_taildup_propagation_profile
                    ),
                    "function": "taildup_propagate",
                    "modification_reasons": sorted(propagation_reasons),
                    "deleted_move_original_identity": deleted_move_identity,
                    "cloned_original_addresses": observed_clone_originals,
                    "original_size": 22,
                    "output_size": 20,
                    "propagated_instruction": "add.w r0, r0, #5",
                }
            if label == "m3_loop_inversion":
                loop_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "loop_worker"
                )
                loop_reasons = set(
                    loop_worker["firmware_modification_reasons"]
                )
                worker_start = symbol_address(
                    m3_loop_inversion_image, "loop_worker"
                )
                origin_tuples = [
                    (
                        origin.get("original_address"),
                        int(origin["output_address"]),
                        origin["origin_kind"],
                    )
                    for origin in loop_worker["instruction_origins"]
                ]
                expected_origins = [
                    (worker_start, worker_start, "original_instruction"),
                    (None, worker_start + 2, "synthesized_control_edge"),
                    (worker_start + 4, worker_start + 6,
                     "original_instruction"),
                    (worker_start + 6, worker_start + 8,
                     "original_instruction"),
                    (worker_start + 8, worker_start + 4,
                     "original_instruction"),
                    (worker_start + 0xC, worker_start + 0xA,
                     "original_instruction"),
                    (worker_start + 0xE, worker_start + 0xC,
                     "original_instruction"),
                ]
                if (
                    not loop_worker["firmware_modified"]
                    or loop_reasons != {"loop_inversion"}
                    or loop_worker["firmware_publication_rejections"]
                    or origin_tuples != expected_origins
                    or int(loop_worker["original_size"]) != 16
                    or int(loop_worker["output_size"]) != 14
                ):
                    raise RuntimeError(
                        "M3 profile-driven loop inversion did not survive "
                        "compact publication with exact origins"
                    )
                results[label]["loop_inversion_validation"] = {
                    "status": "accepted",
                    "profile": str(m3_loop_inversion_profile),
                    "profile_sha256": sha256(m3_loop_inversion_profile),
                    "function": "loop_worker",
                    "modification_reasons": sorted(loop_reasons),
                    "instruction_origins": origin_tuples,
                    "original_size": int(loop_worker["original_size"]),
                    "output_size": int(loop_worker["output_size"]),
                }
            if label == "m3_peephole":
                peephole_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "peephole_worker"
                )
                peephole_reasons = set(
                    peephole_worker["firmware_modification_reasons"]
                )
                worker_start = symbol_address(
                    m3_peephole_image, "peephole_worker"
                )
                origin_pairs = [
                    (int(origin["original_address"]),
                     int(origin["output_address"]))
                    for origin in peephole_worker["instruction_origins"]
                    if origin["origin_kind"] == "original_instruction"
                ]
                expected_pairs = [
                    (worker_start + offset, worker_start + offset)
                    for offset in range(0, 14, 2)
                ]
                native_worker = next(
                    function for function in native_pass_report["functions"]
                    if function["name"] == "peephole_worker"
                )
                expected_direct_edge = {
                    "source_block_original_address": worker_start,
                    "target_block_original_address": worker_start + 0xA,
                }
                if (
                    not peephole_worker["firmware_modified"]
                    or peephole_reasons != {"control_flow_peepholes"}
                    or peephole_worker["firmware_publication_rejections"]
                    or origin_pairs != expected_pairs
                    or int(peephole_worker["original_size"]) != 14
                    or int(peephole_worker["output_size"]) != 14
                    or expected_direct_edge not in
                    native_worker["post_control_edges"]
                ):
                    raise RuntimeError(
                        "M3 double-jump peephole did not survive compact "
                        "publication with exact origins and direct edge"
                    )
                results[label]["control_flow_peephole_validation"] = {
                    "status": "accepted",
                    "function": "peephole_worker",
                    "modification_reasons": sorted(peephole_reasons),
                    "instruction_origin_pairs": origin_pairs,
                    "direct_edge": expected_direct_edge,
                    "original_size": int(peephole_worker["original_size"]),
                    "output_size": int(peephole_worker["output_size"]),
                }
            if label in {"m3_icp", "m3_icp_r12"}:
                icp_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "indirect_caller"
                )
                icp_reasons = set(
                    icp_worker["firmware_modification_reasons"]
                )
                icp_image = (
                    m3_icp_r12_image
                    if label == "m3_icp_r12" else m3_icp_image
                )
                icp_profile = (
                    m3_icp_r12_profile
                    if label == "m3_icp_r12" else m3_icp_profile
                )
                worker_start = symbol_address(icp_image, "indirect_caller")
                call_identity = worker_start + 2
                origins = icp_worker["instruction_origins"]
                observed = [
                    (
                        int(origin["output_address"]) - worker_start,
                        origin["origin_kind"],
                        origin.get("original_address"),
                        origin.get("source_original_address"),
                    )
                    for origin in origins
                ]
                expected = [
                    (0, "original_instruction", worker_start, None),
                    (2, "synthesized_call_promotion", None,
                     call_identity),
                    (6, "synthesized_call_promotion", None,
                     call_identity),
                    (10, "synthesized_call_promotion", None,
                     call_identity),
                    (14, "synthesized_call_promotion", None,
                     call_identity),
                    (16, "cloned_original_instruction", call_identity,
                     None),
                    (20, "synthesized_call_promotion", None,
                     call_identity),
                    (22, "original_instruction", call_identity, None),
                    (24, "synthesized_call_promotion", None,
                     call_identity),
                    (26, "original_instruction", worker_start + 38, None),
                ]
                if (
                    not icp_worker["firmware_modified"]
                    or icp_reasons != {
                        "unreachable_code_elimination",
                        "indirect_call_promotion",
                    }
                    or icp_worker["firmware_publication_rejections"]
                    or observed != expected
                    or int(icp_worker["original_size"]) != 40
                    or int(icp_worker["output_size"]) != 28
                    or int(final_layout[
                        "synthesized_call_promotion_instruction_count"
                    ]) != 6
                ):
                    raise RuntimeError(
                        "M3 indirect-call promotion did not survive compact "
                        "publication with exact direct/fallback origins"
                    )
                results[label]["indirect_call_promotion_validation"] = {
                    "status": "accepted",
                    "profile": str(icp_profile),
                    "profile_sha256": sha256(icp_profile),
                    "call_target_register": (
                        "r12" if label == "m3_icp_r12" else "r3"
                    ),
                    "function": "indirect_caller",
                    "modification_reasons": sorted(icp_reasons),
                    "original_call_identity": call_identity,
                    "instruction_origins": observed,
                    "original_size": int(icp_worker["original_size"]),
                    "output_size": int(icp_worker["output_size"]),
                }
            if label == "m0plus_icp_method_load":
                method_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "virtual_caller"
                )
                method_reasons = set(
                    method_worker["firmware_modification_reasons"]
                )
                worker_start = symbol_address(
                    m0_icp_method_load_image, "virtual_caller"
                )
                vptr_load_identity = worker_start + 2
                method_load_identity = worker_start + 4
                call_identity = worker_start + 6
                observed = [
                    (
                        int(origin["output_address"]) - worker_start,
                        int(origin["output_size"]),
                        origin["origin_kind"],
                        origin.get("original_address"),
                        origin.get("source_original_address"),
                    )
                    for origin in method_worker["instruction_origins"]
                ]
                expected = [
                    (0, 2, "original_instruction", worker_start, None),
                    (2, 2, "original_instruction", vptr_load_identity, None),
                    (4, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (6, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (8, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (10, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (12, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (14, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (20, 4, "cloned_original_instruction", call_identity,
                     None),
                    (24, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (26, 2, "original_instruction", method_load_identity,
                     None),
                    (28, 2, "original_instruction", call_identity, None),
                    (30, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (32, 2, "original_instruction", worker_start + 50,
                     None),
                ]
                retained_method_loads = [
                    origin for origin in method_worker["instruction_origins"]
                    if origin.get("original_address") == method_load_identity
                    and origin["origin_kind"] == "original_instruction"
                    and origin["may_load"]
                ]
                inline_data = method_worker.get("synthesized_inline_data", [])
                vtable_output = symbol_address(
                    m0_icp_method_load_image, "immutable_hot_vtable"
                )
                if (
                    not method_worker["firmware_modified"]
                    or method_reasons != {
                        "unreachable_code_elimination",
                        "indirect_call_promotion",
                        "method_load_elimination",
                    }
                    or method_worker["firmware_publication_rejections"]
                    or observed != expected
                    or len(retained_method_loads) != 1
                    or int(method_worker["original_size"]) != 52
                    or int(method_worker["output_size"]) != 34
                    or int(final_layout[
                        "synthesized_call_promotion_instruction_count"
                    ]) != 8
                    or len(inline_data) != 1
                    or int(inline_data[0]["output_address"])
                        != worker_start + 16
                    or int(inline_data[0]["target_output_address"])
                        != vtable_output
                    or inline_data[0]["target_symbol"]
                        != "immutable_hot_vtable"
                ):
                    raise RuntimeError(
                        "M0+ immutable-vtable method-load elimination did "
                        "not retain typed raw-vtable data and exact hot/cold "
                        "origins"
                    )
                results[label]["method_load_elimination_validation"] = {
                    "status": "accepted",
                    "profile": str(m0_icp_method_load_profile),
                    "profile_sha256": sha256(m0_icp_method_load_profile),
                    "function": "virtual_caller",
                    "contract": (
                        "one exact mapped allocatable non-writable Thumb-1 "
                        "method slot plus an independently admitted immutable "
                        "flash guard read; cold fallback retains the load"
                    ),
                    "modification_reasons": sorted(method_reasons),
                    "original_vptr_load_identity": vptr_load_identity,
                    "original_method_load_identity": method_load_identity,
                    "original_call_identity": call_identity,
                    "retained_cold_method_load_count":
                        len(retained_method_loads),
                    "instruction_origins": observed,
                    "inline_vtable_data": inline_data,
                    "original_size": int(method_worker["original_size"]),
                    "output_size": int(method_worker["output_size"]),
                }
            if label == "m3_icp_method_load":
                method_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "virtual_caller"
                )
                method_reasons = set(
                    method_worker["firmware_modification_reasons"]
                )
                worker_start = symbol_address(
                    m3_icp_method_load_image, "virtual_caller"
                )
                vptr_load_identity = worker_start + 2
                method_load_identity = worker_start + 4
                call_identity = worker_start + 6
                observed = [
                    (
                        int(origin["output_address"]) - worker_start,
                        int(origin["output_size"]),
                        origin["origin_kind"],
                        origin.get("original_address"),
                        origin.get("source_original_address"),
                    )
                    for origin in method_worker["instruction_origins"]
                ]
                expected = [
                    (0, 2, "original_instruction", worker_start, None),
                    (2, 2, "original_instruction", vptr_load_identity, None),
                    (4, 4, "synthesized_call_promotion", None,
                     call_identity),
                    (8, 4, "synthesized_call_promotion", None,
                     call_identity),
                    (12, 4, "synthesized_call_promotion", None,
                     call_identity),
                    (16, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (18, 4, "cloned_original_instruction", call_identity,
                     None),
                    (22, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (24, 2, "original_instruction", method_load_identity,
                     None),
                    (26, 2, "original_instruction", call_identity, None),
                    (28, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (30, 2, "original_instruction", worker_start + 50,
                     None),
                ]
                retained_method_loads = [
                    origin for origin in
                    method_worker["instruction_origins"]
                    if origin.get("original_address") == method_load_identity
                    and origin["origin_kind"] == "original_instruction"
                    and origin["may_load"]
                ]
                if (
                    not method_worker["firmware_modified"]
                    or method_reasons != {
                        "unreachable_code_elimination",
                        "indirect_call_promotion",
                        "method_load_elimination",
                    }
                    or method_worker["firmware_publication_rejections"]
                    or observed != expected
                    or len(retained_method_loads) != 1
                    or int(method_worker["original_size"]) != 52
                    or int(method_worker["output_size"]) != 32
                    or int(final_layout[
                        "synthesized_call_promotion_instruction_count"
                    ]) != 6
                ):
                    raise RuntimeError(
                        "M3 immutable-vtable method-load elimination did not "
                        "retain the exact cold fallback load/call identities"
                    )
                results[label]["method_load_elimination_validation"] = {
                    "status": "accepted",
                    "profile": str(m3_icp_method_load_profile),
                    "profile_sha256": sha256(m3_icp_method_load_profile),
                    "function": "virtual_caller",
                    "contract": (
                        "one exact mapped allocatable non-writable method "
                        "slot; load elision admitted only on the matching "
                        "vtable guard; cold fallback retains the load"
                    ),
                    "modification_reasons": sorted(method_reasons),
                    "original_vptr_load_identity": vptr_load_identity,
                    "original_method_load_identity": method_load_identity,
                    "original_call_identity": call_identity,
                    "retained_cold_method_load_count":
                        len(retained_method_loads),
                    "instruction_origins": observed,
                    "original_size": int(method_worker["original_size"]),
                    "output_size": int(method_worker["output_size"]),
                }
            if label == "m0plus_icp_tail":
                tail_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "tail_dispatch"
                )
                tail_reasons = set(
                    tail_worker["firmware_modification_reasons"]
                )
                worker_start = symbol_address(
                    m0_icp_tail_image, "tail_dispatch"
                )
                call_identity = worker_start + 4
                origins = tail_worker["instruction_origins"]
                observed = [
                    (
                        int(origin["output_address"]) - worker_start,
                        int(origin["output_size"]),
                        origin["origin_kind"],
                        origin.get("original_address"),
                        origin.get("source_original_address"),
                    )
                    for origin in origins
                ]
                expected = [
                    (0, 2, "original_instruction", worker_start, None),
                    (2, 2, "original_instruction", worker_start + 2, None),
                    (4, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (6, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (8, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (10, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (12, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (14, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (20, 2, "cloned_original_instruction", call_identity,
                     None),
                    (22, 2, "original_instruction", call_identity, None),
                ]
                inline_data = tail_worker.get("synthesized_inline_data", [])
                target_output = symbol_address(
                    m0_icp_tail_image, "promoted_target"
                ) + 1
                if (
                    not tail_worker["firmware_modified"]
                    or tail_reasons != {
                        "unreachable_code_elimination",
                        "indirect_call_promotion",
                    }
                    or tail_worker["firmware_publication_rejections"]
                    or observed != expected
                    or int(tail_worker["original_size"]) != 38
                    or int(tail_worker["output_size"]) != 24
                    or int(final_layout[
                        "synthesized_call_promotion_instruction_count"
                    ]) != 6
                    or len(inline_data) != 1
                    or int(inline_data[0]["output_address"])
                        != worker_start + 16
                    or int(inline_data[0]["target_output_address"])
                        != target_output
                ):
                    raise RuntimeError(
                        "M0+ indirect-tail-call promotion did not survive "
                        "compact publication with aligned target data, "
                        "LR preservation, argument restoration, and exact "
                        "direct/fallback origins"
                    )
                results[label]["indirect_tail_call_promotion_validation"] = {
                    "status": "accepted",
                    "profile": str(m0_icp_tail_profile),
                    "profile_sha256": sha256(m0_icp_tail_profile),
                    "call_target_register": "r3",
                    "temporary_argument_register": "r2",
                    "temporary_preservation_register": "r12",
                    "function": "tail_dispatch",
                    "modification_reasons": sorted(tail_reasons),
                    "original_call_identity": call_identity,
                    "instruction_origins": observed,
                    "inline_target_data": inline_data,
                    "original_size": int(tail_worker["original_size"]),
                    "output_size": int(tail_worker["output_size"]),
                }
            if label == "m3_icp_tail":
                tail_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "tail_dispatch"
                )
                tail_reasons = set(
                    tail_worker["firmware_modification_reasons"]
                )
                worker_start = symbol_address(
                    m3_icp_tail_image, "tail_dispatch"
                )
                call_identity = worker_start + 4
                origins = tail_worker["instruction_origins"]
                observed = [
                    (
                        int(origin["output_address"]) - worker_start,
                        int(origin["output_size"]),
                        origin["origin_kind"],
                        origin.get("original_address"),
                        origin.get("source_original_address"),
                    )
                    for origin in origins
                ]
                expected = [
                    (0, 2, "original_instruction", worker_start, None),
                    (2, 2, "original_instruction", worker_start + 2, None),
                    (4, 4, "synthesized_call_promotion", None,
                     call_identity),
                    (8, 4, "synthesized_call_promotion", None,
                     call_identity),
                    (12, 4, "synthesized_call_promotion", None,
                     call_identity),
                    (16, 2, "synthesized_call_promotion", None,
                     call_identity),
                    (18, 4, "cloned_original_instruction", call_identity,
                     None),
                    (22, 2, "original_instruction", call_identity, None),
                ]
                if (
                    not tail_worker["firmware_modified"]
                    or tail_reasons != {
                        "unreachable_code_elimination",
                        "instruction_shortening",
                        "indirect_call_promotion",
                    }
                    or tail_worker["firmware_publication_rejections"]
                    or observed != expected
                    or int(tail_worker["original_size"]) != 38
                    or int(tail_worker["output_size"]) != 24
                    or int(final_layout[
                        "synthesized_call_promotion_instruction_count"
                    ]) != 4
                ):
                    raise RuntimeError(
                        "M3 indirect-tail-call promotion did not survive "
                        "compact publication with exact epilogue, direct and "
                        "fallback origins"
                    )
                results[label]["indirect_tail_call_promotion_validation"] = {
                    "status": "accepted",
                    "profile": str(m3_icp_tail_profile),
                    "profile_sha256": sha256(m3_icp_tail_profile),
                    "function": "tail_dispatch",
                    "modification_reasons": sorted(tail_reasons),
                    "original_call_identity": call_identity,
                    "instruction_origins": observed,
                    "original_size": int(tail_worker["original_size"]),
                    "output_size": int(tail_worker["output_size"]),
                }
            if label == "m3_jump_table_icp":
                jump_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "jump_table_worker"
                )
                jump_reasons = set(
                    jump_worker["firmware_modification_reasons"]
                )
                worker_start = symbol_address(
                    m3_jump_table_icp_image, "jump_table_worker"
                )
                dispatch_identity = worker_start + 4
                origins = jump_worker["instruction_origins"]
                observed = [
                    (
                        int(origin["output_address"]) - worker_start,
                        int(origin["output_size"]),
                        origin["origin_kind"],
                        origin.get("original_address"),
                        origin.get("source_original_address"),
                    )
                    for origin in origins
                ]
                expected = [
                    (0, 2, "original_instruction", worker_start, None),
                    (2, 2, "original_instruction", worker_start + 2, None),
                    (4, 4, "synthesized_jump_table_promotion", None,
                     dispatch_identity),
                    (8, 2, "synthesized_jump_table_promotion", None,
                     dispatch_identity),
                    (10, 4, "original_instruction", dispatch_identity, None),
                    (18, 2, "original_instruction", worker_start + 12,
                     None),
                    (20, 2, "original_instruction", worker_start + 14,
                     None),
                    (22, 2, "original_instruction", worker_start + 16,
                     None),
                    (24, 2, "original_instruction", worker_start + 18,
                     None),
                    (26, 2, "original_instruction", worker_start + 20,
                     None),
                    (28, 2, "original_instruction", worker_start + 22,
                     None),
                    (30, 2, "original_instruction", worker_start + 24,
                     None),
                    (32, 2, "original_instruction", worker_start + 26,
                     None),
                ]
                table_validation = results[label][
                    "inline_jump_table_validation"
                ]
                table = next(
                    item for item in table_validation["tables"]
                    if item["function"] == "jump_table_worker"
                )
                if (
                    not jump_worker["firmware_modified"]
                    or jump_reasons != {
                        "unreachable_code_elimination",
                        "jump_table_promotion",
                    }
                    or jump_worker["firmware_publication_rejections"]
                    or observed != expected
                    or int(jump_worker["original_size"]) != 60
                    or int(jump_worker["output_size"]) != 36
                    or int(final_layout[
                        "synthesized_jump_table_promotion_instruction_count"
                    ]) != 2
                    or not table["mapping_symbols_exact"]
                ):
                    raise RuntimeError(
                        "M3 TBB promotion did not survive compact publication "
                        "with exact guard/fallback origins and mapping symbols"
                    )
                results[label]["jump_table_promotion_validation"] = {
                    "status": "accepted",
                    "profile": str(m3_jump_table_icp_profile),
                    "profile_sha256": sha256(m3_jump_table_icp_profile),
                    "function": "jump_table_worker",
                    "modification_reasons": sorted(jump_reasons),
                    "original_dispatch_identity": dispatch_identity,
                    "instruction_origins": observed,
                    "mapping_symbols_exact": True,
                    "original_size": int(jump_worker["original_size"]),
                    "output_size": int(jump_worker["output_size"]),
                }
            if label == "m3_abs32_jump_table_icp":
                jump_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "abs32_jump_table_worker"
                )
                jump_reasons = set(
                    jump_worker["firmware_modification_reasons"]
                )
                worker_start = symbol_address(
                    m3_abs32_jump_table_icp_image,
                    "abs32_jump_table_worker",
                )
                dispatch_identity = worker_start + 16
                observed = [
                    (
                        int(origin["output_address"]) - worker_start,
                        int(origin["output_size"]),
                        origin["origin_kind"],
                        origin.get("original_address"),
                        origin.get("source_original_address"),
                    )
                    for origin in jump_worker["instruction_origins"]
                ]
                expected = [
                    (0, 2, "original_instruction", worker_start, None),
                    (2, 2, "original_instruction", worker_start + 2, None),
                    (4, 2, "original_instruction", worker_start + 4, None),
                    (6, 2, "original_instruction", worker_start + 6, None),
                    (8, 2, "original_instruction", worker_start + 8, None),
                    (10, 2, "original_instruction", worker_start + 10, None),
                    (12, 2, "original_instruction", worker_start + 12, None),
                    (14, 2, "original_instruction", worker_start + 14, None),
                    (16, 4, "synthesized_jump_table_promotion", None,
                     dispatch_identity),
                    (20, 2, "synthesized_jump_table_promotion", None,
                     dispatch_identity),
                    (22, 2, "original_instruction", dispatch_identity, None),
                    (24, 2, "original_instruction", worker_start + 18, None),
                    (26, 2, "original_instruction", worker_start + 20, None),
                    (28, 2, "original_instruction", worker_start + 22, None),
                    (30, 2, "original_instruction", worker_start + 24, None),
                    (32, 2, "original_instruction", worker_start + 26, None),
                    (34, 2, "original_instruction", worker_start + 28, None),
                ]
                table = next(
                    item for item in results[label][
                        "inline_jump_table_validation"
                    ]["tables"]
                    if item["function"] == "abs32_jump_table_worker"
                )
                table_address = int(table["emitted_table_address"])
                literal_pointer = int.from_bytes(
                    virtual_bytes(
                        candidates[label], table_address - 4, 4
                    ),
                    "little",
                )
                references = results[label]["pc_relative_data_validation"]
                literal_references = references["references"]
                if (
                    not jump_worker["firmware_modified"]
                    or jump_worker["address_stable_layout"]
                    or jump_reasons != {
                        "unreachable_code_elimination",
                        "jump_table_promotion",
                    }
                    or jump_worker["firmware_publication_rejections"]
                    or observed != expected
                    or int(jump_worker["original_size"]) != 80
                    or int(jump_worker["output_size"]) != 52
                    or int(final_layout[
                        "synthesized_jump_table_promotion_instruction_count"
                    ]) != 2
                    or not table["exact"]
                    or not table["mapping_symbols_exact"]
                    or literal_pointer != table_address
                    or len(literal_references) != 1
                    or int(literal_references[0]["emitted_target"])
                    != table_address - 4
                ):
                    raise RuntimeError(
                        "M3 ABS32 promotion did not preserve the exact "
                        "literal pointer, table targets, fallback and origins"
                    )
                results[label]["abs32_jump_table_promotion_validation"] = {
                    "status": "accepted",
                    "profile": str(m3_abs32_jump_table_icp_profile),
                    "profile_sha256": sha256(
                        m3_abs32_jump_table_icp_profile
                    ),
                    "function": "abs32_jump_table_worker",
                    "modification_reasons": sorted(jump_reasons),
                    "original_dispatch_identity": dispatch_identity,
                    "instruction_origins": observed,
                    "literal_pointer_output_address": table_address - 4,
                    "literal_pointer_value": literal_pointer,
                    "literal_load": literal_references[0],
                    "table_output_address": table_address,
                    "table_targets": table["actual_emitted_targets"],
                    "mapping_symbols_exact": True,
                    "original_size": int(jump_worker["original_size"]),
                    "output_size": int(jump_worker["output_size"]),
                }
            if label == "m3_detached_abs32_jump_table_icp":
                jump_worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] ==
                    "detached_abs32_jump_table_worker"
                )
                jump_reasons = set(
                    jump_worker["firmware_modification_reasons"]
                )
                worker_start = symbol_address(
                    m3_detached_abs32_jump_table_icp_image,
                    "detached_abs32_jump_table_worker",
                )
                dispatch_identity = worker_start + 16
                table_report = next(
                    table for table in jump_worker["inline_jump_tables"]
                    if table["kind"] == "thumb_abs32"
                )
                validated_table = next(
                    table for table in results[label][
                        "inline_jump_table_validation"
                    ]["tables"]
                    if table["function"] ==
                    "detached_abs32_jump_table_worker"
                )
                pointer_original = int(
                    table_report["table_pointer_original_address"]
                )
                pointer_output = int(
                    table_report["table_pointer_output_address"]
                )
                table_original = int(table_report["original_table_address"])
                table_output = int(table_report["table_output_address"])
                pointer_value = int.from_bytes(
                    virtual_bytes(candidates[label], pointer_output, 4),
                    "little",
                )
                input_pointer_records = [
                    record for record in static_relocation_records(
                        m3_detached_abs32_jump_table_icp_image, ".rel.text"
                    )
                    if record["type"] == 2
                    and record["offset"] == pointer_original
                ]
                output_pointer_records = [
                    record for record in static_relocation_records(
                        candidates[label], ".rel.text"
                    )
                    if record["type"] == 2
                    and record["offset"] == pointer_output
                ]
                input_entry_records = [
                    record for record in static_relocation_records(
                        m3_detached_abs32_jump_table_icp_image, ".rel.rodata"
                    )
                    if record["type"] == 2
                ]
                output_entry_records = [
                    record for record in static_relocation_records(
                        candidates[label], ".rel.rodata"
                    )
                    if record["type"] == 2
                ]
                expected_entry_offsets = [table_output + 4 * index
                                          for index in range(3)]
                observed_entry_offsets = [
                    record["offset"] for record in output_entry_records
                ]
                if (
                    not jump_worker["firmware_modified"]
                    or jump_reasons != {
                        "unreachable_code_elimination",
                        "jump_table_promotion",
                    }
                    or jump_worker["firmware_publication_rejections"]
                    or not table_report["reference_relocations_complete"]
                    or table_original != table_output
                    or pointer_original == pointer_output
                    or pointer_value != table_output
                    or len(input_pointer_records) != 1
                    or len(output_pointer_records) != 1
                    or len(input_entry_records) != 3
                    or len(output_entry_records) != 3
                    or observed_entry_offsets != expected_entry_offsets
                    or not validated_table["exact"]
                    or not validated_table["mapping_symbols_exact"]
                    or int(final_layout[
                        "synthesized_jump_table_promotion_instruction_count"
                    ]) != 2
                ):
                    raise RuntimeError(
                        "M3 detached ABS32 promotion did not atomically "
                        "preserve its pointer/table relocation graph, exact "
                        "targets and compact guard/fallback publication"
                    )
                results[label][
                    "detached_abs32_jump_table_promotion_validation"
                ] = {
                    "status": "accepted",
                    "profile": str(m3_detached_abs32_jump_table_icp_profile),
                    "profile_sha256": sha256(
                        m3_detached_abs32_jump_table_icp_profile
                    ),
                    "function": "detached_abs32_jump_table_worker",
                    "modification_reasons": sorted(jump_reasons),
                    "original_dispatch_identity": dispatch_identity,
                    "reference_relocations_complete": True,
                    "pointer_original_address": pointer_original,
                    "pointer_output_address": pointer_output,
                    "pointer_value": pointer_value,
                    "table_original_address": table_original,
                    "table_output_address": table_output,
                    "table_targets": validated_table[
                        "actual_emitted_targets"
                    ],
                    "input_pointer_relocation": input_pointer_records[0],
                    "output_pointer_relocation": output_pointer_records[0],
                    "input_entry_relocations": input_entry_records,
                    "output_entry_relocations": output_entry_records,
                    "mapping_symbols_exact": True,
                    "original_size": int(jump_worker["original_size"]),
                    "output_size": int(jump_worker["output_size"]),
                }
            if label == "m0plus_literal":
                references = results[label]["pc_relative_data_validation"]
                if references["pc_relative_reference_count"] != 2:
                    raise RuntimeError(
                        "M0+ qualification fixture did not retain its exact "
                        "Thumb-1 LDR-literal and ADR references"
                    )
            if label == "m0plus_movable_literals":
                references = results[label]["pc_relative_data_validation"]
                worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "large_literal_worker"
                )
                pools = worker["owned_literal_pools"]
                reasons = set(worker["firmware_modification_reasons"])
                publication = results[label]["pass_publication"]
                if (
                    not worker["firmware_modified"]
                    or worker["address_stable_layout"]
                    or "literal_pool_placement" not in reasons
                    or references["pc_relative_reference_count"] != 3
                    or len(pools) != 2
                    or any(pool["alignment"] != 4 for pool in pools)
                    or any(pool["output_address"] % 4 for pool in pools)
                    or sorted(
                        pool["relocation_count"] for pool in pools
                    ) != [0, 1]
                    or publication["selected_function_count"] != 1
                ):
                    raise RuntimeError(
                        "M0+ movable-literal fixture did not install two "
                        "aligned, mapped instruction-owned pools with shared "
                        "load binding and one "
                        "carried SHT_REL dependency"
                    )
            if label == "m0plus_moved_relocation":
                worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "relocated_pool"
                )
                pools = worker["owned_literal_pools"]
                relocations = static_relocation_records(
                    candidate, ".rel.text"
                )
                external = [
                    relocation for relocation in relocations
                    if relocation["symbol"] == "external_object"
                ]
                if (
                    not worker["firmware_modified"]
                    or len(pools) != 1
                    or pools[0]["relocation_count"] != 1
                    or pools[0]["original_address"] ==
                        pools[0]["output_address"]
                    or len(external) != 1
                    or external[0]["offset"] != pools[0]["output_address"]
                ):
                    raise RuntimeError(
                        "M0+ moved-relocation fixture did not remap its "
                        "SHT_REL source offset and rebuilt symbol index"
                    )
                results[label]["static_relocation_validation"] = {
                    "status": "accepted",
                    "original_pool_address": pools[0]["original_address"],
                    "output_pool_address": pools[0]["output_address"],
                    "records": relocations,
                }
            if label == "m0plus_relocation_clone":
                worker = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "shared_relocation_worker"
                )
                pools = worker["owned_literal_pools"]
                input_relocations = static_relocation_records(
                    image, ".rel.text"
                )
                output_relocations = static_relocation_records(
                    candidate, ".rel.text"
                )
                input_external = [
                    relocation for relocation in input_relocations
                    if relocation["symbol"] == "external_object"
                ]
                output_external = [
                    relocation for relocation in output_relocations
                    if relocation["symbol"] == "external_object"
                ]
                pool_addresses = sorted(
                    pool["output_address"] for pool in pools
                    if pool["relocation_count"] == 1
                )
                references = results[label]["pc_relative_data_validation"]
                publication = results[label]["pass_publication"]
                if (
                    not worker["firmware_modified"]
                    or references["pc_relative_reference_count"] != 2
                    or len(input_external) != 1
                    or len(output_external) != 2
                    or pool_addresses != sorted(
                        relocation["offset"]
                        for relocation in output_external
                    )
                    or publication["selected_function_count"] != 1
                ):
                    raise RuntimeError(
                        "M0+ relocation-clone fixture did not grow SHT_REL "
                        "for both live pool copies"
                    )
                results[label]["static_relocation_validation"] = {
                    "status": "accepted",
                    "input_external_record_count": len(input_external),
                    "output_external_record_count": len(output_external),
                    "output_pool_addresses": pool_addresses,
                    "records": output_relocations,
                }
            if label == "m4" and (
                table_validation["table_count"] != 8
                or table_validation["entry_count"] != 44
            ):
                raise RuntimeError(
                    "M4 FreeRTOS inline-table cohort changed unexpectedly"
                )
            if label == "m3_jump6":
                input_relocations = static_relocation_records(
                    image, ".rel.text"
                )
                output_relocations = static_relocation_records(
                    candidate, ".rel.text"
                )
                input_jump6 = [
                    relocation for relocation in input_relocations
                    if relocation["type"] == 52  # R_ARM_THM_JUMP6
                ]
                output_jump6 = [
                    relocation for relocation in output_relocations
                    if relocation["type"] == 52  # R_ARM_THM_JUMP6
                ]
                if (
                    len(input_jump6) != 1
                    or len(output_jump6) != 1
                    or input_jump6[0]["offset"] != output_jump6[0]["offset"]
                    or output_jump6[0]["symbol"] != "jump6_target"
                ):
                    raise RuntimeError(
                        "M3 JUMP6 fixture did not retain its exact CBZ/CBNZ "
                        "relocation and target identity"
                    )
                results[label]["static_relocation_validation"] = {
                    "status": "accepted",
                    "input": input_jump6,
                    "output": output_jump6,
                }
            if label == "m3_narrow_data":
                input_relocations = static_relocation_records(
                    image, ".rel.text"
                )
                output_relocations = static_relocation_records(
                    candidate, ".rel.text"
                )
                expected = {
                    (8, "answer8"),   # R_ARM_ABS8
                    (5, "answer16"),  # R_ARM_ABS16
                }
                input_narrow = [
                    relocation for relocation in input_relocations
                    if (relocation["type"], relocation["symbol"]) in expected
                ]
                output_narrow = [
                    relocation for relocation in output_relocations
                    if (relocation["type"], relocation["symbol"]) in expected
                ]
                input_identity = {
                    (item["offset"], item["type"], item["symbol"])
                    for item in input_narrow
                }
                output_identity = {
                    (item["offset"], item["type"], item["symbol"])
                    for item in output_narrow
                }
                if (
                    len(input_narrow) != 2
                    or len(output_narrow) != 2
                    or input_identity != output_identity
                ):
                    raise RuntimeError(
                        "M3 narrow-data fixture did not retain exact ABS8/"
                        "ABS16 relocation sites and target identities"
                    )
                results[label]["static_relocation_validation"] = {
                    "status": "accepted",
                    "input": input_narrow,
                    "output": output_narrow,
                }
            if label == "m0plus":
                match = re.search(
                    r"relaxed (\d+) Thumb-1 local branch\(es\)",
                    process.stdout + process.stderr,
                )
                if match is None or int(match.group(1)) == 0:
                    raise RuntimeError(
                        "real M0+ corpus did not exercise Thumb-1 branch "
                        "relaxation"
                    )
                results[label]["thumb1_local_branches_relaxed"] = int(
                    match.group(1)
                )
            if label == "m0plus_branch":
                match = re.search(
                    r"relaxed (\d+) Thumb-1 local branch\(es\)",
                    process.stdout + process.stderr,
                )
                if match is None or int(match.group(1)) == 0:
                    raise RuntimeError(
                        "M0+ branch fixture did not require a local trampoline"
                    )
                publication = results[label]["pass_publication"]
                final_synthesized = int(
                    final_layout["synthesized_control_edge_instruction_count"]
                )
                if (
                    publication["byte_changed_function_count"] != 1
                    or publication[
                        "unique_changed_executable_bytes_in_selected_ranges"
                    ] == 0
                    or final_synthesized == 0
                ):
                    raise RuntimeError(
                        "M0+ branch fixture did not install its mapped Thumb-1 "
                        "trampoline"
                    )
                results[label]["thumb1_local_branches_relaxed"] = int(
                    match.group(1)
                )
            if label == "m0plus_call_veneer":
                publication = results[label]["pass_publication"]
                veneer_count = int(
                    final_layout["synthesized_call_veneer_instruction_count"]
                )
                inline_bytes = int(
                    final_layout["synthesized_inline_data_bytes"]
                )
                caller = next(
                    function for function in final_layout["functions"]
                    if function["name"] == "caller"
                )
                literals = caller["synthesized_inline_data"]
                if (
                    publication["byte_changed_function_count"] != 1
                    or publication[
                        "unique_changed_executable_bytes_in_selected_ranges"
                    ] == 0
                    or veneer_count < 2
                    or inline_bytes != 4
                    or len(literals) != 1
                    or literals[0]["output_address"] % 4 != 0
                    or literals[0]["target_output_address"] & 1 != 1
                ):
                    raise RuntimeError(
                        "M0+ call fixture did not install one aligned, mapped "
                        "literal-backed Thumb-1 veneer"
                    )
            if label == "m0plus_reserved_veneer":
                publication = results[label]["pass_publication"]
                slot = next(
                    function for function in final_layout["functions"]
                    if function["name"] ==
                    "__firmbolt_thumb_veneer_slot0"
                )
                literals = slot["synthesized_inline_data"]
                reasons = set(slot["firmware_modification_reasons"])
                if (
                    publication["byte_changed_function_count"] != 2
                    or not slot["firmware_veneer_slot"]
                    or "veneer_slot_allocation" not in reasons
                    or int(final_layout[
                        "synthesized_call_veneer_instruction_count"
                    ]) < 2
                    or int(final_layout["synthesized_inline_data_bytes"]) != 4
                    or len(literals) != 1
                    or literals[0]["output_address"] % 4 != 0
                    or literals[0]["target_output_address"] & 1 != 1
                ):
                    raise RuntimeError(
                        "M0+ reserved-slot fixture did not install one typed, "
                        "aligned external Thumb-1 veneer"
                    )

    mismatch = run(
        [str(args.bolt.resolve()), str(M4_IMAGE),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "-o", str(output / "must-not-emit-mismatched-m3.elf")],
        logs / "reject-m4-as-m3.log",
    )
    if mismatch.returncode == 0 or "CPU/input attribute mismatch" not in mismatch.stderr:
        raise RuntimeError("M4-as-M3 negative qualification did not reject")

    store_removal = run(
        [str(args.bolt.resolve()), str(m3_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline", "--frame-opt=all",
         "--frame-opt-rm-stores",
         "-o", str(output / "must-not-emit-store-removal.elf")],
        logs / "reject-experimental-store-removal.log",
    )
    if (
        store_removal.returncode == 0
        or "does not qualify experimental store removal"
        not in (store_removal.stdout + store_removal.stderr)
    ):
        raise RuntimeError("ARM experimental store-removal negative did not reject")

    rodata_without_contract = run(
        [str(args.bolt.resolve()), str(m3_rodata_load_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline", "--simplify-rodata-loads",
         "-o", str(output / "must-not-emit-unadmitted-rodata-load.elf")],
        logs / "reject-unadmitted-rodata-load.log",
    )
    if (
        rodata_without_contract.returncode == 0
        or "read-only-load simplification removes a mapped read/fault event"
        not in (rodata_without_contract.stdout +
                rodata_without_contract.stderr)
    ):
        raise RuntimeError(
            "unadmitted Cortex-M read-only-load simplification did not reject"
        )

    inlining_without_contract = run(
        [str(args.bolt.resolve()), str(m3_inline_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--force-inline=inline_callee",
         "-o", str(output / "must-not-emit-unadmitted-inlining.elf")],
        logs / "reject-unadmitted-inlining.log",
    )
    if (
        inlining_without_contract.returncode == 0
        or "Cortex-M inlining changes call boundaries" not in
        (inlining_without_contract.stdout + inlining_without_contract.stderr)
    ):
        raise RuntimeError("unadmitted Cortex-M inlining did not reject")

    taildup_without_contract = run(
        [str(args.bolt.resolve()), str(m3_taildup_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         f"--data={m3_taildup_profile}",
         "--tail-duplication=aggressive",
         "--tail-duplication-minimum-offset=0",
         "-o", str(output / "must-not-emit-unadmitted-taildup.elf")],
        logs / "reject-unadmitted-taildup.log",
    )
    if (
        taildup_without_contract.returncode == 0
        or "Cortex-M tail duplication changes execution multiplicity" not in
        (taildup_without_contract.stdout + taildup_without_contract.stderr)
    ):
        raise RuntimeError(
            "unadmitted Cortex-M tail duplication did not reject"
        )

    split_without_contract = run(
        [str(args.bolt.resolve()), str(m3_function_split_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         f"--data={M3_FUNCTION_SPLIT_PROFILE}",
         "--relocs", "--use-old-text", "--merge-text-sections",
         "--align-functions=4", "--align-text=4",
         "--split-functions", "--split-all-cold",
         "-o", str(output / "must-not-emit-unadmitted-split.elf")],
        logs / "reject-unadmitted-function-split.log",
    )
    if (
        split_without_contract.returncode == 0
        or "Cortex-M function splitting changes fragment placement" not in
        (split_without_contract.stdout + split_without_contract.stderr)
    ):
        raise RuntimeError(
            "unadmitted Cortex-M function splitting did not reject"
        )

    cdsplit_without_compact_model = run(
        [str(args.bolt.resolve()), str(m3_cdsplit_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         f"--data={M3_CDSPLIT_PROFILE}",
         "--relocs", "--use-old-text", "--merge-text-sections",
         "--split-functions", "--split-strategy=cdsplit",
         "--firmbolt-allow-function-splitting",
         "-o", str(output / "must-not-emit-cdsplit-longjmp.elf")],
        logs / "reject-cdsplit-without-compact-model.log",
    )
    if (
        cdsplit_without_compact_model.returncode == 0
        or "CDSplit is not supported with LongJmp" not in
        (cdsplit_without_compact_model.stdout
         + cdsplit_without_compact_model.stderr)
    ):
        raise RuntimeError(
            "Cortex-M CDSplit without a compatible branch model did not "
            "reject cleanly"
        )

    taildup_copy_propagation = run(
        [str(args.bolt.resolve()), str(m3_taildup_propagation_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         f"--data={m3_taildup_propagation_profile}",
         "--tail-duplication=aggressive",
         "--tail-duplication-minimum-offset=0",
         "--firmbolt-allow-tail-duplication",
         "--tail-duplication-const-copy-propagation",
         "-o", str(output / "must-not-emit-taildup-propagation.elf")],
        logs / "reject-taildup-copy-propagation.log",
    )
    if (
        taildup_copy_propagation.returncode == 0
        or "Cortex-M tail-duplication propagation removes "
           "register-defining instructions" not in
        (taildup_copy_propagation.stdout + taildup_copy_propagation.stderr)
    ):
        raise RuntimeError(
            "unqualified Cortex-M tail-duplication propagation did not reject"
        )

    loop_without_contract_report = (
        output / "loop-inversion-without-contract.json"
    )
    loop_without_contract = run(
        [str(args.bolt.resolve()), str(m3_loop_inversion_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         f"--data={m3_loop_inversion_profile}",
         "--firmbolt-preserve-guest-instruction-count=false",
         f"--fastdyn-firmware-report={loop_without_contract_report}",
         "-o", str(output / "loop-inversion-without-contract.elf")],
        logs / "skip-unadmitted-loop-inversion.log",
    )
    require_success(
        loop_without_contract,
        "unadmitted Cortex-M loop inversion conservative control",
    )
    loop_without_contract_data = json.loads(
        loop_without_contract_report.read_text()
    )
    loop_without_contract_worker = next(
        function for function in loop_without_contract_data["functions"]
        if function["name"] == "loop_worker"
    )
    if (
        int(loop_without_contract_data["summary"]
            ["firmware_modification_reason_counts"]
            ["loop_inversion"]) != 0
        or loop_without_contract_worker["firmware_modified"]
    ):
        raise RuntimeError(
            "Cortex-M loop inversion ran without its explicit contract"
        )

    peephole_without_contract = run(
        [str(args.bolt.resolve()), str(m3_peephole_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         "--peepholes=double-jumps",
         "-o", str(output / "must-not-emit-unadmitted-peephole.elf")],
        logs / "reject-unadmitted-peephole.log",
    )
    if (
        peephole_without_contract.returncode == 0
        or "control-flow peepholes change executed branch identities" not in
        (peephole_without_contract.stdout +
         peephole_without_contract.stderr)
    ):
        raise RuntimeError(
            "unadmitted Cortex-M control-flow peephole did not reject"
        )

    peephole_tail_traps = run(
        [str(args.bolt.resolve()), str(m3_peephole_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         "--peepholes=tailcall-traps",
         "--firmbolt-allow-control-flow-peepholes",
         "-o", str(output / "must-not-emit-tailcall-traps.elf")],
        logs / "reject-peephole-tailcall-traps.log",
    )
    if (
        peephole_tail_traps.returncode == 0
        or "tail-call trap insertion is not qualified" not in
        (peephole_tail_traps.stdout + peephole_tail_traps.stderr)
    ):
        raise RuntimeError(
            "unqualified Cortex-M tail-call trap insertion did not reject"
        )

    peephole_useless_branches = run(
        [str(args.bolt.resolve()), str(m3_peephole_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         "--peepholes=useless-branches",
         "--firmbolt-allow-control-flow-peepholes",
         "-o", str(output / "must-not-emit-useless-branches.elf")],
        logs / "reject-peephole-useless-branches.log",
    )
    if (
        peephole_useless_branches.returncode == 0
        or "useless-conditional-branch removal is not yet qualified" not in
        (peephole_useless_branches.stdout +
         peephole_useless_branches.stderr)
    ):
        raise RuntimeError(
            "unqualified Cortex-M useless-branch removal did not reject"
        )

    icp_without_contract = run(
        [str(args.bolt.resolve()), str(m3_icp_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         f"--data={m3_icp_profile}", "--icp=calls",
         "--icp-calls-topn=1", "--assume-abi",
         "-o", str(output / "must-not-emit-unadmitted-icp.elf")],
        logs / "reject-unadmitted-icp.log",
    )
    if (
        icp_without_contract.returncode == 0
        or "indirect-call promotion changes call and branch events" not in
        (icp_without_contract.stdout + icp_without_contract.stderr)
    ):
        raise RuntimeError(
            "unadmitted Cortex-M indirect-call promotion did not reject"
        )

    thumb1_icp_without_read_report = (
        output / "retain-thumb1-icp-without-read-contract.json"
    )
    thumb1_icp_without_read = run(
        [str(args.bolt.resolve()), str(m0_icp_image),
         "--firmbolt-cortex-m-cpu=cortex-m0plus",
         "--firmbolt-arm-full-pipeline",
         f"--data={m0_icp_profile}", "--icp=calls",
         "--icp-calls-topn=1", "--icp-top-callsites=0", "--assume-abi",
         "--firmbolt-allow-indirect-call-promotion",
         "--firmbolt-preserve-guest-instruction-count=false",
         "--eliminate-unreachable=false", "-v=1",
         f"--fastdyn-firmware-report={thumb1_icp_without_read_report}",
         "-o", str(output / "retain-thumb1-icp-without-read-contract.elf")],
        logs / "retain-thumb1-icp-without-read-contract.log",
    )
    thumb1_read_log = (
        thumb1_icp_without_read.stdout + thumb1_icp_without_read.stderr
    )
    thumb1_read_layout_path = Path(
        f"{thumb1_icp_without_read_report}.layout.json"
    )
    if (
        thumb1_icp_without_read.returncode != 0
        or "ICP failed in indirect_caller" not in thumb1_read_log
        or "unable to generate promoted call code" not in thumb1_read_log
        or not thumb1_read_layout_path.is_file()
    ):
        raise RuntimeError(
            "Thumb-1 ICP without its added-read contract was not retained"
        )
    thumb1_read_layout = json.loads(thumb1_read_layout_path.read_text())
    thumb1_retained_worker = next(
        function for function in thumb1_read_layout["functions"]
        if function["name"] == "indirect_caller"
    )
    thumb1_worker_start = symbol_address(m0_icp_image, "indirect_caller")
    if (
        "indirect_call_promotion" in
        thumb1_retained_worker["firmware_modification_reasons"]
        or virtual_bytes(m0_icp_image, thumb1_worker_start, 40)
            != virtual_bytes(
                output / "retain-thumb1-icp-without-read-contract.elf",
                thumb1_worker_start, 40,
            )
    ):
        raise RuntimeError(
            "Thumb-1 ICP changed the worker without the literal-read contract"
        )

    thumb1_tail_without_read_report = (
        output / "retain-thumb1-tail-icp-without-read-contract.json"
    )
    thumb1_tail_without_read = run(
        [str(args.bolt.resolve()), str(m0_icp_tail_image),
         "--firmbolt-cortex-m-cpu=cortex-m0plus",
         "--firmbolt-arm-full-pipeline",
         f"--data={m0_icp_tail_profile}", "--icp=calls",
         "--icp-calls-topn=1", "--icp-top-callsites=0", "--assume-abi",
         "--firmbolt-allow-indirect-call-promotion",
         "--firmbolt-allow-indirect-tail-call-promotion",
         "--firmbolt-preserve-guest-instruction-count=false",
         "--eliminate-unreachable=false", "-v=1",
         f"--fastdyn-firmware-report={thumb1_tail_without_read_report}",
         "-o", str(
             output / "retain-thumb1-tail-icp-without-read-contract.elf"
         )],
        logs / "retain-thumb1-tail-icp-without-read-contract.log",
    )
    thumb1_tail_read_log = (
        thumb1_tail_without_read.stdout + thumb1_tail_without_read.stderr
    )
    thumb1_tail_read_layout_path = Path(
        f"{thumb1_tail_without_read_report}.layout.json"
    )
    if (
        thumb1_tail_without_read.returncode != 0
        or "ICP failed in tail_dispatch" not in thumb1_tail_read_log
        or "unable to generate promoted call code" not in
            thumb1_tail_read_log
        or not thumb1_tail_read_layout_path.is_file()
    ):
        raise RuntimeError(
            "Thumb-1 tail ICP without its added-read contract was not retained"
        )
    thumb1_tail_read_layout = json.loads(
        thumb1_tail_read_layout_path.read_text()
    )
    thumb1_retained_tail_worker = next(
        function for function in thumb1_tail_read_layout["functions"]
        if function["name"] == "tail_dispatch"
    )
    thumb1_tail_worker_start = symbol_address(
        m0_icp_tail_image, "tail_dispatch"
    )
    if (
        "indirect_call_promotion" in
        thumb1_retained_tail_worker["firmware_modification_reasons"]
        or virtual_bytes(m0_icp_tail_image, thumb1_tail_worker_start, 38)
            != virtual_bytes(
                output / "retain-thumb1-tail-icp-without-read-contract.elf",
                thumb1_tail_worker_start, 38,
            )
    ):
        raise RuntimeError(
            "Thumb-1 tail ICP changed the worker without the literal-read "
            "contract"
        )

    thumb1_method_without_read_report = (
        output / "retain-thumb1-method-load-without-read-contract.json"
    )
    thumb1_method_without_read = run(
        [str(args.bolt.resolve()), str(m0_icp_method_load_image),
         "--firmbolt-cortex-m-cpu=cortex-m0plus",
         "--firmbolt-arm-full-pipeline",
         f"--data={m0_icp_method_load_profile}", "--icp=calls",
         "--icp-calls-topn=1", "--icp-top-callsites=0", "--assume-abi",
         "--firmbolt-allow-indirect-call-promotion",
         "--firmbolt-allow-method-load-elimination",
         "--firmbolt-preserve-guest-instruction-count=false",
         "--eliminate-unreachable=false", "-v=1",
         f"--fastdyn-firmware-report={thumb1_method_without_read_report}",
         "-o", str(
             output / "retain-thumb1-method-load-without-read-contract.elf"
         )],
        logs / "retain-thumb1-method-load-without-read-contract.log",
    )
    thumb1_method_read_log = (
        thumb1_method_without_read.stdout + thumb1_method_without_read.stderr
    )
    thumb1_method_read_layout_path = Path(
        f"{thumb1_method_without_read_report}.layout.json"
    )
    if (
        thumb1_method_without_read.returncode != 0
        or "ICP failed in virtual_caller" not in thumb1_method_read_log
        or "unable to generate promoted call code" not in
            thumb1_method_read_log
        or not thumb1_method_read_layout_path.is_file()
    ):
        raise RuntimeError(
            "Thumb-1 method ICP without its added-read contract was not "
            "retained"
        )
    thumb1_method_read_layout = json.loads(
        thumb1_method_read_layout_path.read_text()
    )
    thumb1_retained_method_worker = next(
        function for function in thumb1_method_read_layout["functions"]
        if function["name"] == "virtual_caller"
    )
    thumb1_method_worker_start = symbol_address(
        m0_icp_method_load_image, "virtual_caller"
    )
    if (
        "indirect_call_promotion" in
        thumb1_retained_method_worker["firmware_modification_reasons"]
        or virtual_bytes(
            m0_icp_method_load_image, thumb1_method_worker_start, 52,
        ) != virtual_bytes(
            output / "retain-thumb1-method-load-without-read-contract.elf",
            thumb1_method_worker_start, 52,
        )
    ):
        raise RuntimeError(
            "Thumb-1 method ICP changed the worker without the literal-read "
            "contract"
        )

    method_load_without_contract_report = (
        output / "retain-unadmitted-method-load.json"
    )
    method_load_without_contract = run(
        [str(args.bolt.resolve()), str(m3_icp_method_load_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         f"--data={m3_icp_method_load_profile}", "--icp=calls",
         "--icp-calls-topn=1", "--icp-top-callsites=0", "--assume-abi",
         "--firmbolt-allow-indirect-call-promotion",
         "--firmbolt-preserve-guest-instruction-count=false",
         "--eliminate-unreachable=true", "-v=2",
         f"--fastdyn-firmware-report={method_load_without_contract_report}",
         "-o", str(output / "retain-unadmitted-method-load.elf")],
        logs / "retain-unadmitted-method-load.log",
    )
    method_log = (
        method_load_without_contract.stdout
        + method_load_without_contract.stderr
    )
    if (
        method_load_without_contract.returncode != 0
        or "retained Cortex-M method load in virtual_caller: missing "
        "--firmbolt-allow-method-load-elimination" not in method_log
        or "loads eliminated = 0.0%" not in method_log
    ):
        raise RuntimeError(
            "unadmitted Cortex-M method load was not retained explicitly"
        )
    retained_layout_path = Path(
        f"{method_load_without_contract_report}.layout.json"
    )
    if not retained_layout_path.is_file():
        raise RuntimeError(
            "unadmitted method-load control did not publish a layout report"
        )
    retained_layout = json.loads(retained_layout_path.read_text())
    retained_worker = next(
        function for function in retained_layout["functions"]
        if function["name"] == "virtual_caller"
    )
    retained_start = symbol_address(
        m3_icp_method_load_image, "virtual_caller"
    )
    retained_method_identity = retained_start + 4
    retained_method_origins = [
        origin for origin in retained_worker["instruction_origins"]
        if origin.get("original_address") == retained_method_identity
        and origin["origin_kind"] == "original_instruction"
        and origin["may_load"]
    ]
    if (
        "method_load_elimination" in
        retained_worker["firmware_modification_reasons"]
        or len(retained_method_origins) != 1
        or int(retained_method_origins[0]["output_address"])
        >= min(
            int(origin["output_address"])
            for origin in retained_worker["instruction_origins"]
            if origin["origin_kind"] == "synthesized_call_promotion"
        )
    ):
        raise RuntimeError(
            "unadmitted Cortex-M method load did not remain before the "
            "ordinary ICP guard with its exact original identity"
        )

    icp_without_abi = run(
        [str(args.bolt.resolve()), str(m3_icp_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         f"--data={m3_icp_profile}", "--icp=calls",
         "--icp-calls-topn=1",
         "--firmbolt-allow-indirect-call-promotion",
         "-o", str(output / "must-not-emit-icp-without-abi.elf")],
        logs / "reject-icp-without-abi.log",
    )
    if (
        icp_without_abi.returncode == 0
        or "requires --assume-abi" not in
        (icp_without_abi.stdout + icp_without_abi.stderr)
    ):
        raise RuntimeError(
            "Cortex-M ICP without its AAPCS contract did not reject"
        )

    icp_all_mode = run(
        [str(args.bolt.resolve()), str(m3_icp_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         f"--data={m3_icp_profile}", "--icp=all",
         "--firmbolt-allow-indirect-call-promotion", "--assume-abi",
         "-o", str(output / "must-not-emit-icp-all-mode.elf")],
        logs / "reject-icp-all-mode.log",
    )
    if (
        icp_all_mode.returncode == 0
        or "jump-table promotion changes compare and branch events" not in
        (icp_all_mode.stdout + icp_all_mode.stderr)
    ):
        raise RuntimeError(
            "Cortex-M ICP all mode without the jump-table contract did not "
            "reject"
        )

    jump_table_icp_without_contract = run(
        [str(args.bolt.resolve()), str(m3_jump_table_icp_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         f"--data={m3_jump_table_icp_profile}", "--icp=jump-tables",
         "--icp-jt-topn=1", "--icp-top-callsites=0",
         "-o", str(output /
                   "must-not-emit-unadmitted-jump-table-icp.elf")],
        logs / "reject-unadmitted-jump-table-icp.log",
    )
    if (
        jump_table_icp_without_contract.returncode == 0
        or "jump-table promotion changes compare and branch events" not in
        (jump_table_icp_without_contract.stdout +
         jump_table_icp_without_contract.stderr)
    ):
        raise RuntimeError(
            "unadmitted Cortex-M jump-table promotion did not reject"
        )

    detached_without_contract_report = (
        output / "detached-abs32-without-contract.json"
    )
    detached_without_contract_output = (
        output / "detached-abs32-without-contract.elf"
    )
    detached_without_contract = run(
        [str(args.bolt.resolve()),
         str(m3_detached_abs32_jump_table_icp_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         f"--data={m3_detached_abs32_jump_table_icp_profile}",
         "--icp=jump-tables", "--icp-jt-topn=1",
         "--icp-top-callsites=0",
         "--firmbolt-allow-jump-table-promotion",
         "--firmbolt-preserve-guest-instruction-count=false",
         "--eliminate-unreachable=true", "-v=1",
         f"--fastdyn-firmware-report={detached_without_contract_report}",
         "-o", str(detached_without_contract_output)],
        logs / "retain-detached-abs32-without-contract.log",
    )
    require_success(
        detached_without_contract,
        "detached ABS32 table without atomic-update contract control",
    )
    detached_without_contract_log = (
        detached_without_contract.stdout + detached_without_contract.stderr
    )
    detached_without_contract_data = json.loads(
        detached_without_contract_report.read_text()
    )
    detached_without_contract_worker = next(
        function
        for function in detached_without_contract_data["functions"]
        if function["name"] == "detached_abs32_jump_table_worker"
    )
    detached_start = symbol_address(
        m3_detached_abs32_jump_table_icp_image,
        "detached_abs32_jump_table_worker",
    )
    detached_size = int(detached_without_contract_worker["original_size"])
    if (
        "ICP skipped detached Thumb ABS32 table" not in
        detached_without_contract_log
        or "explicit atomic update contract is incomplete" not in
        detached_without_contract_log
        or detached_without_contract_worker["firmware_modified"]
        or virtual_bytes(
            m3_detached_abs32_jump_table_icp_image,
            detached_start, detached_size,
        ) != virtual_bytes(
            detached_without_contract_output,
            detached_start, detached_size,
        )
    ):
        raise RuntimeError(
            "detached ABS32 switch changed without its explicit atomic "
            "function/table update contract"
        )

    icp_tail_without_contract = run(
        [str(args.bolt.resolve()), str(m3_icp_tail_image),
         "--firmbolt-cortex-m-cpu=cortex-m3",
         "--firmbolt-arm-full-pipeline",
         f"--data={m3_icp_tail_profile}", "--icp=calls",
         "--icp-calls-topn=1", "--icp-top-callsites=0", "--assume-abi",
         "--firmbolt-allow-indirect-call-promotion",
         "-v=1", "-o",
         str(output / "must-not-promote-unadmitted-icp-tail.elf")],
        logs / "retain-unadmitted-icp-tail.log",
    )
    if (
        icp_tail_without_contract.returncode != 0
        or "ICP skipped tail call in tail_dispatch" not in
        (icp_tail_without_contract.stdout +
         icp_tail_without_contract.stderr)
    ):
        raise RuntimeError(
            "unadmitted Cortex-M indirect tail call was not retained"
        )

    m0_literal_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m0_literal_execution(
            m0_literal_image, candidates["m0plus_literal"], output,
            args.fastdyn.resolve(),
        )
    )
    m0_movable_literals_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m0_two_word_execution(
            m0_movable_literals_image,
            candidates["m0plus_movable_literals"], output,
            args.fastdyn.resolve(),
            run_label="movable-literals", expected_words=(15, 15),
        )
    )
    m0_relocation_clone_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m0_two_word_execution(
            m0_relocation_clone_image,
            candidates["m0plus_relocation_clone"], output,
            args.fastdyn.resolve(),
            run_label="relocation-clone",
            expected_words=(0x12345678, 0x12345678),
        )
    )
    m0_branch_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m0_branch_execution(
            m0_branch_image, candidates["m0plus_branch"], output,
            args.fastdyn.resolve(),
        )
    )
    m0_call_veneer_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m0_call_veneer_execution(
            m0_call_veneer_image, candidates["m0plus_call_veneer"], output,
            args.fastdyn.resolve(),
        )
    )
    m0_reserved_veneer_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m0_call_veneer_execution(
            m0_reserved_veneer_image,
            candidates["m0plus_reserved_veneer"], output,
            args.fastdyn.resolve(),
        )
    )
    m0_icp_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m0_two_word_execution(
            m0_icp_image, candidates["m0plus_icp"], output,
            args.fastdyn.resolve(), run_label="native-thumb1-icp",
            expected_words=(13, 35),
        )
    )
    m0_icp_tail_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m0_two_word_execution(
            m0_icp_tail_image, candidates["m0plus_icp_tail"], output,
            args.fastdyn.resolve(), run_label="native-thumb1-tail-icp",
            expected_words=(13, 35),
        )
    )
    m0_icp_method_load_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m0_method_load_fault_execution(
            m0_icp_method_load_image,
            candidates["m0plus_icp_method_load"], output,
            args.fastdyn.resolve(),
            results["m0plus_icp_method_load"]["final_layout_report"],
        )
    )
    m3_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_execution(m3_image, candidates["m3"], output,
                              args.fastdyn.resolve())
    )
    m3_jump6_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_single_word_execution(
            m3_jump6_image, candidates["m3_jump6"], output,
            args.fastdyn.resolve(), "jump6",
        )
    )
    m3_narrow_data_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_single_word_execution(
            m3_narrow_data_image, candidates["m3_narrow_data"], output,
            args.fastdyn.resolve(), "narrow-data",
        )
    )
    m3_inline_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_single_word_execution(
            m3_inline_image, candidates["m3_inline"], output,
            args.fastdyn.resolve(), "native-inlining", expected_word=43,
        )
    )
    m3_taildup_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_single_word_execution(
            m3_taildup_image, candidates["m3_taildup"], output,
            args.fastdyn.resolve(), "native-taildup", expected_word=3,
        )
    )
    m3_taildup_propagation_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_words_execution(
            m3_taildup_propagation_image,
            candidates["m3_taildup_propagation"], output,
            args.fastdyn.resolve(), "native-taildup-propagation",
            expected_words=(5, 10),
        )
    )
    m3_rodata_load_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_words_execution(
            m3_rodata_load_image, candidates["m3_rodata_load"], output,
            args.fastdyn.resolve(), "native-read-only-load",
            expected_words=(0x1234, 0x5678),
        )
    )
    m3_function_reorder_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_words_execution(
            m3_function_reorder_image,
            candidates["m3_function_reorder"], output,
            args.fastdyn.resolve(), "whole-image-function-reorder",
            expected_words=(17, 42),
        )
    )
    m3_profile_function_reorder_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_words_execution(
            m3_profile_function_reorder_image,
            candidates["m3_profile_function_reorder"], output,
            args.fastdyn.resolve(), "profile-function-reorder",
            expected_words=(17, 42),
        )
    )
    m3_callgraph_function_reorder_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_words_execution(
            m3_callgraph_function_reorder_image,
            candidates["m3_callgraph_function_reorder"], output,
            args.fastdyn.resolve(), "callgraph-function-reorder",
            expected_words=(17, 42),
        )
    )
    m3_function_split_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_words_execution(
            m3_function_split_image,
            candidates["m3_function_split"], output,
            args.fastdyn.resolve(), "native-function-split",
            expected_words=(17, 42),
        )
    )
    m3_cdsplit_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_words_execution(
            m3_cdsplit_image,
            candidates["m3_cdsplit"], output,
            args.fastdyn.resolve(), "native-cdsplit",
            expected_words=(5, 42, 2),
        )
    )
    m3_loop_inversion_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_single_word_execution(
            m3_loop_inversion_image, candidates["m3_loop_inversion"],
            output, args.fastdyn.resolve(), "native-loop-inversion",
            expected_word=4,
        )
    )
    m3_peephole_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_single_word_execution(
            m3_peephole_image, candidates["m3_peephole"], output,
            args.fastdyn.resolve(), "native-control-flow-peephole",
            expected_word=5,
        )
    )
    m3_icp_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_single_word_execution(
            m3_icp_image, candidates["m3_icp"], output,
            args.fastdyn.resolve(), "native-indirect-call-promotion",
            expected_word=17,
        )
    )
    m3_icp_r12_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_single_word_execution(
            m3_icp_r12_image, candidates["m3_icp_r12"], output,
            args.fastdyn.resolve(), "native-r12-indirect-call-promotion",
            expected_word=17,
        )
    )
    m3_icp_method_load_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_method_load_fault_execution(
            m3_icp_method_load_image,
            candidates["m3_icp_method_load"], output,
            args.fastdyn.resolve(),
            results["m3_icp_method_load"]["final_layout_report"],
        )
    )
    m3_icp_tail_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_single_word_execution(
            m3_icp_tail_image, candidates["m3_icp_tail"], output,
            args.fastdyn.resolve(), "native-indirect-tail-call-promotion",
            expected_word=19,
        )
    )
    m3_jump_table_icp_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_single_word_execution(
            m3_jump_table_icp_image,
            candidates["m3_jump_table_icp"], output,
            args.fastdyn.resolve(), "native-jump-table-promotion",
            expected_word=10,
        )
    )
    m3_abs32_jump_table_icp_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_words_execution(
            m3_abs32_jump_table_icp_image,
            candidates["m3_abs32_jump_table_icp"], output,
            args.fastdyn.resolve(), "native-abs32-jump-table-promotion",
            expected_words=(10, 20, 30, 0),
        )
    )
    m3_detached_abs32_jump_table_icp_execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m3_words_execution(
            m3_detached_abs32_jump_table_icp_image,
            candidates["m3_detached_abs32_jump_table_icp"], output,
            args.fastdyn.resolve(),
            "native-detached-abs32-jump-table-promotion",
            expected_words=(10, 20, 30, 0),
        )
    )
    execution = (
        {"status": "skipped_by_request"} if args.skip_fastdyn else
        validate_m4_execution(candidates["m4"], output, args.fastdyn.resolve())
    )
    accepted = all(
        item["status"] in {"accepted", "skipped_by_request"}
        for item in (
            m0_literal_execution, m0_movable_literals_execution,
            m0_relocation_clone_execution,
            m0_branch_execution,
            m0_call_veneer_execution, m0_reserved_veneer_execution,
            m0_icp_execution, m0_icp_tail_execution,
            m0_icp_method_load_execution,
            m3_execution, m3_jump6_execution, m3_narrow_data_execution,
            m3_inline_execution, m3_taildup_execution,
            m3_taildup_propagation_execution,
            m3_rodata_load_execution,
            m3_function_reorder_execution,
            m3_profile_function_reorder_execution,
            m3_callgraph_function_reorder_execution,
            m3_function_split_execution,
            m3_cdsplit_execution,
            m3_loop_inversion_execution,
            m3_peephole_execution,
            m3_icp_execution,
            m3_icp_r12_execution,
            m3_icp_method_load_execution,
            m3_icp_tail_execution,
            m3_jump_table_icp_execution,
            m3_abs32_jump_table_icp_execution,
            m3_detached_abs32_jump_table_icp_execution,
            execution
        )
    )
    report = {
        "schema_version": 1,
        "status": "accepted" if accepted else "rejected",
        "classification": (
            "normal_bolt_pipeline_with_thumb_fop_qualification"
            if args.full_pipeline and args.frame_opt != "none" else
            "normal_bolt_pipeline_cortex_m_qualification"
            if args.full_pipeline else
            "conservative_direct_bolt_cortex_m_backend"
        ),
        "optimization_policy": {
            "normal_bolt_pipeline": args.full_pipeline,
            "frame_opt": args.frame_opt,
            "experimental_store_removal": False,
            "preserve_guest_instruction_count": True,
            "preserve_guest_instruction_count_scope": (
                "default; explicitly disabled only for the "
                "m0plus_relocation_clone, m0plus_branch, "
                "m0plus_call_veneer, m3_taildup and "
                "m3_taildup_propagation fixtures"
                "; the m3_loop_inversion fixture also admits branch-count "
                "changes; m0plus_icp, m0plus_icp_tail, "
                "m0plus_icp_method_load, m3_icp, "
                "m3_icp_r12, m3_icp_tail, "
                "m3_icp_method_load, "
                "m3_jump_table_icp and "
                "m3_abs32_jump_table_icp plus the relocation-complete "
                "m3_detached_abs32_jump_table_icp admit "
                "checked expansions into proven unreachable symbol-bounded "
                "capacity"
            ),
            "eliminate_unreachable": False,
            "call_boundary_changes": (
                "explicitly admitted only for the m3_inline, m0plus_icp, "
                "m0plus_icp_tail, m0plus_icp_method_load, m3_icp, m3_icp_r12, "
                "m3_icp_method_load and m3_icp_tail fixtures"
            ),
            "execution_multiplicity_changes": (
                "explicitly admitted only for the m3_taildup, "
                "m3_taildup_propagation and m3_loop_inversion fixtures; "
                "the propagation fixture separately admits deletion of a "
                "register-only definition proven not live-out; executed "
                "branch-identity "
                "changes are also admitted for m3_peephole"
                "; call/branch-event changes are admitted for m0plus_icp, "
                "m0plus_icp_tail, m0plus_icp_method_load, m3_icp, "
                "m3_icp_r12 and m3_icp_tail; "
                "Thumb-1 additionally "
                "admits one immutable flash target-word read and preserves "
                "one argument register through LR for ordinary calls or "
                "through AAPCS r12/IP for tail calls; AAPCS r12/IP "
                "scratch applies to the Thumb-2 cases; "
                "immutable method-slot read elision is separately admitted "
                "for m0plus_icp_method_load and m3_icp_method_load while "
                "their cold fallbacks retain the original read; "
                "the TBB and "
                "ABS32 fixtures separately admit a flags-dead compare/branch "
                "guard; the detached ABS32 fixture additionally admits an "
                "atomic update of its exact retained relocation graph"
            ),
            "read_only_load_elision": (
                "explicitly admitted only for m3_rodata_load; the loaded "
                "object must be allocatable, non-writable, scalar and free "
                "of retained relocations, and the contract admits removal "
                "of that flash read/fault/modeled-time event"
            ),
        },
        "bolt_revision": revision,
        "bolt_binary": {
            "path": str(args.bolt.resolve()),
            "sha256": sha256(args.bolt.resolve()),
        },
        "host": {"platform": platform.platform(), "python": sys.version},
        "profiles": results,
        "training_profiles": {
            "m0plus_indirect_caller": {
                "path": str(m0_icp_profile),
                "sha256": sha256(m0_icp_profile),
                "format": "BOLT function-relative indirect-call profile",
                "policy": "one hot ordinary Thumb-1 register-call target "
                          "under explicit AAPCS, call-event and immutable "
                          "flash target-read contracts",
            },
            "m0plus_indirect_tail_dispatch": {
                "path": str(m0_icp_tail_profile),
                "sha256": sha256(m0_icp_tail_profile),
                "format": "BOLT function-relative indirect-call profile",
                "policy": "one hot Thumb-1 register tail-call target under "
                          "explicit AAPCS, tail-call-event and immutable "
                          "flash target-read contracts; r12 preserves one "
                          "borrowed low argument register and LR is retained",
            },
            "m0plus_method_load_indirect_caller": {
                "path": str(m0_icp_method_load_profile),
                "sha256": sha256(m0_icp_method_load_profile),
                "format": "BOLT branch and memory-access profile records",
                "policy": "one exact mapped immutable Thumb-1 vtable slot; "
                          "hot load elision requires independent read-elision "
                          "and added-flash-read contracts; the cold load and "
                          "its precise boundary-crossing fault are retained",
            },
            "m3_profiled_worker": {
                "path": str(m3_profile),
                "sha256": sha256(m3_profile),
                "format": "BOLT no-LBR function-relative profile",
                "policy": "ext-tsp",
            },
            "m3_profile_function_order": {
                "path": str(M3_FUNCTION_REORDER_PROFILE),
                "sha256": sha256(M3_FUNCTION_REORDER_PROFILE),
                "format": "BOLT no-LBR function-relative profile",
                "policy": "exec-count whole-image function order inside "
                          "the linker-qualified input .text flash extent",
            },
            "m3_callgraph_function_order": {
                "path": str(M3_FUNCTION_REORDER_CALLGRAPH_PROFILE),
                "sha256": sha256(M3_FUNCTION_REORDER_CALLGRAPH_PROFILE),
                "format": "BOLT function-relative branch/call profile",
                "policy": "CDSort call-graph whole-image function order "
                          "inside the linker-qualified input .text flash "
                          "extent",
            },
            "m3_function_split": {
                "path": str(M3_FUNCTION_SPLIT_PROFILE),
                "sha256": sha256(M3_FUNCTION_SPLIT_PROFILE),
                "format": "BOLT no-LBR function-relative profile",
                "policy": "Profile2 hot/cold splitting under explicit "
                          "fragment-placement and cross-fragment branch-event "
                          "contracts inside linker-qualified .text capacity",
            },
            "m3_cdsplit": {
                "path": str(M3_CDSPLIT_PROFILE),
                "sha256": sha256(M3_CDSPLIT_PROFILE),
                "format": "BOLT function-relative branch/call profile",
                "policy": "cache-directed hot/warm/cold splitting under "
                          "explicit fragment-placement and cross-fragment "
                          "branch-event contracts, using the compatible "
                          "compact branch model inside exact linker-owned "
                          ".text capacity",
            },
            "m3_taildup_worker": {
                "path": str(m3_taildup_profile),
                "sha256": sha256(m3_taildup_profile),
                "format": "BOLT no-LBR function-relative profile",
                "policy": "aggressive tail duplication with zero minimum "
                          "offset",
            },
            "m3_taildup_propagation_worker": {
                "path": str(m3_taildup_propagation_profile),
                "sha256": sha256(m3_taildup_propagation_profile),
                "format": "BOLT no-LBR function-relative profile",
                "policy": "aggressive tail duplication plus checked "
                          "register-only constant propagation under a "
                          "separate definition-deletion contract",
            },
            "m3_loop_worker": {
                "path": str(m3_loop_inversion_profile),
                "sha256": sha256(m3_loop_inversion_profile),
                "format": "BOLT no-LBR function-relative profile",
                "policy": "loop inversion under an explicit branch-timing "
                          "contract",
            },
            "m3_indirect_caller": {
                "path": str(m3_icp_profile),
                "sha256": sha256(m3_icp_profile),
                "format": "BOLT function-relative indirect-call profile",
                "policy": "one hot ordinary Thumb-2 register-call target "
                          "under explicit AAPCS and call-event contracts",
            },
            "m3_r12_indirect_caller": {
                "path": str(m3_icp_r12_profile),
                "sha256": sha256(m3_icp_r12_profile),
                "format": "BOLT function-relative indirect-call profile",
                "policy": "one hot ordinary Thumb-2 r12/IP call target; "
                          "LR is guard scratch because both admitted call "
                          "paths overwrite it",
            },
            "m3_method_load_indirect_caller": {
                "path": str(m3_icp_method_load_profile),
                "sha256": sha256(m3_icp_method_load_profile),
                "format": "BOLT branch and memory-access profile records",
                "policy": "one exact mapped immutable vtable method slot; "
                          "hot load elision under an explicit read-elision "
                          "contract with the original cold load retained",
            },
            "m3_indirect_tail_dispatch": {
                "path": str(m3_icp_tail_profile),
                "sha256": sha256(m3_icp_tail_profile),
                "format": "BOLT function-relative indirect-call profile",
                "policy": "one hot Thumb-2 register tail-call target under "
                          "explicit AAPCS, tail-call-event and conservative "
                          "stack-restore epilogue contracts",
            },
            "m3_jump_table_worker": {
                "path": str(m3_jump_table_icp_profile),
                "sha256": sha256(m3_jump_table_icp_profile),
                "format": "BOLT function-relative TBB edge profile",
                "policy": "one hot typed TBB index under an explicit "
                          "flags-dead compare/branch-event contract",
            },
            "m3_abs32_jump_table_worker": {
                "path": str(m3_abs32_jump_table_icp_profile),
                "sha256": sha256(m3_abs32_jump_table_icp_profile),
                "format": "BOLT function-relative ABS32 switch edge profile",
                "policy": "one hot typed ABS32 index under an explicit "
                          "flags-dead compare/branch-event and atomic "
                          "in-function reference contract",
            },
            "m3_detached_abs32_jump_table_worker": {
                "path": str(m3_detached_abs32_jump_table_icp_profile),
                "sha256": sha256(
                    m3_detached_abs32_jump_table_icp_profile
                ),
                "format": "BOLT function-relative ABS32 switch edge profile",
                "policy": "one hot detached ABS32 index under explicit "
                          "flags-dead compare/branch-event and atomic "
                          "retained-relocation update contracts",
            },
        },
        "negative_controls": {
            "m4_as_m3_rejected": True,
            "experimental_store_removal_rejected": True,
            "unadmitted_read_only_load_elision_rejected": True,
            "unadmitted_inlining_rejected": True,
            "unadmitted_tail_duplication_rejected": True,
            "unadmitted_function_splitting_rejected": True,
            "cdsplit_without_compact_branch_model_rejected": True,
            "tail_duplication_propagation_without_contract_rejected": True,
            "unadmitted_loop_inversion_left_unchanged": True,
            "unadmitted_control_flow_peephole_rejected": True,
            "control_flow_peephole_tailcall_traps_rejected": True,
            "control_flow_peephole_useless_branches_rejected": True,
            "unadmitted_indirect_call_promotion_rejected": True,
            "thumb1_icp_without_added_read_left_unchanged": True,
            "thumb1_tail_icp_without_added_read_left_unchanged": True,
            "thumb1_method_icp_without_added_read_left_unchanged": True,
            "unadmitted_method_load_elimination_retained": True,
            "indirect_call_promotion_without_abi_rejected": True,
            "indirect_call_promotion_all_mode_rejected": True,
            "indirect_tail_call_promotion_without_contract_retained": True,
            "unadmitted_jump_table_promotion_rejected": True,
            "detached_abs32_without_atomic_update_left_unchanged": True,
        },
        "m0plus_literal_architectural_execution": m0_literal_execution,
        "m0plus_movable_literals_architectural_execution":
            m0_movable_literals_execution,
        "m0plus_relocation_clone_architectural_execution":
            m0_relocation_clone_execution,
        "m0plus_branch_architectural_execution": m0_branch_execution,
        "m0plus_call_veneer_architectural_execution":
            m0_call_veneer_execution,
        "m0plus_reserved_veneer_architectural_execution":
            m0_reserved_veneer_execution,
        "m0plus_native_indirect_call_promotion_architectural_execution":
            m0_icp_execution,
        "m0plus_native_indirect_tail_call_promotion_architectural_execution":
            m0_icp_tail_execution,
        "m0plus_native_method_load_elimination_architectural_execution":
            m0_icp_method_load_execution,
        "m3_architectural_execution": m3_execution,
        "m3_jump6_architectural_execution": m3_jump6_execution,
        "m3_narrow_data_architectural_execution":
            m3_narrow_data_execution,
        "m3_native_inlining_architectural_execution":
            m3_inline_execution,
        "m3_native_tail_duplication_architectural_execution":
            m3_taildup_execution,
        "m3_native_tail_duplication_propagation_architectural_execution":
            m3_taildup_propagation_execution,
        "m3_native_read_only_load_architectural_execution":
            m3_rodata_load_execution,
        "m3_whole_image_function_reorder_architectural_execution":
            m3_function_reorder_execution,
        "m3_profile_driven_function_reorder_architectural_execution":
            m3_profile_function_reorder_execution,
        "m3_callgraph_function_reorder_architectural_execution":
            m3_callgraph_function_reorder_execution,
        "m3_native_function_split_architectural_execution":
            m3_function_split_execution,
        "m3_native_cdsplit_architectural_execution":
            m3_cdsplit_execution,
        "m3_native_loop_inversion_architectural_execution":
            m3_loop_inversion_execution,
        "m3_native_control_flow_peephole_architectural_execution":
            m3_peephole_execution,
        "m3_native_indirect_call_promotion_architectural_execution":
            m3_icp_execution,
        "m3_native_r12_indirect_call_promotion_architectural_execution":
            m3_icp_r12_execution,
        "m3_native_method_load_elimination_architectural_execution":
            m3_icp_method_load_execution,
        "m3_native_indirect_tail_call_promotion_architectural_execution":
            m3_icp_tail_execution,
        "m3_native_jump_table_promotion_architectural_execution":
            m3_jump_table_icp_execution,
        "m3_native_abs32_jump_table_promotion_architectural_execution":
            m3_abs32_jump_table_icp_execution,
        "m3_native_detached_abs32_jump_table_promotion_architectural_execution":
            m3_detached_abs32_jump_table_icp_execution,
        "fastdyn_execution": execution,
        "scope": {
            "emission": (
                "compact in-place publication by default; the explicit "
                "m3_function_reorder, m3_profile_function_reorder and "
                "m3_callgraph_function_reorder gates perform atomic "
                "whole-image relocation inside the exact original .text "
                "PT_LOAD; m3_function_split and m3_cdsplit additionally "
                "qualify two- and three-way fragments inside exact "
                "linker-owned capacity"
            ),
            "unknown_functions": (
                "compact mode retains the original implementation; "
                "relocation mode rejects the whole candidate atomically"
            ),
            "patch_contract": "empty in phase one",
            "new_allocatable_sections": "rejected",
            "m0plus_execution": (
                "Thumb-1 literal/ADR, two movable interleaved literal pools, "
                "relocation-bearing shared-pool cloning, "
                "installed local-trampoline, and "
                "literal-backed call-veneer and native Thumb-1 ordinary/tail "
                "indirect-call plus immutable-vtable method-load promotion "
                "fixtures executed on cortex-m0; all ICP fixtures execute "
                "direct and cold fallback paths "
                "while checking argument-register restoration, and the tail "
                "fixture additionally checks preservation of incoming LR; "
                "the method fixture maps a precise cold-load HardFault back "
                "to its original instruction identity; "
                "one veneer uses an explicit external typed slot; "
                "original ESCape32 board/device "
                "execution remains unavailable"
            ),
            "m3_narrow_data_execution": (
                "R_ARM_ABS8/R_ARM_ABS16 fields in typed data embedded in "
                "executable flash are retained exactly and consumed by "
                "reference/candidate Cortex-M3 execution"
            ),
        },
        "backend_capabilities": {
            "qualified": [
                "Cortex-M0/M0+ Armv6-M, M3 Armv7-M, M4/M7 Armv7E-M, M23 Armv8-M Baseline, M33 Armv8-M Mainline, and M55/M85 Armv8.1-M Mainline CPU matching",
                "ARM/Thumb/data mapping-symbol separation",
                "direct calls and conditional/unconditional Thumb branches",
                "ABS/REL/PREL and qualified Thumb branch/literal/MOVW/MOVT relocations",
                "R_ARM_THM_JUMP6 CBZ/CBNZ wrapped-REL handling, forward-range validation, retained relocation publication, and Cortex-M3 execution",
                "R_ARM_ABS8/R_ARM_ABS16 signed REL addends, signed-or-unsigned overflow ranges, typed executable-section data ownership, retained relocation publication, and Cortex-M3 execution",
                "Thumb-2 ADR.W and PC-relative MOVW/MOVT instruction references",
                "fixed-address literal pools and firmware allocated-range preservation",
                "guarded interleaved/shared and terminal non-fallthrough Thumb-1 literal pools with exact-address prepublication fallback and final validation, typed ARM SHT_REL dependencies, mapping-symbol reconstruction, and Cortex-M0 execution",
                "retained ARM SHT_REL source-offset remapping for moved instruction-owned data and symbol-index remapping after output symtab rebuilding",
                "relaxed value-only shared-pool cloning into explicit symbol-bounded typed capacity, with address-producing ADR clones rejected",
                "retained ARM SHT_REL growth for relocation-bearing value-pool clones, with one record per live copy",
                "PC- and ADR-register-based TBB/TBH recovery with independent branch/table bases, symbolic regeneration, and identity-aware validation",
                "Thumb-1 ABS32 switch-table recovery and exact target validation",
                "installed Thumb-1 local conditional-branch relaxation with exact synthesized-edge origins and Cortex-M0 execution",
                "Thumb-1 literal-backed absolute call veneers with explicit fixed-address padding, r3 call-scratch, exact literal/reference provenance, and Cortex-M0 execution",
                "external Thumb-1 absolute call veneers in explicit word-aligned .firmbolt.veneer slots, under the default source instruction-count contract",
                "opt-in native BOLT indirect-call promotion for profiled ordinary Thumb-1 register calls under explicit AAPCS, call-event and immutable-flash target-read contracts, with LR-based argument preservation, word-aligned typed target data, exact direct/fallback origins, compact in-place emission, a no-read-contract retention control, and Cortex-M0 hot/cold execution",
                "opt-in native BOLT indirect-tail-call promotion for profiled Thumb-1 BX epilogues under conservative stack-restore recognition plus explicit AAPCS, tail-call-event and immutable-flash target-read contracts, with r12-based argument preservation, incoming-LR retention, word-aligned typed target data, exact direct/fallback origins, compact in-place emission, a no-read-contract retention control, and Cortex-M0 hot/cold execution",
                "opt-in native BOLT virtual-method-load elimination for exact profiled Thumb-1 slots in mapped allocatable non-writable vtables under independent read-elision and immutable-flash guard-read contracts, with raw typed vtable-address data, the original method load retained in the cold fallback, exact origins, compact in-place emission, hot/cold execution and a precise mapped-boundary HardFault compared through original identity on Cortex-M0",
                "Thumb-2 absolute call veneers in a qualified caller-owned range, with r12 call-scratch and an explicit Thumb-state target bit",
                "exact synthesized-call-veneer attribution and linker-resolved output PCs",
                "Cortex-M4 DSP/FPU feature-qualified decode and re-emission",
                "profile-driven Profile2 hot/cold and cache-directed CDSplit hot/warm/cold function splitting inside exact linker-owned flash capacity, with compact-model branch repair, protected vector entries, exact fragment/origin ledgers, and Cortex-M3 execution of trained and held-out paths",
                "Cortex-M33 TrustZone instruction decode with whole-function preservation",
                "Cortex-M55 MVE integer/floating-point decode and exact re-emission",
                "Cortex-M85 PACBTI-M decode with whole-function preservation",
                "Cortex-M vector import with exact-entry handler contracts",
                "same-count ordinary FOP in exact vector handlers with stores and exception return retained",
                "ordinary redundant stack-load forwarding with stores retained",
                "opt-in native BOLT inlining for direct Thumb calls with protected call-boundary admission, exact clone identities, compact in-place emission, and Cortex-M3 execution",
                "opt-in native BOLT tail duplication for profiled Thumb blocks with execution-multiplicity admission, exact clone identities, compact in-place emission, and Cortex-M3 execution",
                "opt-in register-only constant/copy propagation after Thumb tail duplication under a separate definition-deletion contract, with descriptor-aware Thumb MOV source discovery, encoding-class-checked replacements, exact retained/clone identities, compact publication and Cortex-M3 hot/cold execution",
                "opt-in native BOLT read-only-load simplification for linked allocatable non-writable relocation-free scalar data on Thumb-2, using flag-preserving MOVW/MOVT materialization under an explicit read/fault/timing-elision contract, exact transformed-source origins, compact in-place emission, and Cortex-M3 execution",
                "explicit ARM relocation mode with native BOLT user-directed, profile exec-count and call-graph CDSort whole-image function ordering inside the linker-qualified input .text flash extent, repaired direct calls and vector/entry Thumb pointers, live-section SHT_REL ownership, moved alternate labels, exact origins, atomic rejection, unchanged PT_LOAD ranges, and Cortex-M3 execution",
                "opt-in native BOLT Profile2 hot/cold function splitting inside explicit linker-owned capacity, with vector/secondary entries protected, exact fragment and instruction-origin ledgers, widened cross-fragment branch repair, live SHT_REL ownership, unchanged PT_LOAD ranges, held-out cold-path Cortex-M3 execution, and atomic rejection without the fragment-placement/branch-event contract",
                "opt-in native BOLT loop inversion for profiled Thumb loops with branch-timing admission, exact moved/synthesized instruction origins, compact in-place emission, and Cortex-M3 execution",
                "opt-in native BOLT double-jump control-flow peepholes with branch-event admission, exact retained instruction origins, compact in-place emission, and Cortex-M3 execution",
                "opt-in native BOLT indirect-call promotion for profiled ordinary Thumb-2 register calls under explicit AAPCS and call-event contracts, including r12/IP targets using LR as non-memory guard scratch because both call paths overwrite it, with exact direct/fallback identities, compact in-place emission, and Cortex-M3 execution",
                "opt-in native BOLT virtual-method-load elimination for exact profiled slots in mapped allocatable non-writable vtables under an explicit read-elision contract, with raw data-address guarding, the original method load retained in the cold fallback, exact origin identities, compact in-place emission, and Cortex-M3 hot/cold execution",
                "opt-in native BOLT indirect-tail-call promotion for profiled Thumb-2 BX epilogues under conservative stack-restore recognition plus AAPCS and tail-call-event contracts, with exact direct/fallback identities, compact in-place emission, and Cortex-M3 execution",
                "opt-in native BOLT hot-index promotion for recovered inline TBB/TBH and atomic in-function ABS32 switch tables, with exact guard/fallback origins, relocated literal/table references, compact in-place emission, and Cortex-M3 execution",
                "opt-in native BOLT hot-index promotion for detached read-only ABS32 switch tables with complete retained pointer/entry relocations and an explicit atomic-update contract, including exact pointer-source remapping, table-entry regeneration, compact in-place emission, and Cortex-M3 hot/cold/default execution",
                "original-instruction, forwarding-dependency, and per-pass publication ledger",
                "compact publication of only audited modified functions",
            ],
            "retained_opaque_or_rejected": [
                "ARM-state regions on Thumb-only Cortex-M",
                "TBB/TBH bases produced by non-ADR or ambiguous data flow",
                "switch bounds reloaded across observable memory or asynchronous effects",
                "experimental stack-store removal",
                "secondary vector entries and handler transformations beyond the same-count FOP contract",
                "TrustZone gateway, security-test, and Secure/Non-secure transition transformations",
                "PACBTI-M function transformations",
                "layout changes that violate strict modeled-time state",
                "tail-call trap peepholes because post-tail-call execution and fault semantics are not qualified",
                "useless-conditional-branch peepholes until their removed branch events and all admitted entries are separately qualified",
                "method-load elimination for mutable, unmapped, indexed, writeback, negative-offset, conditional, narrow, SP/PC or otherwise unsupported fetches; shared ABS32 jump tables or detached tables without a complete unique retained reference graph; Thumb-1 r12-target tail calls without a genuinely dead low guard register; and tail calls without conservative epilogue evidence",
                "read-only scalar loads on Thumb-1, relocation-bearing values, writable or unallocated data, and local literal pools whose ownership/reference graph is not complete; attempted unresolved-pool rewrites retain the original body and immutable origin ledger",
            ],
            "independent_producer_limits": [
                "LLVM MC relaxes an unresolved CBZ/CBNZ fixup to a hint, so the JUMP6 execution gate materializes a conforming resolved ET_EXEC directly",
                "the pinned LLD rejects R_ARM_THM_JUMP6 relocation 52; this is distinct from Firm-BOLT and AArch32 JITLink support",
            ],
            "not_yet_qualified": [
                "Thumb literal islands without a proven non-fallthrough owner",
                "additional ARM relocation classes beyond the modeled compact set",
                "out-of-function Thumb veneer placement without explicit typed slots or under arbitrary whole-image layout",
                "HFSort/Pettis-Hansen parity and function splitting for jump-table, address-stable, veneer-slot or secondary-entry functions",
                "DSP/FPU/MVE-specific optimization rewrites",
            ],
        },
    }
    (output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "status": report["status"],
        "report": str(output / "report.json"),
        "m0plus_candidate": str(candidates["m0plus"]),
        "m0plus_literal_candidate": str(candidates["m0plus_literal"]),
        "m0plus_movable_literals_candidate": str(
            candidates["m0plus_movable_literals"]
        ),
        "m0plus_moved_relocation_candidate": str(
            candidates["m0plus_moved_relocation"]
        ),
        "m0plus_relocation_clone_candidate": str(
            candidates["m0plus_relocation_clone"]
        ),
        "m0plus_branch_candidate": str(candidates["m0plus_branch"]),
        "m0plus_call_veneer_candidate":
            str(candidates["m0plus_call_veneer"]),
        "m0plus_reserved_veneer_candidate":
            str(candidates["m0plus_reserved_veneer"]),
        "m0plus_icp_candidate": str(candidates["m0plus_icp"]),
        "m0plus_icp_tail_candidate": str(candidates["m0plus_icp_tail"]),
        "m0plus_icp_method_load_candidate":
            str(candidates["m0plus_icp_method_load"]),
        "m3_candidate": str(candidates["m3"]),
        "m3_jump6_candidate": str(candidates["m3_jump6"]),
        "m3_narrow_data_candidate": str(candidates["m3_narrow_data"]),
        "m3_inline_candidate": str(candidates["m3_inline"]),
        "m3_taildup_candidate": str(candidates["m3_taildup"]),
        "m3_taildup_propagation_candidate":
            str(candidates["m3_taildup_propagation"]),
        "m3_rodata_load_candidate": str(candidates["m3_rodata_load"]),
        "m3_function_reorder_candidate":
            str(candidates["m3_function_reorder"]),
        "m3_profile_function_reorder_candidate":
            str(candidates["m3_profile_function_reorder"]),
        "m3_callgraph_function_reorder_candidate":
            str(candidates["m3_callgraph_function_reorder"]),
        "m3_function_split_candidate":
            str(candidates["m3_function_split"]),
        "m3_cdsplit_candidate": str(candidates["m3_cdsplit"]),
        "m3_loop_inversion_candidate":
            str(candidates["m3_loop_inversion"]),
        "m3_peephole_candidate": str(candidates["m3_peephole"]),
        "m3_icp_candidate": str(candidates["m3_icp"]),
        "m3_icp_r12_candidate": str(candidates["m3_icp_r12"]),
        "m3_icp_method_load_candidate":
            str(candidates["m3_icp_method_load"]),
        "m3_icp_tail_candidate": str(candidates["m3_icp_tail"]),
        "m3_jump_table_icp_candidate":
            str(candidates["m3_jump_table_icp"]),
        "m3_abs32_jump_table_icp_candidate":
            str(candidates["m3_abs32_jump_table_icp"]),
        "m3_detached_abs32_jump_table_icp_candidate":
            str(candidates["m3_detached_abs32_jump_table_icp"]),
        "m4_candidate": str(candidates["m4"]),
        "m4_fastdyn": execution["status"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
