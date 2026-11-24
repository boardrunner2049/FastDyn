"""Intraprocedural Thumb control flow shared by RTOS instance and loop analyses."""
from collections import defaultdict

import capstone
from capstone import arm as A


def direct_target(insn):
    if insn.operands and insn.operands[-1].type == A.ARM_OP_IMM:
        return insn.operands[-1].imm & 0xffffffff
    return None


def predicated(insn):
    return insn.cc not in (A.ARM_CC_INVALID, A.ARM_CC_AL)


def decode_function(image, func, ranges, md):
    insns = {}
    for lo, hi in ranges:
        lo, hi = max(func.start, lo), min(func.end, hi)
        if hi > lo:
            for insn in md.disasm(image.read(lo, hi - lo) or b"", lo):
                insns[insn.address] = insn
    return insns


def resolve_veneer(image, addr, entries, ranges, md):
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
        if branch is None or branch.id != A.ARM_INS_B or predicated(branch):
            return None
        addr = direct_target(branch)
        if addr is None:
            return None
    return addr


def resolve_function(image, addr, funcs, ranges, md):
    root = resolve_veneer(image, addr, funcs, ranges, md)
    seen = set()
    while root is not None and root not in seen:
        func = funcs[root]
        data = image.read(root, 4) or image.read(root, 2) or b""
        first = next(md.disasm(data, root, count=1), None)
        if first is None or first.id != A.ARM_INS_B or predicated(first):
            break
        dst = direct_target(first)
        if dst is None or func.start <= dst < func.end:
            break
        # A kept public function symbol may itself consist of a single branch
        # to the displaced implementation. Preserve the registered pointer in
        # the receipt and analyze the symbol covering the implementation.
        body = resolve_veneer(image, dst, funcs, ranges, md)
        if body is None:
            break
        seen.add(root)
        root = body
    return root


def _edges(insn, terminals, tables):
    pc = insn.address + insn.size
    target = direct_target(insn)
    if insn.id == A.ARM_INS_BL:
        return (pc, None) if target in terminals else (pc,)
    if insn.id == A.ARM_INS_BLX:
        # Its callee may be unknown; ordinary returns still resume at the next PC.
        return (pc,)
    if insn.id == A.ARM_INS_B:
        return (target, pc) if predicated(insn) else (target,)
    if insn.id in (A.ARM_INS_CBZ, A.ARM_INS_CBNZ):
        return (target, pc)
    if insn.address in tables:
        return tables[insn.address]
    if (insn.id in (A.ARM_INS_BKPT, A.ARM_INS_UDF, A.ARM_INS_SVC, A.ARM_INS_TBB,
                    A.ARM_INS_TBH, A.ARM_INS_BX)
            or any(group in insn.groups for group in (capstone.CS_GRP_JUMP, capstone.CS_GRP_RET,
                                                       capstone.CS_GRP_INT, capstone.CS_GRP_IRET))
            or A.ARM_REG_PC in insn.regs_access()[1]):
        return (None, pc) if predicated(insn) else (None,)
    return (pc,)


def jump_tables(image, func, insns):
    """Recognize cmp index,#last; bhi default; tbb/tbh [pc,index{,lsl #1}].

    Require an aligned PC base, table bytes in a mapped data span, code targets,
    and a range check that cannot be bypassed by a known branch/table entry.
    This is only a selection graph, never authority for deleting an indirect exit.
    """
    tables = {}
    guard_pairs = {}
    prev = {i.address + i.size: i for i in insns.values()}
    landings = {func.start}
    for i in insns.values():
        if i.id in (A.ARM_INS_B, A.ARM_INS_CBZ, A.ARM_INS_CBNZ, A.ARM_INS_BL):
            landings.add(direct_target(i))
    for pc, i in insns.items():
        if i.id not in (A.ARM_INS_TBB, A.ARM_INS_TBH) or pc % 4 or predicated(i):
            continue
        guard = prev.get(pc)
        if guard is None or guard.id != A.ARM_INS_B or guard.cc != A.ARM_CC_HI:
            continue
        cmp = prev.get(guard.address)
        if cmp is None or cmp.id != A.ARM_INS_CMP or predicated(cmp):
            continue
        if (len(cmp.operands) != 2 or cmp.operands[0].type != A.ARM_OP_REG or
                cmp.operands[1].type != A.ARM_OP_IMM or len(i.operands) != 1):
            continue
        op = i.operands[0]
        if (op.type != A.ARM_OP_MEM or op.mem.base != A.ARM_REG_PC or
                op.mem.index != cmp.operands[0].reg):
            continue
        width = 1 if i.id == A.ARM_INS_TBB else 2
        start = pc + 4
        last = cmp.operands[1].imm
        end = start + width * (last + 1)
        # The compiler may emit a separate data mapping for each table.
        if last < 0 or end > func.end or (start, "d") not in image.mapping:
            continue
        if any(start < addr < end and kind != "d" for addr, kind in image.mapping):
            continue
        data = image.read(start, end - start)
        if data is None:
            continue
        targets = tuple(dict.fromkeys(start + 2 * int.from_bytes(data[k:k+width], "little")
                                      for k in range(0, len(data), width)))
        if any(t not in insns for t in targets):
            continue
        tables[pc] = targets
        landings.update(targets)
        guard_pairs[pc] = (pc, guard.address)
    return {pc: dst for pc, dst in tables.items() if not landings.intersection(guard_pairs[pc])}


def reachable(func, insns, terminals, tables):
    edges, pred = {}, defaultdict(set)
    pending = [func.start]
    while pending:
        pc = pending.pop()
        if pc in edges or pc not in insns:
            continue
        edges[pc] = _edges(insns[pc], terminals, tables)
        for dst in edges[pc]:
            if dst in insns:
                pred[dst].add(pc)
                pending.append(dst)
    return edges, pred
