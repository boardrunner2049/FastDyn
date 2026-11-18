"""A small x86-64 encoder for synthetic units and the V18 self-test. WP19.

Only the instruction forms TCG emits for the ARM frontend and the guard
template of design §4.1 are encoded. Every synthetic unit built with it is
re-decoded with capstone by the tests, so an encoding slip shows up as a
decode mismatch, not as a silent wrong program.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

__all__ = ["R", "CC", "Asm", "nop"]

R = {n: i for i, n in enumerate(
    "rax rcx rdx rbx rsp rbp rsi rdi r8 r9 r10 r11 r12 r13 r14 r15".split())}
CC = {"o": 0, "no": 1, "b": 2, "ae": 3, "e": 4, "ne": 5, "be": 6, "a": 7, "s": 8, "ns": 9,
      "p": 10, "np": 11, "l": 12, "ge": 13, "le": 14, "g": 15}

_NOPS = {1: "90", 2: "6690", 3: "0f1f00", 4: "0f1f4000", 5: "0f1f440000", 6: "660f1f440000",
         7: "0f1f8000000000", 8: "0f1f840000000000", 9: "660f1f840000000000"}


def nop(n: int) -> bytes:
    """``n`` bytes of multi-byte nops (the B4 template)."""
    out = b""
    while n > 0:
        k = min(n, 9)
        out += bytes.fromhex(_NOPS[k])
        n -= k
    return out


def _r(name: str) -> int:
    return R[name]


def _rex(w: int = 0, r: int = 0, x: int = 0, b: int = 0, force: bool = False) -> bytes:
    v = 0x40 | (w << 3) | ((r >> 3) << 2) | ((x >> 3) << 1) | (b >> 3)
    return bytes([v]) if v != 0x40 or force else b""


def _modrm(mod: int, reg: int, rm: int) -> bytes:
    return bytes([(mod << 6) | ((reg & 7) << 3) | (rm & 7)])


def _d32(v: int) -> bytes:
    return struct.pack("<i", v)


def _env(reg: int, disp: int) -> bytes:
    """ModRM for ``[rbp + disp32]`` with ``reg`` in the reg field."""
    return _modrm(2, reg, 5) + _d32(disp)


def _mem(reg: int, base: int, disp: int) -> bytes:
    """ModRM (+SIB) for ``[base + disp32]``."""
    out = _modrm(2, reg, base)
    if base & 7 == 4:
        out += b"\x24"
    return out + _d32(disp)


@dataclass
class Asm:
    """Assembler with labels; ``base`` is the absolute address of byte 0."""

    base: int = 0x7FFF00100000
    buf: bytearray = field(default_factory=bytearray)
    labels: dict[str, int] = field(default_factory=dict)
    fix: list[tuple[int, str, int]] = field(default_factory=list)  # (field off, label, insn end)
    absfix: list[tuple[int, int, int]] = field(default_factory=list)  # (field off, target, end)
    marks: dict[str, int] = field(default_factory=dict)

    @property
    def pc(self) -> int:
        return len(self.buf)

    def label(self, name: str) -> int:
        self.labels[name] = self.pc
        return self.pc

    def mark(self, name: str) -> int:
        self.marks[name] = self.pc
        return self.pc

    def raw(self, b: bytes | str) -> int:
        off = self.pc
        self.buf += bytes.fromhex(b) if isinstance(b, str) else b
        return off

    def finish(self) -> bytes:
        for f, lab, end in self.fix:
            self.buf[f:f + 4] = _d32(self.labels[lab] - end)
        for f, tgt, end in self.absfix:
            self.buf[f:f + 4] = _d32(tgt - (self.base + end))
        return bytes(self.buf)

    # -- env -----------------------------------------------------------------------
    def mov_r_env(self, r: str, disp: int, w: int = 4) -> int:
        n = _r(r)
        return self.raw(_rex(1 if w == 8 else 0, n) + b"\x8b" + _env(n, disp))

    def mov_env_r(self, disp: int, r: str, w: int = 4) -> int:
        n = _r(r)
        if w == 1:
            return self.raw(_rex(0, n, force=n >= 4) + b"\x88" + _env(n, disp))
        pre = b"\x66" if w == 2 else b""
        return self.raw(pre + _rex(1 if w == 8 else 0, n) + b"\x89" + _env(n, disp))

    def mov_env_imm(self, disp: int, imm: int, w: int = 4) -> int:
        if w == 1:
            return self.raw(b"\xc6" + _env(0, disp) + bytes([imm & 0xFF]))
        if w == 2:
            return self.raw(b"\x66\xc7" + _env(0, disp) + struct.pack("<H", imm & 0xFFFF))
        return self.raw(_rex(1 if w == 8 else 0) + b"\xc7" + _env(0, disp) +
                        struct.pack("<I", imm & 0xFFFFFFFF))

    def cmp_env_imm(self, disp: int, imm: int, w: int = 4) -> int:
        if w == 1:
            return self.raw(b"\x80" + _env(7, disp) + bytes([imm & 0xFF]))
        return self.raw(_rex(1 if w == 8 else 0) + b"\x81" + _env(7, disp) +
                        struct.pack("<I", imm & 0xFFFFFFFF))

    def cmp_env_r(self, disp: int, r: str, w: int = 8) -> int:
        n = _r(r)
        return self.raw(_rex(1 if w == 8 else 0, n) + b"\x39" + _env(n, disp))

    def and_r_env(self, r: str, disp: int) -> int:
        n = _r(r)
        return self.raw(_rex(0, n) + b"\x23" + _env(n, disp))

    def add_r64_env(self, r: str, disp: int) -> int:
        n = _r(r)
        return self.raw(_rex(1, n) + b"\x03" + _env(n, disp))

    # -- registers -------------------------------------------------------------------
    def alu_r_imm(self, op: str, r: str, imm: int, w: int = 4) -> int:
        ext = {"add": 0, "or": 1, "and": 4, "sub": 5, "xor": 6, "cmp": 7}[op]
        n = _r(r)
        return self.raw(_rex(1 if w == 8 else 0, 0, 0, n) + b"\x81" + _modrm(3, ext, n) +
                        struct.pack("<I", imm & 0xFFFFFFFF))

    def alu_r_r(self, op: str, dst: str, src: str, w: int = 4) -> int:
        opc = {"add": 0x01, "or": 0x09, "and": 0x21, "sub": 0x29, "xor": 0x31, "cmp": 0x39,
               "test": 0x85, "mov": 0x89}[op]
        d, s = _r(dst), _r(src)
        return self.raw(_rex(1 if w == 8 else 0, s, 0, d) + bytes([opc]) + _modrm(3, s, d))

    def mov_r_imm(self, r: str, imm: int) -> int:
        n = _r(r)
        return self.raw(_rex(0, 0, 0, n) + bytes([0xB8 + (n & 7)]) +
                        struct.pack("<I", imm & 0xFFFFFFFF))

    def movabs(self, r: str, imm: int) -> int:
        n = _r(r)
        return self.raw(_rex(1, 0, 0, n) + bytes([0xB8 + (n & 7)]) + struct.pack("<Q", imm))

    def shr_imm(self, r: str, imm: int, w: int = 8) -> int:
        n = _r(r)
        return self.raw(_rex(1 if w == 8 else 0, 0, 0, n) + b"\xc1" + _modrm(3, 5, n) +
                        bytes([imm]))

    def lea_r_m8(self, dst: str, base: str, disp: int, w: int = 4) -> int:
        d, b = _r(dst), _r(base)
        sib = b"\x24" if b & 7 == 4 else b""
        return self.raw(_rex(1 if w == 8 else 0, d, 0, b) + b"\x8d" + _modrm(1, d, b) + sib +
                        bytes([disp & 0xFF]))

    def lea_rip(self, r: str, label: str | None = None, target: int | None = None) -> int:
        n = _r(r)
        off = self.raw(_rex(1, n) + b"\x8d" + _modrm(0, n, 5) + b"\0\0\0\0")
        self._fix(self.pc - 4, label, target)
        return off

    def cmp_r_m(self, r: str, base: str, disp: int = 0) -> int:
        n, b = _r(r), _r(base)
        return self.raw(_rex(0, n, 0, b) + b"\x3b" + _modrm(1, n, b) + bytes([disp & 0xFF]))

    def mov_r64_m(self, r: str, base: str, disp: int) -> int:
        n, b = _r(r), _r(base)
        return self.raw(_rex(1, n, 0, b) + b"\x8b" + _modrm(1, n, b) + bytes([disp & 0xFF]))

    def mov_r_bi(self, r: str, base: str, index: str) -> int:
        """``mov r32, [base + index]`` (the guest load access)."""
        n, b, i = _r(r), _r(base), _r(index)
        return self.raw(_rex(0, n, i, b) + b"\x8b" + _modrm(0, n, 4) +
                        bytes([((i & 7) << 3) | (b & 7)]))

    def mov_bi_r(self, base: str, index: str, r: str) -> int:
        """``mov [base + index], r32`` (the guest store access)."""
        n, b, i = _r(r), _r(base), _r(index)
        return self.raw(_rex(0, n, i, b) + b"\x89" + _modrm(0, n, 4) +
                        bytes([((i & 7) << 3) | (b & 7)]))

    def mov_m_r(self, base: str, disp: int, r: str, w: int = 4) -> int:
        """``mov [base + disp32], r`` (a host store: UPDATE_MEM / LOG_REG shape)."""
        n, b = _r(r), _r(base)
        return self.raw(_rex(1 if w == 8 else 0, n, 0, b) + b"\x89" + _mem(n, b, disp))

    # -- control -----------------------------------------------------------------------
    def _fix(self, field_off: int, label: str | None, target: int | None) -> None:
        if label is not None:
            self.fix.append((field_off, label, field_off + 4))
        elif target is not None:
            self.absfix.append((field_off, target, field_off + 4))
        else:  # pragma: no cover
            raise ValueError("label or target")

    def jcc(self, cc: str, label: str) -> int:
        off = self.raw(bytes([0x0F, 0x80 + CC[cc]]) + b"\0\0\0\0")
        self._fix(self.pc - 4, label, None)
        return off

    def jmp(self, label: str | None = None, target: int | None = None) -> int:
        off = self.raw(b"\xe9\0\0\0\0")
        self._fix(self.pc - 4, label, target)
        return off

    def jmp0(self) -> int:
        """``jmp rel32 0`` (an unlinked goto_tb / slot field)."""
        return self.raw(b"\xe9\0\0\0\0")

    def call(self, target: int) -> int:
        off = self.raw(b"\xe8\0\0\0\0")
        self._fix(self.pc - 4, None, target)
        return off

    def call_pool(self, label: str) -> int:
        off = self.raw(b"\xff\x15\0\0\0\0")
        self._fix(self.pc - 4, label, None)
        return off

    def jmp_reg(self, r: str) -> int:
        n = _r(r)
        return self.raw(_rex(0, 0, 0, n) + b"\xff" + _modrm(3, 4, n))

    def mov_rdi_rbp(self) -> int:
        return self.raw("4889ef")

    def nop(self, n: int) -> int:
        return self.raw(nop(n))

    def int3(self, n: int = 1) -> int:
        return self.raw(b"\xcc" * n)

    def align(self, k: int, fill: bytes = b"\x90") -> None:
        while self.pc % k:
            self.raw(fill)

    def quad(self, v: int) -> int:
        return self.raw(struct.pack("<Q", v))
