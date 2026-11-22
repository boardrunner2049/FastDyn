"""Profile-guided Cache-Directed Sort through the existing layout linker."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import time

from ..analysis.llvm_layout import OPERATION, LayoutTraffic, build_cache_directed_request, guarded_lookups
from ..canon import sha256_id
from .cluster import ClusterHot


def _bridge_order(bridge, request, timeout):
    process = subprocess.run([str(bridge)], input=json.dumps(request).encode(),
                             capture_output=True, timeout=timeout, check=True,
                             env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    response = json.loads(process.stdout)
    if (not isinstance(response, dict) or type(response.get("version")) is not int
            or response["version"] != 1):
        raise ValueError("bridge returned an incompatible response")
    if response.get("status") == "refused":
        raise ValueError(f"bridge refused the graph: {str(response.get('detail', ''))[:200]}")
    if response.get("status") != "ok" or response.get("operation") != OPERATION:
        raise ValueError("bridge returned an incompatible response")
    order = response.get("order")
    size = len(request["nodes"])
    if (not isinstance(order, list) or len(order) != size
            or any(type(i) is not int for i in order) or set(order) != set(range(size))):
        raise ValueError("bridge order is not a permutation of the requested nodes")
    return order


class LLVMCacheLayout(ClusterHot):
    descriptor = replace(
        ClusterHot.descriptor, name="llvm-cache-layout",
        params_schema={**ClusterHot.descriptor.params_schema, "bridge": str, "estimate_general_reuse": bool},
    )

    def _order(self, snap, ctx, movable, heat, edges, kinds):
        def fallback(detail, stats=None):
            order, _, _, base = super(LLVMCacheLayout, self)._order(
                snap, ctx, movable, heat, edges, kinds)
            return order, "pettis_hansen_fallback", detail, {**base, **(stats or {})}

        path = (ctx.params.get("bridge") or os.environ.get("SCOPT_LLVM_LAYOUT_BRIDGE")
                or os.environ.get("SCOPT_HOST_INLINE_BRIDGE"))
        if not path:
            return fallback("configure the pinned LLVM bridge with SCOPT_LLVM_LAYOUT_BRIDGE")
        original = snap.layout()
        known = sorted((uid for uid in movable if uid in heat), key=lambda u: (original[u], u))
        units = [snap.unit(uid) for uid in known]
        guarded = {}
        private_sources = set()
        state = getattr(ctx, "b3_state", None)
        profile = ctx.profile(ctx.params.get("phase", "steady"))
        replays = None
        if ctx.params.get("estimate_general_reuse") and profile is not None and profile.profile is not None:
            collection = profile.profile.collection
            if collection.get("complete") is True:
                replays = max(1, len(collection.get("runs", ())), collection.get("repetitions", 1))
        if state is not None and state.grown_uids():
            from ..link import LinkError, NativeLinkerV2
            from ..manager_b3 import materialize

            # Analyze exact emitted stub extents and captured exit offsets at
            # the semantic generation the final linker is about to place.
            plan = materialize(state, ctx.b3).plan
            preview = NativeLinkerV2(ctx.b3.snapshot, plan, offsets=ctx.b3.offsets,
                                     derive_captures=False)
            try:
                effective, grown = preview.effective_units(units)
            except LinkError as exc:
                return fallback(f"cannot preview B3 node extents: {exc}")
            units = [effective[uid] for uid in known]
            guarded = guarded_lookups(grown, state, ctx.b3.page_bits)
            private_sources = {variant.src_uid for variant in plan.variants}
        # A source-profile entry count for a cloned body includes subsequent
        # executions of its private copies. These cannot be separate exact
        # LLVM node counts until the transformed artifact has been profiled.
        split_inputs = private_sources.intersection(known)
        known = [uid for uid in known if uid not in split_inputs]
        units = [unit for unit in units if unit.uid not in split_inputs]
        traffic = LayoutTraffic()
        request, unlocated = build_cache_directed_request(
            units, heat, edges, kinds, guarded=guarded, private_sources=private_sources,
            same_generation=bool(profile and profile.same_snapshot and not (state and state.grown_uids())),
            estimate_replays=replays, traffic=traffic)
        weight = sum(edge["count"] for edge in request["edges"])
        stats = {"llvm_nodes": len(known), "llvm_edges": len(request["edges"]),
                 "llvm_edge_weight": weight, "llvm_edges_unlocated": unlocated,
                 "llvm_unknown_counts": len(movable) - len(known) - len(split_inputs),
                 "llvm_private_source_nodes": len(split_inputs),
                 **traffic.stats()}
        if not weight:
            return fallback("no positive exact-count transfer with an unambiguous native site",
                            stats)
        try:
            bridge = Path(path).resolve(strict=True)
            bridge_id = sha256_id(bridge.read_bytes())
            # Leave headroom for the deterministic Python fallback and proposal.
            seconds = min(10.0, max(0.001, (ctx.deadline - time.monotonic()) / 2))
            indices = _bridge_order(bridge, request, seconds)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return fallback(f"LLVM bridge unavailable: {exc}", stats)
        ctx.budget()
        order = [known[index] for index in indices]
        order.extend(uid for uid in movable if uid not in heat or uid in split_inputs)
        return order, "cache_directed_layout", f"bridge {bridge_id}", stats


PASSES = (LLVMCacheLayout,)
