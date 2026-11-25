"""Automatic PGO training keeps the pipeline's phase when a marker exists."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import scopt.formats.profile_v1 as P
import scopt.run.helper_pgo as H
from scopt.commands import optimize as command
from scopt.commands.optimize import _automatic_loop_pgo


class _Profile:
    """The ingested training profile: phase rows as profile_v1 exports them."""

    def __init__(self, state):
        self.state = state
        self.profile_id = "sha256:" + "ab" * 32

    @property
    def phases(self):
        rows = [{"id": 0, "name": "boot", "marker": None, "reached": True,
                 "match": "exact_start", "first_dispatch_index": 0}]
        resolved = self.state.resolved
        if resolved and resolved["phases"]:
            reached = self.state.reached.get("steady", False)
            rows.append({"id": 1, "name": "steady", "marker": resolved["marker"],
                         "reached": reached, "match": "exact_start" if reached else None,
                         "first_dispatch_index": 17415 if reached else None})
        return rows

    def phase_id(self, name):
        return next((p["id"] for p in self.phases if p["name"] == name), None)

    def phase_reached(self, pid):
        return any(p["id"] == pid and p["reached"] for p in self.phases)


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

    monkeypatch.setattr(H, "collect_edges", collect_edges)
    monkeypatch.setattr(H, "resolve_training_phases", resolve)
    monkeypatch.setattr(P, "ingest", lambda *a, **k: _Profile(state))
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


def _resolved(marker, phase, source, explicit, candidates=()):
    return {"phases": f"{phase}@{marker}" if marker else None, "phase": phase,
            "marker": marker, "source": source, "explicit": explicit,
            "candidates": list(candidates)}


_UART = [{"source": "launch_virtual:benchmark_start", "vaddr": "0x822fc"},
         {"source": "symbol:vComputeWindowStart", "vaddr": "0x81ba0",
          "not_chosen": "launch_virtual:benchmark_start_precedence"}]


def test_marked_training_keeps_the_steady_phase(training, capsys):
    training.resolved = _resolved("0x822fc", "steady", "launch_virtual:benchmark_start", False,
                                  _UART)
    training.reached["steady"] = True
    options, record, views = training.run()
    assert training.calls == ["steady@0x822fc"] and views == ["view"]
    assert options["phase"] == "steady"
    assert record["source"] == "launch_virtual:benchmark_start" and record["phase"] == "steady"
    assert record["reached"] is True and record["first_dispatch_index"] == 17415
    assert record["match"] == "exact_start" and record["profile_id"] == "sha256:" + "ab" * 32
    assert record["candidates"] == _UART and record["marker"] == "0x822fc"
    assert "warning" not in capsys.readouterr().err
    # An explicit --params phase still wins over the training phase.
    options, record, _ = training.run(options={"phase": "boot"})
    assert options["phase"] == "boot" and record["phase"] == "boot"


def test_unmarked_training_falls_back_to_whole_run_loudly(training, capsys):
    training.resolved = _resolved(None, "boot", "whole_run_unmarked", False)
    options, record, _ = training.run()
    assert training.calls == [None] and options["phase"] == "boot"
    assert record["source"] == "whole_run_unmarked" and record["wanted"] == "steady"
    assert record["reached"] is None and record["candidates"] == []
    assert "whole-run 'boot' phase" in capsys.readouterr().err


def test_unreached_auto_marker_is_an_error(training, capsys):
    # The pre-fix uart case: auto resolved the dead public symbol.
    training.resolved = _resolved("0x81ba0", "steady", "symbol:vComputeWindowStart", False,
                                  [{"source": "symbol:vComputeWindowStart", "vaddr": "0x81ba0"}])
    options = {}
    with pytest.raises(H.TrainingPhaseError,
                       match=r"never reached phase marker steady@0x81ba0 \(source "
                             r"symbol:vComputeWindowStart; candidates: symbol:vComputeWindowStart "
                             r"0x81ba0\).*--pgo-phase boot") as error:
        training.run(options=options)
    assert "training-profile.json" in str(error.value)
    assert "phase" not in options and "warning" not in capsys.readouterr().err
    # The message lists every candidate auto examined.
    training.resolved = _resolved("0x822fc", "steady", "launch_virtual:benchmark_start", False,
                                  _UART)
    with pytest.raises(H.TrainingPhaseError, match=r"symbol:vComputeWindowStart 0x81ba0 "
                                                   r"\(not chosen: launch_virtual:"):
        training.run()
    # Explicit and launch-declared markers keep failing.
    training.resolved = _resolved("0x822fc", "steady", "cli", True)
    with pytest.raises(ValueError, match="never reached"):
        training.run(spec="steady@0x822fc")
    training.resolved = _resolved("0x822fc", "steady", "launch", True)
    with pytest.raises(H.TrainingPhaseError, match=r"steady@0x822fc \(source launch\)"):
        training.run()


def test_explicit_boot_is_the_whole_run_opt_in(training, capsys):
    training.resolved = _resolved(None, "boot", "whole_run_explicit", True)
    options, record, _ = training.run(spec="boot")
    assert training.calls == [None] and options["phase"] == "boot"
    assert record["source"] == "whole_run_explicit" and record["reached"] is None


@pytest.fixture
def supplied_profile_cli(tmp_path, monkeypatch):
    """optimize_cmd with a supplied --profile on a firmware-inline-only pipeline."""
    monkeypatch.setattr(command, "_open_snapshot", lambda *args: SimpleNamespace(units=lambda: ()))
    monkeypatch.setattr(command, "_profiles", lambda paths, snap: (["view"] if paths else [], []))
    monkeypatch.setattr(command, "_automatic_loop_pgo",
                        lambda *a, **k: pytest.fail("supplied views must not train"))
    seen = {}

    def optimize_b3(snap, pl, store, views, notes, *args, training_phase=None, **kwargs):
        # firmware-inline (EMIT_FIRMWARE_REGION) pipelines package through _optimize_b3,
        # which copies ``notes`` and ``training_phase`` into its JSON document.
        seen.update(views=views, notes=list(notes), training_phase=training_phase)

    monkeypatch.setattr(command, "_optimize_b3", optimize_b3)
    pipeline = tmp_path / "fw.toml"
    pipeline.write_text('schema = "scopt.pipeline"\nversion = 1\nname = "fw"\n'
                        'allowed_capabilities = ["EMIT_FIRMWARE_REGION"]\n'
                        '[[pass]]\nname = "firmware-inline"\nversion = 1\n'
                        'params = { mode = "hot-loop", phase = "steady" }\n')
    profile = tmp_path / "merged.profile.json"
    profile.write_text("{}")

    def invoke(*flags):
        result = CliRunner().invoke(command.optimize_cmd, [
            str(tmp_path), "--pipeline", str(pipeline), "--store", str(tmp_path / "store"),
            "--json", "--profile", str(profile), *flags])
        return result, seen

    return invoke


def test_supplied_profile_notes_an_ignored_pgo_phase(supplied_profile_cli):
    # cli c7b3 shape: --profile plus an explicit --pgo-phase on a firmware-inline pipeline.
    result, seen = supplied_profile_cli("--pgo-phase", "steady@0x82fc4")
    assert result.exit_code == 0, result.output
    assert seen["views"] == ["view"] and seen["training_phase"] is None
    assert len(seen["notes"]) == 1
    assert seen["notes"][0].startswith("--pgo-phase steady@0x82fc4 had no effect")
    assert "note: --pgo-phase steady@0x82fc4 had no effect" in result.stderr
    # The default auto stays quiet: Trebuchet candidates pass --profile without --pgo-phase.
    seen.clear()
    result, seen = supplied_profile_cli()
    assert result.exit_code == 0, result.output
    assert seen["notes"] == [] and "had no effect" not in result.stderr


@pytest.mark.parametrize("error, prefix", [
    (H.TrainingPhaseError("training replay never reached phase marker steady@0x81ba0"),
     "training phase: training replay never reached"),
    (H.PhaseSpecError("ambiguous_work_start: virtuals.txt has benchmark_start at 0x822fc, "
                      "0x82fc4"), "training phase: ambiguous_work_start"),
    (ValueError("helper PGO replay failed (1)"), "training replay failed: helper PGO"),
])
def test_training_phase_errors_are_reported_as_such(tmp_path, monkeypatch, error, prefix):
    monkeypatch.setattr(command, "_open_snapshot", lambda *args: SimpleNamespace(units=lambda: ()))
    monkeypatch.setattr(command, "_profiles", lambda paths, snap: ([], []))

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(command, "_automatic_loop_pgo", fail)
    monkeypatch.setattr(command, "_optimize_b3",
                        lambda *a, **k: pytest.fail("a failed training replay must not package"))
    pipeline = tmp_path / "fw.toml"
    pipeline.write_text('schema = "scopt.pipeline"\nversion = 1\nname = "fw"\n'
                        'allowed_capabilities = ["EMIT_FIRMWARE_REGION"]\n'
                        '[[pass]]\nname = "firmware-inline"\nversion = 1\n'
                        'params = { mode = "hot-loop", phase = "steady" }\n')
    launch = tmp_path / "launch.json"
    launch.write_text("{}")
    result = CliRunner().invoke(command.optimize_cmd, [
        str(tmp_path), "--pipeline", str(pipeline), "--store", str(tmp_path / "store"),
        "--json", "--pgo-launch", str(launch)])
    assert result.exit_code == 1
    assert f"Error: {prefix}" in result.stderr
