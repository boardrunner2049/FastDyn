"""QEMU's exit status reaches the caller instead of being reported as success."""

from __future__ import annotations

import pytest

import fastdyn.fastdyn as fastdyn_module
import fastdyn.virtual_preprocessing as virtual_preprocessing
from fastdyn.targets import qemu_target
from fastdyn.targets.qemu_target import QemuExitError


@pytest.mark.parametrize("command, expected", [(["true"], 0), (["false"], 1)])
def test_start_execution_returns_qemu_exit_code(command, expected):
    assert qemu_target.start_execution(command, False, None, "firmware.elf") == expected


@pytest.fixture
def machine_runner(monkeypatch, tmp_path):
    """A Fastdyn handle whose QEMU command is replaced by a fake executable."""
    monkeypatch.setattr(virtual_preprocessing, "prepare_run_preprocessors", lambda *_: None)
    monkeypatch.setattr(fastdyn_module, "compile_device_routing", lambda *_, **__: None)
    monkeypatch.setattr(fastdyn_module, "_python_endpoints_for_machine", lambda _machine: [])

    def run_with(command, before_run=None):
        monkeypatch.setattr(
            qemu_target, "setup_qemu", lambda _machine, _out: (command, None, False, "firmware.elf")
        )
        handle = fastdyn_module.Fastdyn()
        handle.create_machine("machine0", "generic-cortexm")
        if before_run is not None:
            before_run(handle)
        return handle.run(target="qemu", machine_name="machine0", out_path=str(tmp_path))

    return run_with


def test_successful_qemu_returns_zero(machine_runner):
    assert machine_runner(["true"]) == 0


def test_failing_qemu_raises_with_details(machine_runner, tmp_path):
    with pytest.raises(QemuExitError) as error:
        machine_runner(["false"])

    assert error.value.machine_name == "machine0"
    assert error.value.exit_code == 1
    assert "exited with code 1" in str(error.value)
    assert str(tmp_path) in str(error.value)


def test_requested_shutdown_is_not_a_failure(machine_runner, monkeypatch):
    # Simulate the terminator watcher calling shutdown() while QEMU runs:
    # the fake QEMU "dies" with a non-zero code after shutdown was requested.
    def start_after_shutdown(*_args, **_kwargs):
        handle.shutdown("machine0")
        return -9

    handle = None

    def capture(h):
        nonlocal handle
        handle = h
        monkeypatch.setattr(qemu_target, "stop_qemu_process", lambda *_a, **_k: None)
        monkeypatch.setattr(qemu_target, "start_execution", start_after_shutdown)

    assert machine_runner(["false"], before_run=capture) == -9


def test_ctrl_c_stops_qemu_and_propagates(monkeypatch):
    class InterruptedQemu:
        pid = 4242

        def __init__(self, *_args, **_kwargs):
            self.waits = []

        def wait(self, timeout=None):
            self.waits.append(timeout)
            if len(self.waits) == 1:
                raise KeyboardInterrupt  # Ctrl-C arrives while FastDyn waits for QEMU
            return 0  # QEMU then exits on its own SIGINT

    procs = []
    monkeypatch.setattr(
        qemu_target.subprocess, "Popen", lambda *a, **k: procs.append(InterruptedQemu()) or procs[-1]
    )

    with pytest.raises(KeyboardInterrupt):
        qemu_target.start_execution(["qemu-system-arm"], False, None, "firmware.elf", exit_timeout_ms=2000)

    assert procs[0].waits == [None, 2.0]  # waited for QEMU to stop before re-raising


def test_signal_exit_is_described():
    error = QemuExitError("machine0", -11)
    assert "killed by signal 11" in str(error)


def test_shutdown_lets_qemu_exit_before_killing_it(monkeypatch):
    """SIGINT first (QEMU writes bbl.txt etc. at exit), SIGKILL only if it is still running."""
    signals, alive = [], {111: True, 222: True}

    def fake_kill(pid, sig):
        if sig == 0:
            if not alive.get(pid):
                raise ProcessLookupError
            return
        signals.append((pid, sig))
        if sig == qemu_target.signal.SIGINT and pid == 111:
            alive[111] = False  # exits on SIGINT
        if sig == qemu_target.signal.SIGKILL:
            alive[pid] = False

    monkeypatch.setattr(qemu_target, "_pids_using_port", lambda port: [111, 222])
    monkeypatch.setattr(qemu_target.os, "kill", fake_kill)
    monkeypatch.setattr(qemu_target.os, "isatty", lambda fd: False)

    qemu_target.stop_qemu_process(5555, timeout_ms=200)

    sigint, sigkill = qemu_target.signal.SIGINT, qemu_target.signal.SIGKILL
    assert signals == [(111, sigint), (222, sigint), (222, sigkill)]


def test_shutdown_does_not_kill_a_qemu_that_exits(monkeypatch):
    signals = []
    monkeypatch.setattr(qemu_target, "_pids_using_port", lambda port: [333])

    def fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError  # gone after SIGINT
        signals.append(sig)

    monkeypatch.setattr(qemu_target.os, "kill", fake_kill)

    qemu_target.stop_qemu_process(5555, timeout_ms=200)

    assert signals == [qemu_target.signal.SIGINT]
