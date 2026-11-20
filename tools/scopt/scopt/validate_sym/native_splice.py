"""Certify register-only x86-64 helpers stitched into their TCG callers.

Copying a position-independent leaf through its single final RET preserves
its register and flags effects. Internal branches keep their displacements
and a branch to the original RET now reaches the fallthrough padding. An
optional unary argument fusion also removes the callee's initial register
copy when the original argument register becomes dead before its next use.
"""

from __future__ import annotations

from dataclasses import replace

import capstone
from capstone import x86 as X

from ..analysis.hostops import reg_family
from ..decode.x86 import Insn, decoder, sweep
from ..formats.enums import OpMarkKind, SiteKind
from ..model.native import InlineCall, NativeSplice, supports_tcg_integer_abi
from ..model.unit import TBUnit
from .native_inline import NativeInlineError, _require, verify_fragment, verify_site

# TCG's i32 and s32 type codes for the return and exactly one argument.
_I32_ABIS = frozenset(ret | (arg << 3) for ret in (2, 3) for arg in (2, 3))
_ENDBR64 = b"\xf3\x0f\x1e\xfa"
_COPY_ARG = frozenset((b"\x89\xf8", b"\x8b\xc7"))
_ZERO_RDI = frozenset(bytes.fromhex(code) for code in ("31ff", "33ff", "4831ff", "4833ff"))

# Intel's canonical single-instruction NOPs. These leave flags and all
# architecturally visible operands unchanged, including the nominal memory
# operand in the multibyte encodings. Keep TB-local layout delta 8-aligned.
_PADDING = tuple(bytes.fromhex(code) for code in (
    "", "90", "6690", "0f1f00", "0f1f4000", "0f1f440000", "660f1f440000", "0f1f8000000000",
))


def _is_rdi(insn: capstone.CsInsn, reg: int) -> bool:
    family = reg_family(insn.reg_name(reg))
    return family is not None and family[0] == "rdi"


def _kills_argument(insn: capstone.CsInsn, reads_rdi: bool) -> bool:
    # XOR of a complete register with itself is independent of the old
    # value even though the instruction decoder lists a read of that register.
    if bytes(insn.bytes) in _ZERO_RDI:
        return True
    if reads_rdi or insn.mnemonic not in ("mov", "movabs"):
        return False
    dst = insn.operands[0]
    return dst.type == X.X86_OP_REG and dst.reg in (X.X86_REG_EDI, X.X86_REG_RDI)


def _fused_body(source: bytes, instructions: list[capstone.CsInsn], prefix: bytes) -> bytes | None:
    # MOV EDI,[RBP+disp8/disp32] -> MOV EAX,[RBP+disp8/disp32].
    if not ((len(prefix) == 3 and prefix[:2] == b"\x8b\x7d") or
            (len(prefix) == 6 and prefix[:2] == b"\x8b\xbd")):
        return None
    if bytes(instructions[0].bytes) == _ENDBR64:
        instructions = instructions[1:]
    if len(instructions) < 2 or bytes(instructions[0].bytes) not in _COPY_ARG:
        return None
    body = instructions[1:-1]
    if any(ins.group(capstone.CS_GRP_JUMP) for ins in body):
        return None
    for insn in body:
        reads, writes = insn.regs_access()
        reads_rdi = any(_is_rdi(insn, reg) for reg in reads)
        if _kills_argument(insn, reads_rdi):
            load = prefix[:1] + bytes((prefix[1] & 0xc7,)) + prefix[2:]
            return load + source[instructions[0].address + instructions[0].size:-1]
        if reads_rdi or any(_is_rdi(insn, reg) for reg in writes):
            return None
    return None


def _prepare_body(request: InlineCall, prefix: bytes) -> NativeSplice:
    _require(supports_tcg_integer_abi(request.tcg_typemask),
             "splice_abi", "expected an integer result and up to six register arguments")
    start, cont = request.call_off, request.continuation_off
    direct = len(request.call_bytes) == 5 and request.call_bytes[:1] == b"\xe8"
    indirect = len(request.call_bytes) == 6 and request.call_bytes[:2] == b"\xff\x15"
    _require(0 <= start and cont == start + len(request.call_bytes) and
             ((direct and request.pool_slot_off is None) or
              (indirect and request.pool_slot_off is not None)),
             "splice_origin", "CALL does not match the captured span")
    verify_fragment(request)
    source = request.callee_code
    instructions = list(decoder().disasm(source, 0))
    _require(bytes(instructions[-1].bytes) == b"\xc3" and not any(
        ins.group(capstone.CS_GRP_RET) for ins in instructions[:-1]
    ), "splice_callee_control", "fallthrough splice requires a single final near RET")

    code = None
    if request.tcg_typemask in _I32_ABIS and start >= len(prefix):
        code = _fused_body(source, instructions, prefix)
    if code is None:
        # The TCG emitter already set up the SysV registers. Keeping the setup
        # allows all integer ABIs and preserves even caller-saved registers
        # that the original callee happened to leave unchanged.
        prefix = b""
        code = source[:-1]
        # ENDBR64 has no effect on a fallthrough. Keep it if the original
        # helper has a direct back edge to its entry so the displacement holds.
        if source.startswith(_ENDBR64) and not any(
            ins.group(capstone.CS_GRP_JUMP) and ins.operands[0].imm == 0 for ins in instructions
        ):
            code = code[len(_ENDBR64):]
    start -= len(prefix)
    padding = (cont - start - len(code)) % 8
    return NativeSplice(start_off=start, prefix=prefix, code=code + _PADDING[padding])


