import itertools
import json
import random
from pathlib import Path

import pytest

from scopt.run import warm_selection as gate


@pytest.fixture(autouse=True)
def simulated_lock(monkeypatch):
    monkeypatch.setattr(gate.fcntl, "flock", lambda *a: None)


def _trial_result(elapsed):
    return {"elapsed_ns": elapsed, "qemu_sha256": "sha256:runtime", "ram_storage": "tmpfs-private-file",
            "bundle_id": "bundle", "runtime_identity_id": "runtime"}


def _fixture(tmp_path):
    artifacts = {}
    for arm in gate.ARMS:
        path = tmp_path / arm
        path.mkdir()
        (path / "cache.sc").write_bytes(arm.encode())
        for name in ("bundle.json", "runtime_identity.json"):
            (path / name).write_text("{}")
        artifacts[arm] = path
    training = tmp_path / "train"
    training.write_bytes(b"train")
    config = tmp_path / "config.toml"
    config.touch()
    cases = []
    for i in range(2):
        data = tmp_path / f"timing-{i}"
        data.write_bytes(f"timing {i}".encode())
        guest = tmp_path / f"guest-{i}"
        guest.write_text(f"guest {i}\n")
        cases.append(gate.TimingInput(f"input-{i}", config, {"input": data}, guest))
    return (artifacts["warm"], artifacts["candidate"], tmp_path / "evidence"), {
        "inputs": cases, "training_inputs": [training],
    }


def test_equal_sparse_bytes_skip_launch_even_without_timing_inputs(tmp_path, monkeypatch):
    warm, candidate, out = [tmp_path / p for p in ("warm.sc", "candidate.sc", "evidence")]
    with warm.open("wb") as cache:
        cache.write(b"cache")
        cache.truncate(8192)
    candidate.write_bytes(warm.read_bytes())
    monkeypatch.setattr(gate, "_trial", lambda *a, **kw: pytest.fail("unexpected launch"))
    result = gate.select_warm_cache(warm, candidate, out, inputs=(), training_inputs=())
    assert result["status"] == "ok" and result["selected_artifact"] == str(warm)
    assert result["reason"] == "identical_cache_bytes"
    assert not (out / "samples.jsonl").exists()


def test_each_input_must_support_a_win(tmp_path, monkeypatch):
    args, kwargs = _fixture(tmp_path)
    invocations = []

    def fake_trial(artifact, case, path, **kw):
        invocations.append(path)
        if Path(artifact).name == "candidate" and case.guest_lines == ["guest 0"]:
            return _trial_result(80)
        return _trial_result(100)

    monkeypatch.setattr(gate, "_trial", fake_trial)
    result = gate.select_warm_cache(*args, **kwargs)
    assert result["reason"] == "insufficient_speedup_evidence"
    assert result["selected_artifact"] == str(args[0])
    assert result["inputs"]["input-0"]["passes"]
    assert result["inputs"]["input-0"]["p_value_numerator"] == 1
    assert result["inputs"]["input-1"]["ties"] == 12
    assert result["inputs"]["input-1"]["p_value"] == 1
    assert len(invocations) == len(set(invocations)) == 52
    assert len({len(path.name) for path in invocations}) == 1
    plan = json.loads((args[2] / "plan.json").read_text())
    for orders in plan["orders"].values():
        assert len(orders) == 12
        assert sum(order[0] == "candidate" for order in orders) == 6


def test_all_inputs_can_select_candidate(tmp_path, monkeypatch):
    args, kwargs = _fixture(tmp_path)
    monkeypatch.setattr(gate, "_trial", lambda artifact, *a, **kw:
                        _trial_result(90 if Path(artifact).name == "candidate" else 100))
    result = gate.select_warm_cache(*args, **kwargs)
    assert result["reason"] == "speedup_established"
    assert result["selected_artifact"] == str(args[1])


def test_stricter_alpha_applies_to_exact_p_value(tmp_path, monkeypatch):
    args, kwargs = _fixture(tmp_path)
    monkeypatch.setattr(gate, "_trial", lambda artifact, *a, **kw:
                        _trial_result(90 if Path(artifact).name == "candidate" else 100))
    result = gate.select_warm_cache(*args, **kwargs, alpha=0.001)
    assert result["selected"] == "warm"
    assert result["alpha"] == result["plan"]["alpha"] == 0.001
    assert all(row["p_value_numerator"] == 1 and row["p_value_denominator"] == 924
               and not row["passes"] for row in result["inputs"].values())


