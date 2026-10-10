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


def _loop(tmp_path, persist_work_dir=False, no_build_fmu=False):
    config = tmp_path / "fastdyn.toml"
    config.write_text("", encoding="utf-8")
    return main.loop.callback(
        config=str(config), map_file=None, work_dir=str(tmp_path / "work"), svd=None,
        persist_work_dir=persist_work_dir, fmu=None, no_build_fmu=no_build_fmu, no_run_processes=False,
    )


def _script(monkeypatch, outcomes):
    """Make runner.run_config play `outcomes` (None = clean exit, else raise it)."""
    calls = []

    def fake_run_config(config, work_dir, **kwargs):
        calls.append(kwargs)
        outcome = outcomes[len(calls) - 1]
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(main.runner, "run_config", fake_run_config)
    return calls


def test_loop_restarts_after_run_failures_and_stops_on_setup_errors(tmp_path, monkeypatch):
    calls = _script(monkeypatch, [
        None,                                                     # clean exit
        main.qemu_target.QemuExitError("machine0", 1),            # QEMU failed
        main.runtime_config.HelperExitedError("helper 'sim' exited with status 2"),
        None,                                                     # clean exit
        main.VirtualPreparationError("[CPU.cpu0.plugins.x].y is wrong"),  # config error
    ])

    with pytest.raises(main.click.ClickException, match=r"plugins.x\].y is wrong"):
        _loop(tmp_path)

    # A clean exit starts the next run fresh; a failed run keeps the work dir.
    assert [c["persist_work_dir"] for c in calls] == [False, False, True, True, False]
    assert [c["build_fmu"] for c in calls] == [True, False, False, False, False]
    assert {c["phase_name"] for c in calls} == {"fastdyn.loop.iteration"}


@pytest.mark.parametrize("error", [
    main.runtime_config.RuntimeConfigError("helper 'sim' failed to start"),
    main.runner.RunError("FMU build failed"),
])
def test_loop_stops_on_errors_that_would_repeat(tmp_path, monkeypatch, error):
    calls = _script(monkeypatch, [error])

    with pytest.raises(main.click.ClickException):
        _loop(tmp_path)
    assert len(calls) == 1


def test_loop_honours_persist_work_dir_and_ctrl_c(tmp_path, monkeypatch):
    calls = _script(monkeypatch, [None, KeyboardInterrupt()])

    with pytest.raises(SystemExit) as stopped:
        _loop(tmp_path, persist_work_dir=True, no_build_fmu=True)

    assert stopped.value.code == 130
    assert [c["persist_work_dir"] for c in calls] == [True, False]
    assert [c["build_fmu"] for c in calls] == [False, False]


def test_a_helper_dying_mid_run_raises_helper_exited_error(tmp_path):
    manager = main.runtime_config.RuntimeProcessManager([], tmp_path / "c.toml", tmp_path)
    handle = types.SimpleNamespace(process_config=types.SimpleNamespace(name="sim"))
    manager._terminator_exit = (handle, 3)

    with pytest.raises(main.runtime_config.HelperExitedError, match="helper 'sim' exited with status 3"):
        manager.raise_for_terminator_failure()
    assert issubclass(main.runtime_config.HelperExitedError, main.runtime_config.RuntimeConfigError)
