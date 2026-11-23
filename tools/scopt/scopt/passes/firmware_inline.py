"""``firmware-inline@1``: private regions for verified firmware calls.

Exhaustive mode visits every
captured call and uses ``None`` for all profitability, growth, member, depth,
iteration and wall-clock bounds.  Missing capture and recursive call edges
are reported rather than inferred unreachable.
"""

from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, ClassVar

from ..analysis.api import ScopeRef
from ..analysis.b3_inputs import guest_image_for
from ..analysis.firmware_calls import firmware_compatibility_index
from ..analysis.firmware_regions import build_firmware_region
from ..analysis.helper_hot_loop import exact_loop_profile, observed_loop_units
from ..analysis.firmware_whopr import balanced_partitions
from ..formats.enums import ExitKind
from ..formats.region_v1 import unit_exits
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult

__all__ = ["FirmwareInline", "PASSES"]


def _hot_loop_calls(snapshot, calls, profile, phase, minimum):
    """Rank exact observed normal transfers of verified BLs to a captured full key."""
    edges = [e for e in profile.edges(phase) if e["status"] == "exact" and not e["flags"]]
    loops = observed_loop_units(edges)
    transfers = Counter()
    for e in edges:
        transfers[e["src"], e["dst"], e["kind"]] += e["count"]
    kept, residual, selected_counts = [], [], {}
    for call in calls:
        count = None
        if not call.eligible:
            reason = call.refusal_reasons[0]
        elif call.source_uid not in loops:
            reason = "outside_observed_loop"
        elif call.source_uid == call.callee_candidates[0]:
            reason = "recursive_call_edge"
        else:
            exits = unit_exits(snapshot.unit(call.source_uid))
            source_exit = next((e for e in exits if e.id == call.exit_id), None)
            kinds = ()
            if (source_exit and source_exit.kind == ExitKind.GOTO_TB
                    and source_exit.slot in (0, 1) and sum(
                        e.kind == ExitKind.GOTO_TB and e.slot == source_exit.slot
                        for e in exits) == 1):
                kinds = (f"chain{source_exit.slot}",)
            elif (source_exit and source_exit.kind == ExitKind.GOTO_PTR
                  and sum(e.kind == ExitKind.GOTO_PTR for e in exits) == 1):
                kinds = ("lookup_hit", "lookup_miss")
            reason = "call_edge_count_unavailable"
            if kinds:
                count = sum(transfers[call.source_uid, call.callee_candidates[0], kind]
                            for kind in kinds)
                if count >= minimum:
                    kept.append(call)
                    selected_counts[call.callsite_id] = count
                    continue
                reason = "below_min_call_count"
        residual.append({"callsite_id": call.callsite_id, "source_uid": call.source_uid,
                         "reason": reason, "observed_call_edges": count})
    kept.sort(key=lambda c: (-selected_counts[c.callsite_id], c.callsite_id))
    return kept, residual, selected_counts


