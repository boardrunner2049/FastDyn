"""``.scm`` v1 reader and writer (design §2.4-2.5; WP4).

The ``.scm`` file is written by the QEMU capture (``scc``, WP3) next to a ``.sc``
v5 cache. It is a 64-byte file header followed by framed chunks::

    u32 type | u32 payload_len | u64 seq | payload | zero pad to 8 | u64 trailer

where ``trailer`` is QEMU's FNV-1a-64 (:func:`scopt.canon.fnv1a64`) over
``[type .. end of padding]``.

Reader rules (§2.5.2), implemented by :func:`parse_scm`:

* a wrong file header rejects the whole file (:class:`ScmHeaderError`);
* a bad trailer, a ``seq`` that is not one greater than the previous one, a
  chunk that runs past end-of-file, or any chunk after END rejects that chunk
  **and every chunk after it** (prefix semantics, like a torn ``.sc`` tail);
* an unknown chunk type with a valid trailer is skipped (``skipped_chunks``);
  so are the Tier 1 OPS/PLUGIN_CBS chunks, which this version does not parse;
* a missing END marks the capture ``incomplete``; every complete TB chunk is
  still usable.

In addition, a correctly framed chunk whose payload does not decode (wrong
length, unsupported chunk version, a duplicate RUN_INFO, conflicting
duplicates of a helper id or a TB slot) is listed in ``rejected_chunks`` with
``prefix=False``; the chunks after it are unaffected. Conflicting duplicates are
dropped on *both* sides (fail closed: a slot without a chunk is ``no_meta`` and
pinned by the importer).

Insn indices are 0-based (the index into ``insns``). ``Site.insn_idx`` uses
``0xFFFFFFFF`` and ``OpMark``/``StaticExit.insn_idx`` (u16) use ``0xFFFF`` for
"before the first insn_start" (TB-start code).

Payload length convention. ``payload_len`` excludes the framing pad. The writer
emits the exact content length. The reader also accepts a ``payload_len`` that
already includes up to 7 trailing zero bytes (e.g. a TB chunk whose last array,
``guest_bytes``, was padded to 8 inside the payload) and records that in
``ScmChunk.pad_in_payload`` so that ``encode(parse(x)) == x`` byte for byte.

All array records are kept raw (every reserved/pad field included) so the file
round-trips exactly; :meth:`TbChunk.model_arrays` converts them to the
``scopt.model.unit`` types the importer uses.
"""

from __future__ import annotations

import json
import os
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Union

from ..canon import canonical_json, fnv1a64, hexaddr
from . import layouts as L
from .enums import (
    NONE_U32, CaptureLevel, ChunkType, Encoding, MapsTag, OpMarkKind, SiteKind, TbFlag,
)

if TYPE_CHECKING:
    from ..model.native import InlineCall


__all__ = [
    "ScmError", "ScmHeaderError", "ScmDecodeError", "ScmEncodeError", "SnapshotMergeConflict",
    "SNAPSHOT_MERGE_CONFLICT", "NONE_U16",
    "ScmFileHeader", "RunInfo", "HelperInfo", "ScmInsn", "ScmSite", "ScmPool", "ScmLdst",
    "ScmOpMark", "ScmStaticExit", "TbChunk", "MapsChunk", "RamBlock", "RamblocksChunk",
    "EndChunk", "RawChunkBody", "ChunkSource", "ScmChunk", "RejectedChunk", "ScmFile",
    "VariantChunk", "VariantPatch", "parse_scm", "read_scm", "encode_scm", "write_scm", "make_scm", "frame_chunk",
    "encode_body", "body_type", "replace_body", "slice_scm", "merge_scm", "merge_scm_sources", "scm_to_jsonl_rows", "chunk_to_json",
]

SNAPSHOT_MERGE_CONFLICT = "snapshot_merge_conflict"
# u16 "none" used by the capture for OpMark/StaticExit insn_idx outside any guest
# insn (e.g. TB-start code); the u32 fields use enums.NONE_U32.
NONE_U16 = 0xFFFF

_U32 = struct.Struct("<I")
_TRAILER = L.SCM_CHUNK_TRAILER
_CHDR = L.SCM_CHUNK_HEADER
# Same 32 bytes as layouts.SCM_LDST, but the 3 pad bytes are kept (lossless).
_LDST_RAW = struct.Struct("<2IIIIIIB3s")
assert _LDST_RAW.size == L.SCM_LDST.size
_TIER1_TYPES = frozenset({int(ChunkType.OPS), int(ChunkType.PLUGIN_CBS)})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ScmError(Exception):
    """Base class for ``.scm`` errors."""


class ScmHeaderError(ScmError):
    """The 64-byte file header is wrong; the whole file is rejected."""


class ScmDecodeError(ScmError):
    """A correctly framed chunk whose payload does not decode."""


class ScmEncodeError(ScmError, ValueError):
    """An in-memory object cannot be encoded (e.g. a field out of range)."""


class SnapshotMergeConflict(ScmError):
    """Two captures carry different TB chunks for the same ``.sc`` slot."""

    reason = SNAPSHOT_MERGE_CONFLICT

    def __init__(self, slot: int, first: str | None, second: str | None):
        self.slot = slot
        self.first = first
        self.second = second
        super().__init__(f"{SNAPSHOT_MERGE_CONFLICT}: slot {slot} differs between "
                         f"{first or '<memory>'} and {second or '<memory>'}")


# ---------------------------------------------------------------------------
# File header
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScmFileHeader:
    """§2.5.1. ``checksum=None`` means "compute when encoding"."""

    capture_level: int = int(CaptureLevel.RELOC)
    pid: int = 0
    created_realtime_ns: int = 0
    reserved: tuple[int, int, int] = (0, 0, 0)
    magic: bytes = L.SCM_MAGIC
    version: int = L.SCM_VERSION
    header_size: int = L.SCM_FILE_HEADER.size
    checksum: int | None = None

    SIZE = L.SCM_FILE_HEADER.size

    @classmethod
    def decode(cls, b: bytes) -> "ScmFileHeader":
        (magic, version, header_size, level, pid, ns, r0, r1, r2,
         ck) = L.SCM_FILE_HEADER.unpack(b[: L.SCM_FILE_HEADER.size])
        return cls(capture_level=level, pid=pid, created_realtime_ns=ns, reserved=(r0, r1, r2),
                   magic=magic, version=version, header_size=header_size, checksum=ck)

    def _body(self) -> bytes:
        return L.SCM_FILE_HEADER.pack(self.magic, self.version, self.header_size,
                                      self.capture_level, self.pid, self.created_realtime_ns,
                                      *self.reserved, 0)[: L.SCM_FILE_HEADER_CHECKSUM_LEN]

    def computed_checksum(self) -> int:
        return fnv1a64(self._body())

    def encode(self) -> bytes:
        body = self._body()
        ck = fnv1a64(body) if self.checksum is None else self.checksum
        return body + struct.pack("<Q", ck)

    @property
    def checksum_ok(self) -> bool:
        return self.checksum is None or self.checksum == self.computed_checksum()


# ---------------------------------------------------------------------------
# Chunk bodies
# ---------------------------------------------------------------------------

_RUN_INFO_FIELDS = (
    "info_version", "capture_level", "code_address", "code_size", "prologue_end",
    "tb_ret_off", "epilogue_off", "runtime_signature", "header_record_count_at_load",
    "loaded_max_end", "highwater_off", "icache_linesize", "tb_struct_size", "host_page_size",
    "target_page_bits", "insn_start_words", "cpuinfo", "exe_st_dev", "exe_st_ino",
    "exe_st_size", "exe_st_mtim_ns", "flags",
)


