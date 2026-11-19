import json
from pathlib import Path
import shutil

import pytest
from click.testing import CliRunner

from scopt.commands.select_warm import tune_warm
from scopt.run import warm_selection as gate
from scopt.run import warm_tuning as tuning
from scopt.run.warm_tuning import tune_warm_cache
from scopt.run.selection_receipt import verify_candidate_receipt


def fixture(root):
    warm = root/"warm"
    warm.mkdir()
    for filename in ("cache.sc", "bundle.json", "runtime_identity.json"):
        (warm/filename).write_text("{}")
    paths = []
    for name in ("candidate-a","candidate-b"):
        p = root/name
        shutil.copytree(warm,p)
        (p/"cache.sc").write_text(name)
        paths.append(p)
    cfg, oracle, earlier = (root/name for name in ("cfg", "expected", "earlier"))
    cfg.touch()
    oracle.write_text("guest ready\n")
    earlier.write_bytes(b"previous")
    cases=[]
    for name in ("training","final"):
        p = root/name
        p.write_text(name)
        cases.append(gate.TimingInput("input",cfg,{"input":p},oracle))
    return warm, paths, cases, earlier


def trial(elapsed):
    return {"elapsed_ns": elapsed, "qemu_sha256": "test:qemu", "ram_storage": "tmpfs-private-file",
            "bundle_id": "bundle", "runtime_identity_id": "runtime"}


def test_train_freeze_confirm_through_cli(tmp_path, monkeypatch):
    warm, paths, (train, final), earlier = fixture(tmp_path)
    def mock_trial(path, case, *args, **kwargs):
        times = {"warm":100,"candidate-a":90,"candidate-b":80}
        if case.bank_inputs["input"] == final.bank_inputs["input"]:
            assert Path(path) in (warm, paths[1])
            assert (tmp_path/"out/frozen.json").is_file()
            times["candidate-b"]=110
        return trial(times[Path(path).name])
    monkeypatch.setattr(gate,"_trial",mock_trial)
    monkeypatch.setattr(gate.fcntl,"flock",lambda *_:None)
    definitions=[]
    for name,case in (("train",train),("held",final)):
        file=tmp_path/(name+".json")
        file.write_text(json.dumps({"training_inputs":[str(earlier)],"inputs":[
            {"name":"input","config":str(case.config),"expected_guest":str(case.expected_guest),
             "bank_inputs":{"input":str(case.bank_inputs["input"])}}]}))
        definitions.append(file)
    cmd=CliRunner().invoke(tune_warm,[*map(str,[warm,*paths]),"--training-cases",str(definitions[0]),
                  "--cases",str(definitions[1]),"--out",str(tmp_path/"out"),"--json"])
    assert cmd.exit_code==0, cmd.output
    report=json.loads(cmd.output)
    assert report["training_winner"]==1 and report["selected"]=="warm"
    assert json.loads((tmp_path/"out/training/1/selection.json").read_text())["schema"]=="scopt.warm_training/1"
    promotion = json.loads((tmp_path/"out/training/1/selection.json").read_text())
    promotion["schema"]="scopt.warm_selection/1"
    with pytest.raises(ValueError,match="separate confirmation"):
        verify_candidate_receipt(tmp_path/"out/training/1/selection.json",promotion,train.config)
    receipt=json.loads(Path(report["selection"]).read_text())
    assert receipt["schema"]=="scopt.warm_selection/1"
    assert str(train.bank_inputs["input"]) in receipt["plan"]["excluded_inputs"]
    assert receipt["selected_artifact"]==str(warm)


def test_no_training_gain_keeps_warm_without_confirmation_launches(tmp_path,monkeypatch):
    warm, paths, (train,final), earlier=fixture(tmp_path)
    def mock_trial(path,case,*args,**kwargs):
        assert case.bank_inputs==train.bank_inputs
        return trial(90 if Path(path)==warm else 100)
    monkeypatch.setattr(gate,"_trial",mock_trial)
    monkeypatch.setattr(gate.fcntl,"flock",lambda *_:None)
    report=tune_warm_cache(warm,paths,tmp_path/"out",training=[train],confirmation=[final],
                           earlier_inputs=[earlier])
    assert report["reason"]=="no_training_winner" and report["selected"]=="warm"
    assert not (tmp_path/"out/confirmation/samples.jsonl").exists()


def test_training_bank_reused_by_content_is_refused_before_trials(tmp_path,monkeypatch):
    warm,paths,(train,final),earlier=fixture(tmp_path)
    final.bank_inputs["input"].write_bytes(train.bank_inputs["input"].read_bytes())
    monkeypatch.setattr(gate,"_trial",lambda *a,**kw:pytest.fail("unexpected launch"))
    with pytest.raises(ValueError,match="not independent"):
        tune_warm_cache(warm,paths,tmp_path/"out",training=[train],confirmation=[final],earlier_inputs=[earlier])
    assert not (tmp_path/"out").exists()


def test_modified_frozen_artifact_refused_before_any_confirmation(tmp_path,monkeypatch):
    warm,paths,(train,final),earlier=fixture(tmp_path)
    monkeypatch.setattr(gate.fcntl,"flock",lambda *_:None)
    monkeypatch.setattr(gate,"_trial",lambda *a,**kw:trial(100))
    training=gate.select_warm_cache(warm,paths[0],tmp_path/"screen",inputs=[train],
                                    training_inputs=[earlier],training_only=True)
    (paths[0]/"cache.sc").write_text("changed after training")
    monkeypatch.setattr(gate,"_trial",lambda *a,**kw:pytest.fail("unexpected launch"))
    result=gate.select_warm_cache(warm,paths[0],tmp_path/"out",inputs=[final],training_inputs=[earlier],
                                  expected_artifacts=training["artifacts"])
    assert result["reason"]=="frozen_candidate_changed" and result["status"]=="error"


def test_warm_symlink_resolved_before_training(tmp_path, monkeypatch):
    warm, candidates, (train, final), earlier = fixture(tmp_path)
    link = tmp_path / "reference"
    link.symlink_to(warm)
    write = tuning.write_json

    def retarget_after_plan(path, data):
        write(path, data)
        if path.name == "plan.json":
            link.unlink()
            link.symlink_to(candidates[0])

    monkeypatch.setattr(tuning, "write_json", retarget_after_plan)
    monkeypatch.setattr(gate.fcntl, "flock", lambda *_: None)
    monkeypatch.setattr(gate, "_trial", lambda *a, **kw: trial(100))
    result = tune_warm_cache(link, candidates, tmp_path/"out", training=[train],
                             confirmation=[final], earlier_inputs=[earlier])
    assert result["selected_artifact"] == str(warm)


def test_optional_hint_appearing_during_training_invalidates_tuning(tmp_path, monkeypatch):
    warm, candidates, (train, final), earlier = fixture(tmp_path)
    compare = tuning.select_warm_cache

    def add_hint_after_stage(*args, **kwargs):
        result = compare(*args, **kwargs)
        (warm/"cache.sc.restore-hints").write_text("new")
        return result

    monkeypatch.setattr(tuning, "select_warm_cache", add_hint_after_stage)
    monkeypatch.setattr(gate.fcntl, "flock", lambda *_: None)
    monkeypatch.setattr(gate, "_trial", lambda *a, **kw: trial(100))
    with pytest.raises(ValueError, match="artifact changed"):
        tune_warm_cache(warm, candidates, tmp_path/"out", training=[train],
                        confirmation=[final], earlier_inputs=[earlier])
    assert not (tmp_path/"out/confirmation/selection.json").exists()
