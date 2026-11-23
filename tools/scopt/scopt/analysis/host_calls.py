"""Captured native call sites in the current x86-64 unit view."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from ..decode.x86 import decode_one
from ..formats.enums import Encoding, OpMarkKind, SiteKind
from ..model.unit import Site, TBUnit
from .api import AnalysisSpec, ScopeRef
from .helper_effects import call_class, effects_for_hostops
from .hostops import HostOp, OpKind, UnitHostOps
from .patch_join import insn_vaddrs


@dataclass(frozen=True, slots=True)
class HostCallSite:
    uid: str
    host_off: int
    original_bytes: bytes
    encoding: str
    continuation_off: int | None
    insn_idx: int | None
    guest_pcs: tuple[int, ...]
    opmark_off: int | None
    helper_id: int | None
    helper_name: str | None
    helper_flags: int | None
    target_abs: int | None
    pool_slot_off: int | None
    module: str | None
    symbol: str | None
    call_class: str
    recovery_return_off: int | None
    reason: str | None

    @property
    def resolved(self) -> bool:
        """Whether the machine encoding agrees with an imported call binding."""
        return self.reason is None


def _crosscheck(unit: TBUnit, site: Site, code_address: int, insn) -> tuple[bool, int | None]:
    raw = insn.raw
    if site.width != 4 or site.final_field != int.from_bytes(
            unit.code[site.field_off:site.field_off + 4], "little"):
        return False, None
    if site.enc is Encoding.REL32:
        return (len(raw) == 5 and raw[0] == 0xE8 and site.field_off == site.insn_off + 1
                and insn.branch_target == site.value), None
    if site.enc is not Encoding.POOL_INDIRECT or raw[:2] != b"\xff\x15" or len(raw) != 6:
        return False, None
    pool = tuple(p for p in unit.pool if p.user_field_off == site.field_off)
    if len(pool) != 1 or unit.pool_start is None:
        return False, None
    slot = pool[0].slot_off
    agrees = (site.field_off == site.insn_off + 2 and insn.disp_size == 4
              and insn.rip_target == code_address + slot and pool[0].data0 == site.value
              and unit.pool_start <= slot <= unit.pool_end - 8
              and int.from_bytes(unit.code[slot:slot + 8], "little") == site.value)
    return agrees, slot if agrees else None


def build_host_calls(unit: TBUnit, hostops: UnitHostOps,
                     code_address: int, page_bits: int) -> tuple[HostCallSite, ...]:
    sites: dict[int, list[Site]] = defaultdict(list)
    for site in unit.sites:
        if site.kind in (SiteKind.HELPER_CALL, SiteKind.LDST_HELPER_CALL):
            sites[site.insn_off].append(site)
    ops: dict[int, HostOp] = {op.off: op for op in hostops.ops if op.kind is OpKind.CALL}
    helpers = {h.id: h for h in unit.bindings.helpers}
    recovery = {point.host_return_offset for point in unit.recovery if point.source == "helper_call"}
    marks = tuple(m for m in unit.opmarks if m.kind is OpMarkKind.CALL)
    base = code_address + unit.tc_offset
    vaddrs = insn_vaddrs(unit, page_bits) or ()
    effects = effects_for_hostops(hostops)
    # A certificate names its call by the offset in the code it was proved against.
    # Until the splices are materialized that is where the retired call still stands.
    # Afterwards the retired calls are gone from the code and from the sites, and every
    # remaining offset is physical: the same number can then name a call that survived
    # (an untouched call moved there by an earlier expansion), so nothing is subtracted.
    replaced = (frozenset() if unit.splice_applied
                else {call.call_off for call in unit.native_fragments})
    rows = []
    for off in sorted((sites.keys() | ops.keys()) - replaced):
        at_off = sites.get(off, ())
        site = at_off[0] if len(at_off) == 1 else None
        op = ops.get(off)
        helper_id = (site.aux if site is not None and
                     site.kind in (SiteKind.HELPER_CALL, SiteKind.LDST_HELPER_CALL)
                     else None)
        binding = helpers.get(helper_id)
        guest_index = site.insn_idx if site is not None else (op.insn if op else None)
        mark = max((m for m in marks if m.host_off <= off), key=lambda m: m.host_off, default=None)
        if mark is not None and (mark.helper_id != helper_id or mark.insn_idx != guest_index):
            mark = None
        guest_pcs = ()
        if guest_index is not None and 0 <= guest_index < len(vaddrs):
            guest_delta = vaddrs[guest_index] - vaddrs[0]
            guest_pcs = tuple(pc + guest_delta for pc in unit.vaddrs)
        name = binding.name if binding is not None else None
        flags = binding.flags if binding is not None else None
        target = site.value if site is not None else None
        slow_path = site is not None and site.kind is SiteKind.LDST_HELPER_CALL
        classification = ("ldst_slow" if slow_path else "unknown" if binding is None
                          else call_class(name, flags, effects))
        window = unit.code[off:min(off + 15, unit.ldst_end)] if 0 <= off < unit.ldst_end else b""
        decoded = decode_one(window, base + off)
        raw = decoded.raw if decoded is not None and decoded.is_call else b""
        continuation = off + len(raw) if raw else None
        agrees, slot = _crosscheck(unit, site, base, decoded) if site and raw else (False, None)

        reason = None
        if len(at_off) > 1 or (site and (not agrees or op is not None and (
                op.size != len(raw) or op.target_abs != target or op.helper_id != helper_id
                or binding is not None and (op.helper_name != name or op.helper_flags != flags)))):
            reason = "call_target_mismatch"
        elif unit.meta_status != "ok" or site is None:
            reason = "call_binding_unknown"
        elif slow_path:
            reason = "unsupported_recovery"
        elif binding is None or binding.func is None:
            reason = "call_binding_unknown"
        elif binding.func != target:
            reason = "call_target_mismatch"
        elif guest_index is not None and (mark is None or not guest_pcs):
            reason = "call_origin_unverified"
        rows.append(HostCallSite(
            uid=unit.uid, host_off=off, original_bytes=raw,
            encoding=site.enc.name.lower() if site else "unknown", continuation_off=continuation,
            insn_idx=guest_index, guest_pcs=guest_pcs, opmark_off=mark.host_off if mark is not None else None,
            helper_id=helper_id, helper_name=name, helper_flags=flags, target_abs=target,
            pool_slot_off=slot, module=binding.module if binding else None,
            symbol=binding.symbol if binding else None, call_class=classification,
            recovery_return_off=continuation if continuation in recovery else None, reason=reason,
        ))
    return tuple(rows)


def _compute(ctx: Any, scope: ScopeRef) -> tuple[HostCallSite, ...]:
    ops = ctx.analysis("hostops", scope, min_version=1).value
    return build_host_calls(ctx.unit(scope.id), ops, ctx.header.code_address, ctx.header.target_page_bits)


_HOST_CALLS_V1 = AnalysisSpec(name="host-calls", version=1, scope="unit",
                              requires=(("hostops", 1),),
                              inputs=frozenset({"code", "meta"}), compute=_compute,
                              result_schema=tuple)
ANALYSES = (_HOST_CALLS_V1, replace(_HOST_CALLS_V1, version=2))
