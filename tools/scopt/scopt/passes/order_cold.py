"""``order-cold@1`` (design §5.10, §5.7, §9): order the non-hot sections. WP11.

Reads the ``hotness`` annotations of ``hotcold-partition`` and emits one
``layout_order`` per section, each in **original slot order**::

    warm     <- class warm
    unknown  <- class unknown, and units without a hotness annotation
    boot     <- class boot_only
    cold     <- class cold_zero

``unknown`` is never put in ``cold`` (§9). NativeLinker places the sections in
the order hot -> warm -> unknown -> boot -> cold after the pinned obstacles.

Param ``hot_fallback`` (default true): hot units that no earlier pass laid
out (``cluster-hot`` disabled, or it skipped) are put in the ``hot`` section
in slot order, so that ``--disable cluster-hot`` is the same partition
without clustering rather than leaving the hot units at their old offsets.

Only ``ok_relocatable``, unpinned units that no earlier pass laid out are
ordered; pinned / no_meta / incomplete units are never in an order request
(they stay where they are). Each one this pass would otherwise have ordered
is listed in ``affected`` as ``skipped_unsupported:<reason>:<UnitId>`` (as in
``cluster-hot``; hot units only when ``hot_fallback`` makes them this pass's
job) and counted in ``stats``.

Outcomes: SKIPPED_UNKNOWN ``missing_hotness`` when no unit carries a
``hotness`` annotation; NO_CHANGE ``nothing_to_order``; else CANDIDATE
``ordered``.
"""

from __future__ import annotations

from ..analysis.hotness import SECTION_OF_CLASS
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult
from .hotcold import ANNOTATION_KEY, skipped_unsupported

__all__ = ["OrderCold", "PASSES", "SECTIONS"]

SECTIONS = ("hot", "warm", "unknown", "boot", "cold")


class OrderCold:
    descriptor = PassDescriptor(
        name="order-cold", version=1, unit_scope="snapshot",
        requires=(("annotations", 1),),
        capabilities=frozenset({EditKind.LAYOUT_ORDER}), budgets=Budgets(max_wall_s=30.0),
        preserves=frozenset({"refs", "recovery", "cfg.known_edges", "profile.counts",
                             "annotations", "hotness"}),
        invalidates=frozenset({"layout"}),
        params_schema={"hot_fallback": bool})

    def run(self, snap, ctx, edits) -> PassResult:
        fallback = bool(ctx.params.get("hot_fallback", True))
        ann = ctx.analysis("annotations", snap).value
        classes = {u: a.get(ANNOTATION_KEY) for u, a in ann.items() if ANNOTATION_KEY in a}
        if not classes:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "missing_hotness",
                              "no unit carries a 'hotness' annotation (hotcold-partition "
                              "did not run)")
        lists: dict[str, list[str]] = {s: [] for s in SECTIONS}
        fixed = laid_out = unannotated = 0
        skipped: list[str] = []
        for u in snap.unit_ids():
            cls = classes.get(u)
            if cls is None:
                unannotated += 1
                cls = "unknown"
            sec = SECTION_OF_CLASS.get(cls, "unknown")
            if not snap.is_relocatable(u) or snap.is_pinned(u):
                fixed += 1
                if sec != "hot" or fallback:
                    skipped += skipped_unsupported(snap, u)
                continue
            if snap.section_of(u) is not None or snap.placement(u) is not None:
                laid_out += 1
                continue
            if sec == "hot" and not fallback:
                continue
            lists[sec].append(u)
        n = 0
        for sec in SECTIONS:
            if lists[sec]:
                ctx.budget()
                edits.layout_order(sec, lists[sec])
                n += len(lists[sec])
        stats = {**{f"section_{s}": float(len(v)) for s, v in lists.items()},
                 "pinned_or_not_relocatable": float(fixed),
                 "already_laid_out": float(laid_out), "unannotated": float(unannotated)}
        if not n:
            return PassResult(Outcome.NO_CHANGE, "nothing_to_order",
                              affected=tuple(sorted(skipped)), stats=stats)
        return PassResult(Outcome.CANDIDATE, "ordered",
                          affected=tuple(sorted(u for v in lists.values() for u in v))
                          + tuple(sorted(skipped)),
                          stats=stats)


PASSES = (OrderCold,)
