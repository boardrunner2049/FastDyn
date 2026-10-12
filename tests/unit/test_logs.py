"""fastdyn.logs and the fastdyn CLI logging options."""

from __future__ import annotations

import io
import logging
import subprocess
import sys

import pytest
from click.testing import CliRunner

from fastdyn import fastdyn_log, logs, main


@pytest.fixture(autouse=True)
def _restore_fastdyn_logger(monkeypatch):
    monkeypatch.delenv(logs.LEVEL_ENV, raising=False)
    logger = logging.getLogger("fastdyn")
    saved = (list(logger.handlers), logger.level)
    yield
    for handler in list(logger.handlers):
        if handler not in saved[0]:
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(saved[1])


def _emit():
    logging.getLogger("fastdyn.core").debug("core debug")
    logging.getLogger("fastdyn.core").info("core info")
    logging.getLogger("fastdyn.targets").debug("targets debug")
    logging.getLogger("fastdyn.targets").warning("targets warning")


def test_importing_fastdyn_installs_no_handlers():
    code = (
        "import logging, fastdyn, fastdyn.runner\n"
        "handlers = logging.getLogger('fastdyn').handlers\n"
        "assert all(isinstance(h, logging.NullHandler) for h in handlers), handlers\n"
        "assert not logging.getLogger().handlers\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_default_level_and_format():
    stream = io.StringIO()
    logs.configure(stream=stream)
    _emit()

    assert stream.getvalue().splitlines() == [
        "fastdyn.core|INFO|  core info",
        "fastdyn.targets|WARNING|  targets warning",
    ]


def test_per_logger_level_overrides_the_default():
    stream = io.StringIO()
    logs.configure(level="WARNING", levels={"fastdyn.targets": "DEBUG"}, stream=stream)
    _emit()

    assert stream.getvalue().splitlines() == [
        "fastdyn.targets|DEBUG|  targets debug",
        "fastdyn.targets|WARNING|  targets warning",
    ]


def test_environment_variable_sets_the_default(monkeypatch):
    monkeypatch.setenv(logs.LEVEL_ENV, "WARNING")
    stream = io.StringIO()
    logs.configure(stream=stream)
    _emit()

    assert "core info" not in stream.getvalue()
    assert "targets warning" in stream.getvalue()


def test_log_file_records_debug_without_color(tmp_path):
    log_file = tmp_path / "fastdyn.log"
    logs.configure(level="WARNING", log_file=str(log_file), stream=io.StringIO())
    _emit()
    for handler in logging.getLogger("fastdyn").handlers:
        handler.flush()

    content = log_file.read_text(encoding="utf-8")
    assert "fastdyn.core|DEBUG|  core debug" in content
    assert "\033[" not in content


def test_configuring_again_replaces_the_previous_handlers():
    first, second = io.StringIO(), io.StringIO()
    logs.configure(stream=first)
    logs.configure(stream=second)
    logging.getLogger("fastdyn.core").info("once")

    assert first.getvalue() == ""
    assert second.getvalue().count("once") == 1


def test_unknown_level_is_rejected():
    with pytest.raises(ValueError, match="unknown log level"):
        logs.configure(level="LOUD", stream=io.StringIO())


def test_deprecated_helpers_still_work():
    assert fastdyn_log.getFastdynLogger() is logging.getLogger("fastdyn")
    fastdyn_log.setLogConfig()  # same as logs.configure()


def _cli(*args):
    return CliRunner(mix_stderr=False).invoke(main.cli, list(args))


def test_cli_logs_go_to_stderr_and_results_to_stdout():
    result = _cli("-v", "timing-summary", "--help")

    assert result.exit_code == 0
    assert "Fastdyn Framework" in result.stderr  # the DEBUG banner
    assert "Usage:" in result.stdout and "Fastdyn Framework" not in result.stdout


@pytest.mark.parametrize(
    "args, banner_shown",  # the banner is a DEBUG message
    [((), False), (("-q",), False), (("--log-level", "DEBUG"), True), (("-v",), True)],
)
def test_cli_level_options(args, banner_shown):
    result = _cli(*args, "timing-summary", "--help")

    assert result.exit_code == 0
    assert ("Fastdyn Framework" in result.stderr) is banner_shown


def test_cli_rejects_conflicting_or_unknown_levels():
    assert _cli("-v", "-q", "timing-summary", "--help").exit_code != 0
    assert _cli("--log-level", "fastdyn.targets=LOUD", "timing-summary", "--help").exit_code != 0


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, logging.INFO),
        ({"level": "ERROR"}, logging.ERROR),
        ({"level": "ERROR", "levels": {"fastdyn.native": "DEBUG"}}, logging.DEBUG),
        ({"levels": {"fastdyn.targets": "DEBUG"}}, logging.INFO),  # other loggers do not count
    ],
)
def test_native_level_follows_the_configuration(kwargs, expected):
    logs.configure(stream=io.StringIO(), **kwargs)

    assert logs.native_level() == expected
