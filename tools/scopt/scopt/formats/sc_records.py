"""SuperCache public-record reader and sparse writer (design §0 G1, §2.7, §4.6, §5.8 V1/V9). WP1.

File layout (qemu tcg/supercache.c):

* ``[0, 80)``: :class:`ScHeader` (``SupercacheHeader``).
* ``[0x10000, 0x10000 + code_size)``: the arena image. QEMU writes only each
  TB's ``[tc_offset, end_offset)`` (host code plus search data) there, at file
  offset ``0x10000 + tc_offset``. The prologue and the TB structs are never
  written; they read back as zeros (holes).
* ``[0x10000 + code_size, ...)``: the record array, 104 bytes per
  :class:`ScRecord` (``SupercacheRecord``), appended in commit order.

The reader mirrors what QEMU does with a file it opens:

* ``valid_header`` (supercache.c:364-375): magic, version, ``header_size``,
  checksum. The runtime half (``code_size``, ``runtime_signature``, host page
  size, target page bits, ``tb_size``) can only be checked against a known
  runtime, so it is checked only for the fields passed as ``expect``.
* ``load_records`` (supercache.c:378-423): ``count = min(record_count,
  (file_size - record_offset(0)) / 104)``, then per record, in this order:
  committed, checksum, bounds. The first failing check is the skip reason.

Reason codes (stable; tests and the rejection suite assert them):

* header: ``truncated_header``, ``magic``, ``version``, ``header_size``,
  ``header_checksum``, ``runtime_mismatch:<field>``;
* file: ``truncated_records`` (``record_count`` is larger than the record
  array the file holds);
* per record (:class:`SkippedRecord`): ``not_committed``, ``record_checksum``,
  ``bounds``.

A header problem means QEMU would not reuse the file at all: ``loadable`` is
false and :meth:`ScFile.units` is empty. The records are still parsed (for canonical
files) so that ``scopt sc dump`` can show them.
"""

from __future__ import annotations

import errno
import hashlib
import os
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from ..canon import sc_header_checksum, sc_record_checksum
from ..ids import TBKey, snapshot_id, tb_uid, unit_ids
from .layouts import SC_CODE_OFFSET, SC_HEADER, SC_MAGIC, SC_RECORD, SC_RECORD_COMMIT, SC_VERSION

__all__ = [
    "ScHeader", "ScRecord", "ScFile", "ScUnit", "SkippedRecord", "Finding",
    "ScFormatError", "read_sc", "write_sc", "sparse_copy", "sha256_sparse",
    "structural_findings", "identity_copy", "IdentityResult", "data_extents",
    "SC_CODE_OFFSET", "SC_MAGIC", "SC_VERSION", "SC_RECORD_COMMIT", "trusted_record_input",
    "HEADER_SIZE", "RECORD_SIZE", "TB_ALIGN",
]

HEADER_SIZE = SC_HEADER.size  # 80
RECORD_SIZE = SC_RECORD.size  # 104
TB_ALIGN = 64  # icache line size on the x86-64 host (tb_offset % 64 == 0, §5.8 V1)

RUNTIME_FIELDS = ("code_size", "runtime_signature", "host_page_size", "target_page_bits",
                  "tb_size")


_SKIP_RECORD_CHECKSUM = ContextVar("scopt_skip_record_checksum", default=False)


@contextmanager
def trusted_record_input():
    """Accept stored record checksums within this compiler/load operation."""
    token = _SKIP_RECORD_CHECKSUM.set(True)
    try:
        yield
    finally:
        _SKIP_RECORD_CHECKSUM.reset(token)


class ScFormatError(ValueError):
    """Raised by the writer on inconsistent input, and by strict reads."""


def _round_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a


