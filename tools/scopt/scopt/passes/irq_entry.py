"""Fuse a scratch load and zero test in profiled non-icount IRQ polls."""
from collections import Counter
from ..analysis.hotness import phase_profile_counts
from ..analysis.irq_entry import OLD, NEW, can_fuse_irq_entry
from ..link import tb_ret_abs_of
from ..validate_sym.equiv import check_equiv
from ..validate_sym.program import patched, program_of, world_of
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult, Patch


class IrqEntryMemCompare:
    descriptor = PassDescriptor(
        name="irq-entry-mem-compare", version=1, unit_scope="snapshot",
        requires=(("profile.counts", 1),), requires_profile_phase="steady",
        min_profile_match=0.95, capabilities=frozenset({EditKind.PURE_REWRITE}),
        budgets=Budgets(max_wall_s=900.0), preserves=frozenset({"envlayout", "obs-contract"}),
        invalidates=frozenset({"code"}))

    def run(self, snap, ctx, edits):
        if ctx.b3 is None or not ctx.b3.layout_bound:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "unbound_native_layout")
        base = ctx.b3.snapshot
        if (base.header.version != 5 or ctx.b3_state.rewrites or ctx.b3_state.exits
                or ctx.b3_state.variants):
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "requires_original_v5_code")
        counts, view = phase_profile_counts(ctx, snap, "steady")
        if view is None:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "missing_steady_profile")
        world = world_of(base, ctx.b3.layout)
        tb_ret = tb_ret_abs_of(base)
        weight = counts.phase("steady")
        changed, stats = [], Counter()
        for u in base.units():
            ctx.budget()
            hits = weight.get(u.uid)
            if hits is None or hits.status != "exact" or not hits.value:
                continue
            if not can_fuse_irq_entry(u, base.header.code_address, tb_ret):
                continue
            original = program_of(u, world)
            proof = check_equiv(original, patched(original, [(0, NEW)]), layout=ctx.b3.layout)
            if proof.status != "equivalent":
                stats["refused:" + proof.status] += 1
                continue
            edits.pure_rewrite(Patch(u.uid, 0, OLD, NEW, "irq_entry_mem_compare",
                                     "same signed interrupt test, scratch dead on both edges"))
            changed.append(u.uid)
            stats["trained_dynamic_instructions_removed"] += hits.value
        stats["changed_blocks"] = len(changed)
        if not changed:
            return PassResult(Outcome.NO_CHANGE, "no_eligible_irq_poll", stats=stats)
        return PassResult(Outcome.CANDIDATE, "fused_irq_poll",
                          affected=tuple(changed), stats=stats)


PASSES = (IrqEntryMemCompare,)
