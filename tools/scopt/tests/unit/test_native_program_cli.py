import json
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from scopt.commands import optimize as command
from scopt import validate
from scopt.passes.base import EditKind
from scopt.passes.host_inline import host_inline_mode


@pytest.mark.parametrize("flags,expect_splice,expect_final", [
    ([], True, True),
    (["--disable", "host-inline-splice"], False, True),
    (["--disable", "host-inline"], False, True),
    (["--only", "region-state-forward"], False, True),
    (["--only", "host-inline-splice"], False, False),
    (["--only", "host-inline"], False, False),
])
def test_native_program_routes_stages_and_ablation(tmp_path, monkeypatch, flags,
                                                   expect_splice, expect_final):
    observed = []
    snap = SimpleNamespace(units=lambda: ())
    monkeypatch.setattr(command, "_open_snapshot", lambda *args: snap)
    monkeypatch.setattr(command, "_profiles", lambda *args: ([], []))

    def prefix(snapshot, stage, *args):
        assert [p.name for p in stage.passes] == ["host-inline"] and snapshot is snap
        assert stage.allowed_capabilities == {EditKind.INLINE_CALL}
        observed.append("splice")
        return snap, {"passes": []}

    def finish(snapshot, pipeline, *args, **kw):
        assert all(p.name != "host-inline" for p in pipeline.passes)
        observed.append("final")

    def standalone(snapshot, pipeline, *args, **kw):
        assert [p.name for p in pipeline.passes] == ["host-inline"]
        assert pipeline.allowed_capabilities == {EditKind.INLINE_CALL}
        observed.append("standalone")
        return SimpleNamespace(candidate=SimpleNamespace(passes=[]), to_json=lambda: {},
                               published=True)

    monkeypatch.setattr(command, "_splice_generation", prefix)
    monkeypatch.setattr(command, "_optimize_b3", finish)
    monkeypatch.setattr(validate, "optimize", standalone)
    result = CliRunner().invoke(command.optimize_cmd, [
        str(tmp_path), "--pipeline", "native-program", "--store", str(tmp_path), "--json", *flags])
    assert result.exit_code == 0, result.exception
    assert ("splice" in observed) is expect_splice
    assert observed[-1] == ("final" if expect_final else "standalone")


@pytest.mark.parametrize("mode", ["splice", "faulting", "extreme"])
def test_composed_prefix_receives_effective_parameters(tmp_path, monkeypatch, mode):
    snap = SimpleNamespace(units=lambda: ())
    monkeypatch.setattr(command, "_open_snapshot", lambda *args: snap)
    monkeypatch.setattr(command, "_profiles", lambda *args: ([], []))
    supplied = {"mode": mode, "max_total_growth": 1024}
    if mode == "extreme":
        supplied.update(max_inline_depth=2, allow_partial=True)
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"host-inline": supplied, "order-cold": {"hot_fallback": True}}))
    calls = []

    def prefix(snapshot, stage, store, views, overrides):
        assert snapshot is snap
        entry, = stage.passes
        assert entry.name == "host-inline" and stage.allowed_capabilities == {EditKind.INLINE_CALL}
        assert set(overrides) == {"host-inline"}
        effective = {**entry.params, **overrides["host-inline"]}
        assert effective["max_total_growth"] == 1024
        if mode == "faulting":
            assert host_inline_mode(effective) == "faulting"
        elif mode == "extreme":
            assert effective["mode"] == "extreme" and effective["max_inline_depth"] == 2
            assert effective["allow_partial"]
        else:
            assert effective["mode"] == "splice"
        calls.append("prefix")
        return snap, {"passes": []}

    def final(snapshot, pipeline, store, views, notes, overrides, *args, **kwargs):
        assert not pipeline.allowed_capabilities & {EditKind.INLINE_CALL}
        assert overrides == {"order-cold": {"hot_fallback": True}}
        calls.append("final")

    monkeypatch.setattr(command, "_splice_generation", prefix)
    monkeypatch.setattr(command, "_optimize_b3", final)
    result = CliRunner().invoke(command.optimize_cmd, [
        str(tmp_path), "--pipeline", "native-program", "--params", str(params),
        "--store", str(tmp_path), "--json",
    ])
    assert result.exit_code == 0, result.exception
    assert calls == ["prefix", "final"]


def test_composed_island_rejected_before_running_prefix(tmp_path, monkeypatch):
    snap = SimpleNamespace(units=lambda: ())
    monkeypatch.setattr(command, "_open_snapshot", lambda *args: snap)
    monkeypatch.setattr(command, "_profiles", lambda *args: ([], []))
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"host-inline": {"mode": "island", "allow_faulting": False}}))
    result = CliRunner().invoke(command.optimize_cmd, [
        str(tmp_path), "--pipeline", "native-program", "--params", str(params), "--store", str(tmp_path),
    ])
    assert result.exit_code == 2
    assert "host-inline island mode requires a standalone pipeline" in result.output


def test_legacy_custom_prefix_keeps_its_declared_capabilities(tmp_path, monkeypatch):
    # A user pipeline can live in an isolated pipeline directory. Splitting
    # its prefix must not import a built-in file or grant a missing capability.
    pipeline = tmp_path / "custom.toml"
    pipeline.write_text(
        'name = "custom"\nallowed_capabilities = ["CLONE"]\n'
        '[[pass]]\nname = "host-inline-splice"\nversion = 1\n'
        '[[pass]]\nname = "region-state-forward"\nversion = 1\n')
    monkeypatch.setenv("SCOPT_PIPELINES_DIR", str(tmp_path))
    snap = SimpleNamespace(units=lambda: ())
    monkeypatch.setattr(command, "_open_snapshot", lambda *args: snap)
    monkeypatch.setattr(command, "_profiles", lambda *args: ([], []))
    stages = []

    def prefix(snapshot, stage, *args):
        stages.append(stage)
        return snap, {"passes": []}

    monkeypatch.setattr(command, "_splice_generation", prefix)
    monkeypatch.setattr(command, "_optimize_b3", lambda *args, **kwargs: None)
    result = CliRunner().invoke(command.optimize_cmd, [
        str(tmp_path), "--pipeline", str(pipeline), "--store", str(tmp_path)])
    assert result.exit_code == 0, result.exception
    stage, = stages
    assert len(stage.passes) == 1 and not stage.allowed_capabilities
