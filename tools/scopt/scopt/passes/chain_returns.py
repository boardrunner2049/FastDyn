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

Each GENERAL target's first hit in a replay exits through the main loop to
install the native link. Only later matching hits can avoid a lookup.
Aggregate profiles cannot say which targets were used on each replay, so
we reserve up to one installation per target per recorded replay. A new
guard charges all lookup traffic at its exit. The separate SPEC cost model
accounts for its variants’ arming path. Set ``c_link`` to a calibrated nonnegative cost per GENERAL installation
or ``min_target_hits`` to require a higher aggregate exact hit count.
For returns, ``min_return_share`` can additionally require one target to
account for a strict majority of all exactly attributed steady return hits.
"""

from __future__ import annotations

from collections import Counter
from fractions import Fraction
import math
from typing import Any, ClassVar

from ..analysis.api import ScopeRef
from ..analysis.profitability import DEFAULTS
from ..formats.profile_v1 import METHOD
from .base import Budgets, EditKind, GuardedSlot, Outcome, PassDescriptor, PassResult
from ._b3util import refusal_result

__all__ = ["ChainReturns", "general_slots", "PASSES"]

MAX_UNITS = 256


def _descriptor(name: str) -> PassDescriptor:
    params = {"targets_per_exit": int, "max_units": int, "min_pass_share": float,
              "c_lookup": float, "c_guard": float, "c_fail": float,
              "min_target_hits": int, "c_link": float, "compact_guard": bool}
    if name == "chain-returns":
        params["min_return_share"] = float
    return PassDescriptor(
        name=name, version=1, unit_scope="snapshot",
        requires=(("hostops", 1), ("return-edges", 1), ("envlayout", 1), ("profile.counts", 1)),
        requires_profile_phase="steady", min_profile_match=0.95,
        capabilities=frozenset({EditKind.REDIRECT_EXIT}),
        budgets=Budgets(max_wall_s=120.0, max_units=MAX_UNITS),
        preserves=frozenset({"profile.counts", "cfg.known_edges"}),
        invalidates=frozenset({"code", "meta", "layout"}),
        params_schema=params)


def _exact_return_hits(exits, profile_view) -> int | None:
    """Resolve exact steady lookup-hit traffic once per return source.

    The complete profile supplies outbound lookup edges, including external
    and refused continuation hits. Check the source for exact coverage, and
    require positive traffic to be attributable to one lookup tail. The edge
    entering steady belongs to that phase even if its source entered in boot.
    """
    if profile_view is None or profile_view.profile is None:
        return None
    collection = profile_view.profile.collection
    if collection.get("method") != METHOD or collection.get("complete") is not True:
        return None
    counts_by_source = Counter(row.uid for row in exits if row.exit_kind == "goto_ptr")
    total = 0
    seen = set()
    for row in exits:
        if row.exit_kind != "goto_ptr" or row.exit_class != "return" or row.uid in seen:
            continue
        seen.add(row.uid)
        entries = profile_view.count(row.uid, "steady", "entries")
        hits = sum(c.count for c in row.candidates)
        if (not entries.exact
                or any(c.status != "exact" for c in row.candidates)
                or (hits and counts_by_source[row.uid] != 1)):
            return None
        total += hits
    return total


def general_slots(view: Any, ctx: Any, edits: Any, exit_class: str) -> PassResult:
    from ..guard import COMPACT_GUARD, GUARD_TEMPLATE_ID, GuardError, guard_key

    b3 = ctx.b3
    if b3 is None:
        return PassResult(Outcome.SKIPPED_UNSUPPORTED, "capability_not_enabled", "not in B3 mode")
    if not b3.layout_bound or b3.offsets is None:
        return PassResult(Outcome.SKIPPED_UNSUPPORTED, "guard_layout_unbound",
                          b3.layout_reason or "")
    st = view.b3
    p = ctx.params
    lookup = float(p.get("c_lookup", DEFAULTS["c_lookup"]))
    guard = float(p.get("c_guard", DEFAULTS["c_guard"]))
    fail = float(p.get("c_fail", DEFAULTS["c_fail"]))
    link = float(p.get("c_link", 0.0))
    if not math.isfinite(link) or link < 0:
        raise ValueError("c_link must be nonnegative and finite")
    min_share = float(p.get("min_pass_share", 0.5))
    per_exit = int(p.get("targets_per_exit", 2))
    min_target_hits = int(p.get("min_target_hits", 0))
    if min_target_hits < 0:
        raise ValueError("min_target_hits must be nonnegative")
    max_units = int(p.get("max_units", MAX_UNITS))
    sid = ScopeRef("snapshot", ctx.snapshot_id)
    edges = ctx.analysis("return-edges", sid).value
    ctx.analysis("envlayout", sid)
    profile_view = ctx.profile("steady")
    profile = profile_view.profile if profile_view is not None else None
    dominance_stats = {}
    if "min_return_share" in p:
        share = float(p["min_return_share"])
        if not 0.5 < share <= 1.0:
            raise ValueError("min_return_share must be greater than 0.5 and at most 1")
        total_hits = _exact_return_hits(edges.exits, profile_view)
        if total_hits is None:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "return_traffic_inexact")
        if not total_hits:
            return PassResult(Outcome.NO_CHANGE, "no_exact_return_traffic")
        min_target_hits = max(min_target_hits, math.ceil(total_hits * Fraction(str(share))))
        dominance_stats = {"train_exact_return_hits": total_hits,
                           "min_return_share": share, "min_exact_target_hits": min_target_hits}
    collection = profile.collection if profile is not None else {}
    replays = max(1, len(collection.get("runs", ())), collection.get("repetitions") or 0)
    units = b3.units()
    vsrc = st.variant_sources()
    refused: Counter[str] = Counter()
    n_x = 0
    touched: set[str] = set()
    n_slots = 0
    guarded_w = 0
    avoided_w = 0
    installed_w = 0
    guard_attempts = 0
    targets_below_threshold = 0
    targets_without_reuse = 0
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
        eligible = [c for c in row.candidates if c.admitted and c.dst is not None and
                    c.vaddr not in have]
        cands = []
        for c in eligible:
            if c.count <= replays:
                targets_without_reuse += 1
            elif min_target_hits and (c.status != "exact" or c.count < min_target_hits):
                targets_below_threshold += 1
            else:
                cands.append(c)
        if eligible and not cands:
            refused["not_profitable"] += 1
            continue
        if not cands:
            refused["target_not_profiled" if not have else "slot_budget"] += 1
            continue
        try:
            k0 = guard_key(x.key_flags, x.key_cs_base) if x is not None else None
        except GuardError:
            k0 = None
        total = sum(int(c.count) for c in row.candidates)
        guarded = sum(s.weight for s in x.slots) if x is not None else 0
        # Charge a fresh guard once for every lookup attempt. If the exit
        # already has a SPEC or GENERAL slot, that fixed cost is already paid.
        marginal = 0.0 if x is not None else -total * (guard + fail)
        picked = []
        for c in cands:
            if not free or len(picked) >= per_exit - len(have):
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
            installs = min(int(c.count), replays)
            avoided = int(c.count) - installs
            benefit = avoided * lookup - installs * link + int(c.count) * fail
            if benefit <= 0:
                refused["not_profitable"] += 1
                continue
            marginal += benefit
            k0 = kc
            picked.append((c, cu, free.pop(0), installs, avoided))
        if not picked:
            continue
        guarded += sum(int(c.count) for c, _, _, _, _ in picked)
        if marginal <= 0 or guarded < min_share * max(guarded, total):
            refused["not_profitable"] += 1
            continue
        guard_attempts += total
        compact = ctx.params.get("compact_guard", False) and len(picked) == 1 and not have
        for c, cu, slot, installs, avoided in picked:
            edits.redirect_exit(GuardedSlot(row.uid, row.exit_id, slot, "GENERAL", exit_class,
                                            c.vaddr, cu.key, tail_off=row.tail_off,
                                            guard_template_id=COMPACT_GUARD if compact else GUARD_TEMPLATE_ID),
                                dependency_additions=(f"edge:{row.uid}->{cu.uid}",))
            n_slots += 1
            guarded_w += int(c.count)
            avoided_w += avoided
            installed_w += installs
        touched.add(row.uid)
    stats = {"exits": n_x, "slots": n_slots, "units": len(touched),
             "train_lookups_guarded": guarded_w,
             "estimated_train_lookups_avoided": avoided_w,
             "estimated_train_link_installs": installed_w,
             "train_guard_attempts": guard_attempts,
             "training_replays": replays,
             "targets_without_estimated_reuse": targets_without_reuse, **dominance_stats}
    if min_target_hits:
        stats["targets_below_minimum_exact_hits"] = targets_below_threshold
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
