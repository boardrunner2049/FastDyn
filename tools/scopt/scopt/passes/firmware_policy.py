"""Firmware-region selection policies. Policies can nominate captured call IDs only."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from importlib import metadata
from typing import Any

from ..analysis.helper_hot_loop import FIRMWARE_LOOP_KINDS, observed_loop_units, real_exit_null
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
    #: :class:`scopt.analysis.firmware_profit.ProfitModel` when the pass
    #: prices regions; policies may use it to gate nested expansion.
    profit: Any = None


@dataclass(frozen=True)
class PolicySelection:
    roots: tuple[str, ...]
    refusals: Mapping[str, str] = field(default_factory=dict)
    site_evidence: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    expansion: Callable[[ExpansionSite, tuple[str, ...]], ExpansionAction | str] | None = None
    #: Overrides :attr:`FirmwarePolicy.expand_nested` for this selection.
    expand_nested: bool | None = None
    #: Roots the profit model prices after every other root.  The other roots
    #: are ordered and priced as if these were not planned (``hot-loop-tasks``).
    price_after: frozenset[str] = frozenset()
    #: Expansion callback of one-level (flat) plans; ``None`` builds them
    #: without one.  Only branch sites reach it: a flat plan keeps nested
    #: calls public before any callback is asked.
    flat_expansion: Callable[[ExpansionSite, tuple[str, ...]], ExpansionAction | str] | None = None


@dataclass(frozen=True)
class FirmwarePolicy:
    select: Callable[[PolicyInput], PolicySelection]
    expand_nested: bool
    requires_edges: bool = False
    discover_loops: Callable[[PolicyInput], Any] | None = None
    #: Whether firmware-inline prices regions by default (``cost_model=auto``).
    cost_model_default: bool = False


class PolicyError(ValueError):
    pass


def _exhaustive(inputs):
    return PolicySelection(tuple(call.callsite_id for call in inputs.calls))


def _hot_loop_calls(snapshot, calls, profile, phase, minimum):
    """Rank exact observed normal transfers of verified BLs to a captured full key."""
    edges = [e for e in profile.edges(phase) if e["status"] == "exact" and not e["flags"]]

    def unit_of(uid):
        try:
            return snapshot.unit(uid)
        except KeyError:
            return None
    loops = observed_loop_units(edges, kinds=FIRMWARE_LOOP_KINDS, accept=real_exit_null(unit_of))
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
    nested = inputs.params.get("nested", "profit")
    if nested not in ("profit", "public"):
        raise PolicyError("hot-loop nested must be 'profit' or 'public'")
    site_evidence = {uid: {"observed_call_edges": count} for uid, count in counts.items()}
    for row in residual:
        site_evidence[row["callsite_id"]] = {"observed_call_edges": row["observed_call_edges"]}
    expansion = None
    if nested == "profit" and inputs.profit is not None:
        # Recursive hot-loop: a nested call inside an inlined callee is
        # inlined when its marginal profit is positive, so each inline can
        # open the next one.  Without a cost model nested calls stay public.
        from ..analysis.firmware_profit import NestedProfitExpansion

        expansion = NestedProfitExpansion(inputs.profit, inputs.snapshot, inputs.image,
                                          tuple(inputs.calls),
                                          trusted=inputs.params.get("trust") == "user")
    return PolicySelection(
        tuple(call.callsite_id for call in kept),
        {row["callsite_id"]: row["reason"] for row in residual}, site_evidence,
        {"profile_id": inputs.profile.profile.profile_id, "phase": phase, "min_call_count": minimum,
         "nested": nested if expansion is not None else "public",
         "loop_kinds": sorted(FIRMWARE_LOOP_KINDS)},
        expansion=expansion, expand_nested=True if expansion is not None else None)


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
        registrations = {int(row["entry"], 16) for row in loops.registrations
                         if row["status"] == "resolved" and "explicit_entry" not in row}
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
        elif expansion and boundaries.classify_task_source(
                call.insn_vaddr, registrations) is not ExpansionAction.INLINE:
            refusals[call.callsite_id] = "rtos_public_caller"
        else:
            roots.append(call.callsite_id)
    return PolicySelection(tuple(roots), refusals, evidence={"task_loops": loops.report(),
                           "rtos_boundaries": expansion is not None}, expansion=expansion)


def _firmlooper_v2(inputs):
    return _firmlooper(inputs, v2=True)


# FirmLooper v3: analysis revision 2 (reset is not an interrupt, BOLT entry
# patches, word jump tables), task_end, the isb_boundary side exit and the
# selection summary. firmlooper and firmlooper-v2 above stay on revision 1 so
# their selections, receipts and candidate ids remain reproducible.

_V3_OPTIONS = {"task_entries", "include_terminal", "boundaries", "signature_reference", "task_end"}


def _v3_loops(inputs):
    from ..analysis.task_loops import discover_task_loops

    options = inputs.params.get("policy_options", {})
    if not isinstance(options, dict) or options.keys() - _V3_OPTIONS:
        raise PolicyError(f"unknown FirmLooper options; allowed: {sorted(_V3_OPTIONS)}")
    entries = options.get("task_entries", [])
    if not isinstance(entries, list) or any(type(x) not in (int, str) for x in entries):
        raise PolicyError("task_entries must be a list of task symbols or addresses")
    if any(type(options[k]) is not bool for k in ("include_terminal", "boundaries", "task_end")
           if k in options):
        raise PolicyError("include_terminal, boundaries and task_end must be boolean")
    include_terminal = options.get("include_terminal", True)
    task_end = options.get("task_end", True)
    if task_end and not include_terminal:
        raise PolicyError("task_end requires include_terminal=true")
    return discover_task_loops(inputs.image, task_entries=entries,
                               include_terminal=include_terminal, revision=2,
                               task_end=task_end)


def _ends_in_isb(unit, image):
    """Whether the unit's last decoded guest instruction is ISB."""
    from ..decode.guest_symbols import unit_guest_bytes
    from ..decode.thumb_sem import decode_unit

    code = unit_guest_bytes(unit, image)
    # ISB is a 32-bit Thumb-2 encoding whose first halfword is 0xf3bX, so only
    # units whose last four bytes start that way are decoded.
    if code is None or len(code) < 4 or code[-3] != 0xf3 or code[-4] & 0xf0 != 0xb0:
        return False
    decoded = decode_unit(unit, code)
    return bool(decoded) and decoded[-1].mnemonic.split(".")[0] == "isb"