@pytest.mark.parametrize("alpha", [0, 0.051, float("nan")])
def test_invalid_alpha_does_not_start_a_campaign(tmp_path, monkeypatch, alpha):
    args, kwargs = _fixture(tmp_path)
    monkeypatch.setattr(gate, "_trial", lambda *a, **kw: pytest.fail("unexpected launch"))
    result = gate.select_warm_cache(*args, **kwargs, alpha=alpha)
    assert result["status"] == "error"
    assert result["reason"] == "preflight_failed"
    assert "alpha" in result["detail"]


@pytest.mark.parametrize("arm", gate.ARMS)
def test_candidate_and_warm_failures_are_distinct(tmp_path, monkeypatch, arm):
    args, kwargs = _fixture(tmp_path)

    def fake_trial(artifact, *a, **kw):
        if Path(artifact).name == arm:
            raise gate.RunDirError("cache_proof_failed")
        return _trial_result(100)

    monkeypatch.setattr(gate, "_trial", fake_trial)
    result = gate.select_warm_cache(*args, **kwargs)
    assert result["trial_error"]["code"] == "cache_proof_failed"
    if arm == "candidate":
        assert result["reason"] == "candidate_invalid" and result["selected"] == "warm"
    else:
        assert result["reason"] == "warm_control_invalid" and result["selected"] is None


def test_reject_reused_training_bytes_and_underpowered_plan(tmp_path, monkeypatch):
    args, kwargs = _fixture(tmp_path)
    monkeypatch.setattr(gate, "_trial", lambda *a, **kw: pytest.fail("unexpected launch"))
    underpowered = gate.select_warm_cache(*args, **kwargs, pairs=10)
    assert underpowered["status"] == "error" and "at least 12" in underpowered["detail"]
    kwargs["training_inputs"][0].write_bytes(kwargs["inputs"][0].bank_inputs["input"].read_bytes())
    overlap = gate.select_warm_cache(*args[:2], tmp_path / "overlap", **kwargs)
    assert overlap["status"] == "error" and "not independent" in overlap["detail"]


def test_exact_p_value_matches_brute_force_with_ties():
    orders = gate._paired_orders(12, random.Random(123))
    # Fixed times by launch position, including position bias and ties.
    positions = [(i + 2, 8) for i in range(12)]
    rows = [{"first": order[0], "second": order[1], **dict(zip(order, times))}
            for order, times in zip(orders, positions)]
    evidence = gate._paired_evidence(rows)

    def wins(candidate_first):
        return sum((first < second if i in candidate_first else second < first)
                   for i, (first, second) in enumerate(positions))

    expected_tail = sum(wins(assign) >= evidence["wins"]
                        for assign in itertools.combinations(range(12), 6))
    assert evidence["p_value_denominator"] == 924
    assert evidence["p_value_numerator"] == expected_tail


def test_fixed_workload_needs_no_ram_bank_inputs(tmp_path, monkeypatch):
    args, kwargs = _fixture(tmp_path)
    old = kwargs["inputs"][0]
    case = gate.TimingInput("fixed", old.config, {}, old.expected_guest)
    monkeypatch.setattr(gate, "_trial", lambda artifact, *a, **kw:
                        _trial_result(90 if Path(artifact).name == "candidate" else 100))
    result = gate.select_warm_cache(*args, inputs=[case])
    assert result["selected"] == "candidate"
    assert result["plan"]["timing_inputs"]["fixed"]["input_kind"] == "fixed-work"
    assert result["runtime"]["ram_storage"] == "tmpfs-private-file"
    assert result["artifacts"]["warm"]["bundle"]["sha256"] == gate.file_sha256(args[0] / "bundle.json")


def test_mutated_source_prevents_selection(tmp_path, monkeypatch):
    args, kwargs = _fixture(tmp_path)

    def mutate(artifact, *a, **kw):
        if Path(artifact).name == "candidate":
            (Path(artifact) / "cache.sc").write_bytes(b"changed in flight")
            return _trial_result(90)
        return _trial_result(100)

    monkeypatch.setattr(gate, "_trial", mutate)
    result = gate.select_warm_cache(*args, **kwargs)
    assert result["status"] == "error" and result["reason"] == "source_changed"
    assert result["selected"] is None


def test_compared_runs_must_use_same_ram_storage_and_qemu(tmp_path, monkeypatch):
    args, kwargs = _fixture(tmp_path)

    def different(artifact, *a, **kw):
        result = _trial_result(100)
        if Path(artifact).name == "candidate":
            result["qemu_sha256"] = "sha256:another-runtime"
        return result

    monkeypatch.setattr(gate, "_trial", different)
    result = gate.select_warm_cache(*args, **kwargs)
    assert result["status"] == "error" and result["reason"] == "runtime_changed"
    assert result["selected"] is None