@dataclass(frozen=True, slots=True)
class RunInfo:
    """RUN_INFO payload (§2.5.3). ``prologue`` is ``[0, prologue_len)`` of the buffer."""

    info_version: int
    capture_level: int
    code_address: int
    code_size: int
    prologue_end: int
    tb_ret_off: int
    epilogue_off: int
    runtime_signature: int
    header_record_count_at_load: int
    loaded_max_end: int
    highwater_off: int
    icache_linesize: int
    tb_struct_size: int
    host_page_size: int
    target_page_bits: int
    insn_start_words: int
    cpuinfo: int
    exe_st_dev: int
    exe_st_ino: int
    exe_st_size: int
    exe_st_mtim_ns: int
    flags: int
    prologue: bytes

    TYPE = ChunkType.RUN_INFO

    @property
    def prologue_len(self) -> int:
        return len(self.prologue)

    def encode(self) -> bytes:
        vals = [getattr(self, n) for n in _RUN_INFO_FIELDS]
        return _pack(L.SCM_RUN_INFO, *vals, len(self.prologue)) + bytes(self.prologue)

    @classmethod
    def decode(cls, p: bytes) -> tuple["RunInfo", int]:
        _need(p, L.SCM_RUN_INFO.size, "run_info")
        vals = L.SCM_RUN_INFO.unpack_from(p, 0)
        plen = vals[-1]
        need = L.SCM_RUN_INFO.size + plen
        pad = _check_len(p, need, "run_info")
        ri = cls(*vals[:-1], prologue=bytes(p[L.SCM_RUN_INFO.size: need]))
        if ri.info_version != L.SCM_RUN_INFO_VERSION:
            raise ScmDecodeError(f"unsupported_run_info_version:{ri.info_version}")
        return ri, pad


@dataclass(frozen=True, slots=True)
class HelperInfo:
    """HELPER payload (§2.5.4). ``name`` is raw bytes; ``b""`` when ``info->name`` is NULL."""

    helper_id: int
    tcg_flags: int
    typemask: int
    func: int
    name: bytes = b""

    TYPE = ChunkType.HELPER

    @property
    def name_str(self) -> str | None:
        return self.name.decode("utf-8", "replace") if self.name else None

    def encode(self) -> bytes:
        return _pack(L.SCM_HELPER, self.helper_id, self.tcg_flags, self.typemask,
                     len(self.name), self.func) + bytes(self.name)

    @classmethod
    def decode(cls, p: bytes) -> tuple["HelperInfo", int]:
        _need(p, L.SCM_HELPER.size, "helper")
        hid, fl, tm, nlen, func = L.SCM_HELPER.unpack_from(p, 0)
        need = L.SCM_HELPER.size + nlen
        pad = _check_len(p, need, "helper")
        return cls(hid, fl, tm, func, bytes(p[L.SCM_HELPER.size: need])), pad


# --- TB arrays (raw, lossless) ------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScmInsn:
    words: tuple[int, int, int]
    host_start: int
    host_end: int

    def encode(self) -> bytes:
        return _pack(L.SCM_INSN, *self.words, self.host_start, self.host_end)

    @classmethod
    def unpack(cls, b: bytes, off: int) -> "ScmInsn":
        w0, w1, w2, hs, he = L.SCM_INSN.unpack_from(b, off)
        return cls((w0, w1, w2), hs, he)


@dataclass(frozen=True, slots=True)
class ScmSite:
    field_off: int
    insn_off: int
    width: int
    enc: int
    kind: int
    aux: int
    value: int
    insn_idx: int = NONE_U32  # 0xFFFFFFFF = TB-start / none
    final_field: int = 0

    def encode(self) -> bytes:
        return _pack(L.SCM_SITE, self.field_off, self.insn_off, self.width, self.enc, self.kind,
                     self.aux, self.value, self.insn_idx, self.final_field)

    @classmethod
    def unpack(cls, b: bytes, off: int) -> "ScmSite":
        return cls(*L.SCM_SITE.unpack_from(b, off))


@dataclass(frozen=True, slots=True)
class ScmPool:
    user_field_off: int
    slot_off: int
    rtype: int
    nlong: int
    addend: int
    data0: int

    def encode(self) -> bytes:
        return _pack(L.SCM_POOL, self.user_field_off, self.slot_off, self.rtype, self.nlong,
                     self.addend, self.data0)

    @classmethod
    def unpack(cls, b: bytes, off: int) -> "ScmPool":
        return cls(*L.SCM_POOL.unpack_from(b, off))


@dataclass(frozen=True, slots=True)
class ScmLdst:
    jcc_field: tuple[int, int]  # 0xFFFFFFFF = unused
    slow_start: int
    slow_end: int
    raddr_off: int
    oi: int
    op_ordinal: int
    is_ld: int
    pad: bytes = b"\0\0\0"

    def encode(self) -> bytes:
        return _pack(_LDST_RAW, *self.jcc_field, self.slow_start, self.slow_end, self.raddr_off,
                     self.oi, self.op_ordinal, self.is_ld, bytes(self.pad))

    @classmethod
    def unpack(cls, b: bytes, off: int) -> "ScmLdst":
        j0, j1, ss, se, ra, oi, ordn, is_ld, pad = _LDST_RAW.unpack_from(b, off)
        return cls((j0, j1), ss, se, ra, oi, ordn, is_ld, pad)


@dataclass(frozen=True, slots=True)
class ScmOpMark:
    host_off: int
    kind: int
    insn_idx: int
    helper_id: int = NONE_U32
    reserved: int = 0

    def encode(self) -> bytes:
        return _pack(L.SCM_OPMARK, self.host_off, self.kind, self.insn_idx, self.helper_id,
                     self.reserved)

    @classmethod
    def unpack(cls, b: bytes, off: int) -> "ScmOpMark":
        return cls(*L.SCM_OPMARK.unpack_from(b, off))


@dataclass(frozen=True, slots=True)
class ScmStaticExit:
    slot: int  # 0, 1, or 0xFF (off-page goto_ptr fallback)
    direct: int
    insn_idx: int
    dest_vaddr: int
    reserved: int = 0

    def encode(self) -> bytes:
        return _pack(L.SCM_STATIC_EXIT, self.slot, self.direct, self.insn_idx, self.reserved,
                     self.dest_vaddr)

    @classmethod
    def unpack(cls, b: bytes, off: int) -> "ScmStaticExit":
        slot, direct, idx, res, dest = L.SCM_STATIC_EXIT.unpack_from(b, off)
        return cls(slot, direct, idx, dest, res)


_ARRAYS = (
    # (attribute, element struct size, element class)
    ("insns", L.SCM_INSN.size, ScmInsn),
    ("sites", L.SCM_SITE.size, ScmSite),
    ("pool", L.SCM_POOL.size, ScmPool),
    ("ldst", L.SCM_LDST.size, ScmLdst),
    ("opmarks", L.SCM_OPMARK.size, ScmOpMark),
    ("static_exits", L.SCM_STATIC_EXIT.size, ScmStaticExit),
)


@dataclass(frozen=True, slots=True)
class ChunkSource:
    """Where a chunk came from (diagnostics only; never compared)."""

    path: str | None
    seq: int
    offset: int


