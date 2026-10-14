"""Resolution order of the QEMU executable when [Machine].qemu_path is optional."""

from __future__ import annotations

import json

import pytest

from fastdyn.targets.qemu_target import QEMU_ENV_VAR, resolve_qemu_binary


def _executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _plugin_with_record(tmp_path, qemu_dir):
    build = tmp_path / "build"
    build.mkdir()
    plugin = build / "libfastdyn.so"
    plugin.write_bytes(b"")
    (build / "fastdyn_build.json").write_text(json.dumps({"qemu_dir": str(qemu_dir)}), encoding="utf-8")
    return str(plugin)


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    monkeypatch.delenv(QEMU_ENV_VAR, raising=False)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))


def test_configured_path_wins_over_everything(tmp_path, monkeypatch):
    plugin = _plugin_with_record(tmp_path, tmp_path / "qemu")
    _executable(tmp_path / "qemu" / "build" / "qemu-system-arm")
    monkeypatch.setenv(QEMU_ENV_VAR, "/env/qemu-system-arm")

    assert resolve_qemu_binary("/cfg/qemu-system-arm", "arm", plugin) == (
        "/cfg/qemu-system-arm",
        "[Machine].qemu_path",
    )


def test_environment_variable_wins_over_build_record(tmp_path, monkeypatch):
    plugin = _plugin_with_record(tmp_path, tmp_path / "qemu")
    _executable(tmp_path / "qemu" / "build" / "qemu-system-arm")
    monkeypatch.setenv(QEMU_ENV_VAR, "/env/qemu-system-arm")

    assert resolve_qemu_binary("", "arm", plugin) == ("/env/qemu-system-arm", f"${QEMU_ENV_VAR}")


def test_build_record_is_used_when_nothing_is_configured(tmp_path):
    plugin = _plugin_with_record(tmp_path, tmp_path / "qemu")
    qemu = _executable(tmp_path / "qemu" / "build" / "qemu-system-arm")

    path, source = resolve_qemu_binary("", "arm", plugin)

    assert path == str(qemu)
    assert source.startswith("build record")


def test_architecture_selects_the_qemu_target(tmp_path):
    plugin = _plugin_with_record(tmp_path, tmp_path / "qemu")
    _executable(tmp_path / "qemu" / "build" / "qemu-system-arm")
    riscv = _executable(tmp_path / "qemu" / "build" / "qemu-system-riscv64")

    assert resolve_qemu_binary("", "riscv64", plugin)[0] == str(riscv)


def test_falls_back_to_path_when_recorded_qemu_is_not_built(tmp_path, monkeypatch):
    plugin = _plugin_with_record(tmp_path, tmp_path / "unbuilt-qemu")
    on_path = _executable(tmp_path / "bin" / "qemu-system-arm")
    monkeypatch.setenv("PATH", str(on_path.parent))

    assert resolve_qemu_binary("", "arm", plugin) == (str(on_path), "PATH")


def test_error_lists_every_location_tried(tmp_path):
    plugin = tmp_path / "build" / "libfastdyn.so"
    plugin.parent.mkdir()
    plugin.write_bytes(b"")

    with pytest.raises(FileNotFoundError) as error:
        resolve_qemu_binary("", "arm", str(plugin))

    message = str(error.value)
    assert "fastdyn_build.json (missing)" in message
    assert "qemu-system-arm on PATH" in message
    assert QEMU_ENV_VAR in message