def _firmlooper_v3(inputs):
    loops = inputs.task_loops or _v3_loops(inputs)
    options = inputs.params.get("policy_options", {})
    boundaries = (TaskBoundaries(inputs.image, revision=2)
                  if options.get("boundaries", True) else None)
    registrations = {int(row["entry"], 16) for row in loops.registrations
                     if row["status"] == "resolved" and "explicit_entry" not in row}
    units = inputs.snapshot.units()
    # Materialize every decision before parallel region planning. Every
    # internal target must start an existing captured translation.
    actions = None
    if boundaries is not None:
        actions = {pc: boundaries.classify(pc) for pc in {u.vaddrs[0] for u in units if u.vaddrs}}
    # An ISB ends its TB with a goto_tb to the next instruction. Keep that edge
    # a side exit, so a yield requested by cloned code (e.g. PendSV set before
    # the ISB) is taken where the guest asked rather than at the region exit.
    isb_sources = frozenset(u.uid for u in units if _ends_in_isb(u, inputs.image))

    def expansion(site, context):
        if site.kind == "branch" and site.source_uid in isb_sources:
            return "isb_boundary"
        if actions is None:
            return ExpansionAction.INLINE
        return actions.get(site.target_pc, ExpansionAction.PUBLIC)

    roots, refusals, site_evidence = [], {}, {}
    for call in inputs.calls:
        if call.insn_vaddr in loops.call_pcs and boundaries is not None:
            site_evidence[call.callsite_id] = {"boundary": boundaries.explain(call.target_vaddr)}
        if call.insn_vaddr not in loops.call_pcs:
            refusals[call.callsite_id] = "outside_persistent_task_loop"
        elif not call.eligible:
            refusals[call.callsite_id] = call.refusal_reasons[0]
        elif call.source_uid == call.callee_candidates[0]:
            refusals[call.callsite_id] = "recursive_call_edge"
        elif boundaries is not None and boundaries.classify_task_source(
                call.insn_vaddr, registrations) is not ExpansionAction.INLINE:
            refusals[call.callsite_id] = "rtos_public_caller"
            site_evidence[call.callsite_id]["caller_boundary"] = boundaries.explain(call.insn_vaddr)
        else:
            roots.append(call.callsite_id)
    rules = {"isb_boundary": True}
    if boundaries is not None:
        rules = {"reset_entries": [hex(pc) for pc in sorted(boundaries.reset_entries)],
                 "patched_entries_resolved": len(boundaries.patched_entries_resolved), **rules}
    return PolicySelection(tuple(roots), refusals, site_evidence,
                           {"task_loops": loops.report(), "rtos_boundaries": boundaries is not None,
                            "analysis_revision": 2, "boundary_rules": rules},
                           expansion=expansion)