def _valid_native_scalar_fields(call: InlineCall) -> bool:
    offsets = (call.call_off, call.continuation_off, call.helper_id, call.target_abs,
               call.elf_address, call.image_offset, *call.guest_pcs,
               *call.fragment.continuation_fields)
    if call.splice is not None:
        offsets += (call.splice.start_off, call.splice.depth, call.tcg_typemask)
        offsets += tuple(n for r in call.splice.relocations
                         for n in (r.field_off, r.next_off, r.target_abs))
    if call.pool_slot_off is not None:
        offsets += (call.pool_slot_off,)
    offsets += tuple(n for o in call.fragment.origins
                     for n in (o.source_off, o.source_size, o.output_off, o.output_size))
    return (all(type(n) is int and 0 <= n < (1 << 64) for n in offsets)
            and all(isinstance(s, str) and s for s in
                    (call.target, call.unit_hash, call.image_id, call.native_input_id, call.symbol))
            and (call.bridge_sha256 is None or isinstance(call.bridge_sha256, str)))


_NATIVE_TB_VERSIONS = (L.SCM_TB_CHUNK_VERSION_3, L.SCM_TB_CHUNK_VERSION_4)


def _native_metadata_matches(version: int, start: int, size: int,
                             requests: tuple[InlineCall, ...]) -> bool:
    if version == L.SCM_TB_CHUNK_VERSION_3:
        return 0 < start < size and bool(requests) and all(r.splice is None for r in requests)
    if not 0 <= start < size or len(requests) != 1:
        return False
    req, = requests
    splice = req.splice
    if (splice is None or splice.start_off != start or start > req.call_off
            or len(splice.prefix) != req.call_off - start
            or req.continuation_off != req.call_off + len(req.call_bytes)
            or start + len(splice.code) > size):
        return False
    if splice.kind != "frame":
        return splice.kind == "integer" and splice.depth == 1 and not splice.relocations
    if (splice.depth not in (1, 2) or splice.prefix or start != req.call_off
            or req.tcg_typemask != 400
            or req.pool_slot_off is not None or len(req.call_bytes) != 5
            or req.call_bytes[:1] != b"\xe8" or not splice.code):
        return False
    end = 0
    for r in splice.relocations:
        if (not end <= r.field_off < r.field_off + 4 <= r.next_off <= len(splice.code)
                or splice.code[r.field_off:r.field_off + 4] != b"\0" * 4):
            return False
        end = r.field_off + 4
    return True


@dataclass(frozen=True, slots=True)
class TbChunk:
    """Captured TB v1 and derived TB v2–v4. Array counts are tuple lengths."""

    slot: int
    commit_seq: int
    record: bytes  # 104 raw record bytes, as pwritten to the .sc
    vaddr: int
    code_fnv64: int
    attempts: int
    flags: int
    insn0_start: int
    body_end: int
    ldst_end: int
    pool_start: int  # NONE_U32 = no pool
    code_size: int
    insns: tuple[ScmInsn, ...] = ()
    sites: tuple[ScmSite, ...] = ()
    pool: tuple[ScmPool, ...] = ()
    ldst: tuple[ScmLdst, ...] = ()
    opmarks: tuple[ScmOpMark, ...] = ()
    static_exits: tuple[ScmStaticExit, ...] = ()
    guest_bytes: bytes = b""
    tb_chunk_version: int = L.SCM_TB_CHUNK_VERSION
    insn_start_words: int = 3
    reserved: int = 0
    source: ChunkSource | None = field(default=None, compare=False, repr=False)
    native_fragments: tuple[InlineCall, ...] = ()

    TYPE = ChunkType.TB

    # -- derived -------------------------------------------------------------
    @property
    def icount(self) -> int:
        return len(self.insns)

    @property
    def pool_start_or_none(self) -> int | None:
        return None if self.pool_start == NONE_U32 else self.pool_start

    @property
    def stub_start(self) -> int | None:
        """TB chunk v2 (derived, B3 grown units): code offset of the guard stubs
        appended after the pool; None for v1 chunks or when absent."""
        if self.tb_chunk_version != L.SCM_TB_CHUNK_VERSION_2:
            return None
        v = self.reserved & 0xFFFF_FFFF
        return None if v == NONE_U32 else v

    @property
    def inline_start(self) -> int | None:
        if self.tb_chunk_version == L.SCM_TB_CHUNK_VERSION_3:
            return self.reserved & 0xFFFF_FFFF
        return None

    @property
    def splice_start(self) -> int | None:
        if self.tb_chunk_version == L.SCM_TB_CHUNK_VERSION_4:
            return self.reserved & 0xFFFF_FFFF
        return None

    @property
    def pool_end(self) -> int:
        """End of original code and pool, before either kind of appended code."""
        if self.inline_start is not None:
            return self.inline_start
        ss = self.stub_start
        return self.code_size if ss is None else ss

    @property
    def tb_flags(self) -> TbFlag:
        return TbFlag(self.flags & sum(int(f) for f in TbFlag))

    @property
    def sc_record(self):
        """The embedded record as ``scopt.formats.sc_v5.ScRecord``."""
        from .sc_v5 import ScRecord
        return ScRecord.from_bytes(self.record)

    def guest_fnv64(self) -> int:
        return fnv1a64(self.guest_bytes)

    # -- codec ---------------------------------------------------------------
    def encode(self) -> bytes:
        if len(self.record) != 104:
            raise ScmEncodeError(f"slot {self.slot}: record must be 104 bytes")
        native = b""
        reserved = self.reserved
        version = self.tb_chunk_version
        if version in _NATIVE_TB_VERSIONS:
            start = self.reserved & 0xFFFF_FFFF
            if not all(_valid_native_scalar_fields(call) for call in self.native_fragments):
                raise ScmEncodeError(f"tb_chunk_v{version}_native_data")
            if not _native_metadata_matches(version, start, self.code_size, self.native_fragments):
                raise ScmEncodeError(f"tb_chunk_v{version}_native_layout")
            native = canonical_json([call.to_json() for call in self.native_fragments])
            if len(native) > L.SCM_TB_NATIVE_MAX:
                raise ScmEncodeError(f"tb_chunk_v{version}_native_size")
            reserved = len(native) << 32 | start
        elif self.native_fragments:
            raise ScmEncodeError("native fragments require TB v3 or v4")
        hdr = _pack(L.SCM_TB_HEADER, self.tb_chunk_version, self.slot, self.commit_seq,
                    bytes(self.record), self.vaddr, self.code_fnv64, self.attempts, self.flags,
                    self.insn0_start, self.body_end, self.ldst_end, self.pool_start,
                    self.code_size, len(self.insns), self.insn_start_words, len(self.sites),
                    len(self.pool), len(self.ldst), len(self.opmarks), len(self.static_exits),
                    len(self.guest_bytes), reserved)
        parts = [hdr]
        for name, _sz, _cls in _ARRAYS:
            parts.extend(e.encode() for e in getattr(self, name))
        parts.extend((bytes(self.guest_bytes), native))
        return b"".join(parts)

    @classmethod
    def decode(cls, p: bytes, source: ChunkSource | None = None) -> tuple["TbChunk", int]:
        _need(p, L.SCM_TB_HEADER.size, "tb")
        (ver, slot, cseq, record, vaddr, cfnv, attempts, flags, insn0, body_end, ldst_end,
         pool_start, code_size, icount, isw, n_sites, n_pool, n_ldst, n_opm, n_sx, n_gb,
         reserved) = L.SCM_TB_HEADER.unpack_from(p, 0)
        if ver not in L.SCM_TB_CHUNK_VERSIONS:
            raise ScmDecodeError(f"unsupported_tb_chunk_version:{ver}")
        if ver == L.SCM_TB_CHUNK_VERSION_2 and reserved >> 32:
            raise ScmDecodeError("tb_chunk_v2_reserved")
        if isw != 3:
            raise ScmDecodeError(f"insn_start_words:{isw}")
        counts = (icount, n_sites, n_pool, n_ldst, n_opm, n_sx)
        need = L.SCM_TB_HEADER.size + sum(n * sz for n, (_a, sz, _c) in zip(counts, _ARRAYS)) + n_gb
        native = ()
        if ver in _NATIVE_TB_VERSIONS:
            length = reserved >> 32
            start = reserved & 0xFFFF_FFFF
            if (not 0 < length <= L.SCM_TB_NATIVE_MAX or not start < code_size
                    or ver == L.SCM_TB_CHUNK_VERSION_3 and start == 0):
                raise ScmDecodeError(f"tb_chunk_v{ver}_native_layout")
            pad = _check_len(p, need + length, "tb")
            raw = bytes(p[need:need + length])
            from ..model.native import InlineCall

            try:
                doc = json.loads(raw)
                if not isinstance(doc, list) or not doc:
                    raise ValueError("inline call list is empty or malformed")
                native = tuple(InlineCall.from_json(call) for call in doc)
                if not all(_valid_native_scalar_fields(call) for call in native):
                    raise ValueError("invalid inline call metadata scalar")
                if not _native_metadata_matches(ver, start, code_size, native):
                    raise ValueError("native metadata does not match the span")
                if canonical_json([call.to_json() for call in native]) != raw:
                    raise ValueError("non-canonical inline call metadata")
            except (ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError) as e:
                raise ScmDecodeError(f"tb_chunk_v{ver}_native_data:{e}") from e
            reserved &= 0xFFFF_FFFF  # Keep the modeled geometry independent of encoded length.
        else:
            pad = _check_len(p, need, "tb")
        off = L.SCM_TB_HEADER.size
        arrays: dict[str, tuple] = {}
        for n, (name, sz, ecls) in zip(counts, _ARRAYS):
            arrays[name] = tuple(ecls.unpack(p, off + i * sz) for i in range(n))
            off += n * sz
        guest = bytes(p[off: off + n_gb])
        return cls(slot=slot, commit_seq=cseq, record=bytes(record), vaddr=vaddr,
                   code_fnv64=cfnv, attempts=attempts, flags=flags, insn0_start=insn0,
                   body_end=body_end, ldst_end=ldst_end, pool_start=pool_start,
                   code_size=code_size, guest_bytes=guest, tb_chunk_version=ver,
                   insn_start_words=isw, reserved=reserved, source=source,
                   native_fragments=native, **arrays), pad

    # -- conversion to the importer's model types ------------------------------
    def model_arrays(self) -> dict[str, tuple]:
        """The arrays as ``scopt.model.unit`` records.

        Raises ``ValueError`` when an enum value (site kind, encoding, opmark
        kind) is outside the frozen §2.4 numbering; the importer pins such TBs.
        """
        from ..model import unit as U

        def opt(v: int) -> int | None:
            return None if v == NONE_U32 else v

        return {
            "insns": tuple(U.Insn(i, e.words, e.host_start, e.host_end)
                           for i, e in enumerate(self.insns)),
            "sites": tuple(U.Site(s.field_off, s.insn_off, s.width, Encoding(s.enc),
                                  SiteKind(s.kind), s.aux, s.value, opt(s.insn_idx),
                                  s.final_field) for s in self.sites),
            "pool": tuple(U.PoolEntry(p.user_field_off, p.slot_off, p.rtype, p.nlong, p.addend,
                                      p.data0) for p in self.pool),
            "ldst": tuple(U.Ldst((opt(d.jcc_field[0]), opt(d.jcc_field[1])), d.slow_start,
                                 d.slow_end, d.raddr_off, d.oi, d.op_ordinal, bool(d.is_ld))
                          for d in self.ldst),
            "opmarks": tuple(U.OpMark(m.host_off, OpMarkKind(m.kind), m.insn_idx,
                                      opt(m.helper_id)) for m in self.opmarks),
            "static_exits": tuple(U.StaticExit(x.slot, bool(x.direct), x.insn_idx, x.dest_vaddr)
                                  for x in self.static_exits),
        }


