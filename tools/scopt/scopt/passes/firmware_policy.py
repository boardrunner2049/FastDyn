"""Firmware-region selection policies. Policies can nominate captured call IDs only."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

from ..analysis.helper_hot_loop import observed_loop_units
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


@dataclass(frozen=True)
class PolicySelection:
    roots: tuple[str, ...]
    refusals: Mapping[str, str] = field(default_factory=dict)
    site_evidence: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FirmwarePolicy:
    select: Callable[[PolicyInput], PolicySelection]
    expand_nested: bool
    requires_edges: bool = False


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


def _firmlooper(inputs):
    from ..analysis.task_loops import discover_task_loops

    options = inputs.params.get("policy_options", {})
    if not isinstance(options, dict) or options.keys() - {"task_entries"}:
        raise PolicyError("FirmLooper accepts policy_options.task_entries")
    entries = options.get("task_entries", [])
    if not isinstance(entries, list) or any(type(x) not in (int, str) for x in entries):
        raise PolicyError("task_entries must be a list of task symbols or addresses")
    loops = discover_task_loops(inputs.image, task_entries=entries)
    roots, refusals = [], {}
    for call in inputs.calls:
        if call.insn_vaddr not in loops.call_pcs:
            refusals[call.callsite_id] = "outside_persistent_task_loop"
        elif not call.eligible:
            refusals[call.callsite_id] = call.refusal_reasons[0]
        elif call.source_uid == call.callee_candidates[0]:
            refusals[call.callsite_id] = "recursive_call_edge"
        else:
            roots.append(call.callsite_id)
    return PolicySelection(tuple(roots), refusals, evidence={"task_loops": loops.report()})


_BUILTINS = {
    "exhaustive": FirmwarePolicy(_exhaustive, expand_nested=True),
    "hot-loop": FirmwarePolicy(_hot_loop, expand_nested=False, requires_edges=True),
    "firmlooper": FirmwarePolicy(_firmlooper, expand_nested=True),
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
    return [calls[uid] for uid in selected.roots], residual
