"""PassManager (design §5.6): run a pipeline of passes over a WorkingSet.

``PassManager(snapshot, pipeline, profiles, params, store).execute() -> Candidate``

Per pipeline entry (in order):

1. **Resolve** the pass class (registry of ``PASSES`` tuples in the
   ``scopt.passes`` modules) and its effective params (pipeline TOML params
   overlaid with ``params[pass_name]``), checked against ``params_schema``.
2. **Admission**: domain, unit scope (``region`` only in B3 mode, WP21),
   required analyses and versions, capture level, capabilities ⊆ allowed
   (pipeline ``allowed_capabilities`` ∩ the kinds enabled for B2), profile
   phase present and reached, profile match rate ≥ ``min_profile_match``
   (default 0.95 when a phase is required). Failures are recorded as
   SKIPPED_UNSUPPORTED / SKIPPED_UNKNOWN and the pipeline continues.
3. **Run** on a fresh :class:`~scopt.model.view.WorkingSetView` under the
   wall-clock budget. An exception gives FAILED with the sha256 of the
   traceback in ``detail``; ``BudgetExceeded`` (or a run longer than
   ``max_wall_s``) gives BUDGET_EXHAUSTED; an edit-capability violation gives
   FAILED (``edit_capability`` or ``capability_not_enabled``). The pass's edits
   are discarded in every case but CANDIDATE.
4. **Edit check**: capabilities, ``reads`` still current (``stale_edit``),
   cited facts valid (``stale_fact``), budgets (``max_units``), layout edits only
   on relocatable (``not_relocatable``) and unpinned (``pinned``) units,
   conflicts (``conflict``: a unit ordered/placed twice in one pass, or a unit
   whose layout an earlier pass decided, unless this pass declares
   ``overrides={"layout"}``).
5. **Apply** to WorkingSet n+1, rebase the AnalysisStore, ``invalidate`` with
   the changed (unit, facet) pairs and the pass's ``preserves``, and append
   provenance.

Every pipeline entry (run, skipped, disabled or failed) gets a ledger row
(subject ``pass``, stage ``optimized``). Linking, validation and publishing
(§5.6 step 6) are WP10's and consume the returned :class:`Candidate`.

Ablation (§5.6 step 7): ``disable=`` names passes to skip and ``only=`` names
the single pass to run; skipped passes are recorded with reason
``ablation_disabled``.

B3/B4 mode (WP21, design §3.4-3.9): ``PassManager(..., b3=B3Env(...))`` enables
the B3/B4 edit kinds (CLONE, REDIRECT_EXIT, ADD_ENTRY, PURE_REWRITE,
ATTACH_FACT; ADD_REGION stays manager-generated and ADD_RECOVERY is never
enabled), accepts ``unit_scope = "region"`` passes (run once per variant of
the overlay, on a :class:`~scopt.manager_b3.VariantView`), and applies B3
edits to the :class:`~scopt.manager_b3.B3State` overlay after checking the
§3.7 preconditions (a refusal discards the pass's proposal with its §3.8
code and outcome). Every pass row of the ledger then carries ``counts``
(edits per kind, bytes changed, refusals per reason). See
``scopt/manager_b3.py`` and ``docs/b3-passes.md``.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import pkgutil
import time
import tomllib
import traceback
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .analysis.api import AnalysisResult, ScopeRef
from .analysis.registry import AnalysisRegistry, default_registry
from .analysis.store import (
    AnalysisStore, profile_for_phase, profile_id_of, profile_phase_info, scope_ref,
)
from .canon import _check as _check_json
from .canon import canonical_json, canonical_jsonl, sha256_id
from .formats.enums import CaptureLevel
from .ledger import Ledger, LedgerRow, utc_now
from .model.snapshot import FACETS, WorkingSet
from .model.view import WorkingSetView
from .passes.base import (
    B2_EDIT_KINDS, B3_EDIT_KINDS, NATIVE_EDIT_KINDS, BudgetExceeded, EditCapabilityError, EditKind, Outcome,
    PassDescriptor, PassResult, Reason, StaleFact,
)
from .proposal import (
    REASON_EDIT_PRECONDITION, Edit, EditPreconditionError, FactRef, ProposalBuilder, Read,
    capability_reason, edit_digest,
)

__all__ = [
    "PassManager", "Candidate", "PassRecord", "Pipeline", "PipelineEntry", "PipelineError",
    "PassRegistry", "ManagedPassContext", "load_pipeline", "discover_passes",
    "default_pass_registry", "PIPELINES_DIR", "pipelines_dir", "DEFAULT_MIN_PROFILE_MATCH", "R",
]

log = logging.getLogger("scopt.manager")

PIPELINES_DIR = Path(__file__).resolve().parent.parent / "pipelines"


def pipelines_dir() -> Path:
    """``$SCOPT_PIPELINES_DIR`` if set, else :data:`PIPELINES_DIR`."""
    env = os.environ.get("SCOPT_PIPELINES_DIR")
    return Path(env) if env else PIPELINES_DIR

DEFAULT_MIN_PROFILE_MATCH = 0.95
LEDGER_STAGE = "optimized"


class R:
    """Reason codes the manager emits in addition to :class:`~scopt.passes.base.Reason`."""

    PASS_NOT_REGISTERED = "pass_not_registered"
    PASS_VERSION_UNAVAILABLE = "pass_version_unavailable"
    INVALID_PARAMS = "invalid_params"
    ABLATION_DISABLED = "ablation_disabled"
    DOMAIN = "domain_not_supported"
    REGION_SCOPE = "region_scope_not_enabled"
    PHASE_NOT_REACHED = "profile_phase_not_reached"
    PINNED = "pinned"
    UNKNOWN_UNIT = "unknown_unit"
    BAD_RESULT = "bad_pass_result"
    EDITS_WITHOUT_CANDIDATE = "edits_with_no_change"
    EDIT_CAPABILITY = "edit_capability"
    EDIT_PRECONDITION = REASON_EDIT_PRECONDITION


class PipelineError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Pass registry
# ---------------------------------------------------------------------------

class PassRegistry:
    """``(name, version) -> pass class``. Each ``scopt.passes`` module lists its
    passes in a module-level ``PASSES`` tuple (see :func:`discover_passes`)."""

    def __init__(self, classes: Iterable[type] = ()):
        self._by: dict[tuple[str, int], type] = {}
        for c in classes:
            self.register(c)

    def register(self, cls: type, *, replace: bool = False) -> type:
        d = getattr(cls, "descriptor", None)
        if not isinstance(d, PassDescriptor):
            raise TypeError(f"{cls!r} has no PassDescriptor 'descriptor'")
        if not callable(getattr(cls, "run", None)):
            raise TypeError(f"{cls!r} has no run() method")
        k = (d.name, d.version)
        if k in self._by and self._by[k] is not cls and not replace:
            raise ValueError(f"pass {d.name}@{d.version} already registered")
        self._by[k] = cls
        return cls

    def get(self, name: str, version: int) -> type:
        return self._by[(name, version)]

    def versions(self, name: str) -> tuple[int, ...]:
        return tuple(sorted(v for n, v in self._by if n == name))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted({n for n, _ in self._by}))

    def copy(self) -> "PassRegistry":
        return PassRegistry(self._by.values())


def discover_passes(registry: PassRegistry | None = None,
                    package: str = "scopt.passes") -> PassRegistry:
    reg = registry if registry is not None else PassRegistry()
    pkg = importlib.import_module(package)
    for mi in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if mi.name in ("base",) or mi.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{package}.{mi.name}")
        for cls in getattr(mod, "PASSES", ()):
            reg.register(cls, replace=True)
    return reg


_DEFAULT_PASSES: PassRegistry | None = None


def default_pass_registry() -> PassRegistry:
    global _DEFAULT_PASSES
    if _DEFAULT_PASSES is None:
        _DEFAULT_PASSES = discover_passes()
    return _DEFAULT_PASSES.copy()


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PipelineEntry:
    name: str
    version: int
    params: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "params": dict(self.params)}


@dataclass(frozen=True)
class Pipeline:
    name: str
    passes: tuple[PipelineEntry, ...]
    allowed_capabilities: frozenset[EditKind] = frozenset()
    version: int = 1
    description: str = ""
    source: str | None = field(default=None, compare=False)

    def to_json(self) -> dict[str, Any]:
        return {"schema": "scopt.pipeline", "version": self.version, "name": self.name,
                "description": self.description,
                "allowed_capabilities": sorted(k.name for k in self.allowed_capabilities),
                "pass": [p.to_json() for p in self.passes]}

    @property
    def pipeline_id(self) -> str:
        return sha256_id(canonical_json(self.to_json()))


_PIPELINE_KEYS = {"schema", "version", "name", "description", "allowed_capabilities", "pass"}
_ENTRY_KEYS = {"name", "version", "params"}


def _strict_keys(d: Mapping[str, Any], allowed: set[str], where: str) -> None:
    extra = [k for k in d if k not in allowed and not k.startswith("x_")]
    if extra:
        raise PipelineError(f"{where}: unknown keys {sorted(extra)}")


def pipeline_from_mapping(doc: Mapping[str, Any], source: str | None = None) -> Pipeline:
    where = source or "<pipeline>"
    _strict_keys(doc, _PIPELINE_KEYS, where)
    if doc.get("schema", "scopt.pipeline") != "scopt.pipeline":
        raise PipelineError(f"{where}: schema must be 'scopt.pipeline'")
    if doc.get("version", 1) != 1:
        raise PipelineError(f"{where}: unsupported pipeline version {doc.get('version')!r}")
    name = doc.get("name")
    if not isinstance(name, str) or not name:
        raise PipelineError(f"{where}: 'name' is required")
    caps = set()
    for c in doc.get("allowed_capabilities", []):
        try:
            caps.add(EditKind[c])
        except KeyError:
            raise PipelineError(f"{where}: unknown edit kind {c!r} in allowed_capabilities") \
                from None
    entries = []
    for i, p in enumerate(doc.get("pass", [])):
        if not isinstance(p, Mapping):
            raise PipelineError(f"{where}: pass[{i}] must be a table")
        _strict_keys(p, _ENTRY_KEYS, f"{where}: pass[{i}]")
        pn, pv = p.get("name"), p.get("version")
        if not isinstance(pn, str) or not pn:
            raise PipelineError(f"{where}: pass[{i}] needs a name")
        if isinstance(pv, bool) or not isinstance(pv, int) or pv < 1:
            raise PipelineError(f"{where}: pass[{i}] ({pn}) needs an integer version >= 1")
        params = p.get("params", {})
        if not isinstance(params, Mapping):
            raise PipelineError(f"{where}: pass[{i}] params must be a table")
        try:
            _check_json(params)
        except TypeError as exc:
            raise PipelineError(f"{where}: pass[{i}] params are not JSON values: {exc}") \
                from None
        entries.append(PipelineEntry(pn, pv, MappingProxyType(dict(params))))
    return Pipeline(name=name, passes=tuple(entries), allowed_capabilities=frozenset(caps),
                    version=1, description=str(doc.get("description", "")), source=source)


def load_pipeline(spec: "str | os.PathLike | Mapping[str, Any] | Pipeline") -> Pipeline:
    """A Pipeline from a name, a TOML path or a mapping.

    A bare name (no suffix, no directory part, e.g. ``"identity"``) is always a
    named pipeline: ``$SCOPT_PIPELINES_DIR/<name>.toml`` if that variable is set,
    else :data:`PIPELINES_DIR` (``<repo>/pipelines``; the directory sits outside
    the package, so a non-editable install must set ``SCOPT_PIPELINES_DIR``). A
    file of the same name in the cwd is never picked up. Anything else is a path.
    """
    if isinstance(spec, Pipeline):
        return spec
    if isinstance(spec, Mapping):
        return pipeline_from_mapping(spec)
    p = Path(spec)
    if not p.suffix and len(p.parts) == 1:
        p = pipelines_dir() / f"{p.name}.toml"
    try:
        with open(p, "rb") as f:
            doc = tomllib.load(f)
    except FileNotFoundError:
        raise PipelineError(f"pipeline {spec!s} not found (looked at {p})") from None
    except tomllib.TOMLDecodeError as exc:
        raise PipelineError(f"{p}: {exc}") from None
    return pipeline_from_mapping(doc, source=str(p))


# ---------------------------------------------------------------------------
# PassContext
# ---------------------------------------------------------------------------

class ManagedPassContext:
    """Concrete ``PassContext`` (§5.4) given to a pass by the manager."""

    def __init__(self, *, descriptor: PassDescriptor, view: WorkingSetView, store: AnalysisStore,
                 params: Mapping[str, Any], profiles: Sequence[Any], deadline: float,
                 logger: logging.Logger | logging.LoggerAdapter):
        self.descriptor = descriptor
        self.view = view
        self.store = store
        self.params: Mapping[str, Any] = MappingProxyType(dict(params))
        self.bundle = view.bundle
        self.runtime = view.runtime
        self.snapshot_id = view.snapshot_id
        self.log = logger
        self.deadline = deadline
        self._profiles = tuple(profiles)
        self._requires = {n: v for n, v in descriptor.requires}
        self._reads: list[Read] = []
        self._facts: list[FactRef] = []
        self.profiles_used: set[str] = set()
        self.b3: Any = None  # scopt.manager_b3.B3Ctx in B3 mode
        self.b3_state: Any = None  # the overlay this pass starts from

    def analysis(self, name: str, scope: Any, min_version: int = 1) -> AnalysisResult:
        if name not in self._requires:
            raise LookupError(f"pass {self.descriptor.name} reads analysis {name!r} without "
                              f"declaring it in requires")
        sref = scope_ref(scope)
        r = self.store.get(name, sref, max(min_version, self._requires[name]))
        self._reads.append(Read("analysis", r.name, r.result_hash, r.version, sref.kind, sref.id))
        return r

    def fact(self, name: str, scope: Any, min_version: int = 1) -> AnalysisResult:
        """A certified analysis result, cited by every edit proposed after this call."""
        r = self.analysis(name, scope, min_version)
        if not r.certified:
            raise StaleFact(f"{name}@{r.version} for {r.scope_id} is not a certified fact")
        self._facts.append(FactRef.of(r))
        return r

    def profile(self, phase: str | None) -> Any | None:
        """The attached ProfileView that has ``phase`` (reached preferred), or None.

        ``phase`` None returns the first attached view that carries a profile.
        """
        p = profile_for_phase(self._profiles, phase)
        if p is not None:
            pid = profile_id_of(p)
            if pid:
                self.profiles_used.add(pid)
        return p

    def budget(self) -> None:
        if time.monotonic() > self.deadline:
            raise BudgetExceeded(f"pass {self.descriptor.name}: max_wall_s="
                                 f"{self.descriptor.budgets.max_wall_s} exceeded")

    def recorded_reads(self) -> tuple[Read, ...]:
        return tuple(self._reads)

    def recorded_facts(self) -> tuple[FactRef, ...]:
        return tuple(self._facts)


# ---------------------------------------------------------------------------
# Records and candidate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PassRecord:
    index: int
    name: str
    version: int
    params: Mapping[str, Any]
    params_sha256: str
    outcome: Outcome
    reason: str
    detail: str = ""
    affected: tuple[str, ...] = ()
    guest_origins: tuple[str, ...] = ()
    stats: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    edit_digest: str | None = None
    n_edits: int = 0
    duration_s: float = 0.0
    invalidated: int = 0
    generation: int = 0  # WorkingSet generation after this pass
    traceback: str | None = field(default=None, repr=False)
    profiles_used: tuple[str, ...] = ()

    def to_sidecar(self) -> dict[str, Any]:
        """One ``pipeline`` row of the artifact sidecar (§4.6)."""
        return {"pass": self.name, "version": self.version, "params_sha256": self.params_sha256,
                "outcome": self.outcome.value, "reason": self.reason,
                "affected": list(self.affected), "edit_digest": self.edit_digest,
                "duration_s": self.duration_s}

    def to_json(self) -> dict[str, Any]:
        d = self.to_sidecar()
        d.update(index=self.index, params=dict(self.params), detail=self.detail,
                 guest_origins=list(self.guest_origins),
                 stats={k: v for k, v in self.stats.items()}, n_edits=self.n_edits,
                 invalidated=self.invalidated, generation=self.generation,
                 profiles_used=list(self.profiles_used))
        return d


@dataclass(frozen=True)
class Candidate:
    """What ``execute`` returns: the applied edit log plus the final WorkingSet."""

    candidate_id: str
    input_snapshot_id: str
    pipeline: Pipeline
    working_set: WorkingSet
    edits: tuple[Edit, ...]
    passes: tuple[PassRecord, ...]
    ledger: tuple[LedgerRow, ...]
    profile_ids: tuple[str, ...] = ()
    profile_verdicts: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    ablation: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    b3_state: Any = None  # scopt.manager_b3.B3State (B3 mode), else None

    @property
    def has_b3_edits(self) -> bool:
        return self.b3_state is not None and not self.b3_state.empty

    @property
    def view(self) -> WorkingSetView:
        return WorkingSetView(self.working_set)

    @property
    def edit_log(self) -> tuple[dict[str, Any], ...]:
        return tuple(e.to_json() for e in self.edits)

    def edit_log_jsonl(self) -> bytes:
        return canonical_jsonl(self.edit_log)

    @property
    def has_layout_changes(self) -> bool:
        return self.working_set.has_layout_edits

    def pipeline_provenance(self) -> list[dict[str, Any]]:
        rows = [p.to_sidecar() for p in self.passes]
        if self.pipeline.allowed_capabilities & NATIVE_EDIT_KINDS:
            for row, record in zip(rows, self.passes):
                if record.stats:
                    row["x_counts"] = dict(record.stats)
                if record.detail:
                    try:
                        row["x_diagnostics"] = json.loads(record.detail)
                    except ValueError:
                        row["x_diagnostics"] = record.detail
        return rows

    def pinned(self) -> list[dict[str, Any]]:
        return [{"unit": uid, "reasons": list(r)} for uid, r in self.working_set.pinned()]

    def profile_rows(self) -> list[dict[str, Any]]:
        return [{"profile_id": p, "verdict": self.profile_verdicts.get(p, "attached")}
                for p in self.profile_ids]

    def outcome(self, name: str) -> PassRecord | None:
        for p in self.passes:
            if p.name == name:
                return p
        return None


def _candidate_id(input_snapshot_id: str, pipeline: Pipeline, passes: Sequence[PassRecord],
                  ablation: Mapping[str, Any], profile_ids: Sequence[str]) -> str:
    doc = {"input_snapshot_id": input_snapshot_id, "pipeline_id": pipeline.pipeline_id,
           "ablation": dict(ablation), "profile_ids": list(profile_ids),
           "passes": [[p.name, p.version, p.params_sha256, p.outcome.value, p.reason,
                       p.edit_digest] for p in passes]}
    return sha256_id(canonical_json(doc))


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

def _check_params(d: PassDescriptor, params: Mapping[str, Any]) -> str | None:
    schema = d.params_schema
    for k, v in params.items():
        if k not in schema:
            return f"unknown parameter {k!r} (schema: {sorted(schema)})"
        t = schema[k]
        if not isinstance(t, type) or t is object:
            continue
        if t is float and isinstance(v, int) and not isinstance(v, bool):
            continue
        if t is int and isinstance(v, bool):
            return f"parameter {k!r} must be int, got bool"
        if not isinstance(v, t):
            return f"parameter {k!r} must be {t.__name__}, got {type(v).__name__}"
    try:
        _check_json(params)
    except TypeError as exc:
        return f"parameters are not JSON values: {exc}"
    return None


def _capture_level(view: WorkingSetView) -> int:
    cl = view.capture_level
    if cl is not None:
        return int(cl)
    return int(CaptureLevel.RELOC) if any(u.meta_status == "ok" for u in view.units()) else 0


@dataclass
class _Run:
    outcome: Outcome
    reason: str
    detail: str = ""
    affected: tuple[str, ...] = ()
    guest_origins: tuple[str, ...] = ()
    stats: Mapping[str, float] = field(default_factory=dict)
    traceback: str | None = None


class PassManager:
    def __init__(self, snapshot: Any, pipeline: Any, profiles: Iterable[Any] = (),
                 params: Mapping[str, Mapping[str, Any]] | None = None,
                 store: AnalysisStore | None = None, *, disable: Iterable[str] = (),
                 only: str | None = None, enabled_capabilities: Iterable[EditKind] = B2_EDIT_KINDS | NATIVE_EDIT_KINDS,
                 passes: PassRegistry | None = None,
                 analyses: AnalysisRegistry | None = None, ledger: Ledger | None = None,
                 clock=utc_now, bundle_id: str | None = None, b3: Any = None):
        self.base = snapshot.base if isinstance(snapshot, WorkingSet) else snapshot
        self.initial = WorkingSet.initial(snapshot)
        self.pipeline = load_pipeline(pipeline)
        self.profiles = tuple(profiles)
        self.params = {k: dict(v) for k, v in (params or {}).items()}
        self.enabled = frozenset(enabled_capabilities) - B3_EDIT_KINDS
        self.b3env = b3
        if b3 is not None:
            # B3/B4 mode (WP21): the pass-proposable B3 kinds; ADD_REGION is
            # manager-generated and ADD_RECOVERY is never admitted in v1 (§3.7)
            self.enabled = self.enabled | (B3_EDIT_KINDS - {EditKind.ADD_REGION,
                                                            EditKind.ADD_RECOVERY})
        self.passes = passes if passes is not None else default_pass_registry()
        names = {p.name for p in self.pipeline.passes}
        self.disable = frozenset(disable)
        unknown = sorted(self.disable - names)
        if unknown:
            raise PipelineError(f"--disable names passes not in pipeline "
                                f"{self.pipeline.name}: {unknown}")
        if only is not None and only not in names:
            raise PipelineError(f"--only {only!r} is not in pipeline {self.pipeline.name}")
        self.only = only
        unknown_params = sorted(set(self.params) - names)
        if unknown_params:
            raise PipelineError(f"params given for passes not in the pipeline: {unknown_params}")
        view0 = WorkingSetView(self.initial)
        if store is None:
            store = AnalysisStore(view0, analyses if analyses is not None else default_registry(),
                                  profiles=self.profiles, bundle_id=bundle_id)
        self.store = store
        self.ledger = ledger if ledger is not None else Ledger(clock=clock)
        self.b3ctx = None
        self.b3_state = None
        if b3 is not None:
            from .manager_b3 import B3Ctx, B3State

            self.b3ctx = B3Ctx(b3, store)
            self.b3_state = B3State()

    # ------------------------------------------------------------------
    def execute(self) -> Candidate:
        ws = self.initial
        if self.b3env is not None:
            from .manager_b3 import B3State

            self.b3_state = B3State()
        self.store.rebase(WorkingSetView(ws))
        if not self.store.profiles and self.profiles:
            self.store.set_profiles(self.profiles)
        records: list[PassRecord] = []
        applied: list[Edit] = []
        profiles_used: set[str] = set()
        ledger_start = len(self.ledger)
        for i, entry in enumerate(self.pipeline.passes):
            rec, edits, ws = self._one(i, entry, ws)
            records.append(rec)
            applied.extend(edits)
            profiles_used.update(rec.profiles_used)
            evidence = rec.edit_digest if rec.edit_digest else (
                sha256_id(rec.traceback.encode("utf-8")) if rec.traceback else None)
            counts = None
            if self.b3env is not None or self.pipeline.allowed_capabilities & NATIVE_EDIT_KINDS:
                counts = {k: int(v) for k, v in rec.stats.items()
                          if isinstance(v, (int, float)) and not isinstance(v, bool)
                          and float(v).is_integer()}
            self.ledger.emit("pass", f"{rec.name}@{rec.version}", LEDGER_STAGE,
                             rec.outcome.value, rec.reason, evidence, counts=counts or None)
            log.info("pass %s@%d: %s (%s)%s", rec.name, rec.version, rec.outcome.value,
                     rec.reason, f" {rec.detail}" if rec.detail else "")
        ablation = {"disable": sorted(self.disable), "only": self.only}
        pids = tuple(dict.fromkeys(p for p in (profile_id_of(v) for v in self.profiles) if p))
        verdicts = {p: ("used" if p in profiles_used else "unused") for p in pids}
        cid = _candidate_id(self.initial.snapshot_id, self.pipeline, records, ablation, pids)
        return Candidate(candidate_id=cid, input_snapshot_id=self.initial.snapshot_id,
                         pipeline=self.pipeline, working_set=ws, edits=tuple(applied),
                         passes=tuple(records), ledger=self.ledger.rows[ledger_start:],
                         profile_ids=pids, profile_verdicts=MappingProxyType(verdicts),
                         ablation=MappingProxyType(ablation), b3_state=self.b3_state)

    # ------------------------------------------------------------------
    def _one(self, i: int, entry: PipelineEntry, ws: WorkingSet
             ) -> tuple[PassRecord, list[Edit], WorkingSet]:
        params = {**entry.params, **self.params.get(entry.name, {})}
        try:
            psha = sha256_id(canonical_json(params))
        except TypeError:
            psha = sha256_id(repr(sorted(params.items())).encode())

        def record(outcome: Outcome, reason: str, detail: str = "", **kw) -> PassRecord:
            return PassRecord(index=i, name=entry.name, version=entry.version,
                              params=MappingProxyType(dict(params)), params_sha256=psha,
                              outcome=outcome, reason=reason, detail=detail,
                              generation=kw.pop("generation", ws.generation), **kw)

        if entry.name in self.disable or (self.only is not None and entry.name != self.only):
            return record(Outcome.SKIPPED_UNSUPPORTED, R.ABLATION_DISABLED,
                          "disabled" if entry.name in self.disable else f"only={self.only}"), \
                [], ws
        try:
            cls = self.passes.get(entry.name, entry.version)
        except KeyError:
            have = self.passes.versions(entry.name)
            if have:
                return record(Outcome.SKIPPED_UNSUPPORTED, R.PASS_VERSION_UNAVAILABLE,
                              f"registered versions {list(have)}"), [], ws
            return record(Outcome.SKIPPED_UNSUPPORTED, R.PASS_NOT_REGISTERED,
                          f"no pass named {entry.name!r}"), [], ws
        d: PassDescriptor = cls.descriptor
        err = _check_params(d, params)
        if err:
            return record(Outcome.FAILED, R.INVALID_PARAMS, err), [], ws

        view = WorkingSetView(ws)
        adm = self._admission(d, view)
        if adm is not None:
            return record(adm[0], adm[1], adm[2]), [], ws
        if self.b3env is not None:
            from .manager_b3 import B3WorkingSetView

            view = B3WorkingSetView(view, self.b3_state)

        logger = logging.LoggerAdapter(logging.getLogger(f"scopt.pass.{d.name}"), {})
        t0 = time.monotonic()
        ctx = ManagedPassContext(descriptor=d, view=view, store=self.store, params=params,
                                 profiles=self.profiles, deadline=t0 + d.budgets.max_wall_s,
                                 logger=logger)
        ctx.b3 = self.b3ctx
        ctx.b3_state = self.b3_state
        if d.requires_profile_phase is not None:
            pid = profile_id_of(profile_for_phase(self.profiles, d.requires_profile_phase))
            if pid:
                ctx.profiles_used.add(pid)
        allowed = self.pipeline.allowed_capabilities & self.enabled
        builder = ProposalBuilder(d, view, enabled=allowed, ctx=ctx)
        run = self._run(cls, d, view, ctx, builder)
        dur = round(time.monotonic() - t0, 6)
        if builder.violations:
            # A capability violation fails the pass even if the pass caught (or
            # re-wrapped) the EditCapabilityError.
            v = builder.violations[0]
            if run.reason != capability_reason(v):
                run = _Run(Outcome.FAILED, capability_reason(v), str(v),
                           traceback=run.traceback)
        elif run.outcome not in (Outcome.FAILED, Outcome.BUDGET_EXHAUSTED):
            if dur > d.budgets.max_wall_s:
                run = _Run(Outcome.BUDGET_EXHAUSTED, Reason.BUDGET_EXHAUSTED,
                           f"ran {dur:.3f}s > max_wall_s={d.budgets.max_wall_s}")
            elif run.outcome is not Outcome.CANDIDATE and len(builder):
                run = _Run(Outcome.FAILED, R.EDITS_WITHOUT_CANDIDATE,
                           f"pass returned {run.outcome.value} with {len(builder)} edit(s)")
        used = tuple(sorted(ctx.profiles_used))
        common = dict(guest_origins=run.guest_origins, stats=MappingProxyType(dict(run.stats)),
                      duration_s=dur, traceback=run.traceback, profiles_used=used)
        if run.outcome is not Outcome.CANDIDATE:
            return record(run.outcome, run.reason, run.detail, affected=run.affected,
                          **common), [], ws

        edits = list(builder.edits)
        bad = self._check_edits(d, edits, ws, allowed)
        if bad is not None:
            return record(bad[0], bad[1], bad[2], affected=run.affected, **common), [], ws
        if not edits:  # CANDIDATE with nothing to apply: no new generation
            return record(Outcome.CANDIDATE, run.reason, run.detail, affected=run.affected,
                          **common), [], ws
        digest = edit_digest(edits)
        affected = run.affected or tuple(sorted({u for e in edits for u in e.units}))
        prov = {"pass": d.name, "version": d.version, "params_sha256": psha,
                "edit_digest": digest, "n_edits": len(edits)}
        b3_edits = [e for e in edits if e.kind in B3_EDIT_KINDS]
        if b3_edits:
            new_state, refusal = self._apply_b3(d, params, b3_edits)
            if refusal is not None:
                common["stats"] = MappingProxyType({**dict(run.stats),
                                                    f"refused:{refusal.code}": 1})
                return record(refusal.outcome, refusal.code, refusal.detail[:1000],
                              affected=run.affected, **common), [], ws
            stats = dict(run.stats)
            stats.update(_b3_edit_counts(b3_edits, self.b3_state, new_state))
            common["stats"] = MappingProxyType(stats)
            self.b3_state = new_state
            edits_b2 = [e for e in edits if e.kind not in B3_EDIT_KINDS]
            if not edits_b2:
                return record(Outcome.CANDIDATE, run.reason, run.detail, affected=affected,
                              edit_digest=digest, n_edits=len(edits), **common), edits, ws
            edits_ws = edits_b2
        else:
            edits_ws = edits
        new_ws, changed = ws.apply(edits_ws, owner=d.name, provenance=prov)
        extra = d.invalidates & FACETS
        if extra:
            for u in {u for e in edits for u in e.units}:
                changed[u] = frozenset(changed.get(u, frozenset()) | extra)
        self.store.rebase(WorkingSetView(new_ws))
        n_inv = self.store.invalidate(changed, d.preserves)
        return record(Outcome.CANDIDATE, run.reason, run.detail, affected=affected,
                      edit_digest=digest, n_edits=len(edits), invalidated=n_inv,
                      generation=new_ws.generation, **common), edits, new_ws

    # ------------------------------------------------------------------
    def _admission(self, d: PassDescriptor, view: WorkingSetView
                   ) -> tuple[Outcome, str, str] | None:
        U, K = Outcome.SKIPPED_UNSUPPORTED, Outcome.SKIPPED_UNKNOWN
        if d.capabilities & NATIVE_EDIT_KINDS and (self.b3env is not None or
                self.pipeline.allowed_capabilities & (B3_EDIT_KINDS | {
                    EditKind.PIN, EditKind.LAYOUT_ORDER, EditKind.PLACE})):
            return U, "native_combination_unsupported", "native islands need a standalone pipeline"
        if d.domain != "HostCache":
            return U, R.DOMAIN, f"domain {d.domain!r}"
        if d.unit_scope == "region" and self.b3env is None:
            return U, R.REGION_SCOPE, "region-scoped passes need multi-TB regions (B3)"
        for name, ver in d.requires:
            if not self.store.registry.has(name, ver):
                have = self.store.registry.versions(name)
                return U, Reason.MISSING_ANALYSIS, (
                    f"{name}@>={ver} not registered" + (f" (have {list(have)})" if have else ""))
        level = _capture_level(view)
        if d.requires_capture_level > level:
            return U, Reason.CAPTURE_LEVEL, (f"needs capture level {d.requires_capture_level}, "
                                             f"snapshot has {level}")
        allowed = self.pipeline.allowed_capabilities & self.enabled
        missing = d.capabilities - allowed
        if missing:
            return U, Reason.CAPABILITY_NOT_ENABLED, (
                "capabilities " + ", ".join(sorted(k.name for k in missing))
                + f" not enabled (allowed: {sorted(k.name for k in allowed)})")
        if d.requires_profile_phase is not None:
            phase = d.requires_profile_phase
            if not self.profiles:
                return K, Reason.MISSING_PROFILE, f"no profile attached (needs phase {phase!r})"
            prof = profile_for_phase(self.profiles, phase)
            if prof is None:
                return K, Reason.MISSING_PROFILE, f"no attached profile has phase {phase!r}"
            present, reached, match = profile_phase_info(prof, phase, view.unit_ids())
            if not reached:
                return K, R.PHASE_NOT_REACHED, f"phase {phase!r} not reached"
            need = d.min_profile_match if d.min_profile_match is not None \
                else DEFAULT_MIN_PROFILE_MATCH
            if match is None:
                return K, Reason.PROFILE_INCOMPATIBLE, f"coverage: match rate of {phase!r} unknown"
            if match < need:
                return K, Reason.PROFILE_INCOMPATIBLE, (f"coverage: match {match:.4f} < "
                                                        f"min_profile_match {need}")
        return None

    def _run(self, cls: type, d: PassDescriptor, view: WorkingSetView,
             ctx: ManagedPassContext, builder: ProposalBuilder) -> _Run:
        try:
            p = cls()
            if d.unit_scope == "region":
                return self._run_regions(p, d, ctx, builder)
            if d.unit_scope == "unit":
                results = []
                for uv in view.unit_views():
                    ctx.budget()
                    results.append(p.run(uv, ctx, builder))
                return self._aggregate(results, builder)
            res = p.run(view, ctx, builder)
            if not isinstance(res, PassResult):
                return _Run(Outcome.FAILED, R.BAD_RESULT,
                            f"run() returned {type(res).__name__}, not PassResult")
            return _Run(res.outcome, res.reason, res.detail, tuple(res.affected),
                        tuple(res.guest_origins), dict(res.stats))
        except BudgetExceeded as exc:
            return _Run(Outcome.BUDGET_EXHAUSTED, Reason.BUDGET_EXHAUSTED, str(exc))
        except EditCapabilityError as exc:
            return _Run(Outcome.FAILED, capability_reason(exc), str(exc))
        except EditPreconditionError as exc:
            return _Run(Outcome.FAILED, R.EDIT_PRECONDITION, str(exc))
        except StaleFact as exc:
            return _Run(Outcome.FAILED, Reason.STALE_FACT, str(exc))
        except Exception as exc:  # noqa: BLE001 - any pass bug is a FAILED outcome
            tb = traceback.format_exc()
            tb_id = sha256_id(tb.encode("utf-8"))
            msg = f"{type(exc).__name__}: {exc}"
            return _Run(Outcome.FAILED, Reason.EXCEPTION, f"{tb_id} {msg}"[:1000], traceback=tb)

    def _run_regions(self, p: Any, d: PassDescriptor, ctx: ManagedPassContext,
                     builder: ProposalBuilder) -> _Run:
        """A region-scope pass runs once per variant of the overlay (design §3.6)."""
        from .manager_b3 import VariantView

        st = self.b3_state
        b3 = self.b3ctx
        if st is None or not st.variants:
            return _Run(Outcome.NO_CHANGE, "no_variants", "no variant in the overlay")
        results = []
        for vid in sorted(st.variants, key=lambda k: (st.variants[k].pred, k)):
            ctx.budget()
            v = st.variants[vid]
            vv = VariantView(vid, v, b3.unit(v.src_uid), b3.layout, b3.code_address)
            results.append(p.run(vv, ctx, builder))
        aggregate = self._aggregate(results, builder)
        stats = dict(aggregate.stats)
        for result in results:
            if isinstance(result, PassResult):
                for name, value in result.stats.items():
                    stats[name] = stats.get(name, 0) + value
        aggregate.stats = stats
        return aggregate

    def _apply_b3(self, d: PassDescriptor, params: Mapping[str, Any], edits: Sequence[Edit]):
        """Check and apply the B3/B4 edits of one pass to the overlay (design §3.7)."""
        from .manager_b3 import B3Refusal, hot_set_bytes

        if self.b3_state is None or self.b3ctx is None:
            return None, B3Refusal("capability_not_enabled", "not in B3 mode")
        b = d.budgets
        per_c = int(params.get("max_variants_per_c", b.max_context_versions or 2))
        total = int(params.get("max_variants", 64))
        growth = params.get("max_code_growth_bytes", b.max_code_growth_bytes or None)
        if growth is None and any(e.kind is EditKind.CLONE for e in edits):
            frac = float(params.get("growth_fraction", 0.10))
            hot = hot_set_bytes(self.store, self.b3ctx.units())
            growth = None if hot is None else int(frac * hot)
        return self.b3_state.apply(d.name, edits, self.b3ctx, max_per_c=per_c,
                                   max_variants=total, max_growth=growth)

    @staticmethod
    def _aggregate(results: Sequence[Any], builder: ProposalBuilder) -> _Run:
        """Combine per-unit results of a unit-scope pass."""
        for r in results:
            if not isinstance(r, PassResult):
                return _Run(Outcome.FAILED, R.BAD_RESULT,
                            f"run() returned {type(r).__name__}, not PassResult")
        order = [Outcome.FAILED, Outcome.BUDGET_EXHAUSTED, Outcome.CANDIDATE, Outcome.NO_CHANGE,
                 Outcome.SKIPPED_UNKNOWN, Outcome.SKIPPED_UNSUPPORTED]
        counts: dict[str, float] = {}
        for r in results:
            counts[f"units_{r.outcome.value}"] = counts.get(f"units_{r.outcome.value}", 0) + 1
        if not results:
            return _Run(Outcome.NO_CHANGE, "no_units", stats=counts)
        best = min(results, key=lambda r: order.index(r.outcome))
        affected = tuple(sorted({a for r in results for a in r.affected}))
        origins = tuple(dict.fromkeys(o for r in results for o in r.guest_origins))
        return _Run(best.outcome, best.reason, best.detail, affected, origins, counts)

    # ------------------------------------------------------------------
    def _read_current(self, r: Read, ws: WorkingSet) -> bool:
        try:
            if r.kind == "unit":
                return ws.content_hash(r.key) == r.hash
            if r.kind == "layout":
                return ws.layout_hash(r.key) == r.hash
            if r.kind == "annotations":
                return ws.annotation_hash(r.key) == r.hash
            if r.kind == "analysis":
                if r.version is not None and r.scope_kind and r.scope_id:
                    return self.store.current_hash(
                        r.key, ScopeRef(r.scope_kind, r.scope_id), r.version) == r.hash
                for res in self.store.cached():
                    if res.name == r.key and res.result_hash == r.hash:
                        return self.store.is_current(res)
                return False
        except (KeyError, LookupError, ValueError):
            return False
        return False

    def _check_edits(self, d: PassDescriptor, edits: Sequence[Edit], ws: WorkingSet,
                     allowed: frozenset[EditKind]) -> tuple[Outcome, str, str] | None:
        F = Outcome.FAILED
        # capability membership
        for e in edits:
            if e.kind not in d.capabilities:
                return F, R.EDIT_CAPABILITY, f"edit {e.seq} {e.kind.name} not in capabilities"
            if e.kind not in allowed or (e.kind in B3_EDIT_KINDS and self.b3env is None):
                return F, Reason.CAPABILITY_NOT_ENABLED, f"edit {e.seq} {e.kind.name}"
            for u in e.units:
                if u not in ws:
                    return F, R.UNKNOWN_UNIT, f"edit {e.seq}: {u}"
        # reads still current
        checked: dict[Read, bool] = {}
        for e in edits:
            for r in e.reads:
                ok = checked.get(r)
                if ok is None:
                    ok = checked[r] = self._read_current(r, ws)
                if not ok:
                    return F, Reason.STALE_EDIT, (f"edit {e.seq} {e.kind.name}: read {r.kind} "
                                                  f"{r.key} {r.hash[:24]}… is not current")
        # cited facts
        seen_facts: set[FactRef] = set()
        for e in edits:
            for f in e.facts:
                if f in seen_facts:
                    continue
                seen_facts.add(f)
                if not f.result.certified:
                    return F, Reason.STALE_FACT, f"edit {e.seq}: {f.name} is not certified"
                try:
                    self.store.check_fact(f.result)
                except StaleFact as exc:
                    return F, Reason.STALE_FACT, f"edit {e.seq}: {exc}"
        # budgets
        b = d.budgets
        touched = {u for e in edits for u in e.units}
        if b.max_units is not None and len(touched) > b.max_units:
            return (Outcome.BUDGET_EXHAUSTED, Reason.BUDGET_EXHAUSTED,
                    f"edits touch {len(touched)} units > max_units={b.max_units}")
        # relocatability, pins, conflicts
        layout_here: dict[str, int] = {}
        pinned_here: set[str] = set()
        overrides = "layout" in d.overrides
        native_ranges: dict[str, list[tuple[int, int]]] = {}
        inline_growth = 0
        for e in edits:
            if e.kind is EditKind.INLINE_CALL:
                r = e.payload
                u = ws.unit(r.target)
                if not ws.is_relocatable(u.uid) or ws.is_pinned(u.uid) or u.stub_start is not None or u.splice_applied:
                    return F, Reason.NOT_RELOCATABLE, f"native island cannot grow {u.uid}"
                ranges = native_ranges.setdefault(u.uid, [(p.splice.start_off if p.splice else p.call_off,
                                                            p.continuation_off)
                                                           for p in u.native_fragments])
                start = r.splice.start_off if r.splice else r.call_off
                if any(lo < r.continuation_off and start < hi for lo, hi in ranges):
                    return F, Reason.CONFLICT, f"overlapping inline requests in {u.uid}"
                ranges.append((start, r.continuation_off))
                inline_growth += r.growth
            if e.kind in (EditKind.LAYOUT_ORDER, EditKind.PLACE):
                for u in e.units:
                    if not ws.is_relocatable(u):
                        return F, Reason.NOT_RELOCATABLE, (
                            f"edit {e.seq} {e.kind.name}: {u} is not ok_relocatable "
                            f"({', '.join(ws.base_pin_reasons(u))})")
                    if ws.edit_pin_reasons(u) or u in pinned_here:
                        return F, R.PINNED, f"edit {e.seq} {e.kind.name}: {u} is pinned"
                    if u in layout_here:
                        return F, Reason.CONFLICT, (f"edit {e.seq}: {u} already laid out by "
                                                    f"edit {layout_here[u]} of this pass")
                    owner = ws.layout_owner(u)
                    if owner is not None and not overrides:
                        return F, Reason.CONFLICT, (f"edit {e.seq}: layout of {u} was decided "
                                                    f"by pass {owner!r}; {d.name} does not "
                                                    "declare overrides={'layout'}")
                    layout_here[u] = e.seq
            elif e.kind is EditKind.PIN:
                u = e.units[0]
                if u in layout_here:
                    return F, Reason.CONFLICT, (f"edit {e.seq}: pins {u} after edit "
                                                f"{layout_here[u]} of this pass laid it out")
                laid_out = ws.section_of(u) is not None or ws.placement(u) is not None
                if laid_out and not overrides:
                    return F, Reason.CONFLICT, (f"edit {e.seq}: {u} was laid out by pass "
                                                f"{ws.layout_owner(u)!r}; pinning it needs "
                                                "overrides={'layout'}")
                pinned_here.add(u)
        if inline_growth > b.max_code_growth_bytes:
            return (Outcome.BUDGET_EXHAUSTED, Reason.BUDGET_EXHAUSTED,
                    f"native growth {inline_growth} > {b.max_code_growth_bytes}")
        return None


def _b3_edit_counts(edits: Sequence[Edit], before: Any, after: Any) -> dict[str, float]:
    """Ledger ``counts`` of one pass's applied B3/B4 edits (design §3.7 "per-pass ledger
    rows": edits per kind, slots by kind, variants, bytes changed by its patches)."""
    out: dict[str, float] = {}
    for e in edits:
        k = f"edits:{e.kind.name}"
        out[k] = out.get(k, 0) + 1
        if e.kind is EditKind.REDIRECT_EXIT:
            kk = f"slots:{e.payload.kind}"
            out[kk] = out.get(kk, 0) + 1
        if e.kind is EditKind.PURE_REWRITE:
            p = e.payload
            out["bytes_changed"] = out.get("bytes_changed", 0) + sum(
                1 for x, y in zip(p.old_bytes, p.new_bytes) if x != y)
            out["patches"] = out.get("patches", 0) + 1
    origins = {o for e in edits for o in e.affected_origins}
    for e in edits:  # a rewrite of a variant: attribute it to the clone's source unit C
        if e.kind is EditKind.PURE_REWRITE and e.payload.target in after.variants:
            src = after.variants[e.payload.target].src_uid
            origins.add(f"variant-of:{src}")
    out["origins"] = len(origins)
    out["deps_added"] = len({repr(d) for e in edits for d in e.dependency_additions})
    out["variants_total"] = len(after.variants)
    out["guarded_exits_total"] = len(after.exits)
    return out
