"""Splice the nonzero arm of the bound ARM UDIV helper and retain zero recovery."""

from __future__ import annotations

from ..decode.x86 import sweep
from ..formats.enums import SiteKind
from ..model.native import (FULL_UDIV_KINDS, UDIV_29_INLINE_KIND, UDIV_29_KIND, UDIV_CLEANUP_KIND, UDIV_KIND, UDIV_TYPEMASK,
                            InlineCall, NativeFragment, NativeSplice)
from ..model.unit import TBUnit
from .native_inline import _require, _verify_bound_site
from .native_splice import _references_splice


_ENDBR64 = bytes.fromhex("f30f1efa")
_TEST_EDX = bytes.fromhex("85d2")
# Move the arguments, form a zero-extended dividend and divide. The exact
# native tail also restores every call-clobbered register/flag it writes.
_NONZERO = bytes.fromhex("89d1 89f0 31d2 f7f1 31d2 31c9 31f6 31ff 4531c0")


def is_udiv_binding(binding) -> bool:
    call, callee = binding.call, binding.callee
    return (callee is not None and callee.role == "qemu" and callee.abi == "sysv-amd64"
            and callee.symbol == call.symbol == "helper_udiv" and call.helper_name == "udiv"
            and callee.tcg_typemask == UDIV_TYPEMASK and call.helper_flags == 1)


def _body(request: InlineCall) -> NativeSplice:
    _require(request.symbol == "helper_udiv" and type(request.tcg_typemask) is int
             and request.tcg_typemask == UDIV_TYPEMASK and request.fragment == NativeFragment(b"", (), ()),
             "splice_abi", "expected the captured ARM UDIV helper signature")
    start, cont = request.call_off, request.continuation_off
    call, slot = request.call_bytes, request.pool_slot_off
    _require(0 <= start and cont == start + 6 and call[:2] == b"\xff\x15" and len(call) == 6
             and slot is not None and slot >= cont
             and cont + int.from_bytes(call[2:], "little", signed=True) == slot,
             "splice_origin", "UDIV needs a forward pool-indirect CALL with an exact return PC")
    source = request.callee_code.removeprefix(_ENDBR64)
    arm = 4
    ret = arm + len(_NONZERO)
    _require(len(source) > ret and source[:3] == _TEST_EDX + b"\x74"
             and source[arm:ret] == _NONZERO and source[ret] == 0xc3
             and ret < arm + int.from_bytes(source[3:4], "little", signed=True) < len(source),
             "splice_callee_shape", "the bound native nonzero UDIV arm does not match")

    zero_target = arm + int.from_bytes(source[3:4], "little", signed=True)
    cold = sweep(source[ret + 1:], ret + 1)
    _require(any(ins.address == zero_target for ins in cold)
             and all(ins.mnemonic == "nop" for ins in cold if ins.address < zero_target),
             "splice_callee_shape", "UDIV zero branch must land at an instruction after the fast return")

    # On zero the extra TEST writes only {CF,PF,AF,ZF,SF,OF}; the original
    # helper overwrites these again before reading any, including undefined AF.
    # The guard and jumps leave argument registers and the other flags intact.
    # Nonzero operands cannot raise #DE: EDX:EAX = zero-extended u32 and ECX
    # is nonzero at DIV. Zero can unwind with GETPC through the original helper.
    # Its CALL ends exactly at the remapped guest continuation. The local pool
    # moves by the same amount, so its original RIP displacement remains exact.
    return _emit_guarded(request, source[arm:ret])


def _emit_guarded(request: InlineCall, hot: bytes, *, guard=_TEST_EDX, fallback_branch=0x74, kind=UDIV_KIND) -> NativeSplice:
    """Keep the fallback CALL ending at the exact remapped guest continuation."""
    call = request.call_bytes
    header_size = len(guard) + 2
    padding = (-(header_size + len(hot) + 2)) % 8
    to_call = len(hot) + 2 + padding
    over_call = padding + len(call)
    code = (guard + bytes((fallback_branch, to_call)) + hot + bytes((0xeb, over_call))
            + b"\x90" * padding + call)
    return NativeSplice(request.call_off, b"", code, kind=kind)


