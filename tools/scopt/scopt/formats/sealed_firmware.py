"""Verification for exhaustive, fail-closed Cortex-M SuperCache captures."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import io
from pathlib import Path
from typing import Any

from elftools.elf.elffile import ELFFile

from .sc_v5 import read_sc

TRACE_SCHEMA = "scopt-sealed-coverage-v1"
MANIFEST_SCHEMA = "scopt.sealed_firmware_coverage/1"


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def executable_ranges(elf_path: Path) -> tuple[tuple[int, int], ...]:
    elf = ELFFile(io.BytesIO(elf_path.read_bytes()))
    if elf["e_machine"] != "EM_ARM":
        raise ValueError("sealed firmware producer currently requires an ARM ELF")
    ranges = []
    for segment in elf.iter_segments():
        if segment["p_type"] != "PT_LOAD" or not segment["p_flags"] & 1:
            continue
        start = int(segment["p_vaddr"])
        end = start + int(segment["p_filesz"])
        if start >= end or (start | end) & 1:
            raise ValueError("executable PT_LOAD ranges must be nonempty and halfword aligned")
        ranges.append((start, end))
    if not ranges:
        raise ValueError("ELF has no executable PT_LOAD range")
    ranges.sort()
    if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
        raise ValueError("executable PT_LOAD ranges overlap")
    return tuple(ranges)


def range_argument(ranges: tuple[tuple[int, int], ...]) -> str:
    return ",".join(f"{start:x}-{end:x}" for start, end in ranges)


def parse_trace(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if len(lines) < 3 or lines[-1] != "end":
        raise ValueError("sealed coverage trace is truncated")
    head = lines[0].split()
    if len(head) != 4 or head[0] != TRACE_SCHEMA:
        raise ValueError("unknown sealed coverage trace schema")
    context = tuple(int(value, 16) for value in head[1:])
    ranges: list[tuple[int, int]] = []
    rows: list[tuple[int, ...]] = []
    for line in lines[1:-1]:
        fields = line.split()
        if len(fields) == 3 and fields[0] == "range":
            ranges.append((int(fields[1], 16), int(fields[2], 16)))
        elif len(fields) == 9 and fields[0] == "tb":
            rows.append(tuple(int(value, 16) for value in fields[1:7]) +
                        tuple(int(value) for value in fields[7:]))
        else:
            raise ValueError(f"invalid sealed coverage trace row: {line!r}")
    positions = [row[0] for row in rows]
    expected = [pc for start, end in ranges for pc in range(start, end, 2)]
    if positions != expected:
        raise ValueError("trace has a missing, duplicate, or reordered Thumb entry")
    flags, cs_base, cflags = context
    if any((row[3], row[4], row[5]) != context for row in rows):
        raise ValueError("trace changed translation context during exhaustive sweep")
    if any(row[6] <= 0 or row[7] != 1 for row in rows):
        raise ValueError("exhaustive fallback records must contain exactly one guest instruction")
    return {"context": {"flags": flags, "cs_base": cs_base, "cflags": cflags},
            "ranges": tuple(ranges), "rows": tuple(rows)}


def verify(elf_path: Path, trace_path: Path, cache_path: Path,
           qemu_path: Path) -> dict[str, Any]:
    declared = executable_ranges(elf_path)
    trace = parse_trace(trace_path.read_text())
    if trace["ranges"] != declared:
        raise ValueError("producer ranges differ from executable ELF PT_LOAD ranges")
    cache = read_sc(cache_path, versions=(7,), strict=True)
    records: dict[tuple[int, ...], list[Any]] = defaultdict(list)
    for record in cache.records:
        key = (record.pc, record.page_addr0, record.page_addr1, record.flags,
               record.cs_base, record.cflags, record.guest_size, record.icount)
        records[key].append(record)
    missing = [row[0] for row in trace["rows"] if row not in records]
    if missing:
        raise ValueError(f"cache lacks {len(missing)} traced entries; first={missing[0]:#x}")
    expected = len(trace["rows"])
    hot = sum(record.icount > 1 for record in cache.records)
    return {
        "schema": MANIFEST_SCHEMA,
        "contract": {
            "lookup": "exact-pc-direct-index; fail-closed on missing context",
            "jit_fallback": False,
            "guest_hash_validation": False,
            "fault_recovery": False,
            "interrupt_poll_at_tb_entry": False,
            "user_verified_immutable_firmware": True,
        },
        "ranges": [{"start": start, "end": end,
                    "halfword_entries": (end - start) // 2}
                   for start, end in declared],
        "context": trace["context"],
        "coverage": {"expected_halfword_entries": expected,
                     "verified_fallback_entries": expected,
                     "cache_records": len(cache.records),
                     "multi_instruction_hot_records": hot,
                     "complete": True},
        "identities": {"firmware": sha256_file(elf_path),
                       "qemu": sha256_file(qemu_path),
                       "cache": sha256_file(cache_path),
                       "trace": sha256_file(trace_path)},
    }