def _references_splice(insn: Insn, first: int, end: int) -> bool:
    # The main entry still works as a branch target. Reading or taking the
    # address of the old encoding can observe different bytes from that entry.
    target = insn.branch_target
    if target is not None and first < target < end:
        return True
    target, mem = insn.rip_target, insn.rip_mem
    if target is None or mem is None:
        return False
    size = 1 if insn.is_lea else max(mem.size, 1)
    return target < end and target + size > first


def _verify_span(unit: TBUnit, request: InlineCall, splice: NativeSplice,
                 base: int, callers: list[Insn]) -> None:
    start, end = request.call_off, request.continuation_off
    first = splice.start_off
    site, = (s for s in unit.sites if s.kind is SiteKind.HELPER_CALL and s.insn_off == start)
    _require(site.insn_idx is not None and any(
        insn.index == site.insn_idx and insn.host_start <= first and end <= insn.host_end
        for insn in unit.insns
    ), "splice_origin", "argument load and CALL must belong to one captured guest instruction")

    # Old offsets strictly inside the stitched span have no equivalent
    # entry point. The load entry and the continuation retain their meaning.
    _require(not any(_references_splice(ins, base + first, base + end) for ins in callers)
             and not any(
        fixup.target_abs is not None and (
            base + first < fixup.target_abs < base + end or
            fixup.rule == "abs_plus_delta" and fixup.target_abs == base + first
        ) for fixup in unit.reloc.fixups
    ), "splice_incoming_reference", "an existing reference observes the stitched span")
    _require(not any(
        s != site and (first <= s.insn_off < end or
                       first < s.field_off + s.width and s.field_off < end)
        for s in unit.sites
    ) and not any(
        first < f.field_off + f.width and f.field_off < end and
        (f.field_off, f.width, f.site_kind) != (site.field_off, site.width, int(site.kind))
        for f in unit.reloc.fixups
    ), "splice_origin", "the stitched span contains an unrelated relocation or site")
    _require(not any(
        first < x < end for insn in unit.insns for x in (insn.host_start, insn.host_end)
    ) and not any(
        first < p.host_return_offset < end for p in unit.recovery
    ) and not any(
        first < mark.host_off < end and
        (mark.kind != OpMarkKind.CALL or mark.helper_id != request.helper_id or
         mark.insn_idx != site.insn_idx)
        for mark in unit.opmarks
    ), "splice_origin", "the stitched span contains an unrelated guest or recovery boundary")


def prepare_splice(unit: TBUnit, request: InlineCall, code_address: int) -> NativeSplice:
    """Prefer argument fusion when every entry and observer permits it."""
    verify_site(unit, request, code_address)
    base = code_address + unit.tc_offset
    code_end = unit.pool_start if unit.pool_start is not None else unit.ldst_end
    callers = sweep(unit.code[:code_end], base)
    arg = next((ins for ins in callers if ins.end == base + request.call_off), None)
    splice = _prepare_body(request, arg.raw if arg is not None else b"")
    try:
        _verify_span(unit, request, splice, base, callers)
    except NativeInlineError:
        if not splice.prefix:
            raise
        # A branch directly to the old CALL or a separate guest origin can
        # forbid removing the load while allowing the call itself to be spliced.
        # Recheck the smaller footprint, including all accesses to its bytes.
        splice = _prepare_body(request, b"")
        _verify_span(unit, request, splice, base, callers)
    return splice


def verify_splice_body(request: InlineCall) -> None:
    """Reconstruct a certified body from its recorded source and original load."""
    splice = request.splice
    if splice is None:
        raise NativeInlineError("splice_missing", "native splice record required")
    expected = _prepare_body(request, splice.prefix)
    _require(expected == replace(splice, branches=()), "splice_correspondence",
             "stitched code or padding differs")


def verify_splice(unit: TBUnit, request: InlineCall, code_address: int) -> None:
    """Recompute the splice using the original caller and compare its exact bytes."""
    expected = prepare_splice(unit, request, code_address)
    _require(request.splice is not None and expected == replace(request.splice, branches=()),
             "splice_correspondence",
             "stitched span, original load or replacement bytes differ")
