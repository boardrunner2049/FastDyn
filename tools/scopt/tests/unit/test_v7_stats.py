from __future__ import annotations

import copy
import json

import pytest

from scopt.run import v7_stats


# Text in the layout sc7_write_loop_return_stats prints (trusted, loaded).
_QEMU_TEXT = """{
  "schema": "qemu-sc-v7-stats@1",
  "loaded": true,
  "mode": "trusted",
  "artifact_id8": "0123456789abcdef",
  "counters_revision": 2,
  "loop_returns_nonnull": 1000,
  "trusted_residual_exits": 300,
  "trusted_residual_exits_slot": [200, 100],
  "trusted_tb_chain_probes": 40,
  "chain_installs_trusted_source": 0,
  "benchmark_loop_returns_nonnull": 900,
  "benchmark_trusted_residual_exits": 250,
  "benchmark_trusted_tb_chain_probes": 30,
  "benchmark_chain_installs_trusted_source": 0,
  "trusted_residual_exit_regions": [
    {"region": 0, "region_id8": "00112233445566ff", "pc": "0x08000120", "exits": 120, "benchmark_exits": 100},
    {"region": 3, "region_id8": "aabbccddeeff0011", "pc": "0x08000400", "exits": 180, "benchmark_exits": 150}
  ],
  "regions_loaded": 4,
  "trusted_tb_calls": 1040,
  "chain_attempts": 20,
  "chain_skipped_global": 0,
  "chain_skipped_trusted_source": 0,
  "search_data_hits": 0
}
"""

# Archived packet config 7 (pre-revision-2) values.
_LEGACY = {
    "schema": "qemu-sc-v7-stats@1", "loaded": True, "mode": "trusted",
    "trusted_tb_calls": 2_946_063, "chain_attempts": 1_429,
    "chain_skipped_global": 0, "chain_skipped_trusted_source": 0,
}


@pytest.fixture
def trusted():
    return json.loads(_QEMU_TEXT)


def _unloaded():
    return json.loads("""{
  "schema": "qemu-sc-v7-stats@1",
  "loaded": false,
  "mode": "safe",
  "counters_revision": 2,
  "loop_returns_nonnull": 77,
  "trusted_residual_exits": 0,
  "trusted_residual_exits_slot": [0, 0],
  "trusted_tb_chain_probes": 0,
  "chain_installs_trusted_source": 0,
  "benchmark_loop_returns_nonnull": 70,
  "benchmark_trusted_residual_exits": 0,
  "benchmark_trusted_tb_chain_probes": 0,
  "benchmark_chain_installs_trusted_source": 0,
  "trusted_residual_exit_regions": [],
  "trusted_tb_calls": 0,
  "chain_attempts": 5,
  "chain_skipped_global": 0
}""")


def test_legacy_file_has_no_problems_and_no_residual_exits():
    assert v7_stats.counters_revision(_LEGACY) == 1
    assert v7_stats.counter_problems(_LEGACY) == []
    assert v7_stats.residual_exits(_LEGACY) is None
    assert v7_stats.residual_exits(_LEGACY, window=True) is None
    assert v7_stats.loop_returns(_LEGACY) is None


def test_legacy_loop_returns_on_packet_config7():
    assert v7_stats.legacy_loop_returns(_LEGACY) == 2_943_205
    summary = v7_stats.summary(_LEGACY)
    assert summary["legacy_loop_returns"] == 2_943_205
    assert "not region exits" in summary["legacy_loop_returns_label"]
    assert "residual_exits" not in summary


def test_legacy_loop_returns_only_for_legacy_loaded_trusted(trusted):
    assert v7_stats.legacy_loop_returns(trusted) is None
    assert v7_stats.legacy_loop_returns({**_LEGACY, "loaded": False}) is None
    assert v7_stats.legacy_loop_returns({**_LEGACY, "mode": "safe"}) is None
    assert v7_stats.legacy_loop_returns({**_LEGACY, "chain_attempts": None}) is None


