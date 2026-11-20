"""Exact framed FastDyn virtual proof under the nested SysV ABI contract.

The matched caller has QEMU's ordinary SysV TCG stack phase and a valid mapped
stack. Each removed CALL's return slot and corresponding CET shadow-stack
slot are unobserved. The retained PLT call follows its normal SysV contract:
it returns without reading caller return addresses, unwinding, or invoking
a stack observer. The execution contract excludes async unwind, PLT auditing,
interposition with different semantics, and JIT return-address inspection.

Under that contract, reserve the outer CALL's eight-byte slot with a
flag-neutral LEA. The original frame and nested CALL then use the same stack
addresses and alignment. LEAVE restores the real caller's RBP. A second LEA
restores RSP without changing flags. The original and copied nested return
addresses differ only by the corresponding code locations. Keep all other
instructions and rebind PC-relative operands to exactly the original targets.
"""

from __future__ import annotations

import capstone
from capstone import x86 as X

from ..canon import is_sha256_id
from ..decode.x86 import decoder, sweep, tiles
from ..formats.enums import Encoding, OpMarkKind, SiteKind
from ..model.native import InlineCall, NativeFrameBody, NativeRelocation, NativeSplice
from ..model.unit import TBUnit
from .frame_chain import valid_chain
from .native_inline import NativeInlineError, _require, _stack_reg
from .native_splice import _PADDING, _references_splice

_PLUGIN_IMAGE_ID = "sha256:b6b9cb54ee1b3c1320299c0dfca6163f624d7876daa3389793364605db79dc0b"
_PLUGIN_CALLBACK_TYPEMASK = 400  # TCG void/u32/ptr
_BENCHMARK_START = 0x26236  # Both ELF VA and file offset in this exact image.
_BENCHMARK_START_CODE = bytes.fromhex(
    "f30f1efa"        # ENDBR64
    "554889e5"        # push rbp; mov rbp,rsp
    "4883ec10"        # sub rsp,16, still SysV aligned for the nested call
    "897dfc488975f0"  # spill cpu_index and udata to the private frame
    "48c705847af40400000000"  # g_bench_tick_count = 0
    "488d05657af404"  # address of g_bench_start_ts
    "4889c6bf01000000"  # RSI = timespec pointer; EDI = CLOCK_MONOTONIC
    "e86851feff"      # same clock_gettime@GLIBC_2.17 PLT stub (JUMP_SLOT 0x39168)
    "c605617af40401"  # g_bench_started = true, after the clock call
    "90c9c3"          # NOP; LEAVE; RET
)
_CANARY_IMAGE_ID = "sha256:fd9e57f674961120fbeb791dd16b5dac8fa211a7695b4cded64d27ce371677fc"
_RESERVE = b"\x48\x8d\x64\x24\xf8"
_RELEASE = b"\x48\x8d\x64\x24\x08"


def legacy_frame_image(image_id: str) -> bool:
    return image_id in (_CANARY_IMAGE_ID, _PLUGIN_IMAGE_ID)


# The isolated canary factors the same benchmark work into a private direct
# callee. The ELF digest binds both bodies and the already-audited clock PLT.
_CANARY_ENTRY = NativeFrameBody(0x26267, bytes.fromhex(
    "f30f1efa554889e54883ec10897dfc488975f0e8b7ffffff90c9c3"
))
_CANARY_WORKER = NativeFrameBody(0x26236, bytes.fromhex(
    "f30f1efa554889e548c7058f7af40400000000488d05707af404"
    "4889c6bf01000000e87351feffc6056c7af40401905dc3"
))


def _emit_instructions(insns, body, bias, code, relocations):
    for insn in insns:
        off = len(code)
        code.extend(insn.raw)
        target = insn.branch_target if insn.is_call else insn.rip_target
        if target is None:
            _require(not insn.is_call and insn.branch_target is None,
                     "frame_image_unverified", "unsupported control transfer in audited frame")
            continue
        disp_off = insn.imm_offset if insn.is_call else insn.disp_offset
        width = insn.imm_size if insn.is_call else insn.disp_size
        _require(width == 4 and not body.address <= target < body.address + len(body.code)
                 and 0 < bias + target < (1 << 64),
                 "frame_image_unverified", "expected an external signed rel32 reference")
        field = off + disp_off
        code[field:field + 4] = bytes(4)
        relocations.append(NativeRelocation(field, len(code), bias + target))


def _emit_frames(bodies, bias, code, relocations):
    """Emit the prefix of each expanded call, then its continuations inside out."""
    continuations = []
    for n, body in enumerate(bodies):
        insns = sweep(body.code, body.address)
        _require(bool(insns) and tiles(insns, body.address, body.address + len(body.code))
                 and insns[-1].raw == b"\xc3" and not any(i.is_ret or i.is_jump for i in insns[:-1]),
                 "frame_image_unverified", "audited frame must have one fall-through path to RET")
        ops = insns[1 if insns[0].mnemonic == "endbr64" else 0:-1]
        code.extend(_RESERVE)
        suffix = ()
        if n + 1 < len(bodies):
            child = bodies[n + 1].address
            calls = [i for i, op in enumerate(ops) if op.is_call and op.branch_target == child]
            _require(len(calls) == 1, "frame_image_unverified", "expected one direct call to child")
            call = calls[0]
            ops, suffix = ops[:call], ops[call + 1:]
        _emit_instructions(ops, body, bias, code, relocations)
        continuations.append((body, suffix))
    for body, suffix in reversed(continuations):
        _emit_instructions(suffix, body, bias, code, relocations)
        code.extend(_RELEASE)


