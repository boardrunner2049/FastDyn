#!/usr/bin/env python3
"""Collect reproducible Cortex-M image/backend qualification evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from fastdyn.preflight.firmware import qualify_firmware


INSN = re.compile(r"^\s*([0-9a-f]+):\s+((?:[0-9a-f]{4}\s+){1,2})([a-z][a-z0-9.]*)\s*(.*)$", re.I)


def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True)


def parse_case(value: str) -> tuple[str, str, Path, int, int]:
    fields = value.split(",", 4)
    if len(fields) != 5:
        raise argparse.ArgumentTypeError("case must be label,cpu,path,vector_base,rom_budget")
    return fields[0], fields[1], Path(fields[2]), int(fields[3], 0), int(fields[4], 0)


def virtual_bytes(elf: ELFFile, stream, address: int, size: int) -> bytes | None:
    for segment in elf.iter_segments():
        if segment["p_type"] != "PT_LOAD":
            continue
        start = int(segment["p_vaddr"])
        if start <= address and address + size <= start + int(segment["p_filesz"]):
            stream.seek(int(segment["p_offset"]) + address - start)
            return stream.read(size)
    return None


def inspect(label: str, cpu: str, path: Path, vector_base: int, budget: int, bolt: Path) -> dict[str, object]:
    path = path.resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        sections = [
            {"name": section.name, "address": int(section["sh_addr"]), "size": int(section["sh_size"]), "type": str(section["sh_type"])}
            for section in elf.iter_sections()
            if int(section["sh_size"])
        ]
        relocations = [item for item in sections if item["type"] in {"SHT_REL", "SHT_RELA"}]
        function_ranges = sorted(
            {
                (int(symbol["st_value"]) & ~1, (int(symbol["st_value"]) & ~1) + int(symbol["st_size"]))
                for section in elf.iter_sections()
                if isinstance(section, SymbolTableSection)
                for symbol in section.iter_symbols()
                if symbol["st_info"]["type"] == "STT_FUNC" and int(symbol["st_size"]) > 0
            }
        )
        vector = virtual_bytes(elf, stream, vector_base, 8)
        header = {"machine": str(elf.header["e_machine"]), "entry": int(elf.header["e_entry"]), "little_endian": elf.little_endian}
    qualification = qualify_firmware(
        path,
        SimpleNamespace(arch="arm", cpu=cpu, init_nsvtor=vector_base),
    ).to_dict()
    disassembly = run(["arm-none-eabi-objdump", "-d", str(path)])
    counts = {"total": 0, "narrow_16bit": 0, "wide_32bit": 0, "pc_relative_loads": 0, "branches": 0, "exception_return_mnemonics": 0}
    mnemonics: dict[str, int] = {}
    for line in disassembly.stdout.splitlines():
        match = INSN.match(line)
        if not match:
            continue
        address = int(match.group(1), 16)
        if not any(start <= address < end for start, end in function_ranges):
            continue
        encoded = match.group(2).split()
        mnemonic, operands = match.group(3).lower(), match.group(4).lower()
        counts["total"] += 1
        counts["narrow_16bit" if len(encoded) == 1 else "wide_32bit"] += 1
        counts["pc_relative_loads"] += int(mnemonic.startswith("ldr") and "pc" in operands)
        counts["branches"] += int(mnemonic.startswith("b") or mnemonic in {"cbz", "cbnz"})
        counts["exception_return_mnemonics"] += int(mnemonic in {"eret", "rfe", "rfeda", "rfeia"})
        mnemonics[mnemonic] = mnemonics.get(mnemonic, 0) + 1
    size_result = run(["arm-none-eabi-size", str(path)])
    size_line = size_result.stdout.splitlines()[-1].split()
    text_bytes, data_bytes, bss_bytes = map(int, size_line[:3])
    bolt_result = run([str(bolt.resolve()), str(path), "-o", "/tmp/fastdyn-cortexm-bolt-output"])
    stack_pointer = int.from_bytes(vector[:4], "little") if vector else None
    reset_vector = int.from_bytes(vector[4:8], "little") if vector else None
    return {
        "label": label,
        "path": str(path),
        "sha256": digest,
        "target": {"cpu": cpu, "profile": "armv6-m" if cpu in {"cortex-m0", "cortex-m0plus", "cortex-m0+"} else "armv7-m", "mode": "thumb"},
        "header": header,
        "vector": {"base": vector_base, "initial_sp": stack_pointer, "reset_raw": reset_vector, "reset_pc": reset_vector & ~1 if reset_vector is not None else None, "thumb_bit": bool(reset_vector & 1) if reset_vector is not None else None},
        "qualification": qualification,
        "sections": sections,
        "relocations": relocations,
        "instructions": {**counts, "function_symbol_ranges": len(function_ranges), "distinct_mnemonics": len(mnemonics), "top_mnemonics": sorted(mnemonics.items(), key=lambda item: (-item[1], item[0]))[:20]},
        "memory": {"text_bytes": text_bytes, "initialized_data_bytes": data_bytes, "bss_bytes": bss_bytes, "rom_accounting_bytes": text_bytes + data_bytes, "rom_budget_bytes": budget, "rom_headroom_bytes": budget - text_bytes - data_bytes, "fits": text_bytes + data_bytes <= budget},
        "bolt_attempt": {"command": [str(bolt.resolve()), str(path)], "returncode": bolt_result.returncode, "stdout": bolt_result.stdout.splitlines(), "stderr": bolt_result.stderr.splitlines()},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bolt", type=Path, required=True)
    parser.add_argument("--case", action="append", type=parse_case, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = {
        "schema_version": 1,
        "classification": "cortex_m_target_qualification",
        "images": [inspect(*case, args.bolt) for case in args.case],
        "claims": {"identity_qualified": True, "bolt_thumb_backend": False, "rewritten_candidate": False, "target_timing": False},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "images": [{"label": image["label"], "fits": image["memory"]["fits"], "rom_headroom_bytes": image["memory"]["rom_headroom_bytes"], "instructions": image["instructions"]["total"], "bolt_returncode": image["bolt_attempt"]["returncode"]} for image in report["images"]]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