def test_consistent_revision2_trusted_file(trusted):
    assert v7_stats.counter_problems(trusted) == []
    assert v7_stats.residual_exits(trusted) == 300
    assert v7_stats.residual_exits(trusted, window=True) == 250
    assert v7_stats.loop_returns(trusted) == 1000
    assert v7_stats.summary(trusted) == {
        "counters_revision": 2,
        "loop_returns_nonnull": 1000,
        "benchmark_loop_returns_nonnull": 900,
        "residual_exits": 300,
        "benchmark_residual_exits": 250,
        "chain_installs_trusted_source": 0,
    }


def test_unloaded_file_counts_loop_returns_only():
    stats = _unloaded()
    assert v7_stats.counter_problems(stats) == []
    assert v7_stats.loop_returns(stats) == 77
    assert v7_stats.residual_exits(stats) == 0


def test_residual_exits_never_fall_back_to_trusted_tb_calls(trusted):
    del trusted["counters_revision"]
    assert v7_stats.residual_exits(trusted) is None


@pytest.mark.parametrize("mutate, needle", [
    (lambda s: s.update(trusted_tb_calls=1041), "trusted_tb_calls"),
    (lambda s: s["trusted_residual_exit_regions"][1].update(exits=181),
     "trusted_residual_exit_regions exits sum"),
    (lambda s: s["trusted_residual_exit_regions"][0].update(benchmark_exits=101),
     "benchmark_exits sum"),
    (lambda s: s.update(trusted_residual_exits_slot=[200, 101]),
     "trusted_residual_exits_slot sum"),
    (lambda s: s.update(benchmark_loop_returns_nonnull=1001),
     "benchmark_loop_returns_nonnull"),
    (lambda s: s.update(trusted_tb_chain_probes=42, trusted_tb_calls=1042),
     "2 * (chain_attempts"),
])
def test_each_broken_invariant_is_one_problem(trusted, mutate, needle):
    mutate(trusted)
    problems = v7_stats.counter_problems(trusted)
    assert len(problems) == 1, problems
    assert needle in problems[0]


def test_residual_exits_above_loop_returns(trusted):
    trusted.update(loop_returns_nonnull=299, benchmark_loop_returns_nonnull=200,
                   trusted_tb_calls=339)
    problems = v7_stats.counter_problems(trusted)
    assert problems == ["trusted_residual_exits 300 > loop_returns_nonnull 299"]


def test_region_window_above_region_total(trusted):
    regions = trusted["trusted_residual_exit_regions"]
    regions[0]["benchmark_exits"] = 130
    regions[1]["benchmark_exits"] = 120
    assert v7_stats.counter_problems(trusted) == [
        "regions with benchmark_exits > exits: [0]"]


def test_probe_identity_is_gated_on_unchained_trusted_sources(trusted):
    trusted.update(chain_installs_trusted_source=2,
                   benchmark_chain_installs_trusted_source=1,
                   trusted_tb_chain_probes=39, trusted_tb_calls=1039)
    assert v7_stats.counter_problems(trusted) == []


def test_unloaded_file_with_trusted_counts():
    stats = _unloaded()
    stats.update(trusted_residual_exits=3, trusted_residual_exits_slot=[3, 0],
                 trusted_residual_exit_regions=[
                     {"region": 0, "exits": 3, "benchmark_exits": 0}],
                 loop_returns_nonnull=80, benchmark_loop_returns_nonnull=70)
    problems = v7_stats.counter_problems(stats)
    assert problems == [
        "trusted_residual_exits 3 without a loaded trusted directory",
        "trusted_residual_exit_regions without a loaded trusted directory",
    ]


