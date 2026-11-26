from types import SimpleNamespace

import pytest

from scopt.link import B3Plan, NativeLinkerV2
from scopt.manager_b3 import B3State
from scopt.model.snapshot import WorkingSet
from scopt.model.view import WorkingSetView
from scopt.passes import arena_slide, page_layout
from scopt.passes.base import Outcome, Patch
from scopt.passes.irq_entry import IrqEntryMemCompare
from tests.synth.fake_snapshot import fake_snapshot


@pytest.mark.parametrize("cls", [arena_slide.AlignTracePages, page_layout.PageAwareLocalLayout,
                                 IrqEntryMemCompare])
def test_public_only_pass_reports_input_features(cls):
    header = SimpleNamespace(version=7, reserved=0x80000001)
    snap = SimpleNamespace(header=header)
    ctx = SimpleNamespace(b3=SimpleNamespace(snapshot=snap, layout_bound=True), b3_state=B3State())
    result = cls().run(snap, ctx, None)
    assert result.reason == "existing_cache_extensions_unsupported"
    assert "0x80000001" in result.detail


@pytest.mark.parametrize("cls", [arena_slide.AlignTracePages, page_layout.PageAwareLocalLayout])
def test_page_layout_identifies_incompatible_pending_edits(cls):
    snap = SimpleNamespace(header=SimpleNamespace(version=7, reserved=0))
    for state, reason in [(B3State(exits={("source", 0): object()}), "pending_exit_growth_changes_layout"),
                          (B3State(firmware_regions={"region": object()}), "firmware_region_terminal_stage")]:
        assert cls().run(snap, SimpleNamespace(b3_state=state), None).reason == reason


def test_alignment_composes_with_a_public_rewrite_in_the_semantic_linker(monkeypatch):
    snapshot = fake_snapshot(16)
    source, target = snapshot.units()[-2:]
    edge = dict(src=source.uid, dst=target.uid, kind="chain0", status="exact", flags=[], count=10)
    profile = SimpleNamespace(edges=lambda phase: [edge])
    monkeypatch.setattr(arena_slide, "phase_profile_counts", lambda *args: (None, profile))
    monkeypatch.setattr(arena_slide, "_transfer_offsets", lambda unit: {"chain0": 0})
    patch = Patch(target.uid, 0, target.code[:1], b"\x90", "dho_nop")
    state = B3State(rewrites={target.uid: (("dead-host-ops", patch),)})
    ctx = SimpleNamespace(b3=object(), b3_state=state, budget=lambda: None)
    placements = {}
    result = arena_slide.AlignTracePages().run(WorkingSetView(WorkingSet(snapshot)), ctx,
                                               SimpleNamespace(place=placements.__setitem__))
    assert result.outcome == Outcome.CANDIDATE
    assert len(placements) == len(snapshot.units())
    plan = B3Plan(rewrites={target.uid: (patch,)})
    linked = NativeLinkerV2(snapshot, plan, placement=WorkingSet(snapshot, placements=placements),
                            derive_captures=False).link()
    assert linked.ok, linked.failures
    assert linked.by_uid()[target.uid].new_tb_offset == placements[target.uid]
    assert linked.by_uid()[target.uid].body[:1] == patch.new_bytes


def test_page_order_uses_a_public_capture_with_semantic_context(monkeypatch):
    snapshot = fake_snapshot(2)
    view = WorkingSetView(WorkingSet(snapshot))
    monkeypatch.setattr(page_layout, "phase_profile_counts", lambda *args: (None, None))
    ctx = SimpleNamespace(b3=object(), b3_state=B3State(), params={})
    assert page_layout.PageAwareLocalLayout().run(view, ctx, None).reason == "missing_steady_profile"
