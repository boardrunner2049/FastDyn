"""GENERAL profitability charges first-use installation once per recorded replay."""
from types import SimpleNamespace

import pytest

from scopt.analysis.return_edges import Candidate
from scopt.ids import TBKey
from scopt.passes.base import Outcome
from scopt.passes.chain_returns import general_slots


def run_slots(monkeypatch, hits, *, params=None, replays=1, exit_class="return",
              existing=()):
    monkeypatch.setattr("scopt.guard.guard_key", lambda flags, cs: (flags, cs))
    candidates = tuple(
        Candidate(f"target-{i}", i * 0x200, count, status, 1.0, count, i, ())
        for i, (count, status) in enumerate(hits, start=1)
    )
    exit_row = SimpleNamespace(exit_class=exit_class, uid="source", tail_off=0,
                               admitted=True, free_slots=(0, 1), exit_id="exit",
                               candidates=candidates)
    units = {item.dst: SimpleNamespace(uid=item.dst, key=TBKey(item.vaddr, None, 2, 0, 0))
             for item in candidates}
    plan = (SimpleNamespace(slots=existing, key_flags=2, key_cs_base=0) if existing else None)
    view = SimpleNamespace(b3=SimpleNamespace(variant_sources=lambda: (),
                          claimed_slots=lambda uid: tuple(s.slot_n for s in existing),
                          exit_plan=lambda uid, off: plan))
    b3 = SimpleNamespace(layout_bound=True, offsets=object(), units=lambda: units)
    rows = SimpleNamespace(exits=(exit_row,))
    collection = {"runs": [{}] * replays, "repetitions": 1}
    profile = SimpleNamespace(profile=SimpleNamespace(collection=collection))
    ctx = SimpleNamespace(b3=b3, params=params or {}, snapshot_id="s",
                          analysis=lambda name, scope: SimpleNamespace(value=rows),
                          profile=lambda phase: profile, budget=lambda: None)
    redirected = []
    edits = SimpleNamespace(redirect_exit=lambda slot, **kw: redirected.append(slot))
    result = general_slots(view, ctx, edits, exit_class)
    return result, [row.target_vaddr for row in redirected]


@pytest.mark.parametrize("exit_class", ("static", "return"))
def test_prune_low_or_uncertain_reuse_before_slot_selection(exit_class, monkeypatch):
    result, targets = run_slots(monkeypatch, [(20, "lower_bound"), (1, "exact"), (12, "exact")],
                                params={"min_target_hits": 12, "min_pass_share": 0.0,
                                        "targets_per_exit": 1}, exit_class=exit_class)
    assert result.outcome == Outcome.CANDIDATE
    assert targets == [0x600]
    assert result.stats["targets_below_minimum_exact_hits"] == 1
    assert result.stats["targets_without_estimated_reuse"] == 1
    assert result.stats["estimated_train_lookups_avoided"] == 11
    assert result.stats["estimated_train_link_installs"] == 1


def test_oneshot_consistently_cold_target_is_never_admitted(monkeypatch):
    result, targets = run_slots(monkeypatch, [(4, "exact")], replays=4)
    assert not targets and result.reason == "not_profitable"
    assert result.stats["training_replays"] == 4


def test_marginal_cost_rejects_cold_target_even_with_hot_target(monkeypatch):
    result, targets = run_slots(monkeypatch, [(22, "exact"), (3, "exact")],
                                params={"c_link": 8.0}, replays=2)
    assert targets == [0x200]
    assert result.stats["estimated_train_link_installs"] == 2
    assert result.stats["estimated_train_lookups_avoided"] == 20
    assert result.stats["train_guard_attempts"] == 25


def test_profitable_second_target_does_not_pay_existing_spec_guard_twice(monkeypatch):
    result, targets = run_slots(monkeypatch, [(20, "exact"), (2, "exact")],
                                existing=(SimpleNamespace(slot_n=0, vaddr=0x200, weight=20),))
    assert targets == [0x400]
    assert result.stats["estimated_train_link_installs"] == 1
    assert result.stats["estimated_train_lookups_avoided"] == 1


def test_fresh_guard_charges_uncovered_lookup_traffic(monkeypatch):
    result, targets = run_slots(monkeypatch, [(2, "exact"), (10, "lower_bound")],
                                params={"min_target_hits": 2, "min_pass_share": 0.0})
    assert not targets and result.reason == "not_profitable"
    assert result.stats["estimated_train_lookups_avoided"] == 0


def test_two_marginally_profitable_targets_share_one_fresh_guard(monkeypatch):
    result, targets = run_slots(monkeypatch, [(2, "exact"), (2, "exact")], params={"c_guard": 2.0})
    assert targets == [0x200, 0x400]
    assert result.stats["estimated_train_link_installs"] == 2
    assert result.stats["estimated_train_lookups_avoided"] == 2
    assert result.stats["train_guard_attempts"] == 4
