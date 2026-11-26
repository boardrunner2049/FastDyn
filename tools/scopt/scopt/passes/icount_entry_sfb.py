"""``icount-entry-sfb@1``: a fix to baseline TCG code shape (LLVM survey §3c B4-1). WP25.

**Not a B3 or B4 optimization.** TCG's TB entry (``gen_tb_start``, translator.c:43-102,
design G14) is::

    mov r32, dword [rbp-8]      ; icount_decr.u32 (32-bit load)
    add r32, -n | dec r32
    test r32, r32
    jl  exit_request
    mov word [rbp-8], r16       ; icount_decr.u16.low (16-bit store)

On a chained edge the 32-bit load follows the predecessor's 16-bit store to the
same address, which the store buffer cannot forward on Intel cores (a store
forwarding stall on every TB entry). WP16 measured the fix at +10.8 %
[+10.1, +11.6] on compute (the maintainer's WP16 probe report, placement control −0.1 %).
This pass rewrites the entry into a forwardable equivalent::

    movzx r32, word [rbp-8]     ; u16.low  (16-bit load: forwards from the 16-bit store)
    cmp   word [rbp-6], 0       ; u16.high
    jne   exit_request
    sub   r32, n
    jb    exit_request
    mov   word [rbp-8], r16

padded to exactly ``old + 16`` bytes with longer encodings (no extra
instructions) so that every later byte shifts by 16 and keeps its 4/8/16-byte
alignment (goto_tb fields 4-aligned, pool 8-aligned).

Equivalence. ``icount_decr.u16.high`` is 0 or 0xFFFF (every writer:
cpu-common.c:90, tcg-accel-ops.c:107, cpu-exec.c:802, reset) and ``n ≤ 512``.
With high = 0 both forms exit iff ``low < n`` and otherwise store
``low − n``; with high = 0xFFFF both exit (``u32 − n`` stays negative; the
new form tests high first). The register holds ``low − n`` on the fall-through
in both forms. The checker (:func:`entry_semantics`) proves it per entry form
by **executing both entries natively** (ctypes + ``mmap(PROT_EXEC)``, the V18
method) for every ``low`` in ``[0, 65536)`` and ``high ∈ {0, 0xFFFF}``,
comparing the exit decision, the env bytes and the register.

Reporting rule (survey §3c, spec §8 "change one mechanism at a time"): this is
its own arm (``tcg-entry-sfb``), never counted as caching, B3 or B4. It breaks
design invariant 3 ("the entry check and the icount immediate are
byte-identical") on purpose and is therefore **not** part of any B3/B4
pipeline: it has its own pipeline (``pipelines/tcg-entry-sfb.toml``), its own
packager (:func:`package_sfb`) and its own validation rows (``SFB*``).

Edit representation. The frozen typed payloads (design §3.7) have no kind for a
uniform prefix growth and PURE_REWRITE is same-length only, so the pass records
its proposal as an ANNOTATE edit (key :data:`ANNOTATION_KEY`: register,
``n``, old length, new prefix bytes) — a B2 kind that today's manager runs —
and :func:`package_sfb` materializes it: the new prefix, every later byte
shifted by +16, references from the shifted part to the own TB struct and
outside the unit re-encoded (−16), search rows ``host_end + 16``, goto_tb slot
offsets +16, the unit moved to the arena end when it no longer fits its gap,
and the derived ``.scm`` shifted so that the artifact re-imports with full
Tier-0 metadata (B1 cross-check, §2.7).

Reason codes (:class:`SfbReason`): ``cpu_noirq_shape`` (CF_NOIRQ units have no
``jl``), ``not_icount`` (no CF_USE_ICOUNT), ``entry_shape`` (not the 5-insn form),
``unit_not_relocatable``, ``ref_into_prefix``, ``unfixed_external_ref``,
``rel8_out_of_region``, ``abs_into_code``, ``sfb_no_encoding``,
``place_out_of_range``, ``sfb_semantics_mismatch`` (failed),
``sfb_body_mismatch`` (failed).
"""

from __future__ import annotations

import ctypes
import itertools
import mmap
import os
import struct
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..canon import canonical_json, fnv1a64, hexaddr, sha256_id
from ..decode.search import SearchRow, decode_search, encode_search, is_pcrel
from ..decode.x86 import sweep
from ..formats.sc_records import ScRecord
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult

__all__ = ["IcountEntrySfb", "PASSES", "SFB_SHIFT", "SfbReason", "EntryShape", "parse_entry",
           "encode_sfb_entry", "check_sfb_entry", "entry_semantics", "SfbUnit", "sfb_rebuild",
           "SfbLink", "link_sfb", "validate_sfb", "package_sfb", "propose_sfb", "ANNOTATION_KEY",
           "LABEL", "CF_USE_ICOUNT", "CF_NOIRQ"]

SFB_SHIFT = 16
ANNOTATION_KEY = "x_icount_entry_sfb"
LABEL = "baseline_tcg_fix_icount_entry_sfb"
CF_USE_ICOUNT = 0x00002000
CF_NOIRQ = 0x00010000
SLOT_FREE = 0xFFFF
MAX_N = 512


class SfbReason:
    CPU_NOIRQ_SHAPE = "cpu_noirq_shape"
    NOT_ICOUNT = "not_icount"
    ENTRY_SHAPE = "entry_shape"
    UNIT_NOT_RELOCATABLE = "unit_not_relocatable"
    REF_INTO_PREFIX = "ref_into_prefix"
    UNFIXED_EXTERNAL_REF = "unfixed_external_ref"
    REL8_OUT_OF_REGION = "rel8_out_of_region"
    ABS_INTO_CODE = "abs_into_code"
    NO_ENCODING = "sfb_no_encoding"
    PLACE_OUT_OF_RANGE = "place_out_of_range"
    SEMANTICS_MISMATCH = "sfb_semantics_mismatch"
    BODY_MISMATCH = "sfb_body_mismatch"
    FAILED = frozenset({"sfb_semantics_mismatch", "sfb_body_mismatch"})


class SfbError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


# ---------------------------------------------------------------------------
# the entry
# ---------------------------------------------------------------------------

REG32 = {"eax": 0, "ecx": 1, "edx": 2, "ebx": 3, "esp": 4, "ebp": 5, "esi": 6, "edi": 7,
         **{f"r{n}d": n for n in range(8, 16)}}
