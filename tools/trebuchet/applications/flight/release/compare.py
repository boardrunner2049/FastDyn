"""Compare bytes the loader maps and run Courbet's unmodified profile validator."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from elftools.elf.elffile import ELFFile


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inspect(path):
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [{"paddr": seg["p_paddr"], "vaddr": seg["p_vaddr"], "filesz": seg["p_filesz"],
                     "memsz": seg["p_memsz"], "align": seg["p_align"],
                     "flags": seg["p_flags"], "bytes_sha256": sha(seg.data())}
                    for seg in elf.iter_segments() if seg["p_type"] == "PT_LOAD"]
        relocs = {section.name: section.num_relocations() for section in elf.iter_sections()
                  if section["sh_type"] in ("SHT_REL", "SHT_RELA")
                  and elf.get_section(section["sh_info"])["sh_flags"] & 4}
        symtab = elf.get_section_by_name(".symtab")
        names = sorted((s.name, s["st_value"], s["st_size"], s["st_info"]["type"])
                       for s in symtab.iter_symbols() if s.name and s["st_info"]["type"] in ("STT_FUNC", "STT_OBJECT"))
        stream.seek(0)
        return {"sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                "entry": elf["e_entry"], "e_flags": elf["e_flags"], "load_segments": segments,
                "named_symbols_sha256": sha(json.dumps(names).encode()), "code_relocations": relocs}


def named_symbols(path):
    with path.open("rb") as stream:
        table = ELFFile(stream).get_section_by_name(".symtab")
        return Counter((sym.name, sym["st_value"], sym["st_size"], sym["st_info"]["type"], sym["st_info"]["bind"])
                       for sym in table.iter_symbols() if sym.name and sym["st_info"]["type"] in ("STT_FUNC", "STT_OBJECT"))


def symbol_delta(normal, relocs):
    original, retained = named_symbols(normal), named_symbols(relocs)
    added = retained - original
    extents = {(addr, size, kind) for _, addr, size, kind, _ in original}
    aliases = sum(count for (_, addr, size, kind, binding), count in added.items()
                  if binding == "STB_LOCAL" and (addr, size, kind) in extents)
    return {"removed_or_changed": (original - retained).total(), "added": added.total(),
            "local_aliases_of_existing_extents": aliases,
            "preserved_with_only_local_aliases": original <= retained and aliases == added.total()}


def same_load(a, b):
    return all(a[k] == b[k] for k in ("entry", "e_flags", "load_segments"))


def main():
    root, family, output = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    case = json.loads((root / "tools/trebuchet/applications/flight/manifest.json").read_text())["families"][family]
    files = {"reference": root / case["elf"], "normal": output / "normal.elf",
             "linked": output / "linked.elf", "relocs": output / "relocs.elf"}
    meta = {k: inspect(p) for k, p in files.items()}
    checks = {"linked_and_relocs_load_identical": same_load(meta["linked"], meta["relocs"]),
              "final_and_relocs_load_identical": same_load(meta["normal"], meta["relocs"]),
              "final_and_relocs_symbols_identical":
                  meta["normal"]["named_symbols_sha256"] == meta["relocs"]["named_symbols_sha256"],
              "code_relocation_count": sum(meta["relocs"]["code_relocations"].values()),
              "shipped_and_rebuilt_load_identical": same_load(meta["reference"], meta["normal"])}
    checks["symbol_delta"] = symbol_delta(files["normal"], files["relocs"])
    hooks = {}
    generator = root / "virtuals/physics/flight_controllers/courbet/prepare_execution_configuration.py"
    for label in ("reference", "normal", "relocs"):
        dest = output / ("hooks-" + label)
        run = subprocess.run([sys.executable, str(generator), "--profile", str(root / case["profile"]),
                              "--elf", str(files[label]), "--output-dir", str(dest)],
                             capture_output=True, text=True)
        hooks[label] = {"returncode": run.returncode, "stdout": run.stdout, "stderr": run.stderr,
                        "file_sha256": {p.name: sha(p.read_bytes()) for p in sorted(dest.glob("*.txt"))}}
    hooks_ok = all(hooks[label]["returncode"] == 0 for label in ("normal", "relocs"))
    if hooks_ok:
        normal_hooks, reloc_hooks = output / "hooks-normal", output / "hooks-relocs"
        hooks_ok = (all((normal_hooks / name).read_bytes() == (reloc_hooks / name).read_bytes()
                        for name in ("modifiers.txt", "virtuals.txt"))
                    and set((normal_hooks / "symbols.txt").read_text().splitlines())
                    <= set((reloc_hooks / "symbols.txt").read_text().splitlines()))
    checks["rebuilt_hook_bindings_preserved"] = hooks_ok
    report = {"family": family, "source_revision": case["revision"], "images": meta, "checks": checks,
              "hooks": hooks, "operational_mission_reproduced_on_rebuild": False}
    (output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(checks))
    return int(not (checks["linked_and_relocs_load_identical"] and checks["final_and_relocs_load_identical"]
                    and checks["symbol_delta"]["preserved_with_only_local_aliases"] and checks["code_relocation_count"]
                    and hooks["reference"]["returncode"] == 0 and hooks_ok))


if __name__ == "__main__":
    sys.exit(main())