# --------------------------------------------------------------------------
# Header and record structs
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScHeader:
    magic: bytes
    version: int
    header_size: int
    code_size: int
    code_address: int
    prologue_end: int
    record_count: int
    runtime_signature: int
    host_page_size: int
    target_page_bits: int
    tb_size: int
    reserved: int
    checksum: int

    SIZE = SC_HEADER.size

    @classmethod
    def from_bytes(cls, b: bytes) -> "ScHeader":
        return cls(*SC_HEADER.unpack(b[: SC_HEADER.size]))

    def to_bytes(self, *, fix_checksum: bool = False) -> bytes:
        raw = SC_HEADER.pack(
            self.magic, self.version, self.header_size, self.code_size,
            self.code_address, self.prologue_end, self.record_count,
            self.runtime_signature, self.host_page_size, self.target_page_bits,
            self.tb_size, self.reserved, self.checksum)
        if fix_checksum:
            raw = raw[:72] + sc_header_checksum(raw).to_bytes(8, "little")
        return raw

    def replace(self, **changes: Any) -> "ScHeader":
        vals = {k: getattr(self, k) for k in self.__dataclass_fields__}
        vals.update(changes)
        return ScHeader(**vals)

    def with_checksum(self) -> "ScHeader":
        """This header with ``checksum`` recomputed."""
        return ScHeader.from_bytes(self.to_bytes(fix_checksum=True))

    @property
    def checksum_ok(self) -> bool:
        return sc_header_checksum(self.to_bytes()) == self.checksum

    @property
    def records_offset(self) -> int:
        return SC_CODE_OFFSET + self.code_size

    def file_size_for(self, n_records: int) -> int:
        """Exact size of a canonical file with ``n_records`` records."""
        return SC_CODE_OFFSET + self.code_size + n_records * RECORD_SIZE

    def problems(self, expect: Mapping[str, int] | None = None) -> list[str]:
        """Check the one supported SuperCache header and optional runtime fields."""
        out = []
        if self.magic != SC_MAGIC:
            out.append("magic")
        if self.version != SC_VERSION:
            out.append("version")
        if self.header_size != HEADER_SIZE:
            out.append("header_size")
        if not self.checksum_ok:
            out.append("header_checksum")
        for k, v in (expect or {}).items():
            if k not in RUNTIME_FIELDS:
                raise ValueError(f"cannot check header field {k!r} (allowed: {RUNTIME_FIELDS})")
            if getattr(self, k) != v:
                out.append(f"runtime_mismatch:{k}")
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "magic": self.magic.decode("latin-1"),
            "version": self.version,
            "header_size": self.header_size,
            "code_size": f"0x{self.code_size:x}",
            "code_address": f"0x{self.code_address:x}",
            "prologue_end": f"0x{self.prologue_end:x}",
            "record_count": self.record_count,
            "runtime_signature": f"0x{self.runtime_signature:x}",
            "host_page_size": self.host_page_size,
            "target_page_bits": self.target_page_bits,
            "tb_size": self.tb_size,
            "reserved": self.reserved,
            "checksum": f"0x{self.checksum:x}",
            "checksum_ok": self.checksum_ok,
        }


