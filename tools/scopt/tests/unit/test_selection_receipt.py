import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scopt.commands.deploy import _selected_cache, deploy_cmd
from scopt.formats.sc_v5 import sparse_copy
from scopt.run import warm_selection as gate
from scopt.run.deploy import resolve_artifact
from scopt.run.launcher import file_sha256, norm_b0, norm_guest, parse_bench, write_json


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.fcntl, "flock", lambda *args: None)
    warm, candidate = (tmp_path / name for name in gate.ARMS)
    qemu_hash = "sha256:test-qemu"
    for arm in (warm, candidate):
        arm.mkdir()
        with (arm / "cache.sc").open("wb") as cache:
            cache.write(arm.name.encode())
            cache.truncate(1 << 20)
        write_json(arm / "bundle.json", {"bundle_id": "bundle"})
        write_json(arm / "runtime_identity.json", {"qemu_sha256": qemu_hash, "plugin_sha256": None})
    config = tmp_path / "base.toml"
    config.touch()
    training = tmp_path / "train"
    training.write_text("train")
    heldout = tmp_path / "heldout"
    heldout.write_text("heldout")
    expected = tmp_path / "expected.norm"
    expected.write_text("Reg 0:0x00000123\n[BENCH][bench] elapsed=<host-time>\n")
    case = gate.TimingInput("case", config, {"input": heldout}, expected)
    elapsed = {"warm": 100, "candidate": 90}
    receipt_path = tmp_path / "receipt" / "selection.json"

    def trial(artifact, inputs, path, **options):
        path.mkdir()
        a = resolve_artifact(artifact)
        cache_dir = path / "cache"
        cache_dir.mkdir()
        cache = cache_dir / "test.sc"
        sparse_copy(a["sc"], cache)
        if a.get("restore_hints"):
            cache.with_name("test.sc.restore-hints").write_bytes(Path(a["restore_hints"]).read_bytes())
        (path / "run.toml").touch()
        ids = {"checked": True, "bundle_id": "bundle", "expected_bundle_id": "bundle",
               "runtime_identity_id": "runtime", "reasons": []}
        deploy = {"artifact": a, "run_dir": str(path), "identity": ids,
                  "state": {"n": 1, "sc_path": str(cache), "file_version": 7,
                            "v7_kind": "public"}}
        if a.get("restore_hints"):
            deploy["restore_hint_sha256"] = file_sha256(a["restore_hints"])
        write_json(path / "deploy.json", deploy)
        write_json(path / "rundir.json", {"base_toml_sha256": file_sha256(config),
                   "run_toml_sha256": file_sha256(path / "run.toml"), "cache_name": "test.sc",
                   "ram_storage": "tmpfs-private-file"})
        n = elapsed[Path(artifact).name]
        stdout = f"Reg 0:0x00000123\n[BENCH][bench] elapsed_ns={n}\n"
        (path / "stdout.log").write_text(stdout)
        (path / "guest.norm").write_text("\n".join(norm_guest(stdout)) + "\n")
        (path / "output.norm").write_text("\n".join(norm_b0(stdout)) + "\n")
        proof = {"verdict": "PASS", "n": 1, "record_count_after": 1, "appended": 0,
                 "failed": [], "file_version": 7, "v7_kind": "public",
                 "checks": [{"check": name, "ok": True}
                            for name in ("sc_exists", "header", "records", "inode", "manifest")]}
        write_json(path / "run.json", {"run_dir": str(path), "exit_code": 0, "timed_out": False,
                   "verdict": "PASS", "appended": 0, "proof": proof, "qemu_sha256": qemu_hash,
                   "inputs": {"input": {"sha256": file_sha256(inputs.bank_inputs["input"])}},
                   "guest_norm": norm_guest(stdout), "output_norm_sha256": file_sha256(path / "output.norm"),
                   "bench": parse_bench(stdout)})
        sample = {"qemu_sha256": qemu_hash, "ram_storage": "tmpfs-private-file", "bundle_id": "bundle",
                  "runtime_identity_id": "runtime", "elapsed_ns": n}
        if options.get("restore_probe"):
            hinted = bool(a.get("restore_hints"))
            probe = {"attempted": 1, "nominated": int(hinted), "skipped": int(hinted)}
            write_json(path / "restore-stats.json", {"schema": "fastdyn.supercache_restore_stats/1",
                       "hint_file_valid": hinted, "benchmark": probe})
            sample["restore"] = {**probe, "hint_file_valid": hinted}
        return sample

    monkeypatch.setattr(gate, "_trial", trial)

    def run():
        return gate.select_warm_cache(warm, candidate, receipt_path.parent,
                                      inputs=[case], training_inputs=[training])

    return run, receipt_path, config, warm, candidate, elapsed


