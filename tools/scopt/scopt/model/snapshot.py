"""Snapshot model (design §4.3, §5.2, §5.6).

* ``CacheSnapshot`` (the immutable imported snapshot) is WP7's and lives in
  :mod:`scopt.model.cache_snapshot`. Everything here only needs the
  ``SnapshotView`` protocol of :mod:`scopt.model.unit`, so WP9 works against
  any base snapshot (the WP7 one, as in ``tests/acceptance/test_wp9_real.py``,
  or a synthetic one in the unit tests).
* :class:`WorkingSet` (WP9) is the copy-on-write overlay the PassManager
  applies edits to. In B2 it overlays only the **layout facet** (pins, section
  orders, explicit placements) and pass **annotations**; code and capture
  metadata are never changed, so the base snapshot's ``TBUnit`` objects are
  shared by every generation.

Facets and per-unit facet hashes
--------------------------------
Analyses record which facet of which unit they read as ``(unit_id, hash)``
pairs (``AnalysisResult.dep_units``). The hash string says which facet it is:

* ``"sha256:…"`` — the unit's ``content_hash``: facets ``code`` and ``meta``;
* ``"layout:…"`` — :meth:`WorkingSet.layout_hash`: facet ``layout``;
* ``"annot:…"`` — :meth:`WorkingSet.annotation_hash`: facet ``annotations``.

The ``layout`` facet of a unit is ``(current tb_offset, explicit pin reasons,
section, index in the section order)``. B2 layout edits never move bytes;
``layout()`` returns the original offset unless a PLACE edit set one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from ..canon import canonical_json, sha256_id
from ..passes.base import EditKind
from .unit import TBUnit

__all__ = [
    "FACET_CODE", "FACET_META", "FACET_LAYOUT", "FACET_ANNOTATIONS", "FACETS",
    "CONTENT_FACETS", "LAYOUT_SECTIONS", "LAYOUT_HASH_PREFIX", "ANNOT_HASH_PREFIX",
    "facets_of_hash", "WorkingSet", "WorkingSetError", "deep_freeze", "thaw",
]

FACET_CODE = "code"
FACET_META = "meta"
FACET_LAYOUT = "layout"
FACET_ANNOTATIONS = "annotations"
FACETS = frozenset({FACET_CODE, FACET_META, FACET_LAYOUT, FACET_ANNOTATIONS})
CONTENT_FACETS = frozenset({FACET_CODE, FACET_META})

#: Layout sections in placement order (§5.5, §5.7).
LAYOUT_SECTIONS: tuple[str, ...] = ("hot", "warm", "unknown", "boot", "cold")

LAYOUT_HASH_PREFIX = "layout:"
ANNOT_HASH_PREFIX = "annot:"

#: Edit kinds a B2 WorkingSet can apply.
_LAYOUT_KINDS = frozenset({EditKind.PIN, EditKind.LAYOUT_ORDER, EditKind.PLACE})


def facets_of_hash(h: str) -> frozenset[str]:
    """Which facets a ``dep_units`` hash string stands for."""
    if h.startswith(LAYOUT_HASH_PREFIX):
        return frozenset({FACET_LAYOUT})
    if h.startswith(ANNOT_HASH_PREFIX):
        return frozenset({FACET_ANNOTATIONS})
    return CONTENT_FACETS


def deep_freeze(v: Any) -> Any:
    """An immutable copy of ``v``: mappings -> MappingProxyType (recursively),
    lists/tuples -> tuples, sets -> frozensets; scalars unchanged. Edit args
    and annotation values are frozen this way, so a pass cannot change applied
    state by mutating a list after proposing it."""
    if isinstance(v, Mapping):
        return MappingProxyType({k: deep_freeze(x) for k, x in v.items()})
    if isinstance(v, (list, tuple)):
        return tuple(deep_freeze(x) for x in v)
    if isinstance(v, (set, frozenset)):
        return frozenset(deep_freeze(x) for x in v)
    return v


def thaw(v: Any) -> Any:
    """The JSON-shaped (dict/list) copy of a :func:`deep_freeze` value."""
    if isinstance(v, Mapping):
        return {k: thaw(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [thaw(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted(thaw(x) for x in v)
    return v


def _ro(d: Mapping) -> Mapping:
    return d if isinstance(d, MappingProxyType) else MappingProxyType(dict(d))


class WorkingSetError(ValueError):
    """An edit the working set cannot apply (B3 kinds, unknown units)."""


class WorkingSet:
    """Generation ``n`` of the copy-on-write overlay over an immutable snapshot.

    Instances are immutable; :meth:`apply` returns generation ``n+1`` and the
    per-unit facets that changed. Only the mappings an edit touches are
    copied; the base snapshot and its ``TBUnit`` objects are shared.
    """

    __slots__ = ("_base", "_by_uid", "_order", "_generation", "_pins", "_sections",
                 "_section_of", "_placements", "_owners", "_annotations", "_provenance")

    def __init__(self, base: Any, *, _by_uid: Mapping[str, TBUnit] | None = None,
                 _order: tuple[str, ...] | None = None, generation: int = 0,
                 pins: Mapping[str, tuple[str, ...]] | None = None,
                 sections: Mapping[str, tuple[str, ...]] | None = None,
                 placements: Mapping[str, int] | None = None,
                 owners: Mapping[str, str] | None = None,
                 annotations: Mapping[str, Mapping[str, Any]] | None = None,
                 provenance: tuple[Mapping[str, Any], ...] = ()):
        self._base = base
        if _by_uid is None:
            units = tuple(base.units())
            by_uid: dict[str, TBUnit] = {}
            for u in units:
                if u.uid in by_uid:
                    raise WorkingSetError(f"duplicate UnitId {u.uid} in base snapshot")
                by_uid[u.uid] = u
            _by_uid = MappingProxyType(by_uid)
            _order = tuple(u.uid for u in units)
        self._by_uid = _by_uid
        self._order = _order
        self._generation = generation
        self._pins = _ro(pins or {})
        secs = {s: tuple((sections or {}).get(s, ())) for s in LAYOUT_SECTIONS}
        self._sections = MappingProxyType(secs)
        self._section_of = MappingProxyType(
            {uid: (s, i) for s in LAYOUT_SECTIONS for i, uid in enumerate(secs[s])})
        self._placements = _ro(placements or {})
        self._owners = _ro(owners or {})
        self._annotations = _ro({k: deep_freeze(v) for k, v in (annotations or {}).items()})
        self._provenance = tuple(provenance)

    @classmethod
    def initial(cls, base: Any) -> "WorkingSet":
        """Generation 0: no overlay. ``base`` is a ``SnapshotView``."""
        if isinstance(base, WorkingSet):
            return base
        return cls(base)

    # ------------------------------------------------------------------ base
    @property
    def base(self) -> Any:
        return self._base

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def snapshot_id(self) -> str:
        """The base SnapshotId. Layout edits do not change it before linking."""
        return self._base.snapshot_id

    @property
    def provenance(self) -> tuple[Mapping[str, Any], ...]:
        return self._provenance

    def unit_ids(self) -> tuple[str, ...]:
        """UnitIds in slot order."""
        return self._order

    def units(self) -> tuple[TBUnit, ...]:
        """Base units in slot order (without overlay annotations)."""
        return tuple(self._by_uid[u] for u in self._order)

    def unit(self, uid: str) -> TBUnit:
        try:
            return self._by_uid[uid]
        except KeyError:
            raise KeyError(f"unknown unit {uid}") from None

    def __contains__(self, uid: object) -> bool:
        return uid in self._by_uid

    # ------------------------------------------------------------ relocation
    def is_relocatable(self, uid: str) -> bool:
        """``ok_relocatable``: the importer marked it relocatable and its metadata is ok."""
        u = self.unit(uid)
        return bool(u.reloc.relocatable) and u.meta_status == "ok"

    def base_pin_reasons(self, uid: str) -> tuple[str, ...]:
        """Import-time pin reasons (non-relocatable units are pinned by the importer)."""
        u = self.unit(uid)
        if self.is_relocatable(uid):
            return ()
        reasons = tuple(u.reloc.reasons)
        if u.meta_status != "ok":
            reasons = (f"meta_{u.meta_status}",) + reasons
        return reasons or ("not_relocatable",)

    def edit_pin_reasons(self, uid: str) -> tuple[str, ...]:
        return self._pins.get(uid, ())

    def pin_reasons(self, uid: str) -> tuple[str, ...]:
        return self.base_pin_reasons(uid) + self.edit_pin_reasons(uid)

    def is_pinned(self, uid: str) -> bool:
        return bool(self.pin_reasons(uid))

    def pinned(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """``(uid, reasons)`` for every pinned unit, in slot order."""
        out = []
        for uid in self._order:
            r = self.pin_reasons(uid)
            if r:
                out.append((uid, r))
        return tuple(out)

    # ---------------------------------------------------------------- layout
    @property
    def sections(self) -> Mapping[str, tuple[str, ...]]:
        """Section -> UnitIds in requested order (every section present)."""
        return self._sections

    def section_of(self, uid: str) -> str | None:
        e = self._section_of.get(uid)
        return None if e is None else e[0]

    def placement(self, uid: str) -> int | None:
        return self._placements.get(uid)

    @property
    def placements(self) -> Mapping[str, int]:
        return self._placements

    def layout_owner(self, uid: str) -> str | None:
        """Name of the pass whose layout edit (pin, order, place) last set this unit."""
        return self._owners.get(uid)

    def tb_offset(self, uid: str) -> int:
        p = self._placements.get(uid)
        return self.unit(uid).tb_offset if p is None else p

    def layout(self) -> Mapping[str, int]:
        """UnitId -> current tb_offset (original unless placed), slot order."""
        return MappingProxyType({uid: self.tb_offset(uid) for uid in self._order})

    def layout_state(self, uid: str) -> Mapping[str, Any]:
        e = self._section_of.get(uid)
        return MappingProxyType({
            "tb_offset": self.tb_offset(uid),
            "placed": uid in self._placements,
            "pins": list(self.edit_pin_reasons(uid)),
            "section": None if e is None else e[0],
            "index": None if e is None else e[1],
        })

    @property
    def has_layout_edits(self) -> bool:
        return bool(self._pins or self._placements or any(self._sections.values()))

    # ----------------------------------------------------------- annotations
    def annotations(self, uid: str) -> Mapping[str, Any]:
        """Overlay annotations merged over the base unit's annotations."""
        self.unit(uid)
        over = self._annotations.get(uid)
        base = self._by_uid[uid].annotations
        if not over:
            return base
        if not base:
            return over
        return MappingProxyType({**base, **over})

    def annotated_units(self) -> tuple[str, ...]:
        return tuple(uid for uid in self._order if uid in self._annotations)

    # -------------------------------------------------------- facet hashes
    def content_hash(self, uid: str) -> str:
        return self.unit(uid).content_hash

    def layout_hash(self, uid: str) -> str:
        st = dict(self.layout_state(uid))
        if fragments := self.unit(uid).native_fragments:
            st["native_growth"] = sum(f.growth for f in fragments)
        return LAYOUT_HASH_PREFIX + hashlib.sha256(canonical_json(st)).hexdigest()

    def annotation_hash(self, uid: str) -> str:
        doc = thaw(self.annotations(uid))
        return ANNOT_HASH_PREFIX + hashlib.sha256(canonical_json(doc)).hexdigest()

    def facet_hash(self, uid: str, like: str) -> str:
        """The current hash of the same facet as the hash string ``like``."""
        if like.startswith(LAYOUT_HASH_PREFIX):
            return self.layout_hash(uid)
        if like.startswith(ANNOT_HASH_PREFIX):
            return self.annotation_hash(uid)
        return self.content_hash(uid)

    # ----------------------------------------------------------------- apply
    def apply(self, edits: Iterable[Any], *, owner: str,
              provenance: Mapping[str, Any] | None = None
              ) -> tuple["WorkingSet", dict[str, frozenset[str]]]:
        """Apply checked edits; return ``(generation n+1, {uid: changed facets})``.

        ``edits`` are :class:`scopt.proposal.Edit` objects (only ``kind`` and
        ``args`` are used) that the PassManager has already checked
        (capabilities, staleness, relocatability, conflicts). Layout edits
        replace any earlier layout decision for the unit (the manager only
        lets that happen for a pass that declares ``overrides={"layout"}``).
        """
        edits = list(edits)
        pins = dict(self._pins)
        sections = {s: list(v) for s, v in self._sections.items()}
        placements = dict(self._placements)
        owners = dict(self._owners)
        annotations = {k: dict(v) for k, v in self._annotations.items()}
        touched_layout: set[str] = set()
        touched_sections: set[str] = set()
        touched_annot: set[str] = set()
        native_by_unit: dict[str, list] = {}
        retired_by_unit: dict[str, list] = {}

        def drop_layout(uid: str) -> None:
            e = self._section_of.get(uid)
            for s, lst in sections.items():
                if uid in lst:
                    lst.remove(uid)
                    touched_sections.add(s)
            if e is not None:
                touched_sections.add(e[0])
            placements.pop(uid, None)

        for ed in edits:
            kind = ed.kind
            a = ed.args
            if kind is EditKind.ANNOTATE:
                uid = a["uid"]
                self.unit(uid)
                annotations.setdefault(uid, {})[a["key"]] = deep_freeze(a["value"])
                touched_annot.add(uid)
            elif kind is EditKind.PIN:
                uid = a["uid"]
                self.unit(uid)
                drop_layout(uid)
                if a["reason"] not in pins.get(uid, ()):
                    pins[uid] = pins.get(uid, ()) + (a["reason"],)
                owners[uid] = owner
                touched_layout.add(uid)
            elif kind is EditKind.LAYOUT_ORDER:
                sec = a["section"]
                if sec not in sections:
                    raise WorkingSetError(f"unknown layout section {sec!r}")
                for uid in a["uids"]:
                    self.unit(uid)
                    drop_layout(uid)
                    sections[sec].append(uid)
                    owners[uid] = owner
                    touched_layout.add(uid)
                touched_sections.add(sec)
            elif kind is EditKind.PLACE:
                uid = a["uid"]
                self.unit(uid)
                drop_layout(uid)
                placements[uid] = int(a["tb_offset"])
                owners[uid] = owner
                touched_layout.add(uid)
            elif kind is EditKind.INLINE_CALL:
                native_by_unit.setdefault(ed.payload.target, []).append(ed.payload)
            elif kind is EditKind.RETIRE_DIRECT_EXIT:
                retired_by_unit.setdefault(ed.payload.target, []).append(ed.payload)
            else:
                raise WorkingSetError(f"edit kind {kind.name} cannot be applied to a B2 "
                                      "working set (capability_not_enabled)")

        prov = self._provenance + ((MappingProxyType(dict(provenance)),) if provenance else ())
        by_uid = self._by_uid
        if native_by_unit or retired_by_unit:
            copies = dict(by_uid)
            for uid, requests in native_by_unit.items():
                u = copies[uid]
                ordered = sorted(requests, key=lambda r: r.call_off)
                fragments = u.native_fragments + tuple(ordered)
                content = sha256_id(canonical_json([u.content_hash, [r.certificate_id for r in ordered]]))
                copies[uid] = replace(u, native_fragments=fragments, content_hash=content)
            for uid, requests in retired_by_unit.items():
                u = copies[uid]
                slots = tuple(sorted(r.slot for r in requests))
                content = sha256_id(canonical_json([u.content_hash, [r.to_json() for r in requests]]))
                copies[uid] = replace(u, retired_exit_slots=slots, content_hash=content)
            by_uid = MappingProxyType(copies)
        new = WorkingSet(self._base, _by_uid=by_uid, _order=self._order,
                         generation=self._generation + 1, pins=pins,
                         sections={s: tuple(v) for s, v in sections.items()},
                         placements=placements, owners=owners, annotations=annotations,
                         provenance=prov)

        candidates = set(touched_layout)
        for s in touched_sections:
            candidates.update(self._sections.get(s, ()))
            candidates.update(new._sections.get(s, ()))
        changed: dict[str, set[str]] = {}
        for uid in candidates:
            if self.layout_hash(uid) != new.layout_hash(uid):
                changed.setdefault(uid, set()).add(FACET_LAYOUT)
        for uid in touched_annot:
            if self.annotation_hash(uid) != new.annotation_hash(uid):
                changed.setdefault(uid, set()).add(FACET_ANNOTATIONS)
        for uid in native_by_unit.keys() | retired_by_unit.keys():
            changed.setdefault(uid, set()).update({FACET_CODE, FACET_META, FACET_LAYOUT})
        return new, {uid: frozenset(f) for uid, f in changed.items()}

    def __repr__(self) -> str:
        return (f"WorkingSet(snapshot={self.snapshot_id[:19]}…, gen={self._generation}, "
                f"pins={len(self._pins)}, placed={len(self._placements)}, "
                f"ordered={sum(len(v) for v in self._sections.values())}, "
                f"annotated={len(self._annotations)})")
