"""Synthetic builders for ``.sc`` v5, ``.scm`` v1 and ``.qprof`` v1 files.

WP4 implements ``build_sc``/``build_scm`` and the corruption helpers; WP6
implements ``build_qprof``. Every builder returns the file bytes and, when
``path`` is given, also writes them there (sparsely for ``.sc``).

How a :class:`SynthUnit` becomes a slot
---------------------------------------
For unit ``i`` (slot ``i`` in the ``.sc``; slot ``first_slot + i`` in a ``.scm``)
the builders compute, exactly as QEMU lays a TB out (tcg_tb_alloc,
translate-all.c:258-480):

* ``tc_offset = tb_offset + ROUND_UP(tb_size, icache_linesize)`` (192 with the
  B0 defaults ``tb_size=152``, line 64);
* ``search``: ``unit.search`` if non-empty, otherwise the sleb128 search data
  that ``encode_search`` would write for the unit's ``insns`` (CF_PCREL seeds);
* ``end_offset = ROUND_UP(tc_offset + len(code) + len(search), 16)``
  (CODE_GEN_ALIGN);
* the record: ``pc=0``, the key fields, ``guest_size=len(guest_bytes)``,
  ``icount=unit.icount``, ``guest_hash`` (FNV of ``guest_bytes`` unless given),
  checksum and ``committed``.

The ``.sc`` arena holds only ``[tc_offset, end_offset)`` of each unit (as
QEMU's pwrite does); the TB struct area and the prologue read back as zeros.

``SynthUnit.meta`` keys (all optional):

* ``.scm`` arrays ``insns``, ``sites``, ``pool``, ``ldst``, ``opmarks``,
  ``static_exits``: sequences of ``scm_v1`` raw records, ``scopt.model.unit``
  records or dicts of the raw field names. A site dict without
  ``final_field`` gets the field's bytes from ``code`` (low 4 bytes, LE).
  Without ``insns``, ``icount`` rows are synthesized: host ranges split
  ``[insn0_start, body_end)`` evenly, ``word0 = (vaddr & page_mask) + 2*k``.
* TB chunk scalars: ``slot``, ``commit_seq``, ``attempts``, ``flags``,
  ``insn0_start``, ``body_end``, ``ldst_end``, ``pool_start``, ``code_size``,
  ``code_fnv64``, ``reserved``, ``tb_chunk_version``, ``insn_start_words``,
  ``scm_record`` (104 bytes replacing the embedded record), ``scm_guest_bytes``.
* layout: ``tc_offset``, ``end_offset``, ``auto_search`` (default True).
* record tampering: ``record_overrides`` (ScRecord field -> value, applied
  before the checksum), ``record_checksum`` (forced value), ``committed``.
* ``scm``: False leaves the unit out of the ``.scm`` (a ``no_meta`` slot).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from typing import Any, Iterable, Mapping, Sequence

from scopt.canon import fnv1a64
from scopt.formats import layouts as L
from scopt.formats import scm_v1 as S
from scopt.formats.enums import (
    NONE_U32, ChunkType, Encoding, MapsTag, OpMarkKind, SiteKind, TbFlag,
)
from scopt.formats.sc_records import ScHeader, ScRecord
from scopt.ids import TBKey
from scopt.model import unit as U

# --------------------------------------------------------------------------
# Defaults (taken from runs/b0/cache/minimal.sc, B0; code_size is synthetic)
# --------------------------------------------------------------------------
CF_NOIRQ = 0x0001_0000
CF_PCREL = 0x0002_0000
CODE_GEN_ALIGN = 16

DEFAULT_CODE_ADDRESS = 0x7FFF6C000000
DEFAULT_PROLOGUE_END = 45
DEFAULT_RUNTIME_SIGNATURE = 0xCD59117CBC9F8837
DEFAULT_HOST_PAGE_SIZE = 4096
DEFAULT_TARGET_PAGE_BITS = 10
DEFAULT_TB_SIZE = 152
DEFAULT_ICACHE_LINESIZE = 64
MIN_CODE_SIZE = 64 << 10  # synthetic arenas are small (minimal.sc uses 2 GiB)
DEFAULT_KEY = TBKey(page_addr0=0x28098740, page_addr1=None, flags=0x110, cs_base=0x800400,
                    cflags=0xFF020000)  # slot 0 of minimal.sc

# A decodable x86-64 stand-in for the TCG prologue/epilogue (45 bytes):
#   0: push rbp/rbx/r12-r15; mov rbp,rdi; sub rsp,0x488; jmp rsi
#  22: epilogue: xor eax,eax
#  24: tb_ret:   add rsp,0x488; pop r15-r12/rbx/rbp; ret; int3 x3
SYNTH_PROLOGUE = bytes.fromhex(
    "55" "53" "4154" "4155" "4156" "4157" "488bef" "4881ec88040000" "ffe6"
    "33c0"
    "4881c488040000" "415f" "415e" "415d" "415c" "5b" "5d" "c3" "cccccc")
SYNTH_EPILOGUE_OFF = 22
SYNTH_TB_RET_OFF = 24
assert len(SYNTH_PROLOGUE) == DEFAULT_PROLOGUE_END

DEFAULT_SCM_PID = 4242
DEFAULT_SCM_REALTIME_NS = 1_763_000_000_000_000_000


def round_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a


@dataclass(frozen=True)
class SynthUnit:
    """One TB for the builders. Offsets are arena offsets (``tb_offset`` 64-aligned)."""

    key: TBKey
    tb_offset: int
    code: bytes
    search: bytes = b""
    guest_bytes: bytes = b""
    icount: int = 1
    guest_hash: int | None = None  # None: FNV-1a-64 of guest_bytes
    jmp_reset_offset: tuple[int, int] = (0xFFFF, 0xFFFF)
    jmp_insn_offset: tuple[int, int] = (0xFFFF, 0xFFFF)
    vaddr: int = 0
    meta: Mapping[str, Any] = field(default_factory=dict)  # .scm TB arrays (sites, insns, ...)


# --------------------------------------------------------------------------
# Search data (translate-all.c encode_search, CF_PCREL seeds)
# --------------------------------------------------------------------------

def encode_sleb128(v: int) -> bytes:
    """``encode_sleb128`` of translate-all.c (``v`` is taken as int64)."""
    v &= 0xFFFF_FFFF_FFFF_FFFF
    if v >= 1 << 63:
        v -= 1 << 64
    out = bytearray()
    while True:
        byte = v & 0x7F
        v >>= 7
        if (v == 0 and not byte & 0x40) or (v == -1 and byte & 0x40):
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def encode_search(insns: Sequence[S.ScmInsn], *, pc: int | None = None) -> bytes:
    """Search data for ``insns`` (``pc=None``: CF_PCREL, column 0 seeded with 0)."""
    out = bytearray()
    prev_words = [pc or 0, 0, 0]
    prev_end = 0
    for ins in insns:
        for j in range(3):
            out += encode_sleb128(ins.words[j] - prev_words[j])
        out += encode_sleb128(ins.host_end - prev_end)
        prev_words = list(ins.words)
        prev_end = ins.host_end
    return bytes(out)


# --------------------------------------------------------------------------
# Array element coercion
# --------------------------------------------------------------------------

def _final_field(code: bytes, off: int, width: int) -> int:
    n = min(width, 4)
    if width <= 0 or off + n > len(code):
        return 0
    return int.from_bytes(code[off: off + n], "little")


def _none32(v: int | None) -> int:
    return NONE_U32 if v is None else int(v)


def _coerce(kind: str, e: Any, code: bytes, index: int) -> Any:
    if kind == "insns":
        if isinstance(e, S.ScmInsn):
            return e
        if isinstance(e, U.Insn):
            return S.ScmInsn(tuple(e.words), e.host_start, e.host_end)
        if isinstance(e, Mapping):
            return S.ScmInsn(tuple(e.get("words", (0, 0, 0))), e["host_start"], e["host_end"])
        words, hs, he = e
        return S.ScmInsn(tuple(words), hs, he)
    if kind == "sites":
        if isinstance(e, S.ScmSite):
            return e
        if isinstance(e, U.Site):
            return S.ScmSite(e.field_off, e.insn_off, e.width, int(e.enc), int(e.kind), e.aux,
                             e.value, _none32(e.insn_idx), e.final_field)
        d = dict(e)
        d.setdefault("insn_off", d["field_off"])
        d.setdefault("width", 4)
        d.setdefault("enc", Encoding.REL32)
        d.setdefault("aux", 0)
        d.setdefault("value", 0)
        d["insn_idx"] = _none32(d.get("insn_idx"))
        if d.get("final_field") is None:
            d["final_field"] = _final_field(code, d["field_off"], d["width"])
        d["enc"], d["kind"] = int(d["enc"]), int(d["kind"])
        return S.ScmSite(**d)
    if kind == "pool":
        if isinstance(e, S.ScmPool):
            return e
        if isinstance(e, U.PoolEntry):
            return S.ScmPool(e.user_field_off, e.slot_off, e.rtype, e.nlong, e.addend, e.data0)
        d = dict(e)
        d.setdefault("rtype", 2)  # R_386_PC32
        d.setdefault("nlong", 1)
        d.setdefault("addend", -4)
        return S.ScmPool(**d)
    if kind == "ldst":
        if isinstance(e, S.ScmLdst):
            return e
        if isinstance(e, U.Ldst):
            return S.ScmLdst((_none32(e.jcc_fields[0]), _none32(e.jcc_fields[1])), e.slow_start,
                             e.slow_end, e.raddr_off, e.oi, e.op_ordinal, int(e.is_ld))
        d = dict(e)
        j = d.pop("jcc_field", d.pop("jcc_fields", (None, None)))
        d["jcc_field"] = (_none32(j[0]), _none32(j[1]))
        d.setdefault("op_ordinal", index)
        d["is_ld"] = int(d.get("is_ld", 1))
        return S.ScmLdst(**d)
    if kind == "opmarks":
        if isinstance(e, S.ScmOpMark):
            return e
        if isinstance(e, U.OpMark):
            return S.ScmOpMark(e.host_off, int(e.kind), e.insn_idx, _none32(e.helper_id))
        d = dict(e)
        d["kind"] = int(d["kind"])
        d["helper_id"] = _none32(d.get("helper_id"))
        return S.ScmOpMark(**d)
    if kind == "static_exits":
        if isinstance(e, S.ScmStaticExit):
            return e
        if isinstance(e, U.StaticExit):
            return S.ScmStaticExit(e.slot, int(e.direct), e.insn_idx, e.dest_vaddr)
        d = dict(e)
        d["direct"] = int(d.get("direct", 1))
        return S.ScmStaticExit(**d)
    raise KeyError(kind)


# --------------------------------------------------------------------------
# Unit resolution (shared by build_sc and build_scm so both always agree)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedUnit:
    unit: SynthUnit
    tc_offset: int
    end_offset: int
    search: bytes
    record: ScRecord
    record_bytes: bytes
    arena_bytes: bytes  # [tc_offset, end_offset)
    insns: tuple[S.ScmInsn, ...]
    arrays: Mapping[str, tuple]


def resolve_unit(u: SynthUnit, *, tb_size: int = DEFAULT_TB_SIZE,
                 icache_linesize: int = DEFAULT_ICACHE_LINESIZE,
                 target_page_bits: int = DEFAULT_TARGET_PAGE_BITS) -> ResolvedUnit:
    m = u.meta
    code = bytes(u.code)
    tc = m.get("tc_offset", u.tb_offset + round_up(tb_size, icache_linesize))
    if "insns" in m:
        insns = tuple(_coerce("insns", e, code, i) for i, e in enumerate(m["insns"]))
    else:
        start = m.get("insn0_start", 0)
        end = m.get("body_end", len(code))
        page_mask = (1 << target_page_bits) - 1
        n = u.icount
        insns = tuple(S.ScmInsn(((u.vaddr & page_mask) + 2 * k, 0, 0),
                                start + (end - start) * k // n if n else start,
                                start + (end - start) * (k + 1) // n)
                      for k in range(n))
    search = bytes(u.search)
    if not search and m.get("auto_search", True):
        search = encode_search(insns)
    end_offset = m.get("end_offset", round_up(tc + len(code) + len(search), CODE_GEN_ALIGN))
    if end_offset < tc + len(code) + len(search):
        raise ValueError(f"tb_offset {u.tb_offset:#x}: end_offset before end of search data")
    arena = code + search + b"\0" * (end_offset - tc - len(code) - len(search))
    gh = fnv1a64(u.guest_bytes) if u.guest_hash is None else u.guest_hash
    rec = ScRecord(tb_offset=u.tb_offset, end_offset=end_offset, tc_offset=tc, tc_size=len(code),
                   pc=0, cs_base=u.key.cs_base, page_addr0=u.key.page_addr0,
                   page_addr1=u.key.page_addr1_raw, guest_hash=gh, flags=u.key.flags,
                   cflags=u.key.cflags, guest_size=len(u.guest_bytes), icount=u.icount,
                   jmp_reset_offset=tuple(u.jmp_reset_offset),
                   jmp_insn_offset=tuple(u.jmp_insn_offset), checksum=0,
                   committed=m.get("committed", L.SC_RECORD_COMMIT))
    if m.get("record_overrides"):
        rec = replace(rec, **m["record_overrides"])
    raw = rec.to_bytes(fix_checksum=True)
    if "record_checksum" in m:
        raw = raw[:92] + int(m["record_checksum"]).to_bytes(4, "little") + raw[96:]
    rec = ScRecord.from_bytes(raw)
    arrays = {k: tuple(_coerce(k, e, code, i) for i, e in enumerate(m.get(k, ())))
              for k in ("sites", "pool", "ldst", "opmarks", "static_exits")}
    return ResolvedUnit(u, tc, end_offset, search, rec, raw, arena, insns, arrays)


def sc_header_fields(units: Sequence[SynthUnit], header: Mapping[str, Any] | None = None,
                     resolved: Sequence[ResolvedUnit] | None = None,
                     icache_linesize: int = DEFAULT_ICACHE_LINESIZE) -> dict[str, Any]:
    """The ``.sc`` header fields ``build_sc`` uses (``code_size`` auto-sized)."""
    h: dict[str, Any] = {
        "magic": L.SC_MAGIC, "version": L.SC_VERSION, "header_size": L.SC_HEADER.size,
        "code_size": None, "code_address": DEFAULT_CODE_ADDRESS,
        "prologue_end": DEFAULT_PROLOGUE_END, "record_count": None,
        "runtime_signature": DEFAULT_RUNTIME_SIGNATURE, "host_page_size": DEFAULT_HOST_PAGE_SIZE,
        "target_page_bits": DEFAULT_TARGET_PAGE_BITS, "tb_size": DEFAULT_TB_SIZE,
        "reserved": 0, "checksum": None,
    }
    h.update(header or {})
    if h["code_size"] is None:
        if resolved is None:
            resolved = [resolve_unit(u, tb_size=h["tb_size"], icache_linesize=icache_linesize,
                                     target_page_bits=h["target_page_bits"]) for u in units]
        max_end = max([r.end_offset for r in resolved] + [h["prologue_end"]])
        h["code_size"] = max(MIN_CODE_SIZE, round_up(max_end, 64 << 10))
    if h["record_count"] is None:
        h["record_count"] = len(units)
    return h


# --------------------------------------------------------------------------
# .sc v5
# --------------------------------------------------------------------------

def build_sc(units: Sequence[SynthUnit], header: Mapping[str, Any] | None = None, *,
             path: str | None = None, record_count: int | None = None,
             extra_records: bytes = b"", icache_linesize: int = DEFAULT_ICACHE_LINESIZE) -> bytes:
    """A v5 ``.sc``: header (defaults from minimal.sc unless overridden), arena, records.

    ``record_count`` overrides the header count (to model torn tails);
    ``extra_records`` is appended raw after the generated records. ``header``
    may override any ``ScHeader`` field; ``checksum`` forces a (bad) value.
    ``code_size`` defaults to the smallest multiple of 64 KiB (>= 64 KiB) that
    holds every unit (minimal.sc's real 2 GiB is not useful in tests).
    """
    hdr0 = dict(header or {})
    tb_size = hdr0.get("tb_size", DEFAULT_TB_SIZE)
    tpb = hdr0.get("target_page_bits", DEFAULT_TARGET_PAGE_BITS)
    resolved = [resolve_unit(u, tb_size=tb_size, icache_linesize=icache_linesize,
                             target_page_bits=tpb) for u in units]
    h = sc_header_fields(units, hdr0, resolved, icache_linesize)
    if record_count is not None:
        h["record_count"] = record_count
    code_size = h["code_size"]
    _check_layout(resolved, code_size)
    forced_ck = h.pop("checksum")
    hb = ScHeader(**h, checksum=0).to_bytes(fix_checksum=True)
    if forced_ck is not None:
        hb = hb[:72] + int(forced_ck).to_bytes(8, "little")
    records = b"".join(r.record_bytes for r in resolved) + bytes(extra_records)

    data = bytearray(L.SC_CODE_OFFSET + code_size)
    data[: len(hb)] = hb
    for r in resolved:
        o = L.SC_CODE_OFFSET + r.tc_offset
        data[o: o + len(r.arena_bytes)] = r.arena_bytes
    data += records
    if path is not None:
        with open(path, "wb") as f:  # sparse: only header, TB bytes and records are written
            f.write(hb)
            for r in resolved:
                f.seek(L.SC_CODE_OFFSET + r.tc_offset)
                f.write(r.arena_bytes)
            f.seek(L.SC_CODE_OFFSET + code_size)
            f.write(records)
            f.truncate(len(data))
    return bytes(data)


def _check_layout(resolved: Sequence[ResolvedUnit], code_size: int) -> None:
    spans = sorted((r.unit.tb_offset, r.end_offset) for r in resolved)
    for (a0, a1), (b0, _b1) in zip(spans, spans[1:]):
        if b0 < a1:
            raise ValueError(f"units overlap: [{a0:#x},{a1:#x}) and {b0:#x}")
    for r in resolved:
        if r.end_offset > code_size:
            raise ValueError(f"unit at {r.unit.tb_offset:#x} ends past code_size {code_size:#x}")


def auto_place(units: Sequence[SynthUnit], start: int = 64, *,
               tb_size: int = DEFAULT_TB_SIZE,
               icache_linesize: int = DEFAULT_ICACHE_LINESIZE) -> list[SynthUnit]:
    """Re-assign ``tb_offset`` like ``tcg_tb_alloc``: each TB at ROUND_UP(prev end, line)."""
    out, off = [], round_up(start, icache_linesize)
    for u in units:
        u2 = replace(u, tb_offset=off)
        r = resolve_unit(u2, tb_size=tb_size, icache_linesize=icache_linesize)
        out.append(u2)
        off = round_up(r.end_offset, icache_linesize)
    return out


# --------------------------------------------------------------------------
# .scm v1
# --------------------------------------------------------------------------

def default_run_info(sc: Mapping[str, Any], *, first_slot: int = 0,
                     icache_linesize: int = DEFAULT_ICACHE_LINESIZE) -> dict[str, Any]:
    """RUN_INFO fields consistent with the ``.sc`` header fields ``sc``."""
    pe = sc["prologue_end"]
    prologue = SYNTH_PROLOGUE[:pe] if pe <= len(SYNTH_PROLOGUE) else (
        SYNTH_PROLOGUE + b"\xcc" * (pe - len(SYNTH_PROLOGUE)))
    return {
        "info_version": L.SCM_RUN_INFO_VERSION, "capture_level": 1,
        "code_address": sc["code_address"], "code_size": sc["code_size"], "prologue_end": pe,
        "tb_ret_off": SYNTH_TB_RET_OFF, "epilogue_off": SYNTH_EPILOGUE_OFF,
        "runtime_signature": sc["runtime_signature"], "header_record_count_at_load": first_slot,
        "loaded_max_end": 0, "highwater_off": sc["code_size"] - 1024,
        "icache_linesize": icache_linesize, "tb_struct_size": sc["tb_size"],
        "host_page_size": sc["host_page_size"], "target_page_bits": sc["target_page_bits"],
        "insn_start_words": 3, "cpuinfo": 0, "exe_st_dev": 0x10303, "exe_st_ino": 4242,
        "exe_st_size": 60_000_000, "exe_st_mtim_ns": 1_763_000_000_000_000_000, "flags": 0,
        "prologue": prologue,
    }


def default_maps(code_address: int, code_size: int) -> bytes:
    return (
        "555555554000-555555c00000 r-xp 00000000 103:03 4242      /synth/qemu-system-arm\n"
        f"{code_address:x}-{code_address + code_size:x} rwxp 00000000 00:00 0 \n"
        "7ffff7c00000-7ffff7c28000 r-xp 00000000 103:03 77        /usr/lib/libc.so.6\n"
    ).encode()


DEFAULT_RAMBLOCKS = (
    {"idstr": b"mps.ram", "ram_offset": 0, "used_length": 0x400000, "max_length": 0x400000,
     "host": 0x7FFF00000000},
)


def tb_chunk_for(r: ResolvedUnit, slot: int, commit_seq: int,
                 run_info: Mapping[str, Any] | None = None) -> S.TbChunk:
    """The TB chunk the capture would write for resolved unit ``r``."""
    u, m = r.unit, r.unit.meta
    insns = r.insns
    insn0 = m.get("insn0_start", insns[0].host_start if insns else 0)
    body_end = m.get("body_end", insns[-1].host_end if insns else len(u.code))
    ldst = r.arrays["ldst"]
    ldst_end = m.get("ldst_end", max([body_end] + [d.slow_end for d in ldst]))
    pool = r.arrays["pool"]
    pool_start = m.get("pool_start", min(p.slot_off for p in pool) if pool else NONE_U32)
    flags = m.get("flags", int(TbFlag.IRQ_CHECK)
                  if insn0 > 0 and not u.key.cflags & CF_NOIRQ else 0)
    return S.TbChunk(
        slot=m.get("slot", slot), commit_seq=m.get("commit_seq", commit_seq),
        record=bytes(m.get("scm_record", r.record_bytes)), vaddr=u.vaddr,
        code_fnv64=m.get("code_fnv64", fnv1a64(r.arena_bytes)), attempts=m.get("attempts", 1),
        flags=flags, insn0_start=insn0, body_end=body_end, ldst_end=ldst_end,
        pool_start=pool_start, code_size=m.get("code_size", len(u.code)), insns=insns,
        guest_bytes=bytes(m.get("scm_guest_bytes", u.guest_bytes)),
        tb_chunk_version=m.get("tb_chunk_version", L.SCM_TB_CHUNK_VERSION),
        insn_start_words=m.get("insn_start_words", 3), reserved=m.get("reserved", 0),
        **r.arrays)


def build_scm(units: Sequence[SynthUnit], *, run_info: Mapping[str, Any] | None = None,
              helpers: Iterable[Mapping[str, Any]] = (), first_slot: int = 0,
              with_end: bool = True, path: str | None = None,
              sc_header: Mapping[str, Any] | None = None,
              header: Mapping[str, Any] | None = None, maps: bytes | None = None,
              ramblocks: Sequence[Mapping[str, Any]] | None = None,
              end: Mapping[str, Any] | None = None,
              extra_chunks: Sequence[tuple[int, bytes]] = (),
              all_units: Sequence[SynthUnit] | None = None) -> bytes:
    """A v1 ``.scm`` binding to the ``.sc`` that ``build_sc(units)`` produces.

    ``units[i]`` is written as the TB chunk of slot ``first_slot + i``. Pass the
    same ``sc_header`` overrides that were given to ``build_sc`` (and, when this
    capture covers only part of the ``.sc``, the full unit list as
    ``all_units`` so that the auto-sized ``code_size`` agrees). ``run_info``
    overrides any RUN_INFO field (including ``prologue``). Chunk order follows
    the capture: RUN_INFO, HELPERs, TB..., with MAPS(first_commit) and
    RAMBLOCKS after the first TB chunk, ``extra_chunks`` (raw ``(type,
    payload)``), and, when ``with_end``, MAPS(exit) and END.
    """
    sc_h = dict(sc_header or {})
    resolved = [resolve_unit(u, tb_size=sc_h.get("tb_size", DEFAULT_TB_SIZE),
                             target_page_bits=sc_h.get("target_page_bits",
                                                       DEFAULT_TARGET_PAGE_BITS))
                for u in units]
    sc = sc_header_fields(all_units if all_units is not None else units, sc_h,
                          None if all_units is not None else resolved)
    ri_fields = default_run_info(sc, first_slot=first_slot)
    ri_fields.update(run_info or {})
    ri = S.RunInfo(**ri_fields)

    fh = {"capture_level": ri.capture_level, "pid": DEFAULT_SCM_PID,
          "created_realtime_ns": DEFAULT_SCM_REALTIME_NS}
    fh.update(header or {})
    file_header = S.ScmFileHeader(**fh)

    helper_bodies = []
    for i, h in enumerate(helpers):
        name = h.get("name")
        if isinstance(name, str):
            name = name.encode()
        helper_bodies.append(S.HelperInfo(helper_id=h.get("helper_id", i),
                                          tcg_flags=h.get("tcg_flags", 0),
                                          typemask=h.get("typemask", 0),
                                          func=h.get("func", 0x555555600000 + 0x40 * i),
                                          name=name or b""))

    bodies: list[Any] = [ri, *helper_bodies]
    tb_chunks: list[S.TbChunk] = []
    for i, r in enumerate(resolved):
        if not r.unit.meta.get("scm", True):
            continue
        tb = tb_chunk_for(r, first_slot + i, len(tb_chunks), ri_fields)
        tb_chunks.append(tb)
        bodies.append(tb)
        if len(tb_chunks) == 1:
            bodies.append(S.MapsChunk(int(MapsTag.FIRST_COMMIT), 0,
                                      maps if maps is not None else default_maps(
                                          ri.code_address, ri.code_size)))
            rows = ramblocks if ramblocks is not None else DEFAULT_RAMBLOCKS
            bodies.append(S.RamblocksChunk(tuple(S.RamBlock(**dict(row)) for row in rows)))
    bodies.extend((t, S.RawChunkBody(p)) for t, p in extra_chunks)
    if with_end:
        bodies.append(S.MapsChunk(int(MapsTag.EXIT), 0, maps if maps is not None else
                                  default_maps(ri.code_address, ri.code_size)))
        e = {"committed": len(tb_chunks), "discarded": 0,
             "overflow": sum(1 for t in tb_chunks if t.flags & TbFlag.CAPTURE_OVERFLOW),
             "capture_error": sum(1 for t in tb_chunks if t.flags & TbFlag.CAPTURE_ERROR),
             "write_errors": 0, "helpers": len(helper_bodies), "clean": 1}
        e.update(end or {})
        bodies.append(S.EndChunk(**e))

    chunks = []
    for seq, b in enumerate(bodies):
        t, body = (b if isinstance(b, tuple) else (S.body_type(b), b))
        chunks.append(S.ScmChunk(seq=seq, type=int(t), body=body))
    data = S.encode_scm((file_header, chunks))
    if path is not None:
        with open(path, "wb") as f:
            f.write(data)
    return data


def build_pair(units: Sequence[SynthUnit], directory: str, *, name: str = "synth",
               sc_header: Mapping[str, Any] | None = None, **scm_kw) -> tuple[str, str]:
    """Write ``<name>.sc`` and a matching ``<name>.scm`` into ``directory``."""
    sc_path = os.path.join(directory, f"{name}.sc")
    scm_path = os.path.join(directory, f"{name}.scm")
    build_sc(units, sc_header, path=sc_path)
    build_scm(units, sc_header=sc_header, path=scm_path, **scm_kw)
    return sc_path, scm_path


# --------------------------------------------------------------------------
# A realistic minimal TB (x86-64 bytes + consistent capture metadata)
# --------------------------------------------------------------------------

def simple_tb(tb_offset: int, *, key: TBKey = DEFAULT_KEY, vaddr: int | None = None,
              guest_bytes: bytes = b"\x00\xbf\x00\xbf", irq_check: bool = True,
              code_address: int = DEFAULT_CODE_ADDRESS, tb_size: int = DEFAULT_TB_SIZE,
              icache_linesize: int = DEFAULT_ICACHE_LINESIZE,
              tb_ret_off: int = SYNTH_TB_RET_OFF,
              target_page_bits: int = DEFAULT_TARGET_PAGE_BITS, icount_entry: bool = False,
              body_prefix: bytes = b"") -> SynthUnit:
    """A two-insn TB shaped like QEMU's x86-64 output for a Thumb block ending in exit_tb.

    Layout (offsets from tc)::

        [irq check]  mov eax,[rbp-0x10]; test eax,eax; jl L      LABEL_JCC (TB start)
        insn 0       mov dword [rbp+0x3c], imm32
        insn 1       lea rax,[rip+tb+0]; jmp tb_ret               MOVI_EXIT_TB_PTR(0), EXIT_TB_RET_JMP(0)
        L:           lea rax,[rip+tb+3]; jmp tb_ret               MOVI_EXIT_TB_PTR(3), EXIT_TB_RET_JMP(3)

    Every reference has a captured site with the right ``value`` and
    ``final_field``, so a capstone sweep matches the capture one to one.
    Insn and site ``insn_idx`` are 0-based. Without ``irq_check`` the key gets
    CF_NOIRQ and the check and label are omitted.
    """
    if not irq_check:
        key = replace(key, cflags=key.cflags | CF_NOIRQ)
    if vaddr is None:
        vaddr = 0x20000000 | (key.page_addr0 & ((1 << target_page_bits) - 1))
    tc = tb_offset + round_up(tb_size, icache_linesize)
    base = code_address + tc
    tb_abs = code_address + tb_offset
    tb_ret = code_address + tb_ret_off
    code = bytearray()
    sites: list[dict[str, Any]] = []
    opmarks: list[dict[str, Any]] = []

    def rel32(target: int, next_ip_off: int) -> bytes:
        return (target - (base + next_ip_off)).to_bytes(4, "little", signed=True)

    jl_at = None
    if icount_entry:
        if not irq_check:
            raise ValueError("icount entry requires IRQ checks")
        key = replace(key, cflags=key.cflags | 0x2000)
    if irq_check:
        code += bytes.fromhex("8b45f8 83c0fe 85c0" if icount_entry else "8b45f0 85c0")
        jl_at = len(code)
        code += b"\x0f\x8c" + b"\0\0\0\0"  # patched below
    if icount_entry:
        code += bytes.fromhex("668945f8")
    insn0_start = len(code)
    code += body_prefix
    code += bytes.fromhex("c7453c") + (vaddr & 0xFFFFFFFF).to_bytes(4, "little")
    insn0_end = len(code)

    def exit_tb(idx: int, insn_idx: int) -> None:
        at = len(code)
        code.extend(b"\x48\x8d\x05" + rel32(tb_abs + idx, at + 7))
        sites.append({"field_off": at + 3, "insn_off": at, "width": 4, "enc": Encoding.RIPREL32,
                      "kind": SiteKind.MOVI_EXIT_TB_PTR, "aux": idx, "value": tb_abs + idx,
                      "insn_idx": insn_idx})
        opmarks.append({"host_off": at, "kind": OpMarkKind.EXIT_TB, "insn_idx": insn_idx})
        j = len(code)
        code.extend(b"\xe9" + rel32(tb_ret, j + 5))
        sites.append({"field_off": j + 1, "insn_off": j, "width": 4, "enc": Encoding.REL32,
                      "kind": SiteKind.EXIT_TB_RET_JMP, "aux": idx, "value": tb_ret,
                      "insn_idx": insn_idx})

    exit_tb(0, 1)
    if irq_check:
        label = len(code)
        exit_tb(3, 1)
        code[jl_at + 2: jl_at + 6] = rel32(base + label, jl_at + 6)
        sites.insert(0, {"field_off": jl_at + 2, "insn_off": jl_at, "width": 4,
                         "enc": Encoding.REL32, "kind": SiteKind.LABEL_JCC,
                         "aux": 0, "value": base + label, "insn_idx": None})
    body_end = len(code)
    w0 = vaddr & ((1 << target_page_bits) - 1)
    insns = [S.ScmInsn((w0, 0, 0), insn0_start, insn0_end),
             S.ScmInsn((w0 + 2, 0, 0), insn0_end, body_end)]
    return SynthUnit(key=key, tb_offset=tb_offset, code=bytes(code), guest_bytes=guest_bytes,
                     icount=2, vaddr=vaddr,
                     meta={"insns": insns, "sites": sites, "opmarks": opmarks,
                           "insn0_start": insn0_start, "body_end": body_end})


def simple_units(n: int, *, start: int = 64, **kw) -> list[SynthUnit]:
    """``n`` distinct :func:`simple_tb` units placed back to back like tcg_tb_alloc."""
    out: list[SynthUnit] = []
    base_key = kw.pop("key", DEFAULT_KEY)
    off = round_up(start, DEFAULT_ICACHE_LINESIZE)
    for i in range(n):
        key = replace(base_key, page_addr0=base_key.page_addr0 + 4 * i)
        u = simple_tb(off, key=key, guest_bytes=bytes([i & 0xFF, 0xBF, 0x00, 0xBF]), **kw)
        out.append(u)
        off = round_up(resolve_unit(u).end_offset, DEFAULT_ICACHE_LINESIZE)
    return out


# --------------------------------------------------------------------------
# .qprof v1 (WP6)
# --------------------------------------------------------------------------

def build_qprof(tb_rows: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]], *,
                phases: Sequence[Mapping[str, Any]] = ({"id": 0, "name": "boot", "marker": 0},),
                header: Mapping[str, Any] | None = None, path: str | None = None) -> bytes:
    """A v1 ``.qprof`` with the given rows (WP6).

    ``tb_rows``: mappings with ``tb_offset`` and optionally ``first_vaddr``,
    ``page_addr0``, ``page_addr1`` (raw; default none), ``cs_base``, ``flags``,
    ``cflags``, ``size``, ``icount``, ``pad``, ``reserved`` and ``phase``: a
    sequence (one per phase) or a ``{phase_id: counts}`` mapping of
    ``{entries, aborted, longjmp, lookup_hits}`` (missing = 0). A ``key``
    (:class:`TBKey`) may replace the five key fields.

    ``edges``: mappings with ``src`` (tb_offset or None for NONE), ``dst``,
    ``kind`` (int or :class:`QprofEdgeKind` name), ``flags`` (int), ``phase``,
    ``count``.

    ``phases``: ``{id, name, marker, reached (default 1), first_dispatch_index
    (default 0)}``.

    ``header`` overrides any :class:`QprofHeader` field. Defaults: B0
    ``code_address``/``runtime_signature``, ``flags=CLEAN_EXIT``,
    ``total_dispatches = Σ entries``, counts from the rows, valid checksums.
    Special keys: ``checksum`` forces the header checksum, ``trailer`` forces
    the body trailer, ``keep_counts=True`` keeps given ``n_*`` values.
    """
    from scopt.formats import qprof_v1 as Q
    from scopt.formats.enums import QprofEdgeKind, QprofHeaderFlag

    hdr = dict(header or {})
    forced_ck = hdr.pop("checksum", None)
    forced_tr = hdr.pop("trailer", None)
    keep_counts = hdr.pop("keep_counts", False)
    n_ph = len(phases)
    ph_rows = []
    for i, p in enumerate(phases):
        ph_rows.append(Q.QprofPhase(int(p.get("id", i)), int(p.get("reached", 1)),
                                    int(p.get("marker", 0) or 0),
                                    int(p.get("first_dispatch_index", 0)), str(p["name"])))
    tbs = []
    for r in tb_rows:
        key = r.get("key")
        if key is not None:
            kf = dict(page_addr0=key.page_addr0, page_addr1=key.page_addr1_raw,
                      flags=key.flags, cs_base=key.cs_base, cflags=key.cflags)
        else:
            kf = dict(page_addr0=r.get("page_addr0", DEFAULT_KEY.page_addr0),
                      page_addr1=r.get("page_addr1", 0xFFFF_FFFF_FFFF_FFFF),
                      flags=r.get("flags", DEFAULT_KEY.flags),
                      cs_base=r.get("cs_base", DEFAULT_KEY.cs_base),
                      cflags=r.get("cflags", DEFAULT_KEY.cflags))
        per_in = r.get("phase", ())
        if isinstance(per_in, Mapping):
            per_in = [per_in.get(i, per_in.get(str(i), {})) for i in range(n_ph)]
        per = []
        for i in range(n_ph):
            c = per_in[i] if i < len(per_in) else {}
            if isinstance(c, Q.QprofTbPhase):
                per.append(c)
            else:
                per.append(Q.QprofTbPhase(int(c.get("entries", 0)), int(c.get("aborted", 0)),
                                          int(c.get("longjmp", 0)),
                                          int(c.get("lookup_hits", 0))))
        tbs.append(Q.QprofTbRow(int(r["tb_offset"]), int(r.get("first_vaddr", 0)),
                                kf["page_addr0"], kf["page_addr1"], kf["cs_base"], kf["flags"],
                                kf["cflags"], int(r.get("size", 4)), int(r.get("icount", 2)),
                                int(r.get("pad", 0)), tuple(r.get("reserved", (0, 0))),
                                tuple(per)))
    ed = []
    for e in edges:
        kind = e["kind"]
        if isinstance(kind, str):
            kind = QprofEdgeKind[kind.upper()]
        src = e.get("src")
        ed.append(Q.QprofEdge(Q.NONE_SRC if src is None else int(src), int(e["dst"]), int(kind),
                              int(e.get("flags", 0)), int(e.get("phase", 0)),
                              int(e.get("count", 1))))
    h: dict[str, Any] = {
        "magic": L.QPROF_MAGIC, "version": L.QPROF_VERSION, "header_size": L.QPROF_HEADER.size,
        "code_address": DEFAULT_CODE_ADDRESS, "runtime_signature": DEFAULT_RUNTIME_SIGNATURE,
        "loaded_record_count": 0, "loaded_max_end": 0, "loaded_header_checksum": 0,
        "n_phases": n_ph, "flags": int(QprofHeaderFlag.CLEAN_EXIT),
        "n_tb_rows": len(tbs), "n_edge_rows": len(ed),
        "total_dispatches": sum(t.entries for t in tbs),
        "created_realtime_ns": DEFAULT_SCM_REALTIME_NS, "reserved": (0, 0, 0), "checksum": 0,
    }
    h.update(hdr)
    data = Q.encode_qprof(Q.QprofHeader(**h), ph_rows, tbs, ed, fix_counts=not keep_counts,
                          trailer=forced_tr)
    if forced_ck is not None:
        data = data[:120] + int(forced_ck).to_bytes(8, "little") + data[128:]
    if path is not None:
        with open(path, "wb") as f:
            f.write(data)
    return data


def qprof_binding(sc: bytes | str) -> dict[str, Any]:
    """The ``.qprof`` header fields that bind a profile to the snapshot ``sc``
    (bytes or a path): code_address, runtime_signature and the ``loaded_*``
    fields as QEMU computes them when it loads that file."""
    from scopt.formats.sc_records import read_sc
    import tempfile

    if isinstance(sc, (bytes, bytearray)):
        with tempfile.NamedTemporaryFile(suffix=".sc") as f:
            f.write(sc)
            f.flush()
            scf = read_sc(f.name)
    else:
        scf = read_sc(sc)
    return {"code_address": scf.header.code_address,
            "runtime_signature": scf.header.runtime_signature,
            "loaded_record_count": scf.header.record_count,
            "loaded_header_checksum": scf.header.checksum,
            "loaded_max_end": scf.max_end}


@dataclass(frozen=True)
class Dispatch:
    """One TB entry in a simulated profile run (for :func:`simulate_qprof`).

    ``exit`` is how this execution ended, which becomes the kind of the next
    entry's in-edge: ``chain0``, ``chain1``, ``lookup_hit``, ``lookup_miss``,
    ``exit_null``, ``exit_requested`` or ``longjmp`` (EXIT_NULL + LONGJMP flag).
    ``via_lookup`` marks the entry as a ``helper_lookup_tb_ptr`` hit (counted in
    ``lookup_hits``); ``flags`` (IRQ/EXCEPTION bits) are added to the in-edge.
    """

    tb_offset: int
    exit: str = "chain0"
    via_lookup: bool = False
    flags: int = 0


def simulate_qprof(rows: Sequence[Mapping[str, Any]], trace: Sequence[Dispatch | int], *,
                   phases: Sequence[Mapping[str, Any]] = ({"id": 0, "name": "boot",
                                                           "marker": 0},),
                   phase_starts: Mapping[int, int] | None = None,
                   header: Mapping[str, Any] | None = None, path: str | None = None) -> bytes:
    """Build a self-consistent ``.qprof`` from a dispatch trace, the way WP5's
    ``scp`` counts: one in-edge per entry (FIRST for the first), the edge kind
    from the previous entry's exit, the edge phase = the destination's phase.

    ``rows``: per TB (``tb_offset`` plus key fields, as for :func:`build_qprof`;
    the counts are computed). ``phase_starts``: ``{phase_id: dispatch index}``
    at which a phase begins (phase 0 begins at 0); phases without a start are
    not reached. Row order is first-dispatch order (as QEMU writes it).
    """
    from scopt.formats.enums import QprofEdgeFlag, QprofEdgeKind

    starts = {0: 0, **dict(phase_starts or {})}
    n_ph = len(phases)
    ph = []
    for i, p in enumerate(phases):
        q = dict(p)
        q.setdefault("id", i)
        if i in starts:
            q.setdefault("reached", 1)
            q.setdefault("first_dispatch_index", starts[i])
        else:
            q["reached"] = 0
            q["first_dispatch_index"] = 0
        ph.append(q)
    by_off = {int(r["tb_offset"]): dict(r) for r in rows}
    counts: dict[int, list[dict[str, int]]] = {}
    order: list[int] = []
    edges: dict[tuple, int] = {}
    edge_order: list[tuple] = []
    kinds = {"chain0": QprofEdgeKind.CHAIN0, "chain1": QprofEdgeKind.CHAIN1,
             "lookup_hit": QprofEdgeKind.LOOKUP_HIT, "lookup_miss": QprofEdgeKind.LOOKUP_MISS,
             "exit_null": QprofEdgeKind.EXIT_NULL,
             "exit_requested": QprofEdgeKind.EXIT_REQUESTED,
             "longjmp": QprofEdgeKind.EXIT_NULL}
    cur_phase = 0
    prev: Dispatch | None = None
    for idx, d in enumerate(trace):
        if isinstance(d, int):
            d = Dispatch(d)
        while cur_phase + 1 < n_ph and starts.get(cur_phase + 1) == idx:
            cur_phase += 1
        if d.tb_offset not in counts:
            counts[d.tb_offset] = [dict(entries=0, aborted=0, longjmp=0, lookup_hits=0)
                                   for _ in range(n_ph)]
            order.append(d.tb_offset)
        c = counts[d.tb_offset][cur_phase]
        c["entries"] += 1
        if d.via_lookup:
            c["lookup_hits"] += 1
        if prev is None:
            k = (None, d.tb_offset, int(QprofEdgeKind.FIRST), d.flags, cur_phase)
        else:
            fl = d.flags | (int(QprofEdgeFlag.LONGJMP) if prev.exit == "longjmp" else 0)
            k = (prev.tb_offset, d.tb_offset, int(kinds[prev.exit]), fl, cur_phase)
        if k not in edges:
            edges[k] = 0
            edge_order.append(k)
        edges[k] += 1
        if d.exit == "exit_requested":
            c["aborted"] += 1
        if d.exit == "longjmp":
            c["longjmp"] += 1
        prev = d
    tb_rows = []
    for off in order:
        r = by_off.get(off, {"tb_offset": off})
        tb_rows.append({**r, "tb_offset": off, "phase": counts[off]})
    edge_rows = [{"src": s, "dst": t, "kind": k, "flags": f, "phase": p, "count": edges[(s, t, k, f, p)]}
                 for (s, t, k, f, p) in edge_order]
    return build_qprof(tb_rows, edge_rows, phases=ph, header=header, path=path)


def qprof_rows_for(units: Sequence[SynthUnit], **kw) -> list[dict[str, Any]]:
    """``.qprof`` TB-row key fields for synthetic units (as QEMU copies them from
    the TB struct): tb_offset, first_vaddr, key, size, icount."""
    out = []
    for u in units:
        out.append({"tb_offset": u.tb_offset, "first_vaddr": u.vaddr, "key": u.key,
                    "size": len(u.guest_bytes), "icount": u.icount, **kw})
    return out


# --------------------------------------------------------------------------
# Corruption helpers (WP4): each returns modified bytes.
# --------------------------------------------------------------------------

def chunk_table(scm: bytes) -> list[dict[str, int]]:
    """Framing walk: ``{seq, type, offset, length, payload_len}`` per chunk (stops at damage)."""
    out, off = [], L.SCM_FILE_HEADER.size
    while off + L.SCM_CHUNK_HEADER.size <= len(scm):
        typ, plen, seq = L.SCM_CHUNK_HEADER.unpack_from(scm, off)
        total = L.SCM_CHUNK_HEADER.size + L.align8(plen) + 8
        if off + total > len(scm):
            break
        out.append({"seq": seq, "type": typ, "offset": off, "length": total, "payload_len": plen})
        off += total
    return out


def _chunk(scm: bytes, chunk_seq: int) -> dict[str, int]:
    for c in chunk_table(scm):
        if c["seq"] == chunk_seq:
            return c
    raise KeyError(f"no chunk with seq {chunk_seq}")


def flip_byte(data: bytes, offset: int, mask: int = 0x01) -> bytes:
    """XOR one byte with ``mask``."""
    b = bytearray(data)
    b[offset] ^= mask
    return bytes(b)


def drop_site(scm: bytes, slot: int, site_index: int) -> bytes:
    """Remove ``sites[site_index]`` from the TB chunk of ``slot``; re-frame (valid trailers).

    Models a capture that missed a reference (the importer must report
    ``unexplained_reference``)."""
    f = S.parse_scm(scm)
    for ch in f.chunks:
        if isinstance(ch.body, S.TbChunk) and ch.body.slot == slot:
            tb = ch.body
            sites = tb.sites[:site_index] + tb.sites[site_index + 1:]
            if len(sites) == len(tb.sites):
                raise IndexError(site_index)
            return S.encode_scm(S.replace_body(f, ch.seq, replace(tb, sites=sites)))
    raise KeyError(f"no TB chunk for slot {slot}")


def truncate_chunk(scm: bytes, chunk_seq: int, keep: int) -> bytes:
    """Cut the file ``keep`` bytes into chunk ``chunk_seq`` (a torn write at exit)."""
    c = _chunk(scm, chunk_seq)
    if not 0 <= keep < c["length"]:
        raise ValueError(f"keep must be in [0, {c['length']})")
    return bytes(scm[: c["offset"] + keep])


def swap_run(scm: bytes, other_run_info: Mapping[str, Any]) -> bytes:
    """Replace RUN_INFO fields (e.g. ``code_address``, ``runtime_signature``); re-frame.

    Models a ``.scm`` left over from another run/cache: it parses cleanly but
    must not bind (§2.5.8)."""
    f = S.parse_scm(scm)
    for ch in f.chunks:
        if isinstance(ch.body, S.RunInfo):
            return S.encode_scm(S.replace_body(f, ch.seq, replace(ch.body, **other_run_info)))
    raise KeyError("no RUN_INFO chunk")


def corrupt_trailer(scm: bytes, chunk_seq: int) -> bytes:
    """Flip the low byte of chunk ``chunk_seq``'s trailer."""
    c = _chunk(scm, chunk_seq)
    return flip_byte(scm, c["offset"] + c["length"] - 8, 0xFF)


def set_chunk_seq(scm: bytes, chunk_seq: int, new_seq: int) -> bytes:
    """Rewrite one chunk's ``seq`` and recompute its trailer (a seq gap with valid framing)."""
    c = _chunk(scm, chunk_seq)
    o, n = c["offset"], c["length"]
    raw = bytearray(scm[o: o + n - 8])
    raw[8:16] = new_seq.to_bytes(8, "little")
    framed = bytes(raw) + fnv1a64(bytes(raw)).to_bytes(8, "little")
    return bytes(scm[:o]) + framed + bytes(scm[o + n:])


def drop_chunk(scm: bytes, chunk_seq: int) -> bytes:
    """Remove chunk ``chunk_seq`` entirely (later seqs are not renumbered)."""
    c = _chunk(scm, chunk_seq)
    return bytes(scm[: c["offset"]]) + bytes(scm[c["offset"] + c["length"]:])


def append_raw_chunk(scm: bytes, type_: int, payload: bytes, seq: int | None = None) -> bytes:
    """Append a correctly framed chunk (``seq`` defaults to the next one)."""
    tab = chunk_table(scm)
    if seq is None:
        seq = tab[-1]["seq"] + 1 if tab else 0
    return bytes(scm) + S.frame_chunk(type_, seq, payload)


__all__ = [
    "SynthUnit", "ResolvedUnit", "build_sc", "build_scm", "build_qprof", "build_pair",
    "resolve_unit", "sc_header_fields", "default_run_info", "tb_chunk_for", "auto_place",
    "simple_tb", "simple_units", "encode_sleb128", "encode_search", "chunk_table",
    "flip_byte", "drop_site", "truncate_chunk", "swap_run", "corrupt_trailer",
    "set_chunk_seq", "drop_chunk", "append_raw_chunk", "DEFAULT_KEY", "CF_PCREL", "CF_NOIRQ",
    "SYNTH_PROLOGUE", "SYNTH_TB_RET_OFF", "SYNTH_EPILOGUE_OFF", "DEFAULT_CODE_ADDRESS",
]
