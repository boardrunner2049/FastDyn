"""Importer cross-check of one bound slot (design §2.7 steps 1-9). WP7.

:func:`crosscheck_unit` takes a valid ``.sc`` slot, the TB chunk bound to it
(:mod:`scopt.importer.join`) and the :class:`~scopt.importer.classify.HostContext`
of that chunk's capture, and runs every check of §2.7. It never stops at the
first problem: every failing check adds a reason (``code`` or ``code@0x<off>``
with the offset relative to the TB's first code byte), and any reason pins the
TB (fail closed, invariant 4). Each check also writes an evidence row.

Steps (§2.7):

1. **Bind extras** — ``FNV64(guest_bytes) == guest_hash``, ``code_size ==
   tc_size``, ``icount``, ``Insn[0].word[0] == vaddr & page_mask`` (CF_PCREL);
   the TB flags ``capture_overflow``/``capture_error`` make the metadata
   ``incomplete`` (pinned, later steps skipped).
2. **Layout** — ``tc_offset == tb_offset + ROUND_UP(tb_size, icache_linesize)``;
   ``insn0_start ≤ body_end ≤ ldst_end ≤ pool_start ≤ code_size``;
   ``[ldst_end, pool_start)`` is all 0x90; pool slots tile
   ``[pool_start, code_size)``, 8-aligned in absolute address, bytes equal to
   the captured data.
3. **Search data** — the ``icount`` sleb128 rows at ``tc+tc_size`` equal the
   ``Insn`` words and ``host_end``; consecutive insn ranges abut; the last
   ``host_end == body_end``; ``end_offset`` is the 16-aligned end of the table.
4. **Linear sweep** of ``[0, pool_start)`` with capstone (tiles exactly, no
   ``(bad)``); every site's ``insn_off`` is an instruction start and its field
   lies inside that instruction. Failure: ``decode_failed@off``.
5. **Two-sided reference matching** — every decoded reference (rel8/rel32
   branches and calls, every ``[rip+d32]``, every movabs, every imm32/disp32
   whose value is in the arena or a MAPS range, register-indirect branches)
   must match exactly one captured site (same field offset, width and
   instruction, an encoding consistent with the site's kind, equal value);
   otherwise ``unexplained_reference@off``. Every captured site must be found
   (``missing_reference@off``). Width-0 sites: GOTO_PTR matches a ``jmp reg``
   with the captured register; XOR0 is a ``xor r32, r32``. Each site's
   ``final_field`` must equal the bytes in the ``.sc``.
6. **Target classification** (table in §2.7) — produces the Fixups and the
   bindings; an unknown class pins.
7. **Recovery points** (:mod:`scopt.importer.recovery`).
8. **Exits** — the record's ``jmp_insn_offset``/``jmp_reset_offset`` equal the
   GOTO_TB sites; every EXIT_TB_RET_JMP(idx) is preceded by its
   MOVI_EXIT_TB_PTR(idx); ``flags.irq_check`` iff ``!(cflags & CF_NOIRQ)``.
9. **Guest** — the vaddr maps through the firmware ELF's PT_LOADs (bytes equal:
   ``elf_verified``; else ``ram_resident``; no firmware: ``unknown``), and a
   Thumb decode of the guest bytes gives the insn lengths implied by the
   consecutive ``Insn`` word0 values.
"""

from __future__ import annotations

import bisect
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..canon import fnv1a64
from ..decode.search import SearchDecodeError, SearchRows, decode_search, is_pcrel
from ..decode.thumb import check_lengths, lengths_from_words
from ..decode.x86 import Arena, DecodedRef, Insn as X86Insn, RefKind, extract_refs, sweep, tiles
from ..formats.enums import NONE_U32, Encoding, SiteKind, TbFlag
from ..formats.sc_v5 import ScHeader, ScUnit
from ..formats.scm_v1 import TbChunk
from ..model.unit import (AnchoredPointer, Fixup, HelperBinding, Insn, Ldst, OpMark,
                          PoolEntry, RecoveryPoint, Site)
from .classify import OWN_STRUCT_IDX, AddrClass, HostContext, helper_symbol_ok
from .recovery import RECOVERY_MISMATCH, RecoveryCheck, check_recovery

__all__ = [
    "CF_NOIRQ", "ClassifiedSite", "UnitCheck", "Firmware", "crosscheck_unit", "X86_REGS",
    "round_up",
]

CF_NOIRQ = 0x0001_0000
NOP = 0x90
SEARCH_ALIGN = 16  # CODE_GEN_ALIGN
X86_REGS = ("rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
            "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15")


def round_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a


