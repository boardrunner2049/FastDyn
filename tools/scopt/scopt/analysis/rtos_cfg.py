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


def patched_entry(image, pc, md):
    """The moved function behind an unsymbolized BOLT entry patch at ``pc``, or None.

    BOLT moves a function out of ``.bolt.org.text``, drops its symbol and
    ``$t`` mapping and rewrites the old entry as ``b.w <new entry>``. Accept
    exactly that shape: ``pc`` inside an executable section, no symbol there
    (or one starting at ``pc`` of at most 4 bytes), and a first instruction
    that is an unconditional B to the exact start of a symbolized function.
    Callers apply this only to direct control-transfer targets.
    """
    if not any(lo <= pc < hi for lo, hi in image.text_sections):
        return None
    func = image.func_at(pc)
    if func is not None and (func.start != pc or func.end - func.start > 4):
        return None
    branch = next(md.disasm(image.read(pc, 4) or b"", pc, count=1), None)
    if (branch is None or branch.id != A.ARM_INS_B or predicated(branch)
            or not branch.operands or branch.operands[0].type != A.ARM_OP_IMM):
        return None
    dst = branch.operands[0].imm & ~1
    target = image.func_at(dst)
    if dst == pc or target is None or target.start != dst:
        return None
    return dst


def resolve_veneer(image, addr, entries, ranges, md, *, patched=False):
    """Follow pure direct branch veneers if the original address has no matching entry.

    With ``patched`` (analysis revision 2), an address outside the Thumb code
    ranges may also be a BOLT entry patch (:func:`patched_entry`).
    """
    seen = set()
    while addr not in entries:
        if addr in seen:
            return None
        if not any(lo <= addr < hi for lo, hi in ranges):
            nxt = patched_entry(image, addr, md) if patched else None
            if nxt is None:
                return None
            seen.add(addr)
            addr = nxt
            continue
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


def resolve_function(image, addr, funcs, ranges, md, *, patched=False):
    root = resolve_veneer(image, addr, funcs, ranges, md, patched=patched)
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
        body = resolve_veneer(image, dst, funcs, ranges, md, patched=patched)
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


def jump_tables(image, func, insns, *, word_tables=False):
    """Recognize cmp index,#last; bhi default; tbb/tbh [pc,index{,lsl #1}].

    Require an aligned PC base, table bytes in a mapped data span, code targets,
    and a range check that cannot be bypassed by a known branch/table entry.
    This is only a selection graph, never authority for deleting an indirect exit.

    With ``word_tables`` (analysis revision 2), also recognize GCC's Thumb-2
    word table ``cmp index,#last; bhi default; adr base,table;
    ldr.w pc,[base,index,lsl #2]``: same guards, plus base != index, a
    4-aligned table and words that all have the Thumb bit set.
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
    if word_tables:
        for pc, i in insns.items():
            table = _word_table(image, func, insns, prev, i)
            if table is not None:
                targets, guards = table
                tables[pc] = targets
                landings.update(targets)
                guard_pairs[pc] = guards
    return {pc: dst for pc, dst in tables.items() if not landings.intersection(guard_pairs[pc])}


def _word_table(image, func, insns, prev, ldr):
    """``(targets, guarded pcs)`` of a GCC Thumb-2 word table dispatched by ``ldr``."""
    ops = ldr.operands
    if (ldr.id != A.ARM_INS_LDR or predicated(ldr) or len(ops) != 2
            or ops[0].type != A.ARM_OP_REG or ops[0].reg != A.ARM_REG_PC
            or ops[1].type != A.ARM_OP_MEM or ldr.writeback):
        return None
    mem = ops[1].mem
    if (mem.index in (0, A.ARM_REG_PC) or mem.disp or ops[1].shift.type != A.ARM_SFT_LSL
            or ops[1].shift.value != 2):
        return None
    adr = prev.get(ldr.address)
    if (adr is None or adr.id != A.ARM_INS_ADR or predicated(adr) or len(adr.operands) != 2
            or adr.operands[0].type != A.ARM_OP_REG or adr.operands[0].reg != mem.base
            or adr.operands[0].reg == mem.index or adr.operands[1].type != A.ARM_OP_IMM):
        return None
    guard = prev.get(adr.address)
    if guard is None or guard.id != A.ARM_INS_B or guard.cc != A.ARM_CC_HI:
        return None
    cmp = prev.get(guard.address)
    if (cmp is None or cmp.id != A.ARM_INS_CMP or predicated(cmp) or len(cmp.operands) != 2
            or cmp.operands[0].type != A.ARM_OP_REG or cmp.operands[0].reg != mem.index
            or cmp.operands[1].type != A.ARM_OP_IMM):
        return None
    start = ((adr.address + 4) & ~3) + adr.operands[1].imm
    last = cmp.operands[1].imm
    end = start + 4 * (last + 1)
    if last < 0 or start % 4 or end > func.end or (start, "d") not in image.mapping:
        return None
    if any(start < addr < end and kind != "d" for addr, kind in image.mapping):
        return None
    data = image.read(start, end - start)
    if data is None:
        return None
    words = [int.from_bytes(data[k:k + 4], "little") for k in range(0, len(data), 4)]
    if any(not word & 1 for word in words):
        return None
    targets = tuple(dict.fromkeys(word & ~1 for word in words))
    if any(t not in insns for t in targets):
        return None
    return targets, (ldr.address, adr.address, guard.address)


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
