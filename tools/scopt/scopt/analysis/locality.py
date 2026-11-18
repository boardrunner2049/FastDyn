"""Static locality **model** (design §5.3 ``locality.v1``, §7 F-B2-7). WP11.

Everything here is a *model*: arena offsets of a layout combined with profile
entry counts. It is not a measurement of instruction-cache, iTLB or branch
behaviour (the host has no usable hardware counters), and every output
carries :data:`LABEL` saying so.

For one layout (``uid -> (tb, tc, tc_size, end)``) and one profile phase:

* **coverage rows** for 50 %, 90 % and 99 % of the phase's *exact* entries:
  the hottest exact units (``(-entries, UnitId)`` order) whose entries first
  reach the target, with

  - ``hot_code_bytes``: Σ host code bytes ``tc_size`` (layout-invariant);
  - ``hot_bytes``: Σ arena footprint ``end - tb`` (TB struct slot, code and
    search data; layout-invariant);
  - ``lines64_code`` / ``lines64``: distinct 64 B lines touched by
    ``[tc, tc+tc_size)`` / ``[tb, end)``;
  - ``pages4k_code`` / ``pages4k``: the same for 4 KiB pages;
  - ``span_bytes``: from the lowest ``tb`` to the highest ``end`` of the set.

  In a B2 artifact every TB starts on a 64 B boundary and moves by a multiple
  of 64 (§4.7), so no two units share a 64 B line and the line counts cannot
  change under B2 layout; only pages and span can. The line counts are
  reported anyway, so that this is visible rather than assumed.
* **executed**: the same footprint for every unit with exact entries > 0.
* **edges**: exact ``chain0``/``chain1``/``lookup_hit`` edges (no IRQ /
  exception / longjmp flag) between units of the layout: total weight, the
  weighted mean distance in bytes from the end of the source to the start of
  the destination, and the weight share whose endpoints are adjacent in the
  layout (destination ``tb`` = source ``end`` rounded up to 64) or on the
  same 4 KiB page.
* **non_exact**: units whose phase count is ``lower_bound`` (their observed
  entries reported separately, never mixed into the exact total) or
  ``unknown`` (by reason). Unknown is never treated as zero.

:func:`locality_model` is the pure function. :data:`LOCALITY`
(``locality@1``) evaluates it for the WorkingSet-aware ``layout()`` of the
snapshot being optimized (the original offsets plus any explicit PLACE; the
section orders are only turned into offsets by NativeLinker).
:func:`locality_sidecar` evaluates it for the input layout and for the
linked candidate; :class:`scopt.validate.ValidateAndPackage` stores that in
the artifact sidecar as ``x_locality`` and ``scopt report`` renders it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .api import AnalysisSpec, ScopeRef

__all__ = [
    "NAME", "VERSION", "LABEL", "COVERAGES", "EDGE_KINDS", "locality_model",
    "snapshot_layout", "link_layout", "locality_sidecar", "LOCALITY", "ANALYSES",
]

NAME = "locality"
VERSION = 1
LABEL = ("MODEL (static arena offsets x profile entry counts); not a measurement of "
         "cache, TLB or branch behaviour")
COVERAGES: tuple[float, ...] = (0.50, 0.90, 0.99)
EDGE_KINDS = frozenset({"chain0", "chain1", "lookup_hit"})
LINE = 64
PAGE = 4096

Layout = Mapping[str, tuple[int, int, int, int]]  # uid -> (tb, tc, tc_size, end)


def _cover(ranges: Iterable[tuple[int, int]], gran: int) -> int:
    blocks: set[int] = set()
    for a, b in ranges:
        if b > a:
            blocks.update(range(a // gran, (b - 1) // gran + 1))
    return len(blocks)


def _footprint(layout: Layout, uids: Sequence[str]) -> dict[str, Any]:
    code = [(layout[u][1], layout[u][1] + layout[u][2]) for u in uids]
    full = [(layout[u][0], layout[u][3]) for u in uids]
    lo = min((a for a, _ in full), default=0)
    hi = max((b for _, b in full), default=0)
    return {"units": len(uids), "hot_code_bytes": sum(b - a for a, b in code),
            "hot_bytes": sum(b - a for a, b in full),
            "lines64_code": _cover(code, LINE), "lines64": _cover(full, LINE),
            "pages4k_code": _cover(code, PAGE), "pages4k": _cover(full, PAGE),
            "span_bytes": hi - lo}


def _round_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a


def locality_model(layout: Layout, counts: Mapping[str, Any], *,
                   edges: Iterable[Mapping[str, Any]] = (), phase: str = "steady",
                   coverages: Sequence[float] = COVERAGES,
                   profile_id: str | None = None) -> dict[str, Any]:
    """The locality model of ``layout`` (see the module docstring).

    ``counts`` maps UnitId to a ``Count`` (``value``/``status``/``reason``)
    for ``phase``; ``edges`` are Profile v1 edge rows of that phase.
    """
    exact: dict[str, int] = {}
    lb_units = lb_entries = 0
    unknown: dict[str, int] = {}
    for uid in layout:
        c = counts.get(uid)
        st = getattr(c, "status", None) if c is not None else None
        if st == "exact":
            exact[uid] = int(c.value or 0)
        elif st == "lower_bound":
            lb_units += 1
            lb_entries += int(c.value or 0)
        else:
            r = (getattr(c, "reason", None) if c is not None else None) or "not_collected"
            unknown[r] = unknown.get(r, 0) + 1
    total = sum(exact.values())
    order = [(u, n) for u, n in sorted(exact.items(), key=lambda kv: (-kv[1], kv[0])) if n > 0]
    out: dict[str, Any] = {
        "label": LABEL, "phase": phase, "profile_id": profile_id,
        "units": len(layout), "exact_units": len(exact), "exact_entries": total,
        "non_exact": {"lower_bound": {"units": lb_units, "entries_observed": lb_entries},
                      "unknown": {"units": sum(unknown.values()),
                                  "reasons": dict(sorted(unknown.items()))}},
    }
    rows = []
    for cov in coverages:
        target = cov * total
        hot: list[str] = []
        acc = 0
        for u, n in order:
            if acc >= target:
                break
            hot.append(u)
            acc += n
        rows.append({"coverage": cov, "entries": acc,
                     "fraction": (acc / total) if total else None, **_footprint(layout, hot)})
    out["coverage"] = rows
    out["executed"] = _footprint(layout, [u for u, _ in order])
    w_tot = w_adj = w_page = 0
    dist = 0
    n_edges = 0
    for e in edges:
        if e.get("kind") not in EDGE_KINDS or e.get("flags") or e.get("status", "exact") != "exact":
            continue
        s, d = e.get("src"), e.get("dst")
        if s is None or s == d or s not in layout or d not in layout:
            continue
        w = int(e.get("count", 0))
        if w <= 0:
            continue
        n_edges += 1
        s_end, d_tb = layout[s][3], layout[d][0]
        w_tot += w
        dist += w * abs(d_tb - s_end)
        if d_tb == _round_up(s_end, LINE):
            w_adj += w
        if d_tb // PAGE == (s_end - 1) // PAGE:
            w_page += w
    out["edges"] = {"kinds": sorted(EDGE_KINDS), "edges": n_edges, "weight": w_tot,
                    "weighted_mean_distance_bytes": (dist / w_tot) if w_tot else None,
                    "adjacent_fraction": (w_adj / w_tot) if w_tot else None,
                    "same_page_fraction": (w_page / w_tot) if w_tot else None}
    return out


def snapshot_layout(units: Iterable[Any], offsets: Mapping[str, int] | None = None) -> Layout:
    """``uid -> (tb, tc, tc_size, end)`` of ``units``, moved to ``offsets`` (tb) if given."""
    out = {}
    for u in units:
        tb = u.tb_offset if offsets is None else int(offsets.get(u.uid, u.tb_offset))
        d = tb - u.tb_offset
        out[u.uid] = (tb, u.tc_offset + d, u.tc_size, u.end_offset + d)
    return out


def link_layout(snapshot: Any, link: Any) -> Layout:
    """The candidate layout of a :class:`~scopt.link.LinkResult`."""
    return snapshot_layout(snapshot.units(), {lu.uid: lu.new_tb_offset for lu in link.units})


def _phase_inputs(profiles: Sequence[Any], phase: str
                  ) -> tuple[Any | None, Mapping[str, Any], list[Mapping[str, Any]]]:
    from .store import profile_for_phase

    view = profile_for_phase(profiles, phase)
    if view is None or not callable(getattr(view, "phase", None)):
        return None, {}, []
    counts = view.phase(phase)
    edges = list(view.edges(phase)) if callable(getattr(view, "edges", None)) else []
    return view, counts, edges


def locality_sidecar(snapshot: Any, link: Any, profiles: Sequence[Any], *,
                     phase: str = "steady") -> dict[str, Any]:
    """``x_locality`` of an artifact sidecar: the model for the input snapshot's
    layout and for the linked candidate, plus the differences."""
    from .store import profile_id_of

    view, counts, edges = _phase_inputs(tuple(profiles), phase)
    doc: dict[str, Any] = {"label": LABEL, "phase": phase,
                           "coverages": list(COVERAGES)}
    if view is None:
        doc.update(status="not_run", reason=f"no_profile_with_phase:{phase}")
        return doc
    pid = profile_id_of(view)
    reached = None
    prof = getattr(view, "profile", None)
    if prof is not None and callable(getattr(prof, "phase_id", None)):
        p = prof.phase_id(phase)
        reached = p is not None and prof.phase_reached(p)
    if reached is False:
        doc.update(status="not_run", reason=f"phase_not_reached:{phase}", profile_id=pid)
        return doc
    units = tuple(snapshot.units())
    before = locality_model(snapshot_layout(units), counts, edges=edges, phase=phase,
                            profile_id=pid)
    after = locality_model(link_layout(snapshot, link), counts, edges=edges, phase=phase,
                           profile_id=pid)
    delta = []
    for b, a in zip(before["coverage"], after["coverage"]):
        delta.append({"coverage": b["coverage"],
                      **{k: a[k] - b[k] for k in ("lines64_code", "lines64", "pages4k_code",
                                                  "pages4k", "span_bytes")}})
    doc.update(status="ok" if before["exact_entries"] else "no_exact_entries", profile_id=pid,
               input=before, candidate=after, delta=delta)
    return doc


def _compute(ctx: Any, scope: ScopeRef) -> dict[str, Any]:
    if scope.kind != "snapshot":
        raise ValueError(f"{NAME} is snapshot-scoped, got {scope.kind}")
    from .hotness import phase_profile_counts

    pc, view = phase_profile_counts(ctx, scope, "steady")  # counts and edges: one profile
    units = ctx.units()
    lay = snapshot_layout(units, dict(ctx.layout()))
    edges = list(view.edges("steady")) if view is not None and \
        callable(getattr(view, "edges", None)) else []
    return locality_model(lay, pc.phase("steady"), edges=edges, phase="steady",
                          profile_id=pc.profile_id)


LOCALITY = AnalysisSpec(name=NAME, version=VERSION, scope="snapshot",
                        requires=(("profile.counts", 1),),
                        inputs=frozenset({"code", "meta", "layout", "profile"}),
                        compute=_compute, result_schema=dict)
ANALYSES = (LOCALITY,)
