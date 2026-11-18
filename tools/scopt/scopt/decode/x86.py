"""x86-64 host-code decoder: linear sweep and reference extraction (design §2.7 steps 4-5). WP2.

The decoder is capstone 5.0.3 (``CS_ARCH_X86``, ``CS_MODE_64``, detail on).

* :func:`sweep` decodes ``[addr, end)`` linearly. Bytes capstone cannot decode
  become a one-byte :class:`Insn` with ``bad=True`` (objdump's ``(bad)``) and
  the sweep continues at the next byte, so a sweep always reports every
  problem instead of stopping at the first.
* :func:`sweep_to_pool` is the meta-less variant: it stops at the lowest
  address any RIP-relative *memory* operand (not LEA) seen so far points to,
  i.e. at the start of the TB's constant pool (critic A2 heuristic). It never
  decodes pool data as code.
* :func:`extract_refs` lists every reference-bearing operand (§2.7 step 5):
  rel8/rel32 branches and calls, every ``[rip+disp32]`` operand (memory or
  LEA), every movabs imm64, and every imm32/disp32 whose zero- or
  sign-extended value lies in the arena or in a MAPS range. Register-indirect
  ``jmp``/``call`` (``FF /4 reg``, ``FF /2 reg``) are listed too, with
  ``width=0``: goto_ptr is one. So are memory-indirect branches through a
  register (``jmp [rax+8]``), which TCG never emits; they are listed so a
  classifier fails closed on them.

Addresses in :class:`Insn` and :class:`DecodedRef` are absolute host addresses
(the sweep's ``addr`` is ``code_address + tc_offset`` for a TB).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Protocol

import capstone
from capstone import x86 as cs_x86

from ..formats.enums import Encoding

__all__ = [
    "Insn", "X86Insn", "MemOperand", "RefKind", "DecodedRef", "Arena", "AddressRanges",
    "decode_one", "sweep", "sweep_to_pool", "extract_refs", "bad_insns", "tiles", "decoder",
]

_U64 = (1 << 64) - 1


@lru_cache(maxsize=1)
def decoder() -> capstone.Cs:
    """The shared capstone handle (x86-64, detail on)."""
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    return md


@dataclass(frozen=True, slots=True)
class MemOperand:
    """One memory operand: ``[base + index*scale + disp]`` (register names, '' if none)."""

    base: str
    index: str
    scale: int
    disp: int
    size: int  # access size in bytes


@dataclass(frozen=True, slots=True)
class Insn:
    """One decoded host instruction.

    ``imm_offset``/``imm_size`` and ``disp_offset``/``disp_size`` are capstone's
    encoding offsets, relative to the start of the instruction (0 if absent).
    ``branch_target`` is the absolute target of a direct (relative) jmp/jcc/call.
    ``reg_target`` is the register of an indirect ``jmp reg``/``call reg``.
    """

    address: int
    size: int
    raw: bytes
    mnemonic: str
    op_str: str
    bad: bool = False
    is_jump: bool = False
    is_call: bool = False
    is_ret: bool = False
    branch_target: int | None = None
    reg_target: str | None = None
    mem: tuple[MemOperand, ...] = ()
    op_sizes: tuple[int, ...] = ()  # operand sizes in operand order
    dst_reg: str | None = None  # first operand, when it is a register
    imm_offset: int = 0
    imm_size: int = 0
    disp_offset: int = 0
    disp_size: int = 0

    @property
    def end(self) -> int:
        return self.address + self.size

    @property
    def rip_mem(self) -> MemOperand | None:
        """The RIP-relative memory operand, if any."""
        for m in self.mem:
            if m.base == "rip":
                return m
        return None

    @property
    def rip_target(self) -> int | None:
        """Absolute address a RIP-relative operand refers to (``end + disp``)."""
        m = self.rip_mem
        return None if m is None else (self.end + m.disp) & _U64

    @property
    def is_lea(self) -> bool:
        return self.mnemonic == "lea"

    @property
    def is_jcc(self) -> bool:
        return self.is_jump and self.mnemonic != "jmp"

    def field(self, which: str) -> tuple[int, int] | None:
        """``(absolute address, width)`` of the ``imm`` or ``disp`` field."""
        if which == "imm" and self.imm_size:
            return self.address + self.imm_offset, self.imm_size
        if which == "disp" and self.disp_size:
            return self.address + self.disp_offset, self.disp_size
        return None

    def text(self) -> str:
        return f"{self.mnemonic} {self.op_str}".strip()


X86Insn = Insn  # unambiguous alias (scopt.model.unit.Insn is the *guest* insn record)


def _from_cs(i: capstone.CsInsn) -> Insn:
    groups = set(i.groups)
    is_jump = capstone.CS_GRP_JUMP in groups
    is_call = capstone.CS_GRP_CALL in groups
    is_ret = capstone.CS_GRP_RET in groups
    rel = capstone.CS_GRP_BRANCH_RELATIVE in groups
    mem: list[MemOperand] = []
    sizes: list[int] = []
    branch_target = reg_target = dst_reg = None
    for n, op in enumerate(i.operands):
        sizes.append(op.size)
        if op.type == cs_x86.X86_OP_MEM:
            m = op.mem
            mem.append(MemOperand(i.reg_name(m.base) if m.base else "",
                                  i.reg_name(m.index) if m.index else "",
                                  m.scale, m.disp, op.size))
        elif op.type == cs_x86.X86_OP_IMM and rel:
            # (is_jump or is_call) is not required: capstone 5.0.3 gives
            # loop/loope/loopne/jrcxz/jecxz only CS_GRP_BRANCH_RELATIVE, and
            # their rel8 field must still be extracted (they then pin, since
            # no capture site explains them).
            branch_target = op.imm & _U64
        elif op.type == cs_x86.X86_OP_REG:
            if n == 0:
                dst_reg = i.reg_name(op.reg)
            if (is_jump or is_call) and len(i.operands) == 1:
                reg_target = i.reg_name(op.reg)
    return Insn(
        address=i.address, size=i.size, raw=bytes(i.bytes), mnemonic=i.mnemonic,
        op_str=i.op_str, is_jump=is_jump, is_call=is_call, is_ret=is_ret,
        branch_target=branch_target, reg_target=reg_target, mem=tuple(mem),
        op_sizes=tuple(sizes), dst_reg=dst_reg,
        imm_offset=i.imm_offset, imm_size=i.imm_size,
        disp_offset=i.disp_offset, disp_size=i.disp_size)


def decode_one(code: bytes, address: int) -> Insn | None:
    """Decode only the instruction beginning at ``address``."""
    insn = next(decoder().disasm(code, address, count=1), None)
    return None if insn is None else _from_cs(insn)


def _bad(addr: int, byte: bytes) -> Insn:
    return Insn(addr, 1, byte, "(bad)", "", bad=True)


def sweep(code: bytes, addr: int, end: int | None = None) -> list[Insn]:
    """Linear sweep of ``code`` (mapped at ``addr``) over ``[addr, end)``.

    ``end`` is an absolute address (default ``addr + len(code)``). Decoding
    may read past ``end`` (up to ``len(code)``) to finish the last
    instruction; use :func:`tiles` to check that the sweep ends exactly at
    ``end``. Undecodable bytes become ``(bad)`` one-byte instructions.
    """
    if end is None:
        end = addr + len(code)
    limit = end - addr
    if limit < 0 or limit > len(code):
        raise ValueError(f"end 0x{end:x} outside [0x{addr:x}, 0x{addr + len(code):x}]")
    md = decoder()
    out: list[Insn] = []
    off = 0
    while off < limit:
        for ci in md.disasm(code[off:], addr + off):
            out.append(_from_cs(ci))
            off += ci.size
            if off >= limit:
                break
        if off < limit:  # capstone stopped: undecodable byte
            out.append(_bad(addr + off, code[off:off + 1]))
            off += 1
    return out


def sweep_to_pool(code: bytes, addr: int) -> tuple[list[Insn], int]:
    """Meta-less sweep: decode until the constant pool starts.

    The pool start is the lowest address hit by a RIP-relative memory operand
    (not LEA) of an already decoded instruction, provided it lies after that
    instruction and inside ``code`` (critic A2). Without one, the whole of
    ``code`` is swept. Returns ``(insns, pool_start)`` with ``pool_start``
    absolute.
    """
    end = addr + len(code)
    pool = end
    md = decoder()
    out: list[Insn] = []
    off = 0
    while addr + off < pool:
        for ci in md.disasm(code[off:], addr + off):
            ins = _from_cs(ci)
            out.append(ins)
            off += ci.size
            t = ins.rip_target
            if t is not None and not ins.is_lea and ins.end <= t < pool:
                pool = t
            if addr + off >= pool:
                break
        if addr + off < pool:  # capstone stopped: undecodable byte
            out.append(_bad(addr + off, code[off:off + 1]))
            off += 1
    return out, pool


def bad_insns(insns: Iterable[Insn]) -> list[Insn]:
    return [i for i in insns if i.bad]


def tiles(insns: Sequence[Insn], start: int, end: int) -> bool:
    """True when ``insns`` cover ``[start, end)`` exactly, contiguously, with no (bad)."""
    pos = start
    for i in insns:
        if i.bad or i.address != pos:
            return False
        pos = i.end
    return pos == end


# --------------------------------------------------------------------------
# Reference extraction
# --------------------------------------------------------------------------

class RefKind(StrEnum):
    """What kind of operand a :class:`DecodedRef` came from."""

    JMP_REL8 = "jmp_rel8"
    JMP_REL32 = "jmp_rel32"
    JCC_REL8 = "jcc_rel8"
    JCC_REL32 = "jcc_rel32"
    CALL_REL32 = "call_rel32"
    RIP_LEA = "rip_lea"  # lea reg, [rip+d32]
    RIP_MEM = "rip_mem"  # any other [rip+d32] memory operand (load/store/alu)
    RIP_CALL = "rip_call"  # call [rip+d32]  (FF /2)
    RIP_JMP = "rip_jmp"  # jmp [rip+d32]   (FF /4)
    MOVABS = "movabs"  # mov r64, imm64
    IMM32 = "imm32"  # imm32 whose value is in the arena or a MAPS range
    DISP32 = "disp32"  # non-RIP disp32 whose value is in the arena or a MAPS range
    JMP_REG = "jmp_reg"  # jmp reg (goto_ptr); width 0
    CALL_REG = "call_reg"  # call reg; width 0
    JMP_MEM = "jmp_mem"  # jmp [reg+...] (non-RIP memory-indirect); width 0
    CALL_MEM = "call_mem"  # call [reg+...]; width 0


@dataclass(frozen=True, slots=True)
class DecodedRef:
    """One reference-bearing operand found by the sweep.

    ``field_addr``/``width`` locate the encoded field (``width == 0`` for
    register-indirect branches, whose ``field_addr`` is the instruction).
    ``value`` is the absolute address the field resolves to: the branch
    target, the RIP-relative operand address, or the immediate/displacement
    value (zero- or sign-extended per ``enc``). ``region`` says where
    ``value`` lies: ``"arena"``, ``"maps"`` or ``""``.
    """

    insn_addr: int
    insn_size: int
    field_addr: int
    width: int
    kind: RefKind
    enc: Encoding
    value: int | None
    region: str
    mnemonic: str
    reg: str | None = None

    @property
    def insn_end(self) -> int:
        return self.insn_addr + self.insn_size


class AddressRanges(Protocol):
    """Anything with ``contains(addr) -> bool`` (e.g. :class:`scopt.decode.symbols.Maps`)."""

    def contains(self, addr: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class Arena:
    """The code arena ``[base, base + size)`` (``code_address``, ``code_size``)."""

    base: int
    size: int

    def contains(self, addr: int) -> bool:
        return self.base <= addr < self.base + self.size

    def offset(self, addr: int) -> int:
        return addr - self.base


def _region(v: int, arena: Arena | None, maps: AddressRanges | None) -> str:
    if arena is not None and arena.contains(v):
        return "arena"
    if maps is not None and maps.contains(v):
        return "maps"
    return ""


def _sx(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return (v - (1 << bits)) & _U64 if v >> (bits - 1) else v


def _field_value(ins: Insn, which: str) -> int:
    off, size = (ins.imm_offset, ins.imm_size) if which == "imm" else (ins.disp_offset,
                                                                      ins.disp_size)
    return int.from_bytes(ins.raw[off:off + size], "little")


def extract_refs(insns: Iterable[Insn], arena: Arena | tuple[int, int] | None,
                 maps: AddressRanges | None = None) -> list[DecodedRef]:
    """Every reference-bearing operand of ``insns`` (§2.7 step 5), in address order.

    ``arena`` is ``Arena(code_address, code_size)`` (or that tuple); ``maps``
    is anything with ``contains(addr)``, normally a
    :class:`scopt.decode.symbols.Maps`. Branches, RIP-relative operands and
    movabs are always listed; imm32 and non-RIP disp32 values only when they
    lie in the arena or in ``maps``.
    """
    if isinstance(arena, tuple):
        arena = Arena(*arena)
    out: list[DecodedRef] = []
    for ins in insns:
        if ins.bad:
            continue
        a = ins.address
        # direct branches and calls
        if ins.branch_target is not None and ins.imm_size in (1, 4):
            w = ins.imm_size
            if ins.is_call:
                kind = RefKind.CALL_REL32
            elif ins.mnemonic == "jmp":
                kind = RefKind.JMP_REL8 if w == 1 else RefKind.JMP_REL32
            else:
                kind = RefKind.JCC_REL8 if w == 1 else RefKind.JCC_REL32
            out.append(DecodedRef(a, ins.size, a + ins.imm_offset, w, kind,
                                  Encoding.REL8 if w == 1 else Encoding.REL32,
                                  ins.branch_target, _region(ins.branch_target, arena, maps),
                                  ins.mnemonic))
            continue
        if ins.reg_target is not None:
            kind = RefKind.CALL_REG if ins.is_call else RefKind.JMP_REG
            out.append(DecodedRef(a, ins.size, a, 0, kind, Encoding.NONE, None, "",
                                  ins.mnemonic, ins.reg_target))
            continue
        rip = ins.rip_target
        if rip is None and (ins.is_jump or ins.is_call) and ins.mem:
            # memory-indirect branch through a register: no encoded target
            kind = RefKind.CALL_MEM if ins.is_call else RefKind.JMP_MEM
            out.append(DecodedRef(a, ins.size, a, 0, kind, Encoding.NONE, None, "",
                                  ins.mnemonic, ins.mem[0].base or None))
        # RIP-relative operand (always disp32)
        if rip is not None:
            if ins.is_lea:
                kind = RefKind.RIP_LEA
            elif ins.is_call:
                kind = RefKind.RIP_CALL
            elif ins.is_jump:
                kind = RefKind.RIP_JMP
            else:
                kind = RefKind.RIP_MEM
            out.append(DecodedRef(a, ins.size, a + ins.disp_offset, ins.disp_size, kind,
                                  Encoding.RIPREL32, rip, _region(rip, arena, maps),
                                  ins.mnemonic, ins.dst_reg if ins.is_lea else None))
        elif ins.disp_size == 4:
            raw = _field_value(ins, "disp")
            for v in dict.fromkeys((_sx(raw, 32), raw)):
                r = _region(v, arena, maps)
                if r:
                    out.append(DecodedRef(a, ins.size, a + ins.disp_offset, 4, RefKind.DISP32,
                                          Encoding.IMM32_SX if v != raw else Encoding.IMM32_ZX,
                                          v, r, ins.mnemonic))
                    break
        # immediates
        if ins.imm_size == 8:
            v = _field_value(ins, "imm")
            out.append(DecodedRef(a, ins.size, a + ins.imm_offset, 8, RefKind.MOVABS,
                                  Encoding.IMM64, v, _region(v, arena, maps), ins.mnemonic,
                                  ins.dst_reg))
        elif ins.imm_size == 4:
            raw = _field_value(ins, "imm")
            # 64-bit operand size: the imm32 is sign-extended; otherwise zero-extended
            wide = bool(ins.op_sizes) and ins.op_sizes[0] == 8
            v, enc = (_sx(raw, 32), Encoding.IMM32_SX) if wide else (raw, Encoding.IMM32_ZX)
            r = _region(v, arena, maps)
            if r:
                out.append(DecodedRef(a, ins.size, a + ins.imm_offset, 4, RefKind.IMM32, enc, v,
                                      r, ins.mnemonic, ins.dst_reg))
    return out
