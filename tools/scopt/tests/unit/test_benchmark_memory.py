"""Default placement belongs only to compatible benchmark configurations."""

import os
from pathlib import Path
import re
import tomllib

import pytest

from scopt.run.benchmark_memory import benchmark_ram, benchmark_ram_override
from scopt.run.toml_rewrite import memory_banks


@pytest.mark.parametrize("name", ("host-inline", "host-inline-two-rbit"))
def test_fresh_native_fixture_defaults(name):
    template = Path(__file__).resolve().parents[2] / "fixtures" / name / "fastdyn.toml.in"
    text = re.sub(r"@[A-Z_]+@", '"value"', template.read_text())
    config = tomllib.loads(text)
    assert config["Machine"]["log_options"] == "none"
    banks = memory_banks(config)
    assert banks and all(bank.is_file and not bank.share for bank in banks)


@pytest.mark.parametrize("backend, share, compatible", [
    ("file", "false", True), ("file", "true", False), ("ram", "false", False),
])
def test_default_only_for_private_file_banks(tmp_path, backend, share, compatible):
    config = tmp_path / "run.toml"
    text = f'''[Memory.main]
id = "ram0"
memory_size = "64K"
base_address = "0x20000000"
backend = "{backend}"
share = {share}

[Machine]
log_options = "in_asm,op"
'''
    config.write_text(text)
    assert benchmark_ram_override(config, "/dev/shm", {}) == ("/dev/shm" if compatible else None)
    assert benchmark_ram_override(config, None, {}) is None
    assert benchmark_ram_override(config, "/dev/shm", {"SCOPT_RAM_ROOT": ""}) is None
    assert benchmark_ram_override(config, "/dev/shm", {"SCOPT_RAM_ROOT": "/custom"}) is None
    assert config.read_text() == text


def test_context_restores_environment_on_failure(tmp_path, monkeypatch):
    config = tmp_path / "run.toml"
    config.write_text('''[Memory.main]
memory_size = "64K"
base_address = "0x20000000"
backend = "file"
share = false
''')
    monkeypatch.delenv("SCOPT_RAM_ROOT", raising=False)
    with pytest.raises(RuntimeError):
        with benchmark_ram(config, "/dev/shm") as root:
            assert root == os.environ["SCOPT_RAM_ROOT"] == "/dev/shm"
            raise RuntimeError("stop")
    assert "SCOPT_RAM_ROOT" not in os.environ
    monkeypatch.setenv("SCOPT_RAM_ROOT", "")
    with benchmark_ram(config, "/dev/shm") as root:
        assert root is None
    assert os.environ["SCOPT_RAM_ROOT"] == ""


@pytest.mark.parametrize("fail_oracle", [False, True])
def test_prepare_releases_standalone_oracle_tmpfs(tmp_path, monkeypatch, fail_oracle):
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace

    from scopt.run import deploy, launcher, prepare

    try:
        root = launcher._tmpfs_root("/dev/shm")
    except launcher.RunDirError:
        pytest.skip("requires tmpfs")
    config = tmp_path / "base.toml"
    config.write_text("""[Machine]
supercache = "cache/test.sc"
supercache_flat = false

[Memory.main]
id = "ram"
backend = "file"
memory_size = "64K"
share = false
""")
    oracle_ram = []

    def cold(rd, *, extra_env, **kwargs):
        rd.cache.write_bytes(b"cache")
        Path(extra_env["QEMU_SC_CAPTURE"]).write_bytes(b"capture")
        log = rd.root / "stdout.log"
        log.write_text(prepare.CAPTURE_BANNER)
        launcher._cleanup_tmpfs_ram(rd)
        return {"stdout_log": str(log), "exit_code": 0, "timed_out": False,
                "qemu": "qemu-system-arm", "qemu_sha256": "sha256:" + "1" * 64}

    def oracle(rd, **kwargs):
        backing, = launcher.reset_ram(rd)
        oracle_ram.append(Path(backing).parent)
        assert Path(backing).is_file()
        (rd.root / "qemu_argv.json").write_text("{}")
        if fail_oracle:
            raise deploy.DeployError("oracle_failed", "fixture failure")
        return {"argv": ["qemu-system-arm"], "supercache": {}}

    monkeypatch.setattr(prepare, "launch", cold)
    monkeypatch.setattr(prepare, "read_sc",
                        lambda _: SimpleNamespace(header=SimpleNamespace(record_count=1), valid_slots=[0]))
    monkeypatch.setattr(deploy, "oracle", oracle)
    with TemporaryDirectory(dir=root) as mount:
        monkeypatch.setenv("SCOPT_RAM_ROOT", mount)
        out = tmp_path / "prep"
        result = prepare.prepare(config, out, qemu=launcher.FASTDYN_DEFAULT_QEMU)
        assert result["verdict"] == ("FAIL" if fail_oracle else "PASS")
        assert (out / "oracle/qemu_argv.json").is_file()
        saved, = oracle_ram
        assert not saved.exists()
