"""``specialize-returns@1``: continuation variants behind SPEC guarded slots (design §3.1-3.5). WP21.

For every **admitted return exit** E of a unit R (``return-edges@1``: §3.1
rules 1-6, ``indirect_multiway`` exits such as ``tbb``/``ldr pc,[rX]`` never
qualify) that has **certified** facts (CalleeSummary v1 row, V14 certifier,
§3.2) the pass proposes, for each of the (at most two) top-ranked admitted
continuations C of E:

* ``CLONE`` C -> C' (``Clone(src_uid, variant_id, facts)``),
* ``REDIRECT_EXIT`` (``GuardedSlot`` kind SPEC on a free slot of R, ``Vc`` = C's
  profiled vaddr, ``key_C`` = C's key),
* ``ADD_ENTRY`` (the variant's one predecessor ``(R, slot)``, its guard atoms
  and facts),
* ``ATTACH_FACT`` (each FactRef, on R).

Refusals (stable codes, counted in the pass's stats): the exit's own §3.1
reason, ``fact_not_constant`` (no certified fact; or the row's first
refusal, e.g. ``fact_killed_by_helper``, ``fact_not_local_to_return_tb``),
``fact_unused`` (``hconst@1`` predicts no decided branch and no foldable load
in C under F, §3.4), ``not_profitable`` (§10.3 exit saving ≤ 0 or guard pass
share < 50 %), ``unit_not_relocatable`` (C is grown in this plan), and the
budgets ``context_budget`` (2 variants per C, ranked by each R unit's own
train weight; 64 per artifact) and ``growth_budget`` (stub + variant bytes
≤ 10 % of the 99 %-coverage hot-set bytes).

Variants are per (R unit, exit, C, fact set) and never shared (§3.1 [R4]).
The profile only selects and ranks; facts come only from certified rows.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, ClassVar

from ..analysis.api import ScopeRef
from ..analysis.hconst import build_hconst
from ..analysis.profitability import exit_saving
from ..link import variant_key
from .base import (Budgets, Clone, EditKind, FactRef, GuardAtom, GuardedSlot, Outcome,
                   PassDescriptor, PassResult, SpecEntry)
from ._b3util import certified_facts, factref_of_row, refusal_result

__all__ = ["SpecializeReturns", "PASSES"]


class SpecializeReturns:
    exit_class = "return"
    exit_stat = "return_exits"
    no_exit_reason = "no_return_exit"

    def _select_exit(self, row: Any) -> bool:
        return row.exit_class == self.exit_class

    def _select_target(self, row: Any, candidate: Any, unit: Any) -> bool:
        return True

    def _emit(self, kept, view, ctx, edits) -> str | None:
        for choice in kept:
            emit_specialization(PlannedSpecialization.from_choice(choice), ctx.b3.offsets, edits)
        return None

    descriptor: ClassVar[PassDescriptor] = PassDescriptor(
        name="specialize-returns", version=1, unit_scope="snapshot",
        requires=(("hostops", 1), ("hcfg", 1), ("return-edges", 1), ("exit-facts", 1),
                  ("patch-join", 1), ("hconst", 1), ("envlayout", 1), ("profile.counts", 1)),
        requires_profile_phase="steady", min_profile_match=0.95,
        capabilities=frozenset({EditKind.CLONE, EditKind.REDIRECT_EXIT, EditKind.ADD_ENTRY,
                                EditKind.ATTACH_FACT}),
        budgets=Budgets(max_wall_s=120.0, max_region_members=2, max_context_versions=2),
        preserves=frozenset({"profile.counts", "callee-summary"}),
        invalidates=frozenset({"code", "meta", "layout"}),
        params_schema={"max_variants_per_c": int, "max_variants": int,
                       "growth_fraction": float, "max_code_growth_bytes": int,
                       "targets_per_exit": int, "min_pass_share": float,
                       "c_lookup": float, "c_guard": float, "c_fail": float})

    def run(self, view: Any, ctx: Any, edits: Any) -> PassResult:
        b3 = ctx.b3
        if b3 is None:
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "capability_not_enabled",
                              "not in B3 mode")
        if not b3.layout_bound or b3.offsets is None:
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "guard_layout_unbound",
                              b3.layout_reason or "")
        st = view.b3
        p = ctx.params
        cal = {k: float(p[k]) for k in ("c_lookup", "c_guard", "c_fail") if k in p}
        min_share = float(p.get("min_pass_share", 0.5))
        per_exit = int(p.get("targets_per_exit", 2))
        max_per_c = int(p.get("max_variants_per_c", 2))
        max_total = int(p.get("max_variants", 64))
        sid = ScopeRef("snapshot", ctx.snapshot_id)
        edges = ctx.analysis("return-edges", sid).value
        ctx.analysis("envlayout", sid)
        rows = {(r["return_unit"], r["exit_id"]): r for r in b3.summaries()}
        units = b3.units()
        refused: Counter[str] = Counter()
        plans: list[dict[str, Any]] = []
        n_ret = 0
        grown = st.grown_uids()
        for row in edges.exits:
            if not self._select_exit(row):
                continue
            n_ret += 1
            ctx.budget()
            if not row.admitted:
                refused[row.reason_codes[0]] += 1
                continue
            srow = rows.get((row.uid, row.exit_id))
            cf = certified_facts(srow)
            if not cf:
                rs = [x.get("reason") for x in (srow or {}).get("refused", ())]
                refused[rs[0] if rs and rs[0] else "fact_not_constant"] += 1
                continue
            facts = {f["field"]: int(f["value"], 0) for f in cf}
            claimed = st.claimed_slots(row.uid)
            slots = [n for n in row.free_slots if n not in claimed]
            total = sum(int(c.count) for c in row.candidates)
            cands = [c for c in row.candidates if c.admitted and c.dst is not None][:per_exit]
            if not cands:
                refused["target_not_profiled"] += 1
                continue
            for c in cands:
                if not slots:
                    refused["slot_budget"] += 1
                    break
                cu = units[c.dst]
                if not self._select_target(row, c, cu):
                    refused["not_static_dest"] += 1
                    continue
                if c.dst in grown or c.dst == row.uid:
                    refused["unit_not_relocatable"] += 1
                    continue
                # A host-inline splice prefix certified C's native fragments
                # in place; a clone C' would need a new proof, and the linker
                # would refuse the whole plan (native_layout_unsupported).
                if cu.native_fragments:
                    refused["native_layout_unsupported"] += 1
                    continue
                ctx.analysis("hostops", ScopeRef("unit", cu.uid))
                h = b3.hostops(cu.uid)
                g = b3.hcfg(cu.uid)
                ctx.analysis("hcfg", ScopeRef("unit", cu.uid))
                ctx.analysis("hconst", ScopeRef("unit", cu.uid))
                hc = build_hconst(h, g, facts, code=cu.code)
                if not hc.ok or not hc.useful:
                    refused["fact_unused"] += 1
                    continue
                weights = srow.get("x_guarded_origin", {})
                guarded_count = min(int(c.count), int(weights.get("writer_tb_edge_count", c.count)))
                sv = exit_saving(guarded_count, total, cal, min_share=min_share)
                if not sv.profitable:
                    refused["not_profitable"] += 1
                    continue
                plans.append({"row": row, "srow": srow, "facts": cf, "c": c, "cu": cu,
                              "slot": slots.pop(0), "weight": int(c.count), "saving": sv,
                              "hconst": hc})
        # budgets: per C (ranked by the R unit's own train weight) and per artifact
        plans.sort(key=lambda d: (-d["weight"], d["row"].uid, d["row"].exit_id, d["c"].dst))
        per_c: Counter[str] = Counter(v.src_uid for v in st.variants.values())
        kept = []
        for d in plans:
            cu = d["cu"]
            if per_c[cu.uid] >= max_per_c:
                refused["context_budget"] += 1
                continue
            if len(st.variants) + len(kept) >= max_total:
                refused["context_budget"] += 1
                continue
            per_c[cu.uid] += 1
            kept.append(d)
        # growth budget (§3.4 max_code_growth_bytes), admitted in rank order: the
        # manager checks the whole proposal against the same budget, so a proposal
        # that exceeds it would lose every variant (WP21 int: F-B3-6, 2 variants of
        # 790 bytes > 619); dropping the lowest-ranked variants keeps the rest
        kept, n_growth = _fit_growth(kept, st, units, b3, p)
        if n_growth:
            refused["growth_budget"] += n_growth
        detail = self._emit(kept, view, ctx, edits)
        stats = {self.exit_stat: n_ret, "variants": len(kept),
                 "train_lookups_guarded": sum(d["weight"] for d in kept),
                 "saving_ns_model": round(sum(d["saving"].saving_ns for d in kept), 3)}
        if kept:
            stats.update({f"refused:{k}": v for k, v in refused.items()})
            return PassResult(Outcome.CANDIDATE, "", detail or f"{len(kept)} variants",
                              affected=tuple(sorted({d["row"].uid for d in kept})),
                              stats=stats)
        return refusal_result(refused, nothing=self.no_exit_reason, stats=stats, detail=detail or "")


@dataclass(frozen=True, slots=True)
class PlannedSpecialization:
    source: str
    source_hash: str
    exit_id: str
    exit_class: str
    tail_off: int
    slot: int
    dest: str
    dest_hash: str
    vaddr: int
    key: Any
    facts: tuple[FactRef, ...]
    summary_hash: str

    @classmethod
    def from_choice(cls, choice: dict[str, Any]) -> "PlannedSpecialization":
        row, summary, dest, cand = (choice[k] for k in ("row", "srow", "cu", "c"))
        return cls(row.uid, summary["deps"]["unit"]["content_hash"], row.exit_id, row.exit_class,
                   row.tail_off, choice["slot"], dest.uid, dest.content_hash, cand.vaddr, dest.key,
                   tuple(factref_of_row(summary, f) for f in choice["facts"]), summary["row_hash"])


def emit_specialization(choice: PlannedSpecialization, offsets: Any, edits: Any,
                        dependencies: tuple[str, ...] = ()) -> None:
    """Emit a selected and certified predecessor-specific variant."""
    from ..guard import guard_atoms, guard_key

    refs = choice.facts
    vid = variant_key(choice.dest, choice.source, choice.exit_id, [r.to_json() for r in refs], ())
    kf, kf2 = guard_key(choice.key.flags, choice.key.cs_base)
    atoms = tuple(GuardAtom(a["atom"], a["env_field"], a["width"], a["op"],
                            int(a["value"], 16))
                  for a in guard_atoms(offsets, kf, kf2, [choice.vaddr],
                                       r0_zero=any(r.source == "guard" for r in refs)))
    deps = (tuple(f"fact:{r.fact_id}" for r in refs)
            + (f"summary:{choice.summary_hash}", f"variant:{vid}") + dependencies)
    for ref in refs:
        edits.attach_fact(ref, units=(choice.source,), dependency_additions=deps)
    edits.clone(Clone(choice.dest, vid, refs), dependency_additions=deps)
    edits.redirect_exit(GuardedSlot(choice.source, choice.exit_id, choice.slot, "SPEC",
                                    choice.exit_class, choice.vaddr, choice.key, variant_ref=vid,
                                    tail_off=choice.tail_off), dependency_additions=deps)
    edits.add_entry(SpecEntry(vid, (choice.source, choice.slot), atoms, refs), dependency_additions=deps)


def growth_budget(params: Any, b3: Any, units: Any) -> int | None:
    """The §3.4 budget exactly as the manager computes it (``PassManager._apply_b3``):
    ``max_code_growth_bytes`` or ``growth_fraction`` (default 0.10) × the 99 %-coverage
    hot-set bytes; None (no budget) without a hot set."""
    from ..manager_b3 import hot_set_bytes

    g = params.get("max_code_growth_bytes")
    if g:
        return int(g)
    hot = hot_set_bytes(b3.store, units)
    return None if hot is None else int(float(params.get("growth_fraction", 0.10)) * hot)


def _fit_growth(kept: list[dict[str, Any]], st: Any, units: Any, b3: Any,
                params: Any) -> tuple[list[dict[str, Any]], int]:
    """Keep the ranked plans whose cumulative growth (variant bytes plus the guard
    stub of every exit that carries a SPEC slot, :meth:`B3State.growth_bytes`) stays
    within the budget; returns (kept plans, number dropped for ``growth_budget``)."""
    from ..manager_b3 import stub_bytes_estimate

    budget = growth_budget(params, b3, units)
    if budget is None or budget <= 0 or not kept:
        return kept, 0
    base = st.growth_bytes(units)
    slots = {k: len(x.slots) for k, x in st.exits.items()}
    spec = {k for k, x in st.exits.items() if any(s.kind == "SPEC" for s in x.slots)}
    variant_bytes = sum(units[v.src_uid].end_offset - units[v.src_uid].tb_offset
                        for v in st.variants.values())

    guarded = {k for k, x in st.exits.items() if any(getattr(s, "r0_zero", False) for s in x.slots)}

    def growth(sl: dict, sp: set, gd: set, vb: int) -> int:
        return vb + sum(stub_bytes_estimate(sl[k], k in gd) for k in sp)

    out: list[dict[str, Any]] = []
    dropped = 0
    for d in kept:
        k = (d["row"].uid, d["row"].tail_off)
        sl = dict(slots)
        sl[k] = sl.get(k, 0) + 1
        sp = spec | {k}
        vb = variant_bytes + d["cu"].end_offset - d["cu"].tb_offset
        gd = guarded | ({k} if any(f["source"] == "guard" for f in d.get("facts", ())) else set())
        g = growth(sl, sp, gd, vb)
        if g > budget and g > base:
            dropped += 1
            continue
        out.append(d)
        slots, spec, guarded, variant_bytes = sl, sp, gd, vb
    return out, dropped


PASSES = (SpecializeReturns,)