REG16 = {"ax": "eax", "cx": "ecx", "dx": "edx", "bx": "ebx", "sp": "esp", "bp": "ebp",
         "si": "esi", "di": "edi", **{f"r{n}w": f"r{n}d" for n in range(8, 16)}}


@dataclass(frozen=True)
class EntryShape:
    reg: str  # 32-bit register name
    n: int  # icount decrement
    length: int  # bytes of the 5-insn entry
    exit_target: int  # tc-relative target of the jl
    jl_field: int  # tc-relative offset of the jl rel32 field


def parse_entry(u: Any) -> EntryShape | None:
    """The 5-insn icount entry of ``u`` (None: another shape, e.g. CF_NOIRQ)."""
    end = min(u.insn0_start, len(u.code))
    ins = sweep(u.code, 0, end)
    if len(ins) < 5:
        return None
    i0, i1, i2, i3, i4 = ins[:5]
    if not (i0.mnemonic == "mov" and i0.op_str.endswith("dword ptr [rbp - 8]")):
        return None
    r = i0.op_str.split(",")[0].strip()
    if r not in REG32 or r in ("esp", "ebp"):
        return None
    if i1.mnemonic == "dec" and i1.op_str == r:
        n = 1
    elif i1.mnemonic == "add" and i1.op_str.startswith(r + ","):
        n = -int(i1.op_str.split(",")[1], 0)
    elif i1.mnemonic == "sub" and i1.op_str.startswith(r + ","):
        n = int(i1.op_str.split(",")[1], 0)
    else:
        return None
    if not 0 < n <= MAX_N:
        return None
    if not (i2.mnemonic == "test" and i2.op_str == f"{r}, {r}"):
        return None
    if not (i3.mnemonic == "jl" and i3.size == 6 and i3.branch_target is not None):
        return None
    if not (i4.mnemonic == "mov" and i4.op_str.startswith("word ptr [rbp - 8],")):
        return None
    if REG16.get(i4.op_str.split(",")[1].strip()) != r:
        return None
    if i4.address + i4.size > u.insn0_start:
        return None  # the entry must lie inside the prefix [0, insn0_start)
    return EntryShape(r, n, i4.address + i4.size, i3.branch_target, i3.address + 2)


def _modrm_rbp(reg: int, disp: int, wide: bool) -> bytes:
    if wide or not -128 <= disp <= 127:
        return bytes([0x80 | (reg & 7) << 3 | 5]) + struct.pack("<i", disp)
    return bytes([0x40 | (reg & 7) << 3 | 5]) + struct.pack("<b", disp)


def encode_sfb_entry(reg: str, n: int, exit_target_new: int, total: int
                     ) -> tuple[bytes, tuple[int, int]]:
    """New entry of exactly ``total`` bytes and the offsets of its two rel32 fields
    (jne, jb). ``exit_target_new`` is in new code offsets."""
    r = REG32[reg]
    rex = 0x44 if r >= 8 else None  # REX.R for the modrm reg field

    def build(wm: bool, rex40: bool, wc: bool, c16: bool, ws: bool, s32: bool
              ) -> tuple[bytes, tuple[int, int]]:
        out = bytearray()
        p = bytes([rex]) if rex else (b"\x40" if rex40 else b"")
        out += p + b"\x0f\xb7" + _modrm_rbp(r, -8, wm)  # movzx r32, word [rbp-8]
        imm = b"\x00\x00" if c16 else b"\x00"
        out += b"\x66" + (b"\x81" if c16 else b"\x83") + _modrm_rbp(7, -6, wc) + imm
        out += b"\x0f\x85"  # jne rel32
        jne_at = len(out)
        out += b"\0\0\0\0"
        rb = bytes([0x41]) if r >= 8 else b""
        if s32 or n > 127:
            out += rb + b"\x81" + bytes([0xE8 | (r & 7)]) + struct.pack("<I", n)  # sub r32, imm32
        else:
            out += rb + b"\x83" + bytes([0xE8 | (r & 7)]) + bytes([n])  # sub r32, imm8
        out += b"\x0f\x82"  # jb rel32
        jb_at = len(out)
        out += b"\0\0\0\0"
        out += b"\x66" + (bytes([rex]) if rex else b"") + b"\x89" + _modrm_rbp(r, -8, ws)
        for at in (jne_at, jb_at):
            out[at:at + 4] = struct.pack("<i", exit_target_new - (at + 4))
        return bytes(out), (jne_at, jb_at)

    for combo in itertools.product((False, True), repeat=6):
        if combo[1] and rex:
            continue
        b, f = build(*combo)
        if len(b) == total:
            return b, f
    raise SfbError(SfbReason.NO_ENCODING, f"{reg} n={n} total={total}")


def check_sfb_entry(new: bytes, e: EntryShape, shift: int = SFB_SHIFT) -> list[str]:
    """Decode the new entry back and compare with the intended form."""
    ins = sweep(new, 0, len(new))
    if any(i.bad for i in ins) or len(ins) != 6:
        return ["sfb_decode"]
    m = [i.mnemonic for i in ins]
    probs = []
    if m != ["movzx", "cmp", "jne", "sub", "jb", "mov"]:
        probs.append(f"sfb_mnemonics:{m}")
    if ins[0].op_str != f"{e.reg}, word ptr [rbp - 8]":
        probs.append("sfb_movzx")
    if ins[1].op_str != "word ptr [rbp - 6], 0":
        probs.append("sfb_cmp")
    if int(ins[3].op_str.split(",")[1], 0) != e.n or ins[3].op_str.split(",")[0] != e.reg:
        probs.append(f"sfb_sub:{ins[3].op_str}")
    tgt = e.exit_target + shift
    if ins[2].branch_target != tgt or ins[4].branch_target != tgt:
        probs.append("sfb_target")
    r16 = next(k for k, v in REG16.items() if v == e.reg)
    if ins[5].op_str != f"word ptr [rbp - 8], {r16}":
        probs.append("sfb_store")
    return probs


# ---------------------------------------------------------------------------
# native equivalence of the two entries (V18 method)
# ---------------------------------------------------------------------------

