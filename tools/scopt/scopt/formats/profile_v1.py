"""Profile v1: ingest, file I/O, merge, flow check and ProfileView (design §3.6-3.8; WP6).

Pipeline::

    .qprof (QEMU scp, WP5) + staged .sc (the run's copy, after the run) + snapshot
        --ingest()-->  Profile (profiles/<hex of profile_id>/profile.json, canonical JSON)
        --merge()-->   Profile (several runs of one snapshot)
        --ProfileView.open(profile, snapshot_or_artifact)--> counts by UnitId, with status

Ingest and identity join (§3.6)
-------------------------------
* The ``.qprof`` header binds the run to the snapshot it loaded:
  ``code_address``, ``runtime_signature``, ``loaded_record_count``,
  ``loaded_header_checksum`` and ``loaded_max_end`` must equal the snapshot's
  ``.sc`` header / valid records (``snapshot_mismatch`` and friends otherwise).
  A profile of a cold run (nothing loaded) is ``not_warm``.
* The staged ``.sc`` must be that snapshot plus appended records: same header
  apart from ``record_count``/``checksum``, and byte-equal records and arena
  bytes for every loaded valid slot (``staged_mismatch``).
* A TB row whose ``tb_offset`` is a loaded valid slot with equal key, size and
  icount is ``SNAPSHOT`` and maps to that slot's UnitId. A key mismatch is
  ``row_key_mismatch``.
* A row at ``tb_offset >= loaded_max_end`` that matches an appended valid record
  is ``PROFILE_ONLY``: kept by key and guest_hash in ``profile_only`` and never
  attributed to a snapshot unit, even when its tb_uid equals one.
* A row with ``page_addr0 == -1`` is ``TRANSIENT`` (one-shot TB); its entries go
  to ``transient`` and do not affect completeness.
* Any other row is ``unjoined_row`` (fail closed: the ingest is refused).

Status (§3.7)
-------------
* ``exact``: clean exit, valid trailer (the reader refuses a bad trailer), no
  overflow, method ``exact_dispatch_unchained_v1``, unit in the domain, phase
  reached. An exact 0 is a real zero: every domain unit is listed, with zeros.
* ``lower_bound``: unclean exit or overflow; observed values are kept and the
  unit's ``reason`` says why (``unclean_exit``, ``tb_overflow``,
  ``edge_overflow``, comma-joined).
* ``unknown``: ``not_in_domain``, ``phase_not_reached`` or ``not_collected``.
  These are produced by :class:`ProfileView`; a profile file never stores an
  unknown unit. An ingested (single-run) unit's ``phase`` map omits phases the
  run did not reach. In a merge where only some runs reached a phase, that
  phase is ``reached=false`` (``x_reached_runs`` counts the runs that did) but
  the counts of those runs are kept, so flow conservation still holds on the
  merged file; :class:`ProfileView` reports every count of such a phase as
  ``unknown/phase_not_reached``.

JSON extensions (``x_`` keys, allowed by the frozen schema)
----------------------------------------------------------
* ``x_external_edges``: edges with an endpoint outside the snapshot domain.
  Endpoints are a UnitId, ``"po:<i>"`` (index into ``profile_only``),
  ``"transient"`` or ``null`` (NONE, kind ``first``). ``edges`` itself only
  holds edges whose endpoints are snapshot units (or a ``null`` src), as the
  schema requires. Together they make flow conservation checkable on the
  Profile alone (:func:`check_flow`).
* ``transient.x_phase`` / ``x_rows``: per-phase transient counts and row count.
* ``profile_only[i].x_tb_offset`` / ``x_slot`` / ``x_run``: observed placement
  (never an identity) and the index of the run it came from.
* ``phases[i].x_reached_runs``: how many merged runs reached the phase.
* ``collection.runs[i].x_*``: qprof sha256, header flags, appended records.
* ``x_runtime``: the RuntimeIdentity fields that §3.8's compatibility check
  compares (``qemu_sha256``, ``plugin_sha256``, ``runtime_signature``,
  ``prologue_sha256``, ``cpuinfo``), when the ingest was given the document.
* ``x_sc_binding``: ``code_address`` and ``runtime_signature`` from the
  ``.qprof`` header (always written by the ingest, whatever form
  ``runtime_identity`` took). :meth:`ProfileView.open` rejects a target ``.sc``
  whose header ``runtime_signature`` differs (``profile_incompatible:runtime``),
  even when the caller passes the same ``runtime_identity_id``.
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ..canon import canonical_json, hexaddr, is_sha256_id, parse_hex
from ..ids import TBKey, profile_id as compute_profile_id, runtime_identity_id
from . import schemas
from .enums import (
    NONE_U64, CountStatus, QprofEdgeFlag, QprofEdgeKind, UnknownReason,
    EDGE_FLAG_NAMES, EDGE_KIND_NAMES,
)
from .qprof_v1 import QprofFile, read_qprof
from .sc_v5 import ScFile, ScHeader, read_sc

__all__ = [
    "SCHEMA", "VERSION", "METHOD", "DEFAULT_MIN_PROFILE_MATCH", "RUNTIME_COMPAT_FIELDS",
    "INDIRECT_KINDS", "ProfileError", "ProfileIngestError", "ProfileFormatError",
    "ProfileIdMismatch", "ProfileIncompatible", "ProfileMergeError", "Count", "Profile",
    "SnapshotUnits", "UnitRef", "ProfileTarget", "ProfileView", "FlowReport", "ingest",
    "merge", "check_flow", "write_profile", "read_profile", "publish_profile", "profile_dir",
    "status_min", "build_profile_doc",
]

SCHEMA = "fastdyn.profile"
VERSION = 1
METHOD = "exact_dispatch_unchained_v1"
DEFAULT_MIN_PROFILE_MATCH = 0.95
RUNTIME_COMPAT_FIELDS = ("qemu_sha256", "plugin_sha256", "runtime_signature", "prologue_sha256",
                         "cpuinfo")
COUNT_FIELDS = ("entries", "aborted", "longjmp", "lookup_hits")
# Indirect transfers: an unobserved destination means "not observed", never
# "unreachable" (§3.7). goto_ptr shows up as lookup_hit/lookup_miss.
INDIRECT_KINDS = frozenset({"lookup_hit", "lookup_miss", "exit_null", "exit_requested"})
TRANSIENT_NODE = "transient"
_STATUS_RANK = {CountStatus.EXACT: 3, CountStatus.LOWER_BOUND: 2, CountStatus.SAMPLED: 1,
                CountStatus.UNKNOWN: 0}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProfileError(ValueError):
    """Base class; ``reason`` is a stable code."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(reason + (f": {detail}" if detail else ""))


class ProfileIngestError(ProfileError):
    """The ``.qprof`` cannot be joined to the snapshot (fail closed)."""


class ProfileFormatError(ProfileError):
    """A ``profile.json`` fails the schema or is not canonical."""


class ProfileIdMismatch(ProfileFormatError):
    def __init__(self, stored: str, computed: str):
        super().__init__("profile_id_mismatch", f"stored {stored}, computed {computed}")
        self.stored = stored
        self.computed = computed


class ProfileIncompatible(ProfileError):
    """§3.8 hard reject. ``reason`` is ``profile_incompatible:<what>`` with
    ``what`` in ``bundle``, ``contract``, ``runtime``, ``coverage``."""

    CODE = "profile_incompatible"

    def __init__(self, what: str, detail: str = "", coverage: float | None = None):
        self.what = what
        self.coverage = coverage
        super().__init__(f"{self.CODE}:{what}", detail)


class ProfileMergeError(ProfileError):
    pass


# ---------------------------------------------------------------------------
# Status and counts
# ---------------------------------------------------------------------------

def status_min(*statuses: str) -> str:
    """The weakest status (exact > lower_bound > sampled > unknown)."""
    return min((CountStatus(s) for s in statuses), key=lambda s: _STATUS_RANK[s]).value


