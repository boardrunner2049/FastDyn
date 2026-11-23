"""Trusted, byte-exact transplantation of a captured QEMU helper graph.

This deliberately aggressive policy copies each exactly bounded QEMU function
once per call site and retargets every direct edge whose destination is another
copied body.  Cycles therefore remain cycles instead of being expanded by a
depth counter.  Indirect edges and direct edges outside the captured QEMU image
remain ordinary external transfers and are reported by the pass receipt.

Replacing the outer CALL with LEA/PUSH plus the copied root preserves the
ordinary SysV return-stack shape without a depth limit.  Configuration 7's
trusted contract excludes CET shadow-stack enforcement, asynchronous unwind,
and live fault recovery for these private copies.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import replace
from functools import lru_cache

from keystone import KS_ARCH_X86, KS_MODE_64, Ks, KsError

from ..analysis.hostops import reg_family
from ..decode.x86 import decoder, sweep, tiles
from ..model.native import (HELPER_GRAPH_KIND, InlineCall, NativeFrameBody,
                            NativeSplice)
from ..model.unit import TBUnit
from .native_inline import NativeInlineError, _require, _verify_bound_site
from .native_splice import _PADDING, _verify_span

_LEA_R11_RIP = bytes.fromhex("4c8d1d")
_PUSH_R11 = bytes.fromhex("4153")
_SCRATCH = ("r11", "r10", "r9", "r8", "rcx", "rdx", "rsi", "rdi",
            "rax", "rbx", "r12", "r13", "r14", "r15", "rbp")
_RIP_OPERAND = re.compile(r"\[[^\]]*\brip\b[^\]]*\]")
_ASSEMBLER = Ks(KS_ARCH_X86, KS_MODE_64)


def _signed(value: int, width: int) -> bytes:
    try:
        return value.to_bytes(width, "little", signed=True)
    except OverflowError as exc:
        raise NativeInlineError("helper_graph_reference_range",
                                f"displacement {value:#x} does not fit in {width} bytes") from exc


def _asm(text: str) -> bytes:
    try:
        encoded, _ = _ASSEMBLER.asm(text)
    except KsError as exc:
        raise NativeInlineError("helper_graph_encode", f"cannot encode {text!r}: {exc}") from exc
    _require(bool(encoded), "helper_graph_encode", f"empty encoding for {text!r}")
    return bytes(encoded)


def _scratch(insn) -> str:
    decoded = tuple(decoder().disasm(insn.raw, insn.address))
    _require(len(decoded) == 1, "helper_graph_decode", "source instruction differs in detailed decode")
    source = decoded[0]
    reads, writes = source.regs_access()
    used = set()
    for reg in (*reads, *writes):
        family = reg_family(source.reg_name(reg))
        if family is not None:
            used.add(family[0])
    _require("rsp" not in used, "helper_graph_stack_operand",
             f"cannot bracket an RIP rewrite that observes RSP: {insn.text()}")
    scratch = next((name for name in _SCRATCH if name not in used), None)
    _require(scratch is not None, "helper_graph_scratch", f"no scratch register for {insn.text()}")
    return scratch


def _absolute_rip(insn, target_abs: int) -> bytes:
    """Rewrite one out-of-arena RIP reference without a rel32 dependency."""
    if insn.is_lea:
        _require(insn.dst_reg is not None and insn.op_sizes[:1] == (8,),
                 "helper_graph_rip_operand", f"unsupported LEA destination in {insn.text()}")
        return _asm(f"mov {insn.dst_reg}, {target_abs:#x}")
    scratch = _scratch(insn)
    if insn.is_jump:
        _require(insn.rip_mem is not None and insn.rip_mem.size == 8,
                 "helper_graph_rip_operand", "indirect tail jump is not a qword pointer")
        # Restore the scratch register and RSP while transferring to the loaded
        # target. PUSH/MOV/XCHG/RET do not modify arithmetic flags.
        return (_asm(f"push {scratch}") + _asm(f"mov {scratch}, {target_abs:#x}")
                + _asm(f"mov {scratch}, qword ptr [{scratch}]")
                + _asm(f"xchg {scratch}, qword ptr [rsp]") + b"\xc3")
    replaced, count = _RIP_OPERAND.subn(f"[{scratch}]", insn.op_str)
    _require(count == 1, "helper_graph_rip_operand",
             f"expected one RIP operand in {insn.text()}")
    operation = f"{insn.mnemonic} {replaced}"
    return (_asm(f"push {scratch}") + _asm(f"mov {scratch}, {target_abs:#x}")
            + _asm(operation) + _asm(f"pop {scratch}"))


def _direct_template(insn) -> bytes:
    if insn.is_call:
        _require(len(insn.raw) == 5 and insn.raw[:1] == b"\xe8",
                 "helper_graph_branch_encoding", "expected a direct near CALL")
        return b"\xe8" + bytes(4)
    if insn.mnemonic == "jmp":
        _require((len(insn.raw) == 2 and insn.raw[:1] == b"\xeb")
                 or (len(insn.raw) == 5 and insn.raw[:1] == b"\xe9"),
                 "helper_graph_branch_encoding", "expected a direct relative JMP")
        return b"\xe9" + bytes(4)
    raw = insn.raw
    if len(raw) == 2 and 0x70 <= raw[0] <= 0x7f:
        condition = raw[0] & 15
    elif len(raw) == 6 and raw[:1] == b"\x0f" and 0x80 <= raw[1] <= 0x8f:
        condition = raw[1] & 15
    else:
        raise NativeInlineError("helper_graph_branch_encoding",
                                f"cannot widen direct {insn.text()} at {insn.address:#x}")
    return bytes((0x0f, 0x80 | condition)) + bytes(4)


@lru_cache(maxsize=64)
def _build_code(elf_address: int, callee_code: bytes, target_abs: int,
                call_size: int, graph: tuple[NativeFrameBody, ...]) -> bytes:
    decoded = {}
    ranges = []
    for body in graph:
        insns = sweep(body.code, body.address)
        _require(bool(insns) and tiles(insns, body.address, body.address + len(body.code)),
                 "helper_graph_decode", f"function {body.address:#x} does not tile")
        decoded[body.address] = insns
        ranges.append((body.address, body.address + len(body.code), body))
    ranges.sort()
    _require(all(a1 <= a2 for (_, a1, _), (a2, _, _) in zip(ranges, ranges[1:])),
             "helper_graph_origin", "helper function ranges overlap")

    # Root first makes the removed CALL fall directly into its body. The rest
    # are deterministic by source address; recursive edges do not clone again.
    ordered = (graph[0], *sorted(graph[1:], key=lambda body: body.address))
    prefix_len = len(_LEA_R11_RIP) + 4 + len(_PUSH_R11)
    source_to_output = {}
    plans = {}
    external_direct = set()
    bias = target_abs - elf_address
    _require(0 < bias < (1 << 64), "helper_graph_origin", "invalid QEMU load bias")

    range_starts = [lo for lo, _, _ in ranges]

    def source_range(target: int):
        index = bisect.bisect_right(range_starts, target) - 1
        if index >= 0:
            lo, hi, body = ranges[index]
            if target < hi:
                return lo, hi, body
        return None

    for body in ordered:
        for insn in decoded[body.address]:
            if insn.branch_target is not None:
                plans[insn.address] = ("direct", _direct_template(insn), insn.branch_target)
                if source_range(insn.branch_target) is None:
                    external_direct.add(insn.branch_target)
            elif insn.rip_target is not None:
                if source_range(insn.rip_target) is not None:
                    plans[insn.address] = ("rip-internal", insn.raw, insn.rip_target)
                else:
                    plans[insn.address] = ("bytes", _absolute_rip(insn, bias + insn.rip_target), None)
            else:
                plans[insn.address] = ("bytes", insn.raw, None)

    cursor = prefix_len
    for body in ordered:
        for insn in decoded[body.address]:
            source_to_output[insn.address] = cursor
            cursor += len(plans[insn.address][1])
    stubs = {}
    stub_bytes = _asm("mov r11, 0x1122334455667788") + _asm("jmp r11")
    for target in sorted(external_direct):
        stubs[target] = cursor
        cursor += len(stub_bytes)

    code = bytearray(_LEA_R11_RIP + bytes(4) + _PUSH_R11)
    for body in ordered:
        for insn in decoded[body.address]:
            kind, encoded, _ = plans[insn.address]
            _require(len(code) == source_to_output[insn.address],
                     "helper_graph_correspondence", "instruction layout is inconsistent")
            code.extend(encoded)
    for target in sorted(external_direct):
        _require(len(code) == stubs[target], "helper_graph_correspondence",
                 "external thunk layout is inconsistent")
        code.extend(_asm(f"mov r11, {bias + target:#x}"))
        code.extend(_asm("jmp r11"))

    for body in ordered:
        for insn in decoded[body.address]:
            kind, _, target = plans[insn.address]
            off = source_to_output[insn.address]
            if kind == "direct":
                destination = source_to_output.get(target, stubs.get(target))
                _require(destination is not None, "helper_graph_control_target",
                         f"missing copied target {target:#x}")
                field = off + (1 if code[off] in (0xe8, 0xe9) else 2)
                size = 5 if code[off] in (0xe8, 0xe9) else 6
                code[field:field + 4] = _signed(destination - (off + size), 4)
            elif kind == "rip-internal":
                destination = source_to_output.get(target)
                _require(destination is not None, "helper_graph_rip_target",
                         f"internal RIP target {target:#x} is not an instruction boundary")
                # x86-64 RIP displacements are always disp32. Capstone 5.0.7
                # incorrectly reports disp16 for 0x66-prefixed cmp/test forms.
                field = off + insn.disp_offset
                _require(field + 4 <= off + insn.size - insn.imm_size,
                         "helper_graph_reference_width", "invalid internal RIP displacement field")
                code[field:field + 4] = _signed(destination - (off + insn.size), 4)
    padding = (call_size - len(code)) % 8
    code.extend(_PADDING[padding])
    continuation = len(code)
    code[3:7] = _signed(continuation - 7, 4)

    return bytes(code)


def _build(request: InlineCall, graph: tuple[NativeFrameBody, ...]) -> NativeSplice:
    _require(bool(graph) and graph[0] == NativeFrameBody(request.elf_address,
                                                        request.callee_code),
             "helper_graph_origin", "graph root differs from the captured helper")
    _require(len({body.address for body in graph}) == len(graph),
             "helper_graph_origin", "helper graph contains duplicate function entries")
    _require(request.continuation_off == request.call_off + len(request.call_bytes)
             and len(request.call_bytes) in (5, 6),
             "helper_graph_origin", "expected one captured x86-64 helper CALL")
    code = _build_code(request.elf_address, request.callee_code, request.target_abs,
                       len(request.call_bytes), graph)

    return NativeSplice(request.call_off, b"", code, HELPER_GRAPH_KIND,
                        depth=0, frame_chain=graph[1:])


def prepare_helper_graph_splice(unit: TBUnit, request: InlineCall, code_address: int,
                                graph: tuple[NativeFrameBody, ...]) -> NativeSplice:
    _verify_bound_site(unit, request, code_address, required_flags=None, allow_ldst=True)
    base = code_address + unit.tc_offset
    from ..decode.x86 import sweep

    code_end = unit.pool_start if unit.pool_start is not None else unit.ldst_end
    callers = sweep(unit.code[:code_end], base)
    splice = _build(request, graph)
    _verify_span(unit, request, splice, base, callers, allow_ldst=True)
    return splice


def verify_helper_graph_splice_body(request: InlineCall) -> None:
    splice = request.splice
    _require(splice is not None and splice.kind == HELPER_GRAPH_KIND,
             "helper_graph_correspondence", "trusted helper graph record required")
    expected = _build(request, (NativeFrameBody(request.elf_address, request.callee_code),
                                *splice.frame_chain))
    _require(expected == replace(splice, branches=()), "helper_graph_correspondence",
             "helper graph bytes, source bodies, or relocations differ")


def verify_helper_graph_splice(unit: TBUnit, request: InlineCall, code_address: int) -> None:
    splice = request.splice
    _require(splice is not None and splice.kind == HELPER_GRAPH_KIND,
             "helper_graph_correspondence", "trusted helper graph record required")
    expected = prepare_helper_graph_splice(
        unit, request, code_address,
        (NativeFrameBody(request.elf_address, request.callee_code), *splice.frame_chain))
    _require(expected == replace(splice, branches=()), "helper_graph_correspondence",
             "helper graph differs from the frozen source and original call site")
