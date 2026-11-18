"""``profile.counts.v1`` (design §5.3): per-phase counts with status, per unit.

The value is a :class:`ProfileCounts`: for every unit of the snapshot (slot
order) and every phase the profile defines, a :class:`~scopt.formats.profile_v1.Count`
for ``entries``, ``executions`` (entries − aborted), ``aborted``, ``longjmp``
and ``lookup_hits``. Unknown is never zero: without a profile every count is
``unknown/not_collected``; a unit outside the profiled snapshot is
``unknown/not_in_domain``; an unreached phase is ``unknown/phase_not_reached``;
a phase the profile does not define is ``unknown/not_collected``.

``ProfileCounts.phase(name)`` returns ``{UnitId: Count}`` of entries, which is
what the §5.10 example pass reads (``ctx.analysis("profile.counts",
snap).value.phase("steady")``).

The AnalysisStore (WP9) calls :data:`SPEC`'s ``compute(ctx, scope)``. The
context must expose the ProfileView as ``ctx.profile_view`` (attribute or
zero-argument callable), or ``ctx.profile(None)``; ``None`` means no profile
is attached. The unit list comes from ``ctx.snapshot.units()`` (or
``ctx.snapshot_view``), else from the view's target. The pure function
:func:`compute_profile_counts` does the work and is what tests call.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ..canon import canonical_json, sha256_id
from ..formats.enums import CountStatus, UnknownReason
from ..formats.profile_v1 import Count, ProfileView
from .api import AnalysisSpec, ScopeRef

__all__ = ["NAME", "VERSION", "FIELDS", "ProfileCounts", "compute_profile_counts", "SPEC"]

NAME = "profile.counts"
VERSION = 1
FIELDS = ("entries", "executions", "aborted", "longjmp", "lookup_hits")


@dataclass(frozen=True)
class ProfileCounts:
    profile_id: str | None
    snapshot_id: str
    unit_ids: tuple[str, ...]
    phases: tuple[str, ...]  # phase names defined by the profile, in id order
    table: Mapping[str, Mapping[str, Mapping[str, Count]]]  # phase -> field -> uid -> Count
    coverage: float | None = None

    def _missing(self) -> Mapping[str, Count]:
        reason = (UnknownReason.NOT_COLLECTED.value)
        c = Count(None, CountStatus.UNKNOWN.value, reason)
        return MappingProxyType({u: c for u in self.unit_ids})

    def counts(self, phase: str, field: str = "entries") -> Mapping[str, Count]:
        if field not in FIELDS:
            raise KeyError(f"unknown count field {field!r}")
        t = self.table.get(phase)
        return self._missing() if t is None else t[field]

    def phase(self, phase: str) -> Mapping[str, Count]:
        """``{UnitId: Count}`` of entries for ``phase``."""
        return self.counts(phase, "entries")

    def executions(self, phase: str) -> Mapping[str, Count]:
        return self.counts(phase, "executions")

    def to_json(self) -> dict[str, Any]:
        return {
            "name": NAME, "version": VERSION, "profile_id": self.profile_id,
            "snapshot_id": self.snapshot_id, "phases": list(self.phases),
            "coverage": None if self.coverage is None else round(self.coverage, 6),
            "units": list(self.unit_ids),
            "table": {ph: {f: {u: c.to_json() for u, c in m.items()} for f, m in t.items()}
                      for ph, t in self.table.items()},
        }

    def result_hash(self) -> str:
        return sha256_id(canonical_json(self.to_json()))


def compute_profile_counts(view: ProfileView | None, unit_ids: Iterable[str] | None = None, *,
                           snapshot_id: str | None = None) -> ProfileCounts:
    """Counts for ``unit_ids`` (default: the view's target units) from ``view``."""
    if view is None:
        if unit_ids is None or snapshot_id is None:
            raise ValueError("without a ProfileView, unit_ids and snapshot_id are required")
        return ProfileCounts(None, snapshot_id, tuple(unit_ids), (), MappingProxyType({}))
    uids = tuple(view.target.unit_ids if unit_ids is None else unit_ids)
    table: dict[str, Mapping[str, Mapping[str, Count]]] = {}
    for ph in view.phase_names:
        table[ph] = MappingProxyType({
            f: MappingProxyType({u: view.count(u, ph, f) for u in uids}) for f in FIELDS})
    return ProfileCounts(view.profile_id, snapshot_id or view.target.snapshot_id, uids,
                         view.phase_names, MappingProxyType(table), view.coverage)


def _ctx_view(ctx: Any) -> ProfileView | None:
    v = getattr(ctx, "profile_view", None)
    if callable(v) and not isinstance(v, ProfileView):
        v = v()
    if v is None and hasattr(ctx, "profile"):
        try:
            v = ctx.profile(None)
        except (TypeError, KeyError, ValueError):
            v = None
    return v


def _ctx_units(ctx: Any) -> tuple[tuple[str, ...] | None, str | None]:
    snap = getattr(ctx, "snapshot", None) or getattr(ctx, "snapshot_view", None)
    if snap is not None and hasattr(snap, "units"):
        return tuple(u.uid for u in snap.units()), getattr(snap, "snapshot_id", None)
    return None, getattr(ctx, "snapshot_id", None)


def _compute(ctx: Any, scope: ScopeRef) -> ProfileCounts:
    if scope.kind != "snapshot":
        raise ValueError(f"{NAME} is snapshot-scoped, got {scope.kind}")
    uids, sid = _ctx_units(ctx)
    return compute_profile_counts(_ctx_view(ctx), uids, snapshot_id=sid or scope.id)


SPEC = AnalysisSpec(
    name=NAME, version=VERSION, scope="snapshot", requires=(),
    inputs=frozenset({"profile"}), compute=_compute, result_schema=ProfileCounts)