class _Native:
    """One RWX page; ``load(code)`` returns ``f(env_ptr) -> uint64``."""

    def __init__(self, size: int = 1 << 14):
        self.m = mmap.mmap(-1, size, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
        self.addr = ctypes.addressof(ctypes.c_char.from_buffer(self.m))
        self.ftype = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_uint64)

    def load(self, code: bytes) -> Any:
        self.m.seek(0)
        self.m.write(code)
        return self.ftype(self.addr)


def _harness(entry: bytes, reg: str) -> bytes:
    """``f(env)``: rbp = env, rbx/r12-r15 saved; ``entry`` with every jcc rel32
    retargeted to the exit label. Fall-through stores the entry register to
    ``[rbp+8]`` (so its value is compared) and returns 0; the exit returns 1."""
    pro = bytes.fromhex("55" "53" "4154" "4155" "4156" "4157" "4889fd")
    r = REG32[reg]
    st = (b"\x44" if r >= 8 else b"") + b"\x89" + bytes([0x40 | (r & 7) << 3 | 5]) + b"\x08"
    ft = st + bytes.fromhex("31c0") + b"\xeb\x05"
    ex = bytes.fromhex("b801000000")
    epi = bytes.fromhex("415f415e415d415c5b5dc3")
    body = bytearray(entry)
    exit_off = len(body) + len(ft)
    for i in sweep(bytes(body), 0):
        if i.mnemonic.startswith("j") and i.size == 6 and i.mnemonic != "jmp":
            fo = i.address + 2
            body[fo:fo + 4] = struct.pack("<i", exit_off - (fo + 4))
    return pro + bytes(body) + ft + ex + epi


def entry_semantics(old: bytes, new: bytes, reg: str, *, lows: Iterable[int] | None = None
                    ) -> list[str]:
    """Execute ``old`` and ``new`` natively for every ``low`` (default all 65536) and
    ``high ∈ {0, 0xFFFF}``; compare exit decision, env ``[-8, +12)`` and the register."""
    nat = _Native()
    env = ctypes.create_string_buffer(32)  # env-8 is buffer offset 0
    fo = nat.load(_harness(old, reg))
    probs: list[str] = []
    rows_old: list[tuple] = []
    los = list(range(65536)) if lows is None else list(lows)
    base = ctypes.addressof(env) + 8
    for high in (0, 0xFFFF):
        for low in los:
            ctypes.memset(env, 0xA5, 32)
            struct.pack_into("<HH", env, 0, low, high)
            took = fo(base)
            rows_old.append((took, env.raw))
    fn = nat.load(_harness(new, reg))
    k = 0
    for high in (0, 0xFFFF):
        for low in los:
            ctypes.memset(env, 0xA5, 32)
            struct.pack_into("<HH", env, 0, low, high)
            took = fn(base)
            if (took, env.raw) != rows_old[k]:
                probs.append(f"high={high:#x} low={low}: old {rows_old[k][0]} new {took}")
                if len(probs) > 5:
                    return probs
            k += 1
    return probs


# ---------------------------------------------------------------------------
# unit rebuild (+16 after the entry)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SfbUnit:
    uid: str
    shape: EntryShape
    prefix: bytes
    jfields: tuple[int, int]  # new code offsets of the jne/jb rel32 fields
    unit: Any  # TBUnit at the original tb_offset (new code/search/record/fixups)
    old: Any  # the input TBUnit

    @property
    def shift(self) -> int:
        return len(self.prefix) - self.shape.length


def _code_refs(u: Any) -> list[tuple[int, int, int, int]]:
    """``(insn_addr, field_off, target_off, width)`` of every rel/RIP field in ``[0, pool)``;
    targets relative to tc (signed)."""
    end = u.pool_start if u.pool_start is not None else u.tc_size
    out = []
    sx = lambda v: v - (1 << 64) if v >= 1 << 63 else v  # noqa: E731
    for i in sweep(u.code, 0, end):
        if i.bad:
            raise SfbError(SfbReason.UNIT_NOT_RELOCATABLE, f"undecodable @{i.address:#x}")
        if i.branch_target is not None and i.imm_size:
            out.append((i.address, i.address + i.imm_offset, sx(i.branch_target), i.imm_size))
        elif i.rip_mem is not None:
            out.append((i.address, i.address + i.disp_offset, sx(i.rip_target), i.disp_size))
    return out


def sfb_rebuild(u: Any, code_address: int) -> SfbUnit:
    """The SFB form of ``u`` at its own ``tb_offset`` (raises :class:`SfbError`)."""
    R = SfbReason
    if u.key.cflags & CF_NOIRQ:
        raise SfbError(R.CPU_NOIRQ_SHAPE, f"cflags {u.key.cflags:#x}")
    if not u.key.cflags & CF_USE_ICOUNT:
        raise SfbError(R.NOT_ICOUNT, f"cflags {u.key.cflags:#x}")
    if u.meta_status != "ok" or not u.reloc.relocatable:
        raise SfbError(R.UNIT_NOT_RELOCATABLE, u.meta_status)
    e = parse_entry(u)
    if e is None:
        raise SfbError(R.ENTRY_SHAPE)
    D = SFB_SHIFT
    pl = e.length
    prefix, jf = encode_sfb_entry(e.reg, e.n, e.exit_target + D, pl + D)
    probs = check_sfb_entry(prefix, e, D)
    if probs:
        raise SfbError(R.SEMANTICS_MISMATCH, ",".join(probs))
    code = bytearray(u.code)
    fix_fields = {f.field_off for f in u.reloc.fixups}
    struct_span = u.tc_offset - u.tb_offset
    for insn, fo, tgt, width in _code_refs(u):
        if insn < pl:
            continue  # the replaced entry
        intra = 0 <= tgt < u.tc_size
        if intra and tgt >= pl:
            continue  # both ends shift by D
        if intra:
            raise SfbError(R.REF_INTO_PREFIX, f"@{insn:#x} -> {tgt:#x}")
        if width != 4:
            raise SfbError(R.REL8_OUT_OF_REGION, f"@{insn:#x}")
        own_struct = -struct_span <= tgt < 0
        if not own_struct and fo not in fix_fields:
            raise SfbError(R.UNFIXED_EXTERNAL_REF, f"@{insn:#x} -> {tgt:#x}")
        v = int.from_bytes(code[fo:fo + 4], "little", signed=True) - D
        code[fo:fo + 4] = v.to_bytes(4, "little", signed=True)
    tc_abs = code_address + u.tc_offset
    for p in u.pool:
        if tc_abs <= p.data0 < tc_abs + u.tc_size:
            raise SfbError(R.ABS_INTO_CODE, f"pool slot {p.slot_off:#x}")
    for f in u.reloc.fixups:
        if f.field_off < pl:
            raise SfbError(R.REF_INTO_PREFIX, f"fixup @{f.field_off:#x} in the entry")
        if f.rule == "abs_plus_delta" and f.target_abs is not None and \
                tc_abs <= f.target_abs < tc_abs + u.tc_size:
            raise SfbError(R.ABS_INTO_CODE, f"fixup @{f.field_off:#x}")
    new_code = prefix + bytes(code[pl:])
    rec = ScRecord.from_bytes(u.record)
    pcrel = is_pcrel(rec.cflags)
    rows = decode_search(u.search, u.icount, pcrel)
    search = bytearray(encode_search([SearchRow(r.words, r.host_end + D) for r in rows], pcrel))
    end = u.tc_offset + len(new_code) + len(search)
    search += b"\0" * ((-end) % 16)
    jri, jii = list(rec.jmp_reset_offset), list(rec.jmp_insn_offset)
    for k in (0, 1):
        if jii[k] != SLOT_FREE:
            jii[k] += D
        if jri[k] != SLOT_FREE:
            jri[k] += D
    nrec = rec.replace(tc_size=len(new_code), end_offset=u.tc_offset + len(new_code) + len(search),
                       jmp_reset_offset=tuple(jri), jmp_insn_offset=tuple(jii))
    raw = nrec.to_bytes(fix_checksum=True)
    fixups = tuple(replace(f, field_off=f.field_off + D) for f in u.reloc.fixups)
    tag = canonical_json({"sfb_from": u.content_hash, "prefix": prefix.hex()})
    nu = replace(u, code=new_code, search=bytes(search), record=raw, tc_size=len(new_code),
                 end_offset=nrec.end_offset, reloc=replace(u.reloc, fixups=fixups),
                 content_hash=sha256_id(tag))
    return SfbUnit(u.uid, e, prefix, jf, nu, u)


