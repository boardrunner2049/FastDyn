"""Independent x86-64 byte correspondence for register-only native islands."""

from __future__ import annotations

from collections import deque
from typing import Any

import capstone
from capstone import x86 as X

from ..analysis.hostops import reg_family
from ..canon import is_sha256_id
from ..decode.x86 import decoder, sweep, tiles
from ..formats.enums import Encoding, SiteKind
from ..model.native import InlineCall
from ..model.unit import TBUnit


class NativeInlineError(ValueError):
    def __init__(self, reason: str, detail: str):
        self.reason = reason
        super().__init__(detail)


# These x86-64 baseline instructions have no implicit memory or control effects.
# Exact bytes are required for non-branches, preserving even undefined flags.
_REGISTER_MNEMONICS = frozenset((
    "add", "adc", "and", "bswap", "cmp", "dec", "inc", "lea", "mov", "movabs",
    "movsx", "movsxd", "movzx", "neg", "not", "or", "rol", "ror", "sar", "sbb",
    "shl", "shr", "sub", "test", "xor",
))
_CONDS = frozenset(("a", "ae", "b", "be", "e", "g", "ge", "l", "le", "ne", "no",
                    "np", "ns", "o", "p", "s"))
_JUMPS = {"j" + c for c in _CONDS} | {"jmp"}
_ALU = _REGISTER_MNEMONICS | {p + c for p in ("cmov", "set") for c in _CONDS}


def _require(condition: bool, reason: str, detail: str) -> None:
    if not condition:
        raise NativeInlineError(reason, detail)


def _stack_reg(name: str) -> bool:
    fam = reg_family(name)
    return fam is not None and fam[0] == "rsp"


def _movsx_word_from_byte(insn: capstone.CsInsn) -> bool:
    # 66 [REX] 0F BE /r, mod=3 (MOVSX r16,r8). In 64-bit mode REX can
    # select extended GPRs or SIL/DIL/BPL/SPL; the stack-register check
    # separately rejects SPL and SP. No segment, REP or address override.
    raw = insn.bytes
    return (insn.mnemonic == "movsx" and raw[0] == 0x66 and
            (len(raw) == 4 or len(raw) == 5 and raw[1] in (0x40, 0x41, 0x44, 0x45)) and
            raw[-3:-1] == b"\x0f\xbe" and raw[-1] & 0xc0 == 0xc0 and
            len(insn.operands) == 2 and all(op.type == X.X86_OP_REG for op in insn.operands) and
            (insn.operands[0].size, insn.operands[1].size) == (2, 1))


def _function(code: bytes, max_cfg: int) -> dict[int, Any]:
    _require(bool(code), "ambiguous_bounds", "empty function")
    insns = {i.address: i for i in decoder().disasm(code, 0)}
    _require(bool(insns) and sum(i.size for i in insns.values()) == len(code),
             "unsupported_instruction", "function bytes do not tile")
    _require(len(insns) <= max_cfg, "cfg_budget", "function exceeds the instruction budget")
    reached = set()
    work = deque([0])
    returns = 0
    while work:
        off = work.popleft()
        if off in reached:
            continue
        _require(off in insns, "unsupported_control_flow", f"invalid successor {off:#x}")
        reached.add(off)
        i = insns[off]
        next_off = off + i.size
        if bytes(i.bytes) == b"\xc3":
            returns += 1
            continue
        if i.mnemonic in _JUMPS:
            _require(not any(i.prefix) and len(i.operands) == 1 and
                     i.operands[0].type == X.X86_OP_IMM,
                     "unsupported_control_flow", f"indirect branch at {off:#x}")
            work.append(i.operands[0].imm)
            if i.mnemonic != "jmp":
                work.append(next_off)
            continue
        if i.group(capstone.CS_GRP_CALL) or i.group(capstone.CS_GRP_JUMP) or i.group(capstone.CS_GRP_RET):
            raise NativeInlineError("unsupported_control_flow", f"{i.mnemonic} at {off:#x}")
        reads, writes = i.regs_access()
        _require(not any(_stack_reg(i.reg_name(reg)) for reg in (*reads, *writes)),
                 "unsupported_stack", f"{i.mnemonic} uses the stack at {off:#x}")
        if bytes(i.bytes) not in (b"\xf3\x0f\x1e\xfa", b"\x90"):
            _require(i.mnemonic in _ALU and (not any(i.prefix) or _movsx_word_from_byte(i)),
                     "unsupported_instruction",
                     f"unsupported {i.mnemonic} at {off:#x}")
            for op in i.operands:
                if op.type == X.X86_OP_MEM:
                    mem = op.mem
                    _require(i.mnemonic == "lea" and not mem.segment and all(
                        not r or (reg_family(i.reg_name(r)) is not None and
                                  i.reg_name(r) != "rip") for r in (mem.base, mem.index)),
                        "unsupported_memory", f"address-dependent operation at {off:#x}")
                elif op.type == X.X86_OP_REG:
                    _require(reg_family(i.reg_name(op.reg)) is not None and i.reg_name(op.reg) != "rip",
                             "unsupported_instruction", f"unsupported register at {off:#x}")
        work.append(next_off)
    _require(returns > 0 and reached == insns.keys(), "unsupported_control_flow",
             "function has no return or includes unreachable bytes")
    return insns