@dataclass(frozen=True, slots=True)
class VariantPatch:
    host_off: int
    old: bytes
    new: bytes
    reserved: int = 0


@dataclass(frozen=True, slots=True)
class VariantChunk:
    """VARIANT (type 9, Python-only; B3/B4 design §9.1 V19, WP18).

    Describes one variant ``C'`` of an artifact: the ext-table entry it was
    written as (``variant_index``, arena offsets, ``code_fnv64``), the output
    ``.sc`` slot of its general record ``C`` (``general_slot``), its one
    predecessor slot (``pred_slot``, ``slot_n``), the same-length patches
    applied to the relocated clone of ``C`` (offsets relative to tc) and the
    32-byte variant key (``variant_id`` of design §3.4). QEMU never writes it;
    a re-import rebuilds ``relocate(C) + patches`` and compares.
    """

    variant_index: int
    general_slot: int
    tb_offset: int
    tc_offset: int
    tc_size: int
    end_offset: int
    code_fnv64: int
    pred_slot: int
    slot_n: int
    exit_class: int
    variant_key: bytes
    patches: tuple[VariantPatch, ...] = ()
    flags: int = 0
    version: int = 1

    TYPE = ChunkType.VARIANT

    def encode(self) -> bytes:
        if len(self.variant_key) != 32:
            raise ScmEncodeError("variant_key must be 32 bytes")
        out = [_pack(L.SCM_VARIANT_HEADER, self.version, self.variant_index, self.general_slot,
                     self.flags, self.tb_offset, self.tc_offset, self.tc_size, self.end_offset,
                     self.code_fnv64, self.pred_slot, self.slot_n, self.exit_class,
                     len(self.patches), bytes(self.variant_key))]
        for p in self.patches:
            if len(p.old) != len(p.new):
                raise ScmEncodeError("variant patch is not same-length")
            out.append(_pack(L.SCM_VARIANT_PATCH, p.host_off, len(p.old), p.reserved))
            out.append(bytes(p.old) + bytes(p.new))
        return b"".join(out)

    @classmethod
    def decode(cls, p: bytes) -> tuple["VariantChunk", int]:
        _need(p, L.SCM_VARIANT_HEADER.size, "variant")
        (ver, vi, gslot, flags, tb, tc, tcs, end, fnv, pslot, n, xc, npat,
         key) = L.SCM_VARIANT_HEADER.unpack_from(p, 0)
        if ver != 1:
            raise ScmDecodeError(f"unsupported_variant_chunk_version:{ver}")
        off = L.SCM_VARIANT_HEADER.size
        pats = []
        for _ in range(npat):
            _need(p[off:], L.SCM_VARIANT_PATCH.size, "variant patch")
            ho, ln, res = L.SCM_VARIANT_PATCH.unpack_from(p, off)
            off += L.SCM_VARIANT_PATCH.size
            _need(p[off:], 2 * ln, "variant patch bytes")
            pats.append(VariantPatch(ho, bytes(p[off:off + ln]), bytes(p[off + ln:off + 2 * ln]),
                                     res))
            off += 2 * ln
        pad = _check_len(p, off, "variant")
        return cls(vi, gslot, tb, tc, tcs, end, fnv, pslot, n, xc, bytes(key), tuple(pats), flags,
                   ver), pad


