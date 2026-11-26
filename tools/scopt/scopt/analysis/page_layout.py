"""Adjacent-swap layout cost based on exact native transfer sites."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PageLayout:
    order: tuple[int, ...]
    swaps: int
    weighted_crossings: tuple[int, int]
    weighted_distance: tuple[int, int]
    protected_adjacency_weight: int
    considered: int
    blocked_by_original_adjacency: int
    blocked_by_native_distance: int


def improve_page_layout(
    starts: Sequence[int],
    spans: Sequence[int],
    tc_deltas: Sequence[int],
    edges: Iterable[Mapping[str, int]],
    directed_weights: Mapping[tuple[int, int], int],
    movable: set[int],
    *,
    page_size: int,
    max_rounds: int,
    max_swaps: int,
    check_budget: Callable[[], None],
    baseline_starts: Sequence[int] | None = None,
) -> PageLayout:
    """Preserve captured adjacency and improve weighted page crossings.

    Starts are absolute arena record addresses in dense capture order and
    spans include the same alignment used by the linker. Each prediction
    counts page boundaries between TC entry and the actual transfer site.
    This measures address spans, since native control flow inside a TB can
    skip instructions within the span.
    """
    order = list(range(len(starts)))
    positions = list(starts)
    edges = tuple(edges)
    protected = {(s, d) for s, d in zip(order, order[1:]) if directed_weights.get((s, d), 0)}
    protected_weight = sum(directed_weights[e] for e in protected)
    incident: dict[int, set[int]] = defaultdict(set)
    for i, edge in enumerate(edges):
        incident[edge["src"]].add(i)
        incident[edge["dst"]].add(i)

    def cost(indices, overrides=None):
        pos = overrides or {}
        crossings = distance = 0
        for i in indices:
            edge = edges[i]
            s, d = edge["src"], edge["dst"]
            entry = pos.get(s, positions[s]) + tc_deltas[s]
            transfer = entry + edge["offset"]
            target = pos.get(d, positions[d]) + tc_deltas[d]
            weight = edge["count"]
            crossings += weight * (transfer // page_size - entry // page_size)
            if s != d:
                distance += weight * abs(target - transfer)
        return crossings, distance

    before = cost(range(len(edges)), dict(enumerate(baseline_starts)) if baseline_starts is not None else None)
    swaps = considered = blocked_adjacency = blocked_distance = 0
    for _ in range(max_rounds):
        start_swaps = swaps
        for i in range(len(order) - 1):
            if swaps == max_swaps:
                break
            check_budget()
            a, b = order[i:i + 2]
            if a not in movable or b not in movable:
                continue
            affected = incident[a] | incident[b]
            if not affected:
                continue
            proposed = {b: positions[a], a: positions[a] + spans[b]}
            old_crossings, old_distance = cost(affected)
            new_crossings, new_distance = cost(affected, proposed)
            considered += 1
            if new_crossings >= old_crossings:
                continue
            window = order[max(0, i - 1):i + 3]
            if any(pair in protected for pair in zip(window, window[1:])):
                blocked_adjacency += 1
                continue
            if new_distance > old_distance:
                blocked_distance += 1
                continue
            order[i:i + 2] = [b, a]
            positions[a], positions[b] = proposed[a], proposed[b]
            swaps += 1
        if swaps == start_swaps or swaps == max_swaps:
            break
    after = cost(range(len(edges)))
    return PageLayout(tuple(order), swaps, (before[0], after[0]), (before[1], after[1]),
                      protected_weight, considered, blocked_adjacency, blocked_distance)
