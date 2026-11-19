"""Build a CodeLayout locality graph from exact profile and captured TC sites."""

from __future__ import annotations

from collections import Counter

from ..formats.enums import Encoding, SiteKind
from .layout import TB_ALIGN

OPERATION = "cache-directed-layout"


def _transfer_offsets(unit):
    matches = {}
    for site in unit.sites:
        if not 0 <= site.insn_off < unit.tc_size:
            continue
        if site.kind == SiteKind.GOTO_TB and site.enc == Encoding.GOTO_TB_REL32:
            if site.aux in (0, 1):
                matches.setdefault(f"chain{site.aux}", []).append(site.insn_off)
        elif (unit.stub_start is None and site.kind == SiteKind.GOTO_PTR
              and site.enc == Encoding.NONE):
            matches.setdefault("lookup_hit", []).append(site.insn_off)
    return {kind: sites[0] for kind, sites in matches.items() if len(sites) == 1}


def build_cache_directed_request(units, counts, edges, kinds):
    """Return request and count of unlocated profile edges.

    Only exact-count units participate. Sizes use aligned arena footprints.
    The TC start has the same displacement from the arena record for every
    unit, so source/target distances use native TC entry as the node origin
    and retain its actual transfer instruction offset. Several transfers
    between a pair remain separate when their source offsets differ. A lookup
    hit uses a captured goto_ptr only when the source has exactly one such
    native site. A guarded source can bypass its fallback goto_ptr, so the
    original lookup-hit counts there have no unique transfer offset.
    """
    nodes, slots, transfers = [], {}, {}
    for unit in units:
        if unit.uid not in counts:
            continue
        footprint = unit.end_offset - unit.tb_offset
        size = (footprint + TB_ALIGN - 1) // TB_ALIGN * TB_ALIGN
        slots[unit.uid] = len(nodes)
        nodes.append({"size": size, "count": counts[unit.uid]})
        transfers[unit.uid] = _transfer_offsets(unit)
    selected = set(kinds)
    unlocated = 0
    weights = Counter()
    for edge in edges:
        src, dst, kind = edge["src"], edge["dst"], edge["kind"]
        if (kind not in selected or src == dst or src not in slots or dst not in slots
                or edge["status"] != "exact" or edge["flags"]):
            continue
        offset = transfers[src].get(kind)
        if offset is None:
            unlocated += 1
            continue
        weights[slots[src], slots[dst], offset] += edge["count"]
    rows = [{"src": src, "dst": dst, "offset": offset, "count": count}
            for (src, dst, offset), count in sorted(weights.items())]
    return {"operation": OPERATION, "version": 1, "nodes": nodes, "edges": rows}, unlocated
