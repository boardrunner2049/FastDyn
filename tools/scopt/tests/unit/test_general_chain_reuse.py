"""GENERAL profitability charges first-use installation once per recorded replay."""
from types import SimpleNamespace

import pytest
from dataclasses import replace

from scopt.analysis.return_edges import Candidate
from scopt.formats.profile_v1 import Count, METHOD
from scopt.guard import COMPACT_GUARD, stub_size_bound
from scopt.ids import TBKey
from scopt.manager import load_pipeline
from scopt.passes.base import Outcome
from scopt.passes.chain_returns import _exact_return_hits, general_slots


def run_slots(monkeypatch, hits, *, params=None, replays=1, exit_class="return",
              existing=(), complete=True, source_status="exact", extra_rows=(), variant_size=0):
    monkeypatch.setattr("scopt.guard.guard_key", lambda flags, cs: (flags, cs))
    candidates = tuple(
        Candidate(f"target-{i}", i * 0x200, count, status, 1.0, count, i, ())
        for i, (count, status) in enumerate(hits, start=1)
    )
    exit_row = SimpleNamespace(exit_class=exit_class, exit_kind="goto_ptr", uid="source", tail_off=0,
                               admitted=True, free_slots=(0, 1), exit_id="exit",
                               candidates=candidates)
    units = {item.dst: SimpleNamespace(uid=item.dst, key=TBKey(item.vaddr, None, 2, 0, 0))
             for item in candidates}
    plan = (SimpleNamespace(slots=existing, key_flags=2, key_cs_base=0) if existing else None)
    units["variant-source"] = SimpleNamespace(end_offset=variant_size, tb_offset=0)
    variants = {"variant": SimpleNamespace(src_uid="variant-source")} if variant_size else {}
    view = SimpleNamespace(b3=SimpleNamespace(variant_sources=lambda: (), variants=variants,
                          exits={("source", 0): plan} if plan else {},
                          claimed_slots=lambda uid: tuple(s.slot_n for s in existing),
                          exit_plan=lambda uid, off: plan))
    b3 = SimpleNamespace(layout_bound=True, offsets=object(), units=lambda: units)
    rows = SimpleNamespace(exits=(exit_row, *[
        SimpleNamespace(**{**vars(exit_row), "uid": uid,
                         "candidates": (replace(candidates[0], count=count, vaddr=vaddr),)})
        for uid, count, vaddr in extra_rows]))
    collection = {"runs": [{}] * replays, "repetitions": 1, "method": METHOD, "complete": complete}
    profile = SimpleNamespace(profile=SimpleNamespace(collection=collection),
                              count=lambda *_: Count(sum(n for n, _ in hits), source_status))
    ctx = SimpleNamespace(b3=b3, params=params or {}, snapshot_id="s",
                          analysis=lambda name, scope: SimpleNamespace(value=rows),
                          profile=lambda phase: profile, budget=lambda: None)
    redirected = []
    edits = SimpleNamespace(redirect_exit=lambda slot, **kw: redirected.append(slot))
    result = general_slots(view, ctx, edits, exit_class)
    if params and params.get("min_return_share") and redirected:
        assert [row.guard_template_id for row in redirected] == [COMPACT_GUARD]
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


def test_dominant_return_uses_existing_compact_general_guard(monkeypatch):
    p, = load_pipeline("dominant-return").passes
    assert p.name == "chain-returns" and p.version == 1
    result, targets = run_slots(monkeypatch, [(11, "exact"), (1, "exact")], params=p.params)
    assert result.outcome == Outcome.CANDIDATE
    assert targets == [0x200]
    assert result.stats["train_exact_return_hits"] == 12
    assert result.stats["min_exact_target_hits"] == 11


def test_dominant_return_stays_warm_when_no_target_meets_exact_share(monkeypatch):
    p, = load_pipeline("dominant-return").passes
    result, targets = run_slots(monkeypatch, [(89, "exact"), (11, "exact")], params=p.params)
    assert not targets and result.stats["slots"] == 0
    assert result.stats["train_exact_return_hits"] == 100
    assert result.stats["min_exact_target_hits"] == 90


@pytest.mark.parametrize("complete,status,source", [
    (False, "exact", "exact"), (True, "lower_bound", "exact"), (True, "exact", "unknown"),
])
def test_dominant_return_stays_warm_on_inexact_training(monkeypatch, complete, status, source):
    p, = load_pipeline("dominant-return").passes
    result, targets = run_slots(monkeypatch, [(100, "exact"), (1, status)], params=p.params,
                                complete=complete, source_status=source)
    assert not targets and result.reason == "return_traffic_inexact"
    assert result.outcome == Outcome.SKIPPED_UNKNOWN


