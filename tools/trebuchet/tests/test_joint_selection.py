"""Mocked trial measurements exercise decisions only. No performance workload or result."""
import copy
import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
pytest.importorskip("scopt")
from joint_selection import InvalidObservation, select, validate_observations
from scopt.ids import canonical_json, sha256_id


def example():
    objective = {"name": "operation", "direction": "minimize", "unit": "ns",
                 "useful_work": "one completed test operation", "boundaries": "request through response"}
    registry = {"schema": "trebuchet.registry/1", "state": "completed", "application": "test-only",
                "configuration": "test-only", "objective": objective, "budget": {"candidate_limit": 8}, "candidates": []}
    plan = dict(application=registry["application"], configuration=registry["configuration"], objective=objective,
                seed=7, alpha=.05, minimum_gain=1.02, warmup_pairs=1,
                pairs=dict(ranking=12, confirmation=12, promotion=12), candidate_limit=8,
                fallback_identity="original/warm", incumbent_id="original/warm", incumbent_identity="original/warm",
                images={}, scenarios={k: [dict(id=k, weight=1, bank_inputs={"data": {"sha256": k}})]
                                      for k in ("training", "ranking", "confirmation", "promotion", "final")})
    for image in ("original", "firmbolt"):
        binding = {"firmware_sha256": "elf-" + image, "config_sha256": "cfg-" + image,
                   "profile_id": "prof-" + image}
        plan["images"][image] = binding
        for stage in ("warm", "b3"):
            name = image + "/" + stage
            recipe = [] if stage == "warm" else [stage]
            registry["candidates"].append(dict(binding, id=name, status="ready", image_variant=image,
                                               deployment_identity=name, application="test-only", recipe=recipe,
                                               generations=[{"stage": s} for s in recipe]))
    return registry, plan


def parameterized_example():
    from candidates import recipe_invocations, resolved_recipe

    registry, plan = example()
    row = registry["candidates"][1]
    recipe = "partial+sfb+b3"
    nodes = resolved_recipe({}, recipe, {"partial": {"stage": "native", "params": {
        "host-inline": {"mode": "splice", "allow_faulting": True, "udiv_mode": "partial-cleanup"}}}})
    row.update(id="original/" + recipe, deployment_identity="original/" + recipe,
               recipe=[node["stage"] for node in nodes], recipe_nodes=nodes,
               recipe_identity=sha256_id(canonical_json(recipe_invocations(nodes))),
               generations=[{"stage": node["stage"], "params": copy.deepcopy(node["params"]),
                             "input": f"snapshot-{i}", "output": f"snapshot-{i + 1}",
                             "receipt": f"generation-{i}/stdout.json", "effective_passes": []}
                            for i, node in enumerate(nodes)])
    # New materializations also attach metadata to ordinary warm, whose sequence
    # is empty. The other rows deliberately retain the legacy format.
    registry["candidates"][0].update(recipe_nodes=[], recipe_identity=sha256_id(canonical_json([])))
    return registry, plan


def refresh_node_identities(row):
    invocations = []
    for node in row["recipe_nodes"]:
        invocation = {"stage": node["stage"], "params": node["params"]}
        node["identity"] = sha256_id(canonical_json(invocation))
        invocations.append(invocation)
    row["recipe_identity"] = sha256_id(canonical_json(invocations))


def test_named_recipe_and_legacy_rows_share_selection(tmp_path):
    registry, plan = parameterized_example()
    candidate = registry["candidates"][1]
    observed = []

    def trial(row, case, path):
        observed.append(row["id"])
        return {"value": 80 if row["id"] == candidate["id"] else 100}

    receipt = select(registry, plan, trial, tmp_path / "result")
    assert observed and receipt["selected"]["id"] == "original/partial+sfb+b3"
    assert receipt["selected"]["recipe"] == ["native", "sfb", "b3"]


@pytest.mark.parametrize("tamper", [
    "self-consistent-node-params", "generation-params", "boolean-integer-params", "generation-params-missing",
    "self-consistent-node-stage", "node-identity", "recipe-identity", "node-count",
    "nodes-missing", "identity-missing", "nonfinite-params",
])
def test_named_recipe_metadata_rejected_before_trials(tmp_path, tamper):
    registry, plan = parameterized_example()
    row = registry["candidates"][1]
    node = row["recipe_nodes"][0]
    generation = row["generations"][0]
    if tamper == "self-consistent-node-params":
        node["params"]["host-inline"]["udiv_mode"] = "guarded"
        refresh_node_identities(row)
    elif tamper == "generation-params":
        generation["params"]["host-inline"]["udiv_mode"] = "guarded"
    elif tamper == "boolean-integer-params":
        # Python's dictionary equality treats True and 1 as equal; canonical
        # parameter identity must preserve their distinct JSON types.
        generation["params"]["host-inline"]["allow_faulting"] = 1
    elif tamper == "generation-params-missing":
        del generation["params"]
    elif tamper == "self-consistent-node-stage":
        node["stage"] = "general"
        refresh_node_identities(row)
    elif tamper == "node-identity":
        node["identity"] = "sha256:" + "0" * 64
    elif tamper == "recipe-identity":
        row["recipe_identity"] = "sha256:" + "0" * 64
    elif tamper == "node-count":
        row["recipe_nodes"].pop()
    elif tamper == "nodes-missing":
        del row["recipe_nodes"]
    elif tamper == "identity-missing":
        del row["recipe_identity"]
    else:
        node["params"]["host-inline"]["max_sites"] = float("nan")
    with pytest.raises(ValueError, match="recipe|not JSON compliant"):
        select(registry, plan, lambda *args: pytest.fail("unexpected trial"), tmp_path / "result")


