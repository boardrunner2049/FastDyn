from contextlib import contextmanager
import sys
import tomllib
import types

from click.testing import CliRunner
import pytest


# fastdyn.main imports optional analysis backends that are not needed by this
# loop control-flow test.
cmsis_svd = types.ModuleType("cmsis_svd")
cmsis_svd_parser = types.ModuleType("cmsis_svd.parser")
cmsis_svd_parser.SVDParser = object
sys.modules.setdefault("cmsis_svd", cmsis_svd)
sys.modules.setdefault("cmsis_svd.parser", cmsis_svd_parser)

tomli = types.ModuleType("tomli")
tomli.load = tomllib.load
sys.modules.setdefault("tomli", tomli)

from fastdyn import main


def test_run_cli_has_no_introspection_feature_options():
    result = CliRunner().invoke(main.cli, ["run", "--help"])

    assert result.exit_code == 0
    assert "activity-monitor" not in result.output
    assert "introspection" not in result.output


class FakeProcessManager:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.checked = False

    def start_terminator_watcher(self, callback):
        self.started = True
        self.callback = callback

    def stop_terminator_watcher(self):
        self.stopped = True

    def raise_for_terminator_failure(self):
        self.checked = True


class FakeFastDynHandle:
    machines = ["machine0"]

    def __init__(self):
        self.shutdown_called = False

    def run(self, **_kwargs):
        raise KeyboardInterrupt

    def shutdown(self):
        self.shutdown_called = True


def test_loop_starts_helper_terminator_watcher(tmp_path, monkeypatch):
    config = tmp_path / "fastdyn.toml"
    config.write_text("", encoding="utf-8")
    work_dir = tmp_path / "work"
    manager = FakeProcessManager()
    handle = FakeFastDynHandle()

    @contextmanager
    def fake_launch_from_config(config_path, work_path, skip=False):
        assert config_path == str(config)
        assert work_path == str(work_dir)
        assert skip is False
        yield manager

    monkeypatch.setattr(main.runtime_config, "launch_from_config", fake_launch_from_config)
    monkeypatch.setattr(main.runner, "configure_measurement", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main, "_auto_build_fmu", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main.toml_parser, "parser", lambda *_args, **_kwargs: handle)

    # The fake run raises KeyboardInterrupt; loop stops with exit status 130.
    with pytest.raises(SystemExit) as stopped:
        main.loop.callback(
            config=str(config),
            map_file=None,
            work_dir=str(work_dir),
            svd=None,
            persist_work_dir=False,
            fmu=None,
            no_build_fmu=False,
            no_run_processes=False,
        )
    assert stopped.value.code == 130

    assert manager.started is True
    assert manager.stopped is True
    assert manager.checked is True
    manager.callback(None, 0)
    assert handle.shutdown_called is True
