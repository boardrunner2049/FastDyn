"""Build a CodeLayout locality graph from exact profile and captured TC sites."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..formats.enums import Encoding, SiteKind
from .layout import TB_ALIGN

OPERATION = "cache-directed-layout"


def _transfer_offsets(unit, *, same_generation=False):
    matches = {}
    for site in unit.sites:
        if not 0 <= site.insn_off < unit.tc_size:
            continue
        if site.kind == SiteKind.GOTO_TB and site.enc == Encoding.GOTO_TB_REL32:
            if site.aux in (0, 1):
                matches.setdefault(f"chain{site.aux}", []).append(site.insn_off)
        elif ((unit.stub_start is None or same_generation) and site.kind == SiteKind.GOTO_PTR
              and site.enc == Encoding.NONE):
            matches.setdefault("lookup_hit", []).append(site.insn_off)
    return {kind: sites[0] for kind, sites in matches.items() if len(sites) == 1}


@dataclass(frozen=True)
class GuardTransfer:
    destination: str
    pc: int
    offset: int
    kind: str
    slot_n: int


@dataclass(frozen=True)
class GuardedLookup:
    fallback: int
    page_mask: int
    transfers: tuple[GuardTransfer, ...]


def guarded_lookups(grown, state, page_bits):
    """Offsets and target identities from the same materialized exit.

    One source-profile lookup event names its TB, not a particular tail.
    Multiple original lookup sites cannot share those counts unambiguously.
    """
    result = {}
    for uid, growth in grown.items():
        original_offsets = _transfer_offsets(growth.source)
        if "lookup_hit" not in original_offsets or len(growth.stubs) != 1:
            continue
        spec, stub = growth.stubs[0]
        if original_offsets["lookup_hit"] != spec.tail_off + 9:
            continue
        exit_plan = state.exit_plan(uid, spec.tail_off)
        identities = {slot.slot_n: slot for slot in exit_plan.slots}
        transfers = []
        for slot in stub.slots:
            source = identities[slot.slot_n]
            if slot.registered and source.c_uid is not None and source.vaddr == slot.vaddr:
                transfers.append(GuardTransfer(source.c_uid, slot.vaddr, slot.jmp_off, slot.kind, slot.slot_n))
        if len(transfers) == len(stub.slots):
            result[uid] = GuardedLookup(stub.f_off + 9, (1 << page_bits) - 1, tuple(transfers))
    return result


@dataclass
class LayoutTraffic:
    """Partition of exact, unflagged weight for the requested edge kinds."""
    weights: Counter = field(default_factory=Counter)

    def record(self, category, weight):
        self.weights[category] += weight

    def stats(self):
        return {**{f"llvm_traffic_{k}": v for k, v in self.weights.items()},
                "llvm_traffic_total": sum(self.weights.values())}


def build_cache_directed_request(units, counts, edges, kinds, *, include_self_loops=False,
                                 guarded=None, estimate_replays=None, traffic=None,
                                 private_sources=(), same_generation=False):
    """Return the bridge request and the number of partly or wholly unlocated edges.

    The source of a generated guard may hit its slot, enter the main loop to
    install its link, or take the fallback. Preserve ambiguous weight unless
    the caller explicitly requests the registered GENERAL reuse estimate.
    """
    nodes, slots, transfers, by_uid = [], {}, {}, {}
    guarded = guarded or {}
    private_sources = set(private_sources)
    traffic = LayoutTraffic() if traffic is None else traffic
    if estimate_replays is not None and estimate_replays < 1:
        raise ValueError("estimate_replays must be a positive replay count")
    for unit in units:
        if unit.uid not in counts:
            continue
        footprint = unit.end_offset - unit.tb_offset
        size = (footprint + TB_ALIGN - 1) // TB_ALIGN * TB_ALIGN
        slots[unit.uid] = len(nodes)
        nodes.append({"size": size, "count": counts[unit.uid]})
        offsets = _transfer_offsets(unit, same_generation=same_generation and unit.uid not in guarded)
        if unit.uid in guarded:
            # Generated slots did not produce ordinary chain events in the
            # input generation. Only the mapped lookup traffic uses them.
            for target in guarded[unit.uid].transfers:
                offsets.pop(f"chain{target.slot_n}", None)
        transfers[unit.uid] = offsets
        by_uid[unit.uid] = unit
    selected = set(kinds)
    unlocated = 0
    weights = Counter()
    for edge in edges:
        src, dst, kind = edge["src"], edge["dst"], edge["kind"]
        if kind not in selected or edge["status"] != "exact" or edge["flags"]:
            continue
        count = edge["count"]
        if src in private_sources or dst in private_sources:
            # Entry counts of an original body include any private copies.
            traffic.record("private_source_unlocated" if src in private_sources
                           else "private_destination_unlocated", count)
            unlocated += 1
            continue
        if src not in slots or dst not in slots:
            traffic.record("outside_nodes", count)
            continue
        if src == dst and not include_self_loops:
            traffic.record("self_excluded", count)
            continue
        offset = transfers[src].get(kind)
        category = "ordinary"
        guard = guarded.get(src) if kind == "lookup_hit" else None
        if guard:
            dst_pc = by_uid[dst].key.page_addr0 & guard.page_mask
            possible = [t for t in guard.transfers if t.pc & guard.page_mask == dst_pc]
            if not possible:
                # Translation preserves the within-page offset. Every guarded
                # PC differs from this destination, even in the presence of aliases.
                offset, category = guard.fallback, "forced_fallback"
            elif (estimate_replays is not None and len(possible) == 1
                  and possible[0].kind == "GENERAL" and possible[0].destination == dst):
                installs = min(count, estimate_replays)
                traffic.record("estimated_install", installs)
                count -= installs
                offset, category = possible[0].offset, "estimated_general"
                if installs:
                    unlocated += 1
            else:
                traffic.record("guard_unresolved", count)
                unlocated += 1
                continue
        if offset is None:
            traffic.record("unlocated", count)
            unlocated += 1
            continue
        traffic.record(category, count)
        weights[slots[src], slots[dst], offset] += count
    rows = [{"src": src, "dst": dst, "offset": offset, "count": count}
            for (src, dst, offset), count in sorted(weights.items())]
    return {"operation": OPERATION, "version": 1, "nodes": nodes, "edges": rows}, unlocated