def test_external_incumbent_named_metadata_is_checked(tmp_path):
    registry, plan = parameterized_example()
    registry["contract_sha256"] = "same-operation"
    old = copy.deepcopy(registry)
    incumbent = old["candidates"][1]
    plan.update(incumbent_id=incumbent["id"], incumbent_identity="historical-deployment",
                incumbent_image=dict(plan["images"]["original"]))
    incumbent["deployment_identity"] = plan["incumbent_identity"]
    incumbent["recipe_nodes"][0]["params"]["host-inline"]["udiv_mode"] = "guarded"
    refresh_node_identities(incumbent)
    with pytest.raises(ValueError, match="recipe node parameters differ"):
        select(registry, plan, lambda *args: pytest.fail("unexpected trial"), tmp_path / "result", old)


@pytest.mark.parametrize("ranking,confirmation,expected", [
    # FirmBOLT+b3 gains 44% over FirmBOLT warm, yet original+b3 finishes sooner.
    ([100, 80, 170, 95], 80, "original/b3"),
    ([100, 80, 110, 70], 70, "firmbolt/b3"),
    ([100, 80, 110, 70], 99, "original/warm"),
])
def test_joint_choice(tmp_path, ranking, confirmation, expected):
    registry, plan = example()
    names = [row["id"] for row in registry["candidates"]]
    times = dict(zip(names, ranking))
    alias = dict(registry["candidates"][1], id="original/b3-alias")
    registry["candidates"].append(alias)
    def mock_trial(row, case, path):
        assert row["id"] != alias["id"]
        value = times[row["id"]]
        if row["id"] != "original/warm" and case["id"] == "confirmation":
            value = confirmation
        return {"value": value}

    out = tmp_path / "selection"
    receipt = select(registry, plan, mock_trial, out)
    assert receipt["selected"]["id"] == expected
    assert receipt["selected"]["deployment_identity"] == expected
    assert receipt["state"] == "completed"
    winner = receipt["ranking_winner"]["id"]
    assert json.loads((out / "frozen.json").read_text())["deployment"]["id"] == winner
    observations = [json.loads(line) for line in (out / "observations.jsonl").read_text().splitlines()]
    assert not any(r["case"] in ("training", "final") for r in observations)
    assert set(r["candidate"] for r in observations if r["phase"] == "confirmation") == {winner, "original/warm"}
    assert all(row["candidate_first"] == 6 for row in receipt["confirmation"].values())


@pytest.mark.parametrize("key", ["profile_id", "firmware_sha256", "config_sha256"])
def test_wrong_per_image_binding(tmp_path, key):
    registry, plan = example()
    registry["candidates"][2][key] = registry["candidates"][0][key]
    with pytest.raises(ValueError, match="mismatch"):
        select(registry, plan, lambda *args: pytest.fail("unexpected trial"), tmp_path / "result")


def test_missing_stage_rejected(tmp_path):
    registry, plan = example()
    registry["candidates"][1]["generations"] = []
    with pytest.raises(ValueError, match="stage receipts"):
        select(registry, plan, lambda *args: pytest.fail("unexpected trial"), tmp_path / "result")


def test_fast_selections_can_lose_promotion(tmp_path):
    registry, plan = example()
    plan.update(incumbent_id="firmbolt/warm", incumbent_identity="firmbolt/warm")
    def mock_trial(row, case, path):
        if case["id"] == "promotion" and row["id"] == "firmbolt/warm":
            return {"value": 50}
        return {"value": 70 if row["id"] == "original/b3" else 100}
    receipt = select(registry, plan, mock_trial, tmp_path / "result")
    assert receipt["selected"]["id"] == "original/b3"
    assert receipt["promoted"] is False


def test_application_tolerances():
    validate_observations({"command": "stop", "position": 5.0}, {"position": 5.01, "command": "stop"},
                          {"position": 0.02})
    with pytest.raises(ValueError, match="oracle mismatch"):
        validate_observations({"command": 1}, {"command": True}, {})