def test_exact_dominance_denominator_includes_refused_and_external_returns():
    def source(uid, count, dst, reason=(), exit_class="return"):
        c = Candidate(dst, None, count, "exact", 1.0, count, 1, reason)
        return SimpleNamespace(uid=uid, exit_kind="goto_ptr", exit_class=exit_class, candidates=(c,))

    collection = {"complete": True, "method": METHOD}
    # An edge entering steady may have a source whose entries were all in boot.
    view = SimpleNamespace(profile=SimpleNamespace(collection=collection),
                           count=lambda uid, *_: Count(0 if uid == "out" else 100, "exact"))
    rows = [source("hot", 80, "target"),
            source("out", 12, None, ("continuation_not_in_snapshot",)),
            source("rejected", 8, "other", ("not_relocatable",)),
            source("static", 90, "tail", exit_class="static")]
    assert _exact_return_hits(rows, view) == 100
    assert _exact_return_hits([*rows, source("hot", 80, "second", exit_class="static")], view) is None


def test_net_work_ranks_exits_before_applying_global_unit_limit(monkeypatch):
    extra = [("warmer", 32, 0x800), ("hottest", 80, 0xa00)]
    for order in (extra, list(reversed(extra))):
        control, chosen = run_slots(monkeypatch, [(10, "exact")], extra_rows=order,
                                    params={"max_units": 1})
        assert chosen == [0x200]
        ranked, chosen = run_slots(monkeypatch, [(10, "exact")], extra_rows=order,
                                   params={"rank_by_net_work": True, "max_units": 1})
        assert chosen == [0xa00]
        assert ranked.stats["estimated_train_lookups_avoided"] > control.stats["estimated_train_lookups_avoided"]
        assert ranked.stats["eligible_exit_plans"] == 3


def test_second_target_must_pay_compare_cost_on_remaining_traffic(monkeypatch):
    hits = [(75, "exact"), (3, "exact"), (22, "exact")]
    result, chosen = run_slots(monkeypatch, hits, params={
        "rank_by_net_work": True, "c_target_check": 10, "c_guard": 1, "c_lookup": 5})
    assert chosen == [0x200]
    assert result.stats["estimated_net_saving_ns"] == 74 * 5 - 100
    assert result.stats["train_unmatched_guard_attempts"] == 25


def test_shared_footprint_counts_prior_variant_and_general_guard_only_once(monkeypatch):
    existing = (SimpleNamespace(slot_n=0, vaddr=0x200, weight=80, compact=False, r0_zero=False),)
    limit = 45 + stub_size_bound(2, False)
    for cap, expected in [(limit, [0x400]), (limit - 1, [])]:
        result, chosen = run_slots(monkeypatch, [(80, "exact"), (18, "exact")], existing=existing,
                                   variant_size=45, params={"max_total_growth_bytes": cap})
        assert chosen == expected
        assert result.stats["existing_growth_upper_bytes"] == 45 + stub_size_bound(1, False)
        if chosen:
            assert result.stats["proposed_growth_upper_bytes"] == limit
        else:
            assert result.stats["refused:growth_budget"] == 1


def test_compact_guard_fits_tighter_shared_budget(monkeypatch):
    cap = stub_size_bound(1, True)
    assert cap < stub_size_bound(1, False)
    for compact in (False, True):
        result, chosen = run_slots(monkeypatch, [(30, "exact")], params={
            "targets_per_exit": 1, "max_total_growth_bytes": cap, "compact_guard": compact})
        assert bool(chosen) == compact


def test_new_net_work_policy_requires_complete_training_evidence(monkeypatch):
    result, chosen = run_slots(monkeypatch, [(80, "exact")], complete=False,
                               params={"rank_by_net_work": True})
    assert not chosen and result.outcome == Outcome.SKIPPED_UNKNOWN


def test_ranked_exit_can_admit_the_single_target_that_fits(monkeypatch):
    result, chosen = run_slots(monkeypatch, [(60, "exact"), (40, "exact")], params={
        "rank_by_net_work": True, "c_target_check": 1, "compact_guard": True,
        "max_total_growth_bytes": stub_size_bound(1, True)})
    assert chosen == [0x200]
    assert result.stats["proposed_growth_upper_bytes"] == stub_size_bound(1, True)
    assert result.stats["estimated_train_link_installs"] == 1
