"""``firmware-inline@1``: private regions for verified firmware calls.

The pass is deliberately profile-independent.  Exhaustive mode visits every
captured call and uses ``None`` for all profitability, growth, member, depth,
iteration and wall-clock bounds.  Missing capture and recursive call edges
are reported rather than inferred unreachable.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, ClassVar

from ..analysis.api import ScopeRef
from ..analysis.b3_inputs import guest_image_for
from ..analysis.firmware_regions import build_firmware_region
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult

__all__ = ["FirmwareInline", "PASSES"]


class FirmwareInline:
    descriptor: ClassVar[PassDescriptor] = PassDescriptor(
        name="firmware-inline", version=1, unit_scope="snapshot",
        requires=(("firmware-calls", 1),),
        capabilities=frozenset({EditKind.EMIT_FIRMWARE_REGION}),
        budgets=Budgets(max_wall_s=None, max_code_growth_bytes=None,
                        max_region_members=None, max_context_versions=None,
                        max_units=None),
        invalidates=frozenset({"code", "meta", "layout"}),
        params_schema={"mode": str},
    )

    def run(self, view: Any, ctx: Any, edits: Any) -> PassResult:
        if ctx.b3 is None:
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "capability_not_enabled",
                              "firmware-inline requires the region-capable B3 packager")
        if not ctx.b3.layout_bound or ctx.b3.offsets is None:
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "guard_layout_unbound",
                              ctx.b3.layout_reason or "")
        mode = ctx.params.get("mode", "exhaustive")
        if mode != "exhaustive":
            return PassResult(Outcome.FAILED, "invalid_params",
                              "firmware-inline mode must be 'exhaustive'")
        sid = ScopeRef("snapshot", ctx.snapshot_id)
        discovered = ctx.analysis("firmware-calls", sid).value
        image = guest_image_for(ctx.b3.snapshot, ctx.bundle)
        if image is None:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "guest_image_missing",
                              "the exact firmware ELF is required")
        refused: Counter[str] = Counter()
        affected: list[str] = []
        embedded: list[dict[str, Any]] = []
        residual: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = [dict(row) for row in discovered.diagnostics]
        source_identities: dict[str, dict[str, Any]] = {}
        for row in pending:
            refused[str(row.get("reason", "incomplete"))] += 1
        max_depth = 0
        for call in discovered.calls:
            ctx.budget()
            plan = build_firmware_region(ctx.b3.snapshot, image, discovered, call)
            max_depth = max(max_depth, max((len(m.inline_context) for m in plan.members), default=0))
            for row in plan.pending:
                reason = str(row.get("reason", "incomplete"))
                refused[reason] += 1
                pending.append(dict(row))
            if not plan.complete:
                continue
            edits.emit_firmware_region(plan)
            embedded.append({"callsite_id": call.callsite_id,
                             "region_id": plan.region_id,
                             "source_uids": list(plan.source_uids),
                             "members": len(plan.members)})
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
                 **{f"refused:{k}": float(v) for k, v in sorted(refused.items())}}
        receipt = {"discovered": [call.to_json() for call in discovered.calls],
                   "embedded": embedded, "residual": residual,
                   "pending": pending,
                   "source_identities": [source_identities[k]
                                         for k in sorted(source_identities)],
                   "actual_expansion_depth": max_depth,
                   "emitted_size": None, "loaded_artifact_id": None,
                   "policy": {"mode": "exhaustive", "max_hotness": None,
                              "max_growth": None, "max_sites": None,
                              "max_members": None, "max_depth": None,
                              "max_iterations": None, "max_wall_s": None}}
        if embedded:
            return PassResult(Outcome.CANDIDATE, "firmware_regions_emitted",
                              json.dumps(receipt, sort_keys=True),
                              tuple(dict.fromkeys(affected)), stats=stats)
        reason = "no_eligible_call" if discovered.calls else "no_firmware_call"
        return PassResult(Outcome.SKIPPED_UNKNOWN, reason,
                          json.dumps(receipt, sort_keys=True), stats=stats)


PASSES = (FirmwareInline,)