@dataclass(frozen=True, slots=True)
class ScRecord:
    tb_offset: int
    end_offset: int
    tc_offset: int
    tc_size: int
    pc: int
    cs_base: int
    page_addr0: int
    page_addr1: int  # raw; 0xffffffffffffffff = none
    guest_hash: int
    flags: int
    cflags: int
    guest_size: int
    icount: int
    jmp_reset_offset: tuple[int, int]
    jmp_insn_offset: tuple[int, int]
    checksum: int
    committed: int

    SIZE = SC_RECORD.size

    @classmethod
    def from_bytes(cls, b: bytes) -> "ScRecord":
        v = SC_RECORD.unpack(b[: SC_RECORD.size])
        return cls(*v[:13], (v[13], v[14]), (v[15], v[16]), v[17], v[18])

    def to_bytes(self, *, fix_checksum: bool = False) -> bytes:
        raw = SC_RECORD.pack(
            self.tb_offset, self.end_offset, self.tc_offset, self.tc_size, self.pc,
            self.cs_base, self.page_addr0, self.page_addr1, self.guest_hash,
            self.flags, self.cflags, self.guest_size, self.icount,
            *self.jmp_reset_offset, *self.jmp_insn_offset, self.checksum, self.committed)
        if fix_checksum:
            raw = raw[:92] + sc_record_checksum(raw).to_bytes(4, "little") + raw[96:]
        return raw

    def replace(self, **changes: Any) -> "ScRecord":
        vals = {k: getattr(self, k) for k in self.__dataclass_fields__}
        vals.update(changes)
        return ScRecord(**vals)

    def with_checksum(self) -> "ScRecord":
        return ScRecord.from_bytes(self.to_bytes(fix_checksum=True))

    @property
    def key(self) -> TBKey:
        return TBKey.from_raw(self.page_addr0, self.page_addr1, self.flags,
                              self.cs_base, self.cflags)

    @property
    def tb_uid(self) -> str:
        return tb_uid(self.key, self.guest_size, self.icount, self.guest_hash)

    @property
    def checksum_ok(self) -> bool:
        return sc_record_checksum(self.to_bytes()) == self.checksum

    @property
    def search_offset(self) -> int:
        """Arena offset of the search data (``tc_offset + tc_size``)."""
        return self.tc_offset + self.tc_size

    def problem(self, code_size: int, tb_size: int) -> tuple[str, str] | None:
        """``(reason, detail)`` for the first ``load_records`` check that fails, else None."""
        if self.committed != SC_RECORD_COMMIT:
            return "not_committed", f"committed=0x{self.committed:08x}"
        if not _SKIP_RECORD_CHECKSUM.get() and not self.checksum_ok:
            return "record_checksum", (f"stored=0x{self.checksum:08x} "
                                       f"computed=0x{sc_record_checksum(self.to_bytes()):08x}")
        # supercache.c:409-414, same order; unsigned arithmetic as in C
        if code_size < tb_size or self.tb_offset > code_size - tb_size:
            return "bounds", "tb_offset > size - sizeof(TranslationBlock)"
        if self.tb_offset >= self.tc_offset:
            return "bounds", "tb_offset >= tc_offset"
        if self.tc_offset >= self.end_offset:
            return "bounds", "tc_offset >= end_offset"
        if self.tc_size > self.end_offset - self.tc_offset:
            return "bounds", "tc_size > end_offset - tc_offset"
        if self.end_offset > code_size:
            return "bounds", "end_offset > size"
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "tb_offset": f"0x{self.tb_offset:x}",
            "end_offset": f"0x{self.end_offset:x}",
            "tc_offset": f"0x{self.tc_offset:x}",
            "tc_size": self.tc_size,
            "pc": f"0x{self.pc:x}",
            "cs_base": f"0x{self.cs_base:x}",
            "page_addr0": f"0x{self.page_addr0:x}",
            "page_addr1": f"0x{self.page_addr1:x}",
            "guest_hash": f"0x{self.guest_hash:x}",
            "flags": f"0x{self.flags:x}",
            "cflags": f"0x{self.cflags:x}",
            "guest_size": self.guest_size,
            "icount": self.icount,
            "jmp_reset_offset": list(self.jmp_reset_offset),
            "jmp_insn_offset": list(self.jmp_insn_offset),
            "checksum": f"0x{self.checksum:x}",
            "committed": f"0x{self.committed:x}",
        }


@dataclass(frozen=True, slots=True)
class SkippedRecord:
    slot: int
    reason: str  # "not_committed" | "record_checksum" | "bounds"
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Finding:
    """A file-level observation: a load problem or a non-canonical property."""

    code: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ScUnit:
    """One valid slot: the record and the arena bytes ``[tc_offset, end_offset)``."""

    slot: int
    record: ScRecord
    record_bytes: bytes
    body: bytes  # [tc_offset, end_offset): host code then search data (and alignment pad)

    @property
    def code(self) -> bytes:
        """Host code ``[tc_offset, tc_offset + tc_size)``."""
        return self.body[: self.record.tc_size]

    @property
    def search(self) -> bytes:
        """``[tc_offset + tc_size, end_offset)``: search data and its alignment pad."""
        return self.body[self.record.tc_size:]

    @property
    def tb_uid(self) -> str:
        return self.record.tb_uid


