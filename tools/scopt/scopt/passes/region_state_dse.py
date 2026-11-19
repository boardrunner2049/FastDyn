"""Remove CPU-state stores made dead by a specialized region's actual CFG."""

from __future__ import annotations

from collections import Counter

from ..analysis.api import ScopeRef
from ..analysis.global_values import EFFECT_INPUT
from ..analysis.helper_effects import refresh_effects_for_layout
from ..analysis.store import value_digest
from ..analysis.syncdead import B4Reason, MAX_CANDIDATES, nop_bytes, syncdead_unit
from .base import Outcome, PassResult, Patch
from .constant_propagation import region_descriptor


class RegionStateDSE:
    descriptor = region_descriptor(
        "region-state-dse",
        (("native-region", 1), ("obs-contract", 1), ("hostops", 1), ("hcfg", 1)),
    )

    def run(self, vv, ctx, edits):
        cap = ctx.params.get("max_patches", 64)
        if not 0 <= cap <= MAX_CANDIDATES:
            return PassResult(Outcome.FAILED, "invalid_params",
                              f"max_patches must be in [0, {MAX_CANDIDATES}]")
        scope = ScopeRef("unit", vv.src.uid)
        region = ctx.analysis("native-region", scope).value
        obs = ctx.analysis("obs-contract", ScopeRef("snapshot", ctx.snapshot_id)).value
        if region.status != "ok" or vv.layout is None or obs.envlayout_id != vv.layout.envlayout_id:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "observer_contract_unknown",
                              "requires a bound, analyzable native region")
        host, graph, _, _ = vv.analyses()
        if not graph.ok or graph.succ_full != graph.succ_atomic:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "state_path_unresolved",
                              "full CFG includes unmodeled atomic slow or recovery paths")
        effects = refresh_effects_for_layout(vv.layout)
        # The table is immutable for all passes that consumed the current
        # generation. Do not mix two versions if the file changes mid-run.
        effect_id = value_digest((vv.layout.envlayout_id, effects.table_id))
        prefix = f"external:{EFFECT_INPUT}:"
        bound = {i for i in ctx.store.inputs() if i.startswith(prefix)}
        if bound and bound != {prefix + effect_id}:
            return PassResult(Outcome.FAILED, "stale_helper_effects",
                              "helper-effect binding changed after region planning")
        sd = syncdead_unit(vv.unit, host, graph, vv.layout, obs, effects=effects)
        if not sd.ok:
            return PassResult(Outcome.SKIPPED_UNKNOWN, sd.reason or "observer_contract_unknown",
                              sd.detail)
        baseline_host = ctx.analysis("hostops", scope).value
        baseline_graph = ctx.analysis("hcfg", scope).value
        baseline = syncdead_unit(vv.src, baseline_host, baseline_graph, vv.layout, obs,
                                 effects=effects)
        if not baseline.ok:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "baseline_state_path_unresolved",
                              baseline.detail)
        baseline_live = {r.off for r in baseline.refused
                         if r.reason == B4Reason.OBSERVED_AT_SYNC_POINT}
        stats = Counter({"deleted": 0, "deleted_store_bytes": 0})
        for r in sd.refused:
            if r.cls == "D1":
                stats[f"refused:{r.reason}"] += 1
        refs = tuple(f"fact:{f.fact_id}" for f in vv.factrefs)
        for store in sd.dead:
            # Preserve PC, checks, bookkeeping, and all observer interfaces.
            if (store.cls != "D1" or store.block != "insn"
                    or store.off not in baseline_live
                    or not region.allows(store.off, store.off + store.size)):
                continue
            if stats["deleted"] >= cap:
                stats["refused:patch_budget"] += 1
                break
            replacement = nop_bytes(store.size)
            assert replacement is not None
            deps = refs + (f"observations:{obs.contract_id}", f"helper-effects:{effect_id}")
            edits.pure_rewrite(Patch(vv.variant_id, store.off, store.old, replacement, "b4_nop_store",
                                     f"no observer of {store.field} before overwrite in variant"),
                               dependency_additions=deps)
            stats["deleted"] += 1
            stats["deleted_store_bytes"] += store.size
        if stats["deleted"]:
            return PassResult(Outcome.CANDIDATE, "variant_dead_stores",
                              f"{stats['deleted']} stores made dead by the specialized CFG",
                              affected=(vv.src.uid,), stats=dict(stats))
        return PassResult(Outcome.NO_CHANGE, "no_new_dead_store", stats=dict(stats))


PASSES = (RegionStateDSE,)