def test_malformed_revision2_files(trusted):
    missing = copy.deepcopy(trusted)
    del missing["trusted_tb_chain_probes"]
    assert "missing" in v7_stats.counter_problems(missing)[0]
    bad_slots = copy.deepcopy(trusted)
    bad_slots["trusted_residual_exits_slot"] = [300]
    assert "slot" in v7_stats.counter_problems(bad_slots)[0]
    bad_regions = copy.deepcopy(trusted)
    bad_regions["trusted_residual_exit_regions"] = [{"region": 0}]
    assert "region counts" in v7_stats.counter_problems(bad_regions)[0]
    boolean = copy.deepcopy(trusted)
    boolean["loop_returns_nonnull"] = True
    assert "missing" in v7_stats.counter_problems(boolean)[0]


def _rundir_launch(tmp_path, monkeypatch, stats_text):
    from click.testing import CliRunner

    from scopt.commands import deploy as deploy_cmd
    from scopt.run import launcher

    run_dir = tmp_path / "rd"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "deploy.json").write_text(json.dumps(
        {"state": {"firmware_artifact_id8": "0123456789abcdef"}}))

    def fake_launch(rd, **kwargs):
        assert kwargs["extra_env"] == {
            "QEMU_SC_V7_STATS": str((run_dir / "v7-stats.json").resolve())}
        (run_dir / "v7-stats.json").write_text(stats_text)
        return {"verdict": "PASS", "exit_code": 0, "appended": 0, "bench": None,
                "proof": None}

    monkeypatch.setattr(launcher, "launch", fake_launch)
    monkeypatch.setattr(launcher.RunDir, "open", classmethod(lambda cls, p: p))
    result = CliRunner().invoke(deploy_cmd.rundir,
                                ["launch", str(run_dir), "--v7-stats", "--json"])
    return result, json.loads(result.output)


def test_rundir_launch_reports_counter_problems(tmp_path, monkeypatch):
    stats = json.loads(_QEMU_TEXT)
    result, doc = _rundir_launch(tmp_path, monkeypatch, json.dumps(stats))
    assert result.exit_code == 0
    assert "problems" not in doc["v7_stats"]

    stats["trusted_tb_calls"] += 1
    result, doc = _rundir_launch(tmp_path, monkeypatch, json.dumps(stats))
    assert doc["v7_stats"]["problems"] == [
        "v7 counters: trusted_tb_calls 1041 != loop_returns_nonnull 1000 + "
        "trusted_tb_chain_probes 40"]


def _revision3(trusted):
    trusted.update(counters_revision=3, chain_attempts=20, chain_skipped_global=0,
                   chain_skipped_trusted_source=3,
                   trusted_residual_exits_chainable=17,
                   trusted_residual_exits_unchainable=283,
                   trusted_residual_slots_armed=17,
                   chain_allowed_trusted_source=15,
                   chain_skipped_trusted_to_trusted=2,
                   chain_installs_trusted_source=15,
                   benchmark_chain_installs_trusted_source=4)
    return trusted


def test_consistent_revision3_chaining_file(trusted):
    stats = _revision3(trusted)
    assert v7_stats.counter_problems(stats) == []
    summary = v7_stats.summary(stats)
    assert summary["residual_exits"] == 300
    assert summary["trusted_residual_slots_armed"] == 17
    assert summary["chain_skipped_trusted_to_trusted"] == 2


@pytest.mark.parametrize("mutate, needle", [
    (lambda s: s.update(trusted_residual_exits_unchainable=284),
     "trusted_residual_exits_unchainable"),
    (lambda s: s.update(chain_allowed_trusted_source=18, chain_installs_trusted_source=16),
     "chain_allowed_trusted_source 18 >"),
    (lambda s: s.update(chain_installs_trusted_source=16),
     "chain_installs_trusted_source 16 >"),
    (lambda s: s.update(chain_skipped_trusted_to_trusted=4), "chain_skipped_trusted_to_trusted"),
    (lambda s: s.update(trusted_residual_slots_armed=0), "without armed residual slots"),
    # Revision 3 always probes both ends, chained trusted sources included.
    (lambda s: s.update(trusted_tb_chain_probes=39, trusted_tb_calls=1039),
     "2 * (chain_attempts"),
])
def test_each_broken_revision3_invariant_is_one_problem(trusted, mutate, needle):
    stats = _revision3(trusted)
    mutate(stats)
    problems = v7_stats.counter_problems(stats)
    assert len(problems) == 1, problems
    assert needle in problems[0]