@dataclass(frozen=True, slots=True)
class ScFile:
    path: str
    header: ScHeader
    header_bytes: bytes
    records: tuple[ScRecord | None, ...]  # by slot, for the count QEMU reads; None = skipped
    record_bytes: tuple[bytes, ...]  # raw 104 bytes per slot (same length as records)
    skipped: tuple[SkippedRecord, ...] = ()
    file_size: int = 0
    meta: dict = field(default_factory=dict)
    header_problems: tuple[str, ...] = ()
    problems: tuple[Finding, ...] = ()  # file-level load problems (e.g. truncated_records)
    bodies: tuple[bytes | None, ...] = ()  # [tc,end) per slot for valid records
    available_records: int = 0  # whole records the file holds after record_offset(0)

    # -- loader view ----------------------------------------------------
    @property
    def loadable(self) -> bool:
        """True when QEMU would reuse this file (header valid)."""
        return not self.header_problems

    @property
    def count(self) -> int:
        """``min(record_count, available)``: the slots ``load_records`` visits."""
        return len(self.records)

    @property
    def valid_slots(self) -> tuple[int, ...]:
        if not self.loadable:
            return ()
        return tuple(i for i, r in enumerate(self.records) if r is not None)

    def units(self) -> list[ScUnit]:
        """Valid slots in slot order, each with its arena bytes (empty if not loadable)."""
        return [ScUnit(i, self.records[i], self.record_bytes[i], self.bodies[i])
                for i in self.valid_slots]

    def unit_ids(self) -> list[str]:
        return unit_ids(self.records[i].tb_uid for i in self.valid_slots)

    @property
    def max_end(self) -> int:
        return max((self.records[i].end_offset for i in self.valid_slots), default=0)

    def snapshot_id(self) -> str:
        """SnapshotId (§4.1) over the header and every valid slot."""
        return snapshot_id(self.header_bytes,
                           ((self.record_bytes[i], self.bodies[i]) for i in self.valid_slots))

    @property
    def expected_file_size(self) -> int:
        return self.header.file_size_for(self.header.record_count)

    def all_findings(self) -> list[Finding]:
        """Header problems, file problems and skipped records as findings."""
        out = [Finding(p) for p in self.header_problems]
        out += list(self.problems)
        out += [Finding(s.reason, f"slot {s.slot}: {s.detail}") for s in self.skipped]
        return out

    def to_json(self, *, records: bool = True) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "path": self.path,
            "file_size": self.file_size,
            "header": self.header.to_json(),
            "header_problems": list(self.header_problems),
            "problems": [{"code": p.code, "detail": p.detail} for p in self.problems],
            "loadable": self.loadable,
            "count": self.count,
            "available_records": self.available_records,
            "valid": len(self.valid_slots),
            "skipped": [{"slot": s.slot, "reason": s.reason, "detail": s.detail}
                        for s in self.skipped],
            "max_end": f"0x{self.max_end:x}",
        }
        if self.loadable:
            doc["snapshot_id"] = self.snapshot_id()
        if records:
            uids = dict(zip(self.valid_slots, self.unit_ids()))
            rows = []
            for i, b in enumerate(self.record_bytes):
                r = ScRecord.from_bytes(b)
                row = {"slot": i, "valid": i in uids, **r.to_json(), "tb_uid": r.tb_uid}
                if i in uids:
                    row["unit_id"] = uids[i]
                rows.append(row)
            doc["records"] = rows
        return doc


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------

