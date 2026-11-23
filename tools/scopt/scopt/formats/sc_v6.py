"""``.sc`` v6 writer and reader (B3/B4 design §5.2-5.4, §9.1 V11). WP18.

A v6 file is a v5 file (:mod:`scopt.formats.sc_v5`) with

* header ``version = 6`` and ``reserved`` (byte 68) used as ``feature_flags``:
  bit0 EXT_TABLE (must be set), bit1 VARIANTS, bit2 GUARDED_SLOTS, bit3
  REWRITTEN_RECORDS (informational); covered by the header checksum;
* an **extension table** at file offset 0x100 (inside the untouched
  ``[80, 0x10000)`` gap): a 64-byte :class:`ExtHeader`, then from 0x140 the
  arrays ``variants`` (64 B each), ``slots`` (32 B) and ``links`` (16 B);
* **variant bodies** in the arena (file offset ``0x10000 + tc_offset``), outside
  every record: ``[tc_offset, end_offset)`` of each variant (its struct
  ``[tb_offset, tc_offset)`` stays a hole, as for records).

Records keep the v5 104-byte layout; grown and rewritten units are ordinary
records.

The reader mirrors the ``qint-v6`` loader (WP22, ``tcg/supercache.c``
``sc6_load_ext`` + ``sc6_verify_hashes``): :func:`ext_problem` returns the
first check that fails, as ``(code, message)``, where ``message`` is the text
the loader prints after ``supercache: invalid v6 extension:`` (the loader exits
before any guest instruction on such a file, design §5.1). The order of the
checks is the loader's. The rules beyond the §5.3 table that the loader added
(design implementation note WP22 item 2) are included: ``guard_off +
guard_len <= R.tc_size``; R's registered jump field (+4) and reset offset lie in
the guard range; the jump field is 4-aligned; no duplicate ``(R, n)``;
VARIANTS/GUARDED_SLOTS feature bits iff the arrays are non-empty; variant
``flags`` bits beyond bit0 and every ``reserved`` field are zero; a link's
target has C's page offset; duplicate record ``tb_offset`` refuses the file;
an all-zero ``artifact_id8`` is refused.

:func:`write_sc6` is the packager's writer: it computes ``feature_flags``,
``variants_max_end`` and the checksum, writes through
:func:`scopt.formats.sc_v5.write_sc` (sparse), and by default re-reads the
file and refuses to leave one the loader would reject (``ScFormatError`` with
the loader's reason). The independent parser
``independent/sc6parse.py`` (written from the C loader) is run by
:func:`sc6parse_compare` (V11).
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..canon import fnv1a64
from .layouts import SC_CODE_OFFSET
from .sc_v5 import (ScFile, ScFormatError, ScHeader, ScRecord, _pread_exact, read_sc,
                    structural_findings, write_sc)

__all__ = [
    "SC6_VERSION", "EXT_OFFSET", "ARRAYS_OFFSET", "EXT_MAGIC", "FEAT_EXT_TABLE",
    "FEAT_VARIANTS", "FEAT_GUARDED_SLOTS", "FEAT_REWRITTEN", "KNOWN_FEATURES",
    "KIND_GENERAL", "KIND_SPEC", "EXIT_RETURN", "EXIT_STATIC", "EXIT_DIRECT", "VARIANT_FACTS",
    "VariantEntry", "SlotEntry", "LinkEntry", "ExtTable", "Sc6File", "read_sc6", "write_sc6",
    "ext_problem", "sc6parse_compare", "SC6PARSE", "max_ext_entries", "TB_STRUCT_ROUND",
    "SC7_SEMANTIC_MARKER", "sc6_snapshot_id", "ScExtRefused",
]

SC6_VERSION = 6
EXT_OFFSET = 0x100
ARRAYS_OFFSET = 0x140
EXT_MAGIC = b"QSCX6EXT"
FEAT_EXT_TABLE = 1 << 0
FEAT_VARIANTS = 1 << 1
FEAT_GUARDED_SLOTS = 1 << 2
FEAT_REWRITTEN = 1 << 3
KNOWN_FEATURES = 0xF
KIND_GENERAL = 1
KIND_SPEC = 2
EXIT_RETURN = 1
EXIT_STATIC = 2
EXIT_DIRECT = 3
VARIANT_FACTS = 1 << 0
NONE16 = 0xFFFF
TB_STRUCT_ROUND = 64
#: Top-level v7 marker for the legacy semantic-extension payload.  The low
#: bits retain the v6 feature flags, and the payload remains at ``0x100``.
#: Firmware v7 uses bit 0 without this marker.
SC7_SEMANTIC_MARKER = 1 << 31

_EXT_HDR = struct.Struct("<8sIIIIIHHHHIQ8sQ")
_VARIANT = struct.Struct("<QQQQQQIIQ")
_SLOT = struct.Struct("<QBBBBIIIQ")
_LINK = struct.Struct("<IIQ")
assert (_EXT_HDR.size, _VARIANT.size, _SLOT.size, _LINK.size) == (64, 64, 32, 16)

SC6PARSE = Path(os.environ.get(
    "SCOPT_SC6PARSE", Path(__file__).resolve().parents[2] / "independent/sc6parse.py"))


def max_ext_entries(nv: int, ns: int, nl: int) -> bool:
    """True when the arrays fit before the arena (``ext_table_too_large`` otherwise)."""
    return ARRAYS_OFFSET + 64 * nv + 32 * ns + 16 * nl <= SC_CODE_OFFSET


# ---------------------------------------------------------------------------
# entries
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VariantEntry:
    general_tb_offset: int
    tb_offset: int
    tc_offset: int
    tc_size: int
    end_offset: int
    code_fnv64: int
    variant_id: int
    flags: int = 0
    reserved: int = 0

    def encode(self) -> bytes:
        return _VARIANT.pack(self.general_tb_offset, self.tb_offset, self.tc_offset,
                             self.tc_size, self.end_offset, self.code_fnv64, self.variant_id,
                             self.flags, self.reserved)

    @classmethod
    def decode(cls, b: bytes, off: int = 0) -> "VariantEntry":
        return cls(*_VARIANT.unpack_from(b, off))


@dataclass(frozen=True)
class SlotEntry:
    src_tb_offset: int
    slot_n: int
    kind: int
    exit_class: int
    target_vaddr: int
    guard_off: int
    guard_len: int
    guard_fnv64: int
    reserved: int = 0

    def encode(self) -> bytes:
        return _SLOT.pack(self.src_tb_offset, self.slot_n, self.kind, self.exit_class,
                          self.reserved, self.target_vaddr, self.guard_off, self.guard_len,
                          self.guard_fnv64)

    @classmethod
    def decode(cls, b: bytes, off: int = 0) -> "SlotEntry":
        src, n, kind, xc, res, vc, goff, glen, gfnv = _SLOT.unpack_from(b, off)
        return cls(src, n, kind, xc, vc, goff, glen, gfnv, res)


@dataclass(frozen=True)
class LinkEntry:
    slot_index: int
    variant_index: int
    reserved: int = 0

    def encode(self) -> bytes:
        return _LINK.pack(self.slot_index, self.variant_index, self.reserved)

    @classmethod
    def decode(cls, b: bytes, off: int = 0) -> "LinkEntry":
        return cls(*_LINK.unpack_from(b, off))


@dataclass(frozen=True)
class ExtTable:
    """The extension table. ``variants_max_end`` / ``checksum`` / sizes default to
    the values a correct packager writes; tests override them to build mutants."""

    variants: tuple[VariantEntry, ...] = ()
    slots: tuple[SlotEntry, ...] = ()
    links: tuple[LinkEntry, ...] = ()
    artifact_id8: bytes = b"\0" * 8
    magic: bytes = EXT_MAGIC
    ext_version: int = 1
    ext_header_size: int = 64
    variant_entry_size: int = 64
    slot_entry_size: int = 32
    link_entry_size: int = 16
    reserved0: int = 0
    reserved1: int = 0
    n_variants: int | None = None  # None: len(variants)
    n_slots: int | None = None
    n_links: int | None = None
    variants_max_end: int | None = None  # None: computed
    checksum: int | None = None  # None: computed

    @property
    def computed_max_end(self) -> int:
        return max((v.end_offset for v in self.variants), default=0)

    def arrays(self) -> bytes:
        return (b"".join(v.encode() for v in self.variants) +
                b"".join(s.encode() for s in self.slots) +
                b"".join(lk.encode() for lk in self.links))

    def header_bytes(self, *, with_checksum: bool = True) -> bytes:
        nv = len(self.variants) if self.n_variants is None else self.n_variants
        ns = len(self.slots) if self.n_slots is None else self.n_slots
        nl = len(self.links) if self.n_links is None else self.n_links
        vmax = self.computed_max_end if self.variants_max_end is None else self.variants_max_end
        raw = _EXT_HDR.pack(self.magic, self.ext_version, self.ext_header_size, nv, ns, nl,
                            self.variant_entry_size, self.slot_entry_size, self.link_entry_size,
                            self.reserved0, self.reserved1, vmax, bytes(self.artifact_id8), 0)
        if not with_checksum:
            return raw
        ck = self.checksum if self.checksum is not None else \
            fnv1a64(self.arrays(), fnv1a64(raw[:56]))
        return raw[:56] + struct.pack("<Q", ck)

    def encode(self) -> bytes:
        """Header followed by the arrays (written at ``EXT_OFFSET``)."""
        return self.header_bytes() + self.arrays()

    def features(self, *, rewritten: bool = False) -> int:
        f = FEAT_EXT_TABLE
        if self.variants:
            f |= FEAT_VARIANTS
        if self.slots:
            f |= FEAT_GUARDED_SLOTS
        if rewritten:
            f |= FEAT_REWRITTEN
        return f

    def to_json(self) -> dict[str, Any]:
        return {"artifact_id8": bytes(self.artifact_id8).hex(),
                "variants_max_end": hex(self.computed_max_end if self.variants_max_end is None
                                        else self.variants_max_end),
                "variants": [asdict(v) for v in self.variants],
                "slots": [asdict(s) for s in self.slots],
                "links": [asdict(lk) for lk in self.links]}

    @classmethod
    def decode(cls, hdr: bytes, arrays: bytes) -> "ExtTable":
        (magic, xv, xhs, nv, ns, nl, ves, ses, les, r0, r1, vmax, aid,
         ck) = _EXT_HDR.unpack(hdr[:64])
        vs = tuple(VariantEntry.decode(arrays, 64 * i) for i in range(nv))
        ss = tuple(SlotEntry.decode(arrays, 64 * nv + 32 * i) for i in range(ns))
        ls = tuple(LinkEntry.decode(arrays, 64 * nv + 32 * ns + 16 * i) for i in range(nl))
        return cls(vs, ss, ls, aid, magic, xv, xhs, ves, ses, les, r0, r1, nv, ns, nl, vmax, ck)


# ---------------------------------------------------------------------------
# the loader's checks
# ---------------------------------------------------------------------------

class ScExtRefused(ScFormatError):
    """The writer's check found a table the loader would refuse; ``code`` is the
    stable :func:`ext_problem` code (e.g. ``link_page_offset``)."""

    def __init__(self, code: str, msg: str):
        self.code = code
        super().__init__(f"{code}: {msg}")


class _Die(Exception):
    def __init__(self, code: str, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(msg)


def _round_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a


def ext_problem(header: ScHeader, loaded: Sequence[ScRecord], read: Any, *,
                host_page_size: int | None = None, check_hashes: bool = True
                ) -> tuple[ExtTable | None, tuple[str, str] | None]:
    """``(table, None)`` when the v6 loader accepts the file, else ``(table or None,
    (code, message))`` for the first failing check.

    ``loaded`` are the records ``load_records`` keeps (valid, file-slot order);
    ``read(off, n)`` reads ``n`` file bytes at ``off`` (zero-filled past EOF).
    ``host_page_size`` defaults to the header's (the loader uses the real host
    page size; equal on the pinned host).
    """
    page = header.host_page_size if host_page_size is None else host_page_size
    tab: ExtTable | None = None
    try:
        feat = header.reserved
        if feat & ~KNOWN_FEATURES:
            raise _Die("unknown_feature_bits", f"unknown feature bits 0x{feat & ~KNOWN_FEATURES:x}")
        if not feat & FEAT_EXT_TABLE:
            raise _Die("ext_table_clear", "feature EXT_TABLE clear")
        xb = read(EXT_OFFSET, 64)
        (magic, xv, xhs, nv, ns, nl, ves, ses, les, r0, r1, vmax, aid,
         ck) = _EXT_HDR.unpack(xb)
        if magic != EXT_MAGIC:
            raise _Die("ext_magic", "bad ext magic")
        if xv != 1:
            raise _Die("ext_version", f"bad ext_version {xv}")
        if xhs != 64:
            raise _Die("ext_header_size", f"bad ext_header_size {xhs}")
        if (ves, ses, les) != (64, 32, 16):
            raise _Die("entry_sizes", f"bad entry sizes {ves}/{ses}/{les}")
        if r0 or r1:
            raise _Die("ext_reserved", "reserved header fields not zero")
        asz = 64 * nv + 32 * ns + 16 * nl
        if ARRAYS_OFFSET + asz > SC_CODE_OFFSET:
            raise _Die("ext_table_too_large", "ext table too large")
        arrays = read(ARRAYS_OFFSET, asz)
        tab = ExtTable.decode(xb, arrays)
        if fnv1a64(arrays, fnv1a64(xb[:56])) != ck:
            raise _Die("ext_checksum", "ext checksum mismatch")
        if bool(feat & FEAT_VARIANTS) != bool(nv):
            raise _Die("feature_variants", f"feature VARIANTS disagrees with n_variants {nv}")
        if bool(feat & FEAT_GUARDED_SLOTS) != bool(ns):
            raise _Die("feature_slots", f"feature GUARDED_SLOTS disagrees with n_slots {ns}")
        if aid == b"\0" * 8:
            raise _Die("artifact_id_zero", "artifact id is zero")
        by_tb: dict[int, ScRecord] = {}
        for r in sorted(loaded, key=lambda r: r.tb_offset):
            if r.tb_offset in by_tb:
                raise _Die("duplicate_tb_offset", f"duplicate record tb_offset 0x{r.tb_offset:x}")
            by_tb[r.tb_offset] = r
        tb_struct = _round_up(header.tb_size, TB_STRUCT_ROUND)
        floor = _round_up(header.prologue_end, 64)
        limit = header.code_size - page - 1024
        max_end = 0
        for i, v in enumerate(tab.variants):
            c = by_tb.get(v.general_tb_offset)
            p = f"variant {i}: "
            if c is None:
                raise _Die("variant_general", p + f"general_tb_offset 0x{v.general_tb_offset:x} "
                           "names no valid record")
            if v.variant_id != i:
                raise _Die("variant_id", p + f"variant_id {v.variant_id} is not dense")
            if v.flags & ~VARIANT_FACTS:
                raise _Die("variant_flags", p + f"unknown flags 0x{v.flags:x}")
            if v.reserved:
                raise _Die("variant_reserved", p + "reserved not zero")
            if v.tb_offset % 64:
                raise _Die("variant_align", p + f"tb_offset 0x{v.tb_offset:x} not 64-aligned")
            if v.tc_offset < v.tb_offset or v.tc_offset - v.tb_offset != tb_struct:
                raise _Die("variant_tc_gap", p + f"tc_offset - tb_offset != {tb_struct}")
            if v.tc_size != c.tc_size:
                raise _Die("variant_tc_size", p + f"tc_size 0x{v.tc_size:x} != C tc_size "
                           f"0x{c.tc_size:x}")
            if v.end_offset != v.tc_offset + (c.end_offset - c.tc_offset):
                raise _Die("variant_extent", p + "end_offset does not match C's extent")
            if v.tb_offset < floor:
                raise _Die("variant_below_prologue", p + "below the prologue")
            if v.end_offset >= limit:
                raise _Die("variant_out_of_range", p + f"end_offset 0x{v.end_offset:x} out of range")
            for r in loaded:
                if v.tb_offset < r.end_offset and r.tb_offset < v.end_offset:
                    raise _Die("variant_overlaps_record",
                               p + f"overlaps record at tb_offset 0x{r.tb_offset:x}")
            for k in range(i):
                w = tab.variants[k]
                if v.tb_offset < w.end_offset and w.tb_offset < v.end_offset:
                    raise _Die("variant_overlaps_variant", p + f"overlaps variant {k}")
            max_end = max(max_end, v.end_offset)
        if vmax != max_end:
            raise _Die("variants_max_end", f"variants_max_end 0x{vmax:x} != 0x{max_end:x}")
        seen: dict[tuple[int, int], int] = {}
        for i, s in enumerate(tab.slots):
            p = f"slot {i}: "
            r = by_tb.get(s.src_tb_offset)
            if r is None:
                raise _Die("slot_src", p + f"src_tb_offset 0x{s.src_tb_offset:x} names no "
                           "valid record")
            if s.slot_n > 1:
                raise _Die("slot_n", p + f"slot_n {s.slot_n}")
            if s.kind not in (KIND_GENERAL, KIND_SPEC):
                raise _Die("slot_kind", p + f"bad kind {s.kind}")
            if s.exit_class not in (EXIT_RETURN, EXIT_STATIC, EXIT_DIRECT):
                raise _Die("slot_exit_class", p + f"bad exit_class {s.exit_class}")
            if s.exit_class == EXIT_DIRECT and s.kind != KIND_SPEC:
                raise _Die("direct_requires_variant", p + "ordinary direct edge requires a private variant")
            if s.reserved:
                raise _Die("slot_reserved", p + "reserved not zero")
            jmp, rst = r.jmp_insn_offset[s.slot_n], r.jmp_reset_offset[s.slot_n]
            if jmp == NONE16 or rst == NONE16:
                raise _Die("slot_unregistered", p + f"R has no registered jump {s.slot_n}")
            if s.guard_len == 0 or s.guard_off >= r.tc_size or \
                    s.guard_off + s.guard_len > r.tc_size:
                raise _Die("guard_range", p + f"guard [0x{s.guard_off:x}, +0x{s.guard_len:x}) "
                           "outside R's code")
            gend = s.guard_off + s.guard_len
            if jmp < s.guard_off or jmp + 4 > gend or rst < s.guard_off or rst >= gend:
                raise _Die("slot_jump_outside_guard",
                           p + f"jump {s.slot_n} (insn 0x{jmp:x}, reset 0x{rst:x}) outside the "
                           "guard")
            if (r.tc_offset + jmp) % 4:
                raise _Die("slot_jump_align", p + "jump field not 4-aligned")
            if (s.src_tb_offset, s.slot_n) in seen:
                raise _Die("slot_duplicate",
                           p + f"duplicates slot {seen[(s.src_tb_offset, s.slot_n)]}")
            seen[(s.src_tb_offset, s.slot_n)] = i
        slot_link: dict[int, int] = {}
        var_link: dict[int, int] = {}
        pmask = (1 << header.target_page_bits) - 1
        for i, lk in enumerate(tab.links):
            p = f"link {i}: "
            if lk.reserved:
                raise _Die("link_reserved", p + "reserved not zero")
            if lk.slot_index >= ns:
                raise _Die("link_slot_range", p + f"slot_index {lk.slot_index} out of range")
            if lk.variant_index >= nv:
                raise _Die("link_variant_range",
                           p + f"variant_index {lk.variant_index} out of range")
            if tab.slots[lk.slot_index].kind != KIND_SPEC:
                raise _Die("link_slot_not_spec", p + f"slot {lk.slot_index} is not SPEC")
            if lk.slot_index in slot_link:
                raise _Die("slot_two_links", f"slot {lk.slot_index}: more than one link")
            if lk.variant_index in var_link:
                raise _Die("variant_two_links", f"variant {lk.variant_index}: more than one link")
            c = by_tb[tab.variants[lk.variant_index].general_tb_offset]
            vc = tab.slots[lk.slot_index].target_vaddr
            if (vc & pmask) != (c.page_addr0 & pmask):
                raise _Die("link_page_offset", p + f"target 0x{vc:x} is not at C's page offset")
            slot_link[lk.slot_index] = i
            var_link[lk.variant_index] = i
        for i, s in enumerate(tab.slots):
            if s.kind == KIND_SPEC and i not in slot_link:
                raise _Die("spec_slot_unlinked", f"slot {i}: SPEC slot has no link")
        for i in range(nv):
            if i not in var_link:
                raise _Die("variant_unlinked", f"variant {i}: has no link")
        if check_hashes:
            for i, v in enumerate(tab.variants):
                if fnv1a64(read(SC_CODE_OFFSET + v.tc_offset, v.end_offset - v.tc_offset)) \
                        != v.code_fnv64:
                    raise _Die("variant_code_hash", f"variant {i}: code hash mismatch")
            for i, s in enumerate(tab.slots):
                r = by_tb[s.src_tb_offset]
                if fnv1a64(read(SC_CODE_OFFSET + r.tc_offset + s.guard_off, s.guard_len)) \
                        != s.guard_fnv64:
                    raise _Die("guard_hash", f"slot {i}: guard hash mismatch")
    except _Die as d:
        return tab, (d.code, d.msg)
    return tab, None


# ---------------------------------------------------------------------------
# reader
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Sc6File:
    """A parsed ``.sc`` (v5 or v6) as the ``qint-v6`` loader sees it."""

    sc: ScFile  # header/records/bodies (records loaded as load_records does)
    ext: ExtTable | None = None
    problem: tuple[str, str] | None = None  # (code, loader message) when v6 and refused
    variant_bodies: tuple[bytes, ...] = ()  # [tc, end) per variant (when ext parsed)

    @property
    def version(self) -> int:
        return self.sc.header.version

    @property
    def is_v6(self) -> bool:
        """False for every production artifact; retained as an API migration aid."""
        return False

    @property
    def is_semantic_v7(self) -> bool:
        return (self.sc.loadable and self.sc.header.version == 7 and
                bool(self.sc.header.reserved & SC7_SEMANTIC_MARKER))

    @property
    def has_ext(self) -> bool:
        return self.is_semantic_v7

    @property
    def loadable(self) -> bool:
        """The v6 loader would run on it (header valid, and no extension problem)."""
        return self.sc.loadable and self.problem is None

    @property
    def features(self) -> int:
        return (self.sc.header.reserved & KNOWN_FEATURES) if self.has_ext else 0

    @property
    def max_end(self) -> int:
        """``max_end`` after the v6 fold (records and variants)."""
        vm = max((v.end_offset for v in (self.ext.variants if self.ext else ())), default=0)
        return max(self.sc.max_end, vm)

    def covered_ranges(self) -> list[tuple[int, int]]:
        """File ranges the v6 parts occupy (for the zero-outside check)."""
        if not self.has_ext or self.ext is None:
            return []
        out = [(EXT_OFFSET, EXT_OFFSET + 64),
               (ARRAYS_OFFSET, ARRAYS_OFFSET + len(self.ext.arrays()))]
        out += [(SC_CODE_OFFSET + v.tc_offset, SC_CODE_OFFSET + v.end_offset)
                for v in self.ext.variants]
        return out

    def structural_findings(self, *, scan_zeros: bool = True):
        return structural_findings(self.sc, scan_zeros=scan_zeros,
                                   extra_covered=self.covered_ranges())

    def to_json(self) -> dict[str, Any]:
        return {"version": self.version, "loadable": self.loadable,
                "features": self.features,
                "problem": None if self.problem is None else
                {"code": self.problem[0], "message": self.problem[1]},
                "ext": None if self.ext is None else self.ext.to_json(),
                "records": len(self.sc.valid_slots), "max_end": hex(self.max_end)}


def read_sc6(path: str | os.PathLike, *, host_page_size: int | None = None,
             expect: Mapping[str, int] | None = None) -> Sc6File:
    """Read the semantic-extension payload of a canonical top-level-v7 cache."""
    path = os.fspath(path)
    sc = read_sc(path, expect=expect, versions=(7,))
    semantic_v7 = (sc.loadable and sc.header.version == 7 and
                   bool(sc.header.reserved & SC7_SEMANTIC_MARKER))
    if not sc.loadable or not semantic_v7:
        return Sc6File(sc)
    unknown = sc.header.reserved & ~(SC7_SEMANTIC_MARKER | KNOWN_FEATURES)
    if unknown:
        return Sc6File(sc, problem=("feature_flags",
                                    f"unknown feature bits 0x{unknown:x}"))
    ext_header = sc.header
    if semantic_v7:
        # The extension rules consume only the legacy low feature bits.
        ext_header = sc.header.replace(reserved=sc.header.reserved & KNOWN_FEATURES)
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        def read(off: int, n: int) -> bytes:
            b = _pread_exact(fd, n, off) if n else b""
            return b + bytes(n - len(b))

        loaded = [sc.records[i] for i in sc.valid_slots]
        tab, prob = ext_problem(ext_header, loaded, read, host_page_size=host_page_size)
        bodies: tuple[bytes, ...] = ()
        if tab is not None and prob is None:
            bodies = tuple(read(SC_CODE_OFFSET + v.tc_offset, v.end_offset - v.tc_offset)
                           for v in tab.variants)
    finally:
        os.close(fd)
    return Sc6File(sc, tab, prob, bodies)


def sc6_snapshot_id(f: "Sc6File") -> str:
    """SnapshotId of a ``.sc`` as the v6 loader sees it.

    v5 files: :meth:`ScFile.snapshot_id` (unchanged). v6 files: ``sha256:`` of
    ``"scopt.snapshot_id.v6/1" ‖ <v5-style SnapshotId of header + valid slots> ‖
    ext table bytes ‖ every variant body`` (the header bytes already carry
    version 6 and the feature flags)."""
    import hashlib

    sc = f.sc
    if not f.has_ext or f.ext is None:
        return sc.snapshot_id()
    h = hashlib.sha256(b"scopt.snapshot_id.v7-semantic/1")
    h.update(sc.snapshot_id().encode())
    h.update(f.ext.encode())
    for b in f.variant_bodies:
        h.update(hashlib.sha256(b).digest())
    return "sha256:" + h.hexdigest()


# ---------------------------------------------------------------------------
# writer
# ---------------------------------------------------------------------------

def write_sc6(path: str | os.PathLike, header: ScHeader, units: Iterable, ext: ExtTable,
              variant_bodies: Sequence[bytes] = (), *, rewritten: bool = False,
              features: int | None = None, check: bool = True, fsync: bool = True,
              host_page_size: int | None = None) -> ScHeader:
    """Write a canonical top-level-v7 cache with the semantic payload: the
    public parts (``units`` as for :func:`~scopt.formats.sc_v5.write_sc`) and ``feature_flags``
    (``features`` overrides the computed value), the extension table at 0x100
    and ``variant_bodies[i]`` at ``0x10000 + ext.variants[i].tc_offset``.

    With ``check`` the table must fit the gap, the bodies must match the
    entries' extents, and the written file must load (the v6 rules,
    :func:`ext_problem`); otherwise :class:`ScFormatError` (the file is left on
    disk for inspection). ``check=False`` writes mutants as given.
    """
    path = os.fspath(path)
    enc = ext.encode()
    if ARRAYS_OFFSET + len(enc) - 64 > SC_CODE_OFFSET:
        raise ScExtRefused("ext_table_too_large", "the extension arrays do not fit before 0x10000")
    if check and len(variant_bodies) != len(ext.variants):
        raise ScFormatError(f"{len(variant_bodies)} variant bodies for {len(ext.variants)} "
                            "variants")
    extra = [(EXT_OFFSET, enc)]
    for v, b in zip(ext.variants, variant_bodies):
        if check and len(b) != v.end_offset - v.tc_offset:
            raise ScFormatError(f"variant {v.variant_id}: body is {len(b)} bytes, entry says "
                                f"{v.end_offset - v.tc_offset}")
        extra.append((SC_CODE_OFFSET + v.tc_offset, bytes(b)))
    feat = ext.features(rewritten=rewritten) if features is None else features
    if check and feat & ~KNOWN_FEATURES:
        raise ScFormatError(f"unknown semantic extension features 0x{feat:x}")
    hdr = header.replace(version=7, reserved=SC7_SEMANTIC_MARKER | feat)
    out = write_sc(path, hdr, units, check=check, fsync=fsync, versions=(7,),
                   extra=extra)
    if check:
        f = read_sc6(path, host_page_size=host_page_size)
        if f.problem is not None:
            raise ScExtRefused(f.problem[0], f"semantic extension refused: {f.problem[1]}")
        if f.sc.skipped:
            raise ScFormatError(f"records skipped by load_records: {f.sc.skipped[:3]}")
    return out


# ---------------------------------------------------------------------------
# V11: the independent parser
# ---------------------------------------------------------------------------

def sc6parse_compare(path: str | os.PathLike, f: Sc6File, *,
                     script: str | os.PathLike | None = None, timeout_s: float = 600.0,
                     host_page_size: int | None = None) -> tuple[bool, str, dict[str, Any]]:
    """Run ``sc6parse.py`` on ``path`` and compare every header field, the loaded
    records, every ext entry and the loader verdict/first reason with ``f``.
    Returns ``(agree, detail, sc6parse document)``."""
    script = Path(script or SC6PARSE)
    if not script.is_file():
        return False, f"independent parser {script} not found", {}
    hp = host_page_size if host_page_size is not None else f.sc.header.host_page_size
    with tempfile.TemporaryDirectory(prefix="scopt-sc6parse-") as td:
        out = Path(td) / "out.json"
        p = subprocess.run([sys.executable, "-I", str(script), os.fspath(path), str(out),
                            "--host-page", str(hp)], cwd=td, capture_output=True, text=True,
                           timeout=timeout_s)
        if p.returncode != 0 or not out.is_file():
            return False, f"sc6parse exit {p.returncode}: {p.stderr.strip()[-300:]}", {}
        doc = json.loads(out.read_text())
    probs: list[str] = []
    h = f.sc.header
    th = doc.get("header", {})
    for k, mine in (("version", h.version), ("header_size", h.header_size),
                    ("code_size", h.code_size), ("code_address", h.code_address),
                    ("prologue_end", h.prologue_end), ("record_count", h.record_count),
                    ("runtime_signature", h.runtime_signature),
                    ("host_page_size", h.host_page_size),
                    ("target_page_bits", h.target_page_bits), ("tb_size", h.tb_size),
                    ("feature_flags", h.reserved), ("checksum", h.checksum)):
        if th.get(k) != mine:
            probs.append(f"header {k}: {th.get(k)} vs {mine}")
    mine_loaded = [f.sc.records[i].tb_offset for i in f.sc.valid_slots] if f.sc.loadable else []
    if doc.get("loaded") != mine_loaded:
        probs.append(f"loaded records differ ({len(doc.get('loaded', []))} vs {len(mine_loaded)})")
    want_verdict = ("invalid_header" if not f.sc.loadable or not f.is_semantic_v7 else
                    "v7_semantic_ok" if f.problem is None else "v7_semantic_die")
    if doc.get("verdict") != want_verdict:
        probs.append(f"verdict {doc.get('verdict')} vs {want_verdict}")
    if f.problem is not None and doc.get("die") != f.problem[1]:
        probs.append(f"die reason {doc.get('die')!r} vs {f.problem[1]!r}")
    if f.ext is not None and doc.get("ext") is not None:
        e = f.ext
        te = doc["ext"]
        for k, mine in (("n_variants", e.n_variants), ("n_slots", e.n_slots),
                        ("n_links", e.n_links), ("variants_max_end", e.variants_max_end),
                        ("artifact_id8", bytes(e.artifact_id8).hex()),
                        ("checksum", e.checksum)):
            if te.get(k) != mine:
                probs.append(f"ext {k}: {te.get(k)} vs {mine}")
        for name, mine_l in (("variants", e.variants), ("slots", e.slots), ("links", e.links)):
            theirs = doc.get(name, [])
            for i, (a, b) in enumerate(zip(theirs, mine_l)):
                if a != asdict(b):
                    probs.append(f"{name}[{i}] differs")
                    break
            if f.problem is None and len(theirs) != len(mine_l):
                probs.append(f"{name}: {len(theirs)} vs {len(mine_l)} entries")
    detail = (f"agrees: verdict {want_verdict}, {len(mine_loaded)} records" +
              ("" if f.ext is None else f", {len(f.ext.variants)} variants, "
               f"{len(f.ext.slots)} slots, {len(f.ext.links)} links")) if not probs \
        else "; ".join(probs[:8])
    return not probs, detail, doc