@dataclass(frozen=True, slots=True)
class Count:
    """``{value, status, reason?}`` (§3.7). ``value`` is None when unknown."""

    value: int | None
    status: str
    reason: str | None = None

    @property
    def exact(self) -> bool:
        return self.status == CountStatus.EXACT

    @property
    def known(self) -> bool:
        return self.status != CountStatus.UNKNOWN

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"value": self.value, "status": self.status}
        if self.reason:
            d["reason"] = self.reason
        return d


def _unknown(reason: str) -> Count:
    return Count(None, CountStatus.UNKNOWN.value, reason)


# ---------------------------------------------------------------------------
# Snapshot side: the units a profile can be attributed to
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class UnitRef:
    uid: str
    slot: int
    tb_offset: int
    end_offset: int
    key: TBKey
    guest_size: int
    icount: int
    guest_hash: int
    record_bytes: bytes | None = None
    body: bytes | None = None


@dataclass(frozen=True)
class SnapshotUnits:
    """The valid slots of a snapshot (or artifact) ``.sc``, with UnitIds."""

    snapshot_id: str
    header: ScHeader
    units: tuple[UnitRef, ...]
    path: str | None = None

    @property
    def max_end(self) -> int:
        return max((u.end_offset for u in self.units), default=0)

    def by_tb_offset(self) -> dict[int, UnitRef]:
        return {u.tb_offset: u for u in self.units}

    @property
    def unit_ids(self) -> tuple[str, ...]:
        return tuple(u.uid for u in self.units)

    @classmethod
    def from_sc(cls, sc: ScFile) -> "SnapshotUnits":
        if not sc.loadable:
            raise ProfileIngestError("snapshot_not_loadable",
                                     f"{sc.path}: {', '.join(sc.header_problems)}")
        units = []
        for uid, u in zip(sc.unit_ids(), sc.units()):
            r = u.record
            units.append(UnitRef(uid, u.slot, r.tb_offset, r.end_offset, r.key, r.guest_size,
                                 r.icount, r.guest_hash, u.record_bytes, u.body))
        return cls(sc.snapshot_id(), sc.header, tuple(units), sc.path)

    @classmethod
    def from_any(cls, obj: Any) -> "SnapshotUnits":
        """From a :class:`SnapshotUnits`, an ``ScFile``, a ``.sc`` path, a store
        snapshot / artifact directory (``cache.sc`` inside), or a SnapshotView-like
        object (``snapshot_id``, ``header``, ``units()`` of TBUnit-like objects)."""
        if isinstance(obj, SnapshotUnits):
            return obj
        if isinstance(obj, ScFile):
            return cls.from_sc(obj)
        if isinstance(obj, (str, os.PathLike)):
            p = os.fspath(obj)
            if os.path.isdir(p):
                p = os.path.join(p, "cache.sc")
            return cls.from_sc(read_sc(p))
        if hasattr(obj, "units") and hasattr(obj, "snapshot_id"):
            units = []
            for u in obj.units():
                rec = getattr(u, "record", None)
                body = None
                if hasattr(u, "code") and hasattr(u, "search"):
                    body = bytes(u.code) + bytes(u.search)
                units.append(UnitRef(u.uid, u.slot, u.tb_offset, u.end_offset, u.key,
                                     u.guest_size, u.icount, u.guest_hash,
                                     bytes(rec) if isinstance(rec, (bytes, bytearray)) else None,
                                     body))
            hdr = obj.header
            if isinstance(hdr, (bytes, bytearray)):
                hdr = ScHeader.from_bytes(bytes(hdr))
            return cls(obj.snapshot_id, hdr, tuple(units), getattr(obj, "path", None))
        raise TypeError(f"cannot read snapshot units from {type(obj).__name__}")


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _key_json(key: TBKey) -> dict[str, Any]:
    return {"page_addr0": hexaddr(key.page_addr0),
            "page_addr1": None if key.page_addr1 is None else hexaddr(key.page_addr1),
            "flags": hexaddr(key.flags), "cs_base": hexaddr(key.cs_base),
            "cflags": hexaddr(key.cflags)}


def _flag_names(flags: int) -> list[str]:
    return [EDGE_FLAG_NAMES[f] for f in QprofEdgeFlag if flags & f]


def _kind_name(kind: int) -> str:
    try:
        return EDGE_KIND_NAMES[QprofEdgeKind(kind)]
    except ValueError as e:
        raise ProfileIngestError("edge_kind", f"unknown edge kind {kind}") from e


def _counts_json(c: Any) -> dict[str, int]:
    return {"entries": c.entries, "aborted": c.aborted, "longjmp": c.longjmp,
            "lookup_hits": c.lookup_hits}


def _zero_counts() -> dict[str, int]:
    return {k: 0 for k in COUNT_FIELDS}


def _add_counts(a: dict[str, int], b: Mapping[str, int]) -> None:
    for k in COUNT_FIELDS:
        a[k] = a.get(k, 0) + int(b.get(k, 0))


def _node_sort_key(n: str | None) -> tuple[int, str]:
    return (0, "") if n is None else (1, n)


# ---------------------------------------------------------------------------
# Profile object
# ---------------------------------------------------------------------------