def _pread_exact(fd: int, n: int, off: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        b = os.pread(fd, n - len(out), off + len(out))
        if not b:
            break
        out += b
    return bytes(out)


def read_sc(path: str | os.PathLike, *, expect: Mapping[str, int] | None = None,
            strict: bool = False) -> ScFile:
    """Parse the public records of a SuperCache the way QEMU's ``valid_header`` + ``load_records`` do.

    Only the header, the record array and each valid record's
    ``[tc_offset, end_offset)`` are read, so a 2 GiB sparse file is cheap.
    ``expect`` optionally checks runtime header fields (``code_size``,
    ``runtime_signature``, ``host_page_size``, ``target_page_bits``,
    ``tb_size``). With ``strict``, any header problem, file problem or skipped
    record raises :class:`ScFormatError`.
    """
    path = os.fspath(path)
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        size = os.fstat(fd).st_size
        raw_hdr = _pread_exact(fd, HEADER_SIZE, 0)
        if len(raw_hdr) < HEADER_SIZE:
            hdr = ScHeader.from_bytes(raw_hdr.ljust(HEADER_SIZE, b"\0"))
            sc = ScFile(path, hdr, raw_hdr, (), (), (), size, {}, ("truncated_header",))
            if strict:
                raise ScFormatError(f"{path}: truncated_header ({size} bytes)")
            return sc
        hdr = ScHeader.from_bytes(raw_hdr)
        hprobs = tuple(hdr.problems(expect))

        problems: list[Finding] = []
        records: list[ScRecord | None] = []
        rbytes: list[bytes] = []
        bodies: list[bytes | None] = []
        skipped: list[SkippedRecord] = []
        available = 0
        # A wrong magic or version means the record layout is unknown: stop there.
        if "magic" not in hprobs and "version" not in hprobs:
            roff = hdr.records_offset
            available = (size - roff) // RECORD_SIZE if size >= roff else 0
            count = min(hdr.record_count, available)
            if hdr.record_count > available:
                problems.append(Finding(
                    "truncated_records",
                    f"record_count={hdr.record_count} but the file holds {available} "
                    f"(file_size={size}, record_offset={roff})"))
            blob = _pread_exact(fd, count * RECORD_SIZE, roff) if count else b""
            if len(blob) != count * RECORD_SIZE:  # file shrank under us
                raise ScFormatError(f"{path}: short read of the record array")
            for i in range(count):
                b = blob[i * RECORD_SIZE:(i + 1) * RECORD_SIZE]
                rec = ScRecord.from_bytes(b)
                rbytes.append(b)
                prob = rec.problem(hdr.code_size, hdr.tb_size)
                if prob is not None:
                    skipped.append(SkippedRecord(i, *prob))
                    records.append(None)
                    bodies.append(None)
                    continue
                n = rec.end_offset - rec.tc_offset
                body = _pread_exact(fd, n, SC_CODE_OFFSET + rec.tc_offset)
                # Bounds passed, so the bytes lie inside [0x10000, records_offset).
                if len(body) != n:  # pragma: no cover - only if file shrank
                    raise ScFormatError(f"{path}: slot {i}: short read of code bytes")
                records.append(rec)
                bodies.append(body)
        sc = ScFile(path, hdr, raw_hdr, tuple(records), tuple(rbytes), tuple(skipped), size,
                    {}, hprobs, tuple(problems), tuple(bodies), available)
    finally:
        os.close(fd)
    if strict and (sc.header_problems or sc.problems or sc.skipped):
        raise ScFormatError(f"{path}: " + ", ".join(
            f"{f.code}{' (' + f.detail + ')' if f.detail else ''}" for f in sc.all_findings()))
    return sc


# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------

def _unit_parts(u: Any) -> tuple[bytes, bytes]:
    """``(record bytes, body bytes)`` from a unit-like object."""
    if isinstance(u, tuple) and len(u) == 2:
        rec, body = u
    else:
        rec = u.record
        if hasattr(u, "body"):
            body = u.body
        elif hasattr(u, "code") and hasattr(u, "search"):
            body = bytes(u.code) + bytes(u.search)
        else:
            raise TypeError(f"unit {u!r} has neither .body nor .code/.search")
    if isinstance(rec, ScRecord):
        rec = rec.to_bytes()
    rec = bytes(rec)
    if len(rec) != RECORD_SIZE:
        raise ScFormatError(f"record is {len(rec)} bytes, expected {RECORD_SIZE}")
    return rec, bytes(body)


def _pwrite_all(fd: int, data: bytes, off: int) -> None:
    mv = memoryview(data)
    while mv:
        n = os.pwrite(fd, mv, off)
        if n <= 0:  # pragma: no cover
            raise OSError(errno.EIO, "short write")
        mv = mv[n:]
        off += n


def write_sc(path: str | os.PathLike, header: ScHeader | bytes, units: Iterable, *,
             sparse: bool = True, check: bool = True, fsync: bool = True,
             extra: Iterable[tuple[int, bytes]] = ()) -> ScHeader:
    """Write a canonical SuperCache (§4.6): truncate to size, then pwrite header, bodies, records.

    ``units`` are, in slot order, :class:`ScUnit` objects, ``TBUnit``-like
    objects (``record`` plus ``code`` and ``search``), or ``(record, body)``
    pairs, where ``body`` is the arena bytes ``[tc_offset, end_offset)``.

    The header's ``record_count`` is set to the number of units and its
    checksum recomputed; every other header field, including ``reserved``, is
    kept. With ``check`` (the default), each record must pass the
    ``load_records`` checks, its body length must equal ``end - tc``, and the
    ``[tb_offset, end_offset)`` intervals must be pairwise disjoint and above
    the prologue. ``check=False`` writes the given bytes as they are (for
    corruption tests). Returns the header that was written.

    With ``sparse`` the file is created by ``ftruncate`` and only non-zero
    regions are written, so holes stay holes; otherwise every byte is written.

    Extension writers pass their tables and private code as additional
    (file offset, bytes) pairs in ``extra``.
    """
    path = os.fspath(path)
    if isinstance(header, (bytes, bytearray, memoryview)):
        header = ScHeader.from_bytes(bytes(header))
    if header.version != SC_VERSION:
        raise ScFormatError(f"unsupported SuperCache header version {header.version}")
    if header.reserved and not extra and check:
        raise ScFormatError("cache with extension flags requires its extension payload")
    parts = [_unit_parts(u) for u in units]
    hdr = header.replace(record_count=len(parts)).with_checksum()
    hraw = hdr.to_bytes()
    if check:
        probs = hdr.problems()
        if probs:
            raise ScFormatError(f"header invalid: {probs}")
        floor = _round_up(hdr.prologue_end, TB_ALIGN)
        spans = []
        for i, (rb, body) in enumerate(parts):
            rec = ScRecord.from_bytes(rb)
            prob = rec.problem(hdr.code_size, hdr.tb_size)
            if prob:
                raise ScFormatError(f"unit {i}: record fails load_records: {prob[0]} ({prob[1]})")
            if len(body) != rec.end_offset - rec.tc_offset:
                raise ScFormatError(f"unit {i}: body is {len(body)} bytes, record says "
                                    f"{rec.end_offset - rec.tc_offset}")
            if rec.tb_offset < floor:
                raise ScFormatError(f"unit {i}: tb_offset 0x{rec.tb_offset:x} is below the "
                                    f"prologue end 0x{floor:x}")
            spans.append((rec.tb_offset, rec.end_offset, i))
        spans.sort()
        for (a0, a1, i), (b0, b1, j) in zip(spans, spans[1:]):
            if b0 < a1:
                raise ScFormatError(f"units {i} and {j} overlap: [0x{a0:x},0x{a1:x}) and "
                                    f"[0x{b0:x},0x{b1:x})")
    roff = hdr.records_offset
    total = roff + len(parts) * RECORD_SIZE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    try:
        if sparse:
            os.ftruncate(fd, total)
        else:
            zero = bytes(1 << 20)
            off = 0
            while off < total:
                n = min(len(zero), total - off)
                _pwrite_all(fd, zero[:n], off)
                off += n
        _pwrite_all(fd, hraw, 0)
        for rb, body in parts:
            tc = ScRecord.from_bytes(rb).tc_offset
            if body:
                if not check and SC_CODE_OFFSET + tc + len(body) > roff:
                    raise ScFormatError("body runs into the record array")
                _pwrite_all(fd, body, SC_CODE_OFFSET + tc)
        for off, data in extra:
            if off < HEADER_SIZE or off + len(data) > roff:
                raise ScFormatError(f"extra bytes at 0x{off:x}+{len(data)} outside "
                                    f"[{HEADER_SIZE}, 0x{roff:x})")
            if data:
                _pwrite_all(fd, bytes(data), off)
        if parts:
            _pwrite_all(fd, b"".join(rb for rb, _ in parts), roff)
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)
    return hdr


