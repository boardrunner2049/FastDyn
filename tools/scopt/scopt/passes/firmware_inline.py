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
from ..analysis.firmware_calls import discover_firmware_calls, firmware_compatibility_index
from ..analysis.firmware_regions import build_firmware_region
from ..analysis.helper_hot_loop import exact_loop_profile
from ..analysis.firmware_whopr import balanced_partitions, summarize_firmware
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult
from .firmware_policy import PolicyError, PolicyInput, firmware_policy, resolve_selection

__all__ = ["FirmwareInline", "PASSES"]


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
                       "phase": str, "min_call_count": int, "policy_options": dict},
    )

    def run(self, view: Any, ctx: Any, edits: Any) -> PassResult:
        if ctx.b3 is None:
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "capability_not_enabled",
                              "firmware-inline requires the region-capable B3 packager")
        mode = ctx.params.get("mode", "exhaustive")
        try:
            policy = firmware_policy(mode)
        except PolicyError as e:
            return PassResult(Outcome.FAILED, "invalid_params", str(e))
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
        planner = ctx.params.get("planner", "legacy")
        if planner not in ("legacy", "whopr"):
            return PassResult(Outcome.FAILED, "invalid_params",
                              "firmware-inline planner must be 'legacy' or 'whopr'")
        partition_rows: list[dict[str, Any]] = []
        image = guest_image_for(ctx.b3.snapshot, ctx.bundle)
        if image is None:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "guest_image_missing",
                              "the exact firmware ELF is required")
        profile = None
        if policy.requires_edges:
            phase = ctx.params.get("phase", "steady")
            profile = ctx.profile(phase)
            if not exact_loop_profile(profile, ctx.snapshot_id, phase):
                return PassResult(Outcome.SKIPPED_UNKNOWN, "hot_loop_exact_profile_required",
                                  "requires an exact reached phase of the ordinary input snapshot")
        inputs = PolicyInput(ctx.b3.snapshot, image, (), ctx.params, profile)
        try:
            if policy.discover_loops:
                loops = policy.discover_loops(inputs)
                discovered = discover_firmware_calls(ctx.b3.snapshot, image,
                                                      verified_entry_pcs=loops.call_pcs)
                inputs = replace(inputs, task_loops=loops)
            else:
                discovered = ctx.analysis("firmware-calls", sid).value
            inputs = replace(inputs, calls=discovered.calls)
            selection = policy.select(inputs)
            planned_calls, residual = resolve_selection(inputs, selection)
        except PolicyError as e:
            return PassResult(Outcome.FAILED, "invalid_params", str(e))
        workers = 1
        if planner == "whopr":
            partitions = ctx.params.get("partitions", 4)
            workers = ctx.params.get("workers", partitions)
            if (type(partitions) is not int or partitions < 1 or
                    type(workers) is not int or workers < 1):
                return PassResult(Outcome.FAILED, "invalid_params",
                                  "WHOPR partitions and workers must be positive integers")
            if inputs.task_loops is None:
                whopr = ctx.analysis("firmware-whopr", sid).value
            else:
                whopr = summarize_firmware(ctx.snapshot_id, tuple(ctx.b3.snapshot.units()),
                                           discovered, inputs.task_loops.call_pcs)
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
        if planned_calls:
            # LTRANS materialization is pure until edits are admitted below.
            # executor.map preserves the deterministic WPA order.
            region_units = tuple(ctx.b3.snapshot.units())
            region_by_uid = {unit.uid: unit for unit in region_units}
            compatibility = firmware_compatibility_index(region_units)

            def plan_call(call):
                return build_firmware_region(
                    ctx.b3.snapshot, image, discovered, call, units=region_units,
                    by_uid=region_by_uid, compatibility=compatibility,
                    expand_nested=policy.expand_nested, expansion=selection.expansion)

            if workers == 1:
                plans = [plan_call(call) for call in planned_calls]
            else:
                with ThreadPoolExecutor(max_workers=min(workers, len(planned_calls)),
                                        thread_name_prefix="scopt-ltrans") as executor:
                    plans = list(executor.map(plan_call, planned_calls))
        else:
            plans = []
        for call, plan in zip(planned_calls, plans):
            if trusted and plan.complete:
                from ..link import (TRUSTED_ENTRY_OFFSET, trusted_firmware_edge_problem,
                                    trusted_firmware_entry_problem, trusted_return_guard_problem)

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
                    if problem is None and edge.kind == "return":
                        target = ctx.b3.snapshot.unit(plan.members[edge.target_member].source_uid)
                        problem = trusted_return_guard_problem(
                            unit, edge.exit_id, target.key, ctx.b3.offsets,
                            ctx.b3.snapshot.header.code_address,
                            cpu_model=(ctx.bundle or {}).get("target", {}).get("cpu"))
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
            embedded.append({**selection.site_evidence.get(call.callsite_id, {}),
                             "callsite_id": call.callsite_id,
                             "region_id": plan.region_id,
                             "source_uids": list(plan.source_uids),
                             "members": len(plan.members),
                             "private_guarded_returns": sum(edge.kind == "return" and
                                                            edge.target_member is not None
                                                            for edge in plan.edges),
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
                 "private_guarded_returns": float(sum(row["private_guarded_returns"]
                                                      for row in embedded)),
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
                   "policy": {**selection.evidence, "mode": mode, "trust": trust,
                              "expand_nested": policy.expand_nested,
                              "interrupts": interrupts, "icount": icount,
                              "planner": planner,
                              "max_hotness": None,
                              "max_growth": None, "max_sites": None,
                              "max_members": None, "max_depth": None if policy.expand_nested else 1,
                              "max_iterations": None, "max_wall_s": None}}
        if embedded:
            return PassResult(Outcome.CANDIDATE, "firmware_regions_emitted",
                              json.dumps(receipt, sort_keys=True),
                              tuple(dict.fromkeys(affected)), stats=stats)
        reason = "no_eligible_call" if discovered.calls else "no_firmware_call"
        return PassResult(Outcome.SKIPPED_UNKNOWN, reason,
                          json.dumps(receipt, sort_keys=True), stats=stats)


PASSES = (FirmwareInline,)
