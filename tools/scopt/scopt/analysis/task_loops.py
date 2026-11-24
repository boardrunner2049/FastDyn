"""Discover closed instruction cycles in registered Thumb FreeRTOS task functions.

Direct calls have their normal-return successor. A selected cycle describes the
intraprocedural CFG; scheduling, blocking, faults and callee termination can all
prevent a subsequent iteration. This analysis selects sites and changes no edges.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import capstone
from capstone import arm as A

from ..decode.guest_symbols import GuestImage
from ..decode.thumb_sem import _md


_CREATE = {"xTaskCreate", "xTaskCreateStatic", "xTaskCreateAffinitySet",
           "xTaskCreateStaticAffinitySet"}
# vTaskDelete can delete another task. Treat even that case as a potential exit.
_MAY_TERMINATE = {"vTaskDelete", "abort", "_exit", "exit", "__assert_func",
                  "__assert_fail", "__stack_chk_fail", "vApplicationStackOverflowHook"}


@dataclass
class TaskLoops:
    call_pcs: frozenset[int]
    instruction_pcs: frozenset[int]
    registrations: list[dict]
    tasks: list[dict]

    def report(self) -> dict:
        return {"discovery": "freertos_r0_literal_or_explicit_entry",
                "loop_rule": "reachable closed single-entry cyclic component",
                "call_model": "direct calls may block; normal return resumes at fallthrough",
                "registrations": self.registrations, "tasks": self.tasks,
                "selected_call_sites": len(self.call_pcs),
                "selected_instructions": len(self.instruction_pcs)}


def _target(insn):
    if insn.operands and insn.operands[-1].type == A.ARM_OP_IMM:
        return insn.operands[-1].imm & 0xffffffff
    return None


def _predicated(insn):
    return insn.cc not in (A.ARM_CC_INVALID, A.ARM_CC_AL)


def _decode(image, func, ranges, md):
    insns = {}
    for lo, hi in ranges:
        lo, hi = max(func.start, lo), min(func.end, hi)
        if hi > lo:
            for insn in md.disasm(image.read(lo, hi - lo) or b"", lo):
                insns[insn.address] = insn
    return insns


def _resolve(image, addr, entries, ranges, md):
    """Follow pure direct branch veneers if the original address has no matching entry."""
    seen = set()
    while addr not in entries:
        if addr in seen or not any(lo <= addr < hi for lo, hi in ranges):
            return None
        container = image.func_at(addr)
        if container is not None and container.start != addr:
            return None
        seen.add(addr)
        data = image.read(addr, 4) or image.read(addr, 2) or b""
        branch = next(md.disasm(data, addr, count=1), None)
        if branch is None or branch.id != A.ARM_INS_B or _predicated(branch):
            return None
        addr = _target(branch)
        if addr is None:
            return None
    return addr


def _task_body(image, addr, funcs, ranges, md):
    root = _resolve(image, addr, funcs, ranges, md)
    seen = set()
    while root is not None and root not in seen:
        func = funcs[root]
        data = image.read(root, 4) or image.read(root, 2) or b""
        first = next(md.disasm(data, root, count=1), None)
        if first is None or first.id != A.ARM_INS_B or _predicated(first):
            break
        dst = _target(first)
        if dst is None or func.start <= dst < func.end:
            break
        # A kept public function symbol may itself consist of a single branch
        # to the displaced implementation. Preserve the registered pointer in
        # the receipt and analyze the symbol covering the implementation.
        body = _resolve(image, dst, funcs, ranges, md)
        if body is None:
            break
        seen.add(root)
        root = body
    return root


def _pointer(image, insn):
    if insn is None or insn.id != A.ARM_INS_LDR or _predicated(insn):
        return None
    ops = insn.operands
    if (len(ops) != 2 or ops[0].type != A.ARM_OP_REG or ops[0].reg != A.ARM_REG_R0
            or ops[1].type != A.ARM_OP_MEM or ops[1].mem.base != A.ARM_REG_PC
            or ops[1].mem.index):
        return None
    addr = ((insn.address + 4) & ~3) + ops[1].mem.disp
    raw = image.read(addr, 4)
    if raw is None:
        return None
    return addr, int.from_bytes(raw, "little")


def _edges(insn, may_terminate):
    pc = insn.address + insn.size
    target = _target(insn)
    if insn.id == A.ARM_INS_BL:
        return (pc, None) if target in may_terminate else (pc,)
    if insn.id == A.ARM_INS_B:
        return (target, pc) if _predicated(insn) else (target,)
    if insn.id in (A.ARM_INS_CBZ, A.ARM_INS_CBNZ):
        return (target, pc)
    if (insn.id in (A.ARM_INS_BKPT, A.ARM_INS_UDF, A.ARM_INS_SVC,
                    A.ARM_INS_TBB, A.ARM_INS_TBH, A.ARM_INS_BX, A.ARM_INS_BLX)
            or any(group in insn.groups for group in (capstone.CS_GRP_JUMP, capstone.CS_GRP_RET,
                                                       capstone.CS_GRP_INT, capstone.CS_GRP_IRET))
            or A.ARM_REG_PC in insn.regs_access()[1]):
        return (None, pc) if _predicated(insn) else (None,)
    return (pc,)


def _components(edges, predecessors):
    """Iterative Kosaraju pass, including very large task bodies."""
    order, seen = [], set()
    for root in edges:
        todo = [(root, False)]
        while todo:
            pc, finish = todo.pop()
            if finish:
                order.append(pc)
            elif pc not in seen:
                seen.add(pc)
                todo.append((pc, True))
                todo.extend((v, False) for v in edges[pc] if v in edges and v not in seen)
    seen.clear()
    for root in reversed(order):
        if root in seen:
            continue
        comp, todo = set(), [root]
        seen.add(root)
        while todo:
            pc = todo.pop()
            comp.add(pc)
            for pred in predecessors[pc]:
                if pred not in seen:
                    seen.add(pred)
                    todo.append(pred)
        yield comp


def _reachable(func, insns, may_terminate):
    edges, pred = {}, defaultdict(set)
    pending = [func.start]
    while pending:
        pc = pending.pop()
        if pc in edges or pc not in insns:
            continue
        edges[pc] = _edges(insns[pc], may_terminate)
        for dst in edges[pc]:
            if dst in insns:
                pred[dst].add(pc)
                pending.append(dst)
    return edges, pred


def _task(func, insns, may_terminate):
    edges, pred = _reachable(func, insns, may_terminate)
    selected, calls, cycles = set(), set(), []
    for comp in _components(edges, pred):
        if len(comp) == 1:
            pc, = comp
            if pc not in edges[pc]:
                continue
        entries = sorted(pc for pc in comp if pc == func.start or pred[pc] - comp)
        exits = {dst for pc in comp for dst in edges[pc] if dst not in comp}
        sites = sorted(pc for pc in comp if insns[pc].id == A.ARM_INS_BL)
        if exits:
            status = "cycle_has_exit"
        elif len(entries) != 1:
            status = "multiple_entry_cycle"
        else:
            status = "selected"
            selected.update(comp)
            calls.update(sites)
        cycles.append({"status": status, "entries": list(map(hex, entries)),
                       "instructions": len(comp), "direct_call_pcs": list(map(hex, sites)),
                       "outgoing": [hex(dst) for dst in sorted(exits - {None})]
                                   + (["unknown_or_termination"] if None in exits else [])})
    return selected, calls, {"name": func.name, "entry": hex(func.start),
                             "reachable_instructions": len(edges),
                             "status": "selected" if selected else "no_closed_task_loop",
                             "cycles": sorted(cycles, key=lambda row: row["entries"])}


def discover_task_loops(image: GuestImage, task_entries: Iterable[str | int] = ()) -> TaskLoops:
    """Scan direct registrations with an immediately preceding r0 literal load.

    Optional entry names or addresses extend recognition to other schedulers.
    Only symbol-bounded functions can be decoded. Refusals remain in the receipt.
    """
    md = _md()
    ranges = image.code_ranges()
    funcs = {}
    names = defaultdict(set)
    for func in image.funcs:
        funcs.setdefault(func.start, func)
        names[func.name].add(func.start)
    creates = {addr for name in _CREATE for addr in names[name]}
    may_terminate = {addr for name in _MAY_TERMINATE for addr in names[name]}
    roots, registrations = set(), []
    resolved = {}

    for func in funcs.values() if creates else ():
        insns = _decode(image, func, ranges, md)
        calls = []
        for pc, insn in insns.items():
            if insn.id != A.ARM_INS_BL or (dst := _target(insn)) is None:
                continue
            if dst not in resolved:
                resolved[dst] = _resolve(image, dst, creates, ranges, md)
            if resolved[dst] is not None:
                calls.append((pc, dst))
        if not calls:
            continue
        # Reject a literal definition bypassed by another branch to this call.
        reachable, predecessors = _reachable(func, insns, may_terminate)
        ending = {i.address + i.size: i for i in insns.values()}
        for pc, dst in calls:
            row = {"registrar": func.name, "call_pc": hex(pc),
                   "api_entry": hex(resolved[dst]), "status": "unresolved_task_pointer"}
            previous = ending.get(pc)
            if pc not in reachable:
                row["status"] = "unreachable_or_unresolved_registration"
            unique = pc != func.start and previous is not None and predecessors[pc] == {previous.address}
            literal = _pointer(image, previous) if unique else None
            if literal is not None:
                where, pointer = literal
                row.update(literal=hex(where), pointer=hex(pointer))
                root = _task_body(image, pointer & ~1, funcs, ranges, md) if pointer & 1 else None
                if root is not None:
                    roots.add(root)
                    row.update(status="resolved", entry=hex(root))
            registrations.append(row)

    for entry in task_entries:
        if isinstance(entry, str) and entry in names:
            matches = names[entry]
        else:
            try:
                addr = int(entry, 0) if isinstance(entry, str) else entry
            except ValueError:
                addr = None
            root = _resolve(image, addr & ~1, funcs, ranges, md) if isinstance(addr, int) else None
            matches = {root} if root is not None else set()
        row = {"explicit_entry": entry, "status": "unknown_or_ambiguous_task_entry"}
        if len(matches) == 1:
            root = _task_body(image, next(iter(matches)), funcs, ranges, md)
            roots.add(root)
            row.update(status="resolved", entry=hex(root))
        registrations.append(row)

    all_insns, all_calls, tasks = set(), set(), []
    for root in sorted(roots):
        func = funcs[root]
        insns, calls, report = _task(func, _decode(image, func, ranges, md), may_terminate)
        all_insns.update(insns)
        all_calls.update(calls)
        tasks.append(report)
    return TaskLoops(frozenset(all_calls), frozenset(all_insns), registrations, tasks)