class Profile:
    """An immutable Profile v1 document (``doc`` is the canonical-JSON dict)."""

    __slots__ = ("_doc", "_bytes")

    def __init__(self, doc: Mapping[str, Any], *, validate: bool = True):
        d = json.loads(canonical_json(doc))  # deep copy, JSON types only
        if validate:
            errs = schemas.errors(d, "profile.v1")
            if errs:
                raise ProfileFormatError("schema", "; ".join(errs[:5]))
            stored = d["profile_id"]
            computed = compute_profile_id(d)
            if stored != computed:
                raise ProfileIdMismatch(stored, computed)
        self._doc = d
        self._bytes = canonical_json(d)

    # -- raw access -------------------------------------------------------
    @property
    def doc(self) -> dict[str, Any]:
        """A deep copy of the document."""
        return json.loads(self._bytes)

    def to_bytes(self) -> bytes:
        """Canonical JSON bytes (what ``profile.json`` holds)."""
        return self._bytes

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Profile) and other._bytes == self._bytes

    def __hash__(self) -> int:
        return hash(self._bytes)

    def __repr__(self) -> str:
        return f"Profile({self.profile_id}, snapshot={self.snapshot_id}, units={len(self.units)})"

    # -- fields -----------------------------------------------------------
    def __getitem__(self, k: str) -> Any:
        return self._doc[k]

    profile_id = property(lambda s: s._doc["profile_id"])
    snapshot_id = property(lambda s: s._doc["snapshot_id"])
    bundle_id = property(lambda s: s._doc["bundle_id"])
    contract_id = property(lambda s: s._doc["contract_id"])
    runtime_identity_id = property(lambda s: s._doc["runtime_identity_id"])
    collection = property(lambda s: s._doc["collection"])
    phases = property(lambda s: s._doc["phases"])
    units = property(lambda s: s._doc["units"])
    edges = property(lambda s: s._doc["edges"])
    profile_only = property(lambda s: s._doc["profile_only"])
    transient = property(lambda s: s._doc["transient"])
    domain = property(lambda s: s._doc["domain"])
    external_edges = property(lambda s: s._doc.get("x_external_edges", []))
    runtime = property(lambda s: s._doc.get("x_runtime"))
    sc_binding = property(lambda s: s._doc.get("x_sc_binding"))

    @property
    def runtime_signature(self) -> int | None:
        """The ``.sc`` runtime_signature the profiled run loaded (``x_sc_binding``,
        else ``x_runtime``), or None when the profile does not record it."""
        for src in (self.sc_binding, self.runtime):
            v = None if src is None else src.get("runtime_signature")
            if v is not None:
                return parse_hex(v) if isinstance(v, str) else int(v)
        return None

    @property
    def method(self) -> str:
        return self.collection["method"]

    @property
    def complete(self) -> bool:
        return bool(self.collection["complete"])

    def phase_id(self, name_or_id: str | int) -> int | None:
        for p in self.phases:
            if p["name"] == name_or_id or p["id"] == name_or_id:
                return p["id"]
        return None

    def phase_reached(self, pid: int) -> bool:
        return any(p["id"] == pid and p["reached"] for p in self.phases)

    def unit_map(self) -> dict[str, Mapping[str, Any]]:
        return {u["unit"]: u for u in self.units}

    def total_entries(self, phase: int | None = None) -> int:
        """Σ entries over units, profile-only rows and transient rows."""
        tot = 0
        for u in self.units:
            for p, c in u["phase"].items():
                if phase is None or int(p) == phase:
                    tot += c["entries"]
        for po in self.profile_only:
            for p, c in po["phase"].items():
                if phase is None or int(p) == phase:
                    tot += c["entries"]
        if phase is None:
            tot += self.transient["entries"]
        else:
            tot += self.transient.get("x_phase", {}).get(str(phase), {}).get("entries", 0)
        return tot


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _phase_table(q: QprofFile) -> list[dict[str, Any]]:
    ph = q.phases
    if not ph:
        raise ProfileIngestError("phase_table", "no phase rows")
    names = set()
    reached_seen_gap = False
    for i, p in enumerate(ph):
        if p.id != i:
            raise ProfileIngestError("phase_table", f"phase row {i} has id {p.id}")
        if not p.name or len(p.name.encode()) > 15 or p.name in names:
            raise ProfileIngestError("phase_table", f"bad or duplicate phase name {p.name!r}")
        names.add(p.name)
        if p.reached not in (0, 1, 2):
            raise ProfileIngestError("phase_table", f"phase {p.name}: reached={p.reached}")
        if p.is_reached and reached_seen_gap:
            raise ProfileIngestError("phase_table", "reached phases are not a prefix")
        if not p.is_reached:
            reached_seen_gap = True
    if ph[0].name != "boot" or ph[0].marker_vaddr != 0:
        raise ProfileIngestError("phase_table", "phase 0 must be 'boot' with marker 0")
    out = []
    for p in ph:
        d: dict[str, Any] = {"id": p.id, "name": p.name,
                             "marker": None if p.id == 0 else hexaddr(p.marker_vaddr),
                             "reached": p.is_reached,
                             "match": None if p.match is None else p.match.value,
                             "first_dispatch_index": p.first_dispatch_index if p.is_reached
                             else None}
        out.append(d)
    return out


def _check_binding(q: QprofFile, snap: SnapshotUnits, staged: ScFile) -> None:
    h = q.header
    sh = snap.header
    if not h.warm:
        raise ProfileIngestError("not_warm", "the profiled run did not load a snapshot "
                                 "(loaded_record_count=0)")
    if h.code_address != sh.code_address:
        raise ProfileIngestError("code_address_mismatch",
                                 f"qprof 0x{h.code_address:x} vs snapshot 0x{sh.code_address:x}")
    if h.runtime_signature != sh.runtime_signature:
        raise ProfileIngestError("runtime_signature_mismatch",
                                 f"qprof 0x{h.runtime_signature:x} vs snapshot "
                                 f"0x{sh.runtime_signature:x}")
    if (h.loaded_record_count != sh.record_count or h.loaded_header_checksum != sh.checksum
            or h.loaded_max_end != snap.max_end):
        raise ProfileIngestError(
            "snapshot_mismatch",
            f"qprof loaded count={h.loaded_record_count} checksum=0x{h.loaded_header_checksum:x} "
            f"max_end=0x{h.loaded_max_end:x}; snapshot count={sh.record_count} "
            f"checksum=0x{sh.checksum:x} max_end=0x{snap.max_end:x}")
    if not staged.loadable:
        raise ProfileIngestError("staged_mismatch",
                                 f"staged .sc not loadable: {', '.join(staged.header_problems)}")
    st = staged.header
    for f in ("magic", "version", "header_size", "code_size", "code_address", "prologue_end",
              "runtime_signature", "host_page_size", "target_page_bits", "tb_size", "reserved"):
        if getattr(st, f) != getattr(sh, f):
            raise ProfileIngestError("staged_mismatch", f"staged header {f} differs")
    if staged.count < h.loaded_record_count:
        raise ProfileIngestError("staged_mismatch",
                                 f"staged .sc has {staged.count} records < loaded "
                                 f"{h.loaded_record_count}")
    staged_valid = set(staged.valid_slots)
    for u in snap.units:
        if u.slot >= h.loaded_record_count or u.slot not in staged_valid:
            raise ProfileIngestError("staged_mismatch", f"snapshot slot {u.slot} not valid in "
                                     "the staged .sc")
        if u.record_bytes is not None and staged.record_bytes[u.slot] != u.record_bytes:
            raise ProfileIngestError("staged_mismatch", f"slot {u.slot}: record bytes differ")
        if u.body is not None and staged.bodies[u.slot] != u.body:
            raise ProfileIngestError("staged_mismatch", f"slot {u.slot}: arena bytes differ")
    snap_slots = {u.slot for u in snap.units}
    extra = sorted(i for i in staged_valid if i < h.loaded_record_count and i not in snap_slots)
    if extra:
        raise ProfileIngestError("staged_mismatch",
                                 f"staged loaded slots {extra[:5]} are not snapshot units")


