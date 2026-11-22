"""Remove proven-dead register clears from a guarded UDIV after it has been spliced.

The retained final XOR R8D,R8D reproduces the original flags, including the
undefined AF. The zero arm still enters the original helper with the original
register state and the bound return PC. Only the continuation of the nonzero
arm can observe the changed values of RDX, RCX, RSI and RDI.
"""

from collections.abc import Sequence
from dataclasses import replace

import capstone
from capstone import x86 as X

from ..analysis.hostops import reg_family
from ..decode.x86 import decoder
from ..formats.enums import Encoding, SiteKind
from ..model.native import UDIV_CLEANUP_KIND as KIND, InlineCall, NativeSplice
from ..model.unit import TBUnit
from .native_inline import _REGISTER_MNEMONICS, _require
from .native_udiv import _body, _emit_guarded, prepare_udiv_splice


_CLEAR_INSNS = {"rdx": b"\x31\xd2", "rcx": b"\x31\xc9", "rsi": b"\x31\xf6", "rdi": b"\x31\xff"}
_CLEARED = frozenset(_CLEAR_INSNS)
# These opcodes have no implicit control flow. Floating-point, vector,
# string, conditional moves, host calls, and guest data accesses are refused.
_DATA = _REGISTER_MNEMONICS | {"imul"}
_HOT = bytes.fromhex("89d1 89f0 31d2 f7f1")
_REMOVED = b"".join(_CLEAR_INSNS.values())
_FLAGS = bytes.fromhex("4531c0")


def _through_data(insn, pending: frozenset[str]) -> frozenset[str] | None:
    """Return still-different families, or None if any such value is observed."""
    if insn.mnemonic == "nop":
        return pending
    if insn.mnemonic not in _DATA or any(insn.prefix):
        return None
    for operand in insn.operands:
        if operand.type == X.X86_OP_MEM and insn.mnemonic != "lea":
            # Only the caller's private CPU state. An arbitrary pointer could
            # lead into an MMIO/slow path outside this intrablock proof.
            if operand.mem.base != X.X86_REG_RBP or operand.mem.index or operand.mem.segment:
                return None

    reads, _ = insn.regs_access()
    families = {family[0] for reg in reads if (family := reg_family(insn.reg_name(reg)))}
    operands = insn.operands
    if (len(operands) == 2 and operands[0].type == X.X86_OP_REG
            and operands[0].size in (4, 8)):
        dst = reg_family(insn.reg_name(operands[0].reg))
        zero = (insn.mnemonic == "xor" and operands[1].type == X.X86_OP_REG
                and operands[1].reg == operands[0].reg)
        if zero and dst:
            return pending - {dst[0]}
        if dst and insn.mnemonic in ("mov", "movzx", "movsx", "movsxd", "lea"):
            # An unconditional full-width definition is a kill only after
            # accounting for its sources (including the destination in an EA).
            if families & pending:
                return None
            return pending - {dst[0]}
    return None if families & pending else pending


def continuation_safe(code: bytes, entry: int, sites: Sequence, tc_abs: int,
                       tb_ret: int | None, epilogue: int | None, *, differing=_CLEARED) -> bool:
    """Forward use-before-def on both successors, up to authenticated TCG exits."""
    insns = {i.address - tc_abs: i for i in decoder().disasm(code, tc_abs)}
    branch_rows = {s.insn_off: s for s in sites if s.kind in (
        SiteKind.LABEL_JMP, SiteKind.LABEL_JCC, SiteKind.GOTO_TB,
        SiteKind.EXIT_TB_RET_JMP, SiteKind.EXIT_TB_EPI_JMP)}
    work = [(entry, differing)]
    visited = {}
    while work:
        off, pending = work.pop()
        pending = pending - visited.get(off, frozenset())
        if not pending:
            continue
        visited[off] = visited.get(off, frozenset()) | pending
        if off < entry or off not in insns or len(visited) > 256:
            return False
        ins = insns[off]
        nxt = off + ins.size
        if not ins.group(capstone.CS_GRP_JUMP):
            remaining = _through_data(ins, pending)
            if remaining is None:
                return False
            work.append((nxt, remaining))
            continue

        site = branch_rows.get(off)
        if (site is None or len(ins.operands) != 1 or ins.operands[0].type != X.X86_OP_IMM
                or any(ins.prefix) or ins.imm_size not in (1, 4)
                or site.field_off != off + ins.imm_offset or site.width != ins.imm_size
                or site.value != ins.operands[0].imm):
            return False
        if site.kind in (SiteKind.EXIT_TB_RET_JMP, SiteKind.EXIT_TB_EPI_JMP):
            dest = tb_ret if site.kind == SiteKind.EXIT_TB_RET_JMP else epilogue
            # The TCG return ABI observes RAX/RBP/RSP and the architected env.
            # Independent import/relocation evidence binds this exact endpoint.
            if (ins.bytes[0] != 0xe9 or site.enc != Encoding.REL32 or site.value != dest
                    or tc_abs <= site.value < tc_abs + len(code)):
                return False
            continue
        if site.kind == SiteKind.GOTO_TB:
            # Runtime linking can jump to the successor's public TCG entry;
            # the unlinked reset path (including its epilogue) must also pass.
            if (bytes(ins.bytes) != b"\xe9\0\0\0\0" or site.enc != Encoding.GOTO_TB_REL32
                    or site.aux not in (0, 1)):
                return False
        elif site.enc != (Encoding.REL8 if ins.imm_size == 1 else Encoding.REL32):
            return False
        elif site.kind == SiteKind.LABEL_JCC:
            # Restrict to plain Jcc. LOOP/JRCXZ read counter registers.
            raw = ins.bytes
            if not ((len(raw) == 2 and 0x70 <= raw[0] <= 0x7f)
                    or (len(raw) == 6 and raw[0] == 0x0f and 0x80 <= raw[1] <= 0x8f)):
                return False
            work.append((nxt, pending))
        elif ins.bytes[0] not in (0xe9, 0xeb):
            return False
        work.append((site.value - tc_abs, pending))
    return True