# Decoded ref kinds a captured site of a given kind / encoding may correspond to.
_JMP = {RefKind.JMP_REL8, RefKind.JMP_REL32}
_JCC = {RefKind.JCC_REL8, RefKind.JCC_REL32}
_MOVI = {RefKind.RIP_LEA, RefKind.MOVABS, RefKind.IMM32}
_KIND_REFS: dict[SiteKind, set[RefKind]] = {
    SiteKind.LABEL_JMP: _JMP,
    SiteKind.LABEL_JCC: _JCC,
    SiteKind.LDST_FAST_JCC: _JCC,
    SiteKind.LDST_RET_JMP: _JMP | {RefKind.RIP_JMP},
    SiteKind.LDST_HELPER_CALL: {RefKind.CALL_REL32, RefKind.RIP_CALL},
    SiteKind.HELPER_CALL: {RefKind.CALL_REL32, RefKind.RIP_CALL},
    SiteKind.EXIT_TB_RET_JMP: _JMP | {RefKind.RIP_JMP},
    SiteKind.EXIT_TB_EPI_JMP: _JMP | {RefKind.RIP_JMP},
    SiteKind.MOVI_EXIT_TB_PTR: _MOVI,
    SiteKind.MOVI_LDST_RA: _MOVI,
    SiteKind.MOVI_CONST: _MOVI,
    SiteKind.POOL_REF: {RefKind.RIP_MEM, RefKind.RIP_LEA, RefKind.RIP_CALL, RefKind.RIP_JMP},
    SiteKind.GOTO_TB: {RefKind.JMP_REL32},
    SiteKind.GOTO_PTR: {RefKind.JMP_REG},
    SiteKind.OTHER_BRANCH: _JMP | _JCC | {RefKind.CALL_REL32, RefKind.RIP_CALL, RefKind.RIP_JMP},
}
_ENC_REFS: dict[Encoding, set[RefKind]] = {
    Encoding.REL8: {RefKind.JMP_REL8, RefKind.JCC_REL8},
    Encoding.REL32: {RefKind.JMP_REL32, RefKind.JCC_REL32, RefKind.CALL_REL32},
    Encoding.GOTO_TB_REL32: {RefKind.JMP_REL32},
    Encoding.POOL_INDIRECT: {RefKind.RIP_CALL, RefKind.RIP_JMP},
    Encoding.RIPREL32: {RefKind.RIP_LEA, RefKind.RIP_MEM, RefKind.RIP_CALL, RefKind.RIP_JMP},
    Encoding.IMM64: {RefKind.MOVABS},
    Encoding.IMM32_ZX: {RefKind.IMM32},
    Encoding.IMM32_SX: {RefKind.IMM32},
    Encoding.NONE: {RefKind.JMP_REG},
}


@dataclass(frozen=True, slots=True)
class ClassifiedSite:
    """One captured site after matching and classification (§2.7 step 6)."""

    index: int
    site: Site
    cls: str  # intra | own_struct+idx | prologue.* | ext.* | ext.ptr_riprel | pool | ...
    target: int | None  # absolute target / value the class is about
    binding: AddrClass | None = None  # what an absolute value points to
    fixup: Fixup | None = None
    problem: str | None = None  # a pin reason (None: classified)

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "i": self.index, "field_off": self.site.field_off, "kind": self.site.kind.name,
            "enc": self.site.enc.name, "width": self.site.width, "class": self.cls,
        }
        if self.target is not None:
            d["target"] = f"0x{self.target:x}"
        if self.binding is not None:
            d["binding"] = self.binding.cls
            d["anchor"] = self.binding.anchor
            if self.binding.symbol_text():
                d["symbol"] = self.binding.symbol_text()
        if self.fixup is not None:
            d["rule"] = self.fixup.rule
        if self.problem:
            d["problem"] = self.problem
        return d


@dataclass
class Firmware:
    """PT_LOAD contents of the guest firmware ELF (for §2.7 step 9)."""

    path: str
    segments: tuple[tuple[int, bytes], ...]

    @classmethod
    def load(cls, path: str) -> "Firmware":
        from elftools.elf.elffile import ELFFile

        segs: dict[int, bytes] = {}
        with open(path, "rb") as f:
            elf = ELFFile(f)
            for seg in elf.iter_segments():
                if seg["p_type"] == "PT_LOAD" and seg["p_filesz"]:
                    data = seg.data()
                    for base in (seg["p_vaddr"], seg["p_paddr"]):
                        segs.setdefault(base, data)
        return cls(path, tuple(sorted(segs.items())))

    def bytes_at(self, vaddr: int, n: int) -> bytes | None:
        for base, data in self.segments:
            if base <= vaddr and vaddr + n <= base + len(data):
                return data[vaddr - base: vaddr - base + n]
        return None


