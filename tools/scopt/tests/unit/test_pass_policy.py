"""Replay and ablation drivers must request opt-in passes and notice a policy skip."""
import pytest

from scopt.manager import DEFAULT_DISABLED_PASSES, R, load_pipeline
from scopt.run.pass_policy import (EDITED, NO_CHANGE, POLICY_DISABLED, UNSUPPORTED, PolicyMismatch,
                                   check_requested, effective_configuration, policy_arguments)


def receipt(name, reason="", n_edits=0, outcome="no_change"):
    return {"pass": name, "outcome": outcome, "reason": reason, "n_edits": n_edits}


def test_arguments_enable_every_opt_in_stage_of_the_arm_and_disable_the_rest():
    names = [entry.name for entry in load_pipeline("b3").passes]
    full = policy_arguments(names, names)
    assert "--disable" not in full
    assert sorted(full[1::2]) == sorted(set(names) & DEFAULT_DISABLED_PASSES)
    assert {"specialize-returns", "chain-returns", "chain-static"} <= set(full[1::2])

    without = policy_arguments(names, [n for n in names if n != "chain-static"])
    assert without[without.index("--disable") + 1] == "chain-static"
    assert "chain-static" not in [without[i + 1] for i, a in enumerate(without) if a == "--enable"]


def test_arguments_reject_a_stage_that_is_not_in_the_pipeline():
    with pytest.raises(ValueError, match="not in the pipeline"):
        policy_arguments(["a"], ["b"])


def test_statuses_separate_policy_unsupported_no_change_and_edits():
    result = {"passes": [
        receipt("specialize-edges", R.DISABLED_BY_DEFAULT, outcome="skipped_unsupported"),
        receipt("order-cold", R.ABLATION_DISABLED, outcome="skipped_unsupported"),
        receipt("llvm-cache-layout", R.PASS_NOT_REGISTERED, outcome="skipped_unsupported"),
        receipt("specialize-returns", "target_not_profiled", outcome="skipped_unsupported"),
        receipt("constant-propagation", "no_variants"),
        receipt("chain-static", n_edits=233, outcome="candidate"),
    ]}
    assert [row["status"] for row in effective_configuration(result)] == [
        POLICY_DISABLED, POLICY_DISABLED, UNSUPPORTED, NO_CHANGE, NO_CHANGE, EDITED]


def test_a_requested_stage_skipped_by_policy_is_an_error():
    result = {"passes": [receipt("local-scalar-cleanup", R.DISABLED_BY_DEFAULT),
                         receipt("constant-propagation", "no_variants")]}
    with pytest.raises(PolicyMismatch, match="local-scalar-cleanup: policy_disabled"):
        check_requested(result, ["local-scalar-cleanup", "constant-propagation"])


def test_a_requested_stage_that_ran_and_changed_nothing_is_accepted():
    result = {"passes": [receipt("constant-propagation", "no_variants"),
                         receipt("order-cold", R.ABLATION_DISABLED)]}
    rows = check_requested(result, ["constant-propagation"])
    assert [(row["pass"], row["status"]) for row in rows] == [
        ("constant-propagation", NO_CHANGE), ("order-cold", POLICY_DISABLED)]


def test_a_requested_stage_missing_from_the_result_is_an_error():
    with pytest.raises(PolicyMismatch, match="chain-static: not in the result"):
        check_requested({"passes": []}, ["chain-static"])
