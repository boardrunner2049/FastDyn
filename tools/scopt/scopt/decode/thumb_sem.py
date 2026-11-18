"""Guest Thumb semantics the return-edge analyses need (B3/B4 §3.1, §3.2, T-B3-42). WP20.

Only what is needed to *explain* host facts and classify exits, never to
derive a fact on its own (facts come from host code, §3.2):

* :func:`decode_unit` decodes a TB's guest bytes (capstone, Thumb + M-class,
  one call so capstone tracks IT blocks that start inside the TB) and joins
  each instruction with its Tier-0 ``insn_start`` words. ``words[1]`` is the
  translator's ``condexec_bits`` at the instruction start
  (``(condexec_cond << 4) | (condexec_mask >> 1)``, translate.c
  ``arm_tr_insn_start``), which is authoritative for "is this instruction
  inside an IT block".
* :func:`imm_defs` recognises the four unconditional immediate moves whose
  value is a literal of the encoding (16-bit ``movs Rd,#imm8``, ``mov{s}.w``
  / ``mvn{s}`` with a modified immediate, ``movw``). Only these give
  ``source=guest_code`` (§3.2 "a Thumb decode writing field with an
  immediate").
* :func:`classify_last` classifies the TB's last instruction for §3.1 rule 2.
* :class:`ItState` replays QEMU's IT-state machine (translate.c
  ``thumb_tr_translate_insn`` "Advance the Thumb condexec condition",
  ``trans_IT``, ``arm_tr_init_disas_context``) for the U-COND check (T-B3-42).

Fields use guest names ``r0``..``r12``, ``sp``, ``lr``, ``pc`` and the flag
words ``NF``, ``ZF``, ``CF``, ``VF`` (QEMU stores NF/ZF as the 32-bit result for
a logical op: ``gen_logic_CC`` copies the result to both).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

import capstone
from capstone import arm as A

__all__ = ["GInsn", "decode_unit", "decode_at", "decode_one", "imm_defs", "classify_last", "ItState",
           "FACT_FIELDS", "ENV_OF_FIELD", "FIELD_OF_ENV", "thumb_expand_imm", "RETURN_CLASSES",
           "is_return_insn"]

FLAGS = ("NF", "ZF", "CF", "VF")
FACT_FIELDS = tuple(f"r{i}" for i in range(13)) + FLAGS
ENV_OF_FIELD = {**{f"r{i}": f"regs[{i}]" for i in range(13)}, **{f: f for f in FLAGS}}
FIELD_OF_ENV = {v: k for k, v in ENV_OF_FIELD.items()}
_ALIASES = {"sb": "r9", "sl": "r10", "fp": "r11", "ip": "r12", "r13": "sp", "r14": "lr",
            "r15": "pc"}
RETURN_CLASSES = ("bx_lr", "pop_pc", "ldm_sp_pc", "ldr_pc_sp")


@lru_cache(maxsize=1)
def _md() -> capstone.Cs:
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    md.detail = True
    return md


def _reg(ci: capstone.CsInsn, r: int) -> str:
    n = ci.reg_name(r) or "?"
    return _ALIASES.get(n, n)


def thumb_expand_imm(imm12: int) -> tuple[int, bool]:
    """ARM ARM ThumbExpandImm: ``(value, rotated)``; ``rotated`` means the carry
    out is bit 31 of the value (an S variant then writes C)."""
    imm8 = imm12 & 0xff
    if imm12 >> 10 == 0:
        sel = (imm12 >> 8) & 3
        if sel == 0:
            return imm8, False
        if sel == 1:
            return (imm8 << 16) | imm8, False
        if sel == 2:
            return (imm8 << 24) | (imm8 << 8), False
        return (imm8 << 24) | (imm8 << 16) | (imm8 << 8) | imm8, False
    unrot = 0x80 | (imm12 & 0x7f)
    rot = (imm12 >> 7) & 0x1f
    return ((unrot >> rot) | (unrot << (32 - rot))) & 0xffffffff, True


@dataclass(frozen=True, slots=True)
class GInsn:
    index: int
    vaddr: int
    size: int
    raw: bytes
    mnemonic: str
    op_str: str
    writes: frozenset[str]  # guest registers and flag words written (over-approximated)
    cc: int  # capstone condition (15 = AL)
    condexec: int | None  # Tier-0 words[1] at the insn start (None: no metadata)
    is_it: bool
    it_cond_mask: int | None
    mem_base: str | None
    writeback: bool

    @property
    def text(self) -> str:
        return f"{self.mnemonic} {self.op_str}".strip()

    @property
    def in_it(self) -> bool:
        if self.condexec is not None:
            return (self.condexec & 0xf) != 0
        return self.cc not in (15, 0)

    @property
    def conditional(self) -> bool:
        """Executes only under a condition (inside an IT block with a non-AL cond)."""
        if self.condexec is not None:
            return (self.condexec & 0xf) != 0 and (self.condexec >> 4) < 0xe
        return self.cc not in (15, 0)

    def to_json(self) -> dict:
        return {"index": self.index, "vaddr": f"0x{self.vaddr:x}", "text": self.text,
                "size": self.size}


def _ginsn(ci: capstone.CsInsn, raw: bytes, index: int, w1: int | None) -> GInsn:
    _, wr = ci.regs_access()
    writes = {_reg(ci, r) for r in wr}
    mem_base = None
    for op in ci.operands:
        if op.type == A.ARM_OP_REG and op.access & capstone.CS_AC_WRITE:
            writes.add(_reg(ci, op.reg))
        if op.type == A.ARM_OP_MEM:
            mem_base = _reg(ci, op.mem.base) if op.mem.base else None
    if ci.writeback and mem_base:
        writes.add(mem_base)
    if ci.mnemonic.startswith(("ldm", "stm")) and ci.writeback and ci.operands and \
            ci.operands[0].type == A.ARM_OP_REG:
        writes.add(_reg(ci, ci.operands[0].reg))  # ldm/stm Rn!, {...}
    flags = (ci.update_flags or "cpsr" in writes or "apsr" in writes or
             ci.mnemonic.startswith("msr") or ci.mnemonic in ("cmp", "cmn", "tst", "teq"))
    writes -= {"cpsr", "apsr", "itstate"}
    if flags:
        writes |= set(FLAGS)
    is_it = ci.mnemonic.startswith("it") and ci.size == 2
    icm = None
    if is_it:
        hw = int.from_bytes(raw[0:2], "little")
        if hw & 0xff00 == 0xbf00 and hw & 0xf:
            icm = hw & 0xff
        else:
            is_it = False
    return GInsn(index, ci.address, ci.size, bytes(raw[:ci.size]), ci.mnemonic, ci.op_str,
                 frozenset(writes), ci.cc, w1, is_it, icm, mem_base, bool(ci.writeback))


def decode_one(code: bytes, vaddr: int) -> GInsn | None:
    """The single Thumb instruction at the start of ``code`` (None if undecodable)."""
    for ci in _md().disasm(code[:4], vaddr, count=1):
        return _ginsn(ci, code[:ci.size], 0, None)
    return None


def decode_at(code: bytes, vaddr: int, *, words1: Sequence[int | None] | None = None,
              lengths: Sequence[int] | None = None) -> list[GInsn] | None:
    """Decode ``code`` (exactly the guest bytes) as consecutive Thumb instructions.

    With ``lengths`` (from the insn_start words) the decode must agree with them
    (else None). ``words1[i]`` is the Tier-0 condexec value of insn ``i``.
    """
    out: list[GInsn] = []
    off = 0
    cis = list(_md().disasm(code, vaddr))
    if sum(c.size for c in cis) != len(code):
        return None
    for i, ci in enumerate(cis):
        if lengths is not None and (i >= len(lengths) or lengths[i] != ci.size):
            return None
        w1 = None if words1 is None or i >= len(words1) else words1[i]
        out.append(_ginsn(ci, code[off:off + ci.size], i, w1))
        off += ci.size
    if lengths is not None and len(out) != len(lengths):
        return None
    return out


def decode_unit(unit, code: bytes | None, page_bits: int = 10) -> list[GInsn] | None:
    """Decode a TB unit's guest bytes, joined with its insn rows (None if unavailable
    or if the decode disagrees with the Tier-0 instruction lengths)."""
    from .thumb import lengths_from_words

    if code is None or not unit.vaddrs:
        return None
    rows = sorted(unit.insns, key=lambda r: r.index)
    w0 = [r.words[0] for r in rows]
    lens = lengths_from_words(w0, unit.guest_size, page_bits) if rows else None
    w1 = [r.words[1] for r in rows] if rows else None
    return decode_at(code, unit.vaddrs[0], words1=w1, lengths=lens)


def imm_defs(g: GInsn) -> dict[str, int | None] | None:
    """``{field: value}`` written by an immediate move ``g``, or None if ``g`` is not
    one of the recognised unconditional forms. Flags only where QEMU writes the
    result to NF/ZF. A value of None means "written, but not a literal of the
    encoding" (a rotated modified immediate with S also writes CF)."""
    if g.conditional:
        return None
    b = g.raw
    if g.size == 2:
        hw = int.from_bytes(b, "little")
        if hw & 0xf800 == 0x2000:  # MOVS/MOV<c> Rd, #imm8 (T1)
            rd, imm = (hw >> 8) & 7, hw & 0xff
            out: dict[str, int | None] = {f"r{rd}": imm}
            if not g.in_it:
                out.update(NF=imm, ZF=imm)
            return out
        return None
    hw1 = int.from_bytes(b[0:2], "little")
    hw2 = int.from_bytes(b[2:4], "little")
    if hw2 & 0x8000:
        return None
    rd = (hw2 >> 8) & 0xf
    if rd > 12:
        return None
    if hw1 & 0xfbef in (0xf04f, 0xf06f):  # MOV{S}.W / MVN{S} Rd, #const (T2 / T1)
        imm12 = ((hw1 >> 10) & 1) << 11 | ((hw2 >> 12) & 7) << 8 | (hw2 & 0xff)
        val, rotated = thumb_expand_imm(imm12)
        if hw1 & 0xfbef == 0xf06f:
            val = ~val & 0xffffffff
        out: dict[str, int | None] = {f"r{rd}": val}
        if (hw1 >> 4) & 1:
            out.update(NF=val, ZF=val)
            if rotated:
                out["CF"] = None
        return out
    if hw1 & 0xfbf0 == 0xf240:  # MOVW Rd, #imm16 (T3)
        imm16 = (hw1 & 0xf) << 12 | ((hw1 >> 10) & 1) << 11 | ((hw2 >> 12) & 7) << 8 | (hw2 & 0xff)
        return {f"r{rd}": imm16}
    return None