# ---------------------------------------------------------------------------
# link + derived captures
# ---------------------------------------------------------------------------

@dataclass
class SfbLink:
    ok: bool
    header: Any
    units: tuple[Any, ...] = ()  # UnitLink per unit, slot order
    rebuilt: Mapping[str, SfbUnit] = field(default_factory=dict)
    moved: tuple[str, ...] = ()
    failure: tuple[str, str] | None = None
    derived_scms: tuple[tuple[str, bytes], ...] = ()

    @property
    def max_end(self) -> int:
        return max((ScRecord.from_bytes(x.record).end_offset for x in self.units), default=0)


def _shift_chunk(tb: Any, su: SfbUnit, code_address: int) -> Any:
    """The TB chunk of ``su`` at the input ``tb_offset`` (no move yet)."""
    from ..formats.enums import NONE_U32, Encoding, SiteKind

    D = su.shift
    pl = su.shape.length
    u = su.unit
    old = ScRecord.from_bytes(tb.record)
    tc_abs = code_address + old.tc_offset
    lo, hi = tc_abs + pl, tc_abs + old.tc_size
    code = u.code

    def ff(fo: int, w: int) -> int:
        return int.from_bytes(code[fo:fo + min(w, 4)], "little") if w > 0 else 0

    sites = []
    for s in tb.sites:
        if s.field_off < pl:
            # the entry's jl LABEL_JCC site becomes the jne and jb sites
            if s.kind != int(SiteKind.LABEL_JCC) or s.width != 4:
                raise SfbError(SfbReason.ENTRY_SHAPE, f"unexpected entry site kind {s.kind}")
            for jf in su.jfields:
                sites.append(replace(s, field_off=jf, insn_off=jf - 2, value=s.value + D,
                                     final_field=ff(jf, 4), enc=int(Encoding.REL32)))
            continue
        v = s.value + D if lo <= s.value < hi else s.value
        nf = s.field_off + D
        sites.append(replace(s, field_off=nf, insn_off=s.insn_off + D, value=v,
                             final_field=ff(nf, s.width)))
    sites.sort(key=lambda s: (s.field_off, s.insn_off))
    insns = tuple(replace(i, host_start=i.host_start + D, host_end=i.host_end + D)
                  for i in tb.insns)
    pool = tuple(replace(p, user_field_off=p.user_field_off + D, slot_off=p.slot_off + D)
                 for p in tb.pool)

    def sh(x: int) -> int:
        return x if x == NONE_U32 else x + D

    ldst = tuple(replace(x, jcc_field=tuple(sh(j) for j in x.jcc_field),
                         slow_start=x.slow_start + D, slow_end=x.slow_end + D,
                         raddr_off=x.raddr_off + D) for x in tb.ldst)
    opmarks = tuple(replace(m, host_off=m.host_off + D) for m in tb.opmarks)
    return replace(tb, record=bytes(u.record), code_fnv64=fnv1a64(u.code + u.search),
                   insn0_start=tb.insn0_start + D, body_end=tb.body_end + D,
                   ldst_end=tb.ldst_end + D, pool_start=sh(tb.pool_start),
                   code_size=u.tc_size, insns=insns, sites=tuple(sites), pool=pool, ldst=ldst,
                   opmarks=opmarks, source=None)


def _placement_limits(units: Sequence[Any]) -> dict[str, int]:
    """Largest permitted in-place end for each unit before any are moved."""
    from ..link import round_up

    ordered = sorted(units, key=lambda unit: unit.tb_offset)
    if not ordered:
        return {}
    ends = [unit.tb_offset for unit in ordered[1:]]
    ends.append(round_up(max(unit.end_offset for unit in ordered), 64))
    return {unit.uid: end for unit, end in zip(ordered, ends)}


