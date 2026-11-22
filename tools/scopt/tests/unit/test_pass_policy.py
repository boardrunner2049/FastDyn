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


@pytest.mark.parametrize(("outcome", "reason", "status"), [
    ("skipped_unknown", "missing_profile", "missing"),
    ("skipped_unsupported", "native_bridge_unavailable", "missing"),
    ("skipped_unsupported", "guard_layout_unbound", "unsupported"),
    ("failed", "exception", "failed"),
    ("budget_exhausted", "budget_exhausted", "incomplete"),
    ("skipped_unknown", "validation_inconclusive", "incomplete"),
    (None, "", "incomplete"),
])
def test_unavailable_or_incomplete_stage_cannot_qualify(outcome, reason, status):
    result = {"passes": [receipt("stage", reason, outcome=outcome)]}
    with pytest.raises(PolicyMismatch, match=f"stage: {status}"):
        check_requested(result, ["stage"])


def test_scan_that_rejects_all_sites_is_complete():
    from collections import Counter
    from scopt.passes._b3util import refusal_result

    refusal = refusal_result(Counter(flags_live=3), nothing="no_foldable_load")
    row = receipt("constant-propagation", refusal.reason, outcome=refusal.outcome.value)
    row["stats"] = refusal.stats
    assert check_requested({"passes": [row]}, ["constant-propagation"])[0]["status"] == NO_CHANGE


@pytest.mark.parametrize("actual", [["second", "first"], ["first", "first", "second"]])
def test_requested_stages_must_match_the_plan(actual):
    result = {"passes": [receipt(name) for name in actual]}
    with pytest.raises(PolicyMismatch, match="expected pipeline stages"):
        check_requested(result, ["first", "second"], stages=["first", "second"])


def test_disabled_native_prefix_may_be_omitted():
    result = {"passes": [receipt("second")]}
    check_requested(result, ["second"], stages=["host-inline", "second"])


@pytest.mark.parametrize("arm,omit_stage", [("identity", False), ("b3", False), ("b3", True)])
def test_arm_driver_argument_boundary_and_receipts(tmp_path, arm, omit_stage):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[4]
    (tmp_path / "baseline.json").write_text(json.dumps({"snapshot": "test snapshot"}))
    names = [stage.name for stage in load_pipeline(arm).passes]
    passes = names[:-1] if omit_stage else names
    doc = {"passes": [receipt(name) for name in passes], "artifact_dir": "test artifact"}
    # Substitute only the optimizer and validator. The driver still loads the
    # actual pipeline, serializes options in Bash and checks the actual receipts.
    stub = tmp_path / "scopt"
    stub.write_text("#!/bin/sh\nif [ \"$1\" = optimize ]; then cat \"$TEST_RESULT\"; fi\n")
    stub.chmod(0o755)
    response = tmp_path / "response.json"
    response.write_text(json.dumps(doc))
    env = dict(os.environ, SCOPT_RUNTIME_QEMU=sys.executable, SCOPT_HOST_INLINE_BRIDGE="unused",
               TEST_RESULT=str(response),
               PATH=os.pathsep.join([str(tmp_path), str(Path(sys.executable).parent), os.environ["PATH"]]))
    dest = tmp_path / "output"
    run = subprocess.run(["bash", "tools/scopt/scripts/run_program_arm.sh", str(tmp_path), arm, str(dest)],
                         cwd=root, env=env, capture_output=True, text=True)
    command = json.loads((dest / "offline.json").read_text())["command"]
    assert command[2] == "test snapshot"
    assert "" not in command
    flags = [command[i + 1] for i, flag in enumerate(command) if flag == "--enable"]
    assert flags == [name for name in names if name in DEFAULT_DISABLED_PASSES]
    if omit_stage:
        assert run.returncode != 0
        assert f"{names[-1]}: not in the result" in run.stderr
        assert not (dest / "v10.log").exists()
    else:
        assert run.returncode == 0, run.stderr
        assert [r["pass"] for r in json.loads((dest / "effective.json").read_text())] == names
