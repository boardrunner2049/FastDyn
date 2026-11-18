"""``cfg.known_edges.v1`` analysis (design §5.3, §4.4). WP7.

Snapshot scope: the capture-derived edges of :mod:`scopt.graph` (static
goto_tb edges with candidates, dynamic goto_ptr/exit edges). Profile-observed
edges are attached by ``scopt graph export --profile`` (Profile v1 files); an
analysis that needs them should read ``profile.counts`` (WP6) as well. Value::

    {"edges": [...graph.json edges...], "by_src": {uid: [edge index, ...]}}
"""

from __future__ import annotations

from typing import Any

from ..graph import static_edges
from .api import AnalysisSpec, ScopeRef

__all__ = ["KNOWN_EDGES", "ANALYSES", "known_edges_value"]


class _Snap:
    def __init__(self, units: Any) -> None:
        self._u = units

    def units(self) -> Any:
        return self._u


def known_edges_value(units: Any, page_bits: int) -> dict[str, Any]:
    snap = _Snap(units)
    edges: list[dict[str, Any]] = []
    by_src: dict[str, list[int]] = {}
    for u in units:
        for e in static_edges(snap, u, page_bits):
            by_src.setdefault(u.uid, []).append(len(edges))
            edges.append(e)
    return {"edges": edges, "by_src": by_src}


def _known_edges(ctx: Any, scope: ScopeRef) -> dict[str, Any]:
    return known_edges_value(ctx.units(), ctx.header.target_page_bits)


KNOWN_EDGES = AnalysisSpec(name="cfg.known_edges", version=1, scope="snapshot", requires=(),
                           inputs=frozenset({"code", "meta"}), compute=_known_edges,
                           result_schema=dict)
ANALYSES = (KNOWN_EDGES,)