def _build_frame(request: InlineCall, max_depth: int = 1, compact_frame: bool = False,
                 frame_chain: tuple[NativeFrameBody, ...] = ()) -> NativeSplice:
    _require(type(max_depth) is int and max_depth > 0, "frame_depth", "expected a positive integer depth")
    if legacy_frame_image(request.image_id):
        if request.symbol != "benchmark_start":
            raise NativeInlineError("frame_virtual_unsupported",
                                    f"no closed normal-return audit for virtual {request.symbol}")
        body = (_CANARY_ENTRY if request.image_id == _CANARY_IMAGE_ID
                else NativeFrameBody(_BENCHMARK_START, _BENCHMARK_START_CODE))
        _require(not frame_chain and request.elf_address == body.address and
                 request.image_offset == body.address and request.callee_code == body.code,
                 "frame_image_unverified", "virtual differs from the audited FastDyn ELF and body")
        nested = (_CANARY_WORKER,) if request.image_id == _CANARY_IMAGE_ID else ()
    else:
        body = NativeFrameBody(request.elf_address, request.callee_code)
        _require(is_sha256_id(request.image_id) and type(body.address) is int and
                 0 < body.address < (1 << 64) and type(request.image_offset) is int and
                 request.image_offset >= 0 and valid_chain(body, frame_chain),
                 "frame_image_unverified", "expected a complete capture-bound chain of normal-return frames")
        nested = frame_chain
    _require(type(request.tcg_typemask) is int and request.tcg_typemask == _PLUGIN_CALLBACK_TYPEMASK,
             "frame_abi", "expected a captured void(u32, ptr) plugin callback")
    _require(request.pool_slot_off is None and len(request.call_bytes) == 5 and
             type(request.call_off) is int and type(request.continuation_off) is int and
             request.call_bytes[:1] == b"\xe8" and request.call_off >= 0 and
             request.continuation_off == request.call_off + 5,
             "frame_origin", "expected one direct rel32 captured CALL")
    _require(type(request.target_abs) is int, "frame_image_unverified",
             "invalid plugin runtime entry address")
    bias = request.target_abs - request.elf_address
    _require(0 < bias < (1 << 64) and bias % 4096 == 0,
             "frame_image_unverified", "invalid plugin runtime load address")
    _require(not request.fragment.code and not request.fragment.origins and
             not request.fragment.continuation_fields,
             "frame_origin", "framed virtual proof requires a direct audited source body")

    code = bytearray()
    relocations: list[NativeRelocation] = []
    depth = min(max_depth, 1 + len(nested))
    _emit_frames((body, *nested[:depth - 1]), bias, code, relocations)
    if compact_frame:
        from .frame_cleanup import cleanup_frame, verify_cleanup

        original, original_relocs = bytes(code), relocations
        code, relocations = cleanup_frame(original, original_relocs)
        verify_cleanup(original, original_relocs, code, relocations)
    padding = (len(request.call_bytes) - len(code)) % 8
    return NativeSplice(start_off=request.call_off, prefix=b"", code=bytes(code) + _PADDING[padding],
                        kind="frame", relocations=tuple(relocations), depth=depth,
                        compact_frame=compact_frame, frame_chain=frame_chain)