@pytest.mark.parametrize(("patch", "reason"), [
    ({"guest_norm": ["wrong"]}, "guest_output_mismatch"),
    ({"appended": 1}, "cache_proof_failed"),
    ({"inputs": {"input": {"sha256": "other"}}}, "input_digest_mismatch"),
])
def test_trial_refuses_invalid_launch_evidence(tmp_path, monkeypatch, patch, reason):
    from types import SimpleNamespace

    case = gate._Case(tmp_path / "config.toml", {"input": tmp_path / "input"},
                      {"input": "sha256:input"}, ["expected"])
    monkeypatch.setattr(gate, "create_run_dir", lambda *a, **kw: SimpleNamespace(meta={}))
    checked = {"checked": True, "bundle_id": "bundle", "runtime_identity_id": "runtime", "reasons": []}
    monkeypatch.setattr(gate, "deploy", lambda *a, **kw: {"identity": checked})
    valid = {"exit_code": 0, "timed_out": False, "verdict": "PASS", "appended": 0,
             "guest_norm": ["expected"], "inputs": {"input": {"sha256": "sha256:input"}},
             "bench": [{"elapsed_ns": 1}], "qemu_sha256": "sha256:runtime"}
    monkeypatch.setattr(gate, "launch", lambda *a, **kw: {**valid, **patch})
    with pytest.raises(gate.RunDirError, match=reason):
        gate._trial("artifact", case, tmp_path / "run", qemu=None, timeout_s=3)



def test_auxiliary_hint_with_identical_native_code_must_be_measured(tmp_path, monkeypatch):
    args, kwargs = _fixture(tmp_path)
    warm, candidate, _ = args
    (candidate / "cache.sc").write_bytes((warm / "cache.sc").read_bytes())
    (candidate / "cache.sc.restore-hints").write_bytes(b"test hint")
    def trial(artifact, *unused, **options):
        hinted = Path(artifact) == candidate
        result = _trial_result(90 if hinted else 100)
        if options.get("restore_probe"):
            result["restore"] = {"hint_file_valid": hinted, "attempted": 7,
                                 "nominated": 6 if hinted else 0, "skipped": 4 if hinted else 0}
        return result
    monkeypatch.setattr(gate, "_trial", trial)
    result = gate.select_warm_cache(*args, **kwargs)
    assert result["byte_identical"] and not result["deployment_inputs_identical"]
    assert result["selected"] == "candidate" and result["artifacts"]["candidate"]["restore_hints"]
    assert all(p["candidate"]["skipped"] == 4 for p in result["restore_probes"].values())


@pytest.mark.parametrize("unsupported", [False, True])
def test_inert_hint_cannot_be_promoted_by_timing_noise(tmp_path, monkeypatch, unsupported):
    args, kwargs = _fixture(tmp_path)
    warm, candidate, out = args
    (candidate / "cache.sc").write_bytes((warm / "cache.sc").read_bytes())
    (candidate / "cache.sc.restore-hints").write_bytes(b"test hint")
    def trial(artifact, *unused, **options):
        assert options["restore_probe"]
        hinted = Path(artifact) == candidate
        result = _trial_result(70 if hinted else 100)
        result["restore"] = None if unsupported else {
            "hint_file_valid": hinted, "attempted": 7, "nominated": 7 if hinted else 0, "skipped": 0}
        return result
    monkeypatch.setattr(gate, "_trial", trial)
    result = gate.select_warm_cache(*args, **kwargs)
    assert result["selected"] == "warm" and result["reason"] == "restore_hints_inactive"
    assert not (out / "samples.jsonl").read_text()


def test_different_hints_with_same_positive_count_need_distinguishable_effects(tmp_path, monkeypatch):
    args, kwargs = _fixture(tmp_path)
    warm, candidate, out = args
    (candidate / "cache.sc").write_bytes((warm / "cache.sc").read_bytes())
    for artifact in (warm, candidate):
        (artifact / "cache.sc.restore-hints").write_bytes(artifact.name.encode())
    def trial(artifact, *unused, **options):
        assert options["restore_probe"]
        return {**_trial_result(70 if Path(artifact) == candidate else 100),
                "restore": {"hint_file_valid": True, "attempted": 7, "nominated": 4, "skipped": 4}}
    monkeypatch.setattr(gate, "_trial", trial)
    result = gate.select_warm_cache(*args, **kwargs)
    assert result["reason"] == "restore_effect_not_distinguished" and result["selected"] == "warm"
    assert not (out / "samples.jsonl").read_text()
