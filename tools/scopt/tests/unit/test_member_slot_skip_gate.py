"""The composed v7 member-skip capability gate.

Member skip flags (SC7_MEMBER_SKIP_SLOT) load only on a QEMU that honours
them; QEMU a1bdec6 rejects any member flag.  scopt therefore emits them only
when the artifact's target RuntimeIdentity exe is explicitly allowlisted
(``--v7-member-skip-exe`` / ``SCOPT_V7_MEMBER_SKIP_EXES``) and the
firmware-inline param ``member_slot_skip`` is not ``off``.  The decision is
made once by firmware-inline, copied into the B3 plan by optimize_b3 and
re-checked by package_b3.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scopt.validate import member_skip_capable_exes, member_slot_skip_decision

from .test_firmware_inline import _continuation_exit_snapshot, _fake_view

#: QEMU treb-wip a1bdec6 (rejects member flags).
A1BDEC6 = "026f8e8bc17a3364b15af49d597e48ac827515548308538a16d6244b75630295"
NEW = "ab" * 32
#: A digest the capability table does not know.
OTHER = "ef" * 32


def _ri(exe):
    return {"qemu_sha256": "sha256:" + exe}


@pytest.fixture(autouse=True)
def _no_env_allowlist(monkeypatch):
    monkeypatch.delenv("SCOPT_V7_MEMBER_SKIP_EXES", raising=False)


def test_decision_needs_an_allowlisted_target_exe(monkeypatch):
    assert member_slot_skip_decision("auto", _ri(NEW), (NEW,)) == (True, "allowlisted")
    assert member_slot_skip_decision("on", _ri(NEW), ("sha256:" + NEW,)) == (True, "allowlisted")
    assert member_slot_skip_decision("auto", _ri(OTHER), (NEW,)) == (False, "not_allowlisted")
    # The capability table marks a1bdec6 incapable (scopt/data/qemu_capabilities.json).
    assert member_slot_skip_decision("auto", _ri(A1BDEC6), (NEW,)) == (False, "known_incapable")
    assert member_slot_skip_decision("auto", None, (NEW,)) == (False, "no_runtime_identity")
    assert member_slot_skip_decision("off", _ri(NEW), (NEW,)) == (False, "param_off")
    # The exe may also come from the admission block.
    admission = {"admission": {"exe_sha256": "sha256:" + NEW}}
    assert member_slot_skip_decision("auto", admission, (NEW,)) == (True, "allowlisted")
    for bad in ("force", "", None, True):
        with pytest.raises(ValueError):
            member_slot_skip_decision(bad, _ri(NEW), (NEW,))


def test_env_allowlist_is_separate_from_the_trusted_v7_list(monkeypatch):
    monkeypatch.setenv("SCOPT_V7_MEMBER_SKIP_EXES", f" x , sha256:{NEW}")
    assert member_skip_capable_exes() == {"x", NEW}
    assert member_slot_skip_decision("auto", _ri(NEW)) == (True, "allowlisted")
    # SCOPT_V7_TRUSTED_EXES names a1bdec6, which rejects member flags.
    monkeypatch.setenv("SCOPT_V7_TRUSTED_EXES", A1BDEC6)
    assert member_slot_skip_decision("auto", _ri(A1BDEC6)) == (False, "known_incapable")
    monkeypatch.setenv("SCOPT_V7_TRUSTED_EXES", OTHER)
    assert member_slot_skip_decision("auto", _ri(OTHER)) == (False, "not_allowlisted")


def _run_firmware_inline(monkeypatch, params, *, runtime=None, member_skip_exes=None):
    import scopt.passes.firmware_inline as FI
    from scopt.analysis.firmware_calls import discover_firmware_calls
    from scopt.guard import GuardOffsets

    snapshot, image = _continuation_exit_snapshot()
    snapshot.runtime = runtime
    calls = discover_firmware_calls(snapshot, image)
    call, = calls.calls
    caller, callee = call.source_uid, call.callee_candidates[0]
    cont = call.continuation_candidates[0]
    view = _fake_view(snapshot, [(caller, callee, "chain0", 5000),
                                 (callee, cont, "lookup_hit", 5000),
                                 (cont, caller, "chain0", 5000)])
    b3 = SimpleNamespace(snapshot=snapshot, layout_bound=True, offsets=GuardOffsets())
    if member_skip_exes is not None:
        b3.env = SimpleNamespace(runtime=None, member_skip_exes=member_skip_exes)
    ctx = SimpleNamespace(b3=b3, params={"mode": "hot-loop", "min_call_count": 2, **params},
                          snapshot_id=snapshot.snapshot_id, bundle=None,
                          analysis=lambda name, sid: SimpleNamespace(value=calls),
                          budget=lambda: None, profile=lambda phase: view)
    monkeypatch.setattr(FI, "guest_image_for", lambda snap, bundle: image)
    emitted = []
    result = FI.FirmwareInline().run(None, ctx, SimpleNamespace(
        emit_firmware_region=emitted.append))
    detail = json.loads(result.detail) if result.detail.startswith("{") else result.detail
    return result, detail, emitted


def test_firmware_inline_on_with_an_unlisted_exe_fails(monkeypatch):
    result, detail, emitted = _run_firmware_inline(
        monkeypatch, {"member_slot_skip": "on"}, runtime=_ri(A1BDEC6),
        member_skip_exes=(NEW,))
    assert result.outcome.value == "failed"
    assert result.reason == "member_slot_skip_unsupported_runtime"
    assert A1BDEC6 in detail and not emitted
    result, _, _ = _run_firmware_inline(monkeypatch, {"member_slot_skip": "maybe"},
                                        runtime=_ri(NEW), member_skip_exes=(NEW,))
    assert (result.outcome.value, result.reason) == ("failed", "invalid_params")


@pytest.mark.parametrize(("params", "runtime", "exes", "enabled", "reason"), [
    ({}, _ri(NEW), (NEW,), True, "allowlisted"),
    ({"member_slot_skip": "on"}, _ri(NEW), (NEW,), True, "allowlisted"),
    ({}, _ri(A1BDEC6), (NEW,), False, "known_incapable"),
    ({}, _ri("ef" * 32), (NEW,), False, "not_allowlisted"),
    ({}, None, None, False, "no_runtime_identity"),
    ({"member_slot_skip": "off"}, _ri(NEW), (NEW,), False, "param_off"),
])
def test_firmware_inline_records_the_decision(monkeypatch, params, runtime, exes, enabled,
                                              reason):
    result, receipt, emitted = _run_firmware_inline(monkeypatch, params, runtime=runtime,
                                                    member_skip_exes=exes)
    assert result.outcome.value == "candidate" and emitted
    assert result.stats["member_slot_skip"] == (1.0 if enabled else 0.0)
    decision = receipt["policy"]["member_slot_skip"]
    assert (decision["enabled"], decision["reason"]) == (enabled, reason)
    assert decision["exe"] == (None if runtime is None else runtime["qemu_sha256"][7:])
    assert decision["param"] == params.get("member_slot_skip", "auto")


def _optimize_b3_plan(monkeypatch, records):
    """optimize_b3 with the pass manager, materialize and packaging stubbed:
    returns the plan and keyword arguments it hands to package_b3."""
    import scopt.manager as M
    import scopt.manager_b3 as MB
    import scopt.validate as V
    from scopt.link import B3Plan

    cand = SimpleNamespace(passes=records, b3_state=SimpleNamespace(empty=False), ledger=[],
                           candidate_id="sha256:" + "1" * 64, has_layout_changes=False,
                           working_set=None, pipeline_provenance=lambda: [],
                           profile_rows=lambda: [])

    class PM:
        def __init__(self, *args, **kwargs):
            self.b3ctx = SimpleNamespace(summaries=lambda: [], units=lambda: {})
            self.store = None

        def execute(self):
            return cand

    captured = {}

    def package(snapshot, plan, store, **kw):
        captured.update(plan=plan, kw=kw)
        return SimpleNamespace(status="published", reason=None)

    monkeypatch.setattr(M, "PassManager", PM)
    monkeypatch.setattr(MB, "materialize", lambda st, b3: SimpleNamespace(
        plan=B3Plan(), downgraded=[], regions=[], pass_bytes={}))
    monkeypatch.setattr(MB, "hot_set_bytes", lambda store, units: 0)
    monkeypatch.setattr(MB, "b3_report", lambda cand, m: {})
    monkeypatch.setattr(V, "package_b3", package)
    MB.optimize_b3(None, "pipeline", None, env=MB.B3Env(member_skip_exes=(NEW,)))
    return captured


def _record(outcome, skip, name="firmware-inline"):
    from scopt.passes.base import Outcome

    return SimpleNamespace(name=name, outcome=getattr(Outcome, outcome), params={},
                           reason="", stats={"member_slot_skip": skip})


@pytest.mark.parametrize(("records", "expected"), [
    ([("CANDIDATE", 1.0)], True),
    ([("CANDIDATE", 0.0)], False),
    ([("CANDIDATE", 1.0), ("CANDIDATE", 0.0)], False),
    ([("SKIPPED_UNKNOWN", 1.0)], False),              # no emitted regions
    ([], False),
])
def test_optimize_b3_copies_the_firmware_inline_decision(monkeypatch, records, expected):
    captured = _optimize_b3_plan(monkeypatch, [_record(o, s) for o, s in records])
    assert captured["plan"].member_slot_skip is expected
    assert captured["kw"]["member_skip_exes"] == (NEW,)


def test_package_b3_rechecks_the_target_exe(tmp_path):
    from scopt.link import B3Plan
    from scopt.manager_b3 import b3_reject_outcome
    from scopt.passes.base import Outcome
    from scopt.validate import package_b3

    from ..synth.fake_snapshot import fake_snapshot

    snapshot = fake_snapshot(2)
    store = tmp_path / "store"
    for runtime, exes in ((_ri(A1BDEC6), ()), (_ri(A1BDEC6), (NEW,)), (None, (NEW,))):
        res = package_b3(snapshot, B3Plan(member_slot_skip=True), store,
                         candidate_id="sha256:" + "41" * 32, runtime=runtime,
                         member_skip_exes=exes)
        assert res.status == "rejected"
        assert res.reason == "failed:member_slot_skip_unsupported_runtime"
        assert b3_reject_outcome(res.reason) == Outcome.FAILED
        assert json.loads(open(res.rejection_path).read())["link"] is None   # never linked
    assert not (store / "artifacts").exists() or not any((store / "artifacts").iterdir())
