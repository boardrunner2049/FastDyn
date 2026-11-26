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
from ..analysis.firmware_profit import (CHAIN_MODES, COST_MODES, MODEL as PROFIT_MODEL,
                                        DispatchCosts, ProfitError, ProfitIndex, ProfitModel,
                                        admission)
from ..analysis.helper_hot_loop import exact_loop_profile
from ..analysis.firmware_whopr import balanced_partitions, summarize_firmware
from ..analysis.rtos_boundaries import ExpansionAction
from ..formats.region_v1 import unit_exits
from ..link import region_exits_chainable
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult
from .firmware_policy import (PolicyError, PolicyInput, firmware_policy, firmlooper_summary,
                              resolve_selection)

__all__ = ["FirmwareInline", "PASSES", "boundary_residual_exits"]

#: Receipt reasons for exits of the two root members that the region never
#: expands: every exit of the caller except the root call, and every exit of
#: the root continuation.  Both members are region code, so these exits leave
#: the region (and the trusted runtime may chain them), but build_firmware_
#: region never visits them and so records no edge for them.
BOUNDARY_REASONS = ("caller_exit", "continuation_exit")


def boundary_residual_exits(plan: Any, exits_of: Any) -> list[tuple[int, Any, str]]:
    """``(member index, Exit, reason)`` for the never-expanded root members.

    Computed from ``plan.members`` and ``exits_of(uid)`` (the unit's
    :func:`unit_exits`) without touching ``plan.edges``, so ``region_id``
    and the artifact are unchanged.  Exits that already have an edge (the
    root call) are skipped; a continuation that was visited is skipped.
    """
    edged = {(edge.source_member, edge.exit_id) for edge in plan.edges}
    sources = {edge.source_member for edge in plan.edges}
    boundary: list[tuple[int, str]] = [(plan.entry_member, "caller_exit")]
    boundary += [(i, "continuation_exit") for i, member in enumerate(plan.members)
                 if member.role == "continuation" and not member.inline_context
                 and i not in sources]
    out: list[tuple[int, Any, str]] = []
    for index, reason in boundary:
        for exit_ in exits_of(plan.members[index].source_uid):
            if (index, exit_.id) not in edged:
                out.append((index, exit_, reason))
    return out


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
                       "phase": str, "min_call_count": int, "policy_options": dict,
                       "nested": str, "cost_model": str, "residual_goto_tb": str,
                       "entry_chain": str, "cost_poll_ns": float, "cost_lookup_ns": float,
                       "cost_guard_ns": float, "cost_dispatch_ns": float,
                       "cost_byte_ns": float, "cost_margin": float,
                       "cost_min_net_per_call_ns": float, "member_slot_skip": str,
                       "isb_boundary": bool},
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
        # isb_boundary: absent keeps the mode's own rule (only firmlooper-v3
        # has one); true makes every branch out of a member whose last guest
        # instruction is ISB a side exit in any mode, as v3 does.
        isb_param = ctx.params.get("isb_boundary")
        if isb_param is not None and type(isb_param) is not bool:
            return PassResult(Outcome.FAILED, "invalid_params", "isb_boundary must be a boolean")
        if mode == "firmlooper-v3" and isb_param is False:
            return PassResult(Outcome.FAILED, "invalid_params",
                              "firmlooper-v3 always keeps the ISB side exit")
        isb_rule = isb_param is True or mode == "firmlooper-v3"
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
        # Composed sealed+B3 artifacts: may trusted member descriptors skip
        # the goto_tb slots B3 registered (v7 member flags)?  Resolved once
        # here from the target RuntimeIdentity exe and the explicit
        # allowlist; optimize_b3 copies it into B3Plan.member_slot_skip and
        # package_b3 re-checks it.  Pricing does not depend on it.
        from ..validate import _ri_exe, member_slot_skip_decision

        skip_param = ctx.params.get("member_slot_skip", "auto")
        b3_env = getattr(ctx.b3, "env", None)
        skip_runtime = (getattr(b3_env, "runtime", None) or
                        getattr(ctx.b3.snapshot, "runtime", None))
        try:
            skip_ok, skip_why = member_slot_skip_decision(
                skip_param, skip_runtime, getattr(b3_env, "member_skip_exes", ()))
        except ValueError as e:
            return PassResult(Outcome.FAILED, "invalid_params", str(e))
        skip_exe = _ri_exe(skip_runtime)
        if skip_param == "on" and not skip_ok:
            remedy = ("scopt/data/qemu_capabilities.json marks its loader incapable of member "
                      "skip flags; no allowlist overrides that" if skip_why == "known_incapable"
                      else "--v7-member-skip-exe / SCOPT_V7_MEMBER_SKIP_EXES")
            return PassResult(Outcome.FAILED, "member_slot_skip_unsupported_runtime",
                              f"member_slot_skip=on but RuntimeIdentity exe {skip_exe} is "
                              f"{skip_why} ({remedy})")
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
        isb_sources: frozenset[str] = frozenset()
        if isb_param is True and mode != "firmlooper-v3":   # v3 applies it in its own expansion
            from .firmware_policy import _ends_in_isb

            isb_sources = frozenset(u.uid for u in ctx.b3.snapshot.units()
                                    if _ends_in_isb(u, image))
        cost_mode = ctx.params.get("cost_model", "auto")
        residual_mode = ctx.params.get("residual_goto_tb", "auto")
        entry_mode = ctx.params.get("entry_chain", "auto")
        if cost_mode not in COST_MODES or residual_mode not in CHAIN_MODES \
                or entry_mode not in CHAIN_MODES:
            return PassResult(Outcome.FAILED, "invalid_params",
                              f"cost_model must be one of {COST_MODES}; residual_goto_tb and "
                              f"entry_chain one of {CHAIN_MODES}")
        try:
            costs = DispatchCosts.from_params(ctx.params)
        except ProfitError as e:
            return PassResult(Outcome.FAILED, "invalid_params", str(e))
        cost_enabled = cost_mode == "profile" or (cost_mode == "auto" and policy.cost_model_default)
        profile = None
        phase = ctx.params.get("phase", "steady")
        if policy.requires_edges or cost_enabled:
            profile = ctx.profile(phase)
            if not exact_loop_profile(profile, ctx.snapshot_id, phase):
                return PassResult(Outcome.SKIPPED_UNKNOWN,
                                  "hot_loop_exact_profile_required" if policy.requires_edges
                                  else "cost_model_exact_profile_required",
                                  "requires an exact reached phase of the ordinary input snapshot")
        # Composed sealed+B3 pipelines run firmware-inline after the B3
        # passes.  Regions with a member source whose exits B3 guards (grown,
        # registered slots) are priced unchainable whatever the member-skip
        # capability: the pre-B3 member copies drop B3's guards and the
        # internal-edge credit assumes lookups B3 already chains.
        state = getattr(view, "b3", None)
        b3_grown = state.grown_uids() if state is not None else frozenset()
        profit = (ProfitModel(ProfitIndex.build(profile, phase), costs, residual_mode,
                              entry_mode, phase, b3_grown) if cost_enabled else None)
        inputs = PolicyInput(ctx.b3.snapshot, image, (), ctx.params, profile, profit=profit)
        try:
            options = ctx.params.get("policy_options", {})
            signature_evidence = None
            if (mode in ("firmlooper-v2", "firmlooper-v3") and isinstance(options, dict)
                    and "signature_reference" in options):
                from ..analysis.rtos_signatures import recover
                from ..decode.guest_symbols import load_guest_image
                reference = options["signature_reference"]
                if not isinstance(reference, str) or not reference:
                    raise PolicyError("signature_reference must name a symbolized Thumb ELF")
                try:
                    annotated, signature_evidence = recover(image, load_guest_image(reference))
                except (OSError, ValueError) as exc:
                    raise PolicyError(f"cannot recover RTOS signatures: {exc}") from exc
                inputs = replace(inputs, image=annotated)
            if policy.discover_loops:
                loops = policy.discover_loops(inputs)
                discovered = discover_firmware_calls(ctx.b3.snapshot, image,
                                                      verified_entry_pcs=loops.call_pcs)
                inputs = replace(inputs, task_loops=loops)
            else:
                discovered = ctx.analysis("firmware-calls", sid).value
            inputs = replace(inputs, calls=discovered.calls)
            selection = policy.select(inputs)
            if signature_evidence is not None:
                selection = replace(selection, evidence={**selection.evidence, "signatures": signature_evidence})
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
        profit_net = 0.0
        exit_cache: dict[str, tuple[Any, ...]] = {}

        def exits_of(uid: str) -> tuple[Any, ...]:
            if uid not in exit_cache:
                exit_cache[uid] = unit_exits(ctx.b3.snapshot.unit(uid))
            return exit_cache[uid]

        def exit_fields(uid: str, exit_id: str) -> dict[str, Any]:
            exit_ = next((x for x in exits_of(uid) if x.id == exit_id), None)
            return {"exit_kind": None if exit_ is None else exit_.kind.value,
                    "patchable_rel32": exit_ is not None and exit_.insn_offset is not None
                    and exit_.reset_offset is not None}
        for _call in planned_calls:
            ctx.budget()
        expand_nested = (selection.expand_nested if selection.expand_nested is not None
                         else policy.expand_nested)
        # A profit-gated recursive selection keeps the non-recursive plan as
        # a fallback: the whole-region gate picks the better of the two.
        flat_fallback = expand_nested and not policy.expand_nested
        region_units = tuple(ctx.b3.snapshot.units()) if planned_calls else ()
        region_by_uid = {unit.uid: unit for unit in region_units}
        compatibility = firmware_compatibility_index(region_units) if planned_calls else None

        def with_isb(inner):
            """The plan's expansion with the isb_boundary rule in front of it.

            Applied to recursive and flat plans alike.  Root and nested call
            decisions still come from the policy (``inner``); a flat plan has
            none, and every other site expands as without the rule.  The
            nested-gain probes of NestedProfitExpansion are built without it."""
            if not isb_sources:
                return inner

            def expansion(site, context):
                if site.kind == "branch" and site.source_uid in isb_sources:
                    return "isb_boundary"
                return inner(site, context) if inner is not None else ExpansionAction.INLINE
            return expansion

        def plan_call(call, nested=expand_nested):
            return build_firmware_region(
                ctx.b3.snapshot, image, discovered, call, units=region_units,
                by_uid=region_by_uid, compatibility=compatibility,
                expand_nested=nested,
                expansion=with_isb(selection.expansion if nested else None))

        if not planned_calls:
            plans = []
        elif workers == 1:
            plans = [plan_call(call) for call in planned_calls]
        else:
            # LTRANS materialization is pure until edits are admitted below.
            # executor.map preserves the deterministic WPA order.
            with ThreadPoolExecutor(max_workers=min(workers, len(planned_calls)),
                                    thread_name_prefix="scopt-ltrans") as executor:
                plans = list(executor.map(plan_call, planned_calls))

        def depth(plan):
            return max((len(m.inline_context) for m in plan.members), default=0)

        def finalize(call, plan):
            """Trusted checks and edge sanitization of one complete plan.

            Returns ``(plan or None, pending rows, failure)``: ``None`` when the
            root edge itself is unpatchable, ``failure`` a (reason, detail)
            pair for a member whose trusted prefix or context is unproven."""
            if not (trusted and plan.complete):
                return plan, [], None
            from ..link import (TRUSTED_ENTRY_OFFSET, trusted_firmware_edge_problem,
                                trusted_firmware_entry_problem, trusted_return_guard_problem)

            entry = ctx.b3.snapshot.unit(plan.members[plan.entry_member].source_uid)
            entry_context = (entry.key.flags, entry.key.cs_base, entry.key.cflags)
            for member in plan.members:
                unit = ctx.b3.snapshot.unit(member.source_uid)
                problem = trusted_firmware_entry_problem(unit)
                if problem is not None:
                    return None, [], ("trusted_prefix_unproven", f"{unit.uid}: {problem}")
                context = (unit.key.flags, unit.key.cs_base, unit.key.cflags)
                if context != entry_context:
                    return None, [], ("trusted_context_mismatch",
                                      f"{unit.uid}: {context} != {entry_context}")
            sanitized_edges = []
            rows = []
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
                rows.append({"callsite_id": call.callsite_id,
                             "region_id": plan.region_id,
                             "source_uid": unit.uid,
                             "exit_id": edge.exit_id,
                             "inline_context": list(
                                 plan.members[edge.source_member].inline_context),
                             "reason": "unpatchable_native_exit",
                             "detail": problem})
                if (edge.source_member == plan.entry_member and
                        edge.exit_id == call.exit_id):
                    return None, rows, None
                sanitized_edges.append(replace(
                    edge, kind="side_exit", target_member=None, guarded=False,
                    refusal_reason="unpatchable_native_exit"))
            return (replace(plan, trusted=True, trusted_entry_offset=TRUSTED_ENTRY_OFFSET,
                            edges=tuple(sanitized_edges)), rows, None)

        trusted_entries = frozenset(call.source_uid for call in planned_calls) if trusted \
            else frozenset()
        nested_fallbacks: list[dict[str, Any]] = []
        # Flow of each unit that admitted regions execute privately; a later
        # root whose caller is such a member is entered only by the rest.
        captured: Counter[str] = Counter()
        work = list(zip(planned_calls, plans))
        if profit is not None:
            # Price covering regions first: a root whose caller is a member
            # of other planned regions is entered only by the executions
            # those regions leave public (see ``captured``), so it is priced
            # after them.  The sort is stable, so the policy order breaks ties.
            members_of = [frozenset(m.source_uid for m in p.members) if p.complete
                          else frozenset() for p in plans]
            covered = [sum(call.source_uid in members_of[j] for j in range(len(plans)) if j != i)
                       for i, call in enumerate(planned_calls)]
            work = [work[i] for i in sorted(range(len(work)), key=covered.__getitem__)]
        for call, plan in work:
            # Candidates in preference order: the (possibly recursive) plan,
            # then, for a profit-gated recursive selection, the flat plan.
            options: list[tuple[Any, Any, list[dict[str, Any]], Any]] = []
            failure = None
            attempts = [plan]
            if flat_fallback and depth(plan) > 1:
                attempts.append(None)
            for attempt in attempts:
                if attempt is None:
                    attempt = plan_call(call, nested=False)
                final, rows, problem = finalize(call, attempt)
                if problem is not None:
                    failure = failure or problem
                    continue
                est = None
                if profit is not None and final is not None and final.complete:
                    est = profit.estimate(final, ctx.b3.snapshot, exits_of=exits_of,
                                          trusted_entries=trusted_entries - {call.source_uid},
                                          captured=captured)
                options.append((attempt, final, rows, est))
            if not options:
                return PassResult(Outcome.FAILED, *failure)
            chosen = options[0]
            if len(options) > 1:
                def rank(option):
                    _a, final, _r, est = option
                    if final is None or not final.complete:
                        return (0, 0.0)
                    if est is None:
                        return (1, 0.0)
                    return (2 if admission(est, costs) is None else 1, est.net_ns)
                best = max(options, key=rank)
                if rank(best) > rank(chosen):
                    nested_fallbacks.append({
                        "callsite_id": call.callsite_id,
                        "reason": ("trusted_prefix_unproven" if failure else
                                   "flat_region_more_profitable"),
                        "recursive_net_ns": None if chosen[3] is None
                        else round(chosen[3].net_ns, 3),
                        "flat_net_ns": None if best[3] is None else round(best[3].net_ns, 3)})
                    chosen = best
            elif failure is not None:
                nested_fallbacks.append({"callsite_id": call.callsite_id,
                                         "reason": failure[0], "recursive_net_ns": None,
                                         "flat_net_ns": None if chosen[3] is None
                                         else round(chosen[3].net_ns, 3)})
            attempt, plan, rows, est = chosen
            for row in rows:
                pending.append(row)
                refused["unpatchable_native_exit"] += 1
            if plan is None:
                continue
            for row in plan.pending:
                reason = str(row.get("reason", "incomplete"))
                refused[reason] += 1
                pending.append(dict(row))
            if not plan.complete:
                continue
            estimate = None if est is None else est.to_json()
            if est is not None:
                reason = admission(est, costs)
                if reason:
                    residual.append({**selection.site_evidence.get(call.callsite_id, {}),
                                     "callsite_id": call.callsite_id,
                                     "source_uid": call.source_uid,
                                     "region_id": plan.region_id, "reason": reason,
                                     "profit": estimate})
                    refused[reason] += 1
                    continue
                profit_net += est.net_ns
                for member, flow in zip(plan.members, est.member_flow):
                    captured[member.source_uid] += flow
            max_depth = max(max_depth, depth(plan))
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
                             "trusted_entry_offset": plan.trusted_entry_offset,
                             # The PRICED value (region_exits_chainable
                             # given the units earlier B3 passes grew).  The
                             # linker decides member descriptors from the
                             # skip masks (sidecar x_b3.firmware_region_links).
                             "residual_exits_chainable":
                                 region_exits_chainable(plan, b3_grown),
                             "expansion_depth": depth(plan),
                             "nested_inlined_calls": sum(
                                 edge.kind == "call" and edge.target_member is not None
                                 and plan.members[edge.source_member].inline_context != ()
                                 for edge in plan.edges),
                             **({"profit": estimate} if estimate is not None else {})})
            residual.extend({"callsite_id": call.callsite_id,
                             "source_member": edge.source_member,
                             "source_role": plan.members[edge.source_member].role,
                             "exit_id": edge.exit_id,
                             "guest_target": edge.guest_target,
                             "reason": edge.refusal_reason,
                             **exit_fields(plan.members[edge.source_member].source_uid,
                                           edge.exit_id)}
                            for edge in plan.edges if edge.target_member is None)
            residual.extend({"callsite_id": call.callsite_id,
                             "source_member": index,
                             "source_role": plan.members[index].role,
                             "exit_id": exit_.id,
                             "guest_target": exit_.static_dest_vaddr,
                             "reason": reason,
                             **exit_fields(plan.members[index].source_uid, exit_.id)}
                            for index, exit_, reason in boundary_residual_exits(plan, exits_of))
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
        # FirmLooper v3 (analysis revision 2): per-task verdicts, and the
        # image verdict as the skip reason when no root was planned.
        summary = None
        if inputs.task_loops is not None and getattr(inputs.task_loops, "revision", 1) >= 2:
            summary = firmlooper_summary(inputs.task_loops, discovered.calls, residual,
                                         {row["callsite_id"] for row in embedded},
                                         {call.callsite_id for call in planned_calls})
        stats = {"discovered": float(len(discovered.calls)),
                 "embedded": float(len(embedded)), "pending": float(len(pending)),
                 "actual_expansion_depth": float(max_depth),
                 "private_guarded_returns": float(sum(row["private_guarded_returns"]
                                                      for row in embedded)),
                 "whopr_partitions": float(len(partition_rows)),
                 "residual_boundary_exits": float(sum(row.get("reason") in BOUNDARY_REASONS
                                                      for row in residual)),
                 "cost_model_enabled": float(profit is not None),
                 "profit_net_ns": round(profit_net, 3),
                 "profit_refused": float(refused["not_profitable"]),
                 "nested_inlined_calls": float(sum(row["nested_inlined_calls"]
                                                   for row in embedded)),
                 "member_slot_skip": 1.0 if skip_ok else 0.0,
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
                              "expand_nested": expand_nested,
                              "cost_model": {"enabled": profit is not None,
                                             "mode": cost_mode, "model": PROFIT_MODEL,
                                             "phase": phase if profit is not None else None,
                                             "profile_id": getattr(profile, "profile_id", None)
                                             if profit is not None else None,
                                             "constants": costs.to_json(),
                                             "residual_goto_tb": residual_mode,
                                             "entry_chain": entry_mode},
                              "nested_decisions": getattr(selection.expansion, "report",
                                                          lambda: None)(),
                              "nested_fallbacks": nested_fallbacks,
                              "isb_boundary": isb_rule,
                              "member_slot_skip": {"enabled": skip_ok, "reason": skip_why,
                                                   "param": skip_param, "exe": skip_exe},
                              "interrupts": interrupts, "icount": icount,
                              "planner": planner,
                              "max_hotness": None,
                              "max_growth": None, "max_sites": None,
                              "max_members": None, "max_depth": None if expand_nested else 1,
                              "max_iterations": None, "max_wall_s": None}}
        if summary is not None:
            receipt["policy"]["selection_summary"] = summary
        if isb_param is True and mode != "firmlooper-v3":
            receipt["policy"]["isb_sources"] = len(isb_sources)
        if embedded:
            return PassResult(Outcome.CANDIDATE, "firmware_regions_emitted",
                              json.dumps(receipt, sort_keys=True),
                              tuple(dict.fromkeys(affected)), stats=stats)
        reason = ("no_profitable_call" if refused["not_profitable"] or
                  refused["profit_estimate_unknown"] else
                  summary["verdict"] if summary is not None and not planned_calls else
                  "no_eligible_call" if discovered.calls else "no_firmware_call")
        return PassResult(Outcome.SKIPPED_UNKNOWN, reason,
                          json.dumps(receipt, sort_keys=True), stats=stats)


PASSES = (FirmwareInline,)
