#!/usr/bin/env python3
"""Check the guest allocation used by the MicroPython MPS2-AN385 board variant."""
import argparse
import json
import struct
from pathlib import Path

from elftools.elf.elffile import ELFFile


NEW_CODE = 0x80000
RESERVED_END = 0x200000
STACK_TOP = 0x400000


def inspect(path):
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        if elf["e_machine"] != "EM_ARM" or not elf.little_endian:
            raise ValueError(f"expected a little-endian ARM board image: {path}")
        vector_load = next((seg for seg in elf.iter_segments() if seg["p_type"] == "PT_LOAD"
                            and seg["p_paddr"] == 0 and seg["p_filesz"] >= 64), None)
        if vector_load is None:
            raise ValueError("missing the MPS2 reset and exception vectors at address zero")
        stream.seek(vector_load["p_offset"])
        vectors = struct.unpack("<16I", stream.read(64))
        if vectors[0] != STACK_TOP or not vectors[1]:
            raise ValueError("the vector table has an invalid initial stack or reset target")
        loads = [{"physical": seg["p_paddr"], "virtual": seg["p_vaddr"],
                  "file_size": seg["p_filesz"], "memory_size": seg["p_memsz"]}
                 for seg in elf.iter_segments() if seg["p_type"] == "PT_LOAD"]
        code = [{"section": s.name, "start": s["sh_addr"], "end": s["sh_addr"] + s["sh_size"]}
                for s in elf.iter_sections() if s["sh_flags"] & 0x6 == 0x6 and s["sh_size"]]
    for address in vectors[1:]:
        if address and (not address & 1 or not any(r["start"] <= (address & ~1) < r["end"] for r in code)):
            raise ValueError(f"invalid Thumb exception target {address:#x}")
    return loads, code, vectors


def check(original, candidate=None, profile=None):
    loads, _, vectors = inspect(original)
    if not loads or any(s["physical"] + s["file_size"] > NEW_CODE
                        or s["virtual"] + s["memory_size"] > NEW_CODE for s in loads):
        raise ValueError("the proposed code allocation overlaps the original image")
    result = {"input_loads": loads, "trampoline_and_code_region": [NEW_CODE, RESERVED_END],
              "stack_top": STACK_TOP, "input_vectors": vectors}
    if candidate:
        candidate_loads, code, new_vectors = inspect(candidate)
        if tuple(bool(v) for v in new_vectors) != tuple(bool(v) for v in vectors):
            raise ValueError("the set of installed exception vectors changed")
        if not candidate_loads or any(s["physical"] + s["file_size"] > RESERVED_END
                                     or s["virtual"] + s["memory_size"] > RESERVED_END
                                     for s in candidate_loads):
            raise ValueError("the optimized image exceeds the allowed board allocation")
        moved = [s for s in code if NEW_CODE <= s["start"] < s["end"] <= RESERVED_END]
        if not moved:
            raise ValueError("the optimized image has no executable section in the new allocation")
        result.update(candidate_loads=candidate_loads, relocated_sections=moved, candidate_vectors=new_vectors)
        if profile:
            site_counts = {}
            with profile.open() as prof:
                for line in prof:
                    row = line.split()
                    if row and row[0] == "TB":
                        pc, count = int(row[1], 16), int(row[3])
                        if any(region["start"] <= pc < region["end"] for region in moved):
                            site_counts[pc] = site_counts.get(pc, 0) + count
            if not sum(site_counts.values()):
                raise ValueError("training application executed no TB in relocated code")
            result.update(relocated_unique_tb_starts=len(site_counts),
                          relocated_tb_executions=sum(site_counts.values()))
    elif profile:
        raise ValueError("a relocated execution profile requires a candidate image")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.original, args.candidate, args.profile), indent=2))