# --------------------------------------------------------------------------
# Sparse helpers
# --------------------------------------------------------------------------

def data_extents(fd: int, size: int | None = None) -> list[tuple[int, int]]:
    """``[(start, end), ...]`` data extents of an open file via SEEK_DATA/SEEK_HOLE.

    Falls back to one extent covering the file when the filesystem does not
    support the seek flags.
    """
    if size is None:
        size = os.fstat(fd).st_size
    out = []
    off = 0
    try:
        while off < size:
            try:
                d = os.lseek(fd, off, os.SEEK_DATA)
            except OSError as e:
                if e.errno == errno.ENXIO:  # no more data
                    break
                raise
            h = os.lseek(fd, d, os.SEEK_HOLE)
            out.append((d, min(h, size)))
            off = h
    except OSError as e:
        if e.errno in (errno.EINVAL, errno.EOPNOTSUPP):
            return [(0, size)] if size else []
        raise
    return out


_ZERO = bytes(1 << 20)
_CHUNK = 1 << 20


def sparse_copy(src: str | os.PathLike, dst: str | os.PathLike, *, fsync: bool = True) -> None:
    """Copy ``src`` to ``dst`` keeping holes as holes (SEEK_DATA/SEEK_HOLE).

    All-zero blocks inside data extents are skipped too. ``dst`` is created
    with mode 0600 (writable, as QEMU opens the cache ``O_RDWR``), replacing
    any existing file.
    """
    sfd = os.open(os.fspath(src), os.O_RDONLY | os.O_CLOEXEC)
    try:
        size = os.fstat(sfd).st_size
        dfd = os.open(os.fspath(dst), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
        try:
            os.ftruncate(dfd, size)
            for a, b in data_extents(sfd, size):
                off = a
                while off < b:
                    n = min(_CHUNK, b - off)
                    buf = os.pread(sfd, n, off)
                    if not buf:  # pragma: no cover
                        raise OSError(errno.EIO, f"short read at {off}")
                    if buf != _ZERO[:len(buf)]:
                        _pwrite_all(dfd, buf, off)
                    off += len(buf)
            if fsync:
                os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)