def test_revision3_requires_chain_keys_and_revision2_does_not(trusted):
    assert v7_stats.counter_problems(trusted) == []      # revision 2 file
    stats = _revision3(copy.deepcopy(trusted))
    del stats["trusted_residual_slots_armed"]
    assert "missing" in v7_stats.counter_problems(stats)[0]


def test_unloaded_revision3_file_with_chain_counts():
    stats = _unloaded()
    stats.update(counters_revision=3, trusted_residual_exits_chainable=0,
                 trusted_residual_exits_unchainable=0, trusted_residual_slots_armed=2,
                 chain_allowed_trusted_source=0, chain_skipped_trusted_to_trusted=0)
    assert v7_stats.counter_problems(stats) == [
        "trusted_residual_slots_armed 2 without a loaded trusted directory"]


# -- revision 4: trusted->trusted chains and composed member skip flags -----

def test_revision4_is_one_bump_for_both_key_groups():
    assert v7_stats.COUNTERS_REVISION == 4
    assert v7_stats.T2T_REVISION == v7_stats.MEMBER_SKIP_REVISION == 4
    assert v7_stats.T2T_COUNTER_KEYS == (
        "chain_allowed_trusted_to_trusted", "chain_installs_trusted_to_trusted",
        "chain_installs_trusted_self", "chain_skipped_trusted_poll_unverified",
        "chain_skipped_trusted_unsealed", "trusted_poll_entries_verified",
        "trusted_poll_entries_unverified", "trusted_poll_entry_exits")
    assert v7_stats.MEMBER_SKIP_COUNTER_KEYS == ("trusted_residual_slots_skipped",)
    # The goto_ptr hook of the original follow-up 1 design was dropped.
    assert "lookup_ptr_trusted_entries" not in v7_stats.T2T_COUNTER_KEYS


def _revision4(trusted):
    """A consistent revision-4 file: 2 of 15 allowed trusted chains go to a
    trusted entry, one of them a self-chain, and its poll fired once; 6 member
    skip bits were seen while arming."""
    stats = _revision3(trusted)
    stats.update(counters_revision=4,
                 chain_skipped_trusted_to_trusted=0,
                 chain_skipped_trusted_source=1,
                 chain_installs_trusted_destination=5,
                 chain_allowed_trusted_to_trusted=2,
                 chain_installs_trusted_to_trusted=2,
                 chain_installs_trusted_self=1,
                 chain_skipped_trusted_poll_unverified=0,
                 chain_skipped_trusted_unsealed=0,
                 trusted_poll_entries_verified=4,
                 trusted_poll_entries_unverified=0,
                 trusted_poll_entry_exits=1,
                 trusted_residual_slots_skipped=6,
                 experiment={"no_trusted_to_trusted_chain": False,
                             "no_trusted_exit_chain": False})
    return stats


def test_consistent_revision4_file_and_summary(trusted):
    stats = _revision4(trusted)
    assert v7_stats.counter_problems(stats) == []
    summary = v7_stats.summary(stats)
    assert summary["counters_revision"] == 4
    assert summary["chain_installs_trusted_to_trusted"] == 2
    assert summary["trusted_poll_entry_exits"] == 1
    assert summary["trusted_residual_slots_skipped"] == 6
    assert summary["trusted_residual_slots_armed"] == 17