def test_existing_candidate_receipt_with_complete_trial_data(campaign):
    run, receipt, config, _, candidate, _ = campaign
    assert run()["selected"] == "candidate"
    assert _selected_cache(receipt, config)[0] == str(candidate)


def test_pre_hint_deployment_records_remain_verifiable(campaign):
    run, receipt, config, _, candidate, _ = campaign
    assert run()["selected"] == "candidate"
    for path in receipt.parent.glob("trial-*/deploy.json"):
        deployment = json.loads(path.read_bytes())
        del deployment["artifact"]["restore_hints"]
        write_json(path, deployment)
    assert _selected_cache(receipt, config)[0] == str(candidate)


def test_hint_only_candidate_requires_the_live_probe(campaign):
    run, receipt, config, warm, candidate, _ = campaign
    (candidate / "cache.sc").write_bytes((warm / "cache.sc").read_bytes())
    (candidate / "cache.sc.restore-hints").write_bytes(b"hint")
    assert run()["selected"] == "candidate"
    assert _selected_cache(receipt, config)[0] == str(candidate)
    (receipt.parent / "probe-0-candidate" / "restore-stats.json").unlink()
    with pytest.raises(OSError):
        _selected_cache(receipt, config)


@pytest.mark.parametrize("mutation", ["missing", "order", "timer", "cache", "hint", "input", "oracle", "append"])
def test_candidate_receipt_rejects_incomplete_or_disagreeing_evidence(campaign, mutation):
    run, receipt, config, _, _, _ = campaign
    assert run()["selected"] == "candidate"
    root = receipt.parent
    samples = root / "samples.jsonl"
    trial = root / "trial-00"
    if mutation == "missing":
        samples.unlink()
    elif mutation in ("order", "timer"):
        rows = [json.loads(line) for line in samples.read_text().splitlines()]
        if mutation == "order":
            rows[2], rows[3] = rows[3], rows[2]
        else:
            rows[2]["elapsed_ns"] = 1
        samples.write_text("".join(json.dumps(row) + "\n" for row in rows))
    elif mutation == "cache":
        with (trial / "cache/test.sc").open("r+b") as copy:
            copy.seek(50_000)
            copy.write(b"altered")
    elif mutation == "hint":
        (trial / "cache/test.sc.restore-hints").touch()
    elif mutation in ("input", "append"):
        record = trial / "run.json"
        doc = json.loads(record.read_bytes())
        if mutation == "input":
            doc["inputs"]["input"]["sha256"] = "sha256:another"
        else:
            doc["appended"] = 1
        write_json(record, doc)
    elif mutation == "oracle":
        (trial / "guest.norm").write_text("wrong\n")
    with pytest.raises((ValueError, OSError)):
        _selected_cache(receipt, config)


def test_manually_changed_summary_cannot_promote_a_losing_candidate(campaign):
    run, receipt, config, _, candidate, elapsed = campaign
    elapsed["candidate"] = 120
    result = run()
    assert result["selected"] == "warm"
    result.update(selected="candidate", selected_artifact=str(candidate), status="ok",
                  reason="speedup_established")
    result["inputs"]["case"]["passes"] = True
    write_json(receipt, result)
    with pytest.raises(ValueError, match="paired trials do not establish"):
        _selected_cache(receipt, config)


def test_warm_fallback_still_deploys_without_trial_archive(campaign):
    run, receipt, config, warm, _, elapsed = campaign
    elapsed["candidate"] = 120
    assert run()["selected"] == "warm"
    (receipt.parent / "samples.jsonl").unlink()
    assert _selected_cache(receipt, config)[0] == str(warm)


def test_cli_rejects_claim_without_archive_before_creating_run_dir(campaign):
    run, receipt, config, _, _, _ = campaign
    assert run()["selected"] == "candidate"
    (receipt.parent / "plan.json").unlink()
    target = receipt.parent / "deployment"
    result = CliRunner().invoke(deploy_cmd, ["--selection", str(receipt), "-c", str(config),
                                            "--run-dir", str(target)])
    assert result.exit_code != 0 and "Invalid cache selection" in result.output
    assert not target.exists()
