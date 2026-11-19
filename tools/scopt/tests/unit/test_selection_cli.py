import json

from click.testing import CliRunner
import pytest

from scopt.commands.deploy import _selected_cache, deploy_cmd
from scopt.commands.optimize import _select_output
from scopt.commands.select_warm import select_warm
from scopt.run.launcher import file_sha256


def test_no_change_cli_then_deploy_uses_original_warm(tmp_path, monkeypatch):
    warm, candidate = (tmp_path / name for name in ('warm.sc','candidate.sc'))
    warm.write_bytes(b'cache')
    candidate.write_bytes(warm.read_bytes())
    output = tmp_path/'selection'
    run = CliRunner()
    result = run.invoke(select_warm, [str(warm), str(candidate), '--out',str(output)])
    assert result.exit_code == 0, result.output
    config = tmp_path/'cfg.toml'
    config.write_text('[Machine]')
    seen = {}
    def deploy(src, cfg, run_dir, **kw):
        seen.update(src=src, **kw)
        return None,dict(run_dir='run',method='oracle',oracle={'fingerprint':'x'},state={'n':0},identity={})
    monkeypatch.setattr('scopt.run.deploy.deploy_new', deploy)
    result = run.invoke(deploy_cmd, ['--selection',str(output/'selection.json'),'-c',str(config),
                                      '--run-dir',str(tmp_path/'run'),'--json'])
    assert result.exit_code==0,result.output
    assert seen['src']==str(warm)
    assert seen['expected_sc_sha256']==file_sha256(warm)
    assert seen['measured_runtime'] is None


def test_reject_changed_or_unmeasured_config(tmp_path):
    config = tmp_path/'config'
    config.write_text('config')
    path=tmp_path/'selection.json'
    doc={'schema':'scopt.warm_selection/1','status':'ok','selected':'candidate','byte_identical':False,
         'reason':'speedup_established','selected_artifact':'fast','artifacts':{'candidate':{'path':'fast','cache_sha256':'sha256:abc'}},
         'inputs':{'case':{'passes':True}},'plan':{'timing_inputs':{'case':{'config_sha256':file_sha256(config)}}},
         'runtime':{'qemu_sha256':'sha256:def','ram_storage':'as-configured'}}
    path.write_text(json.dumps(doc))
    assert _selected_cache(path,config)==('fast','sha256:abc',doc['runtime'])
    config.write_text('unmeasured')
    with pytest.raises(ValueError,match='configuration was not measured'):
        _selected_cache(path,config)
    doc['inputs']={}
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError,match='insufficient'):
        _selected_cache(path,config)


def test_optimize_reports_selected_separately_from_candidate(tmp_path):
    warm, candidate=(tmp_path / s for s in ('warm.sc','candidate.sc'))
    warm.write_bytes(b'x')
    candidate.write_bytes(b'x')
    cases=tmp_path/'cases.json'
    cases.write_text('{"inputs":[]}')
    result={'artifact_dir':str(candidate)}
    _select_output(result,warm,cases,tmp_path/'out')
    assert result['artifact_dir']==str(candidate)
    assert result['selected_artifact']==str(warm)
    assert result['selection_reason']=='identical_cache_bytes'


def test_offline_candidate_requires_performance_evidence(tmp_path):
    doc = {"artifact_dir": str(tmp_path / "new")}
    _select_output(doc, tmp_path / "warm", None, None)
    assert doc["selected_artifact"] == str(tmp_path / "warm")
    assert doc["selection_reason"] == "candidate_unmeasured"
