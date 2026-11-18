"""``.qprof`` v1 reader and writer (design §3.5; WP6).

The file is written by QEMU profile mode (``scp``, WP5) at exit::

    header (128 bytes) | phase rows (40 B each) | TB rows (72 + n_phases*32 B each)
    | edge rows (32 B each) | u64 trailer = FNV-1a-64 over every byte after the header

The byte layout is frozen in :mod:`scopt.formats.layouts` (``QPROF_*``; the TB
row fixed part is 72 bytes, see docs/contracts.md item 6). FNV-1a-64 is QEMU's
variant (:func:`scopt.canon.fnv1a64`).

Reader rules (fail closed; each failure is a :class:`QprofFormatError` with a
stable ``reason``):

* ``short_file``: fewer than 128 bytes;
* ``magic``, ``version``, ``header_size``: wrong header constants;
* ``header_checksum``: FNV-1a-64 over ``[0, 120)`` differs;
* ``provisional``: the file is exactly the provisional header QEMU writes at
  configure time (flags 0, nothing after it). The process died before the
  atexit writer ran, so there is no data at all;
* ``size``: the size differs from ``128 + body + 8`` (truncated or extended);
* ``trailer``: the body FNV differs (the data cannot be trusted, not even as a
  lower bound).

Producer semantics the reader relies on (WP5, ``docs/qprof-producer.md`` of
the WP5 branch, checked against real files):

* phase row ``reached`` is 0 (not reached), 1 (``exact_start``) or 2
  (``contained``): the 40-byte row has no other field for the match kind;
* edge ``phase`` is the phase of the **destination** entry;
* a longjmp out of a TB is recorded as kind EXIT_NULL with flag LONGJMP.

These are reader-side facts only; whether the counts are ``exact`` or
``lower_bound`` is decided by :mod:`scopt.formats.profile_v1`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..canon import fnv1a64, hexaddr, sha256_hex
from . import layouts as L
from .enums import (
    EDGE_FLAG_NAMES, EDGE_KIND_NAMES, NONE_U64, PhaseMatch, QprofEdgeFlag, QprofEdgeKind,
    QprofHeaderFlag,
)

__all__ = [
    "QprofFormatError", "QprofHeader", "QprofPhase", "QprofTbPhase", "QprofTbRow", "QprofEdge",
    "QprofFile", "parse_qprof", "read_qprof", "encode_qprof", "PHASE_NAME_BOOT",
    "REACHED_NO", "REACHED_EXACT_START", "REACHED_CONTAINED", "NONE_SRC",
]

PHASE_NAME_BOOT = "boot"
REACHED_NO = 0
REACHED_EXACT_START = 1
REACHED_CONTAINED = 2
NONE_SRC = NONE_U64  # edge src_tb_offset NONE (kind FIRST)


class QprofFormatError(ValueError):
    """The file is not a usable ``.qprof`` v1. ``reason`` is a stable code."""

    def __init__(self, reason: str, detail: str = "", path: str | None = None):
        self.reason = reason
        self.detail = detail
        self.path = path
        super().__init__(f"{path + ': ' if path else ''}qprof {reason}"
                         + (f" ({detail})" if detail else ""))


@dataclass(frozen=True, slots=True)
class QprofHeader:
    magic: bytes
    version: int
    header_size: int
    code_address: int
    runtime_signature: int
    loaded_record_count: int
    loaded_max_end: int
    loaded_header_checksum: int
    n_phases: int
    flags: int
    n_tb_rows: int
    n_edge_rows: int
    total_dispatches: int
    created_realtime_ns: int
    reserved: tuple[int, int, int]
    checksum: int

    @classmethod
    def unpack(cls, b: bytes) -> "QprofHeader":
        v = L.QPROF_HEADER.unpack_from(b, 0)
        return cls(*v[:14], (v[14], v[15], v[16]), v[17])

    def pack(self, *, fix_checksum: bool = True) -> bytes:
        raw = L.QPROF_HEADER.pack(
            self.magic, self.version, self.header_size, self.code_address,
            self.runtime_signature, self.loaded_record_count, self.loaded_max_end,
            self.loaded_header_checksum, self.n_phases, self.flags, self.n_tb_rows,
            self.n_edge_rows, self.total_dispatches, self.created_realtime_ns,
            *self.reserved, self.checksum)
        if fix_checksum:
            ck = fnv1a64(raw[: L.QPROF_HEADER_CHECKSUM_LEN])
            raw = raw[: L.QPROF_HEADER_CHECKSUM_LEN] + ck.to_bytes(8, "little")
        return raw

    @property
    def checksum_ok(self) -> bool:
        return fnv1a64(self.pack(fix_checksum=False)[: L.QPROF_HEADER_CHECKSUM_LEN]) == self.checksum

    @property
    def header_flags(self) -> QprofHeaderFlag:
        return QprofHeaderFlag(self.flags & 0x7)

    @property
    def clean_exit(self) -> bool:
        return bool(self.flags & QprofHeaderFlag.CLEAN_EXIT)

    @property
    def tb_overflow(self) -> bool:
        return bool(self.flags & QprofHeaderFlag.TB_OVERFLOW)

    @property
    def edge_overflow(self) -> bool:
        return bool(self.flags & QprofHeaderFlag.EDGE_OVERFLOW)

    @property
    def overflow(self) -> bool:
        return self.tb_overflow or self.edge_overflow

    @property
    def warm(self) -> bool:
        """True when QEMU loaded a reused cache (the ``loaded_*`` fields are meaningful)."""
        return self.loaded_record_count > 0

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "code_address": hexaddr(self.code_address),
            "runtime_signature": hexaddr(self.runtime_signature),
            "loaded_record_count": self.loaded_record_count,
            "loaded_max_end": hexaddr(self.loaded_max_end),
            "loaded_header_checksum": hexaddr(self.loaded_header_checksum),
            "n_phases": self.n_phases,
            "flags": hexaddr(self.flags),
            "clean_exit": self.clean_exit,
            "tb_overflow": self.tb_overflow,
            "edge_overflow": self.edge_overflow,
            "n_tb_rows": self.n_tb_rows,
            "n_edge_rows": self.n_edge_rows,
            "total_dispatches": self.total_dispatches,
            "created_realtime_ns": self.created_realtime_ns,
        }


@dataclass(frozen=True, slots=True)
class QprofPhase:
    id: int
    reached: int  # 0 no, 1 exact_start, 2 contained
    marker_vaddr: int
    first_dispatch_index: int
    name: str

    @property
    def is_reached(self) -> bool:
        return self.reached != REACHED_NO

    @property
    def match(self) -> PhaseMatch | None:
        if self.reached == REACHED_EXACT_START:
            return PhaseMatch.EXACT_START
        if self.reached == REACHED_CONTAINED:
            return PhaseMatch.CONTAINED
        return None

    def pack(self) -> bytes:
        nb = self.name.encode()
        if len(nb) >= L.QPROF_PHASE_NAME_LEN:
            raise ValueError(f"phase name {self.name!r} longer than 15 bytes")
        return L.QPROF_PHASE.pack(self.id, self.reached, self.marker_vaddr,
                                  self.first_dispatch_index, nb)


@dataclass(frozen=True, slots=True)
class QprofTbPhase:
    entries: int = 0
    aborted: int = 0
    longjmp: int = 0
    lookup_hits: int = 0

    @property
    def executions(self) -> int:
        """``entries - aborted`` (§3.4)."""
        return self.entries - self.aborted


@dataclass(frozen=True, slots=True)
class QprofTbRow:
    tb_offset: int
    first_vaddr: int
    page_addr0: int
    page_addr1: int  # raw; 0xffffffffffffffff = none
    cs_base: int
    flags: int
    cflags: int
    size: int  # guest size in bytes (tb->size)
    icount: int
    pad: int
    reserved: tuple[int, int]
    phases: tuple[QprofTbPhase, ...]

    @property
    def transient(self) -> bool:
        """One-shot TB (``page_addr0 == -1``, §3.6)."""
        return self.page_addr0 == NONE_U64

    @property
    def entries(self) -> int:
        return sum(p.entries for p in self.phases)

    def pack(self) -> bytes:
        return L.QPROF_TB_ROW.pack(
            self.tb_offset, self.first_vaddr, self.page_addr0, self.page_addr1, self.cs_base,
            self.flags, self.cflags, self.size, self.icount, self.pad, *self.reserved,
        ) + b"".join(L.QPROF_TB_PHASE.pack(p.entries, p.aborted, p.longjmp, p.lookup_hits)
                     for p in self.phases)


@dataclass(frozen=True, slots=True)
class QprofEdge:
    src: int  # tb_offset or NONE_SRC
    dst: int
    kind: int
    flags: int
    phase: int
    count: int

    @property
    def kind_name(self) -> str:
        try:
            return EDGE_KIND_NAMES[QprofEdgeKind(self.kind)]
        except ValueError:
            return f"unknown_{self.kind}"

    @property
    def flag_names(self) -> list[str]:
        return [EDGE_FLAG_NAMES[f] for f in QprofEdgeFlag if self.flags & f]

    def pack(self) -> bytes:
        return L.QPROF_EDGE.pack(self.src, self.dst, self.kind, self.flags, self.phase,
                                 self.count)


@dataclass(frozen=True)
class QprofFile:
    header: QprofHeader
    phases: tuple[QprofPhase, ...]
    tbs: tuple[QprofTbRow, ...]
    edges: tuple[QprofEdge, ...]
    trailer: int
    path: str | None = None
    sha256: str = ""  # plain hex sha256 of the file bytes
    size: int = 0
    warnings: tuple[str, ...] = field(default=())

    def tb_by_offset(self) -> dict[int, QprofTbRow]:
        return {t.tb_offset: t for t in self.tbs}

    def summary(self) -> dict[str, Any]:
        per_phase = [sum(t.phases[i].entries for t in self.tbs) for i in range(len(self.phases))]
        kinds: dict[str, int] = {}
        for e in self.edges:
            kinds[e.kind_name] = kinds.get(e.kind_name, 0) + e.count
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "header": self.header.to_json(),
            "phases": [{"id": p.id, "name": p.name, "marker": hexaddr(p.marker_vaddr),
                        "reached": p.is_reached,
                        "match": None if p.match is None else str(p.match),
                        "first_dispatch_index": p.first_dispatch_index} for p in self.phases],
            "entries_per_phase": per_phase,
            "transient_rows": sum(1 for t in self.tbs if t.transient),
            "edge_kind_counts": dict(sorted(kinds.items())),
            "warnings": list(self.warnings),
        }


def parse_qprof(data: bytes, *, path: str | None = None) -> QprofFile:
    """Parse ``.qprof`` bytes; raises :class:`QprofFormatError` (see module docstring)."""
    data = bytes(data)
    if len(data) < L.QPROF_HEADER.size:
        raise QprofFormatError("short_file", f"{len(data)} bytes", path)
    h = QprofHeader.unpack(data)
    if h.magic != L.QPROF_MAGIC:
        raise QprofFormatError("magic", repr(h.magic), path)
    if h.version != L.QPROF_VERSION:
        raise QprofFormatError("version", str(h.version), path)
    if h.header_size != L.QPROF_HEADER.size:
        raise QprofFormatError("header_size", str(h.header_size), path)
    if not h.checksum_ok:
        raise QprofFormatError("header_checksum", f"stored=0x{h.checksum:x}", path)
    row = L.qprof_tb_row_size(h.n_phases)
    body = L.QPROF_PHASE.size * h.n_phases + row * h.n_tb_rows + L.QPROF_EDGE.size * h.n_edge_rows
    expect = L.QPROF_HEADER.size + body + L.QPROF_TRAILER.size
    if len(data) == L.QPROF_HEADER.size and h.flags == 0 and h.n_tb_rows == 0 \
            and h.n_edge_rows == 0:
        raise QprofFormatError("provisional",
                               "only the configure-time header: QEMU died before the atexit "
                               "writer ran", path)
    if len(data) != expect:
        raise QprofFormatError("size", f"{len(data)} bytes, expected {expect}", path)
    off = L.QPROF_HEADER.size
    end_body = expect - L.QPROF_TRAILER.size
    (trailer,) = L.QPROF_TRAILER.unpack_from(data, end_body)
    if fnv1a64(data[off:end_body]) != trailer:
        raise QprofFormatError("trailer", "FNV-1a-64 over the body differs", path)

    warnings: list[str] = []
    phases = []
    for _ in range(h.n_phases):
        pid, reached, marker, fdi, name = L.QPROF_PHASE.unpack_from(data, off)
        off += L.QPROF_PHASE.size
        nm = name.split(b"\0", 1)[0].decode("utf-8", "replace")
        phases.append(QprofPhase(pid, reached, marker, fdi, nm))
    tbs = []
    for _ in range(h.n_tb_rows):
        v = L.QPROF_TB_ROW.unpack_from(data, off)
        off += L.QPROF_TB_ROW.size
        per = []
        for _p in range(h.n_phases):
            per.append(QprofTbPhase(*L.QPROF_TB_PHASE.unpack_from(data, off)))
            off += L.QPROF_TB_PHASE.size
        tbs.append(QprofTbRow(*v[:10], (v[10], v[11]), tuple(per)))
    edges = []
    for _ in range(h.n_edge_rows):
        edges.append(QprofEdge(*L.QPROF_EDGE.unpack_from(data, off)))
        off += L.QPROF_EDGE.size
    assert off == end_body

    if any(h.reserved) or h.flags & ~0x7:
        warnings.append("header reserved bits or fields non-zero")
    if any(t.pad or any(t.reserved) for t in tbs):
        warnings.append("TB row pad/reserved non-zero")
    return QprofFile(h, tuple(phases), tuple(tbs), tuple(edges), trailer, path,
                     sha256_hex(data), len(data), tuple(warnings))


def read_qprof(path: str | os.PathLike) -> QprofFile:
    path = os.fspath(path)
    with open(path, "rb") as f:
        data = f.read()
    return parse_qprof(data, path=path)


def encode_qprof(header: QprofHeader | Mapping[str, Any], phases: Sequence[QprofPhase],
                 tbs: Sequence[QprofTbRow], edges: Sequence[QprofEdge], *,
                 fix_counts: bool = True, fix_checksum: bool = True,
                 trailer: int | None = None) -> bytes:
    """Encode a ``.qprof``. With ``fix_counts`` the header's ``n_*`` fields are set
    from the rows; with ``fix_checksum`` the header checksum is recomputed;
    ``trailer`` forces a (bad) body trailer."""
    if isinstance(header, Mapping):
        hd = dict(header)
        hd.setdefault("reserved", (0, 0, 0))
        hd.setdefault("checksum", 0)
        header = QprofHeader(**hd)
    if fix_counts:
        header = QprofHeader(**{**{k: getattr(header, k) for k in header.__dataclass_fields__},
                                "n_phases": len(phases), "n_tb_rows": len(tbs),
                                "n_edge_rows": len(edges)})
    body = b"".join(p.pack() for p in phases) + b"".join(t.pack() for t in tbs) \
        + b"".join(e.pack() for e in edges)
    tr = fnv1a64(body) if trailer is None else trailer
    return header.pack(fix_checksum=fix_checksum) + body + L.QPROF_TRAILER.pack(tr)
