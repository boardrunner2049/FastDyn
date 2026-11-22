import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from scopt import validate
from scopt.commands import optimize as command


@pytest.fixture
def optimize(tmp_path, monkeypatch):
    observed = {}
    monkeypatch.setattr(command, "_open_snapshot", lambda *args: SimpleNamespace(units=lambda: ()))

    def run(snapshot, pipeline, store, **kwargs):
        observed.update(pipeline=pipeline, **kwargs)
        return SimpleNamespace(candidate=SimpleNamespace(passes=[]), to_json=lambda: {},
                               published=True)

    monkeypatch.setattr(validate, "optimize", run)

    def invoke(pipeline, *flags):
        result = CliRunner().invoke(command.optimize_cmd, [
            str(tmp_path), "--pipeline", pipeline, "--store", str(tmp_path), "--json", *flags])
        return result, observed

    return invoke


@pytest.mark.parametrize(("options", "mode", "faulting", "depth"), [
    ("splice", "splice", False, None),
    ("faulting", "splice", True, None),
    ("splice,faulting", "splice", True, None),
    ("extreme", "extreme", False, None),
    ("island,1-level", "island", False, 1),
    ("2-level", "splice", False, 2),
    ("splice,1-level", "splice", False, 1),
    ("faulting,2-level", "splice", True, 2),
    ("extreme,2-level", "extreme", False, 2),
    ("2-level,splice,extreme,faulting", "extreme", True, 2),
    ("3-level", "splice", False, 3),
    ("4-level,faulting", "splice", True, 4),
    ("extreme,splice,17-level", "extreme", False, 17),
    ("maximal", "maximal", True, 2147483647),
    ("maximal,faulting", "maximal", True, 2147483647),
])
def test_shorthand_configures_one_pass(optimize, options, mode, faulting, depth):
    result, observed = optimize("host-inline=" + options)
    assert result.exit_code == 0, result.output
    entry, = observed["pipeline"].passes
    assert entry.name == "host-inline"
    assert entry.params["mode"] == mode and entry.params["allow_faulting"] is faulting
    assert entry.params.get("max_inline_depth") == depth
    if mode == "maximal":
        assert entry.params["allow_partial"] is True
        assert entry.params["compact_frame"] is True


def test_selector_with_shorthand_preserves_other_toml_params_and_json_overrides(tmp_path, optimize):
    pipeline = tmp_path / "host-inline=custom.toml"
    pipeline.write_text('name="example"\nallowed_capabilities=["INLINE_CALL"]\n'
                        '[[pass]]\nname="host-inline"\nversion=1\n[pass.params]\nmax_sites=11\n')
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"host-inline": {"max_inline_depth": 1, "max_sites": 3}}))
    result, observed = optimize(str(pipeline), "--only", "host-inline=4-level",
                                "--enable", "host-inline=faulting", "--params", str(params))
    assert result.exit_code == 0, result.output
    entry, = observed["pipeline"].passes
    assert entry.params == {"max_sites": 11, "mode": "splice", "allow_faulting": True,
                            "max_inline_depth": 4}
    assert observed["params"] == {"host-inline": {"max_inline_depth": 1, "max_sites": 3}}
    assert observed["only"] == "host-inline" and tuple(observed["enable"]) == ("host-inline",)


@pytest.mark.parametrize(("pipeline", "flags", "error"), [
    ("host-inline=spilce", [], "unknown host-inline option"),
    ("host-inline=faulting,", [], "unknown host-inline option"),
    ("host-inline=island,faulting", [], "island cannot be combined"),
    ("host-inline=island,2-level", [], "island cannot be combined"),
    ("host-inline=island,4-level", [], "island cannot be combined"),
    ("host-inline=1-level,2-level", [], "one inline depth"),
    ("host-inline=3-level", ["--enable", "host-inline=4-level"], "one inline depth"),
    ("host-inline=0-level", [], "positive integer"),
    ("host-inline=-1-level", [], "positive integer"),
    ("host-inline=x-level", [], "positive integer"),
    ("host-inline=maximal,2-level", [], "maximal already exhausts certified chains"),
    ("host-inline=maximal,extreme", [], "maximal already exhausts certified chains"),
    ("identity", ["--only", "host-inline=faulting"], "host-inline is not in pipeline"),
])
def test_invalid_shorthand_reports_usage_error(optimize, pipeline, flags, error):
    result, observed = optimize(pipeline, *flags)
    assert result.exit_code == 2 and error in result.output
    assert not observed


def test_python_pass_manager_uses_same_shorthand_and_json_precedence(optimize):
    from scopt.manager import PassManager, load_pipeline
    from tests.synth.fake_snapshot import fake_snapshot

    spec = "host-inline=faulting,2-level"
    result, observed = optimize("host-inline=faulting", "--only", "host-inline=2-level")
    assert result.exit_code == 0, result.output
    assert load_pipeline(spec).pipeline_id == observed["pipeline"].pipeline_id
    candidate = PassManager(fake_snapshot(1), spec, disable=("host-inline",),
                            params={"host-inline": {"max_inline_depth": 1}}).execute()
    record, = candidate.passes
    assert record.name == "host-inline"
    assert record.params == {"mode": "splice", "allow_faulting": True, "max_inline_depth": 1}


def test_maximal_pipeline_has_no_growth_profitability_cap():
    from scopt.manager import load_pipeline
    from scopt.passes.host_inline import HostInline

    entry, = load_pipeline("host-inline-maximal").passes
    assert "max_sites" not in entry.params
    assert "max_growth_per_unit" not in entry.params
    assert "max_total_growth" not in entry.params
    assert HostInline.descriptor.budgets.max_code_growth_bytes > (1 << 32)


def test_extensionless_path_with_equals_is_literal(tmp_path, monkeypatch):
    from pathlib import Path

    from scopt.manager import load_pipeline

    monkeypatch.chdir(tmp_path)
    path = Path("host-inline=extreme")
    path.write_text('name="custom"\n')
    assert load_pipeline(path).name == "custom"
    assert load_pipeline("./host-inline=extreme").name == "custom"
    assert load_pipeline("host-inline=extreme").name == "host-inline"