def shorten_udiv_hot(hot: bytes, removed=_CLEARED) -> bytes:
    """Substitute the exact source arithmetic and final flag-producing XOR."""
    _require(hot == _HOT + _REMOVED + _FLAGS, "splice_callee_shape",
             "UDIV cleanup does not match the authenticated nonzero arm")
    return _HOT + b"".join(ins for reg, ins in _CLEAR_INSNS.items() if reg not in removed) + _FLAGS


def _compact(request: InlineCall, ordinary: NativeSplice, removed=_CLEARED) -> NativeSplice:
    end = 4 + len(_HOT) + len(_REMOVED) + len(_FLAGS)
    return replace(_emit_guarded(request, shorten_udiv_hot(ordinary.code[4:end], removed)), kind=KIND)


def _prologue_target(unit: TBUnit, target_class: str) -> int | None:
    targets = {f.target_abs for f in unit.reloc.fixups if f.target_class == target_class
               and f.rule == "rel_minus_delta" and f.width == 4}
    return next(iter(targets)) if len(targets) == 1 else None


def original_continuation_safe(unit: TBUnit, request: InlineCall, code_address: int, *, differing=_CLEARED) -> bool:
    """Check the source unit with epilogue addresses bound by its validated fixups."""
    return unit.meta_status == "ok" and continuation_safe(
        unit.code[:unit.pool_start], request.continuation_off, unit.sites,
        code_address + unit.tc_offset, _prologue_target(unit, "prologue.tb_ret"),
        _prologue_target(unit, "prologue.epilogue"), differing=differing
    )


def prepare_udiv_cleanup(unit: TBUnit, request: InlineCall, code_address: int, *, partial=False) -> NativeSplice:
    ordinary = prepare_udiv_splice(unit, request, code_address)
    if original_continuation_safe(unit, request, code_address):
        return _compact(request, ordinary)
    if partial:
        removed = {reg for reg in _CLEAR_INSNS
                   if original_continuation_safe(unit, request, code_address, differing={reg})}
        if removed:
            return _compact(request, ordinary, removed)
    return ordinary


def _checked_removals(request: InlineCall, ordinary: NativeSplice) -> frozenset[str]:
    """Read the four optional XORs in source order and reconstruct the complete body."""
    splice = request.splice
    _require(splice is not None and splice.kind == KIND, "splice_correspondence",
             "expected a guarded UDIV cleanup")
    offset = 4 + len(_HOT)
    removed = set()
    for reg, clear in _CLEAR_INSNS.items():
        if splice.code.startswith(clear, offset):
            offset += len(clear)
        else:
            removed.add(reg)
    _require(bool(removed) and splice == _compact(request, ordinary, removed),
             "splice_correspondence", "UDIV cleanup fast path, flags or fallback differs")
    return frozenset(removed)


def verify_udiv_cleanup_splice(unit: TBUnit, request: InlineCall, code_address: int) -> None:
    ordinary = prepare_udiv_splice(unit, request, code_address)
    removed = _checked_removals(request, ordinary)
    _require(original_continuation_safe(unit, request, code_address, differing=removed),
             "splice_correspondence", "UDIV cleanup scratch registers are live")


def verify_udiv_cleanup_body(request: InlineCall, code: bytes, continuation_off: int,
                             sites: Sequence, tc_abs: int, *, tb_ret: int | None = None,
                             epilogue: int | None = None) -> None:
    removed = _checked_removals(request, _body(request))
    _require(continuation_safe(code, continuation_off, sites, tc_abs, tb_ret, epilogue, differing=removed),
             "splice_correspondence", "UDIV cleanup scratch registers are live in the linked caller")