def test_confirmation_oracle_failure_keeps_exact_reference(tmp_path):
    registry, plan = example()
    def mock_trial(row, case, path):
        if row["id"] == "original/b3" and case["id"] == "confirmation":
            raise InvalidObservation("wrong job completion count")
        return {"value": 50 if row["id"] == "original/b3" else 100}
    out = tmp_path / "selection"
    report = select(registry, plan, mock_trial, out)
    assert report["confirmation"]["rejected"] == "wrong job completion count"
    assert report["selected"] == registry["candidates"][0]
    assert "wrong job completion count" in (out / "observations.jsonl").read_text()


def test_larger_is_better_fractional_throughput(tmp_path):
    registry, plan = example()
    plan["objective"].update(direction="maximize", name="throughput", unit="work_units/s")
    def mock_trial(row, case, path):
        return {"value": 0.7 if row["id"] == "firmbolt/b3" else 0.5}
    report = select(registry, plan, mock_trial, tmp_path / "selection")
    assert report["selected"]["id"] == "firmbolt/b3" and report["promoted"]


def test_old_incumbent_can_have_same_recipe_name_and_different_identity(tmp_path):
    import copy
    registry, plan = example()
    registry["contract_sha256"] = "same-operation"
    old = copy.deepcopy(registry)
    old["candidates"][0]["deployment_identity"] = plan["incumbent_identity"] = "old-id"
    plan["incumbent_image"] = dict(plan["images"]["original"])
    def mock_trial(row, case, path):
        if row["deployment_identity"] == "old-id":
            return {"value": 60}
        return {"value": 80 if row["id"] == "original/b3" else 100}
    report = select(registry, plan, mock_trial, tmp_path / "selection", old)
    assert report["incumbent"]["deployment_identity"] == "old-id"
    assert report["selected"]["id"] == "original/b3" and not report["promoted"]


@pytest.mark.parametrize("secondary", ["qemu_lifetime_ns", "guest_window_ns"])
def test_final_blocks_use_reserved_scenario_and_deduplicate_same_reference(tmp_path, secondary):
    from final_evaluation import evaluate
    registry, plan = example()
    plan.update(family="test-only", final_seed=13, secondary_measurement=secondary)
    plan["pairs"]["final"] = 12

    def trial(row, case, path):
        v = 90 if row["id"] == "original/b3" else 100
        return {"value": v, "wall_s": v / 100, secondary: v}

    receipt = select(registry, plan, trial, tmp_path / "selection")
    report = evaluate(plan, registry, receipt, None, trial, tmp_path / "final")
    assert report["state"] == "completed"
    assert report["aliases"] == dict(ordinary="ordinary", incumbent="ordinary", selected="selected")
    records = [json.loads(s) for s in (tmp_path / "final/observations.jsonl").read_text().splitlines()]
    assert len(records) == 26 and set(r["case"] for r in records) == {"final"}
    assert report["scenarios"]["final"]["blocks"] == 12
    assert report["scenarios"]["final"]["comparisons"]["incumbent"]["value"]["median_gain"] == 100/90


def test_report_refuses_incomplete_and_emits_no_blocked_mean():
    from final_evaluation import table
    blocked = {"applications": [{"application": "test-only", "status": "blocked", "reason": "fixture"}]}
    with pytest.raises(ValueError, match="incomplete"):
        table(dict(blocked, state="running"))
    assert "geometric mean" not in table(dict(blocked, state="blocked"))


def test_absent_incumbent_retains_ordinary_diagnostic_only(tmp_path):
    from final_evaluation import evaluate, table
    from scopt.run.launcher import file_sha256
    registry, plan = example()
    plan.update(family="test-only", final_seed=1, promotion_enabled=False)
    plan.pop("incumbent_identity")
    plan.pop("incumbent_id")
    plan["pairs"]["final"] = 12
    def trial(row, case, root):
        assert case["id"] != "promotion"
        return {"value": 100, "guest_window_ns": 80, "wall_s": .2}
    choice = select(registry, plan, trial, tmp_path / "selection")
    assert choice["incumbent"] is None and choice["promoted"] is None
    report = evaluate(plan, registry, choice, None, trial, tmp_path / "final")
    assert set(report["scenarios"]["final"]["comparisons"]) == {"ordinary"}
    def pin(path):
        return {"path": str(path), "sha256": file_sha256(path)}
    rendered = table({"state": "completed", "applications": [
        {"application": registry["application"], "status": "admitted", "family": "test-only", "weight": 1,
         "final": pin(tmp_path / "final/final.json"), "protocol": pin(tmp_path / "selection/plan.json")}]})
    assert "ordinary-only diagnostic" in rendered and "geometric mean" not in rendered