def ingest(qprof: QprofFile | str | os.PathLike, staged_sc: ScFile | str | os.PathLike,
           snapshot: Any, *, bundle_id: str, contract_id: str,
           runtime_identity: Mapping[str, Any] | str, run_id: str | None = None,
           inputs: Sequence[Mapping[str, Any]] = (), time_dilation: float | None = None,
           domain_kind: str = "snapshot_units", require_flow: bool = True) -> Profile:
    """Join one ``.qprof`` to its snapshot and build a Profile v1 (§3.6-3.8).

    ``staged_sc`` is the run directory's ``.sc`` **after** the run. ``snapshot``
    is the profiled snapshot or artifact (anything :meth:`SnapshotUnits.from_any`
    accepts). ``runtime_identity`` is the RuntimeIdentity document (its id is
    computed and the compatibility fields are embedded as ``x_runtime``) or a
    ``sha256:`` id. ``run_id`` defaults to ``"qprof-<first 16 hex of the file
    sha256>"``. Raises :class:`ProfileIngestError` (stable ``reason``) when the
    join fails; never returns a partially joined profile.

    With ``require_flow`` (default) a profile that would be ``complete`` (clean
    exit, no overflow) but breaks the §3.4 flow-conservation identities is
    refused with ``flow_conservation``: that is a producer defect, and its
    counts must not be published as exact. A broken flow after an overflow or
    an unclean exit is expected and does not refuse (the counts are already
    ``lower_bound``).
    """
    q = qprof if isinstance(qprof, QprofFile) else read_qprof(qprof)
    staged = staged_sc if isinstance(staged_sc, ScFile) else read_sc(staged_sc)
    snap = SnapshotUnits.from_any(snapshot)
    for name, v in (("bundle_id", bundle_id), ("contract_id", contract_id)):
        if not is_sha256_id(v):
            raise ValueError(f"{name} must be a sha256: id, got {v!r}")
    if isinstance(runtime_identity, str):
        if not is_sha256_id(runtime_identity):
            raise ValueError("runtime_identity must be a document or a sha256: id")
        rid, x_runtime = runtime_identity, None
    else:
        rid = runtime_identity_id(runtime_identity)
        x_runtime = {k: runtime_identity.get(k) for k in RUNTIME_COMPAT_FIELDS}
    if domain_kind not in ("snapshot_units", "artifact_units"):
        raise ValueError(f"domain_kind {domain_kind!r}")

    _check_binding(q, snap, staged)
    phases = _phase_table(q)
    n_ph = len(phases)
    reached = [p["reached"] for p in phases]
    h = q.header

    status = CountStatus.EXACT.value
    lb_reasons = []
    if not h.clean_exit:
        lb_reasons.append("unclean_exit")
    if h.tb_overflow:
        lb_reasons.append("tb_overflow")
    if h.edge_overflow:
        lb_reasons.append("edge_overflow")
    if lb_reasons:
        status = CountStatus.LOWER_BOUND.value
    reason = ",".join(sorted(lb_reasons)) or None

    loaded = snap.by_tb_offset()
    appended = {}
    for i in staged.valid_slots:
        if i >= h.loaded_record_count:
            r = staged.records[i]
            appended.setdefault(r.tb_offset, (i, r))  # first record at an offset

    # -- rows -> nodes ----------------------------------------------------
    node_of: dict[int, str] = {}
    unit_rows: dict[str, Any] = {}
    po_rows: list[tuple[int, int, Any, Any]] = []  # (tb_offset, slot, record, row)
    transient_phase = [_zero_counts() for _ in range(n_ph)]
    transient_rows = 0
    seen = set()
    for t in q.tbs:
        if t.tb_offset in seen:
            raise ProfileIngestError("duplicate_row", f"two TB rows at 0x{t.tb_offset:x}")
        seen.add(t.tb_offset)
        for i, c in enumerate(t.phases):
            if not reached[i] and (c.entries or c.aborted or c.longjmp or c.lookup_hits):
                raise ProfileIngestError("counts_in_unreached_phase",
                                         f"row 0x{t.tb_offset:x} has counts in phase {i}")
        tkey = (t.page_addr0, t.page_addr1, t.flags, t.cs_base, t.cflags, t.size, t.icount)
        if t.transient:
            node_of[t.tb_offset] = TRANSIENT_NODE
            transient_rows += 1
            for i, c in enumerate(t.phases):
                _add_counts(transient_phase[i], _counts_json(c))
            continue
        u = loaded.get(t.tb_offset)
        if u is not None:
            ukey = (u.key.page_addr0, u.key.page_addr1_raw, u.key.flags, u.key.cs_base,
                    u.key.cflags, u.guest_size, u.icount)
            if ukey != tkey:
                raise ProfileIngestError("row_key_mismatch",
                                         f"row 0x{t.tb_offset:x} vs slot {u.slot}")
            node_of[t.tb_offset] = u.uid
            unit_rows[u.uid] = t
            continue
        a = appended.get(t.tb_offset)
        if a is not None and t.tb_offset >= h.loaded_max_end:
            slot, r = a
            rkey = (r.page_addr0, r.page_addr1, r.flags, r.cs_base, r.cflags, r.guest_size,
                    r.icount)
            if rkey != tkey:
                raise ProfileIngestError("row_key_mismatch",
                                         f"row 0x{t.tb_offset:x} vs appended slot {slot}")
            po_rows.append((t.tb_offset, slot, r, t))
            continue
        raise ProfileIngestError("unjoined_row",
                                 f"TB row 0x{t.tb_offset:x} (page_addr0 0x{t.page_addr0:x}) "
                                 "matches no loaded or appended record")
    po_rows.sort(key=lambda x: x[0])
    for i, (off, _slot, _r, _t) in enumerate(po_rows):
        node_of[off] = f"po:{i}"

    # -- units (every domain unit, zeros included) -------------------------
    units_json = []
    for u in snap.units:
        t = unit_rows.get(u.uid)
        ph = {}
        for i in range(n_ph):
            if reached[i]:
                ph[str(i)] = _counts_json(t.phases[i]) if t is not None else _zero_counts()
        d: dict[str, Any] = {"unit": u.uid,
                             "vaddrs": [hexaddr(t.first_vaddr)] if t is not None else [],
                             "status": status, "phase": ph}
        if reason:
            d["reason"] = reason
        units_json.append(d)

    po_json = []
    for (off, slot, r, t) in po_rows:
        po_json.append({
            "key": _key_json(r.key), "guest_hash": hexaddr(r.guest_hash),
            "size": r.guest_size, "icount": r.icount, "vaddrs": [hexaddr(t.first_vaddr)],
            "phase": {str(i): _counts_json(t.phases[i]) for i in range(n_ph) if reached[i]},
            "x_tb_offset": hexaddr(off), "x_slot": slot, "x_run": 0,
        })

    # -- edges -----------------------------------------------------------
    unit_set = set(snap.unit_ids)
    edges: dict[tuple, int] = defaultdict(int)
    ext: dict[tuple, int] = defaultdict(int)
    dropped = 0
    for e in q.edges:
        kind = _kind_name(e.kind)
        if e.phase >= n_ph:
            raise ProfileIngestError("edge_phase", f"edge phase {e.phase} >= {n_ph}")
        if e.flags & ~0x7:
            raise ProfileIngestError("edge_flags", f"edge flags 0x{e.flags:x}")
        if (e.src == NONE_U64) != (e.kind == QprofEdgeKind.FIRST):
            raise ProfileIngestError("edge_first", "src NONE must go with kind FIRST")
        src = None if e.src == NONE_U64 else node_of.get(e.src)
        dst = node_of.get(e.dst)
        if dst is None or (e.src != NONE_U64 and src is None):
            if h.overflow:  # the row was dropped by the producer
                dropped += 1
                continue
            raise ProfileIngestError("edge_unknown_row",
                                     f"edge 0x{e.src:x}->0x{e.dst:x} references no TB row")
        k = (src, dst, kind, e.flags, e.phase)
        if (src is None or src in unit_set) and dst in unit_set:
            edges[k] += e.count
        else:
            ext[k] += e.count

    def edge_rows(src_dst_counts: Mapping[tuple, int], with_status: bool) -> list[dict]:
        out = []
        for (src, dst, kind, flags, phase), cnt in src_dst_counts.items():
            d: dict[str, Any] = {"src": src, "dst": dst, "kind": kind,
                                 "flags": _flag_names(flags), "phase": phase, "count": cnt}
            if with_status:
                d["status"] = status
                if reason:
                    d["reason"] = reason
                if kind in INDIRECT_KINDS:
                    d["dest_set_complete"] = False
            out.append(d)
        out.sort(key=lambda d: (d["phase"], _node_sort_key(d["src"]), d["dst"], d["kind"],
                                d["flags"]))
        return out

    transient_entries = sum(c["entries"] for c in transient_phase)
    run: dict[str, Any] = {
        "run_id": run_id or f"qprof-{q.sha256[:16]}",
        "clean_exit": h.clean_exit,
        "total_dispatches": h.total_dispatches,
        "transient_entries": transient_entries,
        "x_qprof_sha256": "sha256:" + q.sha256,
        "x_flags": hexaddr(h.flags),
        "x_appended_records": staged.count - h.loaded_record_count,
        "x_profile_only_rows": len(po_rows),
        "x_dropped_edges": dropped,
    }
    doc: dict[str, Any] = {
        "schema": SCHEMA, "version": VERSION, "profile_id": "",
        "snapshot_id": snap.snapshot_id, "bundle_id": bundle_id, "contract_id": contract_id,
        "runtime_identity_id": rid,
        "collection": {"method": METHOD, "complete": not lb_reasons, "overflow": h.overflow,
                       "chain_disabled": True, "time_dilation": time_dilation,
                       "repetitions": 1, "inputs": [dict(i) for i in inputs], "runs": [run]},
        "phases": phases,
        "domain": {"kind": domain_kind, "count": len(snap.units)},
        "units": units_json,
        "edges": edge_rows(edges, True),
        "profile_only": po_json,
        "transient": {"entries": transient_entries, "x_rows": transient_rows,
                      "x_phase": {str(i): transient_phase[i] for i in range(n_ph)
                                  if reached[i]}},
        "x_external_edges": edge_rows(ext, False),
        "x_sc_binding": {"code_address": hexaddr(h.code_address),
                         "runtime_signature": hexaddr(h.runtime_signature)},
    }
    if x_runtime is not None:
        doc["x_runtime"] = x_runtime
    prof = build_profile_doc(doc)
    if require_flow and prof.complete:
        f = check_flow(prof)
        if not f.ok:
            raise ProfileIngestError("flow_conservation",
                                     f"{len(f.violations)} violation(s): "
                                     + "; ".join(f.violations[:3]))
    return prof


