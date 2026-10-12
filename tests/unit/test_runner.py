"""fastdyn.runner: the orchestration behind `fastdyn run`, usable in-process."""

from __future__ import annotations

import logging
import subprocess
import sys
from contextlib import contextmanager

import click
import pytest
from click.testing import CliRunner

import fastdyn
from fastdyn import fmu_build, main, runner


class FakeHandle:
    def __init__(self, events, machines=("machine0",), exit_code=0):
        self.events = events
        self.machines = list(machines)
        self.exit_code = exit_code

    def run(self, machine_name, target, out_path):
        self.events.append(f"run:{machine_name}")
        return self.exit_code

    def shutdown(self):
        self.events.append("shutdown")


@pytest.fixture
def events(monkeypatch):
    """Replace validation, timing setup, helpers and parsing with recorders."""
    recorded = []

    monkeypatch.setattr(runner.runtime_config, "validate_config", lambda _c: recorded.append("validate"))
    monkeypatch.setattr(
        runner.runtime_config,
        "configure_run_environment",
        lambda _c, _w: {"FASTDYN_TIMING_FILE": "timing.jsonl", "FASTDYN_PERF_MODE": "off"},
    )

    @contextmanager
    def fake_launch(_config, _work_dir, skip=False):
        recorded.append(f"helpers_start(skip={skip})")
        yield None
        recorded.append("helpers_stop")

    monkeypatch.setattr(runner.runtime_config, "launch_from_config", fake_launch)
    monkeypatch.setattr(runner.fmu_build, "resolve", lambda *_a: (_ for _ in ()).throw(fmu_build.NoFmuConfig("none")))

    def fake_parser(*_args, **_kwargs):
        recorded.append("parse")
        return FakeHandle(recorded)

    monkeypatch.setattr(runner.toml_parser, "parser", fake_parser)
    return recorded


def test_run_config_keeps_the_fastdyn_run_order(events, tmp_path):
    results = runner.run_config(
        "cfg.toml",
        str(tmp_path / "work"),
        adjust=lambda _handle: events.append("adjust"),
    )

    assert events == [
        "validate",
        "helpers_start(skip=False)",
        "parse",
        "adjust",
        "run:machine0",
        "helpers_stop",
    ]
    assert results == [runner.RunResult("machine0", 0, (tmp_path / "work").resolve())]


def test_load_prepares_without_starting_anything(events, tmp_path):
    session = runner.load("cfg.toml", str(tmp_path / "work"), run_processes=False, validate=False)

    assert events == []
    assert (tmp_path / "work").is_dir()

    session.run()
    assert events == ["helpers_start(skip=True)", "parse", "run:machine0", "helpers_stop"]


def test_timing_location_is_logged_not_reported(events, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="fastdyn.runner")
    lines = []
    runner.run_config("cfg.toml", str(tmp_path / "work"), report=lines.append)

    assert "FastDyn timing log: timing.jsonl" in caplog.text
    assert lines == []  # report only carries action results such as FMU builds


def test_prepare_work_dir_replaces_unless_persisted(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "old.txt").write_text("old", encoding="utf-8")

    runner.prepare_work_dir(str(work), persist_work_dir=True)
    assert (work / "old.txt").exists()

    runner.prepare_work_dir(str(work), persist_work_dir=False)
    assert work.is_dir() and not (work / "old.txt").exists()


def test_auto_build_fmu_errors_become_run_errors(monkeypatch):
    def broken(*_args):
        raise fmu_build.FmuConfigError("bad [FMU] table")

    monkeypatch.setattr(runner.fmu_build, "resolve", broken)

    with pytest.raises(runner.RunError, match="bad \\[FMU\\] table"):
        runner.auto_build_fmu("cfg.toml", None, skip_build=False)
    runner.auto_build_fmu("cfg.toml", None, skip_build=True)  # skipping never resolves


def test_package_reexports_are_lazy():
    code = (
        "import sys, fastdyn\n"
        "assert 'fastdyn.runner' not in sys.modules\n"
        "assert fastdyn.run_config.__module__ == 'fastdyn.runner'\n"
        "assert fastdyn.RunResult is sys.modules['fastdyn.runner'].RunResult\n"
        "assert 'fastdyn.model_build' not in sys.modules\n"
        "assert fastdyn.build_models.__module__ == 'fastdyn.model_build'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    assert set(fastdyn.__all__) == {"run_config", "load", "Session", "RunResult", "RunError",
                                    "build_models", "Model", "BuildReport"}


def test_cli_run_delegates_to_the_runner(monkeypatch, tmp_path):
    config = tmp_path / "cfg.toml"
    config.write_text("", encoding="utf-8")
    calls = []
    monkeypatch.setattr(main.runner, "run_config", lambda *a, **k: calls.append((a, k)) or [])

    result = CliRunner().invoke(main.cli, ["run", "-c", str(config), "--no-build-fmu", "--no-run-processes"])

    assert result.exit_code == 0, result.output
    (args, kwargs), = calls
    assert args[0] == str(config)
    assert kwargs["build_fmu"] is False and kwargs["run_processes"] is False
    assert kwargs["report"] is click.echo


@pytest.mark.parametrize(
    "error, exit_code",
    [(runner.RunError("FMU build failed"), 1), (KeyboardInterrupt(), 130)],
)
def test_cli_run_reports_errors(monkeypatch, tmp_path, error, exit_code):
    config = tmp_path / "cfg.toml"
    config.write_text("", encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(main.runner, "run_config", fail)

    result = CliRunner().invoke(main.cli, ["run", "-c", str(config)])

    assert result.exit_code == exit_code
    if exit_code == 1:
        assert "FMU build failed" in result.output
