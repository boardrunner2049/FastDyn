"""Portable TOML generation: overlays and resolved tools must survive a round trip."""

from pathlib import Path
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def test_generated_config_uses_toml_overrides_and_ignores_legacy_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FASTDYN_PARAM_FILE", "should-not-be-selected.param")
    overlay = tmp_path / "overlay.toml"
    overlay.write_text('''[FMU.models.quadrotor.parameters]
mass = 0.5
[Run.processes.mission]
command = ["python", "my-mission.py", "my-gains.param"]
''')
    output = tmp_path / "run.toml"
    subprocess.run([
        sys.executable, str(ROOT / "utils/configure_nix.py"),
        "--base", str(ROOT / "configs/copter462.toml"), "--overlay", str(overlay),
        "--output", str(output), "--qemu", "qemu-system-arm",
        "--plugin", "libfastdyn.so", "--compiler", "rumoca",
    ], check=True)
    config = tomllib.loads(output.read_text())
    assert config["FMU"]["compiler"] == "rumoca"
    assert config["FMU"]["models"]["quadrotor"]["parameters"]["mass"] == 0.5
    assert config["Machine"]["qemu_path"] == "qemu-system-arm"
    assert config["CPU"]["cpu0"][0]["plugin_library"] == "libfastdyn.so"
    assert config["Run"]["processes"]["mission"]["command"][-1] == "my-gains.param"
    assert "${" not in output.read_text()
    assert "should-not-be-selected" not in output.read_text()
    assert Path(config["Machine"]["qmp_socket"]).parent == tmp_path / "run"
