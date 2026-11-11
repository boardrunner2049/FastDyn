"""Control-flow-aware constant propagation for known memory accesses.

The Cortex-M scanner follows reset/vector entry points and direct calls, then
propagates partially known register values across the resulting control-flow
graph. It deliberately forgets values at uncertain writes and ABI call
clobbers. Literal loads, MOVW/MOVT pairs, copies, and immediate arithmetic are
resolved without relying on symbols, so stripped firmware is supported.

This module intentionally reports architecture facts, not MMIO policy. A
consumer may filter the recovered effective addresses for peripherals, RAM,
device protocols, or any other address class.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import struct
from typing import Iterable, List, Optional, Sequence

from capstone import CS_ARCH_ARM, CS_ARCH_ARM64, CS_GRP_CALL, CS_GRP_JUMP, CS_GRP_RET
from capstone import CS_MODE_ARM, CS_MODE_LITTLE_ENDIAN, CS_MODE_THUMB, Cs
from capstone.arm import (
    ARM_CC_AL, ARM_CC_INVALID, ARM_OP_IMM, ARM_OP_MEM, ARM_OP_REG,
    ARM_REG_LR, ARM_REG_PC, ARM_REG_R0, ARM_REG_R1, ARM_REG_R2, ARM_REG_R3,
    ARM_REG_R12,
)
from capstone.arm64 import ARM64_OP_IMM, ARM64_OP_MEM, ARM64_OP_REG

from .errors import UnsupportedTargetError


@dataclass(frozen=True)
class KnownMemoryAccess:
    pc: int
    instruction_size: int
    transfer_size: int
    direction: str
    arch: str
    instruction: str
    base_register: str
    address: int
    note: str

    @property
    def size(self) -> int:
        """Compatibility alias for the old encoded-instruction length."""
        return self.instruction_size


class ConstantPropagation:
    """Cached known-effective-address analysis over executable image regions.

    The result is deliberately address-class agnostic. Consumers decide
    whether an address denotes MMIO, RAM, a protocol buffer, or something
    else. Results are immutable and safe to share across run preprocessors.
    """

    def __init__(self, regions: Sequence[tuple[int, bytes]], *,
                 architecture: str, instruction_mode: str,
                 entry_points: Iterable[int] = ()):
        self._regions = tuple((int(base), bytes(blob)) for base, blob in regions)
        self.architecture = architecture.lower()
        self.instruction_mode = instruction_mode
        self.entry_points = tuple(int(pc) & ~1 for pc in entry_points)
        self._memory_accesses: tuple[KnownMemoryAccess, ...] | None = None

    def memory_accesses(self) -> tuple[KnownMemoryAccess, ...]:
        """Return accesses whose effective address is statically known."""
        if self._memory_accesses is not None:
            return self._memory_accesses
        if self.architecture == "arm" and self.instruction_mode == "thumb-mclass":
            arch, mode = "arm32", "thumb"
        elif self.architecture in ("aarch64", "arm64"):
            arch, mode = "arm64", "arm"
        else:
            raise UnsupportedTargetError(
                "unsupported constant-propagation target: "
                f"architecture={self.architecture!r}, mode={self.instruction_mode!r}"
            )

        found: dict[tuple[int, int, str, int], KnownMemoryAccess] = {}
        for base, blob in self._regions:
            for access in find_known_memory_accesses(
                    blob=blob, base_va=base, arch=arch, mode=mode,
                    entry_points=self.entry_points):
                found[(access.pc, access.address, access.direction,
                       access.transfer_size)] = access
        self._memory_accesses = tuple(sorted(
            found.values(), key=lambda item: (item.pc, item.address, item.direction)
        ))
        return self._memory_accesses


@dataclass(frozen=True)
class _Bits:
    value: int = 0
    mask: int = 0

    @classmethod
    def constant(cls, value: int, bits: int = 32) -> "_Bits":
        mask = (1 << bits) - 1
        return cls(value & mask, mask)

    @property
    def known(self) -> bool:
        return self.mask == 0xFFFFFFFF

    def merge(self, other: "_Bits") -> "_Bits":
        same = ~(self.value ^ other.value) & self.mask & other.mask & 0xFFFFFFFF
        return _Bits(self.value & same, same)


_UNKNOWN = _Bits()
_LOAD_WIDTHS = {"ldr": 4, "ldrb": 1, "ldrh": 2}
_STORE_WIDTHS = {"str": 4, "strb": 1, "strh": 2}


def find_known_memory_accesses(*, blob: bytes, base_va: int, arch: str,
                               mode: str = "thumb",
                               entry_points: Optional[Iterable[int]] = None
                               ) -> List[KnownMemoryAccess]:
    if arch == "arm32":
        return _scan_arm32(blob, base_va, mode, entry_points)
    if arch == "arm64":
        return _scan_arm64(blob, base_va)
    raise UnsupportedTargetError(f"unsupported constant-propagation architecture: {arch}")


def _mnemonic(insn) -> str:
    return insn.mnemonic.lower().split(".", 1)[0]


def _vector_entries(blob: bytes, base_va: int) -> set[int]:
    entries: set[int] = set()
    if len(blob) < 8:
        return entries
    sp, reset = struct.unpack_from("<II", blob)
    if not (0x20000000 <= sp < 0x40000000 and reset & 1):
        return entries
    for offset in range(4, min(len(blob), 256 * 4), 4):
        value = struct.unpack_from("<I", blob, offset)[0]
        address = value & ~1
        if value & 1 and base_va <= address < base_va + len(blob):
            entries.add(address)
    return entries


def _decode_one(cs: Cs, blob: bytes, base_va: int, pc: int):
    offset = pc - base_va
    if offset < 0 or offset >= len(blob):
        return None
    return next(cs.disasm(blob[offset: offset + 4], pc, count=1), None)


def _blank_state() -> tuple[_Bits, ...]:
    return tuple(_UNKNOWN for _ in range(256))


def _merge_state(old: Optional[tuple[_Bits, ...]], new: tuple[_Bits, ...]):
    if old is None:
        return new, True
    merged = tuple(a.merge(b) for a, b in zip(old, new))
    return merged, merged != old


def _reg_value(state: Sequence[_Bits], reg: int) -> _Bits:
    return state[reg] if 0 <= reg < len(state) else _UNKNOWN


def _set_reg(state: list[_Bits], reg: int, value: _Bits) -> None:
    if 0 <= reg < len(state):
        state[reg] = value


def _literal_value(blob: bytes, base_va: int, insn, op) -> Optional[int]:
    if op.mem.base != ARM_REG_PC:
        return None
    address = ((insn.address + 4) & ~3) + op.mem.disp
    offset = address - base_va
    if 0 <= offset <= len(blob) - 4:
        return struct.unpack_from("<I", blob, offset)[0]
    return None


def _arm_mem_address(insn, state: Sequence[_Bits]) -> Optional[tuple[int, int, str]]:
    for op in insn.operands:
        if op.type != ARM_OP_MEM:
            continue
        base = _reg_value(state, op.mem.base)
        if not base.known:
            return None
        value = base.value
        if op.mem.index:
            index = _reg_value(state, op.mem.index)
            if not index.known:
                return None
            shifted = index.value
            if getattr(op.shift, "value", 0):
                shifted = (shifted << op.shift.value) & 0xFFFFFFFF
            value = value - shifted if getattr(op, "subtracted", False) else value + shifted
        value = (value + op.mem.disp) & 0xFFFFFFFF
        return value, op.mem.base, f"disp={op.mem.disp:+d}"
    return None


def _written_registers(insn) -> set[int]:
    try:
        _reads, writes = insn.regs_access()
        return set(writes)
    except Exception:
        return set(insn.regs_write)


def _track_arm32(insn, incoming: Sequence[_Bits], blob: bytes,
                 base_va: int) -> tuple[_Bits, ...]:
    state = list(incoming)
    m, ops = _mnemonic(insn), insn.operands
    handled: set[int] = set()

    def assign(reg: int, value: _Bits) -> None:
        _set_reg(state, reg, value)
        handled.add(reg)

    if m in ("mov", "movs") and len(ops) >= 2 and ops[0].type == ARM_OP_REG:
        if ops[1].type == ARM_OP_IMM:
            assign(ops[0].reg, _Bits.constant(ops[1].imm))
        elif ops[1].type == ARM_OP_REG:
            assign(ops[0].reg, _reg_value(incoming, ops[1].reg))
        else:
            assign(ops[0].reg, _UNKNOWN)
    elif m in ("movw", "movt") and len(ops) == 2:
        dst, old, imm = ops[0].reg, _reg_value(incoming, ops[0].reg), ops[1].imm & 0xFFFF
        if m == "movw":
            assign(dst, _Bits((old.value & 0xFFFF0000) | imm,
                              (old.mask & 0xFFFF0000) | 0xFFFF))
        else:
            assign(dst, _Bits((old.value & 0xFFFF) | (imm << 16),
                              (old.mask & 0xFFFF) | 0xFFFF0000))
    elif m in ("add", "adds", "sub", "subs") and ops and ops[0].type == ARM_OP_REG:
        dst = ops[0].reg
        if len(ops) >= 3:
            lhs_op, rhs_op = ops[1], ops[2]
        elif len(ops) == 2:
            lhs_op, rhs_op = ops[0], ops[1]
        else:
            lhs_op = rhs_op = None
        lhs = _reg_value(incoming, lhs_op.reg) if lhs_op and lhs_op.type == ARM_OP_REG else _UNKNOWN
        rhs = (_Bits.constant(rhs_op.imm) if rhs_op and rhs_op.type == ARM_OP_IMM else
               _reg_value(incoming, rhs_op.reg) if rhs_op and rhs_op.type == ARM_OP_REG else _UNKNOWN)
        if lhs.known and rhs.known:
            value = lhs.value + rhs.value if m.startswith("add") else lhs.value - rhs.value
            assign(dst, _Bits.constant(value))
        else:
            assign(dst, _UNKNOWN)
    elif m == "adr" and len(ops) >= 2 and ops[1].type == ARM_OP_IMM:
        assign(ops[0].reg, _Bits.constant(ops[1].imm))
    elif m.startswith("ldr") and ops and ops[0].type == ARM_OP_REG:
        literal = None
        for op in ops:
            if op.type == ARM_OP_MEM:
                literal = _literal_value(blob, base_va, insn, op)
                break
        assign(ops[0].reg, _Bits.constant(literal) if literal is not None else _UNKNOWN)

    if insn.group(CS_GRP_CALL):
        for reg in (ARM_REG_R0, ARM_REG_R1, ARM_REG_R2, ARM_REG_R3, ARM_REG_R12, ARM_REG_LR):
            assign(reg, _UNKNOWN)
    for reg in _written_registers(insn) - handled:
        _set_reg(state, reg, _UNKNOWN)
    return tuple(state)


def _successors(insn, base_va: int, blob_size: int) -> tuple[list[int], list[int]]:
    end = insn.address + insn.size
    in_blob = lambda pc: base_va <= pc < base_va + blob_size
    targets = [op.imm & ~1 for op in insn.operands
               if op.type == ARM_OP_IMM and in_blob(op.imm & ~1)]
    if insn.group(CS_GRP_CALL):
        return ([end] if in_blob(end) else []), targets[:1]
    if insn.group(CS_GRP_RET) or (_mnemonic(insn) in ("bx", "pop") and
                                 ARM_REG_PC in _written_registers(insn)):
        return [], []
    if insn.group(CS_GRP_JUMP):
        target = targets[:1]
        conditional = getattr(insn, "cc", ARM_CC_INVALID) not in (ARM_CC_AL, ARM_CC_INVALID)
        conditional = conditional or _mnemonic(insn) in ("cbz", "cbnz")
        if conditional and in_blob(end):
            target.append(end)
        return target, []
    return ([end] if in_blob(end) else []), []


def _scan_arm32(blob, base_va, mode, entry_points) -> List[KnownMemoryAccess]:
    cs = Cs(CS_ARCH_ARM, (CS_MODE_ARM if mode == "arm" else CS_MODE_THUMB) |
            CS_MODE_LITTLE_ENDIAN)
    cs.detail = True
    seeds = {int(pc) & ~1 for pc in (entry_points or ())}
    if mode == "thumb":
        vectors = _vector_entries(blob, base_va)
        seeds.update(vectors)
        # A raw Cortex-M image begins with data, not instructions. Only use
        # the section/image base as a fallback when no vector or explicit
        # entry point identifies executable code.
        if not seeds:
            seeds.add(base_va)
    else:
        seeds.add(base_va)

    states: dict[int, tuple[_Bits, ...]] = {}
    queue: deque[int] = deque()
    blank = _blank_state()
    for pc in sorted(seeds):
        if base_va <= pc < base_va + len(blob):
            states[pc] = blank
            queue.append(pc)

    hits: dict[tuple[int, int, str], KnownMemoryAccess] = {}
    steps, max_steps = 0, max(4096, len(blob) * 32)
    while queue and steps < max_steps:
        steps += 1
        pc = queue.popleft()
        insn = _decode_one(cs, blob, base_va, pc)
        if insn is None:
            continue
        incoming, m = states[pc], _mnemonic(insn)
        widths = _LOAD_WIDTHS if m in _LOAD_WIDTHS else _STORE_WIDTHS if m in _STORE_WIDTHS else None
        if widths is not None:
            found = _arm_mem_address(insn, incoming)
            if found:
                address, base_reg, note = found
                direction = "read" if m in _LOAD_WIDTHS else "write"
                hits[(pc, address, direction)] = KnownMemoryAccess(
                    pc, insn.size, widths[m], direction, f"ARM32-{mode.upper()}",
                    f"{insn.mnemonic} {insn.op_str}", cs.reg_name(base_reg), address, note)

        outgoing = _track_arm32(insn, incoming, blob, base_va)
        successors, calls = _successors(insn, base_va, len(blob))
        for target in calls:
            # A direct callee sees the constants passed in argument and
            # callee-saved registers. The return edge below uses `outgoing`,
            # where the ABI-clobbered registers have already been forgotten.
            callee = list(incoming)
            _set_reg(callee, ARM_REG_LR, _UNKNOWN)
            merged, changed = _merge_state(states.get(target), tuple(callee))
            if changed:
                states[target] = merged
                queue.append(target)
        for target in successors:
            merged, changed = _merge_state(states.get(target), outgoing)
            if changed:
                states[target] = merged
                queue.append(target)
    return sorted(hits.values(), key=lambda h: (h.pc, h.address, h.direction))


def _scan_arm64(blob: bytes, base_va: int) -> List[KnownMemoryAccess]:
    """Bounded legacy AArch64 scan; substitution consumes Thumb results only."""
    cs = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
    cs.detail = True
    reg_const: dict[int, int] = {}
    results: list[KnownMemoryAccess] = []
    for insn in cs.disasm(blob, base_va):
        m = _mnemonic(insn)
        if m in ("ldr", "ldrb", "ldrh", "str", "strb", "strh"):
            for op in insn.operands:
                if op.type == ARM64_OP_MEM and op.mem.base in reg_const:
                    addr = reg_const[op.mem.base] + op.mem.disp
                    width = {"ldrb": 1, "strb": 1, "ldrh": 2, "strh": 2}.get(m, 4)
                    results.append(KnownMemoryAccess(
                        insn.address, insn.size, width,
                        "read" if m.startswith("ldr") else "write", "AArch64",
                        f"{insn.mnemonic} {insn.op_str}", cs.reg_name(op.mem.base),
                        addr, "const base"))
        ops = insn.operands
        if m in ("movz", "movk") and len(ops) >= 2 and ops[0].type == ARM64_OP_REG:
            shift = ops[2].imm if len(ops) > 2 and ops[2].type == ARM64_OP_IMM else 0
            old, mask = reg_const.get(ops[0].reg, 0), 0xFFFF << shift
            reg_const[ops[0].reg] = ((ops[1].imm << shift) if m == "movz" else
                                      (old & ~mask) | (ops[1].imm << shift))
        else:
            try:
                _reads, writes = insn.regs_access()
            except Exception:
                writes = insn.regs_write
            for reg in writes:
                reg_const.pop(reg, None)
    return results
