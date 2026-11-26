"""Align the captured trace to host pages while preserving all inter-block distances."""
from ..analysis.hotness import phase_profile_counts
from ..analysis.layout import TB_ALIGN
from ..analysis.llvm_layout import _transfer_offsets
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult
from .cluster import DEFAULT_KINDS
from .public_input import public_input_refusal, public_layout_geometry


def best_page_shift(transfers, page, slack, check_budget):
    """Minimize weighted page crossings along native prefixes and taken transfers."""
    def score(delta):
        cost = 0
        for entry, exit_site, target, weight in transfers:
            origin_page = (entry + delta) // page
            exit_page = (exit_site + delta) // page
            target_page = (target + delta) // page
            cost += weight * (exit_page - origin_page + (target_page != exit_page))
        return cost

    before = best = score(0)
    chosen = 0
    for delta in range(TB_ALIGN, min(page, slack + 1), TB_ALIGN):
        check_budget()
        cost = score(delta)
        if cost < best:
            chosen, best = delta, cost
    return chosen, before, best


class AlignTracePages:
    descriptor = PassDescriptor(
        name="align-trace-pages", version=1, unit_scope="snapshot",
        requires=(("profile.counts", 1),), requires_profile_phase="steady",
        min_profile_match=0.95, capabilities=frozenset({EditKind.PLACE}),
        budgets=Budgets(max_wall_s=30.0),
        preserves=frozenset({"refs", "recovery", "cfg.known_edges", "profile.counts", "hotness", "annotations"}),
        invalidates=frozenset({"layout"}))

    def run(self, snap, ctx, edits):
        h = snap.header
        refusal = public_input_refusal(h)
        if refusal is not None:
            return refusal
        units = {u.uid: u for u in snap.units()}
        if not units or h.host_page_size < TB_ALIGN or h.host_page_size % TB_ALIGN:
            return PassResult(Outcome.NO_CHANGE, "no_page_alignment_choices")
        layout = snap.layout()
        if any(snap.is_pinned(uid) or not snap.is_relocatable(uid) or u.native_fragments
               or snap.placement(uid) is not None or snap.section_of(uid) is not None
               or u.tb_offset != layout[uid] for uid, u in units.items()):
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "requires_original_relocatable_units")
        _, view = phase_profile_counts(ctx, snap, "steady")
        if view is None:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "missing_steady_profile")
        units, starts, private, grown = public_layout_geometry(tuple(units.values()), ctx)
        offsets = {uid: _transfer_offsets(u) for uid,u in units.items()}
        transfers = []
        for edge in view.edges("steady"):
            src, target, kind = edge["src"], edge["dst"], edge["kind"]
            if (kind not in DEFAULT_KINDS or edge["status"] != "exact" or edge["flags"]
                    or edge["count"] <= 0 or target not in units or kind not in offsets.get(src, {})
                    or src in grown or src in private or target in private):
                continue
            entry = h.code_address + starts[src] + units[src].tc_offset - units[src].tb_offset
            destination = h.code_address + starts[target] + units[target].tc_offset - units[target].tb_offset
            transfers.append((entry, entry + offsets[src][kind],
                              destination, edge["count"]))
        end = max(starts[uid] + u.end_offset - u.tb_offset for uid, u in units.items())
        slack = h.code_size - h.host_page_size - 1024 - end
        shift, before, after = best_page_shift(transfers, h.host_page_size, slack, ctx.budget)
        stats = {"delta_bytes": shift, "weighted_crossings_before": before,
                 "weighted_crossings_after": after, "located_transfer_weight": sum(t[3] for t in transfers)}
        if not shift:
            return PassResult(Outcome.NO_CHANGE, "captured_alignment_is_best", stats=stats)
        for uid, start in starts.items():
            edits.place(uid, start + shift)
        return PassResult(Outcome.CANDIDATE, "reduced_page_crossings", affected=tuple(units), stats=stats)


PASSES = (AlignTracePages,)
