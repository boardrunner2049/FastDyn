"""Fully splice the audited ARM UDIV helper, including its synchronous trap."""

from __future__ import annotations

from ..model.native import FULL_UDIV_CLEANUP_KIND, FULL_UDIV_KIND, InlineCall, NativeSplice
from ..model.unit import TBUnit
from .native_inline import _require
from .native_udiv import _body as guarded_body
from .native_udiv import is_udiv_binding, prepare_udiv_splice
from .native_udiv_cleanup import continuation_safe, original_continuation_safe, shorten_udiv_hot


# In this exact executable raise_exception_ra passes its fifth argument to
# cpu_restore_state, then raises EXCP_DIVBYZERO and reaches cpu_loop_exit's
# siglongjmp. Its ENDBR64 entry and its non-returning behavior are part of the
# audit. Another build must establish these obligations for its own image.
_IMAGE = "sha256:2da1d94f82da8f182282877b47959507b1282e11469e7b418fdf7913e99f28b6"
_UDIV_VA = 0x8cf6e0
_RAISE_VA = 0x8cf4d0
_SOURCE = bytes.fromhex(
    "f30f1efa 85d2 7418 89d1 89f0 31d2 f7f1 31d2 31c9 31f6 31ff 4531c0 c3 0f1f4000 "
    "4883ec08 4c8b442408 f687e932010002 7516 31c0 4883c408 31d2 31c9 31f6 31ff "
    "4531c0 c3 0f1f4000 8b87600c0000 8b8487140c0000 83e010 74da b901000000 "
    "31d2 be17000000 e885fdffff"
)


def is_full_udiv_binding(binding) -> bool:
    callee = binding.callee
    return (is_udiv_binding(binding) and callee.image_id == _IMAGE
            and callee.elf_address == callee.image_offset == _UDIV_VA and callee.code == _SOURCE)


def _emit(request: InlineCall, *, cleanup: bool) -> tuple[NativeSplice, int, int]:
    # The shared proof checks the helper signature, pool geometry, absence of
    # a bridge fragment and the precise register effects of its nonzero arm.
    guarded_body(request)
    _require(request.image_id == _IMAGE and request.elf_address == request.image_offset == _UDIV_VA
             and request.callee_code == _SOURCE, "udiv_full_image",
             "full UDIV requires the audited QEMU exception and restore implementation")
    # The final E8 is the helper's only child call. Convert the measured ELF
    # target to its captured runtime address without assuming a fixed load bias.
    delta = len(_SOURCE) + int.from_bytes(_SOURCE[-4:], "little", signed=True)
    target = request.target_abs + delta
    _require(_UDIV_VA + delta == _RAISE_VA and 0 < target < 1 << 64,
             "udiv_full_target", "UDIV child is not the audited raise_exception_ra target")

    # Test the original EDX argument. The normal division arm at the end
    # falls straight through into the unchanged caller's continuation.
    code = bytearray(_SOURCE[4:6] + b"\x75\x00")             # TEST EDX,EDX; JNZ nonzero
    code += _SOURCE[41:48] + b"\x74\x00"                     # TEST ARM_FEATURE_M; JZ zero
    m_clear = len(code) - 1
    code += _SOURCE[72:88] + b"\x74\x00"                     # CCR[secure] & DIV_0_TRP
    trap_clear = len(code) - 1

    # Enter the original nested function with exactly its SysV argument
    # registers, flags and stack alignment. P is the stack pointer at the
    # original TCG CALL. Save the remapped, unadjusted GETPC at P-8. The
    # private scratch word at P-16 holds our target. The child sees P-24,
    # just as in the original. Its real CALL also supplies a CET shadow entry.
    pc_load = len(code)
    code += bytes.fromhex("4c8d0500000000 4150")                # LEA R8,[RIP+cont]; PUSH R8
    code += b"\x48\xb9" + target.to_bytes(8, "little") + b"\x51"  # MOVABS RCX,target; PUSH RCX
    code += _SOURCE[90:102]                                   # MOV ECX,1; XOR EDX,EDX; MOV ESI,23
    child = len(code)
    code += bytes.fromhex("ff1424 0f0b")                      # CALL [RSP]; UD2 (noreturn)
    # On zero with trapping disabled, restore the source's EAX result,
    # registers and final flags. On nonzero, copy the original division arm.
    zero_body = _SOURCE[50:52] + _SOURCE[56:67]
    hot_body = shorten_udiv_hot(_SOURCE[8:27]) if cleanup else _SOURCE[8:27]
    padding = (len(request.call_bytes) - len(code) - len(zero_body) - 2 - len(hot_body)) % 8
    code += b"\x90" * padding                                 # unreachable after UD2
    zero = len(code)
    code += zero_body + bytes((0xeb, len(hot_body)))           # JMP continuation
    hot = len(code)
    code += hot_body
    for field, destination in ((3, hot), (m_clear, zero), (trap_clear, zero)):
        code[field:field+1] = (destination-field-1).to_bytes(1, "little", signed=True)
    code[pc_load+3:pc_load+7] = (len(code)-pc_load-7).to_bytes(4, "little", signed=True)
    kind = FULL_UDIV_CLEANUP_KIND if cleanup else FULL_UDIV_KIND
    return NativeSplice(request.call_off, b"", bytes(code), kind=kind), child, target


def full_udiv_exception_call(request: InlineCall) -> tuple[int, int]:
    """Return the nested CALL's splice-relative offset and captured target."""
    cleanup = request.splice is not None and request.splice.kind == FULL_UDIV_CLEANUP_KIND
    expected, child, target = _emit(request, cleanup=cleanup)
    _require(request.splice == expected, "splice_correspondence", "unexpected full UDIV encoding")
    return child, target


def verify_full_udiv_body(request: InlineCall, code=None, continuation_off=None, sites=None,
                          tc_abs=None, *, tb_ret=None, epilogue=None) -> None:
    full_udiv_exception_call(request)
    splice = request.splice
    if splice.kind == FULL_UDIV_CLEANUP_KIND:
        start = splice.physical_start
        end = start + len(splice.code)
        _require(code is not None and sites is not None and tc_abs is not None
                 and continuation_off == end and code[start:end] == splice.code
                 and continuation_safe(code, end, sites, tc_abs, tb_ret, epilogue),
                 "splice_correspondence", "full UDIV cleanup needs a verified physical continuation")


def prepare_full_udiv_splice(unit: TBUnit, request: InlineCall, code_address: int, *,
                             cleanup: bool = False) -> NativeSplice:
    # The same original call, pool binding, intact guest owner, tracked return
    # PC, caller stack model and incoming-reference conditions apply to both modes.
    prepare_udiv_splice(unit, request, code_address)
    clean = cleanup and original_continuation_safe(unit, request, code_address)
    expected, _, _ = _emit(request, cleanup=clean)
    return expected


def verify_full_udiv_splice(unit: TBUnit, request: InlineCall, code_address: int) -> None:
    cleanup = request.splice is not None and request.splice.kind == FULL_UDIV_CLEANUP_KIND
    _require(request.splice == prepare_full_udiv_splice(unit, request, code_address, cleanup=cleanup),
             "splice_correspondence", "full UDIV or its scratch-register liveness differs")