@pytest.mark.parametrize("mutate, needle", [
    (lambda s: s.update(chain_allowed_trusted_to_trusted=16, chain_installs_trusted_to_trusted=2),
     "chain_allowed_trusted_to_trusted 16 > chain_allowed_trusted_source"),
    (lambda s: s.update(chain_installs_trusted_to_trusted=3, chain_installs_trusted_self=1),
     "chain_installs_trusted_to_trusted 3 > chain_allowed_trusted_to_trusted"),
    (lambda s: s.update(chain_installs_trusted_destination=1),
     "chain_installs_trusted_to_trusted 2 > chain_installs_trusted_destination 1"),
    (lambda s: s.update(chain_installs_trusted_self=3),
     "chain_installs_trusted_self 3 >"),
    # A refusal without a counted reason (e.g. a shadow destination).
    (lambda s: s.update(chain_skipped_trusted_to_trusted=1),
     "chain_skipped_trusted_to_trusted 1 != chain_skipped_trusted_poll_unverified + _unsealed 0"),
    # A counted reason without the refusal.
    (lambda s: s.update(chain_skipped_trusted_unsealed=1),
     "chain_skipped_trusted_to_trusted 0 != chain_skipped_trusted_poll_unverified + _unsealed 1"),
    (lambda s: s.update(chain_allowed_trusted_to_trusted=0, chain_installs_trusted_to_trusted=0,
                        chain_installs_trusted_self=0),
     "trusted_poll_entry_exits 1 without"),
    (lambda s: s.update(experiment={"no_trusted_to_trusted_chain": True}),
     "under no-trusted-to-trusted-chain"),
    (lambda s: s.update(trusted_poll_entries_verified=3),
     "regions_loaded 4"),
    (lambda s: s.update(experiment={"no_trusted_exit_chain": True}),
     "trusted_residual_slots_skipped 6 under no-trusted-exit-chain"),
])
def test_each_broken_revision4_invariant_is_one_problem(trusted, mutate, needle):
    stats = _revision4(trusted)
    mutate(stats)
    problems = v7_stats.counter_problems(stats)
    assert len(problems) == 1, problems
    assert needle in problems[0]


def test_allowed_t2t_needs_armed_slots(trusted):
    stats = _revision4(trusted)
    stats.update(trusted_residual_slots_armed=0, trusted_residual_exits_chainable=0,
                 trusted_residual_exits_unchainable=300)
    problems = v7_stats.counter_problems(stats)
    assert "chain_allowed_trusted_to_trusted 2 without armed residual slots" in problems


@pytest.mark.parametrize("field", ["chain_skipped_trusted_poll_unverified",
                                   "chain_skipped_trusted_unsealed"])
def test_each_refusal_reason_accounts_for_skipped_t2t(trusted, field):
    stats = _revision4(trusted)
    stats.update({field: 3}, chain_skipped_trusted_to_trusted=3,
                 chain_skipped_trusted_source=4)
    assert v7_stats.counter_problems(stats) == []


@pytest.mark.parametrize("key", v7_stats.T2T_COUNTER_KEYS + v7_stats.MEMBER_SKIP_COUNTER_KEYS)
def test_revision4_requires_both_key_groups(trusted, key):
    stats = _revision4(trusted)
    del stats[key]
    problems = v7_stats.counter_problems(stats)
    assert len(problems) == 1 and "missing" in problems[0] and key in problems[0]


def test_revision3_files_are_accepted_unchanged(trusted):
    stats = _revision3(trusted)
    assert not set(v7_stats.T2T_COUNTER_KEYS + v7_stats.MEMBER_SKIP_COUNTER_KEYS) & set(stats)
    assert v7_stats.counter_problems(stats) == []
    assert "trusted_residual_slots_skipped" not in v7_stats.summary(stats)


def test_token_arm_reproduces_revision3_counts(trusted):
    """no-trusted-to-trusted-chain: every T->T attempt refused, as in revision 3."""
    stats = _revision4(trusted)
    stats.update(experiment={"no_trusted_to_trusted_chain": True},
                 chain_allowed_trusted_to_trusted=0, chain_installs_trusted_to_trusted=0,
                 chain_installs_trusted_self=0, trusted_poll_entry_exits=0,
                 chain_allowed_trusted_source=13, chain_installs_trusted_source=13,
                 chain_skipped_trusted_source=3, chain_skipped_trusted_to_trusted=2)
    assert v7_stats.counter_problems(stats) == []
    stats.update(chain_skipped_trusted_poll_unverified=2)
    assert "under no-trusted-to-trusted-chain" in v7_stats.counter_problems(stats)[0]


