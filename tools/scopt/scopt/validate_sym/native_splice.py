"""Certify an x86-64 register-only helper stitched into its caller.

The incoming TCG instruction reads one i32 argument from [RBP+disp] into
EDI. The native helper starts by copying EDI into EAX and reaches a full
RDI overwrite before any further read. Retargeting the same load to EAX
therefore preserves its memory and fault behavior, and the identical native
body restores agreement on every register and on the architectural flag
relation at the continuation.
"""

from __future__ import annotations

from dataclasses import replace

import capstone
from capstone import x86 as X

from ..analysis.hostops import reg_family
from ..decode.x86 import Insn, decoder, sweep
from ..formats.enums import OpMarkKind, SiteKind
from ..model.native import InlineCall, NativeSplice
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


def _retarget_load(prefix: bytes) -> bytes:
    # MOV r32,[RBP+disp8/disp32] with no address-size, segment or REX
    # prefix. Only the ModR/M register field changes, from EDI to EAX.
    _require((len(prefix) == 3 and prefix[:2] == b"\x8b\x7d") or
             (len(prefix) == 6 and prefix[:2] == b"\x8b\xbd"),
             "splice_argument_load", "expected an adjacent MOV EDI,[RBP+disp]")
    return prefix[:1] + bytes((prefix[1] & 0xc7,)) + prefix[2:]


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


def _prepare_body(request: InlineCall, start_off: int, prefix: bytes) -> NativeSplice:
    typemask = request.tcg_typemask
    _require(type(typemask) is int and typemask in _I32_ABIS,
             "splice_abi", "expected (i32) -> i32")
    cont = request.continuation_off
    direct = len(request.call_bytes) == 5 and request.call_bytes[:1] == b"\xe8"
    indirect = len(request.call_bytes) == 6 and request.call_bytes[:2] == b"\xff\x15"
    _require(0 <= start_off < request.call_off and start_off + len(prefix) == request.call_off and
             cont == request.call_off + len(request.call_bytes) and
             ((direct and request.pool_slot_off is None) or
              (indirect and request.pool_slot_off is not None)),
             "splice_origin", "argument load and CALL do not form the captured span")
    load = _retarget_load(prefix)
    verify_fragment(request)

    source = request.callee_code
    instructions = list(decoder().disasm(source, 0))
    if instructions and bytes(instructions[0].bytes) == _ENDBR64:
        instructions.pop(0)
    _require(len(instructions) >= 2 and bytes(instructions[0].bytes) in _COPY_ARG and
             bytes(instructions[-1].bytes) == b"\xc3",
             "splice_callee_shape", "expected a MOV EAX,EDI prologue and one final near RET")
    body = instructions[1:-1]
    _require(not any(ins.group(group) for ins in body for group in (
        capstone.CS_GRP_JUMP, capstone.CS_GRP_CALL, capstone.CS_GRP_RET,
    )), "splice_callee_control", "splice requires a straight-line native body")

    argument_dead = False
    for insn in body:
        reads, writes = insn.regs_access()
        reads_rdi = any(_is_rdi(insn, reg) for reg in reads)
        if _kills_argument(insn, reads_rdi):
            argument_dead = True
            break
        _require(not reads_rdi and not any(_is_rdi(insn, reg) for reg in writes),
                 "splice_argument_live", f"RDI observed before a complete overwrite at {insn.address:#x}")
    _require(argument_dead, "splice_argument_live", "RDI is still different at the continuation")

    code = load + source[instructions[0].address + instructions[0].size:instructions[-1].address]
    padding = (cont - start_off - len(code)) % 8
    return NativeSplice(start_off=start_off, prefix=prefix, code=code + _PADDING[padding])


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


def prepare_splice(unit: TBUnit, request: InlineCall, code_address: int) -> NativeSplice:
    """Find and certify the captured argument load and candidate body."""
    verify_site(unit, request, code_address)
    base = code_address + unit.tc_offset
    start, end = request.call_off, request.continuation_off
    code_end = unit.pool_start if unit.pool_start is not None else unit.ldst_end
    callers = sweep(unit.code[:code_end], base)
    arg = next((ins for ins in callers if ins.address + ins.size == base + start), None)
    _require(arg is not None, "splice_argument_load", "CALL has no adjacent argument instruction")
    assert arg is not None
    first = arg.address - base
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

    return _prepare_body(request, first, arg.raw)


def verify_splice_body(request: InlineCall) -> None:
    """Reconstruct a certified body from its recorded source and original load."""
    splice = request.splice
    if splice is None:
        raise NativeInlineError("splice_missing", "native splice record required")
    expected = _prepare_body(request, splice.start_off, splice.prefix)
    _require(expected == replace(splice, branches=()), "splice_correspondence",
             "stitched code or padding differs")


def verify_splice(unit: TBUnit, request: InlineCall, code_address: int) -> None:
    """Recompute the splice using the original caller and compare its exact bytes."""
    expected = prepare_splice(unit, request, code_address)
    _require(request.splice is not None and expected == replace(request.splice, branches=()),
             "splice_correspondence",
             "stitched span, original load or replacement bytes differ")
