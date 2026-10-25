from pathlib import Path

import pytest

from fastdyn import runtime_config


def write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_reports_duplicate_toml_keys_with_location_table_and_correction(tmp_path):
    config = write_config(
        tmp_path / "fastdyn.toml",
        """[Machine]
monitor_port = 0
enable_gdb = true
monitor_port = 1234
""",
    )

    with pytest.raises(runtime_config.RuntimeConfigError) as error:
        runtime_config.validate_config(config)

    message = str(error.value)
    assert f"{config}:4:" in message
    assert "duplicate key 'monitor_port' in [Machine]" in message
    assert "remove the duplicate or edit the original setting" in message


def test_loads_named_run_processes(tmp_path):
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    config = write_config(
        tmp_path / "fastdyn.toml",
        """
[Run]
cwd = "helpers"

[Run.env]
FAST = "DYN"

[Run.processes.mission]
command = ["python3", "mission.py"]
start_delay_sec = 2.5

[Run.processes.disabled]
enabled = false
command = "never"
""",
    )

    processes = runtime_config.load_processes(config, repo_root=tmp_path)

    assert [process.name for process in processes] == ["mission"]
    process = processes[0]
    assert process.command == ["python3", "mission.py"]
    assert process.cwd == helpers
    assert process.env == {"FAST": "DYN"}
    assert process.ready_message is None
    assert process.start_delay_sec == 2.5


def test_expands_run_process_environment_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("FASTDYN_TEST_PORT", raising=False)
    config = write_config(
        tmp_path / "fastdyn.toml",
        """
[Run.processes.viewer]
command = ["serve", "--port=${FASTDYN_TEST_PORT:-5010}", "$FASTDYN_MISSING"]
ready_message = "open http://127.0.0.1:${FASTDYN_TEST_PORT:-5010}/"
""",
    )

    processes = runtime_config.load_processes(config, repo_root=tmp_path)

    assert len(processes) == 1
    assert processes[0].command == ["serve", "--port=5010", "$FASTDYN_MISSING"]
    assert processes[0].ready_message == "open http://127.0.0.1:5010/"


def test_expands_run_process_environment_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("FASTDYN_TEST_PORT", "6200")
    config = write_config(
        tmp_path / "fastdyn.toml",
        """
[Run.processes.viewer]
command = ["serve", "--port=${FASTDYN_TEST_PORT:-5010}"]
ready_message = "open http://127.0.0.1:${FASTDYN_TEST_PORT:-5010}/"
""",
    )

    processes = runtime_config.load_processes(config, repo_root=tmp_path)

    assert processes[0].command == ["serve", "--port=6200"]
    assert processes[0].ready_message == "open http://127.0.0.1:6200/"


def test_loads_rumoca_webviewer_process(tmp_path):
    rumoca_dir = tmp_path / "third_party" / "common" / "rumoca"
    rumoca_dir.mkdir(parents=True)
    (rumoca_dir / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    config = write_config(
        tmp_path / "fastdyn.toml",
        """
[Rumoca]
enabled = true
config = "rumoca_sim.toml"
features = ["lockstep"]

[Rumoca.webviewer]
http_port = 9090
ws_port = 9091
scene = "scene.js"
debug = true
""",
    )

    processes = runtime_config.load_processes(config, repo_root=tmp_path)

    assert len(processes) == 1
    process = processes[0]
    assert process.name == "rumoca"
    assert process.cwd == rumoca_dir
    assert process.background is True
    assert process.shell is False
    assert process.ready_message == "Rumoca web viewer: http://127.0.0.1:9090"
    assert process.command == [
        "cargo",
        "run",
        "-p",
        "rumoca",
        "--features",
        "lockstep",
        "--release",
        "--",
        "lockstep",
        "run",
        "-c",
        str(tmp_path / "rumoca_sim.toml"),
        "--scene",
        str(tmp_path / "scene.js"),
        "--http-port",
        "9090",
        "--ws-port",
        "9091",
        "--debug",
    ]


def test_rumoca_webviewer_ports_can_come_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FASTDYN_RUMOCA_HTTP_PORT", "16104")
    monkeypatch.setenv("FASTDYN_RUMOCA_WS_PORT", "16105")
    rumoca_dir = tmp_path / "third_party" / "common" / "rumoca"
    rumoca_dir.mkdir(parents=True)
    (rumoca_dir / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    config = write_config(
        tmp_path / "fastdyn.toml",
        """
[Rumoca]
enabled = true
config = "rumoca_sim.toml"
features = ["lockstep"]

[Rumoca.webviewer]
http_port = 9090
ws_port = 9091
""",
    )

    processes = runtime_config.load_processes(config, repo_root=tmp_path)

    assert processes[0].ready_message == "Rumoca web viewer: http://127.0.0.1:16104"
    assert "--http-port" in processes[0].command
    assert "16104" in processes[0].command
    assert "--ws-port" in processes[0].command
    assert "16105" in processes[0].command


def test_launch_from_config_passes_standard_environment(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    marker = tmp_path / "marker.txt"
    config = write_config(
        tmp_path / "fastdyn.toml",
        f"""
[Run.processes.write_marker]
background = false
command = ["sh", "-c", "printf '%s\\n%s\\n' \\"$FASTDYN_CONFIG\\" \\"$FASTDYN_WORK_DIR\\" > {marker}"]
""",
    )

    with runtime_config.launch_from_config(config, work_dir):
        pass

    assert marker.read_text(encoding="utf-8").splitlines() == [
        str(config.resolve()),
        str(work_dir.resolve()),
    ]


def test_bridge_boot_resolves_repository_relative_path(tmp_path, monkeypatch):
    import os

    monkeypatch.delenv("FASTDYN_BRIDGE_BOOT", raising=False)
    monkeypatch.setenv("FASTDYN_REPO_ROOT", str(tmp_path))
    boot = tmp_path / "boot.nsh"
    boot.write_text("ver all\n")
    configs = tmp_path / "configs"
    configs.mkdir()
    config = write_config(configs / "px4.toml", '[Run]\nbridge_boot = "boot.nsh"\n')
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    runtime_config.validate_config(config)
    with runtime_config.bridge_boot_environment(config):
        assert os.environ["FASTDYN_BRIDGE_BOOT"] == str(boot)
    assert "FASTDYN_BRIDGE_BOOT" not in os.environ


@pytest.mark.parametrize("override", ["custom.nsh", ""])
def test_bridge_boot_environment_override_and_shell_mode(tmp_path, monkeypatch, override):
    import os

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FASTDYN_BRIDGE_BOOT", override)
    (tmp_path / "custom.nsh").write_text("ver all\n")
    config = write_config(tmp_path / "px4.toml", '[Run]\nbridge_boot = "not-used.nsh"\n')

    runtime_config.validate_config(config)
    with runtime_config.bridge_boot_environment(config):
        assert os.environ["FASTDYN_BRIDGE_BOOT"] == (str(tmp_path / override) if override else "")
    assert os.environ["FASTDYN_BRIDGE_BOOT"] == override


def test_bridge_boot_rejects_nonstring_setting(tmp_path, monkeypatch):
    monkeypatch.delenv("FASTDYN_BRIDGE_BOOT", raising=False)
    config = write_config(tmp_path / "px4.toml", '[Run]\nbridge_boot = true\n')
    with pytest.raises(runtime_config.RuntimeConfigError, match="bridge_boot.*string"):
        runtime_config.validate_config(config)
