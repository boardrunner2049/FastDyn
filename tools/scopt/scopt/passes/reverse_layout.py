"""``reverse-layout@1`` (design §7 F-B2-2): test-only LAYOUT_ORDER pass.

Puts every relocatable, unpinned unit into the ``hot`` section in reverse slot
order, so that NativeLinker re-places all of them (first-fit from
``ROUND_UP(prologue_end, 64)``) and every fixup class is exercised. Records
keep their original slot order (§4.7). Like ``move-units`` it never runs in
the production pipelines; a test pipeline must allow ``LAYOUT_ORDER``.

Param ``exclude`` (list of UnitIds) leaves units out of the order (they stay
where they are, or a later ``move-units`` places them). Mirrored packing can
have a fixed point (a unit whose reversed position equals its original
one); excluding it and moving it explicitly makes every unit move.
"""

from __future__ import annotations

from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult

__all__ = ["ReverseLayout", "PASSES"]


class ReverseLayout:
    descriptor = PassDescriptor(
        name="reverse-layout", version=1, unit_scope="snapshot",
        capabilities=frozenset({EditKind.LAYOUT_ORDER}), budgets=Budgets(max_wall_s=10.0),
        preserves=frozenset({"refs", "recovery", "cfg.known_edges", "profile.counts",
                             "hotness", "annotations"}),
        invalidates=frozenset({"layout"}),
        params_schema={"exclude": list})

    def run(self, snap, ctx, edits) -> PassResult:
        exclude = set(ctx.params.get("exclude", ()))
        uids = [u.uid for u in snap.units() if u.uid not in exclude
                and snap.is_relocatable(u.uid) and not snap.is_pinned(u.uid)]
        if not uids:
            return PassResult(Outcome.NO_CHANGE, "no_relocatable_units")
        edits.layout_order("hot", list(reversed(uids)))
        return PassResult(Outcome.CANDIDATE, "reversed", affected=tuple(sorted(uids)),
                          stats={"ordered": float(len(uids))})


PASSES = (ReverseLayout,)
