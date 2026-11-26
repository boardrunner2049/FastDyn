"""firmware-inline ``isb_boundary``: FirmLooper v3's ISB side exit as a rule for any mode.

ISB ends its TB with a ``goto_tb`` to the next instruction.  With
``isb_boundary = true`` every branch out of a member whose last guest instruction
is ISB stays a side exit (reason ``isb_boundary``) in the recursive and the flat
plan alike, so a PendSV that cloned code requests just before the ISB is taken
there rather than at the region exit.  Absent, nothing changes: the region ids
below were recorded with the code before the parameter existed.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import scopt.passes.firmware_inline as FI
from scopt.analysis.firmware_calls import discover_firmware_calls
from scopt.decode.guest_symbols import GuestImage
from scopt.formats.enums import Encoding, SiteKind
from scopt.formats.scm_v1 import ScmSite, ScmStaticExit
from scopt.ids import TBKey
from scopt.manager import _check_params
from scopt.passes.firmware_policy import _ends_in_isb

from ..synth.builders import SynthUnit
from ..synth.fake_snapshot import fake_snapshot
from .test_firmlooper_v3 import _V3, _isb_snapshot, asm
from .test_firmware_inline import _fake_view, _run_pass

_JMP = b"\xe9\0\0\0\0"
_KEY = TBKey(0x1000, None, 0x110, 0x800400, 0xff020000)


def _goto(*dests, insn=0):
    """One goto_tb site and static exit per destination (slot n -> dests[n])."""
    return {"sites": tuple(ScmSite(1 + 5 * n, 5 * n, 4, Encoding.REL32, SiteKind.GOTO_TB,
                                   n, 0, insn, 0) for n in range(len(dests))),
            "static_exits": tuple(ScmStaticExit(n, 1, insn, dest) for n, dest in enumerate(dests))}


_RET = {"sites": (ScmSite(0, 0, 0, Encoding.NONE, SiteKind.GOTO_PTR, 0, 0, 0, 0),)}


def _isb_branch_snapshot(barriers="dsb sy; isb sy"):
    """A hot loop whose callee has an ISB arm and a nested-call arm::

        worker  0x1000: bl f            0x1004: b worker
        f       0x1100: cbz r0, isb_arm
                0x1102: bl g            0x1106: bx lr
        isb_arm 0x1108: str r1,[r0]; <barriers>  (goto_tb to the next insn)
                <next>: bx lr
        g       0x1200: bx lr

    The recursive plan inlines g (depth 2), so the pass also builds the flat
    fallback plan; both reach the ISB member through f's branch.
    """
    barrier = asm(0x1108, f"str r1,[r0]; {barriers}")
    after = 0x1108 + len(barrier)
    offsets = [0, *range(2, len(barrier), 4)]
    insns = [((0x108 + off, 0, 0), k, k + 1) for k, off in enumerate(offsets)]
    insns[-1] = (insns[-1][0], len(offsets) - 1, 5)
    guest = {pc: asm(pc, src) for pc, src in (
        (0x1000, "bl 0x1100"), (0x1004, "b 0x1000"), (0x1100, "cbz r0, 0x1108"),
        (0x1102, "bl 0x1200"), (0x1106, "bx lr"), (after, "bx lr"), (0x1200, "bx lr"))}
    one = {"jmp_reset_offset": (5, 0xffff), "jmp_insn_offset": (1, 0xffff)}
    units = [
        SynthUnit(_KEY, 0x100, _JMP, guest_bytes=guest[0x1000], vaddr=0x1000, meta=_goto(0x1100), **one),
        SynthUnit(_KEY, 0x200, _JMP, guest_bytes=guest[0x1004], vaddr=0x1004, meta=_goto(0x1000), **one),
        SynthUnit(_KEY, 0x300, _JMP + _JMP, guest_bytes=guest[0x1100], vaddr=0x1100,
                  jmp_reset_offset=(5, 10), jmp_insn_offset=(1, 6), meta=_goto(0x1102, 0x1108)),
        SynthUnit(_KEY, 0x400, _JMP, guest_bytes=guest[0x1102], vaddr=0x1102, meta=_goto(0x1200), **one),
        SynthUnit(_KEY, 0x500, b"\xff\xe0", guest_bytes=guest[0x1106], vaddr=0x1106, meta=_RET),
        SynthUnit(_KEY, 0x600, _JMP, guest_bytes=barrier, vaddr=0x1108, icount=len(offsets),
                  meta={**_goto(after, insn=len(offsets) - 1), "insns": tuple(insns)}, **one),
        SynthUnit(_KEY, 0x700, b"\xff\xe0", guest_bytes=guest[after], vaddr=after, meta=_RET),
        SynthUnit(_KEY, 0x800, b"\xff\xe0", guest_bytes=guest[0x1200], vaddr=0x1200, meta=_RET),
    ]
    snapshot = fake_snapshot(units)
    snapshot._units = tuple(replace(u, origin="elf_verified") for u in snapshot.units())
    snapshot._by_uid = {u.uid: u for u in snapshot._units}
    data = bytearray(0x202)
    for pc, raw in {**guest, 0x1108: barrier}.items():
        data[pc - 0x1000:pc - 0x1000 + len(raw)] = raw
    image = GuestImage("synthetic", "sha256:" + "6" * 64, ((0x1000, bytes(data)),),
                       ((0x1000, 0x1202),), (), ())
    return snapshot, image


def _view(snapshot):
    """The fall-through arm is the hot loop: worker -> f -> g -> f tail -> worker."""
    uid = {u.vaddrs[0]: u.uid for u in snapshot.units()}
    hot = 5000
    return uid, _fake_view(snapshot, [
        (uid[0x1000], uid[0x1100], "chain0", hot), (uid[0x1100], uid[0x1102], "chain0", hot),
        (uid[0x1102], uid[0x1200], "chain0", hot), (uid[0x1200], uid[0x1106], "lookup_hit", hot),
        (uid[0x1106], uid[0x1004], "lookup_hit", hot), (uid[0x1004], uid[0x1000], "chain0", hot)])


def _recording(monkeypatch):
    """Record every plan the pass builds: (expand_nested, expansion, plan)."""
    built = []
    build = FI.build_firmware_region

    def record(*args, **kwargs):
        plan = build(*args, **kwargs)
        built.append((kwargs["expand_nested"], kwargs["expansion"], plan))
        return plan
    monkeypatch.setattr(FI, "build_firmware_region", record)
    return built


def _run(monkeypatch, params, barriers="dsb sy; isb sy"):
    snapshot, image = _isb_branch_snapshot(barriers)
    uid, view = _view(snapshot)
    built = _recording(monkeypatch)
    result, receipt, emitted, calls = _run_pass(monkeypatch, snapshot, image, params, view)
    root = next(c for c in calls.calls if c.insn_vaddr == 0x1000)
    root_plans = [(nested, expansion, plan) for nested, expansion, plan in built
                  if plan.callsite.callsite_id == root.callsite_id]
    return snapshot, uid, result, receipt, emitted, root_plans


def _member_vaddrs(snapshot, plan):
    return [snapshot.unit(m.source_uid).vaddrs[0] for m in plan.members]


def _edges_from(snapshot, plan, vaddr):
    return [e for e in plan.edges
            if snapshot.unit(plan.members[e.source_member].source_uid).vaddrs[0] == vaddr]


_HOT = {"mode": "hot-loop", "min_call_count": 2}

#: Regions emitted for the fixture by the code before isb_boundary existed
#: (fd-int 5ff088c + the enabled key).  Absent, the parameter must reproduce them.
_REGIONS_BEFORE = {
    "hot-loop": ["sha256:955f3f0035687ff7c35620671ed92676b0187e3cee06b1d951380854709d0e24"],
    "hot-loop-public": ["sha256:f17f6f21d709019560cf0fe9ead3a3d960617e3dd79ed6bf4173241f227c56c2"],
    "exhaustive": ["sha256:955f3f0035687ff7c35620671ed92676b0187e3cee06b1d951380854709d0e24",
                   "sha256:f17f6f21d709019560cf0fe9ead3a3d960617e3dd79ed6bf4173241f227c56c2"],
}
_PARAMS = {"hot-loop": _HOT, "hot-loop-public": {**_HOT, "nested": "public"},
           "exhaustive": {"mode": "exhaustive"}}


def test_isb_boundary_is_a_bool_parameter_of_the_schema():
    descriptor = FI.FirmwareInline.descriptor
    assert descriptor.params_schema["isb_boundary"] is bool
    assert _check_params(descriptor, {"isb_boundary": True}) is None
    assert _check_params(descriptor, {"isb_boundary": False}) is None
    for bad in (1, 0, "true", None):
        assert "isb_boundary" in _check_params(descriptor, {"isb_boundary": bad})


@pytest.mark.parametrize("bad", [1, "true"])
def test_a_non_bool_isb_boundary_is_invalid_params(monkeypatch, bad):
    _, _, result, _, emitted, _ = _run(monkeypatch, {**_HOT, "isb_boundary": bad})
    assert (result.outcome.value, result.reason) == ("failed", "invalid_params")
    assert "isb_boundary" in result.detail and not emitted


@pytest.mark.parametrize("case", sorted(_REGIONS_BEFORE))
def test_absent_isb_boundary_keeps_todays_plans(monkeypatch, case):
    snapshot, uid, result, receipt, emitted, plans = _run(monkeypatch, _PARAMS[case])
    assert [p.region_id for p in emitted] == _REGIONS_BEFORE[case]
    assert receipt["policy"]["isb_boundary"] is False and "isb_sources" not in receipt["policy"]
    assert not any(r["reason"] == "isb_boundary" for r in receipt["residual"])
    for nested, expansion, plan in plans:
        # The policy's own expansion is passed through unchanged (none for flat plans).
        if not nested or case == "exhaustive":
            assert expansion is None
        else:
            assert type(expansion).__name__ == "NestedProfitExpansion"
        isb_edge, = _edges_from(snapshot, plan, 0x1108)
        assert isb_edge.kind == "branch" and isb_edge.target_member is not None
    # A false value is the same as absent outside firmlooper-v3.
    monkeypatch.undo()
    _, _, _, receipt_false, emitted_false, _ = _run(monkeypatch, {**_PARAMS[case], "isb_boundary": False})
    assert [p.region_id for p in emitted_false] == _REGIONS_BEFORE[case]
    assert receipt_false == receipt


@pytest.mark.parametrize("nested", ["profit", "public"])
def test_isb_boundary_true_cuts_every_isb_branch_in_recursive_and_flat_plans(monkeypatch, nested):
    params = {**_HOT, "nested": nested}
    snapshot, uid, _, before, _, plans_before = _run(monkeypatch, params)
    monkeypatch.undo()
    _, _, result, receipt, emitted, plans = _run(monkeypatch, {**params, "isb_boundary": True})
    assert _ends_in_isb(snapshot.unit(uid[0x1108]), _isb_branch_snapshot()[1])
    kinds = sorted(nested_flag for nested_flag, _, _ in plans)
    # Recursive hot-loop builds the recursive plan and its flat fallback;
    # nested=public builds the flat plan only.
    assert kinds == ([False, True] if nested == "profit" else [False])
    assert sorted(n for n, _, _ in plans_before) == kinds
    old = {n: plan for n, _, plan in plans_before}
    for nested_flag, _, plan in plans:
        isb_edge, = _edges_from(snapshot, plan, 0x1108)
        assert (isb_edge.kind, isb_edge.target_member, isb_edge.refusal_reason,
                isb_edge.guest_target) == ("side_exit", None, "isb_boundary", 0x1112)
        # Everything else is the plan without the rule, minus the ISB member's
        # successor, which the side exit no longer reaches.
        assert _member_vaddrs(snapshot, plan) == [
            v for v in _member_vaddrs(snapshot, old[nested_flag]) if v != 0x1112]
        unchanged = [e for e in plan.edges if e is not isb_edge]
        assert [(e.kind, e.exit_id, e.refusal_reason, e.guest_target) for e in unchanged] == [
            (e.kind, e.exit_id, e.refusal_reason, e.guest_target)
            for e in old[nested_flag].edges
            if e not in _edges_from(snapshot, old[nested_flag], 0x1108)
            and e not in _edges_from(snapshot, old[nested_flag], 0x1112)]
    assert receipt["policy"]["isb_boundary"] is True and receipt["policy"]["isb_sources"] == 1
    # The root and nested call decisions still come from the policy.
    assert receipt["policy"]["nested_decisions"] == before["policy"]["nested_decisions"]
    if nested == "profit":
        # The recursive region is admitted and carries the side exit.
        decision, = receipt["policy"]["nested_decisions"]
        assert decision["action"] == "inline"
        region, = emitted
        assert region.callsite.insn_vaddr == 0x1000
        assert any(e.refusal_reason == "isb_boundary" for e in region.edges)
        assert receipt["embedded"][0]["expansion_depth"] == 2
        row, = [r for r in receipt["residual"] if r["reason"] == "isb_boundary"]
        assert row["guest_target"] == 0x1112 and row["exit_kind"] == "goto_tb"
    else:
        # One-level regions: the outer root is refused as unprofitable either way.
        assert receipt["policy"]["nested_decisions"] is None
        assert [r["reason"] for r in receipt["residual"] if r.get("region_id")] == \
            [r["reason"] for r in before["residual"] if r.get("region_id")] == ["not_profitable"]


def test_isb_boundary_true_keeps_a_dsb_member_branch(monkeypatch):
    _, _, _, before, emitted_before, _ = _run(monkeypatch, _HOT, barriers="dsb sy")
    monkeypatch.undo()
    snapshot, _, _, receipt, emitted, plans = _run(monkeypatch, {**_HOT, "isb_boundary": True},
                                                   barriers="dsb sy")
    assert [p.region_id for p in emitted] == [p.region_id for p in emitted_before]
    assert receipt["policy"]["isb_sources"] == 0
    for _, _, plan in plans:
        edge, = _edges_from(snapshot, plan, 0x1108)
        assert edge.kind == "branch" and edge.target_member is not None


def _v3_run(monkeypatch, **extra):
    snapshot, image = _isb_snapshot()
    return _run_pass(monkeypatch, snapshot, image, {**_V3, **extra}, None)


def test_isb_boundary_false_on_firmlooper_v3_is_invalid_params(monkeypatch):
    result, detail, emitted, _ = _v3_run(monkeypatch, isb_boundary=False)
    assert (result.outcome.value, result.reason) == ("failed", "invalid_params")
    assert "firmlooper-v3 always keeps the ISB side exit" in detail and not emitted


def test_isb_boundary_true_on_firmlooper_v3_is_its_own_rule(monkeypatch):
    result, receipt, emitted, _ = _v3_run(monkeypatch)
    explicit, explicit_receipt, explicit_emitted, _ = _v3_run(monkeypatch, isb_boundary=True)
    assert emitted and [p.region_id for p in explicit_emitted] == [p.region_id for p in emitted]
    assert explicit_receipt == receipt
    assert receipt["policy"]["isb_boundary"] is True and "isb_sources" not in receipt["policy"]
    assert any(r["reason"] == "isb_boundary" for r in receipt["residual"])
    assert explicit.reason == result.reason == "firmware_regions_emitted"


def test_isb_boundary_needs_no_change_to_the_discovered_calls(monkeypatch):
    snapshot, image = _isb_branch_snapshot()
    calls = discover_firmware_calls(snapshot, image).calls
    assert sorted(hex(c.insn_vaddr) for c in calls) == ["0x1000", "0x1102"]
    assert all(c.eligible for c in calls)