def check_leaf(code: bytes, *, max_cfg: int = 128) -> None:
    _function(code, max_cfg)


def verify_fragment(request: InlineCall, island: bytes | None = None, *,
                    island_start: int = 0) -> None:
    """Check the source CFG against a preview or against final linked bytes."""
    source = _function(request.callee_code, 128)
    fragment = request.fragment
    actual = fragment.code if island is None else island
    _require(len(actual) == len(fragment.code) <= 1024, "native_correspondence",
             "fragment size differs")
    insns = {i.address: i for i in decoder().disasm(actual, 0)}
    origins = fragment.origins
    _require(len(origins) == len(source) == len(insns), "native_correspondence",
             "instructions were added or dropped")
    _require(tuple(source) == tuple(o.source_off for o in origins) and
             tuple(insns) == tuple(o.output_off for o in origins) and
             sum(i.size for i in insns.values()) == len(actual),
             "native_correspondence", "origins must tile the source and island in order")
    mapped = {o.source_off: o.output_off for o in origins}
    returns = []
    for o in origins:
        original, new = source[o.source_off], insns[o.output_off]
        _require(original.size == o.source_size and new.size == o.output_size,
                 "native_correspondence", "origin instruction lengths differ")
        if bytes(original.bytes) == b"\xc3":
            _require(bytes(new.bytes)[:1] == b"\xe9" and new.size == 5,
                     "native_correspondence", "return is not a direct near jump")
            field = o.output_off + 1
            returns.append(field)
            target = (new.address + new.size if island is None else
                      request.continuation_off - island_start)
            _require(new.operands[0].imm == target, "native_correspondence",
                     "return does not land at the continuation")
        elif original.mnemonic in _JUMPS:
            _require(original.mnemonic == new.mnemonic and not any(new.prefix) and
                     len(new.operands) == 1 and
                     new.operands[0].type == X.X86_OP_IMM and
                     new.operands[0].imm == mapped[original.operands[0].imm],
                     "native_correspondence", "internal branch successor differs")
        else:
            _require(bytes(original.bytes) == bytes(new.bytes), "native_correspondence",
                     f"instruction bytes differ at {o.source_off:#x}")
    _require(tuple(returns) == fragment.continuation_fields, "native_correspondence",
             "return relocation fields do not match the origin map")


def verify_site(unit: TBUnit, request: InlineCall, code_address: int) -> None:
    """Check an ordinary NO_RWG | NO_SIDE_EFFECTS helper before removing its call."""
    _verify_bound_site(unit, request, code_address, required_flags=5)


