"""Request fall-through inlining at every captured host-call site."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from ..analysis.native_callees import NativeCallBinding
from ..decode.x86 import sweep, tiles
from ..model.native import GUARDED_UDIV_KINDS, InlineCall
from .base import Outcome, PassResult
from .host_inline import _EXTREME_SCHEMA, HostInline, HostInlineSplice


EXTREME_PASS = "host-inline-extreme"
# void return, unsigned vCPU index, pointer to registered callback userdata
_PLUGIN_VCPU_CALLBACK_TYPEMASK = (2 << 3) | (6 << 6)
_DEFAULT_INLINE_DEPTH = 1


@dataclass
class _StagedSplices:
    requests: list[InlineCall] = field(default_factory=list)

    def inline_call(self, request: InlineCall) -> None:
        self.requests.append(request)


class HostInlineExtreme(HostInlineSplice):
    """Require accounted coverage under a bound on native call expansion depth."""

    descriptor = replace(
        HostInlineSplice.descriptor, name=EXTREME_PASS,
        params_schema={**HostInlineSplice.descriptor.params_schema, **_EXTREME_SCHEMA},
    )
    default_params = HostInline.default_params
    consider_cold = True
    max_sites_per_unit = 1

    def _binding_refusal(self, binding: NativeCallBinding) -> tuple[str, str] | None:
        call, callee = binding.call, binding.callee
        if callee.role != "plugin":
            return super()._binding_refusal(binding)
        if (callee.abi != "sysv-amd64" or callee.tcg_typemask != _PLUGIN_VCPU_CALLBACK_TYPEMASK
                or call.call_class != "plugin" or call.helper_name is not None
                or call.helper_flags != 0 or call.encoding != "rel32"):
            return "unsupported_abi", "requires a direct x86-64 SysV vCPU callback"
        if not call.guest_pcs:
            return "unsupported_recovery", "callback has no captured guest origin"
        return None

    def _ranking_priority(self, binding: NativeCallBinding) -> int:
        # Calls in the same TB have the same entry proxy. Try a framed callback
        # first, leaving the slot available to a helper if frame proof fails.
        return 0 if binding.callee.role == "plugin" else 1

    def _uses_leaf_fragment(self, binding: NativeCallBinding) -> bool:
        return binding.callee.role != "plugin" and super()._uses_leaf_fragment(binding)

    def _prepare_splice(self, unit, request: InlineCall, code_address: int,
                        binding: NativeCallBinding, params):
        if binding.callee.role != "plugin":
            return super()._prepare_splice(unit, request, code_address, binding, params)
        from ..validate_sym.native_virtual import prepare_virtual_splice

        extra = {"compact_frame": True} if params.get("compact_frame") else {}
        if chain := getattr(binding.callee, "frame_chain", ()):
            extra["frame_chain"] = chain
        return prepare_virtual_splice(
            unit, request, code_address, max_depth=params.get("max_inline_depth", _DEFAULT_INLINE_DEPTH),
            **extra)

    def run(self, view, ctx, edits, *, best_effort: bool = False) -> PassResult:
        depth_limit = ctx.params.get("max_inline_depth", _DEFAULT_INLINE_DEPTH)
        if type(depth_limit) is not int or depth_limit < 1:
            return PassResult(Outcome.FAILED, "invalid_params", "max_inline_depth must be a positive integer")
        partial = best_effort or ctx.params.get("allow_partial", False)
        # A damaged or derived capture cannot establish the original denominator
        # of the full-coverage claim this pipeline makes.
        if not partial and any(unit.meta_status != "ok" or unit.native_fragments
                               for unit in view.units()):
            return PassResult(Outcome.FAILED, "inline_coverage_unknown",
                              "strict coverage requires complete original TB metadata")
        staged = _StagedSplices()
        result = super().run(view, ctx, staged)
        if result.outcome not in (Outcome.CANDIDATE, Outcome.NO_CHANGE):
            return result
        detail = json.loads(result.detail)
        accounted = {(row["unit"], row["call_off"]) for row in detail["sites"]}
        unclassified = 0
        undecodable = 0
        for unit in view.units():
            ctx.budget()
            extent = unit.ldst_end
            base = view.header.code_address + unit.tc_offset
            decoded = sweep(unit.code[:extent], base)
            if not tiles(decoded, base, base + extent):
                undecodable += 1
            for insn in decoded:
                off = insn.address - base
                if insn.is_call and (unit.uid, off) not in accounted:
                    unclassified += 1
                    detail["sites"].append({"unit": unit.uid, "call_off": off, "symbol": None,
                                            "outcome": "unclassified_native_call",
                                            "detail": "decoded CALL has no admitted capture row",
                                            "tb_entries_proxy": None})
        seen = len(accounted) + unclassified
        approved = len(staged.requests)
        if len({(r.target, r.call_off) for r in staged.requests}) != approved or approved > seen:
            return PassResult(Outcome.FAILED, "inline_coverage_inconsistent", result.detail)
        framed_depths = [r.splice.depth for r in staged.requests
                         if r.splice is not None and r.splice.kind == "frame"]
        framed_approved = len(framed_depths)
        approved_depths = sorted(set(framed_depths))
        guarded = {(r.target, r.call_off) for r in staged.requests
                   if r.splice is not None and r.splice.kind in GUARDED_UDIV_KINDS}
        fully_inlined = approved - len(guarded)
        complete = seen == fully_inlined and undecodable == 0
        applied = approved if complete or partial else 0
        for site in detail["sites"]:
            if (site["unit"], site["call_off"]) in guarded:
                site["retains_fallback_call"] = True
        retirement = {"fully_inlined_sites_approved": fully_inlined,
                      "fully_inlined_sites_applied": fully_inlined if applied else 0,
                      "guarded_sites_approved": len(guarded),
                      "guarded_sites_applied": len(guarded) if applied else 0}
        detail["coverage"] = {**retirement, "mode": "partial" if partial else "strict",
                              "sites_seen": seen, "sites_approved": approved,
                              "sites_applied": applied, "complete": complete,
                              "framed_virtuals_approved": framed_approved,
                              "framed_virtuals_applied": framed_approved if applied else 0,
                              "frame_expansion_depth_limit": depth_limit,
                              "frame_expansion_depths_approved": approved_depths,
                              "frame_expansion_depths_applied": approved_depths if applied else [],
                              "undecodable_units": undecodable}
        stats = {**result.stats, **retirement, "sites_seen": seen, "sites_approved": approved,
                 "sites_uncovered": seen - fully_inlined, "sites_applied": applied,
                 "framed_virtuals_approved": framed_approved,
                 "framed_virtuals_applied": framed_approved if applied else 0,
                 "units_undecodable": undecodable}
        if unclassified:
            stats["sites:unclassified_native_call"] = unclassified
        if not complete and not partial:
            # Counters for the inspected sites still describe the trial. Nothing
            # changes the WorkingSet or the baseline when the gate fails.
            stats["sites:applied"] = 0
            stats["sites:approved"] = approved
            stats["emitted_growth_bytes"] = 0
            stats["selected_tb_entries_proxy"] = 0
            for site in detail["sites"]:
                if site["outcome"] == "applied":
                    site["outcome"] = "approved_not_applied"
            return PassResult(Outcome.FAILED, "incomplete_inline_coverage",
                              json.dumps(detail, sort_keys=True), stats=stats)
        for request in staged.requests:
            edits.inline_call(request)
        reason = ("native_inline_full_coverage" if complete else "native_inline_partial_coverage")
        return replace(result, reason=reason, detail=json.dumps(detail, sort_keys=True), stats=stats)


PASSES = ()
