"""Whole-program summaries and partitions for sealed firmware regions.

This follows GCC WHOPR's separation rather than copying its GIMPLE-specific
machinery: LGEN-like per-TB summaries, a body-free WPA call graph/SCC plan,
and deterministic balanced LTRANS partitions.  Partition count affects only
materialization parallelism; it is never a profitability or code-growth cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .api import AnalysisSpec, ScopeRef
from .firmware_calls import FirmwareCalls, selected_firmware_units

__all__ = [
    "WhoprNode", "WhoprEdge", "FirmwareWhoprSummary", "balanced_partitions",
    "FIRMWARE_WHOPR",
]


@dataclass(frozen=True, slots=True)
class WhoprNode:
    uid: str
    slot: int
    native_bytes: int
    guest_instructions: int
    fallback_only: bool

    def to_json(self) -> dict[str, Any]:
        return {"uid": self.uid, "slot": self.slot, "native_bytes": self.native_bytes,
                "guest_instructions": self.guest_instructions,
                "fallback_only": self.fallback_only}


@dataclass(frozen=True, slots=True)
class WhoprEdge:
    callsite_id: str
    source_uid: str
    target_uid: str

    def to_json(self) -> dict[str, str]:
        return {"callsite_id": self.callsite_id, "source_uid": self.source_uid,
                "target_uid": self.target_uid}


@dataclass(frozen=True, slots=True)
class FirmwareWhoprSummary:
    snapshot_id: str
    nodes: tuple[WhoprNode, ...]
    edges: tuple[WhoprEdge, ...]
    sccs: tuple[tuple[str, ...], ...]
    scc_order: tuple[int, ...]
    optimizer_roots: tuple[str, ...]
    fallback_units: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": "scopt.firmware_whopr_summary/1",
            "snapshot_id": self.snapshot_id,
            "nodes": [node.to_json() for node in self.nodes],
            "edges": [edge.to_json() for edge in self.edges],
            "sccs": [list(scc) for scc in self.sccs],
            "scc_order": list(self.scc_order),
            "optimizer_roots": list(self.optimizer_roots),
            "fallback_units": list(self.fallback_units),
        }


def _sccs(nodes: tuple[str, ...], edges: tuple[WhoprEdge, ...]
          ) -> tuple[tuple[tuple[str, ...], ...], tuple[int, ...]]:
    position = {uid: index for index, uid in enumerate(nodes)}
    adjacency = {uid: [] for uid in nodes}
    for edge in edges:
        adjacency.setdefault(edge.source_uid, []).append(edge.target_uid)
        adjacency.setdefault(edge.target_uid, [])
    for targets in adjacency.values():
        targets.sort()
    index = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(uid: str) -> None:
        nonlocal index
        indices[uid] = low[uid] = index
        index += 1
        stack.append(uid)
        on_stack.add(uid)
        for target in adjacency[uid]:
            if target not in indices:
                visit(target)
                low[uid] = min(low[uid], low[target])
            elif target in on_stack:
                low[uid] = min(low[uid], indices[target])
        if low[uid] == indices[uid]:
            component = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == uid:
                    break
            components.append(tuple(sorted(component)))

    for uid in nodes:
        if uid not in indices:
            visit(uid)
    components.sort(key=lambda component: min(position[uid] for uid in component))
    owner = {uid: i for i, component in enumerate(components) for uid in component}
    dag = {i: set() for i in range(len(components))}
    indegree = {i: 0 for i in dag}
    for edge in edges:
        source, target = owner[edge.source_uid], owner[edge.target_uid]
        if source != target and target not in dag[source]:
            dag[source].add(target)
            indegree[target] += 1
    ready = sorted((i for i, degree in indegree.items() if degree == 0),
                   key=lambda i: min(position[uid] for uid in components[i]))
    order: list[int] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for target in sorted(dag[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort(key=lambda i: min(position[uid] for uid in components[i]))
    return tuple(components), tuple(order)


def balanced_partitions(summary: FirmwareWhoprSummary, count: int
                        ) -> tuple[tuple[int, ...], ...]:
    """Balance SCCs while preferring cuts with few crossing call edges.

    Like GCC's balanced WHOPR map, the source order is linearized first and
    SCCs remain atomic.  Between 3/4 and 2x of the target size, the cut with
    the smallest boundary/size pair wins.  No node is dropped or duplicated.
    """
    if type(count) is not int or count < 1:
        raise ValueError("WHOPR partition count must be a positive integer")
    order = list(summary.scc_order)
    if not order:
        return ()
    count = min(count, len(order))
    node_size = {node.uid: max(1, node.native_bytes) for node in summary.nodes}
    scc_size = {i: sum(node_size[uid] for uid in summary.sccs[i]) for i in order}
    owner = {uid: i for i, scc in enumerate(summary.sccs) for uid in scc}
    edge_pairs = [(owner[e.source_uid], owner[e.target_uid]) for e in summary.edges
                  if owner[e.source_uid] != owner[e.target_uid]]
    partitions: list[tuple[int, ...]] = []
    at = 0
    while at < len(order):
        remaining_parts = count - len(partitions)
        if remaining_parts <= 1:
            partitions.append(tuple(order[at:]))
            break
        remaining_size = sum(scc_size[i] for i in order[at:])
        target = max(1, remaining_size // remaining_parts)
        candidates: list[tuple[int, int, int]] = []
        size = 0
        max_end = len(order) - (remaining_parts - 1)
        for end in range(at + 1, max_end + 1):
            size += scc_size[order[end - 1]]
            if size * 4 < target * 3:
                continue
            left = set(order[at:end])
            boundary = sum((source in left) != (target_scc in left)
                           for source, target_scc in edge_pairs)
            candidates.append((boundary, abs(size - target), end))
            if size >= target * 2:
                break
        end = min(candidates)[2] if candidates else at + 1
        partitions.append(tuple(order[at:end]))
        at = end
    return tuple(partitions)


def _compute(ctx: Any, scope: ScopeRef) -> FirmwareWhoprSummary:
    calls: FirmwareCalls = ctx.analysis("firmware-calls", scope).value
    units = tuple(ctx.units())
    selected, fallback = selected_firmware_units(units)
    fallback_set = set(fallback)
    by_uid = {unit.uid: unit for unit in units}
    optimizer = {unit.uid for unit in selected}
    edges = tuple(WhoprEdge(call.callsite_id, call.source_uid,
                            call.callee_candidates[0])
                  for call in calls.calls if call.eligible)
    participating = optimizer | {edge.target_uid for edge in edges}
    ordered = tuple(unit.uid for unit in units if unit.uid in participating)
    nodes = tuple(WhoprNode(uid, by_uid[uid].slot, by_uid[uid].tc_size,
                            by_uid[uid].icount, uid in fallback_set)
                  for uid in ordered)
    components, order = _sccs(ordered, edges)
    return FirmwareWhoprSummary(ctx.snapshot_id, nodes, edges, components, order,
                                tuple(unit.uid for unit in selected), fallback)


FIRMWARE_WHOPR = AnalysisSpec(
    name="firmware-whopr", version=1, scope="snapshot",
    requires=(("firmware-calls", 1),), inputs=frozenset({"code", "meta"}),
    compute=_compute, result_schema=FirmwareWhoprSummary,
)
ANALYSES = (FIRMWARE_WHOPR,)
