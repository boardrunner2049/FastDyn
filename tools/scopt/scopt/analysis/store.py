"""AnalysisStore (design §5.3): typed, versioned, dependency-tracked analysis cache.

How dependencies are tracked
----------------------------
``AnalysisSpec.compute(ctx, scope)`` reads everything through the
:class:`AnalysisCtx` it is given, and the ctx records what was read:

* ``ctx.unit(uid)`` / ``ctx.units()`` -> ``(uid, content_hash)`` (facets code+meta);
* ``ctx.layout(uid)`` / ``ctx.layout_state(uid)`` -> ``(uid, "layout:…")``;
* ``ctx.annotations(uid)`` -> ``(uid, "annot:…")``;
* ``ctx.analysis(name, scope)`` -> ``(name, version, result_hash)`` in ``dep_results``;
* ``ctx.profile(phase)`` / ``ctx.bundle`` -> the profile_id / bundle_id in ``dep_inputs``;
* the snapshot_id is always in ``dep_inputs``.

A compute function may only read inputs its spec declares in ``inputs``
(``"code"``, ``"meta"``, ``"layout"``, ``"annotations"``, ``"profile:<phase>"``
or ``"profile:*"``, ``"bundle"``) and analyses its spec declares in
``requires``; anything else raises :class:`UndeclaredInput`.

A result is *current* when every ``dep_units`` hash equals the unit's current
facet hash, every ``dep_inputs`` id is still an input of the store, and every
``dep_results`` entry still has the same ``result_hash`` (recomputed if needed).
``get`` only returns current results (stale ones are recomputed), and
``check_fact`` raises :class:`~scopt.passes.base.StaleFact` for a result that is
not current.

``result_hash`` is the sha256 of ``(name, version, scope, value digest)``: equal
values give equal hashes, so a recomputation that yields the same value does
not invalidate dependents.

Invalidation
------------
``invalidate(changed, preserved)`` drops every cached result whose
``dep_units`` hit a changed ``(unit, facet)``, unless its analysis name is in
``preserved`` (or ``preserved`` contains ``"*"``), then recursively drops every
non-preserved result whose ``dep_results`` cite a dropped result. Preserved
results that were hit are re-stamped with the units' new facet hashes (that is
what the pass's ``preserves`` declaration asserts). It returns the number of
results dropped. Call it after :meth:`AnalysisStore.rebase` to the new view.

Persistence
-----------
With ``sqlite_path`` the store also persists results in ``analyses.sqlite``
(pickled; a cache only, never a source of truth). Rows are keyed by
``(name, version, scope, digest(deps))`` and are revalidated on load; a schema
version mismatch drops the table, so everything is recomputed. Results whose
values cannot be pickled stay in memory only. The in-memory path is the
default. Security: loading the file unpickles its rows, which can execute
code; only use a cache file this tool wrote locally, never one from an
untrusted source.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import logging
import pickle
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..canon import SHA256_PREFIX
from ..ids import is_unit_id
from ..model.snapshot import (
    CONTENT_FACETS, FACET_ANNOTATIONS, FACET_LAYOUT, FACETS, facets_of_hash,
)
from ..model.view import WorkingSetView, snapshot_view
from ..passes.base import StaleFact
from .api import AnalysisResult, AnalysisSpec, ScopeRef
from .registry import AnalysisRegistry, MissingAnalysis, default_registry

__all__ = [
    "AnalysisStore", "AnalysisCtx", "UndeclaredInput", "AnalysisCycle", "AnalysisSchemaError",
    "MissingAnalysis", "value_digest", "scope_ref", "SQLITE_SCHEMA_VERSION",
    "profile_phase_info", "profile_for_phase", "profile_id_of",
]

log = logging.getLogger("scopt.analysis.store")

SQLITE_SCHEMA_VERSION = 1


class UndeclaredInput(RuntimeError):
    """A compute function read an input or analysis its spec does not declare."""


class AnalysisCycle(RuntimeError):
    pass


class AnalysisSchemaError(TypeError):
    """A compute function returned a value that is not a ``result_schema`` instance."""


# ---------------------------------------------------------------------------
# Stable value digest
# ---------------------------------------------------------------------------

def _enc(v: Any) -> Any:
    if v is None:
        return ["n"]
    if isinstance(v, bool):
        return ["b", v]
    if isinstance(v, enum.Enum):
        return ["e", f"{type(v).__module__}.{type(v).__qualname__}", _enc(v.value)]
    if isinstance(v, int):
        return ["i", str(v)]
    if isinstance(v, float):
        return ["f", v.hex()]
    if isinstance(v, str):
        return ["s", v]
    if isinstance(v, (bytes, bytearray, memoryview)):
        return ["y", bytes(v).hex()]
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return ["d", f"{type(v).__module__}.{type(v).__qualname__}",
                [[f.name, _enc(getattr(v, f.name))] for f in dataclasses.fields(v)]]
    if isinstance(v, Mapping):
        items = [[_enc(k), _enc(x)] for k, x in v.items()]
        items.sort(key=lambda kv: json.dumps(kv[0], sort_keys=True))
        return ["m", items]
    if isinstance(v, (set, frozenset)):
        items = [_enc(x) for x in v]
        items.sort(key=lambda x: json.dumps(x, sort_keys=True))
        return ["S", items]
    if isinstance(v, (list, tuple)):
        return ["l", [_enc(x) for x in v]]
    name = f"{type(v).__module__}.{type(v).__qualname__}"
    f = getattr(v, "content_digest", None)
    if callable(f):
        return ["c", name, str(f())]
    state = getattr(v, "__dict__", None)
    if state is None:
        slots = [s for c in type(v).__mro__ for s in getattr(c, "__slots__", ())
                 if s not in ("__dict__", "__weakref__")]
        if slots:
            state = {s: getattr(v, s) for s in slots if hasattr(v, s)}
    if isinstance(state, Mapping):
        return ["o", name, _enc(dict(state))]
    raise TypeError(f"cannot digest value of type {type(v).__name__}")


def value_digest(value: Any) -> str:
    """``sha256:`` of a type-tagged canonical encoding of ``value``.

    Supports None, bool, int, float, str, bytes, Enum, dataclasses, mappings
    (order-insensitive), sets (order-insensitive), lists and tuples. Other
    objects are digested through a ``content_digest()`` method if they have
    one, otherwise through their ``__dict__``/``__slots__`` state (type name
    included); anything else (e.g. a bare ``object()``) raises TypeError.
    """
    data = json.dumps(_enc(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return SHA256_PREFIX + hashlib.sha256(data.encode("utf-8")).hexdigest()


def _result_hash(name: str, version: int, scope: ScopeRef, value: Any) -> str:
    return value_digest(["analysis_result", name, version, scope.kind, scope.id,
                         value_digest(value)])


def _deps_digest(dep_units, dep_results, dep_inputs) -> str:
    doc = [sorted(map(list, dep_units)), sorted(map(list, dep_results)), sorted(dep_inputs)]
    return SHA256_PREFIX + hashlib.sha256(
        json.dumps(doc, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Scopes and profiles
# ---------------------------------------------------------------------------

def scope_ref(scope: Any) -> ScopeRef:
    """Coerce a ScopeRef, a SnapshotView, a UnitView, a TBUnit or an id string."""
    if isinstance(scope, ScopeRef):
        return scope
    if isinstance(scope, str):
        if is_unit_id(scope):
            return ScopeRef("unit", scope)
        if scope.startswith(SHA256_PREFIX):
            return ScopeRef("snapshot", scope)
        raise ValueError(f"cannot infer scope from {scope!r}")
    uid = getattr(scope, "unit_id", None)
    if isinstance(uid, str):
        return ScopeRef("unit", uid)
    uid = getattr(scope, "uid", None)
    if isinstance(uid, str) and is_unit_id(uid):
        return ScopeRef("unit", uid)
    sid = getattr(scope, "snapshot_id", None)
    if isinstance(sid, str) and hasattr(scope, "units"):
        return ScopeRef("snapshot", sid)
    raise TypeError(f"cannot use {type(scope).__name__} as an analysis scope")


def profile_id_of(view: Any) -> str | None:
    pid = getattr(view, "profile_id", None)
    if pid is None:
        prof = getattr(view, "profile", None)
        if isinstance(prof, Mapping):
            pid = prof.get("profile_id")
        elif prof is not None:
            pid = getattr(prof, "profile_id", None)
    return pid


def _phase_rows(view: Any) -> Any:
    rows = getattr(view, "phases", None)
    prof = getattr(view, "profile", None)
    if rows is None and isinstance(prof, Mapping):
        rows = prof.get("phases")
    return rows


def _profileview_match(view: Any, prof: Any, pid: int, phase: str,
                       unit_ids: Iterable[str] | None = None) -> float | None:
    """Match rate of ``phase`` for a WP6 ``ProfileView`` (``view.profile`` is a Profile).

    ``ProfileView.open`` computes ``coverage`` for one phase (``coverage_phase``;
    None when the profile has no such phase and the coverage is over all
    phases). For that phase it is used as is; for any other phase the same
    ratio is recomputed here: the share of the profile's ``phase`` entries
    that land on the view's target units (§3.8; PROFILE_ONLY and transient
    rows are outside the profile's ``units`` and so outside both sides).
    ``unit_ids`` (the snapshot actually being optimized) replaces the view's
    target units when the two unit sets differ, so a view opened against
    another snapshot is measured against this one.
    """
    target = getattr(view, "target", None)
    tids = getattr(target, "unit_ids", None)
    tset = None if tids is None else set(tids)
    if unit_ids is not None:
        uset = set(unit_ids)
        if uset != tset:
            tset = uset
        else:
            unit_ids = None
    cov = getattr(view, "coverage", None)
    if unit_ids is None and cov is not None and getattr(view, "coverage_phase", None) == phase:
        return float(cov)
    units = getattr(prof, "units", None)
    if units is None or tset is None:
        return None
    tot = hit = 0
    for u in units:
        n = sum(c["entries"] for k, c in u["phase"].items() if int(k) == pid)
        tot += n
        if u["unit"] in tset:
            hit += n
    return 1.0 if tot == 0 else hit / tot


def profile_phase_info(view: Any, phase: str, unit_ids: Iterable[str] | None = None
                       ) -> tuple[bool, bool | None, float | None]:
    """``(present, reached, match_rate)`` of ``phase`` in a profile view.

    The PassManager needs only these three facts. Supported shapes, in order:

    * WP6's :class:`~scopt.formats.profile_v1.ProfileView`: ``view.profile`` is
      a ``Profile`` (``phase_id(name)``, ``phase_reached(pid)``) or None (a
      ``not_collected`` view: no phase is present); the match rate is
      ``view.coverage`` when ``view.coverage_phase == phase``, else recomputed
      for ``phase`` against ``view.target.unit_ids``, or against ``unit_ids``
      when given and different (see :func:`_profileview_match`);
    * a view with ``phase_reached(phase) -> bool | None`` (None: no such
      phase) and ``match_rate(phase) -> float | None``;
    * a view exposing ``phases`` (Profile v1 phase rows: mappings or objects
      with ``name``/``reached``) or a ``profile`` mapping (Profile v1 JSON),
      plus a ``match`` mapping ``phase -> rate``.
    """
    prof = getattr(view, "profile", None)
    if (prof is not None and not isinstance(prof, Mapping)
            and callable(getattr(prof, "phase_id", None))):
        pid = prof.phase_id(phase)
        if pid is None:
            return False, None, None
        return True, bool(prof.phase_reached(pid)), _profileview_match(view, prof, pid, phase,
                                                                       unit_ids)
    if prof is None and hasattr(view, "coverage_phase") and hasattr(view, "target"):
        return False, None, None  # ProfileView.not_collected
    reached: bool | None = None
    present = False
    f = getattr(view, "phase_reached", None)
    if callable(f):
        r = f(phase)
        present = r is not None
        reached = None if r is None else bool(r)
    else:
        for row in _phase_rows(view) or ():
            name = row.get("name") if isinstance(row, Mapping) else getattr(row, "name", None)
            if name == phase:
                present = True
                rv = row.get("reached") if isinstance(row, Mapping) else getattr(row, "reached",
                                                                                  None)
                reached = None if rv is None else bool(rv)
                break
    match: float | None = None
    g = getattr(view, "match_rate", None)
    if callable(g):
        match = g(phase)
    else:
        m = getattr(view, "match", None)
        if isinstance(m, Mapping):
            match = m.get(phase)
        elif present and getattr(view, "coverage_phase", None) == phase:
            match = getattr(view, "coverage", None)
    return present, reached, (None if match is None else float(match))


def profile_for_phase(profiles: Sequence[Any], phase: str | None) -> Any | None:
    """The first profile view that has ``phase`` (reached preferred).

    ``phase`` None (or ``"*"``) means any phase: the first view that carries a
    profile (a ``profile_id``).
    """
    if phase is None or phase == "*":
        for p in profiles:
            if profile_id_of(p):
                return p
        return None
    first_present = None
    for p in profiles:
        present, reached, _ = profile_phase_info(p, phase)
        if present and reached:
            return p
        if present and first_present is None:
            first_present = p
    return first_present


# ---------------------------------------------------------------------------
# Compute context
# ---------------------------------------------------------------------------

class AnalysisCtx:
    """What a compute function sees. Every read is recorded as a dependency."""

    def __init__(self, store: "AnalysisStore", spec: AnalysisSpec, scope: ScopeRef):
        self._store = store
        self.spec = spec
        self.scope = scope
        self._dep_units: set[tuple[str, str]] = set()
        self._dep_results: dict[tuple[str, int, str], ScopeRef] = {}
        self._dep_inputs: set[str] = {store.view.snapshot_id}
        self._certified = False
        self._requires = {n: v for n, v in spec.requires}

    # -- declarations ----------------------------------------------------------
    def _need(self, facet: str) -> None:
        if facet not in self.spec.inputs:
            raise UndeclaredInput(f"analysis {self.spec.name}@{self.spec.version} reads "
                                  f"{facet!r} but declares inputs {sorted(self.spec.inputs)}")

    def _need_content(self) -> None:
        if not (self.spec.inputs & CONTENT_FACETS):
            raise UndeclaredInput(f"analysis {self.spec.name}@{self.spec.version} reads unit "
                                  "content but declares neither 'code' nor 'meta'")

    # -- snapshot --------------------------------------------------------------
    @property
    def snapshot_id(self) -> str:
        return self._store.view.snapshot_id

    @property
    def header(self) -> Any:
        return self._store.view.header

    @property
    def runtime(self) -> Any:
        return self._store.view.runtime

    def unit_ids(self) -> tuple[str, ...]:
        """UnitIds in slot order (the unit set is covered by the snapshot_id)."""
        return self._store.view.unit_ids()

    def unit(self, uid: str):
        self._need_content()
        u = self._store.view.unit(uid)
        self._dep_units.add((uid, u.content_hash))
        return u

    def units(self) -> tuple:
        self._need_content()
        us = self._store.view.units()
        self._dep_units.update((u.uid, u.content_hash) for u in us)
        return us

    def layout(self, uid: str | None = None):
        """Current tb_offset of ``uid``, or the whole ``uid -> tb_offset`` map."""
        self._need(FACET_LAYOUT)
        v = self._store.view
        if uid is None:
            for u in v.unit_ids():
                self._dep_units.add((u, v.layout_hash(u)))
            return v.layout()
        self._dep_units.add((uid, v.layout_hash(uid)))
        return v.working_set.tb_offset(uid)

    def layout_state(self, uid: str) -> Mapping[str, Any]:
        self._need(FACET_LAYOUT)
        v = self._store.view
        self._dep_units.add((uid, v.layout_hash(uid)))
        return v.working_set.layout_state(uid)

    def pin_reasons(self, uid: str) -> tuple[str, ...]:
        """Import pins (content) plus edit pins (layout)."""
        self._need(FACET_LAYOUT)
        v = self._store.view
        self._dep_units.add((uid, v.layout_hash(uid)))
        self._dep_units.add((uid, v.content_hash(uid)))
        return v.pin_reasons(uid)

    def annotations(self, uid: str) -> Mapping[str, Any]:
        self._need(FACET_ANNOTATIONS)
        v = self._store.view
        self._dep_units.add((uid, v.annotation_hash(uid)))
        return v.annotations(uid)

    # -- other analyses and inputs ----------------------------------------------
    def analysis(self, name: str, scope: Any, min_version: int = 1) -> AnalysisResult:
        if name not in self._requires:
            raise UndeclaredInput(f"analysis {self.spec.name}@{self.spec.version} reads "
                                  f"analysis {name!r} without declaring it in requires")
        r = self._store.get(name, scope, max(min_version, self._requires[name]))
        self._dep_results[(r.name, r.version, r.result_hash)] = scope_ref(scope)
        return r

    def profile(self, phase: str | None) -> Any | None:
        """The attached profile view that has ``phase`` (None: any profile), or None.

        Declaring ``"profile"`` or ``"profile:*"`` in ``inputs`` allows every
        phase and ``phase=None``; ``"profile:<phase>"`` allows that phase only.
        """
        ins = self.spec.inputs
        if not ({"profile", "profile:*"} & ins) and (phase is None or
                                                       f"profile:{phase}" not in ins):
            raise UndeclaredInput(f"analysis {self.spec.name}@{self.spec.version} reads "
                                  f"profile phase {phase!r} without declaring it")
        p = profile_for_phase(self._store.profiles, phase)
        pid = profile_id_of(p) if p is not None else None
        if pid:
            self._dep_inputs.add(pid)
        else:
            self._dep_inputs.add(f"no_profile:{'*' if phase is None else phase}")
        return p

    def profile_view(self) -> Any | None:
        """``profile(None)``: the attached ProfileView, if any (WP6 ``profile.counts``)."""
        return self.profile(None)

    @property
    def snapshot(self) -> "_CtxSnapshot":
        """A read-only snapshot facade whose reads go through this ctx."""
        return _CtxSnapshot(self)

    @property
    def bundle(self) -> Any:
        self._need("bundle")
        if self._store.bundle_id:
            self._dep_inputs.add(self._store.bundle_id)
        return self._store.view.bundle

    def certify(self, certified: bool = True) -> None:
        """Mark the result as a certified fact (citable through ``PassContext.fact``)."""
        self._certified = bool(certified)


class _UnitIdOnly:
    """A unit handle that carries only its UnitId (the unit set is covered by
    the snapshot_id, so reading ids records no unit dependency)."""

    __slots__ = ("uid",)

    def __init__(self, uid: str):
        self.uid = uid

    def __repr__(self) -> str:
        return f"_UnitIdOnly({self.uid})"


class _CtxSnapshot:
    """``ctx.snapshot``: snapshot-shaped access for analyses written against a
    SnapshotView (e.g. WP6's ``profile.counts``). ``units()``/``unit()`` go
    through the ctx (recorded, and only allowed with a content input); without
    a content input ``units()`` returns UnitId-only handles."""

    __slots__ = ("_ctx",)

    def __init__(self, ctx: AnalysisCtx):
        self._ctx = ctx

    @property
    def snapshot_id(self) -> str:
        return self._ctx.snapshot_id

    @property
    def header(self) -> Any:
        return self._ctx.header

    @property
    def runtime(self) -> Any:
        return self._ctx.runtime

    def unit_ids(self) -> tuple[str, ...]:
        return self._ctx.unit_ids()

    def units(self) -> tuple:
        if self._ctx.spec.inputs & CONTENT_FACETS:
            return self._ctx.units()
        return tuple(_UnitIdOnly(u) for u in self._ctx.unit_ids())

    def unit(self, uid: str):
        return self._ctx.unit(uid)

    def layout(self) -> Mapping[str, int]:
        return self._ctx.layout()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_Key = tuple[str, int, str, str]  # (name, version, scope kind, scope id)


@dataclass
class _Entry:
    result: AnalysisResult
    dep_scopes: dict[tuple[str, int, str], ScopeRef] = field(default_factory=dict)


class _SqliteCache:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path)
        cur = self.db.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        row = cur.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        if row is None or row[0] != str(SQLITE_SCHEMA_VERSION):
            cur.execute("DROP TABLE IF EXISTS results")
            cur.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                        (str(SQLITE_SCHEMA_VERSION),))
        cur.execute("CREATE TABLE IF NOT EXISTS results (name TEXT, version INTEGER, "
                    "scope_kind TEXT, scope_id TEXT, digest TEXT, result_hash TEXT, blob BLOB, "
                    "PRIMARY KEY (name, version, scope_kind, scope_id, digest))")
        self.db.commit()

    def load(self, key: _Key) -> list[_Entry]:
        rows = self.db.execute(
            "SELECT blob FROM results WHERE name=? AND version=? AND scope_kind=? AND scope_id=?",
            key).fetchall()
        out = []
        for (blob,) in rows:
            try:
                res, scopes = pickle.loads(blob)
                out.append(_Entry(res, {tuple(k): ScopeRef(*v) for k, v in scopes}))
            except Exception:  # corrupt/foreign row: ignore (cache only)
                continue
        return out

    def save(self, key: _Key, e: _Entry) -> bool:
        r = e.result
        try:
            blob = pickle.dumps((r, [(list(k), (s.kind, s.id)) for k, s in e.dep_scopes.items()]))
        except Exception:
            return False
        self.db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?)",
                        (*key, _deps_digest(r.dep_units, r.dep_results, r.dep_inputs),
                         r.result_hash, blob))
        self.db.commit()
        return True

    def close(self) -> None:
        self.db.close()


class AnalysisStore:
    """``get`` / ``invalidate`` / ``check_fact`` over one (current) snapshot view.

    ``view`` is a :class:`~scopt.model.view.WorkingSetView` (a plain
    ``SnapshotView`` is wrapped). ``profiles`` are profile views (see
    :func:`profile_phase_info`).
    """

    def __init__(self, view: Any, registry: AnalysisRegistry | None = None, *,
                 profiles: Iterable[Any] = (), bundle_id: str | None = None,
                 sqlite_path: str | None = None):
        self._view = view if isinstance(view, WorkingSetView) else snapshot_view(view)
        self.registry = registry if registry is not None else default_registry()
        self.profiles: tuple[Any, ...] = tuple(profiles)
        self.bundle_id = bundle_id
        self._cache: dict[_Key, _Entry] = {}
        self._computing: list[_Key] = []
        self._sql = _SqliteCache(sqlite_path) if sqlite_path else None
        self.stats = {"computed": 0, "hits": 0, "stale": 0, "sqlite_hits": 0, "dropped": 0}

    # -- state ---------------------------------------------------------------
    @property
    def view(self) -> WorkingSetView:
        return self._view

    def rebase(self, view: Any) -> None:
        """Point the store at a new WorkingSet generation (call before ``invalidate``)."""
        self._view = view if isinstance(view, WorkingSetView) else snapshot_view(view)

    def set_profiles(self, profiles: Iterable[Any]) -> None:
        self.profiles = tuple(profiles)

    def inputs(self) -> frozenset[str]:
        ids = {self._view.snapshot_id}
        if self.bundle_id:
            ids.add(self.bundle_id)
        for p in self.profiles:
            pid = profile_id_of(p)
            if pid:
                ids.add(pid)
        return frozenset(ids)

    def cached(self) -> tuple[AnalysisResult, ...]:
        return tuple(e.result for _, e in sorted(self._cache.items(), key=lambda kv: kv[0]))

    def peek(self, name: str, scope: Any, version: int | None = None) -> AnalysisResult | None:
        """The cached result, if any, without validating or computing."""
        s = scope_ref(scope)
        if version is None:
            vs = [k[1] for k in self._cache if k[0] == name and k[2] == s.kind and k[3] == s.id]
            if not vs:
                return None
            version = max(vs)
        e = self._cache.get((name, version, s.kind, s.id))
        return None if e is None else e.result

    def close(self) -> None:
        if self._sql is not None:
            self._sql.close()
            self._sql = None

    # -- currency --------------------------------------------------------------
    def _unit_current(self, uid: str, h: str) -> bool:
        try:
            return self._view.facet_hash(uid, h) == h
        except KeyError:
            return False

    def _deps_current(self, r: AnalysisResult, scopes: Mapping[tuple[str, int, str], ScopeRef]
                      ) -> bool:
        inputs = self.inputs()
        for i in r.dep_inputs:
            if i.startswith("no_profile:"):
                if not self._no_profile(i):
                    return False
            elif i not in inputs:
                return False
        for uid, h in r.dep_units:
            if not self._unit_current(uid, h):
                return False
        for dep in r.dep_results:
            n, v, h = dep
            sc = scopes.get(dep)
            if sc is None:
                return False
            try:
                spec = self.registry.exact(n, v)
            except MissingAnalysis:
                return False
            cur = self._get_spec(spec, sc)
            if cur.result_hash != h:
                return False
        return True

    def _no_profile(self, marker: str) -> bool:
        phase = marker[len("no_profile:"):]
        return profile_for_phase(self.profiles, None if phase == "*" else phase) is None

    def is_current(self, r: AnalysisResult) -> bool:
        """True when ``r``'s dependencies all still hold (recomputing deps if needed)."""
        scopes = self._scopes_for(r)
        if scopes is None:
            return False
        return self._deps_current(r, scopes)

    def _scopes_for(self, r: AnalysisResult) -> dict | None:
        for k, e in self._cache.items():
            if k[0] == r.name and k[1] == r.version and k[3] == r.scope_id:
                if e.result.result_hash == r.result_hash:
                    return e.dep_scopes
        # Not cached any more: scopes of its dep_results must be resolvable from the cache.
        scopes: dict = {}
        for dep in r.dep_results:
            found = None
            for k, e in self._cache.items():
                if (k[0], k[1]) == dep[:2] and e.result.result_hash == dep[2]:
                    found = ScopeRef(k[2], k[3])
                    break
            if found is None:
                return None
            scopes[dep] = found
        return scopes

    def check_fact(self, r: AnalysisResult) -> None:
        """Raise :class:`StaleFact` unless ``r`` is still current."""
        if not isinstance(r, AnalysisResult):
            raise StaleFact(f"not an AnalysisResult: {r!r}")
        if not self.is_current(r):
            raise StaleFact(f"{r.name}@{r.version} {r.scope_id} ({r.result_hash[:19]}…) is stale")

    def current_hash(self, name: str, scope: Any, version: int) -> str | None:
        """``result_hash`` of the current result of ``name@version`` for ``scope``."""
        try:
            spec = self.registry.exact(name, version)
        except MissingAnalysis:
            return None
        return self._get_spec(spec, scope_ref(scope)).result_hash

    # -- get / compute ---------------------------------------------------------
    def get(self, name: str, scope: Any, min_version: int = 1) -> AnalysisResult:
        spec = self.registry.lookup(name, min_version)
        return self._get_spec(spec, scope_ref(scope))

    def _get_spec(self, spec: AnalysisSpec, scope: ScopeRef) -> AnalysisResult:
        if spec.scope != scope.kind:
            raise ValueError(f"analysis {spec.name}@{spec.version} has scope {spec.scope!r}, "
                             f"got a {scope.kind!r} scope")
        if scope.kind == "snapshot" and scope.id != self._view.snapshot_id:
            raise ValueError(f"snapshot scope {scope.id} is not the store's snapshot "
                             f"{self._view.snapshot_id}")
        if scope.kind == "unit" and scope.id not in self._view:
            raise KeyError(f"unknown unit {scope.id}")
        key: _Key = (spec.name, spec.version, scope.kind, scope.id)
        if key in self._computing:
            chain = " -> ".join(f"{k[0]}@{k[1]}" for k in self._computing + [key])
            raise AnalysisCycle(f"analysis dependency cycle: {chain}")
        e = self._cache.get(key)
        if e is not None:
            self._computing.append(key)
            try:
                ok = self._deps_current(e.result, e.dep_scopes)
            finally:
                self._computing.pop()
            if ok:
                self.stats["hits"] += 1
                return e.result
            self.stats["stale"] += 1
            del self._cache[key]
        if self._sql is not None:
            for cand in self._sql.load(key):
                self._computing.append(key)
                try:
                    ok = self._deps_current(cand.result, cand.dep_scopes)
                finally:
                    self._computing.pop()
                if ok:
                    self.stats["sqlite_hits"] += 1
                    self._cache[key] = cand
                    return cand.result
        return self._compute(spec, scope, key)

    def _compute(self, spec: AnalysisSpec, scope: ScopeRef, key: _Key) -> AnalysisResult:
        for rn, rv in spec.requires:
            if not self.registry.has(rn, rv):
                raise MissingAnalysis(f"analysis {spec.name}@{spec.version} requires "
                                      f"{rn}@>={rv}, which is not registered")
        ctx = AnalysisCtx(self, spec, scope)
        self._computing.append(key)
        try:
            value = spec.compute(ctx, scope)
        finally:
            self._computing.pop()
        rs = spec.result_schema
        if isinstance(rs, type) and rs is not object and not isinstance(value, rs):
            raise AnalysisSchemaError(f"analysis {spec.name}@{spec.version} returned "
                                      f"{type(value).__name__}, expected {rs.__name__}")
        res = AnalysisResult(
            name=spec.name, version=spec.version, scope_id=scope.id, value=value,
            dep_units=frozenset(ctx._dep_units),
            dep_results=frozenset(ctx._dep_results),
            dep_inputs=frozenset(ctx._dep_inputs), certified=ctx._certified,
            result_hash=_result_hash(spec.name, spec.version, scope, value))
        e = _Entry(res, dict(ctx._dep_results))
        self._cache[key] = e
        self.stats["computed"] += 1
        if self._sql is not None:
            self._sql.save(key, e)
        return res

    # -- invalidation -----------------------------------------------------------
    def invalidate(self, changed: Mapping[str, Iterable[str]], preserved: Iterable[str]) -> int:
        """Drop results hit by ``changed`` (uid -> facets) and their transitive dependents.

        Facets are ``code``, ``meta``, ``layout`` and ``annotations``. Returns the
        number of dropped results.
        """
        ch: dict[str, frozenset[str]] = {}
        for uid, fs in changed.items():
            fs = frozenset(fs)
            bad = fs - FACETS
            if bad:
                raise ValueError(f"unknown facets {sorted(bad)} for {uid}")
            if fs:
                ch[uid] = fs
        preserved = frozenset(preserved)
        keep_all = "*" in preserved

        def is_preserved(r: AnalysisResult) -> bool:
            return keep_all or r.name in preserved

        def hit(r: AnalysisResult) -> bool:
            for uid, h in r.dep_units:
                fs = ch.get(uid)
                if fs and facets_of_hash(h) & fs:
                    return True
            return False

        dropped: set[_Key] = set()
        for k, e in self._cache.items():
            if not is_preserved(e.result) and hit(e.result):
                dropped.add(k)
        grew = True
        while grew:
            grew = False
            gone = {(self._cache[k].result.name, self._cache[k].result.version,
                     self._cache[k].result.result_hash) for k in dropped}
            for k, e in self._cache.items():
                if k in dropped or is_preserved(e.result):
                    continue
                if e.result.dep_results & gone:
                    dropped.add(k)
                    grew = True
        for k, e in list(self._cache.items()):
            if k in dropped:
                continue
            if hit(e.result):  # preserved: re-stamp with the new facet hashes
                new_units = frozenset(
                    (uid, self._view.facet_hash(uid, h)) if uid in ch and uid in self._view
                    else (uid, h) for uid, h in e.result.dep_units)
                self._cache[k] = _Entry(dataclasses.replace(e.result, dep_units=new_units),
                                        e.dep_scopes)
        for k in dropped:
            del self._cache[k]
        self.stats["dropped"] += len(dropped)
        if dropped:
            log.debug("invalidate: dropped %d result(s): %s", len(dropped),
                      ", ".join(f"{k[0]}@{k[1]}[{k[3][:24]}]" for k in sorted(dropped)))
        return len(dropped)