@dataclass
class UnitCheck:
    slot: int
    reasons: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    classified: list[ClassifiedSite] = field(default_factory=list)
    decoded: list[DecodedRef] = field(default_factory=list)
    recovery_checks: list[RecoveryCheck] = field(default_factory=list)
    recovery: tuple[RecoveryPoint, ...] = ()
    helpers: tuple[HelperBinding, ...] = ()
    unchecked: tuple[AnchoredPointer, ...] = ()
    origin: str = "unknown"
    meta_status: str = "ok"
    rows: SearchRows | None = None
    search_len: int | None = None
    arrays: dict[str, tuple] = field(default_factory=dict)
    helper_symbol_checks: list[tuple[int, str | None, str | None, bool | None]] = \
        field(default_factory=list)

    def check(self, name: str, ok: bool | None, detail: str = "",
              reason: str | None = None) -> bool:
        """Record an evidence row; on failure add ``reason`` (default: ``name``).

        ``reason=""`` records the row without adding a reason (the caller has
        already added more specific ones, e.g. ``code@0x<off>``).
        """
        res = "skip" if ok is None else ("pass" if ok else "fail")
        self.evidence.append({"check": name, "result": res, "detail": detail})
        if ok is False and reason != "":
            self.reasons.append(reason or name)
        return bool(ok)

    @property
    def fixups(self) -> tuple[Fixup, ...]:
        fx = [c.fixup for c in self.classified if c.fixup is not None]
        return tuple(sorted(fx, key=lambda f: f.field_off))

    def sorted_reasons(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.reasons)))


def _site_insn_off(s: Site) -> int | None:
    return None if s.insn_off == NONE_U32 else s.insn_off


def _field_bytes(code: bytes, off: int, width: int) -> int:
    n = min(width, 4)
    return int.from_bytes(code[off:off + n], "little")


