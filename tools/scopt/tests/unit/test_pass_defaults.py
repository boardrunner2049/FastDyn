import pytest

from scopt.manager import DEFAULT_DISABLED_PASSES, PassManager, PipelineError, R, default_pass_registry
from scopt.manager_b3 import B3Env
from scopt.passes.base import Outcome
from tests.synth.fake_snapshot import fake_snapshot


RANKS_5_TO_11 = (
    "local-scalar-cleanup", "retire-ordinary-exits", "eliminate-state-sync",
    "specialize-returns", "chain-returns", "specialize-edges", "chain-static",
)


def test_ranked_defaults_skip_before_admission_or_calibration(monkeypatch):
    def unexpected_calibration(*args):
        pytest.fail("a disabled pass requested calibration")

    monkeypatch.setattr("scopt.analysis.profitability.calibration_params", unexpected_calibration)
    names = (*RANKS_5_TO_11, "identity")
    pipeline = {"name": "custom", "pass": [{"name": n, "version": 1} for n in names]}
    snapshot = fake_snapshot(2)
    candidate = PassManager(snapshot, pipeline, b3=B3Env()).execute()
    assert DEFAULT_DISABLED_PASSES == set(RANKS_5_TO_11)
    assert set(names) <= set(default_pass_registry().names())
    assert not candidate.edits
    assert candidate.ablation["disable"] == sorted(RANKS_5_TO_11)
    for name in RANKS_5_TO_11:
        result = candidate.outcome(name)
        assert (result.outcome, result.reason, result.calibrated_costs) == (
            Outcome.SKIPPED_UNSUPPORTED, R.DISABLED_BY_DEFAULT, {})
    assert candidate.outcome("identity").outcome == Outcome.NO_CHANGE


def test_only_and_params_do_not_enable_a_disabled_pass():
    name = "chain-static"
    snapshot = fake_snapshot(2)
    manager = PassManager(snapshot, "b3", params={name: {}}, only=name)
    assert manager.execute().outcome(name).reason == R.DISABLED_BY_DEFAULT
    enabled = PassManager(snapshot, "b3", enable=[name], only=name).execute()
    assert enabled.outcome(name).reason != R.DISABLED_BY_DEFAULT
    assert name not in enabled.ablation["disable"]
    assert enabled.outcome("chain-returns").outcome == Outcome.SKIPPED_UNSUPPORTED


def test_invalid_pass_options_are_reported():
    snapshot = fake_snapshot(2)
    with pytest.raises(PipelineError, match="both enabled and disabled"):
        PassManager(snapshot, "b3", enable=["chain-static"], disable=["chain-static"])
    with pytest.raises(PipelineError, match="not in pipeline"):
        PassManager(snapshot, "b3", enable=["typo"])
