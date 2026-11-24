"""Nominate persistent object storage in a caller-designated, once-entered Thumb routine."""

import argparse
from collections import defaultdict
import json
from pathlib import Path

from capstone import arm as A
from elftools.elf.elffile import ELFFile

from ..decode.guest_symbols import load_guest_image
from .rtos_cfg import direct_target
from .rtos_instances import Program, discover_instances, value_states
from .rtos_signatures import recover
from .task_loops import _components


def word_symbols(path):
    """Return unique names separately per address; aliases cannot select a handle."""
    names = defaultdict(set)
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        table = elf.get_section_by_name(".symtab")
        if table is not None:
            for sym in table.iter_symbols():
                index = sym["st_shndx"]
                if (sym["st_info"].type == "STT_OBJECT" and sym["st_size"] == 4
                        and isinstance(index, int) and elf.get_section(index)["sh_flags"] & 1):
                    names[sym["st_value"]].add(sym.name)
    return names


def public_names(api, r0, r2):
    """A compiled entry can implement multiple source macros."""
    if api == "xQueueGenericCreate":
        return {0: ["xQueueCreate"], 3: ["xSemaphoreCreateBinary"]}.get(r2, [])
    if api == "xQueueCreateMutex":
        return {1: ["xSemaphoreCreateMutex"], 4: ["xSemaphoreCreateRecursiveMutex"]}.get(r0, [])
    if api == "xQueueCreateCountingSemaphore":
        return ["xSemaphoreCreateCounting"]
    if api == "xStreamBufferGenericCreate":
        base = {0: "xStreamBufferCreate", 1: "xMessageBufferCreate",
                2: "xStreamBatchingBufferCreate"}.get(r2)
        return [base, base + "WithCallback"] if base else []
    return [api] if api in ("xTimerCreate", "xEventGroupCreate", "xQueueCreate") else []


def normal_return(insn):
    return ((insn.id == A.ARM_INS_BX and insn.operands[0].reg == A.ARM_REG_LR)
            or (insn.id == A.ARM_INS_POP and any(op.reg == A.ARM_REG_PC for op in insn.operands)))


def storage_sites(image, root, symbols):
    program = Program(image)
    if len(program.names[root]) != 1:
        raise ValueError(f"missing or ambiguous once-entered function {root}")
    entry = next(iter(program.names[root]))
    func = program.funcs[entry]
    insns = program.decode(func)
    edges, predecessors, _ = program.cfg(func, insns)
    if entry not in edges:
        raise ValueError(f"missing decoded body for {root}")
    repeatable = set()
    for component in _components(edges, predecessors):
        if len(component) > 1 or any(pc in edges[pc] for pc in component):
            repeatable.update(component)
    # If an unresolved transfer could return to this routine at an arbitrary
    # instruction, absence of a visible backedge cannot establish multiplicity.
    incomplete = []
    for pc, outgoing in edges.items():
        insn = insns[pc]
        if insn.id == A.ARM_INS_BL:
            target = direct_target(insn)
            if pc not in program.calls[entry] or (target is not None and func.start <= target < func.end):
                incomplete.append(pc)
        elif insn.id == A.ARM_INS_BLX or (any(dst not in edges for dst in outgoing)
                                           and not normal_return(insn)):
            incomplete.append(pc)
    incomplete.sort()
    states = value_states(program, func, insns, edges)
    stores = defaultdict(set)
    for store in discover_instances(program).handle_stores:
        stores[int(store["creation_pc"], 16)].add(int(store["storage"], 16))
    rows = []
    for pc in sorted(program.objects & edges.keys()):
        api = program.api[pc]
        regs = [states[pc].get(r) for r in (A.ARM_REG_R0, A.ARM_REG_R1, A.ARM_REG_R2)]
        r0, r1, r2 = (x if type(x) is int else None for x in regs)
        names = public_names(api, r0, r2)
        reasons = []
        if "Static" in api:
            reasons.append("already_static")
        elif not names:
            reasons.append("unidentified_source_constructor")
        if pc in repeatable:
            reasons.append("creation_inside_cycle")
        if incomplete:
            reasons.append("incomplete_control_flow")
        if insns[pc].id != A.ARM_INS_BL:
            reasons.append("unsupported_transfer")
        if "xQueueCreate" in names and (not r0 or r1 is None):
            reasons.append("unknown_queue_dimensions")
        if api == "xStreamBufferGenericCreate" and not r0:
            reasons.append("unknown_buffer_capacity")
        dests = sorted(stores[pc])
        handles = sorted(symbols.get(dests[0], ())) if len(dests) == 1 else []
        if len(handles) != 1:
            reasons.append("ambiguous_or_unknown_handle_storage")
        rows.append(dict(pc=hex(pc), kernel_entry=api, candidate_constructors=names,
                         r0=r0, r1=r1, r2=r2, handle_addresses=list(map(hex, dests)),
                         handle=handles[0] if len(handles) == 1 else None, reasons=reasons))
    seen_handles = defaultdict(list)
    for row in rows:
        if row["handle"] is not None:
            seen_handles[row["handle"]].append(row)
    for same_handle in seen_handles.values():
        if len(same_handle) > 1:
            for row in same_handle:
                row["reasons"].append("multiple_creation_sites_for_handle")
    return dict(once_function=root, unclassified_control_flow=list(map(hex, incomplete)), sites=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    parser.add_argument("--reference", type=Path, help="matching load-image companion for a stripped ELF")
    parser.add_argument("--once-function", required=True, help="application-declared single entry per boot")
    args = parser.parse_args()
    image = load_guest_image(args.elf)
    labels = args.elf
    if args.reference:
        image, matches = recover(image, load_guest_image(args.reference))
        if matches["method"] != "load_image_companion":
            parser.error("object storage addresses require a matching load-image companion")
        labels = args.reference
    try:
        report = storage_sites(image, args.once_function, word_symbols(labels))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