def _hot_loop_tasks(inputs):
    """Hot-loop roots, unchanged, plus FirmLooper v3 task-loop roots the profile did not
    nominate.  Discovery stays hot-loop's (firmware-calls); v3 loops only nominate roots.
    Hot and 'both' roots expand exactly as hot-loop; v3 boundaries (with its ISB rule)
    apply only to static-only roots, in recursive and flat plans alike, and those roots
    are priced after every hot root."""
    if inputs.profit is None:
        raise PolicyError("hot-loop-tasks needs the firmware cost model")
    if inputs.params.get("nested", "profit") != "profit":
        raise PolicyError("hot-loop-tasks prices nested calls (nested must be 'profit')")
    hot = _hot_loop(inputs)
    loops = _v3_loops(inputs)
    static = _firmlooper_v3(replace(inputs, task_loops=loops))
    hot_set, static_set = frozenset(hot.roots), frozenset(static.roots)
    static_only = tuple(c for c in static.roots if c not in hot_set)
    late = frozenset(static_only)
    roots = tuple(hot.roots) + static_only
    root_set = frozenset(roots)
    refusals = {c: hot.refusals.get(c) or static.refusals.get(c)
                for c in (hot.refusals.keys() | static.refusals.keys()) - root_set}
    evidence = {}
    for c in hot.site_evidence.keys() | static.site_evidence.keys() | root_set:
        row = {**static.site_evidence.get(c, {}), **hot.site_evidence.get(c, {}),
               "o4_evidence": [n for n, hit in (("profile_hot_loop", c in hot_set),
                                                ("static_task_loop", c in static_set)) if hit]}
        if c in static.refusals:
            row["static_refusal"] = static.refusals[c]
        evidence[c] = row
    by_site = {(c.source_uid, c.insn_vaddr): c.callsite_id for c in inputs.calls}
    inner = hot.expansion

    def expansion(site, context):
        # context[0] is the root of every site inside a region; a root site
        # (resolve_selection) has none and is found by its call instruction.
        root = context[0] if context else by_site.get((site.source_uid, site.source_pc))
        if root in late:
            action = static.expansion(site, context)
            if residual_reason(action):
                return action
        return inner(site, context)
    expansion.report = inner.report            # keeps nested_decisions in the receipt
    static_rules = {k: v for k, v in static.evidence.items() if k != "task_loops"}
    return PolicySelection(roots, refusals, evidence,
                           {"task_loops": loops.report(), "hot_loop": dict(hot.evidence),
                            "firmlooper_v3": static_rules,
                            "union": {"hot": len(hot_set), "static": len(static_set),
                                      "static_only": len(static_only),
                                      "both": len(hot_set & static_set)}},
                           expansion=expansion, expand_nested=True, price_after=late,
                           flat_expansion=expansion)


#: Root refusals that come from the RTOS expansion boundaries.
RTOS_BOUNDARY_REASONS = frozenset({"rtos_shared_public_lookup", "rtos_public_boundary",
                                   "rtos_public_caller"})


