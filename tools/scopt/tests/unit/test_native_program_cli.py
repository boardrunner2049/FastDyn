from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from scopt.commands import optimize as command
from scopt import validate


@pytest.mark.parametrize("flags,expect_splice,expect_final", [
    ([], True, True),
    (["--disable", "host-inline-splice"], False, True),
    (["--only", "region-state-forward"], False, True),
    (["--only", "host-inline-splice"], False, False),
])
def test_native_program_routes_stages_and_ablation(tmp_path, monkeypatch, flags,
                                                   expect_splice, expect_final):
    observed = []
    snap = SimpleNamespace(units=lambda: ())
    monkeypatch.setattr(command, "_open_snapshot", lambda *args: snap)
    monkeypatch.setattr(command, "_profiles", lambda *args: ([], []))

    def prefix(snapshot, entry, *args):
        assert entry.name == "host-inline-splice" and snapshot is snap
        observed.append("splice")
        return snap, {"passes": []}

    def finish(snapshot, pipeline, *args, **kw):
        assert all(p.name != "host-inline-splice" for p in pipeline.passes)
        observed.append("final")

    def standalone(snapshot, pipeline, *args, **kw):
        assert [p.name for p in pipeline.passes] == ["host-inline-splice"]
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
