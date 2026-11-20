"""Firmware-side automatic MMIO discovery and pending-plan generation."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import struct
import time
from typing import Iterable, Sequence

from elftools.elf.elffile import ELFFile

from .binary.mmiofinder import find_mmio_accesses
from .host import KnownMemoryAccess


MMIOAccess = KnownMemoryAccess


def _elf_regions(path: Path):
    regions = []
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        entry = int(elf.header["e_entry"]) & ~1
        for section in elf.iter_sections():
            flags = int(section["sh_flags"])
            if flags & 0x4 and section.data_size:
                regions.append((section.data(), int(section["sh_addr"]), [entry]))
    return regions


def _is_elf(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(4) == b"\x7fELF"


def _raw_base(blob: bytes, configured_base: int | None) -> int:
    if configured_base is not None:
        return int(configured_base, 0) if isinstance(configured_base, str) else int(configured_base)
    if len(blob) >= 8:
        reset = struct.unpack_from("<I", blob, 4)[0] & ~1
        # A conventional Cortex-M flash image. Align to a generous image span.
        if reset >= len(blob):
            return reset & ~0xFFFFF
    return 0x08000000


def discover_firmware(path: str | Path, mmio_regions: Sequence[tuple[int, int]],
                      *, configured_base: int | None = None,
                      analysis=None) -> list[MMIOAccess]:
    firmware = Path(path).expanduser().resolve()
    hits: list[MMIOAccess] = []
    if analysis is not None and _is_elf(firmware):
        # Constant propagation is a shared host-analysis fact. MMIO range
        # selection remains this consumer's policy.
        hits.extend(
            access for access in analysis.constant_propagation.memory_accesses()
            if any(start <= access.address <= end for start, end in mmio_regions)
        )
        scan_regions = []
    elif _is_elf(firmware):
        scan_regions = _elf_regions(firmware)
    else:
        blob = firmware.read_bytes()
        scan_regions = [(blob, _raw_base(blob, configured_base), None)]
    for blob, base, entries in scan_regions:
        hits.extend(find_mmio_accesses(
            blob=blob, base_va=base, arch="arm32", mode="thumb",
            mmio_regions=list(mmio_regions), entry_points=entries,
        ))
    unique = {(hit.pc, hit.address, hit.direction, hit.transfer_size): hit for hit in hits}
    return sorted(unique.values(), key=lambda hit: (hit.pc, hit.address, hit.direction))


def write_plan(path: str | Path, report_path: str | Path, firmware: str | Path,
               mmio_regions: Sequence[tuple[int, int]], *,
               configured_base: int | None = None, analysis=None) -> list[MMIOAccess]:
    started = time.perf_counter()
    hits = discover_firmware(firmware, mmio_regions, configured_base=configured_base,
                             analysis=analysis)
    plan = Path(path)
    plan.parent.mkdir(parents=True, exist_ok=True)
    with plan.open("w", encoding="utf-8") as stream:
        stream.write("# pc address transfer_size instruction_size direction\n")
        for hit in hits:
            stream.write(f"{hit.pc:x} {hit.address:x} {hit.transfer_size} "
                         f"{hit.instruction_size} {hit.direction}\n")
    report = {
        "firmware": str(Path(firmware).expanduser().resolve()),
        "preparation_seconds": time.perf_counter() - started,
        "mmio_regions": [[start, end] for start, end in mmio_regions],
        "access_count": len(hits),
        "accesses": [asdict(hit) for hit in hits],
    }
    report_file = Path(report_path)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return hits