def build_profile_doc(doc: Mapping[str, Any]) -> Profile:
    """Set ``profile_id`` from the content and validate (schema + id)."""
    d = dict(doc)
    d.pop("profile_id", None)
    d["profile_id"] = compute_profile_id(d)
    return Profile(d)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def write_profile(path: str | os.PathLike, profile: Profile, *, gzip_: bool | None = None) -> str:
    """Write canonical JSON (gzip when ``path`` ends in ``.gz`` or ``gzip_``) atomically."""
    path = os.fspath(path)
    data = profile.to_bytes()
    if gzip_ is None:
        gzip_ = path.endswith(".gz")
    if gzip_:
        data = gzip.compress(data, mtime=0)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".profile-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def read_profile(path: str | os.PathLike, *, require_canonical: bool = True) -> Profile:
    """Read ``profile.json`` (or ``.json.gz``); validates the schema and recomputes
    ``profile_id`` (:class:`ProfileIdMismatch`). With ``require_canonical`` the
    bytes must be canonical JSON."""
    path = os.fspath(path)
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    try:
        doc = json.loads(data)
    except json.JSONDecodeError as e:
        raise ProfileFormatError("json", str(e)) from e
    p = Profile(doc)
    if require_canonical and p.to_bytes() != data:
        raise ProfileFormatError("not_canonical", path)
    return p


def profile_dir(store: str | os.PathLike, profile_id: str) -> str:
    """``<store>/profiles/<hex>``: the hex part of the id, the same naming as the
    snapshot store (:func:`scopt.model.cache_snapshot.snapshot_dir_name`)."""
    if not is_sha256_id(profile_id):
        raise ValueError(f"not a profile id: {profile_id!r}")
    return os.path.join(os.fspath(store), "profiles", profile_id.split(":", 1)[1])


def publish_profile(store: str | os.PathLike, profile: Profile) -> str:
    """Write ``<store>/profiles/<hex>/profile.json`` once (content-addressed;
    an existing file must be byte-equal) and make it read-only. Returns the path."""
    d = profile_dir(store, profile.profile_id)
    path = os.path.join(d, "profile.json")
    if os.path.exists(path):
        with open(path, "rb") as f:
            if f.read() != profile.to_bytes():
                raise ProfileFormatError("store_conflict", f"{path} exists with other bytes")
        return path
    os.makedirs(d, exist_ok=True)
    write_profile(path, profile, gzip_=False)
    os.chmod(path, 0o444)
    return path


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _phase_sig(p: Profile) -> list[tuple]:
    return [(ph["id"], ph["name"], ph["marker"]) for ph in p.phases]


def merge(profiles: Iterable[Profile]) -> Profile:
    """Merge profiles of the same snapshot (§3.8).

    Requires an identical method, phase list (id, name, marker), snapshot_id and
    bundle_id -- and, because a Profile has one field for each, contract_id,
    runtime_identity_id and domain. Counts add, status is the minimum, inputs and
    runs are concatenated. A phase is ``reached`` only if every run reached it
    (``x_reached_runs`` says how many did; the counts of those runs are kept
    for flow conservation, and ProfileView reports the phase as
    ``unknown/phase_not_reached``). The same run may not appear twice
    (``merge_duplicate_run``: equal ``x_qprof_sha256`` or ``run_id``, which
    includes merging a profile with itself). PROFILE_ONLY rows are never merged:
    the lists are concatenated (``x_run`` renumbered) and never folded into
    units or into each other. The result does not depend on the input order.
    """
    ps = sorted(profiles, key=lambda p: p.profile_id)
    if not ps:
        raise ProfileMergeError("merge_empty", "no profiles")
    if len(ps) == 1:
        return ps[0]
    a = ps[0]
    for b in ps[1:]:
        for what, va, vb in (("method", a.method, b.method),
                             ("phases", _phase_sig(a), _phase_sig(b)),
                             ("snapshot_id", a.snapshot_id, b.snapshot_id),
                             ("bundle_id", a.bundle_id, b.bundle_id),
                             ("contract_id", a.contract_id, b.contract_id),
                             ("runtime_identity_id", a.runtime_identity_id,
                              b.runtime_identity_id),
                             ("domain", a.domain, b.domain),
                             ("x_runtime", a.runtime, b.runtime),
                             ("x_sc_binding", a.sc_binding, b.sc_binding)):
            if va != vb:
                raise ProfileMergeError(f"merge_incompatible:{what}", f"{va!r} != {vb!r}")
    seen_runs: dict[str, str] = {}
    for p in ps:
        for r in p.collection["runs"]:
            for k in ("x_qprof_sha256", "run_id"):
                v = r.get(k)
                if v is None:
                    continue
                if (k, v) in seen_runs:
                    raise ProfileMergeError("merge_duplicate_run",
                                            f"{k} {v} appears twice ({seen_runs[(k, v)]} and "
                                            f"{p.profile_id})")
                seen_runs[(k, v)] = p.profile_id

    colls = [p.collection for p in ps]
    tds = [c["time_dilation"] for c in colls]
    runs: list[dict] = []
    run_base: list[int] = []
    for c in colls:
        run_base.append(len(runs))
        runs.extend(c["runs"])
    collection = {
        "method": a.method,
        "complete": all(c["complete"] for c in colls),
        "overflow": any(c["overflow"] for c in colls),
        "chain_disabled": all(c["chain_disabled"] for c in colls),
        "time_dilation": None if any(t is None for t in tds) else sum(tds) / len(tds),
        "repetitions": sum(c["repetitions"] for c in colls),
        "inputs": [i for c in colls for i in c["inputs"]],
        "runs": runs,
    }

    phases = []
    for i, ph0 in enumerate(a.phases):
        allp = [p.phases[i] for p in ps]
        n_reached = sum(p.get("x_reached_runs", 1 if p["reached"] else 0) for p in allp)
        n_runs = sum(len(p.collection["runs"]) for p in ps)
        reached = all(p["reached"] for p in allp)
        matches = {p.get("match") for p in allp}
        fdis = {p.get("first_dispatch_index") for p in allp}
        phases.append({
            "id": ph0["id"], "name": ph0["name"], "marker": ph0["marker"], "reached": reached,
            "match": (matches.pop() if len(matches) == 1 else "contained") if reached else None,
            "first_dispatch_index": (fdis.pop() if len(fdis) == 1 else None) if reached else None,
            "x_reached_runs": n_reached if n_reached <= n_runs else n_runs,
        })

    # units
    umap: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for p in ps:
        for u in p.units:
            m = umap.get(u["unit"])
            if m is None:
                m = umap[u["unit"]] = {"unit": u["unit"], "vaddrs": set(), "status": u["status"],
                                       "reasons": set(), "phase": {}}
                order.append(u["unit"])
            m["vaddrs"].update(u["vaddrs"])
            m["status"] = status_min(m["status"], u["status"])
            if u.get("reason"):
                m["reasons"].update(u["reason"].split(","))
            for ph, c in u["phase"].items():
                _add_counts(m["phase"].setdefault(ph, _zero_counts()), c)
    if sorted(order) != sorted(a.unit_map()):
        raise ProfileMergeError("merge_incompatible:domain", "unit lists differ")
    units = []
    for uid in [u["unit"] for u in a.units]:
        m = umap[uid]
        d = {"unit": uid, "vaddrs": sorted(m["vaddrs"], key=parse_hex), "status": m["status"],
             "phase": dict(sorted(m["phase"].items(), key=lambda kv: int(kv[0])))}
        if m["reasons"]:
            d["reason"] = ",".join(sorted(m["reasons"]))
        units.append(d)

    # profile-only rows: concatenated, never merged
    po: list[dict] = []
    po_base: list[int] = []
    for k, p in enumerate(ps):
        po_base.append(len(po))
        for row in p.profile_only:
            r = dict(row)
            r["x_run"] = run_base[k] + int(row.get("x_run", 0))
            po.append(r)

    def remap(n: str | None, k: int) -> str | None:
        if n is not None and n.startswith("po:"):
            return f"po:{po_base[k] + int(n[3:])}"
        return n

    def merge_edges(attr: str, with_status: bool) -> list[dict]:
        acc: dict[tuple, dict] = {}
        for k, p in enumerate(ps):
            for e in (p.edges if attr == "edges" else p.external_edges):
                src, dst = remap(e["src"], k), remap(e["dst"], k)
                key = (src, dst, e["kind"], tuple(e["flags"]), e["phase"])
                m = acc.get(key)
                if m is None:
                    m = acc[key] = {"src": src, "dst": dst, "kind": e["kind"],
                                    "flags": list(e["flags"]), "phase": e["phase"], "count": 0}
                    if with_status:
                        m["status"] = e["status"]
                        m["_reasons"] = set()
                        if "dest_set_complete" in e:
                            m["dest_set_complete"] = e["dest_set_complete"]
                m["count"] += e["count"]
                if with_status:
                    m["status"] = status_min(m["status"], e["status"])
                    if e.get("reason"):
                        m["_reasons"].update(e["reason"].split(","))
                    if "dest_set_complete" in e:
                        m["dest_set_complete"] = m.get("dest_set_complete", True) and \
                            e["dest_set_complete"]
        out = []
        for m in acc.values():
            rs = m.pop("_reasons", None)
            if rs:
                m["reason"] = ",".join(sorted(rs))
            out.append(m)
        out.sort(key=lambda d: (d["phase"], _node_sort_key(d["src"]), d["dst"], d["kind"],
                                d["flags"]))
        return out

    tph: dict[str, dict[str, int]] = {}
    for p in ps:
        for ph, c in p.transient.get("x_phase", {}).items():
            _add_counts(tph.setdefault(ph, _zero_counts()), c)
    transient = {"entries": sum(p.transient["entries"] for p in ps),
                 "x_rows": sum(p.transient.get("x_rows", 0) for p in ps),
                 "x_phase": dict(sorted(tph.items(), key=lambda kv: int(kv[0])))}

    doc: dict[str, Any] = {
        "schema": SCHEMA, "version": VERSION,
        "snapshot_id": a.snapshot_id, "bundle_id": a.bundle_id, "contract_id": a.contract_id,
        "runtime_identity_id": a.runtime_identity_id, "collection": collection,
        "phases": phases, "domain": a.domain, "units": units,
        "edges": merge_edges("edges", True), "profile_only": po, "transient": transient,
        "x_external_edges": merge_edges("x_external_edges", False),
    }
    if a.runtime is not None:
        doc["x_runtime"] = a.runtime
    if a.sc_binding is not None:
        doc["x_sc_binding"] = a.sc_binding
    return build_profile_doc(doc)


