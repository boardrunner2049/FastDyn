"""``layout.v1`` analysis (design §5.3, §4.7, §5.7). WP7.

Snapshot scope. Value::

    {"intervals": [[uid, tb_offset, end_offset], ...]   # current layout, slot order
     "pinned": [uid, ...],                              # not ok_relocatable (import status)
     "relocatable": n, "max_end": int,
     "prologue_floor": ROUND_UP(prologue_end, 64),
     "bound": code_size - host_page_size - 1024,         # the placer's max_end limit
     "slack": bound - max_end,
     "overlaps": [[uid_a, uid_b], ...]}                 # must be empty

Offsets come from the WorkingSet-aware ``layout()`` (the current tb_offset of
each unit); a unit's extent ``end - tb`` never changes in B2.
"""

from __future__ import annotations

from typing import Any

from .api import AnalysisSpec, ScopeRef

__all__ = ["LAYOUT", "ANALYSES", "layout_value", "TB_ALIGN"]

TB_ALIGN = 64


def _round_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a


def layout_value(units: Any, offsets: dict[str, int], header: Any) -> dict[str, Any]:
    iv = []
    pinned = []
    for u in units:
        tb = offsets.get(u.uid, u.tb_offset)
        iv.append([u.uid, tb, tb + (u.end_offset - u.tb_offset)])
        if not (u.reloc.relocatable and u.meta_status == "ok"):
            pinned.append(u.uid)
    s = sorted(iv, key=lambda r: (r[1], r[0]))
    overlaps = [[a[0], b[0]] for a, b in zip(s, s[1:]) if b[1] < a[2]]
    max_end = max((r[2] for r in iv), default=0)
    bound = header.code_size - header.host_page_size - 1024
    return {"intervals": iv, "pinned": pinned, "relocatable": len(iv) - len(pinned),
            "max_end": max_end, "prologue_floor": _round_up(header.prologue_end, TB_ALIGN),
            "bound": bound, "slack": bound - max_end, "overlaps": overlaps}


def _layout(ctx: Any, scope: ScopeRef) -> dict[str, Any]:
    return layout_value(ctx.units(), dict(ctx.layout()), ctx.header)


LAYOUT = AnalysisSpec(name="layout", version=1, scope="snapshot", requires=(),
                      inputs=frozenset({"code", "meta", "layout"}), compute=_layout,
                      result_schema=dict)
ANALYSES = (LAYOUT,)
