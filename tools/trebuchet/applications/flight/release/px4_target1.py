"""Verify the linked PX4 constructor table before selecting TARGET1=abs."""
import hashlib
import json
from pathlib import Path
import sys
from elftools.elf.elffile import ELFFile


def verify(path):
    rows = []
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        for relsec in elf.iter_sections():
            if relsec["sh_type"] not in ("SHT_REL", "SHT_RELA"):
                continue
            target, symbols = elf.get_section(relsec["sh_info"]), elf.get_section(relsec["sh_link"])
            for rel in relsec.iter_relocations():
                if rel["r_info_type"] != 38:  # R_ARM_TARGET1
                    continue
                symbol = symbols.get_symbol(rel["r_info_sym"])
                address = rel["r_offset"]
                off = address - target["sh_addr"]
                if not 0 <= off <= target["sh_size"] - 4:
                    raise ValueError("TARGET1 outside constructor section")
                word = int.from_bytes(target.data()[off:off + 4], "little")
                if (target.name != ".init_section" or symbol["st_info"]["type"] != "STT_FUNC"
                        or not word & 1 or word != symbol["st_value"]):
                    raise ValueError(f"TARGET1 lacks an absolute Thumb constructor match at {address:#x}")
                rows.append({"address": address, "pointer": word, "target": symbol.name})
        section = elf.get_section_by_name(".init_section")
        expected = list(range(section["sh_addr"], section["sh_addr"] + section["sh_size"], 4))
        if not rows or sorted(r["address"] for r in rows) != expected:
            raise ValueError("incomplete constructor pointer table")
        stream.seek(0)
        return {"elf_sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                "absolute_thumb_constructors": len(rows), "rows": rows}


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1])), indent=2))
