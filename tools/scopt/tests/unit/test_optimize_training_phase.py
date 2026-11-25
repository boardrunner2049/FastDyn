"""Automatic PGO training keeps the pipeline's phase when a marker exists."""

import json
from types import SimpleNamespace

import pytest

import scopt.formats.profile_v1 as P
import scopt.run.helper_pgo as H
from scopt.commands.optimize import _automatic_loop_pgo


@pytest.fixture
def training(tmp_path, monkeypatch):
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir()
    (snap_dir / "snapshot.json").write_text(json.dumps({"bundle_id": "b", "contract_id": "c"}))
    launch = tmp_path / "launch.json"
    launch.write_text("{}")
    state = SimpleNamespace(calls=[], reached={"boot": True}, resolved=None)

    def collect_edges(source, launch_path, edges, *, trusted=False, phases=None):
        state.calls.append(phases)
        edges.write_bytes(b"")

    def resolve(launch_path, spec, wanted):
        assert spec == state.spec and wanted == state.wanted
        return state.resolved

    names = ["boot", "steady"]
    profile = SimpleNamespace(
        phase_id=lambda name: names.index(name) if name in names else None,
        phase_reached=lambda pid: state.reached.get(names[pid], False))
    monkeypatch.setattr(H, "collect_edges", collect_edges)
    monkeypatch.setattr(H, "resolve_training_phases", resolve)
    monkeypatch.setattr(P, "ingest", lambda *a, **k: profile)
    monkeypatch.setattr(P, "write_profile", lambda path, prof: None)
    monkeypatch.setattr(P.ProfileView, "open", classmethod(lambda cls, prof, snap: "view"))

    def run(spec="auto", wanted="steady", options=None):
        state.spec, state.wanted = spec, wanted
        options = {} if options is None else options
        views = []
        snap = SimpleNamespace(path=str(snap_dir), runtime=None)
        record = _automatic_loop_pgo(snap, tmp_path / "store", launch, None, views, options,
                                     helpers=False, wanted_phase=wanted, phase_arg=spec)
        return options, record, views
    state.run = run
    return state


def _resolved(phases, phase, source, explicit):
    return {"phases": phases, "phase": phase, "marker": "0x833e8" if phases else None,
            "source": source, "explicit": explicit}


def test_marked_training_keeps_the_steady_phase(training, capsys):
    training.resolved = _resolved("steady@0x833e8", "steady", "symbol:vComputeWindowStart", False)
    training.reached["steady"] = True
    options, record, views = training.run()
    assert training.calls == ["steady@0x833e8"] and views == ["view"]
    assert options["phase"] == "steady"
    assert record["source"] == "symbol:vComputeWindowStart" and record["phase"] == "steady"
    assert "warning" not in capsys.readouterr().err
    # An explicit --params phase still wins over the training phase.
    options, record, _ = training.run(options={"phase": "boot"})
    assert options["phase"] == "boot" and record["phase"] == "boot"


def test_unmarked_training_falls_back_to_whole_run_loudly(training, capsys):
    training.resolved = _resolved(None, "boot", "whole_run_unmarked", False)
    options, record, _ = training.run()
    assert training.calls == [None] and options["phase"] == "boot"
    assert record["source"] == "whole_run_unmarked" and record["wanted"] == "steady"
    assert "whole-run 'boot' phase" in capsys.readouterr().err


def test_unreached_marker(training, capsys):
    training.resolved = _resolved("steady@0x833e8", "steady", "symbol:vComputeWindowStart", False)
    options, record, _ = training.run()
    assert options["phase"] == "boot" and record["source"] == "whole_run_unreached"
    assert "warning" in capsys.readouterr().err
    training.resolved = _resolved("steady@0x833e8", "steady", "cli", True)
    with pytest.raises(ValueError, match="never reached"):
        training.run(spec="steady@0x833e8")