@dataclass(frozen=True, slots=True)
class MapsChunk:
    """MAPS (§2.5.6): raw ``/proc/self/maps`` text, tagged first_commit (1) or exit (2)."""

    tag: int
    truncated: int
    data: bytes
    pad: int = 0

    TYPE = ChunkType.MAPS

    def encode(self) -> bytes:
        return _pack(L.SCM_MAPS_HEADER, self.tag, self.truncated, len(self.data),
                     self.pad) + bytes(self.data)

    @classmethod
    def decode(cls, p: bytes) -> tuple["MapsChunk", int]:
        _need(p, L.SCM_MAPS_HEADER.size, "maps")
        tag, trunc, n, pad = L.SCM_MAPS_HEADER.unpack_from(p, 0)
        need = L.SCM_MAPS_HEADER.size + n
        padn = _check_len(p, need, "maps")
        return cls(tag, trunc, bytes(p[L.SCM_MAPS_HEADER.size: need]), pad), padn

    @property
    def text(self) -> str:
        return self.data.decode("utf-8", "replace")


@dataclass(frozen=True, slots=True)
class RamBlock:
    idstr: bytes  # 64 raw bytes (NUL-padded)
    ram_offset: int
    used_length: int
    max_length: int
    host: int

    @property
    def name(self) -> str:
        return self.idstr.split(b"\0", 1)[0].decode("utf-8", "replace")

    def encode(self) -> bytes:
        idb = bytes(self.idstr)
        if len(idb) > 64:
            raise ScmEncodeError("ramblock idstr longer than 64 bytes")
        return _pack(L.SCM_RAMBLOCK_ROW, idb.ljust(64, b"\0"), self.ram_offset, self.used_length,
                     self.max_length, self.host)


@dataclass(frozen=True, slots=True)
class RamblocksChunk:
    """RAMBLOCKS (§2.5.6). Diagnostics and address witness only; never identity."""

    rows: tuple[RamBlock, ...]
    pad: int = 0

    TYPE = ChunkType.RAMBLOCKS

    def encode(self) -> bytes:
        return (_pack(L.SCM_RAMBLOCKS_HEADER, len(self.rows), self.pad)
                + b"".join(r.encode() for r in self.rows))

    @classmethod
    def decode(cls, p: bytes) -> tuple["RamblocksChunk", int]:
        _need(p, L.SCM_RAMBLOCKS_HEADER.size, "ramblocks")
        n, pad = L.SCM_RAMBLOCKS_HEADER.unpack_from(p, 0)
        need = L.SCM_RAMBLOCKS_HEADER.size + n * L.SCM_RAMBLOCK_ROW.size
        padn = _check_len(p, need, "ramblocks")
        rows = tuple(RamBlock(*L.SCM_RAMBLOCK_ROW.unpack_from(
            p, L.SCM_RAMBLOCKS_HEADER.size + i * L.SCM_RAMBLOCK_ROW.size)) for i in range(n))
        return cls(rows, pad), padn


@dataclass(frozen=True, slots=True)
class EndChunk:
    """END (§2.5.6): counters written by ``scc_finish`` at exit."""

    committed: int
    discarded: int
    overflow: int
    capture_error: int
    write_errors: int
    helpers: int
    clean: int = 1

    TYPE = ChunkType.END

    def encode(self) -> bytes:
        return _pack(L.SCM_END, self.committed, self.discarded, self.overflow,
                     self.capture_error, self.write_errors, self.helpers, self.clean)

    @classmethod
    def decode(cls, p: bytes) -> tuple["EndChunk", int]:
        _need(p, L.SCM_END.size, "end")
        pad = _check_len(p, L.SCM_END.size, "end")
        return cls(*L.SCM_END.unpack_from(p, 0)), pad


@dataclass(frozen=True, slots=True)
class RawChunkBody:
    """An unparsed payload (unknown type, Tier 1 chunk, or a malformed payload)."""

    payload: bytes

    def encode(self) -> bytes:
        return bytes(self.payload)


Body = Union[RunInfo, HelperInfo, TbChunk, MapsChunk, RamblocksChunk, EndChunk, VariantChunk,
             RawChunkBody]

_DECODERS = {
    int(ChunkType.RUN_INFO): RunInfo.decode,
    int(ChunkType.HELPER): HelperInfo.decode,
    int(ChunkType.MAPS): MapsChunk.decode,
    int(ChunkType.RAMBLOCKS): RamblocksChunk.decode,
    int(ChunkType.END): EndChunk.decode,
    int(ChunkType.VARIANT): VariantChunk.decode,
}


def body_type(body: Body) -> int:
    t = getattr(type(body), "TYPE", None)
    if t is None:
        raise ScmEncodeError("RawChunkBody needs an explicit chunk type")
    return int(t)


def encode_body(body: Body) -> bytes:
    return body.encode()


# ---------------------------------------------------------------------------
# Chunks and files
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScmChunk:
    """One framed chunk. ``body`` is the decoded payload (``RawChunkBody`` if not decoded)."""

    seq: int
    type: int
    body: Body
    offset: int | None = None  # file offset of the chunk header (reader only)
    pad_in_payload: int = 0  # zero bytes counted in payload_len beyond the content
    frame_pad: bytes | None = None  # raw framing pad as read (None: zeros)
    error: str | None = None  # decode error (the chunk is also in rejected_chunks)

    @property
    def type_name(self) -> str:
        try:
            return ChunkType(self.type).name
        except ValueError:
            return f"type{self.type}"


@dataclass(frozen=True, slots=True)
class RejectedChunk:
    offset: int
    seq: int | None
    type: int | None
    reason: str
    prefix: bool  # True: rejected by prefix semantics (this chunk and all after it)
    length: int | None = None  # framed length in bytes, if known


@dataclass(frozen=True, slots=True)
class ScmFile:
    path: str | None
    header: ScmFileHeader
    chunks: tuple[ScmChunk, ...]  # every correctly framed chunk of the valid prefix, in order
    run_info: RunInfo | None = None
    helpers: Mapping[int, HelperInfo] = field(default_factory=lambda: MappingProxyType({}))
    tbs: Mapping[int, TbChunk] = field(default_factory=lambda: MappingProxyType({}))
    maps: tuple[MapsChunk, ...] = ()
    ramblocks: RamblocksChunk | None = None
    end: EndChunk | None = None
    rejected_chunks: tuple[RejectedChunk, ...] = ()
    skipped_chunks: tuple[ScmChunk, ...] = ()  # unknown types / Tier 1, valid trailer
    variants: tuple[VariantChunk, ...] = ()  # VARIANT chunks (derived B3 captures only)
    warnings: tuple[str, ...] = ()
    file_size: int = 0
    valid_size: int = 0  # bytes of the accepted prefix (header + framed chunks)

    @property
    def incomplete(self) -> bool:
        """True when no END chunk was accepted (§2.5.2)."""
        return self.end is None

    @property
    def clean(self) -> bool:
        return self.end is not None and self.end.clean == 1

    def maps_by_tag(self, tag: int | MapsTag) -> MapsChunk | None:
        for m in self.maps:
            if m.tag == int(tag):
                return m
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "file_size": self.file_size,
            "valid_size": self.valid_size,
            "capture_level": self.header.capture_level,
            "chunks": len(self.chunks),
            "run_info": self.run_info is not None,
            "helpers": len(self.helpers),
            "tbs": len(self.tbs),
            "slots": [min(self.tbs), max(self.tbs)] if self.tbs else None,
            "maps": [m.tag for m in self.maps],
            "ramblocks": None if self.ramblocks is None else len(self.ramblocks.rows),
            "end": None if self.end is None else {
                f.name: getattr(self.end, f.name) for f in fields(EndChunk)},
            "incomplete": self.incomplete,
            "rejected_chunks": len(self.rejected_chunks),
            "skipped_chunks": len(self.skipped_chunks),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def frame_chunk(type_: int, seq: int, payload: bytes, *, frame_pad: bytes | None = None) -> bytes:
    """``u32 type | u32 payload_len | u64 seq | payload | pad | u64 FNV trailer``."""
    plen = len(payload)
    padn = L.align8(plen) - plen
    if frame_pad is None or len(frame_pad) != padn:
        frame_pad = b"\0" * padn
    body = _CHDR.pack(type_, plen, seq) + payload + frame_pad
    return body + _TRAILER.pack(fnv1a64(body))