def _reciprocal29_body(request: InlineCall, *, inline_miss=False) -> NativeSplice:
    _body(request)  # Reprove the captured helper entry, ABI and complete register/flag tail.
    # M = ceil(2**36 / 29), 29*M - 2**36 = 5. For n < 2**32,
    # 5*n / (29*2**36) < 1/29. Even remainder 28 cannot round
    # into the next quotient. The unsigned 32x32 product fits 64 bits.
    multiply = bytes.fromhex("89f0 b909cb3d8d 480fafc1 48c1e824")
    clears = _NONZERO[8:]
    if inline_miss:
        # Zero retains the precise helper CALL. Other nonmatches use the
        # original hardware divide and join the same register/flags tail.
        ordinary = _NONZERO[:8]
        check = bytes.fromhex("83fa1d") + bytes((0x74, len(ordinary) + 2))
        hot = check + ordinary + bytes((0xeb, len(multiply))) + multiply + clears
        return _emit_guarded(request, hot, kind=UDIV_29_INLINE_KIND)
    return _emit_guarded(request, multiply + clears, guard=bytes.fromhex("83fa1d"),
                         fallback_branch=0x75, kind=UDIV_29_KIND)


def verify_reciprocal29_body(request: InlineCall) -> None:
    _require(_reciprocal29_body(request, inline_miss=request.splice.kind == UDIV_29_INLINE_KIND) == request.splice, "splice_correspondence",
             "constant-29 guard, quotient, clobbers or retained recovery CALL differ")


def prepare_reciprocal29(unit: TBUnit, request: InlineCall, code_address: int, *, inline_miss=False) -> NativeSplice:
    prepare_udiv_splice(unit, request, code_address)
    return _reciprocal29_body(request, inline_miss=inline_miss)


def verify_udiv_body(request: InlineCall) -> None:
    _require(_body(request) == request.splice, "splice_correspondence",
             "guarded UDIV bytes, fallback or geometry differ from the bound source")


def prepare_udiv_splice(unit: TBUnit, request: InlineCall, code_address: int) -> NativeSplice:
    splice = _body(request)
    _verify_bound_site(unit, request, code_address, required_flags=1)
    binding, = (h for h in unit.bindings.helpers if h.id == request.helper_id)
    _require(binding.name == "udiv" and binding.flags == 1, "unsupported_recovery",
             "expected the captured ARM UDIV helper with flags NO_RWG")
    first, end = request.call_off, request.continuation_off
    site, = (s for s in unit.sites if s.kind is SiteKind.HELPER_CALL and s.insn_off == first)
    _require(site.field_off == first + 2 and site.width == 4
             and site.final_field == int.from_bytes(request.call_bytes[2:], "little")
             and any(guest.index == site.insn_idx and guest.host_start <= first and end <= guest.host_end
                     for guest in unit.insns)
             and any(p.source == "helper_call" and p.insn == site.insn_idx and p.host_return_offset == end
                     for p in unit.recovery), "splice_origin",
             "UDIV needs one captured call, guest owner and original return recovery")
    base = code_address + unit.tc_offset
    callers = sweep(unit.code[:unit.pool_start], base)
    _require(not any(_references_splice(i, base + first, base + end) for i in callers)
             and not any(f.target_abs is not None and
                         (base + first < f.target_abs < base + end or
                          f.rule == "abs_plus_delta" and f.target_abs == base + first)
                         for f in unit.reloc.fixups),
             "splice_incoming_reference", "a reference observes the original CALL encoding")
    _require(not any(s != site and (first <= s.insn_off < end or first < s.field_off + s.width
                                    and s.field_off < end) for s in unit.sites)
             and not any(first < f.field_off + f.width and f.field_off < end for f in unit.reloc.fixups)
             and not any(first < p.host_return_offset < end for p in unit.recovery)
             and not any(first < m.host_off < end for m in unit.opmarks),
             "splice_origin", "the retained pool call overlaps unrelated metadata")
    return splice


def verify_udiv_splice(unit: TBUnit, request: InlineCall, code_address: int) -> None:
    _require(prepare_udiv_splice(unit, request, code_address) == request.splice,
             "splice_correspondence", "guarded UDIV splice differs from the original site")


def verify_udiv_variant(unit: TBUnit, request: InlineCall, code_address: int) -> None:
    kind = request.splice.kind
    if kind in (UDIV_29_KIND, UDIV_29_INLINE_KIND):
        _require(prepare_reciprocal29(unit, request, code_address, inline_miss=kind == UDIV_29_INLINE_KIND) == request.splice,
                 "splice_correspondence", "constant-29 site/body mismatch")
    elif kind in FULL_UDIV_KINDS:
        from .native_udiv_full import verify_full_udiv_splice

        verify_full_udiv_splice(unit, request, code_address)
    elif kind == UDIV_CLEANUP_KIND:
        from .native_udiv_cleanup import verify_udiv_cleanup_splice

        verify_udiv_cleanup_splice(unit, request, code_address)
    else:
        verify_udiv_splice(unit, request, code_address)