def _verify_frame_site(unit: TBUnit, request: InlineCall, code_address: int) -> None:
    start, end = request.call_off, request.continuation_off
    _require(unit.uid == request.target and unit.content_hash == request.unit_hash and
             unit.code[start:end] == request.call_bytes and is_sha256_id(request.native_input_id) and
             unit.meta_status == "ok" and unit.reloc.relocatable and 0 <= start < end < unit.ldst_end,
             "stale_inline_call", "virtual call is not bound to the current unit")
    sites = [s for s in unit.sites if s.kind is SiteKind.HELPER_CALL and s.insn_off == start]
    bindings = [h for h in unit.bindings.helpers if h.id == request.helper_id]
    _require(len(sites) == 1 and len(bindings) == 1,
             "frame_origin", "expected one matching capture site and one helper binding")
    site, binding = sites[0], bindings[0]
    _require(site.enc is Encoding.REL32 and site.field_off == start + 1 and site.width == 4 and
             site.final_field == int.from_bytes(request.call_bytes[1:], "little") and
             site.aux == request.helper_id and site.value == request.target_abs and
             binding.name is None and binding.flags == 0 and binding.module == "libfastdyn.so" and
             binding.symbol == request.symbol and binding.func == request.target_abs,
             "frame_origin", "capture does not bind the exact FastDyn virtual and direct CALL")
    call_fixups = [f for f in unit.reloc.fixups if f.field_off == site.field_off]
    _require(len(call_fixups) == 1 and call_fixups[0].width == 4 and
             call_fixups[0].site_kind == int(SiteKind.HELPER_CALL) and
             call_fixups[0].rule == "rel_minus_delta" and
             call_fixups[0].target_class == "ext.plugin" and
             call_fixups[0].target_abs == request.target_abs,
             "frame_origin", "caller has no exact plugin-call relocation")
    _require(site.insn_idx is not None and any(
        i.index == site.insn_idx and i.host_start <= start < end <= i.host_end
        for i in unit.insns
    ) and any(
        mark.kind is OpMarkKind.CALL and mark.helper_id == request.helper_id and
        mark.insn_idx == site.insn_idx and mark.host_off <= start
        for mark in unit.opmarks
    ), "frame_origin", "expected a guest instruction and CALL opmark for the callback")
    _require(not any(start < x < end for i in unit.insns for x in (i.host_start, i.host_end))
             and not any(start < p.host_return_offset < end for p in unit.recovery)
             and not any(start < mark.host_off < end for mark in unit.opmarks),
             "frame_origin", "the framed span contains an unrelated guest or recovery boundary")

    base = code_address + unit.tc_offset
    code_end = unit.pool_start if unit.pool_start is not None else unit.ldst_end
    callers = sweep(unit.code[:code_end], base)
    _require(tiles(callers, base, base + code_end), "unsupported_caller", "caller does not tile")
    pc = base + start
    call = next((i for i in callers if i.address == pc), None)
    _require(call is not None and call.is_call and call.end == base + end and
             call.branch_target == request.target_abs,
             "call_target_mismatch", "machine CALL differs from the captured virtual")
    _require(not any(_references_splice(i, pc, base + end) for i in callers)
             and not any(f.target_abs is not None and (pc < f.target_abs < base + end or
                        f.rule == "abs_plus_delta" and f.target_abs == pc) for f in unit.reloc.fixups),
             "frame_incoming_reference", "an existing reference observes the framed span")
    _require(not any(
        s != site and (start <= s.insn_off < end or
                       start < s.field_off + s.width and s.field_off < end)
        for s in unit.sites
    ) and not any(
        start < f.field_off + f.width and f.field_off < end and
        (f.field_off, f.width, f.site_kind) != (site.field_off, site.width, int(site.kind))
        for f in unit.reloc.fixups
    ), "frame_origin", "unrelated relocation or site overlaps the framed span")

    # QEMU reserves RSP for the TCG spill frame; reject evidence of paths
    # that change its phase, expose an address below RSP, or observe it.
    for insn in decoder().disasm(unit.code[:code_end], base):
        if insn.group(capstone.CS_GRP_CALL):
            continue
        reads, writes = insn.regs_access()
        _require(not insn.group(capstone.CS_GRP_RET) and
                 not any(_stack_reg(insn.reg_name(reg)) for reg in writes),
                 "unsupported_stack", "caller changes RSP outside the admitted QEMU CALL ABI")
        if any(_stack_reg(insn.reg_name(reg)) for reg in reads):
            accesses = [op for op in insn.operands if op.type == X.X86_OP_MEM and
                        _stack_reg(insn.reg_name(op.mem.base))]
            _require(bool(accesses) and insn.mnemonic != "lea" and all(
                op.mem.disp >= 0 and not op.mem.index and not op.mem.segment for op in accesses
            ) and not any(op.type == X.X86_OP_REG and _stack_reg(insn.reg_name(op.reg))
                          for op in insn.operands),
                "unsupported_stack", "caller can observe or expose the outer return slot")


def prepare_virtual_splice(unit: TBUnit, request: InlineCall, code_address: int, *,
                           max_depth: int = 1, compact_frame: bool = False,
                           frame_chain: tuple[NativeFrameBody, ...] = ()) -> NativeSplice:
    """Certify one framed virtual's exact original caller and byte translation."""
    splice = _build_frame(request, max_depth, compact_frame, frame_chain)
    _verify_frame_site(unit, request, code_address)
    return splice


def verify_virtual_splice_body(request: InlineCall) -> None:
    """Verify the emitted placeholder bytes and complete relocation list."""
    splice = request.splice
    _require(splice is not None and splice == _build_frame(
        request, splice.depth, splice.compact_frame, splice.frame_chain),
             "frame_correspondence", "exact framed body, padding, or relocation differs")


def verify_virtual_splice(unit: TBUnit, request: InlineCall, code_address: int) -> None:
    """Verify correspondence with the original caller and frozen plugin body."""
    splice = request.splice
    _require(splice is not None and splice == prepare_virtual_splice(
        unit, request, code_address, max_depth=splice.depth, compact_frame=splice.compact_frame,
        frame_chain=splice.frame_chain),
             "frame_correspondence", "framed splice differs from the original caller proof")
