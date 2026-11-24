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


Context = tuple[int, int, int, int]
CompatibilityIndex = dict[Context, tuple[str, ...]]


def _exact_groups(units):
    # Mirrors the sealed exact-PC lookup in QEMU. A later record shadows an
    # earlier one even if the later translation cannot be optimized.
    groups = {}
    for unit in units:
        if unit.vaddrs:
            key = (unit.vaddrs[0], unit.key.flags, unit.key.cs_base, unit.key.cflags,
                   unit.key.page_addr0)
            groups.setdefault(key, []).append(unit)
    return groups


def _verified(unit):
    return unit.meta_status == "ok" and unit.origin == "elf_verified"


def selected_firmware_units(units: tuple[Any, ...], verified_entry_pcs=frozenset()
                            ) -> tuple[tuple[Any, ...], tuple[str, ...]]:
    """A sealed single-insn root needs training or independent instruction evidence."""
    selected, fallback = [], []
    for group in _exact_groups(units).values():
        if is_pcrel(group[0].key.cflags):
            selected.extend(unit for unit in group if _verified(unit))
            continue
        newest = max(group, key=lambda unit: unit.slot)
        if _verified(newest) and (newest.icount > 1 or len(group) > 1 or
                                  newest.vaddrs[0] in verified_entry_pcs):
            selected.append(newest)
        else:
            fallback.append(newest.uid)
        fallback.extend(unit.uid for unit in group if unit is not newest)
    selected.sort(key=lambda unit: unit.slot)
    return tuple(selected), tuple(fallback)


def firmware_compatibility_index(units: tuple[Any, ...]) -> CompatibilityIndex:
    matches = {}
    for key, group in _exact_groups(units).items():
        newest = max(group, key=lambda unit: unit.slot)
        matches.setdefault(key[:4], []).append(newest)
    # Unknown physical mappings must not silently select a verified alias.
    return {key: tuple(unit.uid for unit in group) if all(_verified(u) for u in group) else ()
            for key, group in matches.items()}


def compatible_units(units: tuple[Any, ...], source: Any, vaddr: int, *,
                     index: CompatibilityIndex | None = None) -> tuple[str, ...]:
    if index is None:
        index = firmware_compatibility_index(units)
    return index.get((vaddr, source.key.flags, source.key.cs_base,
                      lookup_cflags(source.key.cflags)), ())


def discover_firmware_calls(snapshot: Any, image: Any | None, *,
                            verified_entry_pcs=frozenset()) -> FirmwareCalls:
    """Discover calls without consulting native call opcodes or profile hotness.

    Every row binds the decoded BL to the capture exit at the same guest
    instruction index.  Candidate sets use complete translation context; an
    empty or ambiguous set is retained with an explicit refusal reason.
    """
    units = tuple(snapshot.units())
    scan_units, fallback_units = selected_firmware_units(units, verified_entry_pcs)
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
