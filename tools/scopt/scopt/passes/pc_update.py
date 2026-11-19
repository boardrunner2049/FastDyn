"""Fold relative updates of ARM's PC before profiled TCG exits."""
from ..analysis.hotness import phase_profile_counts
from ..analysis.pc_update import folded_exits, layout_matches, recorded_targets
from ..validate_sym.equiv import check_equiv
from ..validate_sym.program import patched, program_of, world_of
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult, Patch


class FoldExitPcUpdate:
    descriptor = PassDescriptor(
        name="fold-exit-pc-update", version=1, unit_scope="snapshot",
        requires=(("profile.counts", 1),), requires_profile_phase="steady", min_profile_match=0.95,
        capabilities=frozenset({EditKind.PURE_REWRITE}), invalidates=frozenset({"code"}),
        preserves=frozenset({"envlayout", "obs-contract"}), budgets=Budgets(max_wall_s=900),
    )

    def run(self, snap, ctx, edits):
        if ctx.b3 is None or not ctx.b3.layout_bound or not layout_matches(ctx.b3.layout):
            return PassResult(Outcome.SKIPPED_UNKNOWN, "unbound_arm_pc")
        base = ctx.b3.snapshot
        if (base.header.version != 5 or ctx.b3_state.rewrites or ctx.b3_state.exits
                or ctx.b3_state.variants):
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "requires_original_v5_code")
        counts, view = phase_profile_counts(ctx, snap, "steady")
        if view is None:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "missing_steady_profile")
        world = world_of(base, ctx.b3.layout)
        targets = recorded_targets(base)
        weights = counts.phase("steady")
        affected = []
        stats = {"folded_updates": 0, "blocks_refused_by_hostsym": 0}
        for unit in base.units():
            ctx.budget()
            hits = weights.get(unit.uid)
            if hits is None or hits.status != "exact" or not hits.value:
                continue
            spans = folded_exits(unit, world, targets)
            if not spans:
                continue
            original = program_of(unit, world)
            candidate = patched(original, [(start, new) for start, (_, new) in spans.items()])
            if check_equiv(original, candidate, layout=world.layout).status != "equivalent":
                stats["blocks_refused_by_hostsym"] += 1
                continue
            for start, (old, new) in spans.items():
                edits.pure_rewrite(Patch(unit.uid, start, old, new, "pc_update_mem_add",
                                         "equivalent relative PC update at recorded TCG exit"))
            stats["folded_updates"] += len(spans)
            affected.append(unit.uid)
        if not affected:
            return PassResult(Outcome.NO_CHANGE, "no_proved_exit_updates", stats=stats)
        return PassResult(Outcome.CANDIDATE, "folded_exit_updates", affected=tuple(affected), stats=stats)


PASSES = (FoldExitPcUpdate,)
