"""``effects@1``: the event and sync-point map of one unit (design §7.1, §7.5, §8.1). WP17.

Derived from HostOps + hcfg only (Tier-0 metadata; no TCG IR). Value
(:class:`Effects`)::

    sync     [(op index, event, detail)]  S1 entry, S2 exits, S3 lookup call, S4 non-pure calls,
                                           S5 LDST blocks (at the GUEST_ACCESS op), S6 udiv/sdiv,
                                           P branch/label points
    events   [(op index, kind, detail)]    guest accesses, calls, non-env host stores,
                                           env stores of non-bookkeeping fields, exits

Call classes (``call_class``)::

    lookup     lookup_tb_ptr                      -> S3
    pure       named NO_RWG helper that cannot raise (not udiv/sdiv, not noreturn) -> no sync
    raising    udiv / sdiv (NO_RWG, may raise DIV_0_TRP) -> S6 (treated as S4 by B4)
    plugin     unnamed helper (plugin callback, any flags)  -> S4
    noreturn   TCG_CALL_NO_RETURN                 -> S4 (and an exit)
    global     flags 0 or NO_WG                   -> S4
    ldst_slow  softmmu slow-path helper           -> part of S5
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .api import AnalysisSpec, ScopeRef
from .hcfg import TCG_CALL_NO_RETURN, HCfg, exit_class
from .hostops import OpKind, UnitHostOps

__all__ = ["Effects", "build_effects", "call_class", "EFFECTS", "ANALYSES", "RAISING_NO_RWG",
           "TCG_CALL_NO_RWG", "TCG_CALL_NO_WG"]

TCG_CALL_NO_RWG = 0x0001
TCG_CALL_NO_WG = 0x0002
RAISING_NO_RWG = frozenset({"udiv", "sdiv"})


def call_class(name: str | None, flags: int | None) -> str:
    if name == "<ldst_slow_path>":
        return "ldst_slow"
    if name is None:
        return "plugin"
    if name == "lookup_tb_ptr":
        return "lookup"
    f = 0 if flags is None else flags
    if f & TCG_CALL_NO_RETURN:
        return "noreturn"
    if name in RAISING_NO_RWG:
        return "raising"
    if f & TCG_CALL_NO_RWG:
        return "pure"
    return "global"


_CALL_SYNC = {"lookup": "S3", "plugin": "S4", "noreturn": "S4", "global": "S4", "raising": "S6"}


@dataclass(frozen=True)
class Effects:
    uid: str
    sync: tuple[tuple[int, str, str], ...]
    events: tuple[tuple[int, str, str], ...]
    counts: dict[str, int] = field(default_factory=dict)

    def sync_at(self, i: int) -> tuple[tuple[str, str], ...]:
        return tuple((e, d) for j, e, d in self.sync if j == i)

    def content_digest(self) -> str:
        import hashlib
        import json

        return "sha256:" + hashlib.sha256(json.dumps(
            [self.uid, list(self.sync), list(self.events)]).encode()).hexdigest()


def build_effects(h: UnitHostOps, g: HCfg) -> Effects:
    sync: list[tuple[int, str, str]] = []
    ev: list[tuple[int, str, str]] = []
    if h.ops:
        sync.append((0, "S1", "entry"))
    targets = {o.target for o in h.ops if o.target is not None}
    for i, o in enumerate(h.ops):
        ek = g.exits.get(i)
        if o.kind == OpKind.CALL:
            cc = call_class(o.helper_name, o.helper_flags)
            if cc in _CALL_SYNC and o.block != "ldst_slow":
                sync.append((i, _CALL_SYNC[cc], f"{cc}:{o.helper_name}"))
            ev.append((i, "call", f"{cc}:{o.helper_name}:{o.helper_flags}"))
        if ek is not None and i in g.reachable_atomic:
            sync.append((i, "S2", exit_class(ek)))
            ev.append((i, "exit", ek))
        if o.kind == OpKind.GUEST_ACCESS:
            sync.append((i, "S5", f"ldst:{o.ldst}"))
            ev.append((i, "guest_access", f"ldst:{o.ldst}:oi={o.oi}:{'ld' if o.is_ld else 'st'}"))
        if o.kind == OpKind.HOST_ABS_STORE:
            ev.append((i, "host_store", "abs=" + ("?" if o.abs_addr is None else hex(o.abs_addr))))
        if o.kind in (OpKind.JCC, OpKind.JMP) and o.block in ("insn", "prefix") and \
                ek is None and not {"LDST_FAST_JCC", "LDST_RET_JMP"} & set(o.sites):
            sync.append((i, "P", "branch"))
        if o.off in targets and o.block in ("insn", "exit_stub") and i != 0:
            sync.append((i, "P", "label"))
    counts = Counter(e for _, e, _ in sync)
    counts.update("ev:" + k for _, k, _ in ev)
    return Effects(h.uid, tuple(sync), tuple(ev), dict(sorted(counts.items())))


def _compute(ctx: Any, scope: ScopeRef) -> Effects:
    h = ctx.analysis("hostops", scope).value
    g = ctx.analysis("hcfg", scope).value
    return build_effects(h, g)


EFFECTS = AnalysisSpec(name="effects", version=1, scope="unit",
                       requires=(("hostops", 1), ("hcfg", 1)), inputs=frozenset(),
                       compute=_compute, result_schema=Effects)
ANALYSES = (EFFECTS,)
