"""Bounded x86 rewrite of the non-icount TCG interrupt poll."""
from capstone import x86 as X
from .hconst import decode_one
from .syncdead import b4_pair_anchors
from ..formats.enums import SiteKind

OLD = bytes.fromhex("8b 5d f8 85 db")
# REX preserves the existing JL and all subsequent offsets.
NEW = bytes.fromhex("40 83 7d f8 00")


def matches_irq_patch(unit, patch, code_address, tb_ret):
    return (patch.host_off == 0 and patch.old_bytes == OLD and patch.new_bytes == NEW
            and can_fuse_irq_entry(unit, code_address, tb_ret))


def can_fuse_irq_entry(unit, code_address, tb_ret):
    """Prove scratch death on both successors and reject interior anchors."""
    code = unit.code
    if (unit.meta_status != "ok" or not unit.reloc.relocatable or unit.native_fragments
            or not code.startswith(OLD + b"\x0f\x8c")
            or code[11:15] != bytes.fromhex("c6 45 fc 00")):
        return False
    if any(0 < offset < len(OLD) for offset in b4_pair_anchors(unit, code_address)):
        return False
    slow = 11 + int.from_bytes(code[7:11], "little", signed=True)
    # The slow edge returns through the captured TCG epilogue. It materializes
    # only its tagged TB pointer in RAX, then restores host callee-saved RBX.
    if (not 15 < slow < unit.body_end or code[slow:slow+3] != bytes.fromhex("48 8d 05")
            or code[slow+7:slow+8] != b"\xe9"):
        return False
    slow_jump = slow + 12 + code_address + unit.tc_offset + int.from_bytes(
        code[slow+8:slow+12], "little", signed=True)
    slow_sites = {(s.insn_off, s.kind, s.value) for s in unit.sites}
    if ((slow+7, SiteKind.EXIT_TB_RET_JMP, tb_ret) not in slow_sites or slow_jump != tb_ret
            or not any(s.insn_off == slow and s.kind == SiteKind.MOVI_EXIT_TB_PTR for s in unit.sites)):
        return False
    # Restrict the taken-through path to a complete EBX overwrite from an
    # immediate or a CPU-env field. Neither observes the old EBX or flags.
    first = decode_one(code, 15, 15)
    if first is None or first.mnemonic != "mov" or len(first.operands) != 2:
        return False
    dst, src = first.operands
    if dst.type != X.X86_OP_REG or dst.reg != X.X86_REG_EBX:
        return False
    if src.type != X.X86_OP_IMM and not (
            src.type == X.X86_OP_MEM and src.mem.base == X.X86_REG_RBP
            and not src.mem.index and not src.mem.segment and src.mem.disp >= 0):
        return False
    # CMP x,0 and TEST x,x agree on SF/ZF/PF and clear OF/CF. TEST's AF
    # is undefined. Admit only a straight path where AF is replaced before
    # any observation, including a call, branch, or guest memory operation.
    offset = 15 + first.size
    for _ in range(16):
        ins = decode_one(code, offset, 15)
        if ins is None:
            return False
        if ins.mnemonic in ("add", "sub", "cmp", "test", "and", "or", "xor", "inc", "dec"):
            return True
        if (ins.mnemonic in ("shr", "shl", "sar") and len(ins.operands) == 2
                and ins.operands[1].type == X.X86_OP_IMM and 0 < ins.operands[1].imm < 32):
            return True
        if ins.mnemonic not in ("mov", "movzx", "movsx", "movsxd", "lea", "nop"):
            return False
        if ins.mnemonic != "lea" and any(o.type == X.X86_OP_MEM and (
                o.mem.base != X.X86_REG_RBP or o.mem.index or o.mem.segment) for o in ins.operands):
            return False
        offset += ins.size
    return False
