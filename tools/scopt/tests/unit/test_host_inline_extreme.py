"""Strict host-call coverage must refuse partial publication atomically."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.canon import sha256_id
from scopt.manager import PassManager
from scopt.model.native import UDIV_KIND, NativeSplice
from scopt.passes.base import Outcome
from scopt.validate import ValidateAndPackage
from scopt.validate_sym import native_virtual
from scopt.validate_sym.native_inline import NativeInlineError
from tests.synth.fake_snapshot import FakeSnapshot
from tests.unit.test_host_inline_splice_pass import world


def test_extreme_rejects_partial_candidate_and_artifact(world, tmp_path):
    snap, registry = world
    candidate = PassManager(snap, "host-inline-extreme", analyses=registry).execute()
    record, = candidate.passes
    assert record.outcome is Outcome.FAILED
    assert not candidate.edits
    coverage = json.loads(record.detail)["coverage"]
    assert coverage["sites_seen"] == 4
    assert coverage["sites_approved"] == 2
    assert coverage["sites_applied"] == 0
    assert candidate.working_set.generation == 0
    result = ValidateAndPackage(candidate, tmp_path).run()
    assert not result.published and result.reason == "incomplete_inline_coverage"
    assert not (tmp_path / "artifacts").exists()


def test_extreme_partial_requires_explicit_parameter(world):
    snap, registry = world
    partial = PassManager(snap, "host-inline-extreme", analyses=registry,
                          params={"host-inline-extreme": {"allow_partial": True}}).execute()
    record, = partial.passes
    assert record.reason == "native_inline_partial_coverage"
    assert len(partial.edits) == 2
    assert json.loads(record.detail)["coverage"]["complete"] is False


def test_extreme_requires_original_capture(world):
    snap, registry = world
    units = list(snap.units())
    units[0] = replace(units[0], meta_status="incomplete")
    incomplete = FakeSnapshot(units, snap.snapshot_id, snap.header, runtime=snap.runtime)
    candidate = PassManager(incomplete, "host-inline-extreme", analyses=registry).execute()
    assert candidate.passes[0].reason == "inline_coverage_unknown"
    assert candidate.edits == ()


def test_disabled_extreme_cannot_publish_identity_as_exhaustive(world, tmp_path):
    snap, registry = world
    candidate = PassManager(snap, "host-inline-extreme", analyses=registry,
                            disable=("host-inline-extreme",)).execute()
    result = ValidateAndPackage(candidate, tmp_path).run()
    assert not result.published and result.reason == "ablation_disabled"


def test_extreme_decoded_call_missing_from_analysis_is_uncovered(world):
    snap, registry = world
    original = registry.exact("native-callees", 1)

    def omit_call(ctx, scope):
        return original.compute(ctx, scope)[:1]

    registry.register(replace(original, compute=omit_call), replace=True)
    candidate = PassManager(snap, "host-inline-extreme", analyses=registry).execute()
    assert candidate.edits == ()
    assert candidate.passes[0].stats["sites:unclassified_native_call"] == 2


def test_complete_coverage_applies_all_staged_calls(world):
    snap, registry = world
    spec = registry.exact("native-callees", 1)

    def single_call(ctx, scope):
        return spec.compute(ctx, scope)[:1]

    registry.register(replace(spec, compute=single_call), replace=True)
    units = [replace(u, code=u.code[:9] + b"\x90" * (len(u.code) - 9),
                     sites=u.sites[:1], opmarks=u.opmarks[:1]) for u in snap.units()]
    single = FakeSnapshot(units, snap.snapshot_id, snap.header, runtime=snap.runtime)
    candidate = PassManager(single, "host-inline-extreme", analyses=registry).execute()
    record, = candidate.passes
    assert record.outcome is Outcome.CANDIDATE and len(candidate.edits) == 2
    assert json.loads(record.detail)["coverage"]["complete"] is True


@pytest.mark.parametrize(("frame_passes", "depth", "partial"), [(True, None, True), (False, None, True),
                                                                (True, 2, True), (True, 3, False),
                                                                (True, 17, True)])
def test_extreme_prefers_checked_virtual_and_falls_back_on_refusal(
        world, monkeypatch, frame_passes, depth, partial):
    snap, registry = world
    spec = registry.exact("native-callees", 1)

    def add_plugin(ctx, scope):
        helper, plugin_base = spec.compute(ctx, scope)
        if scope.id != snap.units()[0].uid:
            return helper, plugin_base
        call = replace(plugin_base.call, call_class="plugin", helper_name=None,
                       helper_flags=0, symbol="virtual_test")
        # Use a non-leaf marker so a request must take the frame path.
        code = b"\xcc"
        callee = SimpleNamespace(**{**vars(plugin_base.callee), "role": "plugin",
                                    "tcg_typemask": 400, "symbol": "virtual_test",
                                    "size": len(code), "code": code,
                                    "code_hash": sha256_id(code)})
        return helper, replace(plugin_base, call=call, callee=callee)

    tested = []

    def prepare(unit, request, _base, *, max_depth):
        tested.append((unit.uid, request.call_off, max_depth))
        assert request.symbol == "virtual_test"
        assert request.fragment.code == b"" and request.tcg_typemask == 400
        if not frame_passes:
            raise NativeInlineError("frame_virtual_unsupported", "test refusal")
        return NativeSplice(start_off=request.call_off, prefix=b"", code=b"\x90" * 13,
                            kind="frame", depth=max_depth)

    registry.register(replace(spec, compute=add_plugin), replace=True)
    monkeypatch.setattr(native_virtual, "prepare_virtual_splice", prepare)
    params = {"allow_partial": partial}
    if depth is not None:
        params["max_inline_depth"] = depth
    candidate = PassManager(snap, "host-inline-extreme", analyses=registry,
                            params={"host-inline-extreme": params}).execute()
    limit = depth or 1
    expected_depths = [limit] if frame_passes else []
    assert tested == [(snap.units()[0].uid, 12, limit)]
    record, = candidate.passes
    coverage = json.loads(record.detail)["coverage"]
    assert coverage["frame_expansion_depth_limit"] == limit
    assert coverage["frame_expansion_depths_approved"] == expected_depths
    assert coverage["frame_expansion_depths_applied"] == (expected_depths if partial else [])
    assert record.stats["framed_virtuals_applied"] == int(frame_passes and partial)
    if partial:
        assert len(candidate.edits) == 2
        selected = next(e.payload for e in candidate.edits if e.payload.target == snap.units()[0].uid)
        assert selected.call_off == (12 if frame_passes else 3)
    else:
        assert record.outcome is Outcome.FAILED and not candidate.edits
    if not frame_passes:
        assert record.stats["sites:frame_virtual_unsupported"] == 1


@pytest.mark.parametrize("depth", [0, -1, "3", True, 2.0])
def test_extreme_refuses_invalid_depth(world, depth):
    snap, registry = world
    candidate = PassManager(snap, "host-inline-extreme", analyses=registry,
                            params={"host-inline-extreme": {"max_inline_depth": depth}}).execute()
    record, = candidate.passes
    assert record.outcome is Outcome.FAILED and record.reason == "invalid_params"
    assert not candidate.edits

@pytest.mark.parametrize("partial", [False, True])
def test_guarded_udiv_never_satisfies_strict_retirement_and_applies_in_partial_mode(world, partial):
    from tests.unit.test_native_udiv import _fixture

    base, registry = world
    unit, req, code_address = _fixture()
    assert base.header.code_address == code_address
    snapshot = FakeSnapshot((unit,), base.snapshot_id, base.header, runtime=base.runtime)
    spec = registry.exact("native-callees", 1)

    def guarded_callee(ctx, scope):
        template = spec.compute(ctx, scope)[0]
        call = replace(template.call, host_off=req.call_off, continuation_off=req.continuation_off,
                       original_bytes=req.call_bytes, pool_slot_off=req.pool_slot_off,
                       helper_flags=1, helper_name="udiv", helper_id=req.helper_id, symbol=req.symbol,
                       target_abs=req.target_abs, guest_pcs=req.guest_pcs, encoding="pool_indirect",
                       call_class="throwing", recovery_return_off=req.continuation_off)
        callee = SimpleNamespace(**{**vars(template.callee), "symbol": req.symbol,
                                    "tcg_typemask": req.tcg_typemask, "code": req.callee_code,
                                    "code_hash": req.callee_hash, "size": len(req.callee_code),
                                    "runtime_address": req.target_abs})
        return (replace(template, call=call, callee=callee),)

    registry.register(replace(spec, compute=guarded_callee), replace=True)
    candidate = PassManager(snapshot, "host-inline-extreme", analyses=registry,
                            params={"host-inline-extreme": {"allow_partial": partial}}).execute()
    record, = candidate.passes
    detail = json.loads(record.detail)
    coverage = detail["coverage"]
    assert coverage["sites_seen"] == coverage["sites_approved"] == coverage["guarded_sites_approved"] == 1
    assert not coverage["complete"]
    assert coverage["fully_inlined_sites_approved"] == coverage["fully_inlined_sites_applied"] == 0
    assert coverage["guarded_sites_applied"] == coverage["sites_applied"] == int(partial)
    assert record.stats["sites_uncovered"] == 1 and detail["sites"][0]["retains_fallback_call"]
    if partial:
        assert record.outcome is Outcome.CANDIDATE and record.reason == "native_inline_partial_coverage"
        edit, = candidate.edits
        assert edit.payload.splice.kind == UDIV_KIND
        assert edit.payload.splice.code.endswith(req.call_bytes)
    else:
        assert record.outcome is Outcome.FAILED and record.reason == "incomplete_inline_coverage"
        assert not candidate.edits and record.stats["emitted_growth_bytes"] == 0


@pytest.mark.parametrize("params", [{"mode": "extreme"}, {"mode": "extreme", "max_inline_depth": 2}])
def test_canonical_extreme_rejects_incomplete_or_disabled_publication(world, tmp_path, params):
    snap, registry = world
    for disable, reason in (((), "incomplete_inline_coverage"), (("host-inline",), "ablation_disabled")):
        candidate = PassManager(snap, "host-inline", analyses=registry, disable=disable,
                                params={"host-inline": params}).execute()
        assert not candidate.edits
        result = ValidateAndPackage(candidate, tmp_path).run()
        assert not result.published and result.reason == reason


@pytest.mark.parametrize("params", [
    {"mode": "extreme", "max_inline_depth": 2, "allow_partial": True},
    {"max_inline_depth": 1}, {"mode": "splice", "max_inline_depth": 1},
    {"max_inline_depth": 2}, {"mode": "splice", "max_inline_depth": 2},
    {"mode": "faulting", "max_inline_depth": 2},
    {"max_inline_depth": 3}, {"mode": "splice", "max_inline_depth": 4, "compact_frame": True},
    {"mode": "faulting", "max_inline_depth": 32},
])
def test_recursive_mode_publishes_eligible_sites_despite_incomplete_coverage(world, params):
    snap, registry = world
    result = PassManager(snap, "host-inline", analyses=registry,
                         params={"host-inline": params}).execute()
    row, = result.passes
    assert row.name == "host-inline" and row.reason == "native_inline_partial_coverage"
    coverage = json.loads(row.detail)["coverage"]
    assert coverage["frame_expansion_depth_limit"] == params["max_inline_depth"]
    assert coverage["mode"] == "partial" and not coverage["complete"]
    assert all(edit.payload.splice is not None for edit in result.edits) and len(result.edits) == 2


@pytest.mark.parametrize("params", [
    {"mode": "wrong"}, {"mode": "island", "allow_faulting": True},
    {"mode": "island", "max_inline_depth": 2}, {"mode": "faulting", "allow_faulting": False},
    {"mode": "splice", "udiv_mode": "auto"}, {"allow_partial": True}, {"max_inline_depth": -1},
])
def test_invalid_canonical_combinations_do_not_publish_identity(world, tmp_path, params):
    snap, registry = world
    candidate = PassManager(snap, "host-inline", analyses=registry, params={"host-inline": params}).execute()
    assert candidate.passes[0].reason == "invalid_params" and not candidate.edits
    result = ValidateAndPackage(candidate, tmp_path).run()
    assert not result.published and result.reason == "invalid_params"