def _chunk_bytes(ch: ScmChunk) -> bytes:
    if isinstance(ch.body, RawChunkBody):
        payload = ch.body.encode()
    else:
        payload = ch.body.encode() + b"\0" * ch.pad_in_payload
    return frame_chunk(ch.type, ch.seq, payload, frame_pad=ch.frame_pad)


def encode_scm(scm: ScmFile | tuple[ScmFileHeader, Sequence[ScmChunk]]) -> bytes:
    """Serialize a file: header, then every chunk in order (seq must be 0, 1, 2, ...)."""
    if isinstance(scm, ScmFile):
        header, chunks = scm.header, scm.chunks
    else:
        header, chunks = scm
    out = [header.encode()]
    for i, ch in enumerate(chunks):
        if ch.seq != i:
            raise ScmEncodeError(f"chunk {i} has seq {ch.seq}; use make_scm to renumber")
        out.append(_chunk_bytes(ch))
    return b"".join(out)


def write_scm(path: str | os.PathLike, scm: ScmFile) -> None:
    """Write ``scm`` to ``path`` (atomically: temp file, fsync, rename)."""
    data = encode_scm(scm)
    path = os.fspath(path)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def make_scm(bodies: Iterable[Body | tuple[int, Body]], header: ScmFileHeader | None = None,
             *, path: str | None = None) -> ScmFile:
    """Build an :class:`ScmFile` from chunk bodies, numbering ``seq`` from 0.

    A ``RawChunkBody`` must be given as ``(type, body)``. The result is what
    :func:`parse_scm` would return for the encoded bytes (minus offsets).
    """
    header = header or ScmFileHeader()
    chunks = []
    for i, b in enumerate(bodies):
        if isinstance(b, tuple):
            t, b = b
        else:
            t = body_type(b)
        chunks.append(ScmChunk(seq=i, type=int(t), body=b))
    return parse_scm(encode_scm((header, chunks)), path=path)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

def _try_frame(data: bytes, off: int) -> tuple[int, int, int, int, bool] | None:
    """(type, payload_len, seq, total_len, trailer_ok) of the chunk at ``off``, or None."""
    rem = len(data) - off
    if rem < _CHDR.size:
        return None
    typ, plen, seq = _CHDR.unpack_from(data, off)
    padded = L.align8(plen)
    total = _CHDR.size + padded + _TRAILER.size
    if total > rem:
        return None
    (trailer,) = _TRAILER.unpack_from(data, off + _CHDR.size + padded)
    ok = fnv1a64(memoryview(data)[off: off + _CHDR.size + padded]) == trailer
    return typ, plen, seq, total, ok


def parse_scm(data: bytes | bytearray | memoryview, path: str | None = None) -> ScmFile:
    """Parse ``.scm`` v1 bytes with the §2.5.2 reader rules (see the module docstring)."""
    data = bytes(data)
    if len(data) < L.SCM_FILE_HEADER.size:
        raise ScmHeaderError(f"file shorter than the {L.SCM_FILE_HEADER.size}-byte header")
    hdr = ScmFileHeader.decode(data)
    if hdr.magic != L.SCM_MAGIC:
        raise ScmHeaderError(f"bad magic {hdr.magic!r}")
    if hdr.version != L.SCM_VERSION:
        raise ScmHeaderError(f"unsupported version {hdr.version}")
    if hdr.header_size != L.SCM_FILE_HEADER.size:
        raise ScmHeaderError(f"bad header_size {hdr.header_size}")
    if not hdr.checksum_ok:
        raise ScmHeaderError("header checksum mismatch")

    warnings: list[str] = []
    if hdr.reserved != (0, 0, 0):
        warnings.append("header_reserved_nonzero")
    if hdr.capture_level not in {int(c) for c in CaptureLevel}:
        warnings.append(f"unknown_capture_level:{hdr.capture_level}")

    chunks: list[ScmChunk] = []
    rejected: list[RejectedChunk] = []
    skipped: list[ScmChunk] = []
    run_info: RunInfo | None = None
    helpers: dict[int, HelperInfo] = {}
    bad_helpers: set[int] = set()
    tbs: dict[int, TbChunk] = {}
    bad_slots: set[int] = set()
    maps: list[MapsChunk] = []
    ramblocks: RamblocksChunk | None = None
    end: EndChunk | None = None
    variants: list[VariantChunk] = []

    off = L.SCM_FILE_HEADER.size
    expected_seq = 0

    def reject_suffix(start: int, reason: str, fr) -> None:
        typ = seq = length = None
        if fr is not None:
            typ, _pl, seq, length, _ok = fr
        elif len(data) - start >= 8:
            typ, _pl = struct.unpack_from("<II", data, start)
        if fr is None:
            length = len(data) - start
        rejected.append(RejectedChunk(start, seq, typ, reason, True, length))
        if fr is None:
            return
        # Best effort: keep framing the rest, so the report says what was lost.
        o = start + fr[3]
        while o < len(data):
            nxt = _try_frame(data, o)
            if nxt is None or not nxt[4]:
                rejected.append(RejectedChunk(o, None if nxt is None else nxt[2],
                                              None if nxt is None else nxt[0],
                                              "after_rejected_unframed", True, len(data) - o))
                return
            rejected.append(RejectedChunk(o, nxt[2], nxt[0], "after_rejected", True, nxt[3]))
            o += nxt[3]

    while off < len(data):
        fr = _try_frame(data, off)
        if fr is None:
            reason = "truncated_header" if len(data) - off < _CHDR.size else "truncated"
            reject_suffix(off, reason, None)
            break
        typ, plen, seq, total, trailer_ok = fr
        if not trailer_ok:
            reject_suffix(off, "bad_trailer", fr)
            break
        if seq != expected_seq:
            reject_suffix(off, f"seq_gap:expected {expected_seq} got {seq}", fr)
            break
        if end is not None:
            reject_suffix(off, "after_end", fr)
            break
        expected_seq += 1
        p0 = off + _CHDR.size
        payload = data[p0: p0 + plen]
        frame_pad = data[p0 + plen: p0 + L.align8(plen)]
        if any(frame_pad):
            warnings.append(f"nonzero_frame_pad@{off}")
        src = ChunkSource(path, seq, off)
        body: Body = RawChunkBody(payload)
        pad_in_payload = 0
        error: str | None = None
        try:
            if typ == ChunkType.TB:
                body, pad_in_payload = TbChunk.decode(payload, src)
            elif typ in _DECODERS:
                body, pad_in_payload = _DECODERS[typ](payload)
        except ScmDecodeError as e:
            error = str(e)
        except struct.error as e:  # pragma: no cover - guarded by _need/_check_len
            error = f"malformed:{e}"

        if error is None:
            if typ == ChunkType.RUN_INFO:
                if run_info is not None:
                    error = "duplicate_run_info"
                else:
                    run_info = body  # type: ignore[assignment]
                    if seq != 0:
                        warnings.append(f"run_info_not_first:seq {seq}")
            elif typ == ChunkType.HELPER:
                h: HelperInfo = body  # type: ignore[assignment]
                if h.helper_id in bad_helpers:
                    error = "duplicate_helper_conflict"
                elif h.helper_id in helpers:
                    if helpers[h.helper_id] == h:
                        warnings.append(f"duplicate_helper_equal:{h.helper_id}")
                    else:
                        del helpers[h.helper_id]
                        bad_helpers.add(h.helper_id)
                        error = "duplicate_helper_conflict"
                else:
                    helpers[h.helper_id] = h
            elif typ == ChunkType.TB:
                tb: TbChunk = body  # type: ignore[assignment]
                if tb.slot in bad_slots:
                    error = "duplicate_slot_conflict"
                elif tb.slot in tbs:
                    if tbs[tb.slot].encode() == tb.encode():
                        warnings.append(f"duplicate_slot_equal:{tb.slot}")
                    else:
                        del tbs[tb.slot]
                        bad_slots.add(tb.slot)
                        error = "duplicate_slot_conflict"
                else:
                    tbs[tb.slot] = tb
            elif typ == ChunkType.MAPS:
                maps.append(body)  # type: ignore[arg-type]
            elif typ == ChunkType.RAMBLOCKS:
                if ramblocks is not None:
                    warnings.append("duplicate_ramblocks")
                else:
                    ramblocks = body  # type: ignore[assignment]
            elif typ == ChunkType.END:
                end = body  # type: ignore[assignment]
            elif typ == ChunkType.VARIANT:
                variants.append(body)  # type: ignore[arg-type]

        ch = ScmChunk(seq=seq, type=typ, body=body if error is None else RawChunkBody(payload),
                      offset=off, pad_in_payload=pad_in_payload if error is None else 0,
                      frame_pad=frame_pad, error=error)
        chunks.append(ch)
        if error is not None:
            rejected.append(RejectedChunk(off, seq, typ, error, False, total))
        elif typ in _TIER1_TYPES or (typ not in _DECODERS and typ != ChunkType.TB):
            skipped.append(ch)
        off += total

    valid_size = off if not rejected or not rejected[-1].prefix else next(
        r.offset for r in rejected if r.prefix)
    return ScmFile(
        path=path, header=hdr, chunks=tuple(chunks), run_info=run_info,
        helpers=MappingProxyType(dict(sorted(helpers.items()))),
        tbs=MappingProxyType(dict(sorted(tbs.items()))), maps=tuple(maps), ramblocks=ramblocks,
        end=end, rejected_chunks=tuple(rejected), skipped_chunks=tuple(skipped),
        warnings=tuple(warnings), file_size=len(data), valid_size=valid_size,
        variants=tuple(variants))