def sha256_sparse(path: str | os.PathLike) -> str:
    """Hex sha256 of the file's full logical bytes; holes are hashed as zeros
    without being read. Equal to ``sha256sum`` (no ``sha256:`` prefix)."""
    h = hashlib.sha256()
    fd = os.open(os.fspath(path), os.O_RDONLY | os.O_CLOEXEC)
    try:
        size = os.fstat(fd).st_size
        pos = 0

        def zeros(n: int) -> None:
            while n > 0:
                k = min(n, len(_ZERO))
                h.update(_ZERO[:k])
                n -= k

        for a, b in data_extents(fd, size):
            zeros(a - pos)
            off = a
            while off < b:
                buf = os.pread(fd, min(_CHUNK, b - off), off)
                if not buf:  # pragma: no cover
                    raise OSError(errno.EIO, f"short read at {off}")
                h.update(buf)
                off += len(buf)
            pos = b
        zeros(size - pos)
    finally:
        os.close(fd)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Structure checks (the static part of §5.8 V1, and what "canonical" means for V9)
# --------------------------------------------------------------------------

def structural_findings(sc: ScFile, *, scan_zeros: bool = True,
                        extra_covered: Iterable[tuple[int, int]] = ()) -> list[Finding]:
    """Everything that makes ``sc`` differ from what :func:`write_sc` would write.

    An empty list means the file is *canonical*: the header is valid, every
    record is valid, ``record_count`` equals the records present, the file size
    is exactly ``0x10000 + code_size + n*104``, records have zero padding, all
    TBs satisfy ``tb % 64 == 0`` and ``tc - tb == ROUND_UP(tb_size, 64)``, the
    ``[tb, end)`` intervals are disjoint and above the prologue, and (with
    ``scan_zeros``) every byte outside the header, the bodies and the record
    array is zero. For such a file ``write(read(x))`` is byte-identical (V9).
    ``extra_covered`` are further ``[start, end)`` file ranges that may hold
    data (the v6 extension table and variant bodies, :mod:`sc_semantic`).
    """
    out = [Finding(p) for p in sc.header_problems]
    if not sc.loadable:
        return out
    hdr = sc.header
    out += list(sc.problems)
    out += [Finding(s.reason, f"slot {s.slot}: {s.detail}") for s in sc.skipped]
    if not sc.problems and sc.file_size != sc.expected_file_size:
        # trailing whole records beyond record_count, or a partial record
        out.append(Finding("file_size", f"{sc.file_size} != expected {sc.expected_file_size}"))
    tc_gap = _round_up(hdr.tb_size, TB_ALIGN)
    floor = _round_up(hdr.prologue_end, TB_ALIGN)
    spans = []
    for i in sc.valid_slots:
        r = sc.records[i]
        if sc.record_bytes[i][100:104] != b"\0\0\0\0":
            out.append(Finding("record_padding", f"slot {i}"))
        if r.tb_offset % TB_ALIGN:
            out.append(Finding("tb_alignment", f"slot {i}: tb_offset 0x{r.tb_offset:x}"))
        if r.tc_offset - r.tb_offset != tc_gap:
            out.append(Finding("tc_gap", f"slot {i}: tc-tb = {r.tc_offset - r.tb_offset} "
                                         f"!= {tc_gap}"))
        if r.tb_offset < floor:
            out.append(Finding("below_prologue", f"slot {i}: tb_offset 0x{r.tb_offset:x}"))
        spans.append((r.tb_offset, r.end_offset, i))
    spans.sort()
    for (a0, a1, i), (b0, b1, j) in zip(spans, spans[1:]):
        if b0 < a1:
            out.append(Finding("overlap", f"slots {i} and {j}"))
    if scan_zeros:
        out += _nonzero_outside(sc, extra_covered)
    return out


