"""The C logger (include/fastdyn/log.h, utils/log.c), compiled with gcc and run."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not found")

PROGRAM = r"""
#define FD_LOG_SUBSYSTEM "dev"
#include <fastdyn/log.h>
#include <stdlib.h>

int main(int argc, char **argv) {
    if (argc > 1) {
        fastdyn_log_set_level(atoi(argv[1]));
    }
    FD_LOG_ERROR("error %d", 1);
    FD_LOG_WARN("warn %s", "two");
    FD_LOG_INFO("info");
    FD_LOG_DEBUG("debug");
    FD_LOG_TRACE("trace");
    fastdyn_log(FD_LOG_LEVEL_INFO, NULL, "no subsystem");
    return 0;
}
"""


def _build(tmp_path, *defines):
    source = tmp_path / "main.c"
    source.write_text(PROGRAM)
    binary = tmp_path / "logtest"
    subprocess.run(
        ["gcc", "-std=gnu11", "-Wall", "-Werror", *defines, f"-I{REPO / 'include'}",
         str(source), str(REPO / "utils" / "log.c"), "-o", str(binary)],
        check=True,
    )
    return binary


def _stderr(binary, *args):
    result = subprocess.run([str(binary), *args], capture_output=True, text=True, check=True)
    assert result.stdout == ""
    return result.stderr.splitlines()


def test_default_level_is_info(tmp_path):
    assert _stderr(_build(tmp_path)) == [
        "fastdyn.dev|ERROR|  error 1",
        "fastdyn.dev|WARNING|  warn two",
        "fastdyn.dev|INFO|  info",
        "fastdyn.core|INFO|  no subsystem",
    ]


def test_runtime_level_filters_messages(tmp_path):
    assert _stderr(_build(tmp_path), "40") == ["fastdyn.dev|ERROR|  error 1"]


def test_debug_and_trace_need_debug_print(tmp_path):
    binary = _build(tmp_path, "-DDEBUG_PRINT=1")

    lines = _stderr(binary, "5")
    assert "fastdyn.dev|DEBUG|  debug" in lines
    assert "fastdyn.dev|TRACE|  trace" in lines
    assert _stderr(binary, "10")[-2:] == ["fastdyn.dev|DEBUG|  debug", "fastdyn.core|INFO|  no subsystem"]