# ---------------------------------------------------------------------------
# Flow conservation (§3.4)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FlowReport:
    """``ok`` means: per (node, phase) Σ in-edges == entries; per node (summed
    over phases, because an edge carries its destination's phase) Σ out-edges ==
    entries − [node was a run's last TB]; and the FIRST edges equal the number of
    runs that dispatched anything, as do the out-flow deficits. ``exact`` is the
    profile's completeness: a broken flow is expected after an overflow or an
    unclean exit, and is a defect otherwise."""

    ok: bool
    exact: bool
    violations: tuple[str, ...]
    nodes: int
    runs: int
    first_edges: int
    last_nodes: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "exact": self.exact, "violations": list(self.violations),
                "nodes": self.nodes, "runs": self.runs, "first_edges": self.first_edges,
                "last_nodes": list(self.last_nodes)}


def check_flow(profile: Profile, *, max_violations: int = 50) -> FlowReport:
    entries: dict[str, dict[int, int]] = {}
    for u in profile.units:
        entries[u["unit"]] = {int(k): c["entries"] for k, c in u["phase"].items()}
    for i, po in enumerate(profile.profile_only):
        entries[f"po:{i}"] = {int(k): c["entries"] for k, c in po["phase"].items()}
    t = profile.transient
    t_phase = t.get("x_phase")
    if t_phase is not None:
        entries[TRANSIENT_NODE] = {int(k): c["entries"] for k, c in t_phase.items()}
    elif t["entries"]:
        entries[TRANSIENT_NODE] = {}
    viol: list[str] = []
    inn: dict[tuple[str, int], int] = defaultdict(int)
    out: dict[str, int] = defaultdict(int)
    first = 0
    for e in list(profile.edges) + list(profile.external_edges):
        for end in (e["src"], e["dst"]):
            if end is not None and end not in entries:
                viol.append(f"edge endpoint {end} is not a node")
        if e["src"] is None:
            if e["kind"] != "first":
                viol.append(f"edge with src NONE has kind {e['kind']}")
            first += e["count"]
        else:
            out[e["src"]] += e["count"]
        inn[(e["dst"], e["phase"])] += e["count"]
    in_phases: dict[str, set[int]] = defaultdict(set)
    for (m, p) in inn:
        in_phases[m].add(p)
    for n, per in entries.items():
        for p in sorted(set(per) | in_phases.get(n, set())):
            if n == TRANSIENT_NODE and t_phase is None:
                continue
            if inn.get((n, p), 0) != per.get(p, 0):
                viol.append(f"in-flow {n} phase {p}: in-edges {inn.get((n, p), 0)} != "
                            f"entries {per.get(p, 0)}")
    if t_phase is None and TRANSIENT_NODE in entries:
        tin = sum(c for (m, _p), c in inn.items() if m == TRANSIENT_NODE)
        if tin != t["entries"]:
            viol.append(f"in-flow transient: in-edges {tin} != entries {t['entries']}")
    runs = profile.collection["runs"]
    n_runs = sum(1 for r in runs if r["total_dispatches"] > 0)
    last = []
    deficit_total = 0
    for n, per in entries.items():
        tot = t["entries"] if (n == TRANSIENT_NODE and t_phase is None) else sum(per.values())
        d = tot - out.get(n, 0)
        if d < 0:
            viol.append(f"out-flow {n}: out-edges {out.get(n, 0)} > entries {tot}")
        elif d > 0:
            last.append(n)
            deficit_total += d
    if deficit_total != n_runs:
        viol.append(f"out-flow: Σ(entries − out-edges) = {deficit_total}, expected one last "
                    f"TB per run ({n_runs})")
    if first != n_runs:
        viol.append(f"first edges {first} != runs with dispatches {n_runs}")
    total = sum(r["total_dispatches"] for r in runs)
    have = profile.total_entries()
    if total != have:
        viol.append(f"Σ entries {have} != Σ total_dispatches {total}")
    return FlowReport(not viol, profile.complete, tuple(viol[:max_violations]), len(entries),
                      len(runs), first, tuple(sorted(last)))


# ---------------------------------------------------------------------------
# Compatibility and ProfileView (§3.8)
# ---------------------------------------------------------------------------