def link_sfb(snapshot: Any, rebuilt: Mapping[str, SfbUnit], *, derive: bool = True) -> SfbLink:
    """Place every rebuilt unit (in place when it still ends before the next unit's
    struct, else at the arena end), relocate it, and derive the captures."""
    from ..formats import scm_v1 as S
    from ..link import (LinkError, _rewrite_tb, arena_limit, capture_join, relocate_unit,
                        round_up)

    h = snapshot.header
    units = list(snapshot.units())
    limits = _placement_limits(units)
    arena_end = max(limits.values(), default=0)
    cursor = arena_end
    limit = arena_limit(h)
    links = []
    moved = []
    try:
        for slot, u in enumerate(units):
            su = rebuilt.get(u.uid)
            e = su.unit if su is not None else u
            new_tb = u.tb_offset
            if su is not None:
                ln = e.end_offset - e.tb_offset
                if e.end_offset > limits[u.uid]:
                    new_tb = round_up(cursor, 64)
                    cursor = new_tb + ln
                    moved.append(u.uid)
            lu = relocate_unit(e, new_tb)
            links.append(replace(lu, slot=slot, input_slot=u.slot))
    except LinkError as ex:
        return SfbLink(False, h, failure=(ex.reason, ex.detail))
    if cursor >= limit:
        return SfbLink(False, h, failure=(SfbReason.PLACE_OUT_OF_RANGE,
                                          f"end {cursor:#x} >= {limit:#x}"))
    res = SfbLink(True, h, tuple(links), dict(rebuilt), tuple(moved))
    if not derive:
        return res
    jr = capture_join(snapshot)
    if jr is None:
        return res
    by_slot = {u.slot: (u, lk) for u, lk in zip(units, links)}
    ca = h.code_address
    files: list[tuple[str, bytes]] = []
    for f in jr.captures:
        bodies: list[Any] = []
        n_tb = 0
        for ch in f.chunks:
            b = ch.body
            if isinstance(b, S.TbChunk):
                sb = jr.slots.get(b.slot)
                if sb is None or not sb.bound or sb.source is not f or b.slot not in by_slot:
                    continue
                u, lk = by_slot[b.slot]
                if u.uid in rebuilt:
                    b = _shift_chunk(b, rebuilt[u.uid], ca)
                    # _rewrite_tb moves values in [tb, end) of the chunk's record by d
                bodies.append(_rewrite_tb(b, lk, ca))
                n_tb += 1
            elif isinstance(b, (S.RunInfo, S.HelperInfo, S.MapsChunk, S.RamblocksChunk,
                                S.EndChunk)):
                bodies.append(b)
        if not n_tb:
            continue
        name = "cache.scm" if not files else f"cache.{len(files)}.scm"
        files.append((name, S.encode_scm(S.make_scm(bodies, f.header))))
    res.derived_scms = tuple(files)
    return res


# ---------------------------------------------------------------------------
# validation (own rows; B1's V-rows assume same-length code)
# ---------------------------------------------------------------------------

def _norm_targets(ins: Sequence[Any], base_abs: int, code_len: int, shift_from: int, D: int,
                  struct_span: int = 192) -> list[tuple]:
    sx = lambda v: v - (1 << 64) if v >= 1 << 63 else v  # noqa: E731
    out = []
    for i in ins:
        t = []
        for get in (lambda x: x.branch_target, lambda x: x.rip_target):
            v = get(i)
            if v is None:
                t.append(None)
                continue
            v = sx(v)
            if shift_from <= v < code_len:
                t.append(("intra", v - D))
            elif 0 <= v < shift_from:
                t.append(("entry", v))
            elif -struct_span <= v < 0:
                t.append(("own_struct", v))  # the TB struct moves with the unit
            else:
                t.append(("abs", base_abs + v))
        out.append((i.mnemonic, tuple(t), i.size))
    return out


class SfbValidation:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, code: str, check: str, ok: bool | None, detail: str = "",
            reason: str | None = None, **extra: Any) -> bool:
        row = {"code": code, "check": check,
               "result": "skip" if ok is None else ("pass" if ok else "fail"),
               "detail": detail, **extra}
        if ok is False:
            row["reason"] = reason or check
        self.rows.append(row)
        return bool(ok)

    @property
    def accepted(self) -> bool:
        return not any(r["result"] == "fail" for r in self.rows)

    def first_reason(self) -> str | None:
        f = [r for r in self.rows if r["result"] == "fail"]
        return None if not f else f"{f[0]['code']}:{f[0].get('reason')}"

    def to_json(self, **extra: Any) -> dict[str, Any]:
        return {"schema": "scopt.sfb_validation", "version": 1,
                "verdict": "accepted" if self.accepted else "rejected", **extra,
                "rows": self.rows}


