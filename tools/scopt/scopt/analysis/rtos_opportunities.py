"""Attribute linked FreeRTOS functions and observed TB entries to kernel modules."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import posixpath

from elftools.elf.elffile import ELFFile

from ..decode.guest_symbols import load_guest_image

MODULES = {
    "tasks.c": "tasks",
    "queue.c": "queues_semaphores_mutexes",
    "list.c": "lists",
    "timers.c": "timers",
    "event_groups.c": "event_groups",
    "stream_buffer.c": "stream_message_buffers",
    "croutine.c": "co_routines",
}
FAMILIES = (*MODULES.values(), "heap", "port")


def module_for(comp_dir, source, kernel_dir=None):
    path = Path(posixpath.normpath(posixpath.join(comp_dir, source)))
    if kernel_dir is not None:
        if not path.is_relative_to(kernel_dir):
            return None
        rel = path.relative_to(kernel_dir)
    else:
        parts = path.parts
        roots = [
            i + 1
            for i in range(len(parts))
            if parts[i] == "FreeRTOS-Kernel"
            or (i and parts[i - 1 : i + 1] == ("FreeRTOS", "Source"))
        ]
        if len(roots) != 1:
            return None
        rel = Path(*parts[roots[0] :])
    if rel.as_posix() in MODULES:
        return MODULES[rel.as_posix()]
    if rel.parts[:2] == ("portable", "MemMang"):
        return "heap"
    if rel.parts[:1] == ("portable",):
        return "port"
    return None


def kernel_functions(path, kernel_dir):
    by_address = defaultdict(set)
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        if elf.header.e_type != "ET_EXEC" or not elf.has_dwarf_info(strict=True):
            raise ValueError(
                "kernel attribution requires a linked ELF with DWARF from the reference build"
            )
        # --emit-relocs keeps relocation entries after the final link. Debug
        # addresses in ET_EXEC already contain the resolved location.
        for cu in elf.get_dwarf_info(relocate_dwarf_sections=False).iter_CUs():
            top = cu.get_top_DIE().attributes
            if "DW_AT_name" not in top:
                continue
            directory = top.get("DW_AT_comp_dir")
            mod = module_for(
                directory.value.decode() if directory else "",
                top["DW_AT_name"].value.decode(),
                kernel_dir,
            )
            if mod is None:
                continue
            for die in cu.iter_DIEs():
                a = die.attributes
                if (
                    die.tag == "DW_TAG_subprogram"
                    and "DW_AT_low_pc" in a
                    and "DW_AT_name" in a
                ):
                    by_address[a["DW_AT_low_pc"].value & ~1].add(
                        (a["DW_AT_name"].value.decode(), mod)
                    )
    return by_address


def tb_entries(path):
    counts = Counter()
    if path:
        for line in path.read_text().splitlines():
            if line.startswith("TB "):
                _, start, _, executions, *_ = line.split()
                counts[int(start, 16)] += int(executions)
    return counts


def inventory(target, reference, kernel_dir=None, profile=None):
    owners = kernel_functions(reference, kernel_dir)
    image = load_guest_image(target)
    if target.resolve() == reference.resolve():
        matches = [
            dict(reference=f.start, target=f.start, name=f.name, method="local_symbol")
            for f in image.funcs
        ]
    else:
        from .rtos_signatures import recover

        _, evidence = recover(image, load_guest_image(reference))
        matches = evidence["matched"]
    counts = tb_entries(profile)
    rows = []
    for match in matches:
        mods = {
            mod
            for name, mod in owners.get(match["reference"], ())
            if name == match["name"]
        }
        if len(mods) == 1:
            pc = match["target"]
            rows.append(
                dict(
                    module=mods.pop(),
                    name=match["name"],
                    pc=pc,
                    method=match["method"],
                    entry_tb_executions=counts[pc] if profile else None,
                )
            )
    summary = {}
    for module in FAMILIES:
        entries = {row["pc"] for row in rows if row["module"] == module}
        summary[module] = dict(
            attributed_entries=len(entries),
            observed_entries=sum(counts[pc] > 0 for pc in entries) if profile else None,
            entry_tb_executions=sum(counts[pc] for pc in entries) if profile else None,
        )
    return dict(
        schema="scopt.rtos-module-inventory/1", summaries=summary, functions=rows
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    parser.add_argument(
        "--reference", type=Path, help="symbolized kernel/application ELF"
    )
    parser.add_argument(
        "--kernel-dir", type=Path, help="kernel source directory as recorded in DWARF"
    )
    parser.add_argument(
        "--tb-profile",
        type=Path,
        help="FirmBOLT TB diagnostic from this exact target ELF",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            inventory(
                args.elf, args.reference or args.elf, args.kernel_dir, args.tb_profile
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
