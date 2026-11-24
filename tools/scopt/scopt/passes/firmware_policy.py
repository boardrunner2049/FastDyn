"""Firmware-region selection policies. Policies can nominate captured call IDs only."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

from ..analysis.helper_hot_loop import observed_loop_units
from ..analysis.rtos_boundaries import ExpansionAction, ExpansionSite, TaskBoundaries, residual_reason
from ..decode.guest_symbols import GuestImage
from ..formats.enums import ExitKind
from ..formats.profile_v1 import ProfileView
from ..formats.region_v1 import unit_exits
from ..model.cache_snapshot import CacheSnapshot
from ..model.firmware_region import FirmwareCallSite


@dataclass(frozen=True)
class PolicyInput:
    snapshot: CacheSnapshot
    image: GuestImage
    calls: tuple[FirmwareCallSite, ...]
    params: Mapping[str, Any]
    profile: ProfileView | None = None
    task_loops: Any = None


@dataclass(frozen=True)
class PolicySelection:
    roots: tuple[str, ...]
    refusals: Mapping[str, str] = field(default_factory=dict)
    site_evidence: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    expansion: Callable[[ExpansionSite, tuple[str, ...]], ExpansionAction] | None = None


@dataclass(frozen=True)
class FirmwarePolicy:
    select: Callable[[PolicyInput], PolicySelection]
    expand_nested: bool
    requires_edges: bool = False
    discover_loops: Callable[[PolicyInput], Any] | None = None


class PolicyError(ValueError):
    pass


def _exhaustive(inputs):
    return PolicySelection(tuple(call.callsite_id for call in inputs.calls))


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


def _hot_loop(inputs):
    phase = inputs.params.get("phase", "steady")
    minimum = inputs.params.get("min_call_count", 2)
    if type(minimum) is not int or minimum < 2:
        raise PolicyError("min_call_count must be >= 2")
    kept, residual, counts = _hot_loop_calls(inputs.snapshot, inputs.calls, inputs.profile,
                                             phase, minimum)
    site_evidence = {uid: {"observed_call_edges": count} for uid, count in counts.items()}
    for row in residual:
        site_evidence[row["callsite_id"]] = {"observed_call_edges": row["observed_call_edges"]}
    return PolicySelection(
        tuple(call.callsite_id for call in kept),
        {row["callsite_id"]: row["reason"] for row in residual}, site_evidence,
        {"profile_id": inputs.profile.profile.profile_id, "phase": phase, "min_call_count": minimum})


def _loop_evidence(inputs, *, v2=False):
    from ..analysis.task_loops import discover_task_loops

    options = inputs.params.get("policy_options", {})
    allowed = {"task_entries", "include_terminal", "boundaries", "signature_reference"} if v2 else {"task_entries"}
    if not isinstance(options, dict) or options.keys() - allowed:
        raise PolicyError(f"unknown FirmLooper options; allowed: {sorted(allowed)}")
    entries = options.get("task_entries", [])
    if not isinstance(entries, list) or any(type(x) not in (int, str) for x in entries):
        raise PolicyError("task_entries must be a list of task symbols or addresses")
    if any(type(options[k]) is not bool for k in ("include_terminal", "boundaries") if k in options):
        raise PolicyError("include_terminal and boundaries must be boolean")
    return discover_task_loops(inputs.image, task_entries=entries,
                               include_terminal=options.get("include_terminal", v2))


def _v2_loops(inputs):
    return _loop_evidence(inputs, v2=True)


def _firmlooper(inputs, *, v2=False):
    loops = inputs.task_loops or _loop_evidence(inputs, v2=v2)
    expansion = None
    if v2 and inputs.params.get("policy_options", {}).get("boundaries", True):
        boundaries = TaskBoundaries(inputs.image)
        # Materialize classifications before parallel region planning. Every
        # internal target must start an existing captured translation.
        actions = {pc: boundaries.classify(pc) for pc in
                   {u.vaddrs[0] for u in inputs.snapshot.units() if u.vaddrs}}
        def expansion(site, context):
            return actions.get(site.target_pc, ExpansionAction.PUBLIC)
    roots, refusals = [], {}
    for call in inputs.calls:
        if call.insn_vaddr not in loops.call_pcs:
            refusals[call.callsite_id] = "outside_persistent_task_loop"
        elif not call.eligible:
            refusals[call.callsite_id] = call.refusal_reasons[0]
        elif call.source_uid == call.callee_candidates[0]:
            refusals[call.callsite_id] = "recursive_call_edge"
        elif expansion and boundaries.classify(call.insn_vaddr) is not ExpansionAction.INLINE:
            refusals[call.callsite_id] = "rtos_public_caller"
        else:
            roots.append(call.callsite_id)
    return PolicySelection(tuple(roots), refusals, evidence={"task_loops": loops.report(),
                           "rtos_boundaries": expansion is not None}, expansion=expansion)


def _firmlooper_v2(inputs):
    return _firmlooper(inputs, v2=True)


_BUILTINS = {
    "exhaustive": FirmwarePolicy(_exhaustive, expand_nested=True),
    "hot-loop": FirmwarePolicy(_hot_loop, expand_nested=False, requires_edges=True),
    "firmlooper": FirmwarePolicy(_firmlooper, expand_nested=True, discover_loops=_loop_evidence),
    "firmlooper-v2": FirmwarePolicy(_firmlooper_v2, expand_nested=True, discover_loops=_v2_loops),
}


def firmware_policy(name: str) -> FirmwarePolicy:
    if not isinstance(name, str) or not name:
        raise PolicyError("firmware policy name must be a nonempty string")
    if name in _BUILTINS:
        return _BUILTINS[name]
    matches = tuple(metadata.entry_points(group="scopt.firmware_inline_policies", name=name))
    if len(matches) != 1:
        raise PolicyError(f"expected one installed firmware policy named {name!r}, found {len(matches)}")
    try:
        policy = matches[0].load()
    except (ImportError, AttributeError) as exc:
        raise PolicyError(f"cannot load firmware policy {name!r}: {exc}") from exc
    if (not isinstance(policy, FirmwarePolicy) or not callable(policy.select)
            or type(policy.expand_nested) is not bool or type(policy.requires_edges) is not bool):
        raise PolicyError(f"firmware policy {name!r} must export a FirmwarePolicy")
    return policy


def resolve_selection(inputs: PolicyInput, selected: PolicySelection):
    """Resolve nominated IDs using the original discovered calls, before region legality checks."""
    if not isinstance(selected, PolicySelection):
        raise PolicyError("a firmware policy must return a PolicySelection")
    calls = {c.callsite_id: c for c in inputs.calls}
    roots = set(selected.roots)
    if (len(roots) != len(selected.roots) or roots & selected.refusals.keys()
            or not (roots | selected.refusals.keys() | selected.site_evidence.keys()) <= calls.keys()):
        raise PolicyError("unknown, duplicated or contradictory firmware call-site selection")
    residual = [{**selected.site_evidence.get(uid, {}), "callsite_id": uid,
                 "source_uid": call.source_uid,
                 "reason": selected.refusals.get(uid, "excluded_by_policy")}
                for uid, call in calls.items() if uid not in roots]
    admitted = []
    for uid in selected.roots:
        call = calls[uid]
        if selected.expansion is not None:
            site = ExpansionSite("root", call.source_uid, call.insn_vaddr, call.target_vaddr)
            reason = residual_reason(selected.expansion(site, ()))
            if reason:
                residual.append({**selected.site_evidence.get(uid, {}), "callsite_id": uid,
                                 "source_uid": call.source_uid, "reason": reason})
                continue
        admitted.append(call)
    return admitted, residual
