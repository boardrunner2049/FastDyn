#!/usr/bin/env python3
"""Materialize a linked ESCAPE1 into its physical flash bank and prove relocated code ran."""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile

FLASH = 0x08000000
APP = FLASH + 0x1000
TEXT = FLASH + 0x1800
FREE = FLASH + 0x10000
LIMIT = FLASH + 0x20000
SRAM = 0x20000000


def inspect(path):
    with path.open("rb") as f:
        e = ELFFile(f)
        if e["e_machine"] != "EM_ARM" or not e.little_endian:
            raise ValueError("expected a little-endian ARM image")
        loads = []
        for seg in e.iter_segments():
            if seg["p_type"] != "PT_LOAD" or seg["p_filesz"] == 0:
                continue
            p, v, mem = (seg[k] for k in ("p_paddr", "p_vaddr", "p_memsz"))
            data = seg.data()
            if not (APP <= p and p + len(data) <= LIMIT):
                raise ValueError(f"load outside application flash {p:#x}+{len(data):#x}")
            if not ((APP <= v and v + mem <= LIMIT) or (SRAM <= v and v + mem <= SRAM + 36*1024)):
                raise ValueError("runtime mapping falls outside the device")
            if any(p < a + len(b) and a < p + len(data) for a, _, b in loads):
                raise ValueError("overlapping physical loads")
            loads.append((p, v, data))
        syms = e.get_section_by_name(".symtab")
        if syms is None:
            raise ValueError("function symbols are required for guest execution proof")
        funcs = [(s.name, s["st_value"] & ~1, s["st_size"]) for s in syms.iter_symbols()
                 if s["st_info"]["type"] == "STT_FUNC" and s["st_size"]]
    return loads, funcs


def package(original, candidate, bank, profile=None):
    original_loads, _ = inspect(original)
    new_loads, symbols = inspect(candidate)
    if len(bank) != LIMIT - FLASH:
        raise ValueError("expected full 128 KiB flash bank")
    for p, _, data in original_loads:
        off = p - FLASH
        if p + len(data) > FREE or bank[off:off + len(data)] != data:
            raise ValueError("metadata ELF and original flash differ or collide with new allocation")
    output = bytearray(bank)
    for p, _, data in new_loads:
        output[p-FLASH:p-FLASH+len(data)] = data
    if output[:TEXT-FLASH] != bank[:TEXT-FLASH]:
        raise ValueError("bootloader or persisted application settings changed")
    vectors = struct.unpack_from("<48I", output, TEXT-FLASH)
    if vectors[0] != struct.unpack_from("<I", bank, TEXT-FLASH)[0]:
        raise ValueError("initial stack changed")
    for vector in vectors[1:]:
        if vector and not (vector & 1 and any(a <= (vector & ~1) < a + n for _, a, n in symbols)):
            raise ValueError(f"invalid Thumb vector {vector:#x}")
    moved = [(name, a, sz) for name, a, sz in symbols if FREE <= a and a + sz <= LIMIT]
    if not moved:
        raise ValueError("no relocated function in reserved flash window")
    result = {"reserved_flash": [FREE, LIMIT], "new_functions": moved,
              "deployed_bank_sha256": hashlib.sha256(output).hexdigest(),
              "input_elf_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
              "candidate_elf_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
              "loaded_bytes": sum(len(data) for _, _, data in new_loads),
              "original_loaded_bytes": sum(len(data) for _, _, data in original_loads)}
    if profile:
        counts = {}
        for line in profile.read_text().splitlines():
            fields = line.split()
            if fields[0] == "TB":
                addr, count = int(fields[1], 16), int(fields[3])
                if any(start <= addr < start + sz for _, start, sz in moved):
                    counts[hex(addr)] = counts.get(hex(addr), 0) + count
        if not sum(counts.values()):
            raise ValueError("no dynamically executed relocated function")
        result["relocated_tb_entries"] = sum(counts.values())
        result["relocated_pcs"] = counts
        result["tb_profile_sha256"] = hashlib.sha256(profile.read_bytes()).hexdigest()
    return result, output


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("original_elf", "candidate_elf", "original_bank", "output_bank", "receipt"):
        p.add_argument(key, type=Path)
    p.add_argument("--profile", type=Path)
    a = p.parse_args()
    result, output = package(a.original_elf, a.candidate_elf, a.original_bank.read_bytes(), a.profile)
    a.output_bank.write_bytes(output)
    a.receipt.write_text(json.dumps(result, indent=2) + "\n")
