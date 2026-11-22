"""Select and certify native helper inlining in the offline pass framework."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..analysis.api import ScopeRef
from ..analysis.native_callees import NATIVE_IMAGES, NativeCallBinding, prepare_native_images
from ..canon import sha256_id
from ..model.native import (MAX_INTEGER_SPLICES, InlineCall, NativeFragment, NativeOrigin,
                            supports_tcg_integer_abi)
from ..validate_sym.native_inline import NativeInlineError, check_leaf, verify_fragment, verify_site
from .base import BudgetExceeded, Budgets, EditKind, Outcome, PassDescriptor, PassResult

BACKEND_REVISION = "58df0ef89dd64126512e4ee27b4ac3fd8ddf6247"
BRIDGE_VERSION = 1
POLICY_VERSION = 1

_DEFAULTS = {
    "bridge": "",
    "phase": "steady",
    "max_callee_bytes": 128,
    "max_cfg_insns": 128,
    "max_sites_per_unit": 4,
    "max_sites": 256,
    "max_growth_per_unit": 1024,
    "max_total_growth": 65536,
    "max_wall_s": 60,
    "static_max_callee_bytes": 64,
    "min_tb_entries": 1,
}
_LIMITS = {
    "max_callee_bytes": 1024,
    "max_cfg_insns": 128,
    "max_sites_per_unit": 256,
    "max_sites": 4096,
    "max_growth_per_unit": 65536,
    "max_total_growth": 65536,
    "max_wall_s": 60,
    "static_max_callee_bytes": 1024,
    "min_tb_entries": 1 << 64,
}
_COMMON_SCHEMA = {k: type(v) for k, v in _DEFAULTS.items()}
_EXTREME_SCHEMA = {"allow_partial": bool, "max_inline_depth": int, "compact_frame": bool}
_MODES = ("island", "splice", "faulting", "extreme")
_BRIDGE_ENV = {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}


def host_inline_mode(params: Mapping[str, Any]) -> str:
    """Resolve the native strategy from the same parameters used by the pass manager."""
    mode = params.get("mode")
    if "mode" in params and mode not in _MODES:
        return "invalid"
    if mode in (None, "splice") and params.get("allow_faulting"):
        return "faulting"
    depth = params.get("max_inline_depth", 1)
    if mode is None and "max_inline_depth" in params and type(depth) is int and depth >= 1:
        return "splice"
    return mode or "island"


@dataclass(frozen=True, slots=True)
class _Candidate:
    binding: NativeCallBinding
    unit_slot: int
    entries: int | None


def _remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise BudgetExceeded("host-inline wall limit reached")
    return seconds


def _bridge_call(argv: list[str], deadline: float, request: bytes | None = None) -> bytes:
    try:
        p = subprocess.run(argv, input=request, capture_output=True, check=False,
                           timeout=_remaining(deadline), env=_BRIDGE_ENV)
    except subprocess.TimeoutExpired as e:
        raise BudgetExceeded("host-inline bridge exceeded wall limit") from e
    except OSError as e:
        raise NativeInlineError("native_bridge_unavailable", str(e)) from e
    if p.returncode != 0:
        raise NativeInlineError("native_bridge_failed",
                                p.stderr[:500].decode(errors="replace") or f"exit {p.returncode}")
    return p.stdout


def _fragment(bridge: Path, callee, deadline: float) -> NativeFragment:
    request = {"version": BRIDGE_VERSION, "image_path": callee.path,
               "image_sha256": callee.image_id, "address": callee.elf_address,
               "size": callee.size, "code_sha256": callee.code_hash}
    raw = _bridge_call([str(bridge)], deadline, json.dumps(request).encode())
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc["version"] != BRIDGE_VERSION:
            raise ValueError("bridge response version differs")
        if doc["status"] == "refused":
            reason, detail = doc["reason"], doc["detail"]
            if (not isinstance(reason, str) or not reason.replace("_", "").isalnum()
                    or not isinstance(detail, str)):
                raise ValueError("malformed bridge refusal")
            raise NativeInlineError(reason, detail)
        if doc["status"] != "ok":
            raise ValueError("unknown bridge status")
        code = bytes.fromhex(doc["code"])
        origins = tuple(NativeOrigin(**row) for row in doc["origins"])
        fields = []
        for r in doc["relocations"]:
            if r["target"] != "continuation" or r["width"] != 4 or type(r["field_off"]) is not int:
                raise ValueError("unsupported bridge relocation")
            fields.append(r["field_off"])
        if any(any(type(v) is not int for v in o.to_json().values()) for o in origins):
            raise ValueError("malformed bridge origin")
        return NativeFragment(code, origins, tuple(fields))
    except NativeInlineError:
        raise
    except (ValueError, TypeError, KeyError) as e:
        raise NativeInlineError("bridge_output_invalid", str(e)) from e


class HostInline:
    default_params = _DEFAULTS
    consider_cold = False
    max_sites_per_unit = _LIMITS["max_sites_per_unit"]
    default_sites_per_unit = _DEFAULTS["max_sites_per_unit"]
    success_reason = "native_calls_inlined"
    abi_contract = "requires register-only integer ABI"

    descriptor = PassDescriptor(
        name="host-inline", version=1, unit_scope="snapshot",
        requires=(("envlayout", 1), ("native-callees", 1)),
        capabilities=frozenset({EditKind.INLINE_CALL}),
        budgets=Budgets(max_wall_s=60, max_code_growth_bytes=65536),
        invalidates=frozenset({"code", "meta", "layout"}),
        params_schema={**_COMMON_SCHEMA, **_EXTREME_SCHEMA, "mode": str, "allow_faulting": bool,
                       "udiv_mode": str},
    )

    def _accept_abi(self, typemask: int) -> bool:
        return supports_tcg_integer_abi(typemask)

    def _binding_refusal(self, binding: NativeCallBinding) -> tuple[str, str] | None:
        call, callee = binding.call, binding.callee
        if callee.role != "qemu" or callee.abi != "sysv-amd64":
            return "unsupported_abi", ""
        if not self._accept_abi(callee.tcg_typemask):
            return "unsupported_abi", self.abi_contract
        if call.helper_flags != 5 or not call.guest_pcs:
            return "unsupported_recovery", ""
        return None

    def _ranking_priority(self, binding: NativeCallBinding) -> int:
        return 0

    def _uses_leaf_fragment(self, binding: NativeCallBinding) -> bool:
        return True

    def _prepare_request(self, unit, request: InlineCall, code_address: int,
                         binding: NativeCallBinding, params, growth_room: int) -> InlineCall:
        verify_site(unit, request, code_address)
        verify_fragment(request)
        return request

    def run(self, view, ctx, edits) -> PassResult:
        params = ctx.params
        mode = host_inline_mode(params)
        if mode == "invalid":
            return PassResult(Outcome.FAILED, "invalid_params", f"mode must be one of {', '.join(_MODES)}")
        depth = params.get("max_inline_depth", 1)
        if type(depth) is not int or depth < 1:
            return PassResult(Outcome.FAILED, "invalid_params", "max_inline_depth must be a positive integer")
        faulting = params.get("allow_faulting", params.get("mode") == "faulting")
        if params.get("mode") == "faulting" and not faulting:
            return PassResult(Outcome.FAILED, "invalid_params", "faulting mode requires allow_faulting=true")
        if mode == "island" and (faulting or depth != 1):
            return PassResult(Outcome.FAILED, "invalid_params", "island mode requires nonfaulting, one-level inlining")
        if mode != "extreme" and "allow_partial" in params:
            return PassResult(Outcome.FAILED, "invalid_params", "allow_partial requires extreme mode")
        framed = mode == "extreme" or mode != "island" and "max_inline_depth" in params
        if not framed and "compact_frame" in params:
            return PassResult(Outcome.FAILED, "invalid_params", "compact_frame requires extreme mode or recursive splicing")
        if "udiv_mode" in params and not faulting:
            return PassResult(Outcome.FAILED, "invalid_params", "udiv_mode requires faulting inlining")
        if framed:
            from .host_inline_extreme import HostInlineExtreme

            engine = HostInlineExtreme(allow_faulting=faulting)
            if mode != "extreme":
                engine.consider_cold = HostInlineSplice.consider_cold
                engine.default_params = HostInlineSplice.default_params
                engine.max_sites_per_unit = HostInlineSplice.max_sites_per_unit
                return engine.run(view, ctx, edits, best_effort=True)
            # Extreme mode attempts all sites within the standalone extreme pipeline's limits.
            engine.default_params = {**engine.default_params, **{
                k: _LIMITS[k] for k in ("max_callee_bytes", "static_max_callee_bytes",
                                        "max_sites", "max_growth_per_unit")}}
            return engine.run(view, ctx, edits)
        if mode != "island":
            return HostInlineSplice(allow_faulting=faulting).run(view, ctx, edits)
        return self._run_candidates(view, ctx, edits)

    def _run_candidates(self, view, ctx, edits) -> PassResult:
        params = {**self.default_params, "max_sites_per_unit": self.default_sites_per_unit, **ctx.params}
        for key, limit in _LIMITS.items():
            minimum = 1 if key in ("max_cfg_insns", "max_wall_s") else 0
            if not minimum <= params[key] <= limit:
                return PassResult(Outcome.FAILED, "invalid_params",
                                  f"{key} must be in [{minimum}, {limit}]")
        unit_site_limit = min(params["max_sites_per_unit"], self.max_sites_per_unit)
        deadline = min(ctx.deadline, time.monotonic() + params["max_wall_s"])
        if view.header.version != 5 or (ctx.runtime or {}).get("supercache_flat") is not False:
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "native_runtime_unsupported",
                              "requires a non-flat x86-64 SuperCache v5 runtime")
        layout = ctx.analysis("envlayout", ScopeRef("snapshot", ctx.snapshot_id)).value
        if not layout["bound"]:
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "guard_layout_unbound",
                              "native inlining requires an EnvLayout bound to the QEMU image")
        bridge_path = params["bridge"] or os.environ.get("SCOPT_HOST_INLINE_BRIDGE")
        if not bridge_path:
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "native_bridge_unavailable",
                              "configure the checked offline bridge with SCOPT_HOST_INLINE_BRIDGE")
        try:
            bridge = Path(bridge_path).resolve(strict=True)
            bridge_id = sha256_id(bridge.read_bytes())
            if _bridge_call([str(bridge), "--version"], deadline).strip() != (
                    b"scopt-native bridge 1 (LLVM/BOLT 20.1.2)"):
                return PassResult(Outcome.SKIPPED_UNSUPPORTED, "native_bridge_mismatch",
                                  "requires bridge 1 built with pinned LLVM/BOLT 20.1.2")
        except (OSError, NativeInlineError) as e:
            return PassResult(Outcome.SKIPPED_UNSUPPORTED, "native_bridge_unavailable", str(e))
        prepared = prepare_native_images(
            view.working_set.base, backend_revision=BACKEND_REVISION,
            bridge_version=BRIDGE_VERSION, policy_version=POLICY_VERSION, bridge_sha256=bridge_id)
        ctx.store.set_input(NATIVE_IMAGES, prepared)

        profile = ctx.profile(params["phase"])
        has_profile = profile is not None or ctx.profile(None) is not None
        candidates = []
        stats = Counter({"sites_seen": 0, "sites_applied": 0, "emitted_growth_bytes": 0,
                         "selected_tb_entries_proxy": 0})
        diagnostics = []

        def report(binding, reason, detail="", entries=None):
            call = binding.call
            stats[f"sites:{reason}"] += 1
            diagnostics.append({"unit": call.uid, "call_off": call.host_off,
                                "symbol": call.symbol, "outcome": reason,
                                "detail": detail, "tb_entries_proxy": entries})

        for unit in view.units():
            _remaining(deadline)
            scope = ScopeRef("unit", unit.uid)
            result = ctx.analysis("native-callees", scope)
            count = None if profile is None else profile.count(unit.uid, params["phase"])
            entries = None if count is None or not count.known else count.value
            for binding in result.value:
                stats["sites_seen"] += 1
                call, callee = binding.call, binding.callee
                if binding.reason is not None or callee is None:
                    report(binding, binding.reason or "native_image_unavailable", binding.detail, entries)
                elif (unit.stub_start is not None or unit.native_fragments
                      or unit.splice_applied):
                    report(binding, "grown_unit_unsupported", entries=entries)
                elif view.working_set.is_pinned(unit.uid):
                    report(binding, "pinned_unit", entries=entries)
                elif refusal := self._binding_refusal(binding):
                    report(binding, *refusal, entries=entries)
                elif callee.size > params["max_callee_bytes"]:
                    report(binding, "callee_size_budget", entries=entries)
                elif not self.consider_cold and has_profile and (
                        entries is None or entries < params["min_tb_entries"]):
                    report(binding, "below_hotness_policy", entries=entries)
                elif not self.consider_cold and not has_profile and callee.size > params["static_max_callee_bytes"]:
                    report(binding, "static_callee_size_budget")
                else:
                    candidates.append(_Candidate(binding, unit.slot, entries))

        # Steady TB entries are a ranking proxy, not a dynamic count of calls.
        candidates.sort(key=lambda c: (-(c.entries or 0), self._ranking_priority(c.binding),
                                       c.binding.callee.size, c.unit_slot, c.binding.call.host_off))
        by_unit, growth = Counter(), Counter()
        selected, fragments = {}, {}
        for candidate in candidates:
            _remaining(deadline)
            binding = candidate.binding
            call, callee = binding.call, binding.callee
            entries = candidate.entries
            unit = view.unit(call.uid)
            if (stats["sites_applied"] >= params["max_sites"]
                    or by_unit[call.uid] >= unit_site_limit):
                report(binding, "site_budget", entries=entries)
                continue
            try:
                if self._uses_leaf_fragment(binding):
                    check_leaf(callee.code, max_cfg=params["max_cfg_insns"])
                    key = callee.image_id, callee.elf_address, callee.code_hash
                    if key not in fragments:
                        fragments[key] = _fragment(bridge, callee, deadline)
                    frag = fragments[key]
                else:
                    frag = NativeFragment(b"", (), ())
                req = InlineCall(
                    target=unit.uid, unit_hash=unit.content_hash, call_off=call.host_off,
                    call_bytes=call.original_bytes, continuation_off=call.continuation_off,
                    helper_id=call.helper_id, target_abs=callee.runtime_address,
                    pool_slot_off=call.pool_slot_off, guest_pcs=call.guest_pcs,
                    image_id=callee.image_id, elf_address=callee.elf_address,
                    image_offset=callee.image_offset, symbol=callee.symbol,
                    callee_code=callee.code, native_input_id=prepared.input_id, fragment=frag,
                    bridge_sha256=bridge_id)
                growth_room = min(params["max_growth_per_unit"] - growth[call.uid],
                                  params["max_total_growth"] - stats["emitted_growth_bytes"])
                req = self._prepare_request(unit, req, view.header.code_address, binding, params, growth_room)
                if req.splice is not None and (earlier := selected.get(call.uid)):
                    from ..splice_layout import materialize_splices

                    requests = tuple(sorted((*earlier, req), key=lambda row: row.call_off))
                    materialize_splices(unit, requests, view.header.code_address)
            except NativeInlineError as e:
                report(binding, e.reason, str(e), entries)
                continue
            if req.growth > growth_room:
                report(binding, "growth_budget", entries=entries)
                continue
            if call.uid not in selected:
                ctx.fact("native-callees", ScopeRef("unit", call.uid))
            edits.inline_call(req)
            growth[call.uid] += req.growth
            by_unit[call.uid] += 1
            selected.setdefault(call.uid, []).append(req)
            stats["sites_applied"] += 1
            stats["emitted_growth_bytes"] += req.growth
            stats["selected_tb_entries_proxy"] += entries or 0
            report(binding, "applied", req.certificate_id, entries)

        detail = json.dumps({"native_images_id": prepared.input_id,
                             "ranking": "tb_entry_proxy" if has_profile else "static",
                             "sites": diagnostics}, sort_keys=True)
        if not selected:
            return PassResult(Outcome.NO_CHANGE, "no_eligible_native_calls", detail, stats=stats)
        return PassResult(Outcome.CANDIDATE, self.success_reason, detail,
                          affected=tuple(sorted(selected)), stats=stats)


class HostInlineSplice(HostInline):
    """Replace a call with a verified fallthrough splice."""

    descriptor = replace(HostInline.descriptor, name="host-inline-splice",
                         params_schema={**_COMMON_SCHEMA, "udiv_mode": str})
    default_params = {**HostInline.default_params, "max_callee_bytes": 256,
                      "static_max_callee_bytes": 160}
    max_sites_per_unit = MAX_INTEGER_SPLICES
    default_sites_per_unit = 1
    success_reason = "native_calls_spliced"
    abi_contract = "requires an integer result and at most six integer register arguments"

    def __init__(self, *, allow_faulting: bool = True) -> None:
        self.allow_faulting = allow_faulting

    def run(self, view, ctx, edits) -> PassResult:
        if ctx.params.get("udiv_mode", "auto") not in ("auto", "full", "cleanup", "partial-cleanup", "guarded"):
            return PassResult(Outcome.FAILED, "invalid_params", "udiv_mode must be auto, full, cleanup, partial-cleanup or guarded")
        return self._run_candidates(view, ctx, edits)

    def _binding_refusal(self, binding: NativeCallBinding) -> tuple[str, str] | None:
        from ..validate_sym.native_udiv import is_udiv_binding

        if is_udiv_binding(binding):
            if not self.allow_faulting:
                return "faulting_disabled", "enable faulting inlining for exception-capable helpers"
            if not binding.call.guest_pcs:
                return "unsupported_recovery", "UDIV has no captured guest origin"
            return None
        return super()._binding_refusal(binding)

    def _uses_leaf_fragment(self, binding: NativeCallBinding) -> bool:
        from ..validate_sym.native_udiv import is_udiv_binding

        return not is_udiv_binding(binding)

    def _prepare_splice(self, unit, request: InlineCall, code_address: int,
                        binding: NativeCallBinding, params):
        from ..validate_sym.native_splice import prepare_splice
        from ..validate_sym.native_udiv import is_udiv_binding, prepare_udiv_splice

        if is_udiv_binding(binding):
            from ..validate_sym.native_udiv_full import is_full_udiv_binding, prepare_full_udiv_splice

            mode = params.get("udiv_mode", "auto")
            if mode == "full" or (mode == "auto" and is_full_udiv_binding(binding)):
                return prepare_full_udiv_splice(unit, request, code_address, cleanup=(mode == "auto"))
            if mode in ("cleanup", "partial-cleanup", "auto"):
                from ..validate_sym.native_udiv_cleanup import prepare_udiv_cleanup

                return prepare_udiv_cleanup(unit, request, code_address, partial=mode == "partial-cleanup")
            return prepare_udiv_splice(unit, request, code_address)
        return prepare_splice(unit, request, code_address)

    def _prepare_request(self, unit, request: InlineCall, code_address: int,
                         binding: NativeCallBinding, params, growth_room: int) -> InlineCall:
        from ..link import LinkError
        from ..splice_layout import prepare_splice_layout
        from ..validate_sym.native_udiv_full import is_full_udiv_binding

        request = replace(request, tcg_typemask=binding.callee.tcg_typemask)

        def place(options):
            body = self._prepare_splice(unit, request, code_address, binding, options)
            try:
                return prepare_splice_layout(unit, replace(request, splice=body), code_address)
            except LinkError as error:
                raise NativeInlineError(error.reason, str(error)) from error

        if params.get("udiv_mode", "auto") != "auto" or not is_full_udiv_binding(binding):
            return place(params)
        try:
            full = place(params)
        except NativeInlineError as error:
            # Only a placement or encoding range can be helped by a smaller
            # body. Propagate failed provenance and semantic proofs unchanged.
            size_overflow = error.reason == "splice_metadata_range" and isinstance(error.__cause__, OverflowError)
            if error.reason != "splice_reference_range" and not size_overflow:
                raise
        else:
            if full.growth <= growth_room:
                return full
        # On this image both forms are available. Recheck the independently
        # proven guarded path against the same caller, remaining budget and layout.
        return place({**params, "udiv_mode": "cleanup"})


PASSES = (HostInline,)
