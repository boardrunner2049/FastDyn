"""``chain-returns@1`` and the shared GENERAL-slot logic of ``chain-static@1`` (design §3.1, §3.5). WP21.

``REDIRECT_EXIT(R.slot, GENERAL)`` on the slots that ``specialize-returns``
left free (pipeline order arbitrates slots, §3.5): for each admitted exit of
the pass's class (``return`` here, ``static`` for ``chain-static``) the
top-ranked admitted continuations of the train profile that are not guarded
yet get a GENERAL guarded slot (at most two targets per exit; the guard
compares the whole lookup key, §4.1). A GENERAL slot is linked by the main
loop after a lookup (stock ``tb_add_jump``) and never enters a variant.

Refusals (stats, stable codes): the exit's §3.1 reason (``exit_not_goto_ptr``,
``indirect_multiway``, ``handler_mode_return``, ``pc_modifier_exit``,
``special_cflags_unit``, ``ram_resident``, ``unit_not_relocatable``,
``no_free_slot``, ``target_not_profiled``, …), ``no_free_slot`` (the
remaining slots are claimed), ``unit_not_relocatable`` (R is the source of a
variant: WP19 note item 4, fail closed), ``continuation_key_mismatch`` (the
target's guard key differs from the one already on the exit),
``not_profitable`` (§10.3), ``slot_budget`` (256 units per pass).
``tbb``/``ldr pc,[rX]``/``bx rX`` exits are ``indirect_multiway`` and are
never guarded in v1 (design §3.1 rule 2, §14: candidate ``chain-indirect``).
"""

from __future__ import annotations

from collections import Counter
from typing import Any, ClassVar

from ..analysis.api import ScopeRef
from ..analysis.profitability import exit_saving
from .base import Budgets, EditKind, GuardedSlot, Outcome, PassDescriptor, PassResult
from ._b3util import refusal_result

__all__ = ["ChainReturns", "general_slots", "PASSES"]

MAX_UNITS = 256


def _descriptor(name: str) -> PassDescriptor:
    return PassDescriptor(
        name=name, version=1, unit_scope="snapshot",
        requires=(("hostops", 1), ("return-edges", 1), ("envlayout", 1), ("profile.counts", 1)),
        requires_profile_phase="steady", min_profile_match=0.95,
        capabilities=frozenset({EditKind.REDIRECT_EXIT}),
        budgets=Budgets(max_wall_s=120.0, max_units=MAX_UNITS),
        preserves=frozenset({"profile.counts", "cfg.known_edges"}),
        invalidates=frozenset({"code", "meta", "layout"}),
        params_schema={"targets_per_exit": int, "max_units": int, "min_pass_share": float,
                       "c_lookup": float, "c_guard": float, "c_fail": float})


def general_slots(view: Any, ctx: Any, edits: Any, exit_class: str) -> PassResult:
    from ..guard import GuardError, guard_key

    b3 = ctx.b3
    if b3 is None:
        return PassResult(Outcome.SKIPPED_UNSUPPORTED, "capability_not_enabled", "not in B3 mode")
    if not b3.layout_bound or b3.offsets is None:
        return PassResult(Outcome.SKIPPED_UNSUPPORTED, "guard_layout_unbound",
                          b3.layout_reason or "")
    st = view.b3
    p = ctx.params
    cal = {k: float(p[k]) for k in ("c_lookup", "c_guard", "c_fail") if k in p}
    min_share = float(p.get("min_pass_share", 0.5))
    per_exit = int(p.get("targets_per_exit", 2))
    max_units = int(p.get("max_units", MAX_UNITS))
    sid = ScopeRef("snapshot", ctx.snapshot_id)
    edges = ctx.analysis("return-edges", sid).value
    ctx.analysis("envlayout", sid)
    units = b3.units()
    vsrc = st.variant_sources()
    refused: Counter[str] = Counter()
    n_x = 0
    touched: set[str] = set()
    n_slots = 0
    guarded_w = 0
    for row in edges.exits:
        if row.exit_class != exit_class:
            continue
        n_x += 1
        ctx.budget()
        if not row.admitted:
            refused[row.reason_codes[0]] += 1
            continue
        if row.uid in vsrc:
            refused["unit_not_relocatable"] += 1
            continue
        if row.uid not in touched and len(touched) >= max_units:
            refused["slot_budget"] += 1
            continue
        x = st.exit_plan(row.uid, row.tail_off)
        claimed = st.claimed_slots(row.uid)
        free = [n for n in row.free_slots if n not in claimed]
        if not free:
            refused["no_free_slot"] += 1
            continue
        have = {s.vaddr for s in x.slots} if x is not None else set()
        cands = [c for c in row.candidates if c.admitted and c.dst is not None and
                 c.vaddr not in have][:max(0, per_exit - len(have))]
        if not cands:
            refused["target_not_profiled" if not have else "slot_budget"] += 1
            continue
        try:
            k0 = guard_key(x.key_flags, x.key_cs_base) if x is not None else None
        except GuardError:
            k0 = None
        total = sum(int(c.count) for c in row.candidates)
        guarded = sum(s.weight for s in x.slots) if x is not None else 0
        picked = []
        for c in cands:
            if not free:
                break
            cu = units[c.dst]
            try:
                kc = guard_key(cu.key.flags, cu.key.cs_base)
            except GuardError:
                refused["continuation_key_mismatch"] += 1
                continue
            if k0 is not None and kc != k0:
                refused["continuation_key_mismatch"] += 1
                continue
            k0 = kc
            picked.append((c, cu, free.pop(0)))
        if not picked:
            continue
        sv = exit_saving(guarded + sum(int(c.count) for c, _, _ in picked), total, cal,
                         min_share=min_share)
        if not sv.profitable:
            refused["not_profitable"] += 1
            continue
        for c, cu, slot in picked:
            edits.redirect_exit(GuardedSlot(row.uid, row.exit_id, slot, "GENERAL", exit_class,
                                            c.vaddr, cu.key, tail_off=row.tail_off),
                                dependency_additions=(f"edge:{row.uid}->{cu.uid}",))
            n_slots += 1
            guarded_w += int(c.count)
        touched.add(row.uid)
    stats = {"exits": n_x, "slots": n_slots, "units": len(touched),
             "train_lookups_guarded": guarded_w}
    if n_slots:
        stats.update({f"refused:{k}": v for k, v in refused.items()})
        return PassResult(Outcome.CANDIDATE, "", f"{n_slots} GENERAL slots",
                          affected=tuple(sorted(touched)), stats=stats)
    return refusal_result(refused, nothing=f"no_{exit_class}_exit", stats=stats)


class ChainReturns:
    descriptor: ClassVar[PassDescriptor] = _descriptor("chain-returns")

    def run(self, view: Any, ctx: Any, edits: Any) -> PassResult:
        return general_slots(view, ctx, edits, "return")


PASSES = (ChainReturns,)