def firmlooper_summary(task_loops, calls, residual, embedded_ids, planned_ids):
    """Per-task and image verdicts of a FirmLooper v3 selection (receipt only).

    ``calls`` are the discovered captured calls, ``residual`` the receipt's
    residual rows (root rows have no ``source_member``), ``embedded_ids`` and
    ``planned_ids`` callsite ids. The first matching verdict wins.
    """
    reason_by_call = {row["callsite_id"]: row["reason"] for row in residual
                      if "source_member" not in row}
    by_pc = {}
    for call in calls:
        by_pc.setdefault(call.insn_vaddr, []).append(call.callsite_id)
    tasks, verdicts, captured_reasons = {}, [], []
    for task in task_loops.tasks:
        pcs = [int(pc, 16) for pc in task.get("selected_call_pcs", ())]
        ids = [uid for pc in pcs for uid in by_pc.get(pc, ())]
        embedded = [uid for uid in ids if uid in embedded_ids]
        reasons = [reason_by_call[uid] for uid in ids if uid in reason_by_call]
        captured_reasons += [reason_by_call.get(uid) for uid in ids]
        if task["status"] != "selected":
            verdict = "no_closed_task_loop"
        elif not pcs:
            verdict = "no_task_loop_calls"
        elif not ids:
            verdict = "task_loop_calls_not_captured"
        elif embedded:
            verdict = "embedded"
        elif any(uid in planned_ids for uid in ids):
            verdict = "planned_not_embedded"
        elif all(reason_by_call.get(uid) in RTOS_BOUNDARY_REASONS for uid in ids):
            verdict = "only_rtos_boundary_calls"
        else:
            verdict = "no_eligible_call"
        verdicts.append(verdict)
        name = task["name"] if task["name"] not in tasks else f"{task['name']}@{task['entry']}"
        tasks[name] = {"entry": task["entry"], "status": task["status"],
                       "loop_call_sites": len(pcs), "captured": len(ids),
                       "embedded": len(embedded), "refused": dict(sorted(Counter(reasons).items())),
                       "verdict": verdict}
    if "embedded" in verdicts:
        verdict = "embedded"
    elif not verdicts:
        verdict = "no_task_registrations"
    elif all(v == "no_closed_task_loop" for v in verdicts):
        verdict = "no_closed_task_loop"
    elif not captured_reasons:
        # Some loop was admitted, but none has a captured call.
        verdict = ("task_loop_calls_not_captured" if "task_loop_calls_not_captured" in verdicts
                   else "no_task_loop_calls")
    elif all(reason in RTOS_BOUNDARY_REASONS for reason in captured_reasons):
        verdict = "only_rtos_boundary_calls"
    else:
        verdict = "no_eligible_call"
    return {"verdict": verdict, "tasks": tasks}


_BUILTINS = {
    "exhaustive": FirmwarePolicy(_exhaustive, expand_nested=True),
    "hot-loop": FirmwarePolicy(_hot_loop, expand_nested=False, requires_edges=True,
                               cost_model_default=True),
    "firmlooper": FirmwarePolicy(_firmlooper, expand_nested=True, discover_loops=_loop_evidence),
    "firmlooper-v2": FirmwarePolicy(_firmlooper_v2, expand_nested=True, discover_loops=_v2_loops),
    "firmlooper-v3": FirmwarePolicy(_firmlooper_v3, expand_nested=True, discover_loops=_v3_loops),
    # No discover_loops: calls and the WHOPR summary come from the firmware-calls and
    # firmware-whopr analyses, as for hot-loop; the policy runs v3 loop discovery itself.
    "hot-loop-tasks": FirmwarePolicy(_hot_loop_tasks, expand_nested=False, requires_edges=True,
                                     cost_model_default=True),
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
            or type(policy.expand_nested) is not bool or type(policy.requires_edges) is not bool
            or type(policy.cost_model_default) is not bool):
        raise PolicyError(f"firmware policy {name!r} must export a FirmwarePolicy")
    return policy


def resolve_selection(inputs: PolicyInput, selected: PolicySelection):
    """Resolve nominated IDs using the original discovered calls, before region legality checks."""
    if not isinstance(selected, PolicySelection):
        raise PolicyError("a firmware policy must return a PolicySelection")
    if selected.expand_nested is not None and type(selected.expand_nested) is not bool:
        raise PolicyError("PolicySelection.expand_nested must be a bool or None")
    if selected.flat_expansion is not None and not callable(selected.flat_expansion):
        raise PolicyError("PolicySelection.flat_expansion must be callable or None")
    calls = {c.callsite_id: c for c in inputs.calls}
    roots = set(selected.roots)
    if not isinstance(selected.price_after, frozenset) or not selected.price_after <= roots:
        raise PolicyError("PolicySelection.price_after must be a frozenset of nominated roots")
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
