"""Conservative host-page layout around the original capture order."""

from __future__ import annotations

from collections import Counter

from ..analysis.hotness import phase_profile_counts
from ..analysis.layout import TB_ALIGN
from ..analysis.llvm_layout import build_cache_directed_request
from ..analysis.page_layout import improve_page_layout
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult
from .cluster import DEFAULT_KINDS
from .public_input import public_input_refusal, public_layout_geometry


def align(offset):
    return (offset + TB_ALIGN - 1) // TB_ALIGN * TB_ALIGN


class PageAwareLocalLayout:
    descriptor = PassDescriptor(
        name="page-aware-local-layout", version=1, unit_scope="snapshot",
        requires=(("profile.counts", 1),), requires_profile_phase="steady",
        min_profile_match=0.95, capabilities=frozenset({EditKind.LAYOUT_ORDER}),
        budgets=Budgets(max_wall_s=30.0),
        preserves=frozenset({"refs", "recovery", "cfg.known_edges", "profile.counts",
                             "annotations", "hotness"}),
        invalidates=frozenset({"layout"}),
        params_schema={"max_rounds": int, "max_swaps": int},
    )

    def run(self, snap, ctx, edits):
        h = snap.header
        refusal = public_input_refusal(h)
        if refusal is not None:
            return refusal
        units = sorted(snap.units(), key=lambda u: u.tb_offset)
        if not units:
            return PassResult(Outcome.NO_CHANGE, "empty_arena")
        cursor = align(h.prologue_end)
        layout = snap.layout()
        for u in units:
            uid = u.uid
            if (snap.is_pinned(uid) or not snap.is_relocatable(uid)
                    or snap.section_of(uid) is not None or snap.placement(uid) is not None
                    or u.native_fragments):
                return PassResult(Outcome.SKIPPED_UNSUPPORTED, "requires_unplaced_relocatable_units")
            if u.tb_offset != cursor or layout[uid] != cursor:
                return PassResult(Outcome.SKIPPED_UNSUPPORTED, "requires_dense_capture_order")
            cursor = align(u.end_offset)

        max_rounds = ctx.params.get("max_rounds", 3)
        max_swaps = ctx.params.get("max_swaps", 32)
        if max_rounds < 1 or max_swaps < 1:
            raise ValueError("max_rounds and max_swaps must be positive")
        if h.host_page_size <= 0:
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "invalid_host_page_size")
        pc, view = phase_profile_counts(ctx, snap, "steady")
        if view is None:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "missing_steady_profile")
        effective, baseline, private, grown = public_layout_geometry(units, ctx)
        units = [effective[u.uid] for u in units]
        exact_counts = {uid: int(count.value or 0) for uid, count in pc.phase("steady").items()
                        if count.status == "exact"}
        by_uid = {u.uid: i for i, u in enumerate(units)}
        graph = tuple(edge for edge in view.edges("steady") if edge["kind"] in DEFAULT_KINDS)
        eligible = [edge for edge in graph if edge["status"] == "exact" and not edge["flags"]
                    and edge["src"] in by_uid and edge["dst"] in by_uid
                    and edge["count"] > 0 and edge["src"] not in grown
                    and edge["src"] not in private and edge["dst"] not in private]
        # Protect exact directed adjacency even if the native site is not known.
        directed = Counter()
        for edge in eligible:
            directed[by_uid[edge["src"]], by_uid[edge["dst"]]] += edge["count"]
        request, unlocated = build_cache_directed_request(
            units, exact_counts, eligible, DEFAULT_KINDS, include_self_loops=True,
        )
        exact_units = [by_uid[u.uid] for u in units if u.uid in exact_counts]
        edges = [
            {**e, "src": exact_units[e["src"]], "dst": exact_units[e["dst"]]}
            for e in request["edges"]
        ]
        spans = [align(u.end_offset - u.tb_offset) for u in units]
        starts = []
        cursor = h.code_address + align(h.prologue_end)
        for span in spans:
            starts.append(cursor)
            cursor += span
        plan = improve_page_layout(
            starts, spans,
            [u.tc_offset - u.tb_offset for u in units],
            edges, directed, set(exact_units), page_size=h.host_page_size,
            max_rounds=max_rounds, max_swaps=max_swaps, check_budget=ctx.budget,
            baseline_starts=[h.code_address + baseline[u.uid] for u in units],
        )
        all_weight = sum(int(e["count"]) for e in graph)
        weight = sum(e["count"] for e in edges)
        stats = {
            "profile_transfer_weight": all_weight,
            "exact_captured_transfer_weight": sum(directed.values()),
            "located_transfer_weight": weight,
            "located_transfer_coverage": weight / all_weight if all_weight else 0.0,
            "unlocated_transfer_rows": unlocated,
            "located_self_loop_weight": sum(e["count"] for e in edges if e["src"] == e["dst"]),
            "protected_adjacency_weight": plan.protected_adjacency_weight,
            "weighted_page_crossings_before": plan.weighted_crossings[0],
            "weighted_page_crossings_after": plan.weighted_crossings[1],
            "weighted_transfer_distance_before": plan.weighted_distance[0],
            "weighted_transfer_distance_after": plan.weighted_distance[1],
            "adjacent_swaps": plan.swaps,
            "adjacent_pairs_considered": plan.considered,
            "page_gains_blocked_by_original_adjacency": plan.blocked_by_original_adjacency,
            "page_gains_blocked_by_native_distance": plan.blocked_by_native_distance,
        }
        # Grown units normally move to the end. Compare the proposed packed
        # layout (including growth) with that actual no-layout baseline.
        if (plan.weighted_crossings[1] >= plan.weighted_crossings[0]
                or plan.weighted_distance[1] > plan.weighted_distance[0]):
            return PassResult(Outcome.NO_CHANGE, "no_profitable_local_swap", stats=stats)
        order = tuple(units[i].uid for i in plan.order)
        edits.layout_order("hot", order)
        affected = []
        cursor = align(h.prologue_end)
        for i in plan.order:
            uid = units[i].uid
            if baseline[uid] != cursor:
                affected.append(uid)
            cursor += spans[i]
        return PassResult(Outcome.CANDIDATE, "weighted_page_crossings_reduced",
                          affected=tuple(affected),
                          stats=stats)


PASSES = (PageAwareLocalLayout,)
