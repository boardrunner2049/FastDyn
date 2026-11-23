"""Verified direct firmware calls from guest Thumb bytes and capture metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..decode.guest_symbols import unit_guest_bytes
from ..decode.search import is_pcrel
from ..decode.thumb_sem import decode_unit, direct_call_target
from ..formats.region_v1 import unit_exits
from ..graph import lookup_cflags
from ..model.firmware_region import FirmwareCallSite
from .api import AnalysisSpec
from .b3_inputs import guest_image_for

__all__ = [
    "FirmwareCalls", "compatible_units", "discover_firmware_calls",
    "firmware_compatibility_index", "selected_firmware_units", "FIRMWARE_CALLS",
]


@dataclass(frozen=True, slots=True)
class FirmwareCalls:
    snapshot_id: str
    image_sha256: str | None
    calls: tuple[FirmwareCallSite, ...]
    diagnostics: tuple[dict[str, Any], ...]


def _entry_identity(unit: Any) -> tuple[int, int, int, int]:
    return (unit.vaddrs[0], unit.key.flags, unit.key.cs_base,
            lookup_cflags(unit.key.cflags))


def selected_firmware_units(units: tuple[Any, ...]) -> tuple[tuple[Any, ...], tuple[str, ...]]:
    """Return optimizer roots and fallback-only units in runtime selection order.

    Ordinary CF_PCREL snapshots have no exhaustive exact-entry layer, so every
    verified unit remains an optimizer root.  In a sealed exact-PC snapshot,
    QEMU selects the newest record for a duplicate key.  A unique one-insn
    record is exhaustive fallback evidence, not evidence that the halfword is
    an executable entry.  A duplicated one-insn newest record was observed by
    the training run and is therefore retained.
    """
    verified = tuple(u for u in units if u.vaddrs and u.meta_status == "ok"
                     and u.origin == "elf_verified")
    if not verified or all(is_pcrel(u.key.cflags) for u in verified):
        return verified, ()
    groups: dict[tuple[int, int, int, int], list[Any]] = {}
    for unit in verified:
        groups.setdefault(_entry_identity(unit), []).append(unit)
    selected: list[Any] = []
    fallback: list[str] = []
    for group in groups.values():
        newest = max(group, key=lambda unit: unit.slot)
        if newest.icount > 1 or len(group) > 1:
            selected.append(newest)
        else:
            fallback.append(newest.uid)
        fallback.extend(unit.uid for unit in group if unit is not newest)
    selected.sort(key=lambda unit: unit.slot)
    return tuple(selected), tuple(fallback)


def firmware_compatibility_index(units: tuple[Any, ...]
                                 ) -> dict[tuple[int, int, int, int], str]:
    groups: dict[tuple[int, int, int, int], list[Any]] = {}
    for unit in units:
        if (not unit.vaddrs or unit.meta_status != "ok" or
                unit.origin != "elf_verified"):
            continue
        groups.setdefault(_entry_identity(unit), []).append(unit)
    return {key: max(group, key=lambda unit: unit.slot).uid
            for key, group in groups.items()}


def compatible_units(units: tuple[Any, ...], source: Any, vaddr: int, *,
                     index: dict[tuple[int, int, int, int], str] | None = None
                     ) -> tuple[str, ...]:
    want_cflags = lookup_cflags(source.key.cflags)
    if index is not None:
        uid = index.get((vaddr, source.key.flags, source.key.cs_base, want_cflags))
        return () if uid is None else (uid,)
    matches = [u for u in units if u.vaddrs and u.vaddrs[0] == vaddr
               and u.key.flags == source.key.flags
               and u.key.cs_base == source.key.cs_base
               and u.key.cflags == want_cflags
               and u.meta_status == "ok"
               and u.origin == "elf_verified"]
    if not matches:
        return ()
    # Sealed production writes exhaustive one-insn fallbacks first and then
    # appends ordinary hot translations.  QEMU's record chains are newest
    # first, so replay selects the greatest slot for an otherwise identical
    # exact-PC key.  Model that selection here instead of treating the
    # fallback and hot record as an ambiguous pair.  It also ensures that a
    # private edge never enters the middle of a captured TB.
    return (max(matches, key=lambda unit: unit.slot).uid,)


def discover_firmware_calls(snapshot: Any, image: Any | None) -> FirmwareCalls:
    """Discover calls without consulting native call opcodes or profile hotness.

    Every row binds the decoded BL to the capture exit at the same guest
    instruction index.  Candidate sets use complete translation context; an
    empty or ambiguous set is retained with an explicit refusal reason.
    """
    units = tuple(snapshot.units())
    scan_units, fallback_units = selected_firmware_units(units)
    compatibility = firmware_compatibility_index(units)
    calls: list[FirmwareCallSite] = []
    diagnostics: list[dict[str, Any]] = []
    if image is None:
        diagnostics.append({"reason": "guest_image_missing", "pending": True})
        return FirmwareCalls(snapshot.snapshot_id, None, (), tuple(diagnostics))
    if fallback_units:
        diagnostics.append({"reason": "fallback_only_not_optimizer_root",
                            "units": len(fallback_units), "pending": False})
    for unit in scan_units:
        if unit.origin != "elf_verified":
            diagnostics.append({"unit": unit.uid, "reason": "source_identity_unverified",
                                "pending": True})
            continue
        guest = unit_guest_bytes(unit, image)
        decoded = decode_unit(unit, guest)
        if decoded is None:
            if unit.origin == "elf_verified":
                diagnostics.append({"unit": unit.uid, "reason": "guest_decode_failed"})
            continue
        exits = {}
        priority = {"goto_tb": 3, "goto_ptr": 2, "exit_null": 1}
        for row in unit_exits(unit):
            if row.insn is None:
                continue
            old = exits.get(row.insn)
            if old is None or priority.get(row.kind.value, 0) > priority.get(old.kind.value, 0):
                exits[row.insn] = row
        for insn in decoded:
            target = direct_call_target(insn)
            if target is None:
                continue
            reasons: list[str] = []
            exit_ = exits.get(insn.index)
            if exit_ is None:
                reasons.append("call_exit_missing")
                exit_id = f"insn:{insn.index}"
            else:
                exit_id = exit_.id
                if exit_.static_dest_vaddr is None:
                    reasons.append("call_target_not_captured")
                elif (exit_.static_dest_vaddr & ~1) != target:
                    reasons.append("call_target_mismatch")
            continuation = insn.vaddr + insn.size
            callees = compatible_units(units, unit, target, index=compatibility)
            continuations = compatible_units(units, unit, continuation,
                                              index=compatibility)
            if not callees:
                reasons.append("missing_callee_capture")
            elif len(callees) > 1:
                reasons.append("callee_key_ambiguous")
            if not continuations:
                reasons.append("missing_continuation_capture")
            elif len(continuations) > 1:
                reasons.append("continuation_key_ambiguous")
            calls.append(FirmwareCallSite(
                unit.uid, unit.content_hash, unit.key, exit_id, insn.index,
                insn.vaddr, insn.raw, target, continuation, image.sha256,
                callees, continuations, tuple(dict.fromkeys(reasons))))
    return FirmwareCalls(snapshot.snapshot_id, image.sha256,
                         tuple(sorted(calls, key=lambda c: (c.insn_vaddr, c.source_uid))),
                         tuple(diagnostics))


def _compute(ctx: Any, _scope: Any) -> FirmwareCalls:
    # Bundle provenance is an AnalysisCtx input, not a property of its
    # snapshot facade.  Reading it through ctx both records the dependency and
    # allows an explicitly registered --elf to resolve by its exact hash.
    image = guest_image_for(ctx.snapshot, ctx.bundle)
    return discover_firmware_calls(ctx.snapshot, image)


FIRMWARE_CALLS = AnalysisSpec(
    name="firmware-calls", version=1, scope="snapshot", requires=(),
    inputs=frozenset({"code", "meta", "bundle"}), compute=_compute,
    result_schema=FirmwareCalls,
)
ANALYSES = (FIRMWARE_CALLS,)
