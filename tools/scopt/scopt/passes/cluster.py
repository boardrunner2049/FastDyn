"""``cluster-hot@1`` (design §5.10): Pettis-Hansen chain merge of the hot units. WP11.

Input: the ``hotness`` annotations of ``hotcold-partition`` (only units
annotated ``hot``) and the exact profile edges of the phase.

Edge weights
------------
For every ordered pair ``(src, dst)`` of distinct movable hot units, the
weight is the sum of the phase's ``chain0``, ``chain1`` and ``lookup_hit``
edge counts (param ``kinds``) whose status is ``exact`` and that carry no
flag (an ``irq``/``exception``/``longjmp`` edge is not a transfer the code
makes on its own). ``chain_weight`` is the part from ``chain0``/``chain1``
(the transfers production chains directly, i.e. fallthrough-like).

Chain merge (Pettis-Hansen bottom-up positioning)
-------------------------------------------------
Every movable hot unit starts as a one-unit chain. Edges are visited by
``(-weight, -chain_weight, src, dst)`` (heaviest first, then the more
fallthrough-like, then by UnitId). An edge merges the chain that *ends* in
``src`` with the chain that *starts* with ``dst`` (``src`` is placed directly
before ``dst``); an edge whose endpoints are not a tail and a head of two
different chains is skipped.

Chain order: hottest chain first, by ``(-Σ exact entries, first UnitId)``.
The result is one ``layout_order("hot", ...)``.

Units that are hot but not movable are left out and stay where they are.
A pinned / not ``ok_relocatable`` one (``no_meta``, ``incomplete``,
``unexplained_reference``, ...) is listed in ``affected`` as
``skipped_unsupported:<reason>:<UnitId>``, one entry per reason (design §7
F-B2-4); one that an earlier pass already laid out as
``skipped_laid_out:<UnitId>``. ``detail`` summarises the reasons.

Counts (``heat``) and edges come from the one profile that has the phase
(:func:`scopt.analysis.hotness.phase_profile_counts`), the same one
``hotcold-partition`` and the sidecar locality model use.

Outcomes: SKIPPED_UNKNOWN ``missing_hotness`` when no unit carries a
``hotness`` annotation (``hotcold-partition`` did not run); NO_CHANGE
``no_movable_hot_units``; else CANDIDATE ``clustered``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..analysis.hotness import phase_profile_counts
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult
from .hotcold import ANNOTATION_KEY, skipped_unsupported

__all__ = ["ClusterHot", "PASSES", "DEFAULT_KINDS", "edge_weights", "pettis_hansen"]

DEFAULT_KINDS = ("chain0", "chain1", "lookup_hit")
CHAIN_KINDS = frozenset({"chain0", "chain1"})


def edge_weights(edges: Iterable[Mapping[str, Any]], nodes: set[str],
                 kinds: Iterable[str] = DEFAULT_KINDS) -> dict[tuple[str, str], list[int]]:
    """``{(src, dst): [weight, chain_weight]}`` over exact, unflagged edges of ``kinds``."""
    ks = set(kinds)
    w: dict[tuple[str, str], list[int]] = {}
    for e in edges:
        k = e.get("kind")
        if k not in ks or e.get("flags") or e.get("status", "exact") != "exact":
            continue
        s, d = e.get("src"), e.get("dst")
        if s is None or s == d or s not in nodes or d not in nodes:
            continue
        n = int(e.get("count", 0))
        if n <= 0:
            continue
        cell = w.setdefault((s, d), [0, 0])
        cell[0] += n
        if k in CHAIN_KINDS:
            cell[1] += n
    return w


def pettis_hansen(nodes: Iterable[str], weights: Mapping[tuple[str, str], Any],
                  heat: Mapping[str, int]) -> tuple[list[list[str]], int]:
    """Chains (hottest first) and the edge weight made adjacent by the merges."""
    chains: dict[str, list[str]] = {}
    chain_of: dict[str, str] = {}
    for u in sorted(nodes):
        chains[u] = [u]
        chain_of[u] = u
    merged = 0
    for (s, d), wv in sorted(weights.items(), key=lambda kv: (-kv[1][0], -kv[1][1],
                                                               kv[0][0], kv[0][1])):
        cs, cd = chain_of[s], chain_of[d]
        if cs == cd or chains[cs][-1] != s or chains[cd][0] != d:
            continue
        chains[cs].extend(chains[cd])
        for u in chains[cd]:
            chain_of[u] = cs
        del chains[cd]
        merged += int(wv[0])
    out = sorted(chains.values(), key=lambda c: (-sum(int(heat.get(u, 0)) for u in c), c[0]))
    return out, merged


class ClusterHot:
    descriptor = PassDescriptor(
        name="cluster-hot", version=1, unit_scope="snapshot",
        requires=(("annotations", 1), ("profile.counts", 1)),
        requires_profile_phase="steady", min_profile_match=0.95,
        capabilities=frozenset({EditKind.LAYOUT_ORDER}), budgets=Budgets(max_wall_s=30.0),
        preserves=frozenset({"refs", "recovery", "cfg.known_edges", "profile.counts",
                             "annotations", "hotness"}),
        invalidates=frozenset({"layout"}),
        params_schema={"phase": str, "kinds": list})

    def run(self, snap, ctx, edits) -> PassResult:
        phase = ctx.params.get("phase", "steady")
        kinds = tuple(ctx.params.get("kinds", DEFAULT_KINDS))
        bad = sorted(set(kinds) - set(DEFAULT_KINDS))
        if bad:
            raise ValueError(f"cluster-hot: unsupported edge kinds {bad}")
        ann = ctx.analysis("annotations", snap).value
        classes = {u: a.get(ANNOTATION_KEY) for u, a in ann.items() if ANNOTATION_KEY in a}
        if not classes:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "missing_hotness",
                              "no unit carries a 'hotness' annotation (hotcold-partition "
                              "did not run)")
        hot = [u for u in snap.unit_ids() if classes.get(u) == "hot"]
        movable: list[str] = []
        skipped: list[str] = []  # affected entries, one per (unit, reason)
        n_skipped = 0
        for u in hot:
            if not snap.is_relocatable(u) or snap.is_pinned(u):
                skipped += skipped_unsupported(snap, u)
                n_skipped += 1
            elif snap.section_of(u) is not None or snap.placement(u) is not None:
                skipped.append(f"skipped_laid_out:{u}")
                n_skipped += 1
            else:
                movable.append(u)
        why: dict[str, int] = {}
        for a in skipped:
            k = ":".join(a.split(":", 2)[:2]) if a.startswith("skipped_unsupported:") else \
                "skipped_laid_out"
            why[k] = why.get(k, 0) + 1
        detail = (f"{n_skipped} hot unit(s) not ordered, stay in place: " + ", ".join(
            f"{k} x{v}" for k, v in sorted(why.items()))) if n_skipped else ""
        if not movable:
            return PassResult(Outcome.NO_CHANGE, "no_movable_hot_units", detail,
                              affected=tuple(sorted(skipped)),
                              stats={"hot": float(len(hot)), "skipped": float(n_skipped)})
        pcs, view = phase_profile_counts(ctx, snap, phase)
        pc = pcs.phase(phase)
        heat = {u: int(pc[u].value or 0) for u in movable
                if u in pc and pc[u].status == "exact"}
        edges = view.edges(phase) if view is not None else ()
        w = edge_weights(edges, set(movable), kinds)
        ctx.budget()
        chains, merged = pettis_hansen(movable, w, heat)
        order = [u for c in chains for u in c]
        edits.layout_order("hot", order)
        total = sum(v[0] for v in w.values())
        return PassResult(
            Outcome.CANDIDATE, "clustered", detail,
            affected=tuple(sorted(order)) + tuple(sorted(skipped)),
            stats={"hot": float(len(hot)), "ordered": float(len(order)),
                   "skipped": float(n_skipped), "chains": float(len(chains)),
                   "edges": float(len(w)), "edge_weight": float(total),
                   "merged_weight": float(merged),
                   "merged_fraction": round(merged / total, 6) if total else 0.0})


PASSES = (ClusterHot,)