def _load_json(path: str) -> dict | None:
    try:
        with open(path, "rb") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class ProfileTarget:
    """What a profile is applied to: a snapshot or an artifact, by UnitId."""

    snapshot_id: str
    unit_ids: tuple[str, ...]
    bundle_id: str | None = None
    contract_id: str | None = None
    runtime_identity_id: str | None = None
    runtime_identity: Mapping[str, Any] | None = None
    kind: str = "snapshot"  # or "artifact"
    input_snapshot_ids: tuple[str, ...] = ()
    runtime_signature: int | None = None  # the target .sc header's, when read from a .sc

    @classmethod
    def from_any(cls, obj: Any, **overrides: Any) -> "ProfileTarget":
        """From a :class:`ProfileTarget`, :class:`SnapshotUnits`, ``ScFile``,
        ``.sc`` path, store snapshot directory (``cache.sc`` plus optional
        ``snapshot.json``/``bundle.json``/``runtime_identity.json``), artifact
        directory (``cache.sc.scopt.json`` sidecar) or SnapshotView-like object
        (``bundle``/``runtime`` attributes are used when they carry ids).
        ``overrides`` (bundle_id, contract_id, runtime_identity,
        runtime_identity_id, kind, input_snapshot_ids, runtime_signature) win.
        ``runtime_signature`` is taken from the target ``.sc`` header."""
        if isinstance(obj, ProfileTarget):
            base = obj
        else:
            info: dict[str, Any] = {}
            src = obj
            if isinstance(obj, (str, os.PathLike)) and os.path.isdir(os.fspath(obj)):
                d = os.fspath(obj)
                side = _load_json(os.path.join(d, "cache.sc.scopt.json"))
                snapj = _load_json(os.path.join(d, "snapshot.json"))
                bundle = _load_json(os.path.join(d, "bundle.json"))
                rt = _load_json(os.path.join(d, "runtime_identity.json"))
                for j in (snapj, side):
                    if j:
                        for k in ("bundle_id", "runtime_identity_id"):
                            if j.get(k):
                                info[k] = j[k]
                if side:
                    info["kind"] = "artifact"
                    if side.get("input_snapshot_id"):
                        info["input_snapshot_ids"] = (side["input_snapshot_id"],)
                if bundle:
                    info.setdefault("bundle_id", bundle.get("bundle_id"))
                    info["contract_id"] = bundle.get("contract_id")
                if rt:
                    info["runtime_identity"] = rt
                    info.setdefault("runtime_identity_id", rt.get("runtime_identity_id"))
            elif hasattr(obj, "bundle") or hasattr(obj, "runtime"):
                def get(o: Any, k: str) -> Any:
                    return o.get(k) if isinstance(o, Mapping) else getattr(o, k, None)

                b = getattr(obj, "bundle", None)
                r = getattr(obj, "runtime", None)
                if b is not None:
                    info["bundle_id"] = get(b, "bundle_id")
                    info["contract_id"] = get(b, "contract_id")
                if r is not None:
                    info["runtime_identity"] = (dict(r) if isinstance(r, Mapping) else
                                                {k: get(r, k) for k in RUNTIME_COMPAT_FIELDS})
                    info["runtime_identity_id"] = get(r, "runtime_identity_id")
            su = SnapshotUnits.from_any(src)
            info.setdefault("runtime_signature", getattr(su.header, "runtime_signature", None))
            base = cls(su.snapshot_id, su.unit_ids,
                       **{k: v for k, v in info.items() if v is not None})
        if overrides:
            ov = {k: v for k, v in overrides.items() if v is not None}
            rt = ov.get("runtime_identity")
            if rt is not None and "runtime_identity_id" not in ov:
                ov["runtime_identity_id"] = runtime_identity_id(rt)
            base = ProfileTarget(**{**base.__dict__, **ov})
        return base


def _runtime_check(profile: Profile, target: ProfileTarget,
                   runtime_identities: Mapping[str, Mapping[str, Any]] | None) -> None:
    # The .sc header field is checked first and independently of the ids: an
    # equal runtime_identity_id must not let a profile onto a .sc written by
    # another runtime (fail closed whenever both sides record it).
    psig, tsig = profile.runtime_signature, target.runtime_signature
    if psig is not None and tsig is not None and psig != tsig:
        raise ProfileIncompatible("runtime", f"different runtime_signature: profile 0x{psig:x} "
                                  f"vs target .sc 0x{tsig:x}")
    if target.runtime_identity_id is not None and \
            target.runtime_identity_id == profile.runtime_identity_id:
        return
    prof_rt = profile.runtime
    if prof_rt is None and runtime_identities:
        doc = runtime_identities.get(profile.runtime_identity_id)
        if doc is not None:
            prof_rt = {k: doc.get(k) for k in RUNTIME_COMPAT_FIELDS}
    tgt_rt = target.runtime_identity
    if prof_rt is None or tgt_rt is None:
        raise ProfileIncompatible(
            "runtime", f"runtime_identity_id {profile.runtime_identity_id} != "
            f"{target.runtime_identity_id} and the fields cannot be compared")
    diff = [k for k in RUNTIME_COMPAT_FIELDS if prof_rt.get(k) != tgt_rt.get(k)]
    if diff:
        raise ProfileIncompatible("runtime", "different " + ", ".join(diff))