def _verify_bound_site(unit: TBUnit, request: InlineCall, code_address: int, *,
                       required_flags: int | None, allow_ldst: bool = False) -> None:
    """Verify the original TCG call and the private return-slot obligations.

    Assumes the admitted x86-64 SysV QEMU TCG frame and no observation of dead
    host stack memory or asynchronous stack unwinding through generated code.
    The caller cannot take the address of its stack and can only access the
    nonnegative, allocated part of the TCG spill frame. Remaining ordinary calls
    use the same ABI and own their own call-clobbered space below RSP.
    """
    start, end = request.call_off, request.continuation_off
    _require(request.target == unit.uid and request.unit_hash == unit.content_hash and
             0 <= start < end < unit.ldst_end and unit.code[start:end] == request.call_bytes and
             is_sha256_id(request.image_id) and is_sha256_id(request.native_input_id),
             "stale_inline_call", "original unit hash or call bytes differ")
    binding = next((h for h in unit.bindings.helpers if h.id == request.helper_id), None)
    helper_sites = [s for s in unit.sites
                    if s.kind is SiteKind.HELPER_CALL and s.insn_off == start]
    ldst_sites = [s for s in unit.sites
                  if s.kind is SiteKind.LDST_HELPER_CALL and s.insn_off == start]
    bound = (binding is not None and binding.name is not None and
             (required_flags is None or binding.flags & 0xf == required_flags) and
             binding.func == request.target_abs and binding.symbol == request.symbol and
             len(helper_sites) == 1 and helper_sites[0].aux == binding.id and
             helper_sites[0].value == binding.func)
    trusted_ldst = (allow_ldst and len(ldst_sites) == 1 and
                    ldst_sites[0].value == request.target_abs)
    _require(bound or trusted_ldst,
             "unsupported_recovery", "call lacks the expected TCG helper binding or flags")
    site = helper_sites if bound else ldst_sites
    base = code_address + unit.tc_offset
    code_end = unit.pool_start if unit.pool_start is not None else unit.ldst_end
    callers = sweep(unit.code[:code_end], base)
    _require(tiles(callers, base, base + code_end), "unsupported_caller", "caller does not tile")
    starts = {i.address - base for i in callers}
    _require(start in starts and end in starts and not any(
        i.branch_target is not None and base + start < i.branch_target < base + end for i in callers),
        "unsupported_control_flow", "incoming transfer can enter the replaced instruction")
    i = next(i for i in callers if i.address == base + start)
    _require(i.is_call and i.size == end - start, "stale_inline_call", "call endpoint differs")
    if request.pool_slot_off is not None:
        slot = request.pool_slot_off
        pool = [p for p in unit.pool if p.user_field_off == site[0].field_off]
        _require(site[0].enc is Encoding.POOL_INDIRECT and i.raw[:2] == b"\xff\x15" and
                 len(i.raw) == 6 and unit.pool_start is not None and
                 unit.pool_start <= slot <= unit.pool_end - 8 and
                 i.rip_target == base + slot and len(pool) == 1 and pool[0].slot_off == slot and
                 pool[0].data0 == request.target_abs and
                 int.from_bytes(unit.code[slot:slot + 8], "little") == request.target_abs and
                 not any(slot < f.field_off + f.width and f.field_off < slot + 8
                         for f in unit.reloc.fixups), "call_binding_unknown",
                 "pool slot is not a fixed helper pointer")
    else:
        _require(site[0].enc is Encoding.REL32 and len(i.raw) == 5 and i.raw[0] == 0xe8 and
                 i.branch_target == request.target_abs, "call_target_mismatch", "wrong direct target")
    for command in decoder().disasm(unit.code[:code_end], base):
        if command.group(capstone.CS_GRP_CALL):
            continue  # The x86 TCG emitter's calls obey the admitted host ABI.
        _require(not command.group(capstone.CS_GRP_RET), "unsupported_stack", "host TB returns")
        reads, writes = command.regs_access()
        _require(not any(_stack_reg(command.reg_name(reg)) for reg in writes),
                 "unsupported_stack", "TB changes its host stack pointer")
        if any(_stack_reg(command.reg_name(reg)) for reg in reads):
            accesses = [op for op in command.operands if op.type == X.X86_OP_MEM and
                        _stack_reg(command.reg_name(op.mem.base))]
            _require(bool(accesses) and command.mnemonic != "lea" and all(
                op.mem.disp >= 0 and not op.mem.index for op in accesses) and not any(
                op.type == X.X86_OP_REG and _stack_reg(command.reg_name(op.reg))
                for op in command.operands), "unsupported_stack",
                "TB can expose the return slot below its stack pointer")