def read_scm(path: str | os.PathLike) -> ScmFile:
    """Read and parse a ``.scm`` file (see :func:`parse_scm`)."""
    p = os.fspath(path)
    with open(p, "rb") as f:
        data = f.read()
    return parse_scm(data, path=p)


# ---------------------------------------------------------------------------
# Union of several captures (§2.5.8)
# ---------------------------------------------------------------------------

def _as_scm(f: ScmFile | str | os.PathLike) -> ScmFile:
    return f if isinstance(f, ScmFile) else read_scm(f)


def merge_scm_sources(files: Iterable[ScmFile | str | os.PathLike]
                      ) -> dict[int, tuple[TbChunk, ScmFile]]:
    """Union TB chunks by slot, keeping the file each one came from.

    The importer needs the source file to bind a slot (its RUN_INFO) and to
    resolve helper ids (per-file HELPER tables). Overlapping slots must carry
    byte-equal TB payloads; otherwise :class:`SnapshotMergeConflict`
    (``snapshot_merge_conflict``) is raised.
    """
    out: dict[int, tuple[TbChunk, ScmFile]] = {}
    for f in files:
        scm = _as_scm(f)
        for slot, tb in scm.tbs.items():
            prev = out.get(slot)
            if prev is None:
                out[slot] = (tb, scm)
            elif prev[0].encode() != tb.encode():
                raise SnapshotMergeConflict(slot, prev[1].path, scm.path)
    return dict(sorted(out.items()))


def merge_scm(files: Iterable[ScmFile | str | os.PathLike]) -> dict[int, TbChunk]:
    """Union TB chunks by slot (§2.5.8): byte-equal overlap or ``snapshot_merge_conflict``."""
    return {slot: tb for slot, (tb, _src) in merge_scm_sources(files).items()}


# ---------------------------------------------------------------------------
# JSON views (``scopt meta dump --jsonl``)
# ---------------------------------------------------------------------------

def _enum_name(enum_cls, v: int) -> str:
    try:
        return enum_cls(v).name
    except ValueError:
        return f"0x{v:x}"


def _flags_names(v: int) -> list[str]:
    names = [f.name.lower() for f in TbFlag if v & int(f)]
    rest = v & ~sum(int(f) for f in TbFlag)
    if rest:
        names.append(hexaddr(rest))
    return names


def _record_json(record: bytes) -> dict[str, Any]:
    from .sc_v5 import ScRecord
    r = ScRecord.from_bytes(record)
    return {
        "tb_offset": hexaddr(r.tb_offset), "end_offset": hexaddr(r.end_offset),
        "tc_offset": hexaddr(r.tc_offset), "tc_size": r.tc_size, "pc": hexaddr(r.pc),
        "cs_base": hexaddr(r.cs_base), "page_addr0": hexaddr(r.page_addr0),
        "page_addr1": hexaddr(r.page_addr1), "guest_hash": hexaddr(r.guest_hash),
        "flags": hexaddr(r.flags), "cflags": hexaddr(r.cflags), "guest_size": r.guest_size,
        "icount": r.icount, "jmp_reset_offset": list(r.jmp_reset_offset),
        "jmp_insn_offset": list(r.jmp_insn_offset), "checksum": hexaddr(r.checksum),
        "checksum_ok": r.checksum_ok, "committed": hexaddr(r.committed),
    }


def _opt(v: int) -> int | None:
    return None if v == NONE_U32 else v


def _opt16(v: int) -> int | None:
    return None if v == NONE_U16 else v