def crosscheck_unit(u: ScUnit, chunk: TbChunk, hdr: ScHeader, ctx: HostContext, *,
                    icache_linesize: int = 64, firmware: Firmware | None = None) -> UnitCheck:
    """Run §2.7 steps 1-9 for one bound slot."""
    r = u.record
    code = u.code
    ca = hdr.code_address
    tc_abs = ca + r.tc_offset
    tb_abs = ca + r.tb_offset
    pbits = hdr.target_page_bits
    pmask = (1 << pbits) - 1
    uc = UnitCheck(u.slot)

    # -- model arrays ---------------------------------------------------------
    try:
        arrays = chunk.model_arrays()
    except ValueError as e:
        uc.meta_status = "incomplete"
        uc.check("enums", False, str(e), "capture_error")
        return uc
    uc.arrays = arrays
    insns: tuple[Insn, ...] = arrays["insns"]
    sites: tuple[Site, ...] = arrays["sites"]
    pool: tuple[PoolEntry, ...] = arrays["pool"]
    ldst: tuple[Ldst, ...] = arrays["ldst"]
    opmarks: tuple[OpMark, ...] = arrays["opmarks"]

    # -- step 1: bind extras ---------------------------------------------------
    flags = chunk.flags
    if flags & TbFlag.CAPTURE_OVERFLOW:
        uc.meta_status = "incomplete"
        uc.check("capture_overflow", False, "TB flags: capture_overflow")
    if flags & TbFlag.CAPTURE_ERROR:
        uc.meta_status = "incomplete"
        uc.check("capture_error", False, "TB flags: capture_error")
    if flags & TbFlag.GUEST_BYTES_TRUNCATED:
        uc.check("guest_hash", False, "guest bytes truncated by the capture",
                 "guest_bytes_truncated")
    else:
        gh = fnv1a64(chunk.guest_bytes)
        uc.check("guest_hash", gh == r.guest_hash and len(chunk.guest_bytes) == r.guest_size,
                 f"fnv=0x{gh:x} record=0x{r.guest_hash:x} n={len(chunk.guest_bytes)}",
                 "guest_hash_mismatch")
    uc.check("code_size", chunk.code_size == r.tc_size,
             f"capture {chunk.code_size} record {r.tc_size}", "code_size_mismatch")
    uc.check("icount", len(insns) == r.icount, f"capture {len(insns)} record {r.icount}",
             "insn_count_mismatch")
    if is_pcrel(r.cflags) and insns:
        uc.check("vaddr_word0", insns[0].words[0] == chunk.vaddr & pmask,
                 f"word0 0x{insns[0].words[0]:x} vaddr 0x{chunk.vaddr:x}", "vaddr_mismatch")
    elif not is_pcrel(r.cflags):
        uc.check("pcrel", False, f"cflags 0x{r.cflags:x} without CF_PCREL", "unsupported_guest")
    if uc.meta_status != "ok":
        return uc

    # -- step 2: layout --------------------------------------------------------
    exp_tc = r.tb_offset + round_up(hdr.tb_size, icache_linesize)
    uc.check("layout_tc", r.tc_offset == exp_tc,
             f"tc_offset 0x{r.tc_offset:x} expected 0x{exp_tc:x}", "layout_mismatch")
    ps = chunk.pool_start_or_none
    # B3 grown unit (TB chunk v2, WP18): guard stubs [stub_start, tc_size) after the pool
    ss = chunk.stub_start
    code_end = r.tc_size if ss is None else ss  # end of the TCG-emitted bytes (and pool)
    pool_end = code_end if ps is None else ps
    order = chunk.insn0_start <= chunk.body_end <= chunk.ldst_end <= pool_end <= code_end \
        <= r.tc_size
    uc.check("layout_order", order,
             f"insn0 {chunk.insn0_start} body_end {chunk.body_end} ldst_end {chunk.ldst_end} "
             f"pool_start {ps} stub_start {ss} code_size {r.tc_size}", "layout_mismatch")
    if ps is None:
        uc.check("layout_pad", chunk.ldst_end == code_end,
                 f"no pool: ldst_end {chunk.ldst_end} == code end {code_end}",
                 "layout_mismatch")
    elif order:
        pad = code[chunk.ldst_end:ps]
        uc.check("layout_pad", all(b == NOP for b in pad), f"{len(pad)} bytes before the pool",
                 "layout_mismatch")
    if ps is not None and order:
        _check_pool(uc, code, pool, ps, code_end, tc_abs)
    elif pool:
        uc.check("pool", False, f"{len(pool)} pool rows but no pool_start", "pool_mismatch")
    else:
        uc.check("pool", None, "no pool")

    # -- step 3: search data -----------------------------------------------------
    rows = None
    try:
        rows = decode_search(u.search, r.icount, pcrel=is_pcrel(r.cflags), pc=r.pc)
    except SearchDecodeError as e:
        uc.check("search_decode", False, str(e), "search_mismatch")
    if rows is not None:
        uc.rows = rows
        uc.search_len = rows.nbytes
        bad = [i for i, (row, ins) in enumerate(zip(rows, insns))
               if tuple(row.words) != tuple(ins.words) or row.host_end != ins.host_end]
        uc.check("search_rows", not bad and len(rows) == len(insns),
                 f"rows differing from the capture: {bad[:8]}", "search_mismatch")
        uc.check("search_body_end", bool(rows) and rows[-1].host_end == chunk.body_end,
                 f"last host_end {rows[-1].host_end if rows else None} body_end "
                 f"{chunk.body_end}", "search_mismatch")
        lo = r.tc_offset + r.tc_size + rows.nbytes
        uc.check("search_end", lo <= r.end_offset < lo + SEARCH_ALIGN,
                 f"table ends 0x{lo:x}, end_offset 0x{r.end_offset:x}", "search_mismatch")
    abut = bool(insns) and insns[0].host_start == chunk.insn0_start and all(
        insns[i].host_start == insns[i - 1].host_end for i in range(1, len(insns)))
    uc.check("insn_ranges", abut, "Insn[0].host_start == insn0_start and ranges abut",
             "search_mismatch")

    # -- step 4: linear sweep ------------------------------------------------------
    xs = sweep(code, tc_abs, tc_abs + pool_end) if order else []
    tiled = order and tiles(xs, tc_abs, tc_abs + pool_end)
    xs_stub = []
    if order and ss is not None:
        xs_stub = sweep(code[ss:], tc_abs + ss, tc_abs + r.tc_size)
        tiled = tiled and tiles(xs_stub, tc_abs + ss, tc_abs + r.tc_size)
    if not tiled:
        first_bad = next((i.address - tc_abs for i in xs + xs_stub if i.bad), pool_end)
        uc.check("decode", False, f"sweep of [0, {pool_end})" +
                 (f" + [{ss}, {r.tc_size})" if ss is not None else "") + " does not tile",
                 f"decode_failed@0x{first_bad:x}")
    else:
        uc.check("decode", True, f"{len(xs) + len(xs_stub)} host insns tile [0, {pool_end})" +
                 (f" and the guard stubs [{ss}, {r.tc_size})" if ss is not None else ""))
    xs = xs + xs_stub
    by_off = {i.address - tc_abs: i for i in xs}
    starts = sorted(by_off)
    insn_end = {o: i.end - tc_abs for o, i in by_off.items()}

    def containing(off: int) -> X86Insn | None:
        k = bisect.bisect_right(starts, off) - 1
        if k >= 0:
            i = by_off[starts[k]]
            if off < i.end - tc_abs:
                return i
        return None

    if tiled:
        bad_sites = []
        for k, s in enumerate(sites):
            io = _site_insn_off(s)
            ins = by_off.get(io) if io is not None else containing(s.field_off)
            if ins is None or not (ins.address - tc_abs <= s.field_off and
                                   s.field_off + s.width <= ins.end - tc_abs):
                bad_sites.append(k)
                uc.reasons.append(f"decode_failed@0x{s.field_off:x}")
        uc.check("site_boundaries", not bad_sites,
                 f"sites not inside a decoded insn: {bad_sites[:8]}", "")

    # -- steps 5 + 6: matching and classification ----------------------------------
    if tiled:
        _match_and_classify(uc, u, chunk, ctx, xs, by_off, starts, sites, pool, ldst, tc_abs,
                            tb_abs, hdr)

    # -- step 7: recovery --------------------------------------------------------
    if rows is not None:
        checks, points = check_recovery(rows, ldst, opmarks, sites,
                                        insn_end=insn_end if tiled else None, stub_start=ss)
        uc.recovery_checks = checks
        uc.recovery = tuple(points)
        bad = [c for c in checks if not c.ok]
        uc.check("recovery", not bad, "; ".join(c.detail() for c in bad[:4]) or
                 f"{len(checks)} return offsets unwind to their OpMark insn",
                 RECOVERY_MISMATCH)
    else:
        uc.check("recovery", None, "no search rows")

    # -- step 8: exits -------------------------------------------------------------
    gsites = {s.aux: s for s in sites if s.kind == SiteKind.GOTO_TB}
    valid_jmp = {n: o for n, o in enumerate(r.jmp_insn_offset) if o != 0xFFFF}
    ok = set(valid_jmp) == set(gsites) and all(
        gsites[n].field_off == o and r.jmp_reset_offset[n] == o + 4 for n, o in valid_jmp.items())
    uc.check("exits_goto_tb", ok,
             f"record jmp_insn_offset {list(r.jmp_insn_offset)} reset "
             f"{list(r.jmp_reset_offset)}; GOTO_TB sites "
             f"{ {n: s.field_off for n, s in gsites.items()} }", "exit_mismatch")
    if tiled:
        movi_at = {s.insn_off: s for s in sites if s.kind == SiteKind.MOVI_EXIT_TB_PTR}
        prev = {starts[k]: starts[k - 1] for k in range(1, len(starts))}
        bad = []
        for s in sites:
            if s.kind != SiteKind.EXIT_TB_RET_JMP:
                continue
            p = prev.get(s.insn_off)
            m = movi_at.get(p) if p is not None else None
            if m is None or m.aux != s.aux:
                bad.append(s.field_off)
        uc.check("exits_exit_tb", not bad,
                 f"EXIT_TB_RET_JMP without its MOVI_EXIT_TB_PTR at {bad[:8]}", "exit_mismatch")
    irq = bool(flags & TbFlag.IRQ_CHECK)
    uc.check("irq_check", irq == (not r.cflags & CF_NOIRQ),
             f"flags.irq_check={irq} cflags=0x{r.cflags:x}", "irq_check_mismatch")

    # -- step 9: guest ---------------------------------------------------------------
    gb = chunk.guest_bytes
    if firmware is not None:
        fw = firmware.bytes_at(chunk.vaddr, r.guest_size)
        uc.origin = "elf_verified" if fw is not None and fw == gb else "ram_resident"
        uc.check("guest_origin", True, uc.origin)
    else:
        uc.check("guest_origin", None, "no firmware ELF")
    if insns and len(gb) == r.guest_size:
        exp = lengths_from_words([i.words[0] for i in insns], r.guest_size, pbits)
        probs = check_lengths(gb, exp, chunk.vaddr)
        uc.check("guest_thumb_lengths", not probs, "; ".join(probs) or f"{len(exp)} insns",
                 "guest_decode_mismatch")
    return uc


