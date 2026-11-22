"""Validate the board's flash, exception table, RAM and observed relocated code."""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile


def image(path):
    with path.open("rb") as file:
        elf = ELFFile(file)
        assert elf["e_machine"] == "EM_ARM" and elf.little_endian
        sections = {s.name: (s["sh_addr"], s["sh_size"], s.data() if s["sh_type"] != "SHT_NOBITS" else None)
                    for s in elf.iter_sections() if s["sh_flags"] & 2}
        for base, size, _ in sections.values():
            assert any(lo <= base <= base + size <= hi for lo, hi in
                       ((0x08007000, 0x08040000), (0x20000000, 0x2000c000))), hex(base)
        for seg in elf.iter_segments():
            if seg["p_type"] == "PT_LOAD":
                assert 0x08000000 <= seg["p_paddr"] <= seg["p_paddr"] + seg["p_filesz"] <= 0x08040000
        base, _, vector = sections[".isr_vector"]
        words = struct.unpack(f"<{len(vector) // 4}I", vector)
        assert base == 0x08007000 and words[0] == 0x2000c000 and words[1] == elf["e_entry"]
        # ST startup stores its RAM boot trampoline at offset 0x1e0.
        assert len(words) == 121 and words[120] == 0xf1e0f85f
        assert all(pc == 0 or (pc & 1 and 0x08007000 <= pc < 0x08040000) for pc in words[1:120])
    return sections, words


def check(original, candidate, layout, profile):
    before, vectors = image(original)
    after, current = image(candidate)
    assert vectors == current
    for name in (".rodata", ".data", ".init_array", ".fini_array", ".bss", "._user_heap_stack"):
        assert before[name] == after[name], name
    raw = json.loads(layout.read_text())
    (function,) = raw["functions"]
    origin, moved, size = function["original_address"], function["output_address"], function["output_size"]
    assert function["name"].startswith("__mulsf3") and moved == 0x08038000
    assert after[".text"][:2] == (moved, size)
    old_text, new_text = before[".text"], after[".bolt.org.text"]
    assert old_text[:2] == new_text[:2]
    modified = [address + old_text[0] for address, (old, new) in enumerate(zip(old_text[2], new_text[2], strict=True)) if old != new]
    assert modified and all(origin <= address < origin + function["original_size"] for address in modified)
    sites = {}
    for line in profile.read_text().splitlines():
        fields = line.split()
        if fields and fields[0] == "TB":
            pc, executions = int(fields[1], 16), int(fields[3])
            if moved <= pc < moved + size and executions:
                sites[pc] = sites.get(pc, 0) + executions
    assert sites and moved in sites
    return {"original_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
            "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "identical_vector_words": len(vectors), "installed_nonzero_vectors": sum(bool(v) for v in vectors[1:120]),
            "initial_sp": hex(vectors[0]), "reset_pc": hex(vectors[1]), "moved_start": hex(moved),
            "original_start": hex(origin), "moved_bytes": size, "original_text_changed_bytes": len(modified),
            "distinct_relocated_tb_starts": len(sites), "relocated_tb_executions": sum(sites.values()),
            "relocated_entry_executions": sites[moved]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("original", "candidate", "layout", "profile"):
        parser.add_argument(arg, type=Path)
    a = parser.parse_args()
    print(json.dumps(check(a.original, a.candidate, a.layout, a.profile), indent=2))
