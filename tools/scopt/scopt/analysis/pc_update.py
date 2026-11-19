"""Same-size folding of x86-64 ARM regs[15] updates at typed TCG exits."""
from ..analysis.syncdead import b4_pair_anchors
from ..decode.x86 import decoder, sweep
from ..formats.enums import OpMarkKind, SiteKind
from ..formats.sc_v5 import ScRecord
from ..validate_sym.program import program_of


def layout_matches(layout):
    return layout is not None and any(
        (field.name, field.region, field.off, field.elem, field.dims) == ("regs", "env", 0, 4, (16,))
        for field in layout.fields
    )


def fold_bytes(original):
    # mov ebx, [rbp+60]; add ebx, imm; mov [rbp+60], ebx.
    # The single memory ADD retains the immediate, operand width, and flags.
    # A no-index SIB and disp32 preserve the original length and all anchors.
    if original[:3] != b"\x8b\x5d\x3c" or original[-3:] != b"\x89\x5d\x3c":
        return None
    add = original[3:-3]
    if (len(add), add[:2]) not in ((3, b"\x83\xc3"), (6, b"\x81\xc3")):
        return None
    return bytes((0x40, add[0], 0x84, 0x25, 60, 0, 0, 0)) + add[2:]


def recorded_targets(snapshot):
    targets = set()
    for unit in snapshot.units():
        base = snapshot.header.code_address + unit.tc_offset
        end = unit.pool_start if unit.pool_start is not None else unit.ldst_end
        for insn in sweep(unit.code[:end], base):
            targets.update(x for x in (insn.branch_target, insn.rip_target) if x is not None)
        targets.update(site.value for site in unit.sites)
        targets.update(entry.data0 for entry in unit.pool)
        targets.update(reloc.target_abs for reloc in unit.reloc.fixups if reloc.target_abs is not None)
    return targets


def _captured_exit(tail, unit, prog, record, sites):
    """RBX is a TB-local temporary. Check all remaining instructions up to the public TB ABI."""
    if not tail:
        return False
    first = tail[0]
    if bytes(first.bytes) == b"\x48\x8b\xfd":
        if len(tail) < 3:
            return False
        call, jump = tail[1:3]
        helper_site, jump_site = sites.get(call.address), sites.get(jump.address)
        if not (helper_site and helper_site.kind == SiteKind.HELPER_CALL and
                bytes(call.bytes[:2]) == b"\xff\x15" and jump_site and
                jump_site.kind == SiteKind.GOTO_PTR and bytes(jump.bytes) == b"\xff\xe0"):
            return False
        helper = prog.helpers.get(helper_site.value)
        return bool(helper and helper.name in ("lookup_tb_ptr", "helper_lookup_tb_ptr") and any(
            mark.host_off == first.address and mark.kind == OpMarkKind.CALL and mark.helper_id == helper.id
            for mark in unit.opmarks))

    start = 1 if first.mnemonic == "nop" else 0
    if len(tail) < start + 3:
        return False
    jump, lea, ret = tail[start:start + 3]
    link = sites.get(jump.address)
    token = sites.get(lea.address)
    back = sites.get(ret.address)
    if not (link and link.kind == SiteKind.GOTO_TB and link.aux in (0, 1) and
            bytes(jump.bytes) == b"\xe9\0\0\0\0" and bytes(lea.bytes[:3]) == b"\x48\x8d\x05" and
            token and token.kind == SiteKind.MOVI_EXIT_TB_PTR and
            back and back.kind == SiteKind.EXIT_TB_RET_JMP and bytes(ret.bytes[:1]) == b"\xe9"):
        return False
    slot = link.aux
    return (record.jmp_insn_offset[slot] == jump.address + 1 and
            record.jmp_reset_offset[slot] == jump.address + 5 == lea.address and
            link.value == prog.base + lea.address and
            token.value == prog.struct_base + slot and back.value == prog.tb_ret)


def folded_exits(unit, world, targets):
    """Return captured offsets with independently checked row, relocation and exit boundaries."""
    if (not layout_matches(world.layout) or unit.meta_status != "ok" or not unit.reloc.relocatable
            or unit.origin != "elf_verified" or unit.native_fragments):
        return {}
    anchors = set(b4_pair_anchors(unit, world.code_address))
    for access in unit.ldst:
        anchors.update((access.raddr_off, access.slow_start, access.slow_end))
    prog = program_of(unit, world)
    record = ScRecord.from_bytes(unit.record)
    sites = {site.insn_off: site for site in unit.sites}
    end = unit.pool_start if unit.pool_start is not None else unit.ldst_end
    ops = list(decoder().disasm(unit.code[:end], 0))
    result = {}
    for i in range(len(ops) - 3):
        start, stop = ops[i].address, ops[i + 3].address
        old = unit.code[start:stop]
        new = fold_bytes(old)
        if new is None:
            continue
        if any(k in anchors or prog.base + k in targets for k in range(start + 1, stop)):
            continue
        if not any(row.host_start <= start < stop <= row.host_end for row in unit.insns):
            continue
        if _captured_exit(ops[i + 3:i + 8], unit, prog, record, sites):
            result[start] = (old, new)
    return result