def test_unchained_arm_arms_and_skips_nothing(trusted):
    """no-trusted-exit-chain: no slot is armed, so no skip bit is counted."""
    stats = _revision4(trusted)
    stats.update(experiment={"no_trusted_exit_chain": True},
                 trusted_residual_slots_armed=0, trusted_residual_slots_skipped=0,
                 trusted_residual_exits_chainable=0, trusted_residual_exits_unchainable=300,
                 chain_allowed_trusted_source=0, chain_installs_trusted_source=0,
                 benchmark_chain_installs_trusted_source=0,
                 chain_allowed_trusted_to_trusted=0, chain_installs_trusted_to_trusted=0,
                 chain_installs_trusted_self=0, trusted_poll_entry_exits=0)
    assert v7_stats.counter_problems(stats) == []


def test_unloaded_revision4_file_with_new_counts():
    stats = _unloaded()
    stats.update(counters_revision=4,
                 trusted_residual_exits_chainable=0, trusted_residual_exits_unchainable=0,
                 trusted_residual_slots_armed=0, chain_allowed_trusted_source=0,
                 chain_skipped_trusted_to_trusted=0,
                 **{k: 0 for k in v7_stats.T2T_COUNTER_KEYS + v7_stats.MEMBER_SKIP_COUNTER_KEYS})
    assert v7_stats.counter_problems(stats) == []
    stats["trusted_poll_entries_verified"] = 1
    stats["trusted_residual_slots_skipped"] = 2
    assert v7_stats.counter_problems(stats) == [
        "trusted_poll_entries_verified 1 without a loaded trusted directory",
        "trusted_residual_slots_skipped 2 without a loaded trusted directory"]


# -- per-build counter bounds (armed / skipped residual slots) -------------

def test_per_build_bounds_every_region_built_once_is_exact():
    assert v7_stats.per_build_bounds([3, 0, 5, 1], 4, rebuilt=False) == (9, 9)
    assert v7_stats.per_build_bounds([3, 0, 5, 1], 4, built={2, 0}, rebuilt=False) == (9, 9)
    assert v7_stats.per_build_bounds([3, 0, 5, 1], 0, rebuilt=False) == (0, 0)


def test_per_build_bounds_some_regions_built():
    # Two of four regions built once: the two smallest / largest others.
    assert v7_stats.per_build_bounds([3, 0, 5, 1], 2, rebuilt=False) == (1, 8)
    # Region 2 is known to be built; one more build of the others.
    assert v7_stats.per_build_bounds([3, 0, 5, 1], 2, built=[2], rebuilt=False) == (5, 8)


def test_per_build_bounds_with_rebuilds():
    # A tb_flush rebuild counts again: builds can exceed the region count,
    # and an all-regions build count no longer pins the sum.
    assert v7_stats.per_build_bounds([3, 0, 5, 1], 4) == (0, 20)
    assert v7_stats.per_build_bounds([3, 0, 5, 1], 6, built={0, 1, 2, 3}) == (9, 19)
    assert v7_stats.per_build_bounds([2, 2], 5, built=[0]) == (10, 10)


@pytest.mark.parametrize(("per_region", "builds", "built", "rebuilt", "needle"), [
    ([1, 2], 3, (), False, "3 builds of 2 regions without a rebuild"),
    ([1, 2], 1, (0, 1), True, "2 regions built in 1 builds"),
    ([1, 2], 2, (2,), True, "outside 2 regions"),
])
def test_per_build_bounds_rejects_inconsistent_counts(per_region, builds, built, rebuilt,
                                                      needle):
    with pytest.raises(ValueError, match=needle):
        v7_stats.per_build_bounds(per_region, builds, built=built, rebuilt=rebuilt)