def _check_pool(uc: UnitCheck, code: bytes, pool: Sequence[PoolEntry], ps: int, size: int,
                tc_abs: int) -> None:
    probs = []
    slots: dict[int, tuple[int, int]] = {}
    for k, p in enumerate(pool):
        if p.nlong != 1:
            probs.append(f"row {k}: nlong {p.nlong}")
            continue
        prev = slots.setdefault(p.slot_off, (p.nlong, p.data0))
        if prev != (p.nlong, p.data0):
            probs.append(f"row {k}: slot {p.slot_off} has two values")
    pos = ps
    for off in sorted(slots):
        if off != pos:
            probs.append(f"slot {off} does not follow {pos}")
        if (tc_abs + off) % 8:
            probs.append(f"slot {off} not 8-aligned")
        data = int.from_bytes(code[off:off + 8], "little")
        if data != slots[off][1]:
            probs.append(f"slot {off} bytes 0x{data:x} != captured 0x{slots[off][1]:x}")
        pos = off + 8
    if pos != size:
        probs.append(f"slots end at {pos}, code_size {size}")
    uc.check("pool", not probs, "; ".join(probs[:6]) or f"{len(slots)} slots tile the pool",
             "pool_mismatch")


def _x86_reg(aux: int) -> str | None:
    return X86_REGS[aux] if 0 <= aux < len(X86_REGS) else None


