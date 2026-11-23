"""Trusted, byte-exact transplantation of a captured QEMU helper graph.

This deliberately aggressive policy copies each exactly bounded QEMU function
once per call site and retargets every direct edge whose destination is another
copied body.  Cycles therefore remain cycles instead of being expanded by a
depth counter.  Indirect edges and direct edges outside the captured QEMU image
remain ordinary external transfers and are reported by the pass receipt.

A stack-independent leaf root uses the smaller true fallthrough form: the CALL
and final RET are both removed, earlier RETs become jumps to the continuation,
and internal branches are relocated.  Every other graph replaces the outer
CALL with LEA/PUSH plus the copied root, preserving the ordinary SysV
return-stack shape without a depth limit.  Configuration 7's trusted contract
excludes CET shadow-stack enforcement, asynchronous unwind, and live fault
recovery for these private copies.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import replace
from functools import lru_cache

from keystone import KS_ARCH_X86, KS_MODE_64, Ks, KsError

from ..analysis.hostops import reg_family
from ..decode.x86 import decoder, sweep, tiles
from ..model.native import (HELPER_GRAPH_KIND, HelperCallLowering, InlineCall, NativeFrameBody,
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
_ENDBR64 = bytes.fromhex("f30f1efa")
_SUB_RSP_8 = bytes.fromhex("4883ec08")
_ADD_RSP_8 = bytes.fromhex("4883c408")
_PAST_RED_ZONE = bytes.fromhex("488d642480")       # lea rsp,[rsp-128]
_RESTORE_RED_ZONE = bytes.fromhex("488da42480000000")  # lea rsp,[rsp+128]
# Indirect transfers use a literal embedded in an unreachable MOVABS so the
# complete host span still tiles as instructions. Neither transfer clobbers a
# register. The absolute target always occupies precisely eight bytes.
_ABS_JUMP = bytes.fromhex("ff2502000000 49bb")
_ABS_CALL = bytes.fromhex("eb0a 49bb")
_CALL_LITERAL = bytes.fromhex("ff15f2ffffff")


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
        # Save below the entire SysV red zone. RET 128 consumes the target and
        # releases the reservation as a single instruction, with flags intact.
        return (_PAST_RED_ZONE + _asm(f"push {scratch}") + _asm(f"mov {scratch}, {target_abs:#x}")
                + _asm(f"mov {scratch}, qword ptr [{scratch}]")
                + _asm(f"xchg {scratch}, qword ptr [rsp]") + b"\xc2\x80\x00")
    replaced, count = _RIP_OPERAND.subn(f"[{scratch}]", insn.op_str)
    _require(count == 1, "helper_graph_rip_operand",
             f"expected one RIP operand in {insn.text()}")
    operation = f"{insn.mnemonic} {replaced}"
    return (_PAST_RED_ZONE + _asm(f"push {scratch}") + _asm(f"mov {scratch}, {target_abs:#x}")
            + _asm(operation) + _asm(f"pop {scratch}") + _RESTORE_RED_ZONE)


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


def _observes_rsp(insn) -> bool:
    decoded = tuple(decoder().disasm(insn.raw, insn.address))
    if len(decoded) != 1:
        return True
    reads, writes = decoded[0].regs_access()
    return any((family := reg_family(decoded[0].reg_name(reg))) is not None
               and family[0] == "rsp" for reg in (*reads, *writes))


@lru_cache(maxsize=256)
def _root_wrapper_plan(elf_address: int, code: bytes):
    """Recognize QEMU's tiny ABI wrappers without following their callees.

    Two compiler-emitted shapes are admitted.  A tail wrapper performs only
    register preparation followed by one external direct JMP.  A call wrapper
    has the exact SUB RSP,8 / direct CALL / ADD RSP,8 alignment pair and one
    final RET. The call-wrapper form uses the ordinary graph emitter so that
    the callee sees its actual incoming stack arguments and flags.
    """
    insns = sweep(code, elf_address)
    if not insns or not tiles(insns, elf_address, elf_address + len(code)):
        return None
    meaningful = [insn for insn in insns
                  if insn.raw != _ENDBR64 and insn.mnemonic != "nop"]
    if not meaningful:
        return None
    # A wrapper transfers to a separate body. Keep edges into the root in
    # the normal graph linker, including recursive edges. Capstone exposes
    # some relative opcodes (e.g. LOOP) outside the jump group.
    for insn in meaningful:
        if insn.branch_target is not None and (
                elf_address <= insn.branch_target < elf_address + len(code)
                or not (insn.is_call or insn.is_jump)):
            return None

    last = meaningful[-1]
    if (last.mnemonic == "jmp" and last.branch_target is not None
            and not any(insn.is_call or insn.is_ret or insn.is_jump
                        for insn in meaningful[:-1])
            and not any(_observes_rsp(insn) for insn in meaningful[:-1])):
        return "tail", tuple(meaningful[:-1]), last.branch_target, None

    calls = [insn for insn in meaningful if insn.is_call]
    if (meaningful[0].raw != _SUB_RSP_8 or last.raw != b"\xc3"
            or len(calls) != 1 or calls[0].branch_target is None):
        return None
    call_index = meaningful.index(calls[0])
    if (call_index + 1 >= len(meaningful)
            or meaningful[call_index + 1].raw != _ADD_RSP_8):
        return None
    control = [insn for insn in meaningful
               if insn.is_call or insn.is_jump or insn.is_ret]
    if control != [calls[0], last]:
        return None
    removed = {meaningful[0].address, meaningful[call_index + 1].address,
               last.address}
    if any(_observes_rsp(insn) for insn in meaningful
           if insn.address not in removed and insn is not calls[0]):
        return None
    return "call", (), calls[0].branch_target, calls[0].address


def root_wrapper_refusal(elf_address: int, code: bytes) -> tuple[str, str] | None:
    """Explain whether the root-only runtime-profitability policy can apply."""
    if _root_wrapper_plan(elf_address, code) is None:
        return ("not_root_wrapper",
                "root is not a stack-independent tail wrapper or exact aligned call wrapper")
    return None


def _root_wrapper_fallthrough_code(elf_address: int, target_abs: int,
                                   call_size: int,
                                   graph: tuple[NativeFrameBody, ...]) -> bytes | None:
    if len(graph) != 1:
        return None
    plan = _root_wrapper_plan(elf_address, graph[0].code)
    if plan is None:
        return None
    kind, body, callee_elf, _ = plan
    if kind != "tail":
        # Removing the alignment pair changes the inner callee's stack by
        # 16 bytes, even when its flags are dead inside the wrapper.
        return None
    code = bytearray()
    for insn in body:
        if insn.rip_target is not None:
            code.extend(_absolute_rip(insn, target_abs + insn.rip_target - elf_address))
        else:
            code.extend(insn.raw)
    target = target_abs + callee_elf - elf_address
    code.extend(_ABS_CALL + target.to_bytes(8, "little") + _CALL_LITERAL)
    padding = (call_size - len(code)) % 8
    code.extend(_PADDING[padding])
    return bytes(code)


@lru_cache(maxsize=256)
def _leaf_fallthrough_code(call_size: int,
                           graph: tuple[NativeFrameBody, ...]) -> bytes | None:
    """Return a stack-neutral trusted leaf, or ``None`` for graph lowering.

    Ordinary environment loads/stores are allowed by the user-vouched helper
    contract.  What matters for deleting the CALL's return slot is that no
    non-RET instruction can observe RSP, no nested call exists, and control is
    a closed direct CFG.  Early RETs become direct jumps; the last reachable
    plain RET is the physical final instruction and becomes fallthrough.
    """
    if len(graph) != 1:
        return None
    return _closed_fallthrough_code(call_size, graph[0], {})


def _closed_fallthrough_code(call_size: int | None, body: NativeFrameBody,
                             substitutions: dict[int, bytes]) -> bytes | None:
    """Emit a closed body with proved stack-independent replacements for direct calls."""
    instructions = sweep(body.code, body.address)
    if (not instructions or not tiles(instructions, body.address, body.address + len(body.code))
            or instructions[-1].raw != b"\xc3"):
        return None
    by_address = {insn.address: insn for insn in instructions}
    for insn in instructions:
        if insn.is_call:
            if insn.raw[:1] != b"\xe8" or len(insn.raw) != 5 or insn.branch_target not in substitutions:
                return None
            continue
        if insn.rip_target is not None or (insn.branch_target is not None and not insn.is_jump):
            return None
        if insn.is_ret:
            if insn.raw != b"\xc3":
                return None
            continue
        decoded = tuple(decoder().disasm(insn.raw, insn.address))
        if len(decoded) != 1:
            return None
        reads, writes = decoded[0].regs_access()
        if any((reg_family(decoded[0].reg_name(reg)) or (None,))[0] == "rsp"
               for reg in (*reads, *writes)):
            return None
        if insn.is_jump:
            if insn.branch_target not in by_address:
                return None
            try:
                _direct_template(insn)
            except NativeInlineError:
                return None

    # Prove the direct CFG is closed. Compilers may leave alignment NOPs after
    # an early RET and branch over them to another return block; those bytes
    # are harmless to retain but no other unreachable instruction is admitted.
    reached = set()
    pending = [instructions[0].address]
    while pending:
        address = pending.pop()
        if address in reached:
            continue
        insn = by_address.get(address)
        if insn is None:
            return None
        reached.add(address)
        if insn.is_ret:
            continue
        if insn.is_jump:
            pending.append(insn.branch_target)
            if insn.mnemonic != "jmp":
                pending.append(insn.end)
        else:
            pending.append(insn.end)
    unreachable = set(by_address) - reached
    if (instructions[-1].address not in reached
            or any(by_address[address].mnemonic != "nop" for address in unreachable)):
        return None

    final_ret = instructions[-1].address
    plans = {}
    source_to_output = {}
    cursor = 0
    for insn in instructions:
        source_to_output[insn.address] = cursor
        if insn.is_ret:
            encoded = b"" if insn.address == final_ret else b"\xe9" + bytes(4)
            plans[insn.address] = ("return", encoded, None)
        elif insn.is_jump:
            plans[insn.address] = ("direct", _direct_template(insn), insn.branch_target)
        elif insn.is_call:
            plans[insn.address] = ("bytes", substitutions[insn.branch_target], None)
        else:
            plans[insn.address] = ("bytes", insn.raw, None)
        cursor += len(plans[insn.address][1])

    padding = 0 if call_size is None else (call_size - cursor) % 8
    continuation = cursor + padding
    code = bytearray()
    for insn in instructions:
        kind, encoded, target = plans[insn.address]
        off = len(code)
        code.extend(encoded)
        if kind == "return" and encoded:
            code[off + 1:off + 5] = _signed(continuation - (off + 5), 4)
        elif kind == "direct":
            destination = source_to_output[target]
            field = off + (1 if code[off] == 0xe9 else 2)
            size = 5 if code[off] == 0xe9 else 6
            code[field:field + 4] = _signed(destination - (off + size), 4)
    code.extend(_PADDING[padding])
    return bytes(code)


def _build_code(elf_address: int, callee_code: bytes, target_abs: int,
                call_size: int, graph: tuple[NativeFrameBody, ...]) -> bytes:
    return _compile_graph(elf_address, target_abs, call_size, graph)[0]


@lru_cache(maxsize=64)
def _compile_graph(elf_address: int, target_abs: int,
                   call_size: int, graph: tuple[NativeFrameBody, ...]):
    wrapper = _root_wrapper_fallthrough_code(elf_address, target_abs, call_size, graph)
    if wrapper is not None:
        tail = _root_wrapper_plan(elf_address, graph[0].code)[0] == "tail"
        return wrapper, HelperCallLowering("shared-wrapper", 1, 0, int(not tail), int(tail))
    leaf = _leaf_fallthrough_code(call_size, graph)
    if leaf is not None:
        return leaf, HelperCallLowering("fallthrough", 1, 0, 0)
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
    for target in sorted(external_direct):
        stubs[target] = cursor
        cursor += len(_ABS_JUMP) + 8

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
        code.extend(_ABS_JUMP + (bias + target).to_bytes(8, "little"))

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

    retained = sum(insn.is_call for insns in decoded.values() for insn in insns)
    return bytes(code), HelperCallLowering("synthetic-return", len(graph), 0, retained)


def _build(request: InlineCall, graph: tuple[NativeFrameBody, ...], *,
           selected_root: bool = False) -> NativeSplice:
    _require(bool(graph) and graph[0] == NativeFrameBody(request.elf_address,
                                                        request.callee_code),
             "helper_graph_origin", "graph root differs from the captured helper")
    _require(len({body.address for body in graph}) == len(graph),
             "helper_graph_origin", "helper graph contains duplicate function entries")
    _require(request.continuation_off == request.call_off + len(request.call_bytes)
             and len(request.call_bytes) in (5, 6),
             "helper_graph_origin", "expected one captured x86-64 helper CALL")
    if selected_root:
        from .selected_helper import lower_selected_root

        code, children, lowering = lower_selected_root(graph[0], graph[1:], request.target_abs,
                                                       len(request.call_bytes))
    else:
        code, lowering = _compile_graph(request.elf_address, request.target_abs,
                                        len(request.call_bytes), graph)
        children = graph[1:]

    return NativeSplice(request.call_off, b"", code, HELPER_GRAPH_KIND,
                        depth=0, frame_chain=children, selected_root=selected_root,
                        call_lowering=lowering)


def prepare_helper_graph_splice(unit: TBUnit, request: InlineCall, code_address: int,
                                graph: tuple[NativeFrameBody, ...], *,
                                selected_root: bool = False) -> NativeSplice:
    _verify_bound_site(unit, request, code_address, required_flags=None, allow_ldst=True)
    base = code_address + unit.tc_offset
    from ..decode.x86 import sweep

    code_end = unit.pool_start if unit.pool_start is not None else unit.ldst_end
    callers = sweep(unit.code[:code_end], base)
    splice = _build(request, graph, selected_root=selected_root)
    _verify_span(unit, request, splice, base, callers, allow_ldst=True)
    return splice


def verify_helper_graph_splice_body(request: InlineCall) -> None:
    splice = request.splice
    _require(splice is not None and splice.kind == HELPER_GRAPH_KIND,
             "helper_graph_correspondence", "trusted helper graph record required")
    expected = _build(request, (NativeFrameBody(request.elf_address, request.callee_code),
                                *splice.frame_chain), selected_root=splice.selected_root)
    _require(expected == replace(splice, branches=()), "helper_graph_correspondence",
             "helper graph bytes, source bodies, or relocations differ")


def verify_helper_graph_splice(unit: TBUnit, request: InlineCall, code_address: int) -> None:
    splice = request.splice
    _require(splice is not None and splice.kind == HELPER_GRAPH_KIND,
             "helper_graph_correspondence", "trusted helper graph record required")
    expected = prepare_helper_graph_splice(
        unit, request, code_address,
        (NativeFrameBody(request.elf_address, request.callee_code), *splice.frame_chain),
        selected_root=splice.selected_root)
    _require(expected == replace(splice, branches=()), "helper_graph_correspondence",
             "helper graph differs from the frozen source and original call site")