def _nonzero_outside(sc: ScFile, extra_covered: Iterable[tuple[int, int]] = ()
                     ) -> list[Finding]:
    hdr = sc.header
    covered = [(0, HEADER_SIZE)] + [tuple(x) for x in extra_covered]
    roff = hdr.records_offset
    covered.append((roff, roff + sc.count * RECORD_SIZE))
    for i in sc.valid_slots:
        r = sc.records[i]
        covered.append((SC_CODE_OFFSET + r.tc_offset, SC_CODE_OFFSET + r.end_offset))
    covered.sort()
    out: list[Finding] = []
    fd = os.open(sc.path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        size = os.fstat(fd).st_size
        for a, b in data_extents(fd, size):
            # walk [a, b) minus the covered intervals
            pos = a
            for c0, c1 in covered:
                if c1 <= pos:
                    continue
                if c0 >= b:
                    break
                if c0 > pos:
                    _scan_zero(fd, pos, min(c0, b), out)
                pos = max(pos, c1)
                if pos >= b:
                    break
            if pos < b:
                _scan_zero(fd, pos, b, out)
            if len(out) > 16:
                break
    finally:
        os.close(fd)
    return out


def _scan_zero(fd: int, a: int, b: int, out: list[Finding]) -> None:
    off = a
    while off < b:
        buf = os.pread(fd, min(_CHUNK, b - off), off)
        if not buf:
            return
        if buf != _ZERO[:len(buf)]:
            k = next(i for i, x in enumerate(buf) if x)
            out.append(Finding("nonzero_outside_records", f"file offset 0x{off + k:x}"))
            return
        off += len(buf)


# --------------------------------------------------------------------------
# Identity writer (§5.8 V9, static half)
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class IdentityResult:
    """Outcome of :func:`identity_copy`.

    ``status`` is ``"identity"`` (input canonical, output sha256 equal),
    ``"identity_canonicalized"`` (input not canonical; the output is what QEMU
    would load, with ``dropped`` slots removed) or ``"fail"`` (input canonical
    but the hashes differ: a writer bug).
    """

    status: str
    in_sha256: str
    out_sha256: str
    in_snapshot_id: str
    out_snapshot_id: str
    n_units: int
    dropped: tuple[SkippedRecord, ...]
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return self.status != "fail"

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "in_sha256": "sha256:" + self.in_sha256,
            "out_sha256": "sha256:" + self.out_sha256,
            "sha256_equal": self.in_sha256 == self.out_sha256,
            "in_snapshot_id": self.in_snapshot_id,
            "out_snapshot_id": self.out_snapshot_id,
            "snapshot_id_equal": self.in_snapshot_id == self.out_snapshot_id,
            "n_units": self.n_units,
            "dropped": [{"slot": d.slot, "reason": d.reason, "detail": d.detail}
                        for d in self.dropped],
            "findings": [{"code": f.code, "detail": f.detail} for f in self.findings],
        }


def identity_copy(src: str | os.PathLike, dst: str | os.PathLike) -> IdentityResult:
    """``write(read(src))`` to ``dst`` and compare (V9).

    Raises :class:`ScFormatError` if QEMU would not load ``src`` at all
    (header problem): there is nothing to copy.
    """
    sc = read_sc(src)
    if not sc.loadable:
        raise ScFormatError(f"{os.fspath(src)}: not loadable: {list(sc.header_problems)}")
    if os.path.exists(dst) and os.path.samefile(src, dst):
        raise ScFormatError("identity-copy requires a separate destination")
    if sc.header.reserved:
        from .supercache import canonical_snapshot_id, canonical_structural_findings

        findings = canonical_structural_findings(sc)
        if findings:
            raise ScFormatError(f"cannot copy malformed extended cache: {findings[0].code}")
        source_id = canonical_snapshot_id(src)
        sparse_copy(src, dst)
        input_hash = sha256_sparse(src)
        output_hash = sha256_sparse(dst)
        output_id = canonical_snapshot_id(dst)
        status = "identity" if input_hash == output_hash and source_id == output_id else "fail"
        return IdentityResult(status, input_hash, output_hash, source_id, output_id,
                              len(sc.valid_slots), (), ())
    findings = structural_findings(sc)
    write_sc(dst, sc.header, sc.units(), check=not findings)
    in_sha = sha256_sparse(src)
    out_sha = sha256_sparse(dst)
    out = read_sc(dst)
    if not findings:
        status = "identity" if in_sha == out_sha else "fail"
    else:
        status = "identity_canonicalized"
    return IdentityResult(status, in_sha, out_sha, sc.snapshot_id(), out.snapshot_id(),
                          len(out.valid_slots), sc.skipped, tuple(findings))