def _match_and_classify(uc: UnitCheck, u: ScUnit, chunk: TbChunk, ctx: HostContext,
                        xs: list[X86Insn], by_off: dict[int, X86Insn], starts: list[int],
                        sites: Sequence[Site], pool: Sequence[PoolEntry], ldst: Sequence[Ldst],
                        tc_abs: int, tb_abs: int, hdr: ScHeader) -> None:
    code = u.code
    r = u.record
    code_end = tc_abs + r.tc_size
    refs = extract_refs(xs, Arena(hdr.code_address, hdr.code_size), ctx)
    uc.decoded = refs
    pool_by_user = {p.user_field_off: p for p in pool}

    def ref_ok(s: Site, d: DecodedRef) -> bool:
        if d.kind not in _KIND_REFS.get(s.kind, set()):
            return False
        if s.kind != SiteKind.POOL_REF and d.kind not in _ENC_REFS.get(s.enc, set()):
            return False
        if d.width != s.width:
            return False
        io = _site_insn_off(s)
        if io is not None and d.insn_addr - tc_abs != io:
            return False
        if s.enc in (Encoding.IMM32_ZX, Encoding.IMM32_SX) and d.enc != s.enc:
            return False
        if s.kind == SiteKind.POOL_REF:
            p = pool[s.aux] if 0 <= s.aux < len(pool) else None
            return (p is not None and p.user_field_off == s.field_off
                    and d.value == tc_abs + p.slot_off and p.data0 == s.value)
        if s.enc == Encoding.POOL_INDIRECT:
            p = pool_by_user.get(s.field_off)
            return (p is not None and d.value == tc_abs + p.slot_off and p.data0 == s.value)
        if s.kind == SiteKind.GOTO_PTR:
            return d.reg == _x86_reg(s.aux)
        return d.value == s.value

    # index sites
    by_field: dict[int, list[int]] = {}
    by_insn0: dict[int, list[int]] = {}
    for k, s in enumerate(sites):
        if s.width > 0:
            by_field.setdefault(s.field_off, []).append(k)
        else:
            by_insn0.setdefault(s.insn_off, []).append(k)
    matched: dict[int, DecodedRef] = {}
    unexplained = []
    for d in refs:
        if d.width == 0:
            cands = by_insn0.get(d.insn_addr - tc_abs, [])
        else:
            cands = by_field.get(d.field_addr - tc_abs, [])
        hit = [k for k in cands if k not in matched and ref_ok(sites[k], d)]
        if len(hit) == 1 or (hit and d.width == 0):
            matched[hit[0]] = d
        else:
            off = (d.field_addr if d.width else d.insn_addr) - tc_abs
            unexplained.append(off)
            uc.reasons.append(f"unexplained_reference@0x{off:x}")
    uc.check("refs_decoded", not unexplained,
             f"{len(refs)} decoded references; unexplained at {unexplained[:8]}",
             "")

    # captured side
    missing = []
    for k, s in enumerate(sites):
        if k in matched:
            continue
        if s.enc == Encoding.XOR0 or (s.width == 0 and s.kind != SiteKind.GOTO_PTR):
            ins = by_off.get(s.insn_off)
            ops = [o.strip() for o in ins.op_str.split(",")] if ins is not None else []
            if ins is not None and ins.mnemonic == "xor" and len(ops) == 2 and ops[0] == ops[1] \
                    and s.value == 0:
                continue
        elif s.enc in (Encoding.IMM32_ZX, Encoding.IMM32_SX, Encoding.IMM64):
            # an immediate the decoder did not list (value outside every range):
            # accept it when the instruction has exactly that immediate field
            ins = by_off.get(s.insn_off)
            if ins is not None and ins.imm_size == s.width and \
                    ins.address + ins.imm_offset - tc_abs == s.field_off:
                raw = int.from_bytes(code[s.field_off:s.field_off + s.width], "little")
                v = raw if s.enc != Encoding.IMM32_SX else (
                    (raw - (1 << 32)) & ((1 << 64) - 1) if raw >> 31 else raw)
                if v == s.value:
                    continue
        missing.append(s.field_off)
        uc.reasons.append(f"missing_reference@0x{s.field_off:x}")
    uc.check("refs_captured", not missing,
             f"{len(sites)} captured sites; not found in the bytes at {missing[:8]}",
             "")

    # final_field
    badff = [s.field_off for s in sites
             if s.width > 0 and _field_bytes(code, s.field_off, s.width) != s.final_field]
    uc.check("final_field", not badff, f"final_field differs at {badff[:8]}",
             "final_field_mismatch")

    # classification
    helpers: dict[int, HelperBinding] = {}
    unchecked: list[AnchoredPointer] = []
    boundaries = set(starts)
    for k, s in enumerate(sites):
        cs = _classify(k, s, matched.get(k), ctx, code, tc_abs, tb_abs, code_end, boundaries,
                       pool_by_user, pool, ldst, r)
        uc.classified.append(cs)
        if cs.problem:
            uc.reasons.append(cs.problem)
        if cs.binding is not None and cs.binding.cls.startswith("abs."):
            unchecked.append(AnchoredPointer(cs.binding.cls, cs.binding.anchor, k))
        if s.kind == SiteKind.HELPER_CALL:
            h = ctx.helper(s.aux)
            if h is not None and s.aux not in helpers:
                b = cs.binding
                helpers[s.aux] = HelperBinding(s.aux, h.name_str, b.module if b else None,
                                               b.symbol if b else None, h.tcg_flags, h.func)
                uc.helper_symbol_checks.append(
                    (s.aux, h.name_str, b.symbol if b else None,
                     helper_symbol_ok(h.name_str, b.symbol if b else None)))
    uc.helpers = tuple(sorted(helpers.values(), key=lambda h: h.id))
    uc.unchecked = tuple(unchecked)
    hist = Counter(c.cls for c in uc.classified)
    probs = [c.problem for c in uc.classified if c.problem]
    uc.check("classify", not probs,
             ", ".join(f"{k}:{v}" for k, v in sorted(hist.items())) +
             (f"; problems {probs[:6]}" if probs else ""), "")


