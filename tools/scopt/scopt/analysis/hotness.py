"""``hotness.v1`` analysis and the B2 classification rule (design §5.3, §5.10, §9). WP11.

Classes (one per unit)::

    hot        exact steady entries, inside the hottest prefix covering
               ``coverage`` of the phase's exact entries
    warm       exact steady entries > 0 outside the hot prefix, or a
               ``lower_bound`` steady count > 0 (priority only, see below)
    boot_only  exact steady entries == 0 and boot entries > 0 (exact or lower_bound)
    cold_zero  exact steady entries == 0 and no boot evidence of execution
    unknown    every other case: ``unknown`` status (not collected, not in the
               domain, phase not reached) or a ``lower_bound`` count of 0

Rules from §9 that this module enforces:

* **unknown is never cold.** Only an *exact* zero can make a unit
  ``boot_only``/``cold_zero``; absence of evidence gives ``unknown``.
* **lower_bound is priority only.** A lower bound never enters the hot-set
  computation (its total is not known) and never makes a unit cold. A
  positive lower bound proves the unit ran in the phase, so it raises the
  unit to ``warm`` (the §5.10 example puts every non-exact unit in
  ``unknown``; ``warm`` is placed right after ``hot`` and before ``unknown``,
  so this only raises its placement priority). A lower bound of 0 stays
  ``unknown``.

The hot prefix is the §5.10 rule: exact units sorted by ``(-entries, UnitId)``
(deterministic), added while the accumulated share is below ``coverage``;
zero-entry units are never hot.

:func:`classify` is the pure rule (the ``hotcold-partition`` pass calls it
with its params). The registered analysis ``hotness@1`` applies it with the
defaults (phase ``steady``, coverage 0.99) to ``profile.counts@1``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .api import AnalysisSpec, ScopeRef

__all__ = [
    "NAME", "VERSION", "CLASSES", "SECTION_OF_CLASS", "DEFAULT_PHASE", "DEFAULT_COVERAGE",
    "Hotness", "classify", "phase_profile_counts", "HOTNESS", "ANALYSES",
]

NAME = "hotness"
VERSION = 1
CLASSES: tuple[str, ...] = ("hot", "warm", "boot_only", "cold_zero", "unknown")
#: NativeLinker section (§5.5, §5.7) of each class. ``unknown`` is its own
#: section, placed before ``boot``/``cold``: it is never treated as cold.
SECTION_OF_CLASS: Mapping[str, str] = MappingProxyType({
    "hot": "hot", "warm": "warm", "unknown": "unknown", "boot_only": "boot",
    "cold_zero": "cold"})
DEFAULT_PHASE = "steady"
DEFAULT_COVERAGE = 0.99


def _status(c: Any) -> str:
    return "unknown" if c is None else str(getattr(c, "status", "unknown"))


def _value(c: Any) -> int | None:
    return None if c is None else getattr(c, "value", None)


@dataclass(frozen=True)
class Hotness:
    phase: str
    coverage: float
    classes: Mapping[str, str]  # uid -> class, slot order
    hot_order: tuple[str, ...]  # the hot prefix, hottest first
    exact_total: int  # Σ exact entries in the phase
    hot_entries: int
    stats: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def achieved(self) -> float:
        return self.hot_entries / self.exact_total if self.exact_total else 0.0

    def of(self, cls: str) -> tuple[str, ...]:
        return tuple(u for u, c in self.classes.items() if c == cls)

    def to_json(self) -> dict[str, Any]:
        return {"phase": self.phase, "coverage": self.coverage,
                "classes": dict(self.classes), "hot_order": list(self.hot_order),
                "exact_total": self.exact_total, "hot_entries": self.hot_entries,
                "stats": dict(self.stats)}


def classify(unit_ids: Iterable[str], steady: Mapping[str, Any], boot: Mapping[str, Any], *,
             coverage: float = DEFAULT_COVERAGE, phase: str = DEFAULT_PHASE) -> Hotness:
    """Classify ``unit_ids`` from ``{uid: Count}`` maps of the phase and of ``boot``.

    ``Count`` objects need ``value`` and ``status`` (``exact``,
    ``lower_bound``, ``unknown``); a uid missing from a map is unknown.
    """
    if not 0.0 < coverage <= 1.0:
        raise ValueError(f"coverage must be in (0, 1], got {coverage!r}")
    uids = tuple(unit_ids)
    exact = {u: int(_value(steady.get(u)) or 0) for u in uids
             if _status(steady.get(u)) == "exact"}
    total = sum(exact.values())
    acc = 0
    hot: list[str] = []
    for u, v in sorted(exact.items(), key=lambda kv: (-kv[1], kv[0])):
        if total and acc / total >= coverage:
            break
        if v > 0:
            hot.append(u)
            acc += v
    hot_set = set(hot)
    classes: dict[str, str] = {}
    by_status = {"exact": 0, "lower_bound": 0, "unknown": 0}
    for u in uids:
        c = steady.get(u)
        st = _status(c)
        by_status[st if st in by_status else "unknown"] += 1
        if u in hot_set:
            cls = "hot"
        elif st == "exact":
            if int(_value(c) or 0) > 0:
                cls = "warm"
            else:
                b = boot.get(u)
                bst = _status(b)
                if bst in ("exact", "lower_bound") and int(_value(b) or 0) > 0:
                    cls = "boot_only"
                else:
                    cls = "cold_zero"
        elif st == "lower_bound" and int(_value(c) or 0) > 0:
            cls = "warm"  # priority only: a proven execution, never hot, never cold
        else:
            cls = "unknown"
        classes[u] = cls
    counts = {k: 0 for k in CLASSES}
    for cls in classes.values():
        counts[cls] += 1
    stats = {**{f"class_{k}": v for k, v in counts.items()},
             **{f"status_{k}": v for k, v in by_status.items()},
             "exact_entries": total, "hot_entries": acc}
    return Hotness(phase, float(coverage), MappingProxyType(classes), tuple(hot), total, acc,
                   MappingProxyType(stats))


def phase_profile_counts(ctx: Any, scope: Any, phase: str) -> tuple[Any, Any]:
    """``(ProfileCounts, view)`` of the one profile every B2 consumer uses for ``phase``.

    With several attached profiles, ``profile.counts@1`` is computed from the
    first one (``ctx.profile(None)``), while edges and the sidecar locality
    model come from the first profile that has ``phase`` (reached preferred,
    :func:`scopt.analysis.store.profile_for_phase`). B2 reads counts, boot
    counts and edges from that same profile: ``ctx.profile(phase)``. When it
    is not the one ``profile.counts@1`` was computed from, its counts are
    computed here from that view. ``view`` is None when no profile has the
    phase (then the ``profile.counts@1`` result is returned unchanged).
    """
    from .profile import compute_profile_counts
    from .store import profile_id_of

    pc = ctx.analysis("profile.counts", scope).value
    view = ctx.profile(phase)
    if view is not None and profile_id_of(view) != pc.profile_id:
        pc = compute_profile_counts(view, pc.unit_ids, snapshot_id=pc.snapshot_id)
    return pc, view


def _compute(ctx: Any, scope: ScopeRef) -> Hotness:
    if scope.kind != "snapshot":
        raise ValueError(f"{NAME} is snapshot-scoped, got {scope.kind}")
    pc, _ = phase_profile_counts(ctx, scope, DEFAULT_PHASE)
    return classify(ctx.unit_ids(), pc.phase(DEFAULT_PHASE), pc.phase("boot"),
                    coverage=DEFAULT_COVERAGE, phase=DEFAULT_PHASE)


HOTNESS = AnalysisSpec(name=NAME, version=VERSION, scope="snapshot",
                       requires=(("profile.counts", 1),), inputs=frozenset({"profile"}),
                       compute=_compute, result_schema=Hotness)
ANALYSES = (HOTNESS,)