class ProfileView:
    """A profile applied to a snapshot or artifact, by UnitId (§3.8).

    Use :meth:`open` (compatibility checks, coverage) or :meth:`not_collected`.
    Every count comes back as :class:`Count` with ``exact``, ``lower_bound`` or
    ``unknown`` (``not_in_domain``, ``phase_not_reached``, ``not_collected``).
    """

    def __init__(self, profile: Profile | None, target: ProfileTarget, *,
                 coverage: float | None, coverage_phase: str | None, same_snapshot: bool):
        self.profile = profile
        self.target = target
        self.coverage = coverage
        self.coverage_phase = coverage_phase
        self.same_snapshot = same_snapshot
        self._units = profile.unit_map() if profile is not None else {}
        tset = self._target_set = frozenset(target.unit_ids)
        self._edges: list[dict] = []
        if profile is not None:
            self._edges = [e for e in profile.edges
                           if (e["src"] is None or e["src"] in tset) and e["dst"] in tset]
        self._edge_index: dict[tuple, list[dict]] = defaultdict(list)
        for e in self._edges:
            self._edge_index[(e["src"], e["dst"], e["phase"])].append(e)

    # -- construction --------------------------------------------------------
    @classmethod
    def open(cls, profile: Profile, target: Any, *,
             min_profile_match: float | None = DEFAULT_MIN_PROFILE_MATCH,
             phase: str = "steady",
             runtime_identities: Mapping[str, Mapping[str, Any]] | None = None,
             **target_overrides: Any) -> "ProfileView":
        """Apply ``profile`` to ``target`` or raise :class:`ProfileIncompatible`.

        Hard rejects: a different ``bundle_id`` or ``contract_id``
        (an unknown one on the target side also rejects: fail closed); a
        different RuntimeIdentity in ``qemu_sha256``, ``plugin_sha256``,
        ``runtime_signature``, ``prologue_sha256`` or ``cpuinfo`` (equal
        ``runtime_identity_id`` passes without looking at fields; otherwise both
        documents are needed, from ``x_runtime``/``runtime_identities`` and the
        target). Same snapshot_id: applies directly. Otherwise (another
        snapshot, or an artifact) units join by UnitId and the share of the
        profile's ``phase`` entries (all reached phases if the profile has no
        such phase or did not reach it; ``coverage_phase`` is then None) that
        land on target units must be ``>= min_profile_match``
        (``profile_incompatible:coverage``). PROFILE_ONLY and transient entries
        are outside both sides of that ratio.
        """
        t = ProfileTarget.from_any(target, **target_overrides)
        if t.bundle_id != profile.bundle_id:
            raise ProfileIncompatible("bundle", f"profile {profile.bundle_id} vs target "
                                      f"{t.bundle_id}")
        if t.contract_id != profile.contract_id:
            raise ProfileIncompatible("contract", f"profile {profile.contract_id} vs target "
                                      f"{t.contract_id}")
        _runtime_check(profile, t, runtime_identities)
        # (``runtime_signature`` of the target .sc vs the profile's x_sc_binding
        # is part of the runtime check.)
        same = profile.snapshot_id == t.snapshot_id
        pid = profile.phase_id(phase)
        if pid is not None and not profile.phase_reached(pid):
            # A defined but unreached phase has no counts to measure the join
            # with (every count of it is unknown/phase_not_reached): measure the
            # coverage over the reached phases instead of reporting 1.0.
            pid = None
        reached_ids = {p["id"] for p in profile.phases if p["reached"]}
        tset = set(t.unit_ids)
        tot = hit = 0
        for u in profile.units:
            n = sum(c["entries"] for k, c in u["phase"].items()
                    if ((int(k) in reached_ids) if pid is None else int(k) == pid))
            tot += n
            if u["unit"] in tset:
                hit += n
        cov = 1.0 if tot == 0 else hit / tot
        if same and set(profile.unit_map()) != tset:
            raise ProfileIncompatible("coverage", "same snapshot_id but different unit sets",
                                      cov)
        if not same and min_profile_match is not None and cov < min_profile_match:
            raise ProfileIncompatible(
                "coverage", f"{hit}/{tot} {phase if pid is not None else 'all-phase'} entries "
                f"match target units ({cov:.4f} < {min_profile_match})", cov)
        return cls(profile, t, coverage=cov,
                   coverage_phase=phase if pid is not None else None, same_snapshot=same)

    @classmethod
    def not_collected(cls, target: Any, **target_overrides: Any) -> "ProfileView":
        """A view with no profile attached: every count is ``unknown/not_collected``."""
        return cls(None, ProfileTarget.from_any(target, **target_overrides), coverage=None,
                   coverage_phase=None, same_snapshot=False)

    # -- phases ------------------------------------------------------------
    @property
    def profile_id(self) -> str | None:
        return None if self.profile is None else self.profile.profile_id

    @property
    def via_input_snapshot(self) -> bool | None:
        """For an artifact target that lists its input snapshot(s): whether the
        profile was taken on one of them (the expected B2 case). None when the
        target lists none or there is no profile. Informational only: the join
        is by UnitId and the coverage threshold decides compatibility."""
        if self.profile is None or not self.target.input_snapshot_ids:
            return None
        return self.profile.snapshot_id in self.target.input_snapshot_ids

    @property
    def phase_names(self) -> tuple[str, ...]:
        return () if self.profile is None else tuple(p["name"] for p in self.profile.phases)

    def _phase(self, phase: str | int) -> tuple[int | None, bool]:
        if self.profile is None:
            return None, False
        pid = self.profile.phase_id(phase)
        if pid is None:
            return None, False
        return pid, self.profile.phase_reached(pid)

    # -- unit counts ---------------------------------------------------------
    def count(self, uid: str, phase: str | int, field: str = "entries") -> Count:
        """``field`` is entries, aborted, longjmp, lookup_hits or executions."""
        if self.profile is None:
            return _unknown(UnknownReason.NOT_COLLECTED.value)
        u = self._units.get(uid)
        if uid not in self._target_set or u is None:
            return _unknown(UnknownReason.NOT_IN_DOMAIN.value)
        pid, reached = self._phase(phase)
        if pid is None:
            return _unknown(UnknownReason.NOT_COLLECTED.value)
        if not reached:
            return _unknown(UnknownReason.PHASE_NOT_REACHED.value)
        c = u["phase"].get(str(pid))
        if c is None:  # defensive: a reached phase is always present after ingest
            return _unknown(UnknownReason.PHASE_NOT_REACHED.value)
        if field == "executions":
            v = c["entries"] - c["aborted"]
        else:
            v = c.get(field, 0)
        return Count(v, u["status"], u.get("reason"))

    def counts(self, phase: str | int, field: str = "entries") -> Mapping[str, Count]:
        """``{UnitId: Count}`` for every target unit, in target slot order."""
        return MappingProxyType({uid: self.count(uid, phase, field)
                                 for uid in self.target.unit_ids})

    def phase(self, phase: str | int) -> Mapping[str, Count]:
        """Entries per unit for ``phase`` (the §5.10 ``.phase(name)`` accessor)."""
        return self.counts(phase, "entries")

    def executions(self, phase: str | int) -> Mapping[str, Count]:
        return self.counts(phase, "executions")

    def unit_status(self, uid: str) -> Count:
        """Status of the unit itself (no phase), value = Σ entries over reached phases."""
        if self.profile is None:
            return _unknown(UnknownReason.NOT_COLLECTED.value)
        u = self._units.get(uid)
        if uid not in self._target_set or u is None:
            return _unknown(UnknownReason.NOT_IN_DOMAIN.value)
        v = sum(c["entries"] for k, c in u["phase"].items()
                if self.profile.phase_reached(int(k)))
        return Count(v, u["status"], u.get("reason"))

    def vaddrs(self, uid: str) -> tuple[int, ...]:
        u = self._units.get(uid)
        return () if u is None else tuple(parse_hex(v) for v in u["vaddrs"])

    # -- edges ---------------------------------------------------------------
    def edges(self, phase: str | int | None = None) -> tuple[Mapping[str, Any], ...]:
        """Observed edges whose endpoints are target units (src None = first)."""
        if self.profile is None:
            return ()
        if phase is None:
            return tuple(MappingProxyType(e) for e in self._edges)
        pid, _ = self._phase(phase)
        return tuple(MappingProxyType(e) for e in self._edges if e["phase"] == pid)

    def edge_count(self, src: str | None, dst: str, phase: str | int,
                   kinds: Iterable[str] | None = None) -> Count:
        """Σ count of observed ``src -> dst`` edges in ``phase`` (optionally only
        ``kinds``). An absent edge is an exact 0 only between two exact units in
        a reached phase; otherwise it is unknown."""
        if self.profile is None:
            return _unknown(UnknownReason.NOT_COLLECTED.value)
        pid, reached = self._phase(phase)
        if pid is None:
            return _unknown(UnknownReason.NOT_COLLECTED.value)
        ends = [self.count(dst, pid)] + ([self.count(src, pid)] if src is not None else [])
        for c in ends:
            if not c.known:
                return c
        ks = None if kinds is None else set(kinds)
        rows = [e for e in self._edge_index.get((src, dst, pid), [])
                if ks is None or e["kind"] in ks]
        if not rows:
            if all(c.exact for c in ends):
                return Count(0, CountStatus.EXACT.value)
            worst = min(ends, key=lambda c: _STATUS_RANK[CountStatus(c.status)])
            return Count(0, worst.status, worst.reason)
        st = status_min(*[e["status"] for e in rows])
        rs = sorted({r for e in rows if e.get("reason") for r in e["reason"].split(",")})
        return Count(sum(e["count"] for e in rows), st, ",".join(rs) or None)

    @staticmethod
    def dest_set_complete(kind: str) -> bool:
        """False for indirect transfers: unobserved means "not observed", not "unreachable"."""
        return kind not in INDIRECT_KINDS

    # -- reporting -----------------------------------------------------------
    def verdict(self) -> dict[str, Any]:
        """Row for an artifact's ``profiles.json`` / sidecar ``profile_ids``."""
        if self.profile is None:
            return {"profile_id": None, "verdict": "not_collected"}
        d: dict[str, Any] = {"profile_id": self.profile.profile_id, "verdict": "compatible",
                             "x_same_snapshot": self.same_snapshot,
                             "x_complete": self.profile.complete}
        if self.coverage is not None:
            d["x_coverage"] = round(self.coverage, 6)
        if self.via_input_snapshot is not None:
            d["x_via_input_snapshot"] = self.via_input_snapshot
        return d