def validate_sfb(snapshot: Any, link: SfbLink, art_dir: str | os.PathLike, *,
                 native: bool = True, native_lows: Iterable[int] | None = None,
                 scparse: bool = True) -> tuple[SfbValidation, Any]:
    """The SFB rows on the files written to ``art_dir`` (see the module doc)."""
    from ..analysis.hcfg import build_hcfg
    from ..analysis.hlive import build_hlive
    from ..analysis.hostops import build_hostops
    from ..formats.envlayout_v1 import find_envlayout
    from ..formats.sc_records import read_sc
    from ..validate import reimport, scparse_compare

    v = SfbValidation()
    art = Path(art_dir)
    h = snapshot.header
    ca = h.code_address
    in_units = {u.uid: u for u in snapshot.units()}
    sc = read_sc(art / "cache.sc", strict=True)
    v.add("SFB1", "records", len(sc.valid_slots) == len(in_units),
          f"{len(sc.valid_slots)} valid records of {len(in_units)}")
    if scparse:
        ok, det = scparse_compare(art / "cache.sc", sc)
        v.add("SFB1", "second_parser", ok, det)
    # SFB2 entry semantics, per distinct (reg, n, old bytes, new bytes) form
    forms: dict[tuple, list[str]] = {}
    for uid, su in link.rebuilt.items():
        old = bytes(su.old.code[:su.shape.length])
        forms.setdefault((su.shape.reg, su.shape.n, _strip(old), _strip(su.prefix)),
                         []).append(uid)
    bad2 = []
    for (reg, n, old, new), uids in sorted(forms.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        su = link.rebuilt[uids[0]]
        p = check_sfb_entry(su.prefix, su.shape, su.shift)
        if not p and native:
            p = entry_semantics(bytes(su.old.code[:su.shape.length]), su.prefix, reg,
                                lows=native_lows)
        if p:
            bad2.append(f"{reg} n={n} ({len(uids)} units): {p[:2]}")
    v.add("SFB2", "entry_semantics", not bad2,
          f"{len(forms)} entry forms, {len(link.rebuilt)} units; decode check"
          + ("; native: every low in [0, 65536) x high in {0, 0xffff}" if native and
             native_lows is None else "; native: sampled lows" if native else "; native: off")
          if not bad2 else "; ".join(bad2[:4]), SfbReason.SEMANTICS_MISMATCH)
    # SFB3 body identity modulo the shift, SFB4 search rows, SFB5 record
    bad3, bad4, bad5 = [], [], []
    lk_by = {lk.uid: lk for lk in link.units}
    for uid, su in link.rebuilt.items():
        u, lk = su.old, lk_by[uid]
        rec = ScRecord.from_bytes(lk.record)
        orec = ScRecord.from_bytes(u.record)
        D, pl = su.shift, su.shape.length
        code = lk.body[:rec.tc_size]
        end_old = u.pool_start if u.pool_start is not None else u.tc_size
        oi = [i for i in sweep(u.code, 0, end_old) if i.address >= pl]
        ni = [i for i in sweep(code, 0, end_old + D) if i.address >= pl + D]
        span = u.tc_offset - u.tb_offset
        a = _norm_targets(oi, ca + u.tc_offset, u.tc_size, pl, 0, span)
        b = _norm_targets(ni, ca + rec.tc_offset, rec.tc_size, pl + D, D, span)
        if a != b or bytes(u.code[end_old:]) != bytes(code[end_old + D:]):
            bad3.append(uid)
        if code[:pl + D] != su.prefix:
            bad3.append(f"{uid}: prefix")
        try:
            ro = decode_search(u.search, u.icount, is_pcrel(orec.cflags))
            rn = decode_search(lk.body[rec.tc_size:], rec.icount, is_pcrel(rec.cflags))
            if [r.words for r in ro] != [r.words for r in rn] or \
                    [r.host_end + D for r in ro] != [r.host_end for r in rn]:
                bad4.append(uid)
        except Exception as ex:  # noqa: BLE001
            bad4.append(f"{uid}: {ex}")
        same = all(getattr(rec, k) == getattr(orec, k) for k in
                   ("pc", "cs_base", "flags", "cflags", "page_addr0", "page_addr1",
                    "guest_size", "icount"))
        jo = tuple(x + D if x != SLOT_FREE else x for x in orec.jmp_insn_offset)
        jr = tuple(x + D if x != SLOT_FREE else x for x in orec.jmp_reset_offset)
        if not same or rec.tc_size != orec.tc_size + D or rec.jmp_insn_offset != jo or \
                rec.jmp_reset_offset != jr or rec.end_offset % 16:
            bad5.append(uid)
    v.add("SFB3", "body_identity", not bad3,
          f"{len(link.rebuilt)} units: bytes after the entry decode to the same instructions "
          "with intra targets +16, own-struct and external targets unchanged; pool equal"
          if not bad3 else str(bad3[:5]), SfbReason.BODY_MISMATCH)
    v.add("SFB4", "search_rows", not bad4, "words equal, host_end + 16" if not bad4
          else str(bad4[:5]), "recovery_rows_changed")
    v.add("SFB5", "record", not bad5, "key, pc, flags, cflags, pages, guest size, icount "
          "equal; tc_size + 16; slot offsets + 16; end 16-aligned" if not bad5 else str(bad5[:5]))
    bad6 = [lk.uid for lk in link.units if lk.uid not in link.rebuilt and
            (lk.record != in_units[lk.uid].record or
             lk.body != in_units[lk.uid].code + in_units[lk.uid].search)]
    v.add("SFB6", "untouched_units", not bad6, f"{len(link.units) - len(link.rebuilt)} units "
          "byte-identical (record and body)" if not bad6 else str(bad6[:5]))
    # SFB7 re-import with the derived captures (B1 §2.7 cross-check of every unit)
    res = None
    try:
        res = reimport(art, snapshot)
    except Exception as ex:  # noqa: BLE001
        v.add("SFB7", "reimport", False, f"{type(ex).__name__}: {ex}", "reimport_failed")
    if res is not None:
        out = {u.uid: u for u in res.snapshot.units()}
        bad7 = []
        for uid, u in in_units.items():
            w = out.get(uid)
            if w is None:
                bad7.append(f"{uid}: missing")
                continue
            if (w.meta_status, w.reloc.relocatable) != (u.meta_status, u.reloc.relocatable):
                bad7.append(f"{uid}: {w.meta_status}/{w.reloc.relocatable} "
                            f"{list(w.reloc.reasons)[:2]}")
                continue
            su = link.rebuilt.get(uid)
            if su is not None:
                D = su.shift
                if w.insn0_start != u.insn0_start + D or w.icount != u.icount or \
                        [(i.host_start - D, i.host_end - D) for i in w.insns] != \
                        [(i.host_start, i.host_end) for i in u.insns]:
                    bad7.append(f"{uid}: metadata not shifted by {D}")
        v.add("SFB7", "reimport", not bad7, f"{len(out)} units re-imported; every unit keeps "
              "its meta status and relocatability; rebuilt units' insn ranges shifted by 16"
              if not bad7 else "; ".join(bad7[:5]), "reimport_status")
        # SFB8: flags dead and HostOps ok after the new entry (re-imported units)
        lay = find_envlayout((snapshot.runtime or {}).get("qemu_sha256"))
        bad8 = []
        if lay is not None:
            for uid in list(link.rebuilt)[:400]:
                w = out.get(uid)
                if w is None:
                    continue
                hh = build_hostops(w, lay, res.snapshot.header.code_address)
                if not hh.ok:
                    bad8.append(f"{uid}: hostops {hh.reasons[:2]}")
                    continue
                g = build_hcfg(hh)
                lv = build_hlive(hh, g)
                k = next((i for i, o in enumerate(hh.ops) if o.off >= su_len(link, uid)), None)
                if k is not None and lv.live_at(k) & {"CF", "ZF", "SF", "OF", "PF", "AF"}:
                    bad8.append(f"{uid}: flags live after the entry")
        v.add("SFB8", "hostops_after", None if lay is None else not bad8,
              "no EnvLayout" if lay is None else
              (f"HostOps ok and no flag live after the new entry ({min(400, len(link.rebuilt))}"
               " re-imported units)" if not bad8 else "; ".join(bad8[:5])))
    from ..link import arena_limit

    v.add("SFB9", "arena", link.max_end < arena_limit(h),
          f"max_end {link.max_end:#x} < {arena_limit(h):#x}; {len(link.moved)} units moved")
    return v, res


def su_len(link: SfbLink, uid: str) -> int:
    su = link.rebuilt[uid]
    return len(su.prefix)


def _strip(b: bytes) -> bytes:
    """Entry bytes without the jcc rel32 fields (the form, independent of the exit offset)."""
    out = bytearray(b)
    for i in sweep(bytes(b), 0):
        if i.mnemonic.startswith("j") and i.size == 6:
            out[i.address + 2:i.address + 6] = b"\0\0\0\0"
    return bytes(out)


# ---------------------------------------------------------------------------
# the pass (proposal as ANNOTATE edits) and the packager
# ---------------------------------------------------------------------------

class IcountEntrySfb:
    """Baseline-TCG fix: forwardable icount entry. Annotates; :func:`package_sfb` builds."""

    descriptor = PassDescriptor(
        name="icount-entry-sfb", version=1, unit_scope="snapshot",
        capabilities=frozenset({EditKind.ANNOTATE}), budgets=Budgets(max_wall_s=600.0),
        preserves=frozenset({"*"}), params_schema={"max_units": int})

    def run(self, snap: Any, ctx: Any, edits: Any) -> PassResult:
        return self._collect(snap, ctx, edits, {})

    def _collect(self, snap: Any, ctx: Any, edits: Any, skips: dict[str, str]) -> PassResult:
        ca = snap.header.code_address
        stats: Counter[str] = Counter()
        affected = []
        max_units = ctx.params.get("max_units")
        for u in snap.units():
            ctx.budget()
            try:
                su = sfb_rebuild(u, ca)
            except SfbError as e:
                skips[u.uid] = e.reason
                stats[f"skip:{e.reason}"] += 1
                continue
            if max_units is not None and len(affected) >= int(max_units):
                skips[u.uid] = "units_over_budget"
                stats["units_over_budget"] += 1
                continue
            edits.annotate(u.uid, ANNOTATION_KEY, {
                "reg": su.shape.reg, "n": su.shape.n, "old_len": su.shape.length,
                "shift": su.shift, "prefix": su.prefix.hex(), "label": LABEL,
                "rebuilt_end": su.unit.end_offset})
            affected.append(u.uid)
            stats["units"] += 1
        st = {k: float(v) for k, v in sorted(stats.items())}
        if not affected:
            return PassResult(Outcome.NO_CHANGE, "no_entry", "no unit has the icount entry",
                              stats=st)
        return PassResult(Outcome.CANDIDATE, "entry_rewrites",
                          f"{len(affected)} entries (label {LABEL}; not B3/B4)",
                          affected=tuple(affected), stats=st)


PASSES = (IcountEntrySfb,)


@dataclass
class SfbProposal:
    result: PassResult
    units: dict[str, dict[str, Any]]  # uid -> annotation value
    skips: dict[str, str]
    selection: dict[str, Any] = field(default_factory=dict)
    profiles: tuple[dict[str, Any], ...] = ()


def propose_sfb(snapshot: Any, *, max_units: int | None = None, profile: Any = None,
                phase: str = "steady", entry_coverage: int = 100,
                max_growth_bytes: int | None = None,
                max_moved_units: int | None = None) -> SfbProposal:
    """Run :class:`IcountEntrySfb` with an ANNOTATE-only builder."""
    from ..model.snapshot import WorkingSet
    from ..model.view import WorkingSetView
    from ..proposal import ProposalBuilder

    if not 1 <= entry_coverage <= 100 or any(n is not None and n < 0 for n in
                                              (max_growth_bytes, max_moved_units, max_units)):
        raise ValueError("entry_coverage must be 1..100 and budgets must be nonnegative")
    view_profile = None
    if profile is not None:
        from ..formats.profile_v1 import ProfileView

        view_profile = ProfileView.open(profile, snapshot, phase=phase)
        pid = profile.phase_id(phase)
        if pid is None or not profile.phase_reached(pid):
            raise ValueError(f"SFB profile phase {phase!r} is missing or unreached")
    elif entry_coverage != 100 or max_growth_bytes is not None or max_moved_units is not None:
        raise ValueError("SFB coverage and growth/movement budgets require a training profile")

    class _Ctx:
        params = {} if max_units is None or profile is not None else {"max_units": max_units}
        snapshot_id = snapshot.snapshot_id

        def budget(self) -> None:
            return None

        def recorded_reads(self) -> tuple:
            return ()

        def recorded_facts(self) -> tuple:
            return ()

    view = WorkingSetView(WorkingSet.initial(snapshot))
    p = IcountEntrySfb()
    b = ProposalBuilder(p.descriptor, view, enabled={EditKind.ANNOTATE}, ctx=_Ctx())
    skips: dict[str, str] = {}
    res = p._collect(view, _Ctx(), b, skips)
    units = {e.units[0]: dict(e.args["value"]) for e in b.edits}
    if view_profile is None:
        return SfbProposal(res, units, skips)

    counts = view_profile.counts(phase)
    if units and not any(counts[uid].exact for uid in units):
        raise ValueError(f"SFB requires exact entry counts for eligible units in phase {phase!r}")
    limits = _placement_limits(list(snapshot.units()))
    ranked = []
    for uid in units:
        count = counts[uid]
        if not count.exact:
            skips[uid] = f"count_{count.status}"
        elif not count.value:
            skips[uid] = "zero_entries"
        else:
            ranked.append((count.value, uid))
    total = sum(count for count, _ in ranked)
    selected = {}
    entries = growth = moves = 0
    for count, uid in sorted(ranked, key=lambda row: (-row[0], row[1])):
        ann = units[uid]
        moved = ann["rebuilt_end"] > limits[uid]
        if entries * 100 >= total * entry_coverage:
            skips[uid] = "coverage_reached"
        elif max_units is not None and len(selected) >= max_units:
            skips[uid] = "units_over_budget"
        elif max_growth_bytes is not None and growth + ann["shift"] > max_growth_bytes:
            skips[uid] = "code_growth_budget"
        elif max_moved_units is not None and moves + moved > max_moved_units:
            skips[uid] = "moved_units_budget"
        else:
            selected[uid] = count
            entries += count
            growth += ann["shift"]
            moves += moved

    policy = {"phase": phase, "entry_coverage": entry_coverage, "max_units": max_units,
              "max_growth_bytes": max_growth_bytes, "max_moved_units": max_moved_units}
    receipt = {"policy": policy, "profile_id": profile.profile_id,
               "input_snapshot_id": snapshot.snapshot_id, "eligible_exact_entries": total,
               "selected_entries": entries, "code_growth_bytes": growth, "moved_units": moves,
               "selected": selected, "excluded": skips}
    result = PassResult(Outcome.CANDIDATE if selected else Outcome.NO_CHANGE,
                        "entry_rewrites" if selected else "no_selected_entries",
                        f"{len(selected)} entries selected by exact {phase} activity",
                        affected=tuple(selected), stats={"units": len(selected),
                                                         "code_growth_bytes": growth})
    return SfbProposal(result, {uid: units[uid] for uid in selected}, skips,
                       receipt, (view_profile.verdict(),))


@dataclass
class SfbPackageResult:
    status: str
    candidate_id: str
    reason: str | None = None
    artifact_id: str | None = None
    path: str | None = None
    validation: SfbValidation | None = None
    link: SfbLink | None = None


def package_sfb(snapshot: Any, prop: SfbProposal, store: str | os.PathLike, *,
                candidate_id: str, native: bool = True,
                native_lows: Iterable[int] | None = None) -> SfbPackageResult:
    """Rebuild the annotated units, link, write a public SuperCache, validate (SFB1-SFB9)
    and publish into ``store`` (or write a rejection)."""
    from ..formats import artifact as A
    from ..formats.supercache import write_public_cache
    from ..ledger import Ledger

    ca = snapshot.header.code_address
    rebuilt: dict[str, SfbUnit] = {}
    by = {u.uid: u for u in snapshot.units()}
    led = Ledger()
    if not prop.units:
        return SfbPackageResult("no_change", candidate_id, prop.result.reason)

    def reject(reason: str, detail: str, val: Any = None, link: Any = None
               ) -> SfbPackageResult:
        led.emit("artifact", candidate_id, "accepted", "fail", reason)
        p = A.write_rejection(store, candidate_id, {
            "candidate_id": candidate_id, "reason": reason, "detail": detail,
            "validation": None if val is None else val.to_json(),
            "ledger": [r.to_json() for r in led.rows]})
        return SfbPackageResult("rejected", candidate_id, reason, path=str(p), validation=val,
                                link=link)

    for uid, ann in prop.units.items():
        try:
            su = sfb_rebuild(by[uid], ca)
        except SfbError as e:
            return reject(f"failed:{e.reason}", f"{uid}: {e.detail}")
        if su.prefix.hex() != ann.get("prefix"):
            return reject("stale_edit", f"{uid}: annotated prefix differs from the rebuild")
        rebuilt[uid] = su
    link = link_sfb(snapshot, rebuilt)
    if not link.ok:
        return reject(f"failed:{link.failure[0]}", link.failure[1], link=link)
    stage = A.staging_dir(store)
    try:
        write_public_cache(stage / "cache.sc", snapshot.header,
                         [(x.record, x.body) for x in link.units])
        for name, data in link.derived_scms:
            (stage / name).write_bytes(data)
        if snapshot.bundle is not None:
            A.write_json_file(stage / "bundle.json", dict(snapshot.bundle))
        if snapshot.runtime is not None:
            A.write_json_file(stage / "runtime_identity.json", dict(snapshot.runtime))
        val, res = validate_sfb(snapshot, link, stage, native=native, native_lows=native_lows)
        if not val.accepted:
            return reject(val.first_reason() or "validation", "", val, link)
        out_sid = res.snapshot.snapshot_id if res is not None else None
        # the sidecar schema's move_map rows (as LinkResult.move_map writes them)
        moves = {lk.uid: {"slot": lk.slot, "old_tb_offset": hexaddr(lk.old_tb_offset),
                          "new_tb_offset": hexaddr(lk.new_tb_offset), "delta": lk.delta}
                 for lk in link.units if lk.moved}
        vb = A.write_json_file(stage / A.VALIDATION, val.to_json(
            candidate_id=candidate_id, input_snapshot_id=snapshot.snapshot_id,
            output_snapshot_id=out_sid))
        sidecar = {
            "schema": "fastdyn.artifact_sidecar", "version": 1,
            "input_snapshot_id": snapshot.snapshot_id, "output_snapshot_id": out_sid,
            "bundle_id": None if snapshot.bundle is None else snapshot.bundle.get("bundle_id"),
            "runtime_identity_id": None if snapshot.runtime is None else
            snapshot.runtime.get("runtime_identity_id"),
            "profile_ids": list(prop.profiles), "pipeline": [{
                "pass": "icount-entry-sfb", "version": 1,
                "params_sha256": sha256_id(canonical_json(prop.selection.get("policy", {}))),
                "outcome": prop.result.outcome.value, "reason": prop.result.reason,
                "affected": sorted(prop.units), "edit_digest": None, "duration_s": 0.0}],
            "move_map": moves, "pinned": [], "validation_sha256": sha256_id(vb),
            "tool_versions": A.tool_versions(), "loader_requirement": A.LOADER_V7,
            "x_candidate_id": candidate_id, "x_labels": [LABEL],
            "x_sfb": {"units": len(rebuilt), "moved": len(link.moved),
                      "skips": dict(Counter(prop.skips.values()))}}
        if prop.selection:
            sidecar["x_sfb"]["selection"] = prop.selection
        errs = A.sidecar_errors(sidecar)
        if errs:  # `scopt publish` refuses such a sidecar; never write one
            return reject("sidecar_schema", "; ".join(errs[:3]), val, link)
        A.write_json_file(stage / A.SIDECAR, sidecar)
        if res is not None:
            (stage / A.REGIONS).write_bytes(b"".join(canonical_json(r) + b"\n"
                                                     for r in res.regions))
        A.write_json_file(stage / A.PROFILES, {"profiles": list(prop.profiles)})
        led.emit("artifact", candidate_id, "accepted", "pass", "SFB1-SFB9", sidecar[
            "validation_sha256"])
        led.write(stage / A.LEDGER)
        aid, final, _st = A.publish(stage, store)
        return SfbPackageResult("published", candidate_id, None, aid, str(final), val, link)
    finally:
        if stage.exists():
            A.remove_tree(stage)

