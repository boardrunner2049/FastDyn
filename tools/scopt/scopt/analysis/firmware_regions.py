"""Fixed-point construction of private firmware call regions."""

from __future__ import annotations

from collections import deque
from typing import Any

from ..decode.guest_symbols import unit_guest_bytes
from ..decode.thumb_sem import decode_unit, direct_call_target, is_return_insn
from ..formats.enums import ExitKind
from ..formats.region_v1 import unit_exits
from ..model.firmware_region import (
    FirmwareCallSite, FirmwareRegionEdge, FirmwareRegionMember, FirmwareRegionPlan,
)
from .firmware_calls import FirmwareCalls, compatible_units

__all__ = ["build_firmware_region"]


def build_firmware_region(snapshot: Any, image: Any, calls: FirmwareCalls,
                          root: FirmwareCallSite) -> FirmwareRegionPlan:
    """Expand one eligible call through captured acyclic callees to a fixed point.

    There is deliberately no depth, iteration, member, site, hotness, or growth
    bound.  Ordinary CFG backedges terminate through the visited-member set;
    recursive call edges are retained as side exits.
    """
    units = tuple(snapshot.units())
    by_uid = {u.uid: u for u in units}
    call_by_exit = {(c.source_uid, c.exit_id): c for c in calls.calls}
    members: list[FirmwareRegionMember] = []
    member_index: dict[tuple[str, tuple[str, ...]], int] = {}
    edges: list[FirmwareRegionEdge] = []
    pending: list[dict[str, Any]] = []
    fatal = False

    def member(uid: str, context: tuple[str, ...], role: str) -> int:
        key = uid, context
        if key in member_index:
            return member_index[key]
        unit = by_uid[uid]
        i = len(members)
        member_index[key] = i
        members.append(FirmwareRegionMember(uid, unit.content_hash, unit.key, context, role))
        return i

    root_context: tuple[str, ...] = ()
    caller = member(root.source_uid, root_context, "caller")
    if not root.eligible:
        return FirmwareRegionPlan(root, tuple(members), (), caller,
                                  tuple({"callsite_id": root.callsite_id, "reason": r}
                                        for r in root.refusal_reasons), False)
    callee_context = (root.callsite_id,)
    callee_uid = root.callee_candidates[0]
    callee = member(callee_uid, callee_context, "callee")
    continuation_uid = root.continuation_candidates[0]
    continuation = member(continuation_uid, root_context, "continuation")
    edges.append(FirmwareRegionEdge(caller, root.exit_id, "call", callee,
                                    root.target_vaddr))
    work = deque([(callee_uid, callee_context, continuation,
                   ((callee_uid, by_uid[callee_uid].key),))])
    visited: set[tuple[str, tuple[str, ...]]] = set()

    while work:
        uid, context, return_member, active_calls = work.popleft()
        identity = uid, context
        if identity in visited:
            continue
        visited.add(identity)
        unit = by_uid[uid]
        source_member = member(uid, context, "callee")
        decoded = decode_unit(unit, unit_guest_bytes(unit, image))
        if decoded is None:
            pending.append({"source_uid": uid, "inline_context": list(context),
                            "reason": "guest_decode_failed"})
            # This member was admitted as executable region code.  Without a
            # verified guest decode we cannot classify its exits, so the root
            # region is not publishable.  In contrast, an unsupported nested
            # call below keeps the original lookup side exit and is safe to
            # report as pending without discarding the surrounding region.
            fatal = True
            continue
        by_insn = {g.index: g for g in decoded}
        handled_control: set[int] = set()
        for exit_ in unit_exits(unit):
            g = by_insn.get(exit_.insn) if exit_.insn is not None else None
            nested = call_by_exit.get((uid, exit_.id))
            if g is not None and direct_call_target(g) is not None and g.index in handled_control:
                continue
            if nested is not None and g is not None and direct_call_target(g) is not None:
                handled_control.add(g.index)
                if not nested.eligible:
                    reason = nested.refusal_reasons[0]
                    edges.append(FirmwareRegionEdge(source_member, exit_.id, "side_exit",
                                                    None, nested.target_vaddr,
                                                    refusal_reason=reason))
                    pending.append({"callsite_id": nested.callsite_id, "reason": reason})
                    continue
                nested_uid = nested.callee_candidates[0]
                semantic = nested_uid, by_uid[nested_uid].key
                if semantic in active_calls:
                    edges.append(FirmwareRegionEdge(source_member, exit_.id, "side_exit",
                                                    None, nested.target_vaddr,
                                                    refusal_reason="recursive_call_edge"))
                    continue
                nested_context = context + (nested.callsite_id,)
                nested_entry = member(nested_uid, nested_context, "callee")
                nested_cont = member(nested.continuation_candidates[0], context,
                                     "continuation")
                edges.append(FirmwareRegionEdge(source_member, exit_.id, "call",
                                                nested_entry, nested.target_vaddr))
                work.append((nested_uid, nested_context, nested_cont,
                             active_calls + (semantic,)))
                continue
            if g is not None and is_return_insn(g):
                if g.index in handled_control:
                    continue
                handled_control.add(g.index)
                continuation_vaddr = by_uid[members[return_member].source_uid].vaddrs[0]
                edges.append(FirmwareRegionEdge(source_member, exit_.id, "return",
                                                return_member, continuation_vaddr, guarded=True))
                continue
            target = exit_.static_dest_vaddr
            if target is not None:
                candidates = compatible_units(units, unit, target)
                if len(candidates) == 1:
                    target_member = member(candidates[0], context, "callee")
                    edges.append(FirmwareRegionEdge(source_member, exit_.id, "branch",
                                                    target_member, target))
                    work.append((candidates[0], context, return_member, active_calls))
                    continue
                reason = "missing_capture" if not candidates else "target_key_ambiguous"
            elif exit_.kind in (ExitKind.EXIT_REQUESTED, ExitKind.EXIT_NULL,
                                ExitKind.GOTO_PTR):
                reason = "observation_boundary"
            else:
                reason = "unresolved_dynamic_exit"
            edges.append(FirmwareRegionEdge(source_member, exit_.id, "side_exit", None,
                                            target, refusal_reason=reason))

    complete = not fatal
    return FirmwareRegionPlan(root, tuple(members), tuple(edges), caller,
                              tuple(pending), complete)
