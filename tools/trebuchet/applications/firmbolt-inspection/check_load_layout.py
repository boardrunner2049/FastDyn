"""Verify a FirmBOLT firmware layout-only change against the prior emission."""
import argparse
import hashlib
import json
from pathlib import Path

from elftools.elf.elffile import ELFFile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def inventory(elf):
    allocated = {}
    relocations = {}
    for section in elf.iter_sections():
        if section["sh_flags"] & 2:
            require(section.name not in allocated, "ambiguous section name")
            allocated[section.name] = [
                section[key] for key in ("sh_addr", "sh_type", "sh_size", "sh_flags")
            ] + [digest(section.data())]
        if section["sh_type"] in ("SHT_REL", "SHT_RELA"):
            require(section.name not in relocations, "ambiguous relocation section")
            relocations[section.name] = [
                elf.get_section(section["sh_info"]).name,
                elf.get_section(section["sh_link"]).name,
                section.num_relocations(),
                digest(section.data()),
            ]
    return allocated, relocations


def compare(old_path, new_path):
    with old_path.open("rb") as old_stream, new_path.open("rb") as new_stream:
        old, new = ELFFile(old_stream), ELFFile(new_stream)
        require(old.header.e_entry == new.header.e_entry, "entry changed")
        allocated, relocations = inventory(new)
        require((allocated, relocations) == inventory(old), "allocated sections or relocations differ")
        # The nonallocatable tool note embeds invocation paths. All other
        # payloads must agree, including symbol identities, DWARF and strings.
        require(old.num_sections() == new.num_sections(), "section count changed")
        for a, b in zip(old.iter_sections(), new.iter_sections(), strict=True):
            require(a.name == b.name, "section order changed")
            require(all(a[k] == b[k] for k in ("sh_type", "sh_flags", "sh_addr", "sh_link", "sh_info", "sh_addralign", "sh_entsize")), f"section metadata changed in {a.name}")
            if a.name == ".note.bolt_info" and not a["sh_flags"] & 2:
                continue
            require(a.data() == b.data(), f"section payload changed in {a.name}")
        text = new.get_section_by_name(".text")
        original_text = new.get_section_by_name(".bolt.org.text")
        require(text and original_text, "missing original or relocated text")
        loads = []
        trimmed = 0
        for prior, current in zip(old.iter_segments("PT_LOAD"), new.iter_segments("PT_LOAD"), strict=True):
            require(all(prior[k] == current[k] for k in ("p_paddr", "p_vaddr", "p_flags", "p_align")), "LOAD identity changed")
            require(prior.data()[:current["p_filesz"]] == current.data(), "retained LOAD bytes differ")
            if current["p_vaddr"] == text["sh_addr"]:
                require(current["p_filesz"] == current["p_memsz"] == (text["sh_size"] + 3) & ~3, "unexpected text extent")
                require(current.data() == text.data().ljust(current["p_filesz"], b"\0"), "unexpected text padding")
                require(prior["p_filesz"] == prior["p_memsz"] > current["p_memsz"], "expected a shortened LOAD")
                cut_start = current["p_vaddr"] + current["p_memsz"]
                cut_end = prior["p_vaddr"] + prior["p_memsz"]
                for name, (address, _, size, _, _) in allocated.items():
                    require(not size or address >= cut_end or address + size <= cut_start, f"dropped part of {name}")
                trimmed += 1
            else:
                require(all(prior[k] == current[k] for k in ("p_filesz", "p_memsz")), "other LOAD size changed")
            loads.append({
                "vaddr": hex(current["p_vaddr"]), "paddr": hex(current["p_paddr"]),
                "old_filesz": prior["p_filesz"], "filesz": current["p_filesz"],
                "old_memsz": prior["p_memsz"], "memsz": current["p_memsz"],
                "retained_sha256": digest(current.data()),
            })
        require(trimmed == 1, "expected exactly one shorter LOAD")
        return {
            "previous_elf_sha256": digest(old_path.read_bytes()),
            "candidate_elf_sha256": digest(new_path.read_bytes()),
            "entry": hex(new.header.e_entry),
            "allocatable_sections": allocated, "relocation_sections": relocations, "loads": loads,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("previous", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(args.previous, args.candidate), indent=2))