class FirmwareInline:
    descriptor: ClassVar[PassDescriptor] = PassDescriptor(
        name="firmware-inline", version=1, unit_scope="snapshot",
        requires=(("firmware-calls", 1), ("firmware-whopr", 1)),
        capabilities=frozenset({EditKind.EMIT_FIRMWARE_REGION}),
        budgets=Budgets(max_wall_s=None, max_code_growth_bytes=None,
                        max_region_members=None, max_context_versions=None,
                        max_units=None),
        invalidates=frozenset({"code", "meta", "layout"}),
        params_schema={"mode": str, "trust": str, "interrupts": str, "icount": str,
                       "planner": str, "partitions": int, "workers": int,
                       "phase": str, "min_call_count": int},
    )

    def run(self, view: Any, ctx: Any, edits: Any) -> PassResult:
        if ctx.b3 is None:
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "capability_not_enabled",
                              "firmware-inline requires the region-capable B3 packager")
        mode = ctx.params.get("mode", "exhaustive")
        if mode not in ("exhaustive", "hot-loop"):
            return PassResult(Outcome.FAILED, "invalid_params",
                              "firmware-inline mode must be 'exhaustive' or 'hot-loop'")
        trust = ctx.params.get("trust", "safe")
        interrupts = ctx.params.get("interrupts", "preserved")
        icount = ctx.params.get("icount", "preserved")
        trusted = trust == "user"
        if trusted:
            if (interrupts, icount) != ("disabled", "ignored"):
                return PassResult(
                    Outcome.FAILED, "invalid_trusted_contract",
                    "trust=user requires interrupts=disabled and icount=ignored")
            from ..formats.enums import RunInfoFlag

            ri = ctx.b3.snapshot.run_info
            if ri is None or ri.flags & RunInfoFlag.ICOUNT_ENABLED:
                return PassResult(Outcome.FAILED, "trusted_icount_active",
                                  "trusted firmware-inline requires captured icount=off")
        elif (trust, interrupts, icount) != ("safe", "preserved", "preserved"):
            return PassResult(Outcome.FAILED, "invalid_params",
                              "safe mode requires preserved interrupts and icount")
        if not trusted and (not ctx.b3.layout_bound or ctx.b3.offsets is None):
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "guard_layout_unbound",
                              ctx.b3.layout_reason or "")
        sid = ScopeRef("snapshot", ctx.snapshot_id)
        discovered = ctx.analysis("firmware-calls", sid).value
        planner = ctx.params.get("planner", "legacy")
        if planner not in ("legacy", "whopr"):
            return PassResult(Outcome.FAILED, "invalid_params",
                              "firmware-inline planner must be 'legacy' or 'whopr'")
        partition_rows: list[dict[str, Any]] = []
        planned_calls = list(discovered.calls)
        residual = []
        selection = {}
        selected_counts = {}
        if mode == "hot-loop":
            phase = ctx.params.get("phase", "steady")
            minimum = ctx.params.get("min_call_count", 2)
            if type(minimum) is not int or minimum < 2:
                return PassResult(Outcome.FAILED, "invalid_params", "min_call_count must be >= 2")
            profile = ctx.profile(phase)
            if not exact_loop_profile(profile, ctx.snapshot_id, phase):
                return PassResult(Outcome.SKIPPED_UNKNOWN, "hot_loop_exact_profile_required",
                                  "requires an exact reached phase of the ordinary input snapshot")
            planned_calls, residual, selected_counts = _hot_loop_calls(
                ctx.b3.snapshot, planned_calls, profile, phase, minimum)
            selection = {"profile_id": profile.profile.profile_id, "phase": phase,
                         "min_call_count": minimum, "expand_nested": False}
        workers = 1
        if planner == "whopr":
            partitions = ctx.params.get("partitions", 4)
            workers = ctx.params.get("workers", partitions)
            if (type(partitions) is not int or partitions < 1 or
                    type(workers) is not int or workers < 1):
                return PassResult(Outcome.FAILED, "invalid_params",
                                  "WHOPR partitions and workers must be positive integers")
            whopr = ctx.analysis("firmware-whopr", sid).value
            partition_sccs = balanced_partitions(whopr, partitions)
            owner = {uid: scc_index for scc_index, scc in enumerate(whopr.sccs)
                     for uid in scc}
            partition_of = {scc_index: pi for pi, row in enumerate(partition_sccs)
                            for scc_index in row}
            planned_calls.sort(key=lambda call: (
                partition_of.get(owner.get(call.source_uid, -1), len(partition_sccs)),
                call.insn_vaddr, call.callsite_id))
            by_uid = {node.uid: node for node in whopr.nodes}
            for pi, row in enumerate(partition_sccs):
                uids = tuple(uid for scc_index in row for uid in whopr.sccs[scc_index])
                uid_set = set(uids)
                partition_rows.append({
                    "partition": pi, "sccs": list(row), "source_uids": list(uids),
                    "native_bytes": sum(by_uid[uid].native_bytes for uid in uids),
                    "calls": sum(call.source_uid in uid_set for call in planned_calls),
                })
        image = guest_image_for(ctx.b3.snapshot, ctx.bundle)
        if image is None:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "guest_image_missing",
                              "the exact firmware ELF is required")
        refused: Counter[str] = Counter()
        affected: list[str] = []
        embedded: list[dict[str, Any]] = []
        pending = [dict(row) for row in discovered.diagnostics if row.get("pending", True)]
        exclusions = [dict(row) for row in discovered.diagnostics if not row.get("pending", True)]
        source_identities: dict[str, dict[str, Any]] = {}
        for row in pending:
            refused[str(row.get("reason", "incomplete"))] += 1
        max_depth = 0
        for _call in planned_calls:
            ctx.budget()
        if (planner == "whopr" or mode == "hot-loop") and planned_calls:
            # LTRANS materialization is pure until edits are admitted below.
            # executor.map preserves the deterministic WPA order.
            region_units = tuple(ctx.b3.snapshot.units())
            region_by_uid = {unit.uid: unit for unit in region_units}
            compatibility = firmware_compatibility_index(region_units)
            with ThreadPoolExecutor(max_workers=min(workers, len(planned_calls)),
                                    thread_name_prefix="scopt-ltrans") as executor:
                plans = list(executor.map(
                    lambda call: build_firmware_region(
                        ctx.b3.snapshot, image, discovered, call,
                        units=region_units, by_uid=region_by_uid,
                        compatibility=compatibility, expand_nested=mode != "hot-loop"), planned_calls))
        else:
            plans = [build_firmware_region(ctx.b3.snapshot, image, discovered, call)
                     for call in planned_calls]
        for call, plan in zip(planned_calls, plans):
            if trusted and plan.complete:
                from ..link import (TRUSTED_ENTRY_OFFSET, trusted_firmware_edge_problem,
                                    trusted_firmware_entry_problem)

                entry = ctx.b3.snapshot.unit(plan.members[plan.entry_member].source_uid)
                entry_context = (entry.key.flags, entry.key.cs_base, entry.key.cflags)
                for member in plan.members:
                    unit = ctx.b3.snapshot.unit(member.source_uid)
                    problem = trusted_firmware_entry_problem(unit)
                    if problem is not None:
                        return PassResult(Outcome.FAILED, "trusted_prefix_unproven",
                                          f"{unit.uid}: {problem}")
                    context = (unit.key.flags, unit.key.cs_base, unit.key.cflags)
                    if context != entry_context:
                        return PassResult(Outcome.FAILED, "trusted_context_mismatch",
                                          f"{unit.uid}: {context} != {entry_context}")
                sanitized_edges = []
                root_edge_unpatchable = False
                for edge in plan.edges:
                    if edge.target_member is None:
                        sanitized_edges.append(edge)
                        continue
                    unit = ctx.b3.snapshot.unit(plan.members[edge.source_member].source_uid)
                    problem = trusted_firmware_edge_problem(unit, edge.exit_id)
                    if problem is None:
                        sanitized_edges.append(edge)
                        continue
                    row = {"callsite_id": call.callsite_id,
                           "region_id": plan.region_id,
                           "source_uid": unit.uid,
                           "exit_id": edge.exit_id,
                           "inline_context": list(
                               plan.members[edge.source_member].inline_context),
                           "reason": "unpatchable_native_exit",
                           "detail": problem}
                    pending.append(row)
                    refused["unpatchable_native_exit"] += 1
                    if (edge.source_member == plan.entry_member and
                            edge.exit_id == call.exit_id):
                        root_edge_unpatchable = True
                        break
                    sanitized_edges.append(replace(
                        edge, kind="side_exit", target_member=None, guarded=False,
                        refusal_reason="unpatchable_native_exit"))
                if root_edge_unpatchable:
                    continue
                plan = replace(plan, trusted=True,
                               trusted_entry_offset=TRUSTED_ENTRY_OFFSET,
                               edges=tuple(sanitized_edges))
            max_depth = max(max_depth, max((len(m.inline_context) for m in plan.members), default=0))
            for row in plan.pending:
                reason = str(row.get("reason", "incomplete"))
                refused[reason] += 1
                pending.append(dict(row))
            if not plan.complete:
                continue
            edits.emit_firmware_region(plan)
            embedded.append({"callsite_id": call.callsite_id,
                             **({"observed_call_edges": selected_counts[call.callsite_id]}
                                if mode == "hot-loop" else {}),
                             "region_id": plan.region_id,
                             "source_uids": list(plan.source_uids),
                             "members": len(plan.members),
                             "trusted": plan.trusted,
                             "trusted_entry_offset": plan.trusted_entry_offset})
            residual.extend({"callsite_id": call.callsite_id,
                             "source_member": edge.source_member,
                             "exit_id": edge.exit_id,
                             "guest_target": edge.guest_target,
                             "reason": edge.refusal_reason}
                            for edge in plan.edges if edge.target_member is None)
            for member in plan.members:
                source_identities.setdefault(member.source_uid, {
                    "source_uid": member.source_uid,
                    "source_hash": member.source_hash,
                    "translation_key": {"page_addr0": member.source_key.page_addr0,
                                        "page_addr1": member.source_key.page_addr1,
                                        "flags": member.source_key.flags,
                                        "cs_base": member.source_key.cs_base,
                                        "cflags": member.source_key.cflags}})
            affected.extend(plan.source_uids)
        stats = {"discovered": float(len(discovered.calls)),
                 "embedded": float(len(embedded)), "pending": float(len(pending)),
                 "actual_expansion_depth": float(max_depth),
                 "whopr_partitions": float(len(partition_rows)),
                 **{f"refused:{k}": float(v) for k, v in sorted(refused.items())}}
        receipt = {"discovered": [call.to_json() for call in discovered.calls],
                   "embedded": embedded, "residual": residual,
                   "pending": pending, "capture_exclusions": exclusions,
                   "source_identities": [source_identities[k]
                                         for k in sorted(source_identities)],
                   "actual_expansion_depth": max_depth,
                   "emitted_size": None, "loaded_artifact_id": None,
                   "whopr": {"enabled": planner == "whopr",
                             "stages": ["LGEN", "WPA", "LTRANS"] if planner == "whopr" else [],
                             "partitions": partition_rows,
                             "workers": workers,
                             "virtual_clones": "inline_context"},
                   "policy": {"mode": mode, "trust": trust, **selection,
                              "interrupts": interrupts, "icount": icount,
                              "planner": planner,
                              "max_hotness": None,
                              "max_growth": None, "max_sites": None,
                              "max_members": None, "max_depth": 1 if mode == "hot-loop" else None,
                              "max_iterations": None, "max_wall_s": None}}
        if embedded:
            return PassResult(Outcome.CANDIDATE, "firmware_regions_emitted",
                              json.dumps(receipt, sort_keys=True),
                              tuple(dict.fromkeys(affected)), stats=stats)
        reason = "no_eligible_call" if discovered.calls else "no_firmware_call"
        return PassResult(Outcome.SKIPPED_UNKNOWN, reason,
                          json.dumps(receipt, sort_keys=True), stats=stats)


PASSES = (FirmwareInline,)
