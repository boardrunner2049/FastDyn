"""fastdyn.runner: the orchestration behind `fastdyn run`, usable in-process."""

from __future__ import annotations

import logging
import subprocess
import sys
from contextlib import contextmanager, nullcontext

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
    monkeypatch.setattr(runner.runtime_config, "bridge_boot_environment", lambda _c: nullcontext())
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


def test_auto_build_composes_missing_model_before_validating_sources(tmp_path, monkeypatch):
    from fastdyn import physics_composition

    config = tmp_path / "vehicle.toml"
    config.write_text('[FMU.physics]\nmodel_name = "Generated"\n')
    output = tmp_path / "generated"
    build = fmu_build.FmuBuild(
        name="quad", model="Generated", model_file=output / "Generated.mo",
        source_root=output, extra_source_roots=(), output=output,
        repo_root=tmp_path, package=False, auto_build=True,
        compose_config=config, config_path=config,
    )
    build.rumoca_dir.mkdir(parents=True)
    (build.rumoca_dir / "Cargo.toml").write_text("[workspace]\n")

    def compose(selected):
        selected.model_file.parent.mkdir(parents=True)
        selected.model_file.write_text("model Generated end Generated;\n")

    def compile_model(command, **_kwargs):
        if "--help" in command:
            return subprocess.CompletedProcess(command, 0, stdout="--build")
        assert build.model_file.is_file()
        build.artifact_path.write_text("<fmiModelDescription/>\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(fmu_build, "resolve", lambda *_args: build)
    monkeypatch.setattr(fmu_build.shutil, "which", lambda _name: "/test/cargo")
    monkeypatch.setattr(physics_composition, "compose", compose)
    monkeypatch.setattr(physics_composition, "stage_msl", lambda _build: None)
    monkeypatch.setattr(physics_composition, "record_compilation", lambda *_args: None)
    monkeypatch.setattr(fmu_build.subprocess, "run", compile_model)

    runner.auto_build_fmu(config, None, skip_build=False)

    assert build.model_file.is_file()
    assert build.artifact_path.is_file()
    assert not fmu_build.needs_build(build)


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


def test_boot_environment_reaches_qemu_without_helpers_and_is_restored(tmp_path, monkeypatch):
    import os
    from fastdyn.targets import qemu_target

    monkeypatch.delenv("FASTDYN_BRIDGE_BOOT", raising=False)
    boot = tmp_path / "boot.nsh"
    boot.write_text("echo READY\n")
    config = tmp_path / "px4.toml"
    config.write_text(f'[Run]\nbridge_boot = "{boot}"\n')
    observed = []

    class BootHandle(FakeHandle):
        def run(self, machine_name, target, out_path):
            observed.append(qemu_target._build_qemu_env(["qemu-system-arm"]).get("FASTDYN_BRIDGE_BOOT"))
            raise RuntimeError("QEMU stopped")

    monkeypatch.setattr(runner.toml_parser, "parser", lambda *_a, **_k: BootHandle([]))
    with pytest.raises(RuntimeError, match="QEMU stopped"):
        runner.run_config(config, str(tmp_path / "work"), build_fmu=False, run_processes=False)
    assert observed == [str(boot)]
    assert "FASTDYN_BRIDGE_BOOT" not in os.environ

    # A subsequent run in the same Python process must not inherit PX4's boot.
    config.write_text("[Run]\n")
    with pytest.raises(RuntimeError, match="QEMU stopped"):
        runner.run_config(config, str(tmp_path / "work2"), build_fmu=False, run_processes=False)
    assert observed == [str(boot), None]


def test_missing_boot_is_reported_before_replacing_work_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FASTDYN_BRIDGE_BOOT", raising=False)
    config = tmp_path / "px4.toml"
    config.write_text(f'[Run]\nbridge_boot = "{tmp_path / "missing.nsh"}"\n')
    work = tmp_path / "work"
    work.mkdir()
    marker = work / "keep.txt"
    marker.write_text("keep")
    with pytest.raises(runner.runtime_config.RuntimeConfigError, match="boot script not found"):
        runner.run_config(config, str(work), build_fmu=False, run_processes=False)
    assert marker.read_text() == "keep"
