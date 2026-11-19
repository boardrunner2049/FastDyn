from dataclasses import replace
import json
import subprocess
import sys

import pytest

from scopt.analysis import profitability
from scopt.canon import canonical_json, sha256_id
from scopt.manager import PassManager, PassRegistry, default_pass_registry
from scopt.manager_b3 import B3Env
from scopt.passes.base import Outcome, PassResult
from tests.synth.fake_snapshot import fake_snapshot

NAMES = ("chain-returns", "chain-static", "specialize-returns", "specialize-edges", "identity")


def _file(path, **costs):
    path.write_text(json.dumps({"constants": {name: {"value_ns": value} for name, value in costs.items()}}))
    return path


def _manager(snap, *, env=None, pipeline_params=None, caller=None, only=None):
    registry = default_pass_registry()
    seen = {}
    classes = []
    for name in NAMES:
        base = registry.get(name, 1).descriptor
        descriptor = replace(base, requires=(), requires_profile_phase=None,
                             capabilities=frozenset(), requires_capture_level=0)

        def run(self, view, ctx, edits):
            seen[self.descriptor.name] = dict(ctx.params)
            return PassResult(Outcome.NO_CHANGE, "record_params")

        classes.append(type("ObservedPass", (), {"descriptor": descriptor, "run": run}))
    pipeline = {"name": "calibration-test", "pass": [
        {"name": n, "version": 1, "params": (pipeline_params or {}).get(n, {})} for n in NAMES]}
    return PassManager(snap, pipeline, params=caller, only=only, b3=env,
                       passes=PassRegistry(classes)), seen


def test_exported_calibration_path_is_an_import_time_hint(tmp_path, monkeypatch):
    path = _file(tmp_path / "calibration.json", c_guard=3)
    monkeypatch.setenv("SCOPT_B3_CALIBRATION", str(path))
    value = subprocess.check_output(
        [sys.executable, "-c",
         "from scopt.analysis.profitability import CALIBRATION_PATH; print(CALIBRATION_PATH)"],
        text=True,
    )
    assert value.strip() == str(path)


def test_calibration_is_frozen_and_applied_before_explicit_parameters(tmp_path, monkeypatch):
    base = fake_snapshot(2)
    src = _file(tmp_path / "first.json", c_lookup=14, c_guard=4, c_fail=2, c_link=9)
    monkeypatch.setenv("SCOPT_B3_CALIBRATION", str(src))
    loads = []
    load = profitability.load_calibration

    def recorded_load():
        loads.append(None)
        return load()

    monkeypatch.setattr(profitability, "load_calibration", recorded_load)
    manager, observed = _manager(base, env=B3Env(calibration={"c_fail": 0, "c_link": 8}),
                                 pipeline_params={"chain-returns": {"c_guard": 3},
                                                  "specialize-returns": {"c_guard": 2}},
                                 caller={"chain-returns": {"c_guard": 6}})
    monkeypatch.setenv("SCOPT_B3_CALIBRATION", str(_file(tmp_path / "second.json", c_lookup=99)))
    src.unlink()
    candidate = manager.execute()
    assert len(loads) == 1
    expected = {
        "chain-returns": {"c_lookup": 14.0, "c_guard": 6, "c_link": 8.0},
        "chain-static": {"c_lookup": 14.0, "c_guard": 4.0, "c_link": 8.0},
        "specialize-returns": {"c_lookup": 14.0, "c_guard": 2},
        "specialize-edges": {"c_lookup": 14.0, "c_guard": 4.0},
        "identity": {},
    }
    assert observed == expected
    for p in candidate.passes:
        assert p.params == expected[p.name]
        assert p.params_sha256 == sha256_id(canonical_json(expected[p.name]))
    sidecars = {row["pass"]: row for row in candidate.pipeline_provenance()}
    assert sidecars["chain-returns"]["x_calibrated_costs"] == {"c_lookup": 14.0, "c_link": 8.0}
    assert sidecars["specialize-returns"]["x_calibrated_costs"] == {"c_lookup": 14.0}
    assert "x_calibrated_costs" not in sidecars["identity"]
    fresh, _ = _manager(base, env=B3Env())
    assert fresh.execute().outcome("chain-returns").params == {"c_lookup": 99.0}
    assert len(loads) == 2


def test_legacy_recipe_ids_when_calibration_has_no_effect(tmp_path, monkeypatch):
    base = fake_snapshot(2)
    monkeypatch.delenv("SCOPT_B3_CALIBRATION", raising=False)
    monkeypatch.setenv("FASTDYN_ROOT", str(tmp_path))
    missing, _ = _manager(base, env=B3Env())
    legacy = missing.execute()
    calibration = tmp_path / "runs/b3-explore/wp16/calibration.json"
    calibration.parent.mkdir(parents=True)
    _file(calibration, c_lookup=7.6, c_guard=1.5, c_fail=0, c_link=0)
    equivalent, _ = _manager(base, env=B3Env())
    unchanged = equivalent.execute()
    assert unchanged.candidate_id == legacy.candidate_id
    assert all(not p.params for p in unchanged.passes)
    _file(calibration, c_lookup=1)
    workspace, _ = _manager(base, env=B3Env())
    assert workspace.execute().candidate_id != legacy.candidate_id
    overrides = {name: {"c_lookup": 12} for name in NAMES[:-1]}
    explicit, _ = _manager(base, env=B3Env(), caller=overrides)
    only_identity, _ = _manager(base, env=B3Env(), only="identity")
    ordinary, _ = _manager(base)
    calibration.unlink()
    no_file, _ = _manager(base, env=B3Env(), caller=overrides)
    control_only, _ = _manager(base, env=B3Env(), only="identity")
    control_ordinary, _ = _manager(base)
    assert explicit.execute().candidate_id == no_file.execute().candidate_id
    assert only_identity.execute().candidate_id == control_only.execute().candidate_id
    assert ordinary.execute().candidate_id == control_ordinary.execute().candidate_id


@pytest.mark.parametrize("text", ("{broken", "[]", "null", '{"constants": []}'))
def test_unreadable_or_malformed_calibration_gets_defaults(tmp_path, text):
    path = tmp_path / "costs.json"
    assert profitability.load_calibration(path) == profitability.DEFAULTS
    path.write_text(text)
    assert profitability.load_calibration(path) == profitability.DEFAULTS


def test_validation_is_per_cost_and_respects_finite_failure_interval(tmp_path):
    path = _file(tmp_path / "costs.json", c_lookup=float("inf"), c_guard=-1, c_fail=True, c_link=7)
    assert profitability.load_calibration(path) == {**profitability.DEFAULTS, "c_link": 7}
    path.write_text('{"constants":{"c_lookup": {}, "c_guard": true, "c_fail":{"value_ns":6,'
                    '"ci95_ns":[-2, 8]}, "c_link":{"value_ns": -5}}}')
    assert profitability.load_calibration(path) == profitability.DEFAULTS
    path.write_text('{"constants":{"c_fail":{"value_ns":6,"ci95_ns":["bad", 7]},'
                    '"c_link":{"value_ns": NaN}}}')
    assert profitability.load_calibration(path) == {**profitability.DEFAULTS, "c_fail": 6}
    path.write_text('{"constants":{"c_lookup":{"value_ns":' + "1" + "0" * 320 + '}}}')
    assert profitability.load_calibration(path) == profitability.DEFAULTS