def is_return_insn(g: GInsn) -> bool:
    return classify_last(g)[0] == "return"


def classify_last(g: GInsn) -> tuple[str, str]:
    """Exit class of a TB's last instruction (§3.1 rule 2):
    ``(return | static | indirect_multiway | fallthrough, detail)``."""
    m = g.mnemonic.split(".")[0]
    ops = g.op_str.replace(" ", "")
    if m == "bx":
        return ("return", "bx_lr") if ops == "lr" else ("indirect_multiway", "bx_reg")
    if m == "blx":
        return ("indirect_multiway", "blx_reg") if not ops.startswith("#") else ("static", "blx")
    if m in ("tbb", "tbh"):
        return "indirect_multiway", m
    if m == "pop" and "pc" in g.writes:
        return "return", "pop_pc"
    if m.startswith("ldm") and "pc" in g.writes:
        base = ops.split(",", 1)[0].rstrip("!")
        base = _ALIASES.get(base, base)
        if base == "sp" and g.writeback:
            return "return", "ldm_sp_pc"
        return "indirect_multiway", "ldm_rX_pc"
    if m.startswith("ldr") and "pc" in g.writes:
        # ldr pc, [sp], #k (post-indexed) is a return; anything else loads a computed target
        if g.mem_base == "sp" and g.writeback and "]," in ops:
            return "return", "ldr_pc_sp"
        return "indirect_multiway", "ldr_pc_rX"
    if m == "bl":
        return "static", "bl"
    if m in ("b", "cbz", "cbnz") or (m.startswith("b") and len(m) == 3 and m not in
                                     ("bic", "bfc", "bfi", "bkpt")):
        return "static", m
    if "pc" in g.writes:
        return "indirect_multiway", f"pc_write:{m}"
    return "fallthrough", m


class ItState:
    """QEMU's DisasContext ``condexec_cond`` / ``condexec_mask`` (M-profile, no ECI)."""

    __slots__ = ("cond", "mask", "eci")

    def __init__(self, condexec: int):
        self.cond = self.mask = self.eci = 0
        if condexec & 0xf:
            self.mask = (condexec & 0xf) << 1
            self.cond = condexec >> 4
        else:
            self.eci = condexec >> 4

    @property
    def bits(self) -> int:
        """The value ``arm_tr_insn_start`` records in words[1]."""
        if self.eci:
            return self.eci << 4
        return (self.cond << 4) | (self.mask >> 1)

    def step(self, g: GInsn) -> None:
        """State after translating ``g`` (``trans_IT``, then the advance)."""
        self.eci = 0
        if g.is_it and g.it_cond_mask is not None:
            self.cond = (g.it_cond_mask >> 4) & 0xe
            self.mask = g.it_cond_mask & 0x1f
        if self.mask:
            self.cond = (self.cond & 0xe) | ((self.mask >> 4) & 1)
            self.mask = (self.mask << 1) & 0x1f
            if self.mask == 0:
                self.cond = 0
