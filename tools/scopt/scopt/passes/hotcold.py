"""``hotcold-partition@1`` (design §5.10, §9): B2 hot/cold classification. WP11.

Annotates every unit with ``hotness`` = one of ``hot``, ``warm``,
``boot_only``, ``cold_zero``, ``unknown`` (rule: :func:`scopt.analysis.hotness.classify`).
It only annotates: no code is removed and no unit is moved; ``cluster-hot``
and ``order-cold`` turn the classes into LAYOUT_ORDER edits.

Params: ``phase`` (default ``steady``), ``coverage`` (default 0.99, share of
the phase's exact entries the hot set covers).

Outcomes: SKIPPED_UNKNOWN ``no_exact_counts`` when the phase has no exact
count at all (e.g. a ``lower_bound`` profile); otherwise CANDIDATE
``classified`` with the hot units in ``affected`` and the class histogram in
``stats``. Pinned/no_meta units are classified too (annotations are
metadata); the layout passes never order them.
"""

from __future__ import annotations

from ..analysis.hotness import DEFAULT_COVERAGE, DEFAULT_PHASE, classify, phase_profile_counts
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult

__all__ = ["HotColdPartition", "PASSES", "ANNOTATION_KEY", "unsupported_reasons",
           "skipped_unsupported"]

ANNOTATION_KEY = "hotness"


def unsupported_reasons(snap, uid: str) -> tuple[str, ...]:
    """Reason codes of a unit a layout pass may not order (pinned, not relocatable,
    ``no_meta``/``incomplete``): its pin reasons without the ``@<site>`` detail, and
    ``meta_<status>`` as ``<status>`` (``meta_no_meta`` -> ``no_meta``)."""
    out: list[str] = []
    for r in snap.pin_reasons(uid) or ("not_relocatable",):
        r = r.split("@", 1)[0]
        if r.startswith("meta_"):
            r = r[len("meta_"):]
        if r not in out:
            out.append(r)
    return tuple(out)


def skipped_unsupported(snap, uid: str) -> list[str]:
    """``affected`` entries ``skipped_unsupported:<reason>:<UnitId>`` (design §7 F-B2-4:
    layout reports a pinned unit as ``skipped_unsupported:unexplained_reference``)."""
    return [f"skipped_unsupported:{r}:{uid}" for r in unsupported_reasons(snap, uid)]


class HotColdPartition:
    """B2 classification. Annotates only; cold code is never removed."""

    descriptor = PassDescriptor(
        name="hotcold-partition", version=1, unit_scope="snapshot",
        requires=(("profile.counts", 1), ("refs", 1)), requires_profile_phase="steady",
        min_profile_match=0.95, capabilities=frozenset({EditKind.ANNOTATE}),
        budgets=Budgets(max_wall_s=30.0),
        preserves=frozenset({"refs", "layout", "cfg.known_edges", "profile.counts", "recovery",
                             "hotness", "locality"}),
        params_schema={"phase": str, "coverage": float})

    def run(self, snap, ctx, edits) -> PassResult:
        phase = ctx.params.get("phase", DEFAULT_PHASE)
        cov = float(ctx.params.get("coverage", DEFAULT_COVERAGE))
        pc, _ = phase_profile_counts(ctx, snap, phase)  # same profile as cluster-hot/x_locality
        steady = pc.phase(phase)
        boot = pc.phase("boot")
        if not any(c.status == "exact" for c in steady.values()):
            return PassResult(Outcome.SKIPPED_UNKNOWN, "no_exact_counts",
                              f"phase {phase!r} has no exact count in profile "
                              f"{pc.profile_id}")
        h = classify(snap.unit_ids(), steady, boot, coverage=cov, phase=phase)
        pinned_hot = 0
        for uid, cls in h.classes.items():
            ctx.budget()
            edits.annotate(uid, ANNOTATION_KEY, cls)
            if cls == "hot" and (not snap.is_relocatable(uid) or snap.is_pinned(uid)):
                pinned_hot += 1
        stats = {**{k: float(v) for k, v in h.stats.items()},
                 "hot": float(len(h.hot_order)), "coverage": round(h.achieved, 6),
                 "pinned_hot": float(pinned_hot)}
        return PassResult(Outcome.CANDIDATE, "classified", affected=tuple(sorted(h.hot_order)),
                          stats=stats)


PASSES = (HotColdPartition,)
