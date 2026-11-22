"""``move-units@1`` (design §5.10): explicit PLACE pass.

Moves chosen units to explicit 64-aligned arena offsets so that WP10's linker
and validator can be exercised with known displacements (e.g. ±64·k).

Params (both optional, combined; a unit may appear in only one of them):

* ``moves``: ``{UnitId: new_tb_offset}`` (int, or a ``"0x…"`` string);
* ``deltas``: ``{UnitId: delta}``; the new offset is the unit's current
  WorkingSet offset plus ``delta`` (int, or a ``"0x…"``/``"-0x…"`` string).

Units are processed in slot order. With no moves the pass returns NO_CHANGE.
The pass is primarily test/fixture machinery and is also used by the explicit
``activation-layout`` experiment. Ordinary profitability pipelines such as
``identity`` and ``b2-layout`` do not contain it.
"""

from __future__ import annotations

from typing import Any

from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult

__all__ = ["MoveUnits", "PASSES"]


def _int(v: Any, what: str) -> int:
    if isinstance(v, bool):
        raise ValueError(f"{what}: bool is not an offset")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        neg = s.startswith("-")
        s = s[1:] if neg else s
        if s.startswith("0x"):
            n = int(s, 16)
        else:
            n = int(s, 10)
        return -n if neg else n
    raise ValueError(f"{what}: expected an int or a hex string, got {v!r}")


class MoveUnits:
    descriptor = PassDescriptor(
        name="move-units", version=1, unit_scope="snapshot",
        capabilities=frozenset({EditKind.PLACE}),
        budgets=Budgets(max_wall_s=10.0),
        preserves=frozenset({"refs", "recovery", "cfg.known_edges", "profile.counts",
                             "hotness", "annotations"}),
        invalidates=frozenset({"layout"}),
        params_schema={"moves": dict, "deltas": dict})

    def run(self, snap, ctx, edits) -> PassResult:
        moves = dict(ctx.params.get("moves", {}))
        deltas = dict(ctx.params.get("deltas", {}))
        both = sorted(set(moves) & set(deltas))
        if both:
            raise ValueError(f"units in both moves and deltas: {both}")
        layout = snap.layout()
        targets: dict[str, int] = {}
        for uid, v in moves.items():
            targets[uid] = _int(v, uid)
        for uid, v in deltas.items():
            if uid not in layout:
                raise KeyError(f"unknown unit {uid}")
            targets[uid] = layout[uid] + _int(v, uid)
        if not targets:
            return PassResult(Outcome.NO_CHANGE, "no_moves")
        order = {uid: i for i, uid in enumerate(layout)}
        moved = []
        for uid in sorted(targets, key=lambda u: (order.get(u, len(order)), u)):
            edits.place(uid, targets[uid])
            moved.append(uid)
        return PassResult(Outcome.CANDIDATE, "moved", affected=tuple(sorted(moved)),
                          stats={"moved": float(len(moved))})


PASSES = (MoveUnits,)
