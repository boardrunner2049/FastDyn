"""A run's QMP socket fits in a Unix socket path, whatever the work directory."""

from __future__ import annotations

import os

from fastdyn.targets.qemu_target import UNIX_SOCKET_PATH_MAX, run_qmp_socket


def test_the_socket_is_in_the_work_directory(tmp_path):
    work = tmp_path / "w"

    assert run_qmp_socket(str(work)) == str(work / "qmp.sock")


def test_a_deep_work_directory_gets_a_short_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ("r" * 120)))   # too long itself: skipped
    deep = tmp_path / ("d" * 60) / ("e" * 60) / "fastdyn_work"
    other = tmp_path / ("d" * 60) / ("f" * 60) / "fastdyn_work"

    socket = run_qmp_socket(str(deep))

    assert len(os.fsencode(socket)) <= UNIX_SOCKET_PATH_MAX
    assert os.path.basename(os.path.dirname(socket)) == f"fastdyn-{os.getuid()}"
    assert run_qmp_socket(str(deep)) == socket          # the same run folder, the same socket
    assert run_qmp_socket(str(other)) != socket         # parallel runs do not collide


def test_the_runtime_directory_is_preferred(tmp_path, monkeypatch):
    runtime = tmp_path / "rt"
    if len(os.fsencode(str(runtime))) > 60:
        runtime = "/tmp"                                 # keep the candidate short enough to be used
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    deep = tmp_path / ("d" * 60) / ("e" * 60) / "fastdyn_work"

    assert os.path.dirname(run_qmp_socket(str(deep))) == os.path.join(str(runtime), f"fastdyn-{os.getuid()}")