def chunk_to_json(ch: ScmChunk, *, arrays: bool = True) -> dict[str, Any]:
    """A JSON-able dict for one chunk (addresses as ``"0x…"`` strings)."""
    out: dict[str, Any] = {"kind": "chunk", "seq": ch.seq, "type": ch.type_name,
                           "offset": ch.offset}
    if ch.error:
        out["error"] = ch.error
    b = ch.body
    if isinstance(b, RunInfo):
        d = {n: getattr(b, n) for n in _RUN_INFO_FIELDS}
        for n in ("code_address", "runtime_signature", "cpuinfo", "flags"):
            d[n] = hexaddr(d[n])
        for n in ("code_size", "prologue_end", "tb_ret_off", "epilogue_off", "loaded_max_end",
                  "highwater_off"):
            d[n] = hexaddr(d[n])
        d["prologue_len"] = b.prologue_len
        d["prologue_hex"] = b.prologue.hex()
        out["run_info"] = d
    elif isinstance(b, HelperInfo):
        out["helper"] = {"helper_id": b.helper_id, "tcg_flags": hexaddr(b.tcg_flags),
                         "typemask": hexaddr(b.typemask), "func": hexaddr(b.func),
                         "name": b.name_str}
    elif isinstance(b, TbChunk):
        d: dict[str, Any] = {
            "tb_chunk_version": b.tb_chunk_version, "slot": b.slot, "commit_seq": b.commit_seq,
            "record": _record_json(b.record), "vaddr": hexaddr(b.vaddr),
            "code_fnv64": hexaddr(b.code_fnv64), "attempts": b.attempts,
            "flags": _flags_names(b.flags), "insn0_start": b.insn0_start,
            "body_end": b.body_end, "ldst_end": b.ldst_end,
            "pool_start": _opt(b.pool_start), "code_size": b.code_size, "icount": b.icount,
            "insn_start_words": b.insn_start_words, "n_sites": len(b.sites),
            "n_pool": len(b.pool), "n_ldst": len(b.ldst), "n_opmarks": len(b.opmarks),
            "n_static_exits": len(b.static_exits), "n_guest_bytes": len(b.guest_bytes),
            "guest_fnv64": hexaddr(b.guest_fnv64()), "reserved": b.reserved,
        }
        if arrays:
            d["insns"] = [{"words": [hexaddr(w) for w in i.words], "host_start": i.host_start,
                           "host_end": i.host_end} for i in b.insns]
            d["sites"] = [{"field_off": s.field_off, "insn_off": s.insn_off, "width": s.width,
                           "enc": _enum_name(Encoding, s.enc), "kind": _enum_name(SiteKind, s.kind),
                           "aux": s.aux, "value": hexaddr(s.value), "insn_idx": _opt(s.insn_idx),
                           "final_field": hexaddr(s.final_field)} for s in b.sites]
            d["pool"] = [{"user_field_off": p.user_field_off, "slot_off": p.slot_off,
                          "rtype": p.rtype, "nlong": p.nlong, "addend": p.addend,
                          "data0": hexaddr(p.data0)} for p in b.pool]
            d["ldst"] = [{"jcc_field": [_opt(j) for j in x.jcc_field],
                          "slow_start": x.slow_start, "slow_end": x.slow_end,
                          "raddr_off": x.raddr_off, "oi": x.oi, "op_ordinal": x.op_ordinal,
                          "is_ld": x.is_ld} for x in b.ldst]
            d["opmarks"] = [{"host_off": m.host_off, "kind": _enum_name(OpMarkKind, m.kind),
                             "insn_idx": _opt16(m.insn_idx), "helper_id": _opt(m.helper_id)}
                            for m in b.opmarks]
            d["static_exits"] = [{"slot": x.slot, "direct": x.direct,
                                  "insn_idx": _opt16(x.insn_idx),
                                  "dest_vaddr": hexaddr(x.dest_vaddr)} for x in b.static_exits]
            d["guest_bytes"] = b.guest_bytes.hex()
        out["tb"] = d
    elif isinstance(b, MapsChunk):
        out["maps"] = {"tag": _enum_name(MapsTag, b.tag), "truncated": b.truncated,
                       "len": len(b.data)}
        if arrays:
            out["maps"]["text"] = b.text
    elif isinstance(b, RamblocksChunk):
        out["ramblocks"] = [{"idstr": r.name, "ram_offset": hexaddr(r.ram_offset),
                             "used_length": hexaddr(r.used_length),
                             "max_length": hexaddr(r.max_length), "host": hexaddr(r.host)}
                            for r in b.rows]
    elif isinstance(b, EndChunk):
        out["end"] = {f.name: getattr(b, f.name) for f in fields(EndChunk)}
    elif isinstance(b, VariantChunk):
        out["variant"] = {
            "variant_index": b.variant_index, "general_slot": b.general_slot,
            "tb_offset": hexaddr(b.tb_offset), "tc_offset": hexaddr(b.tc_offset),
            "tc_size": b.tc_size, "end_offset": hexaddr(b.end_offset),
            "code_fnv64": hexaddr(b.code_fnv64), "pred_slot": b.pred_slot, "slot_n": b.slot_n,
            "exit_class": b.exit_class, "variant_key": "sha256:" + b.variant_key.hex(),
            "flags": b.flags, "patches": [{"host_off": p.host_off, "old": p.old.hex(),
                                           "new": p.new.hex()} for p in b.patches]}
    else:
        out["payload_len"] = len(b.payload)
    if isinstance(b, TbChunk) and b.stub_start is not None:
        out["tb"]["stub_start"] = b.stub_start
    if isinstance(b, TbChunk) and b.inline_start is not None:
        out["tb"]["inline_start"] = b.inline_start
    if isinstance(b, TbChunk) and b.splice_start is not None:
        out["tb"]["splice_start"] = b.splice_start
    if isinstance(b, TbChunk) and b.native_fragments:
        out["tb"]["native_fragments"] = [call.to_json() for call in b.native_fragments]
    return out


def scm_to_jsonl_rows(scm: ScmFile, *, arrays: bool = True, slots: set[int] | None = None,
                      types: set[int] | None = None) -> list[dict[str, Any]]:
    """Rows for ``scopt meta dump --jsonl``: file header, chunks, rejected chunks, summary."""
    h = scm.header
    rows: list[dict[str, Any]] = [{
        "kind": "file_header", "path": scm.path, "magic": h.magic.decode("latin-1"),
        "version": h.version, "header_size": h.header_size,
        "capture_level": _enum_name(CaptureLevel, h.capture_level), "pid": h.pid,
        "created_realtime_ns": h.created_realtime_ns, "checksum": hexaddr(h.checksum or 0),
    }]
    for ch in scm.chunks:
        if types is not None and ch.type not in types:
            continue
        if slots is not None and not (isinstance(ch.body, TbChunk) and ch.body.slot in slots):
            continue
        rows.append(chunk_to_json(ch, arrays=arrays))
    for r in scm.rejected_chunks:
        rows.append({"kind": "rejected_chunk", "offset": r.offset, "seq": r.seq,
                     "type": None if r.type is None else _enum_name(ChunkType, r.type),
                     "reason": r.reason, "prefix": r.prefix, "length": r.length})
    rows.append({"kind": "summary", **scm.summary()})
    return rows


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _pack(s: struct.Struct, *vals) -> bytes:
    try:
        return s.pack(*vals)
    except struct.error as e:
        raise ScmEncodeError(f"cannot pack {s.format}: {e}") from None


def _need(p: bytes, n: int, what: str) -> None:
    if len(p) < n:
        raise ScmDecodeError(f"payload_len:{what} needs >= {n} bytes, has {len(p)}")


def _check_len(p: bytes, need: int, what: str) -> int:
    """Accept ``len(p) == need`` or ``need`` plus <= 7 zero bytes up to 8-alignment."""
    if len(p) == need:
        return 0
    if len(p) == L.align8(need) and not any(p[need:]):
        return len(p) - need
    raise ScmDecodeError(f"payload_len:{what} expects {need} bytes, has {len(p)}")


def slice_scm(scm: ScmFile, slots: Iterable[int], *, keep_maps: bool = True) -> ScmFile:
    """A small, valid ``.scm`` holding only the TB chunks of ``slots`` (for tests/data).

    Every non-TB chunk is kept in its original order (MAPS text optionally
    replaced by an empty body), HELPER chunks included, so helper ids still
    resolve. Chunks are renumbered from 0; TB payloads are byte-identical to the
    source. END counters are left as captured.
    """
    want = set(slots)
    bodies: list[Body | tuple[int, Body]] = []
    for ch in scm.chunks:
        b = ch.body
        if isinstance(b, TbChunk) and b.slot not in want:
            continue
        if ch.error is not None:
            continue
        if isinstance(b, MapsChunk) and not keep_maps:
            b = replace(b, data=b"", truncated=1)
        bodies.append((ch.type, b) if isinstance(b, RawChunkBody) else b)
    return make_scm(bodies, scm.header, path=None)


def replace_body(scm: ScmFile, seq: int, body: Body) -> ScmFile:
    """A copy of ``scm`` with chunk ``seq``'s body replaced (re-parsed from bytes)."""
    chunks = [replace(c, body=body, pad_in_payload=0, error=None) if c.seq == seq else c
              for c in scm.chunks]
    return parse_scm(encode_scm((scm.header, chunks)), path=scm.path)
