"""Mocked trial measurements exercise decisions only. No performance workload or result."""
import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
pytest.importorskip("scopt")
from joint_selection import InvalidObservation, select, validate_observations


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
