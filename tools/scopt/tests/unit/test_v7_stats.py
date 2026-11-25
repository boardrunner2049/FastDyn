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
    assert v7_stats.COUNTERS_REVISION == 3
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