def _fix(s: Site, rule: str, cls: str, target: int | None) -> Fixup:
    return Fixup(s.field_off, s.width, rule, int(s.kind), cls, target)  # type: ignore[arg-type]


def _classify(k: int, s: Site, d: DecodedRef | None, ctx: HostContext, code: bytes, tc_abs: int,
              tb_abs: int, code_end: int, boundaries: set[int],
              pool_by_user: dict[int, PoolEntry], pool: Sequence[PoolEntry],
              ldst: Sequence[Ldst], r) -> ClassifiedSite:
    """Class, fixup and binding of captured site ``k`` (§2.7 step 6 table)."""
    at = f"@0x{s.field_off:x}"
    K = SiteKind
    v = s.value

    def cs(cls: str, target: int | None = v, binding: AddrClass | None = None,
           fixup: Fixup | None = None, problem: str | None = None) -> ClassifiedSite:
        return ClassifiedSite(k, s, cls, target, binding, fixup, problem)

    def intra(target: int, need_boundary: bool = True) -> ClassifiedSite:
        if tc_abs <= target < code_end and (not need_boundary or
                                            (target - tc_abs) in boundaries):
            return cs("intra", target)
        if tc_abs <= target < code_end:
            return cs("unknown", target, problem=f"intra_target_not_boundary{at}")
        if ctx.in_arena(target):
            return cs("arena.other_tb", target, problem=f"arena.other_tb{at}")
        return cs("unknown", target, problem=f"intra_target_outside{at}")

    if s.kind == K.OTHER_BRANCH:
        return cs("unknown", problem=f"other_branch{at}")
    if s.kind == K.GOTO_PTR:
        return cs("goto_ptr", None)
    if s.kind == K.GOTO_TB:
        field_abs = tc_abs + s.field_off
        ok = (0 <= s.aux <= 1 and r.jmp_insn_offset[s.aux] == s.field_off
              and field_abs % 4 == 0 and s.final_field == 0
              and v == tc_abs + s.field_off + 4)
        return cs("goto_tb", v, problem=None if ok else f"goto_tb_mismatch{at}")
    if s.kind in (K.LABEL_JMP, K.LABEL_JCC):
        return intra(v)
    if s.kind == K.LDST_FAST_JCC:
        c = intra(v)
        d0 = ldst[s.aux] if 0 <= s.aux < len(ldst) else None
        if c.problem is None and (d0 is None or v != tc_abs + d0.slow_start
                                  or s.field_off not in d0.jcc_fields):
            return cs("intra", v, problem=f"ldst_mismatch{at}")
        return c
    if s.kind == K.LDST_RET_JMP:
        d0 = ldst[s.aux] if 0 <= s.aux < len(ldst) else None
        if s.enc == Encoding.POOL_INDIRECT:
            b = ctx.data_value(v)
            return cs("pool", v, b, problem=f"arena.abs{at}")  # absolute own-TB pointer
        c = intra(v)
        if c.problem is None and (d0 is None or v != tc_abs + d0.raddr_off):
            return cs("intra", v, problem=f"ldst_raddr_mismatch{at}")
        return c
    if s.kind in (K.EXIT_TB_RET_JMP, K.EXIT_TB_EPI_JMP):
        want = ctx.tb_ret_addr if s.kind == K.EXIT_TB_RET_JMP else ctx.epilogue_addr
        cls = "prologue.tb_ret" if s.kind == K.EXIT_TB_RET_JMP else "prologue.epilogue"
        if v != want:
            return cs("unknown", v, problem=f"prologue_target_mismatch{at}")
        if s.enc == Encoding.POOL_INDIRECT:
            return cs("pool", v, ctx.data_value(v))
        return cs(cls, v, fixup=_fix(s, "rel_minus_delta", cls, v))
    if s.kind in (K.HELPER_CALL, K.LDST_HELPER_CALL):
        prob = None
        if s.kind == K.HELPER_CALL:
            h = ctx.helper(s.aux)
            if h is None:
                prob = f"unknown_helper{at}"
            elif h.func != v:
                prob = f"helper_mismatch{at}"
        if ctx.in_arena(v):
            return cs("arena.other_tb", v, problem=f"arena.other_tb{at}")
        b = ctx.code_target(v)
        if b is None:
            return cs("unknown", v, problem=f"unmapped_target{at}")
        if s.enc == Encoding.POOL_INDIRECT:
            return cs("pool", v, b, problem=prob)
        return cs(b.cls, v, b, _fix(s, "rel_minus_delta", b.cls, v), prob)
    if s.kind == K.MOVI_EXIT_TB_PTR:
        if s.aux not in OWN_STRUCT_IDX or v != tb_abs + s.aux:
            return cs("unknown", v, problem=f"own_struct_mismatch{at}")
        cls = f"own_struct+{s.aux}"
        if s.enc == Encoding.RIPREL32:
            return cs(cls, v)
        if s.enc == Encoding.IMM64:
            return cs(cls, v, fixup=_fix(s, "abs_plus_delta", "arena.abs", v))
        return cs("arena.abs", v, problem=f"arena.abs{at}")
    if s.kind == K.MOVI_LDST_RA:
        d0 = ldst[s.aux] if 0 <= s.aux < len(ldst) else None
        if d0 is None or v != tc_abs + d0.raddr_off or (v - tc_abs) not in boundaries:
            return cs("unknown", v, problem=f"ldst_raddr_mismatch{at}")
        if s.enc == Encoding.RIPREL32:
            return cs("intra", v)
        if s.enc == Encoding.IMM64:
            return cs("intra", v, fixup=_fix(s, "abs_plus_delta", "arena.abs", v))
        return cs("arena.abs", v, problem=f"arena.abs{at}")
    if s.kind == K.MOVI_CONST:
        if s.enc == Encoding.RIPREL32:
            if tc_abs <= v < code_end:
                return cs("intra", v)
            if ctx.in_arena(v):
                return cs("arena.other_tb", v, problem=f"arena.other_tb{at}")
            b = ctx.data_value(v)
            return cs("ext.ptr_riprel", v, b, _fix(s, "rel_minus_delta", "ext.ptr_riprel", v))
        if s.enc == Encoding.XOR0:
            return cs("const", 0)
        b = ctx.data_value(v)
        if b.cls == "arena.abs" or b.cls.startswith("prologue."):
            return cs("arena.abs", v, b, problem=f"arena.abs{at}")
        return cs("const", v, b)
    if s.kind == K.POOL_REF:
        p = pool[s.aux] if 0 <= s.aux < len(pool) else None
        b = ctx.data_value(v)
        if p is None:
            return cs("unknown", v, problem=f"pool_mismatch{at}")
        if b.cls == "arena.abs":
            return cs("pool", v, b, problem=f"arena.abs{at}")
        return cs("pool", v, b)
    return cs("unknown", v, problem=f"unknown_site_kind{at}")  # pragma: no cover
