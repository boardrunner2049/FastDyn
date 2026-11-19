"""Private file-backed RAM can be located on tmpfs without moving evidence."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from scopt.run import launcher


TOML = """\
[Machine]
supercache = "cache/original.sc"
[Memory.main]
id = "main"
backend = "file"
memory_size = "512M"
share = false
[[Memory.secondary]]
id = "input"
backend = "file"
memory_size = "4K"
share = false
"""


@pytest.fixture
def tmpfs():
    root = Path("/dev/shm")
    try:
        launcher._tmpfs_root(root)
    except launcher.RunDirError:
        pytest.skip("requires tmpfs")
    with TemporaryDirectory(prefix="scopt-ram-test-", dir=root) as d:
        yield Path(d)


def test_private_tmpfs_sparse_and_cleanup_on_failure(tmp_path, tmpfs, monkeypatch):
    config = tmp_path / "base.toml"
    config.write_text(TOML)
    monkeypatch.setenv("SCOPT_RAM_ROOT", str(tmpfs))
    a = launcher.create_run_dir(config, tmp_path / "first", qemu=launcher.FASTDYN_DEFAULT_QEMU)
    b = launcher.create_run_dir(config, tmp_path / "other", qemu=launcher.FASTDYN_DEFAULT_QEMU)
    assert a.ram.parent == b.ram.parent == tmpfs
    assert a.ram != b.ram and a.cache.parent == a.root / "cache"
    main, inp = map(Path, launcher.reset_ram(a))
    assert main.parent == inp.parent == a.ram
    assert main.stat().st_size == 512 << 20
    assert main.stat().st_blocks == 0
    seed = tmp_path / "seed"
    seed.write_bytes(b"seed")
    ns = launcher.load_inputs(a, {"input": seed})
    assert ns["input"]["bank_file"] == str(inp)
    assert inp.read_bytes() == b"seed" + bytes(4092)

    def fail(*_args, **_kwargs):
        raise RuntimeError("launch failed")

    monkeypatch.setattr(launcher, "_launch", fail)
    with pytest.raises(RuntimeError, match="launch failed"):
        launcher.launch(a)
    assert not a.ram.exists() and b.ram.is_dir()
    assert len(launcher.reset_ram(a)) == 2
    launcher._cleanup_tmpfs_ram(a)
    launcher._cleanup_tmpfs_ram(b)


def test_ram_root_does_not_silently_change_share_mode(tmp_path, tmpfs):
    config = tmp_path / "base.toml"
    config.write_text(TOML.replace("share = false", "share = true", 1))
    with pytest.raises(launcher.RunDirError, match="ram_requires_private_files"):
        launcher.create_run_dir(config, tmp_path / "trial", qemu=launcher.FASTDYN_DEFAULT_QEMU,
                                ram_root=tmpfs)
    with pytest.raises(launcher.RunDirError, match="ram_root_not_tmpfs"):
        launcher.create_run_dir(config, tmp_path / "disk", qemu=launcher.FASTDYN_DEFAULT_QEMU,
                                ram_root=tmp_path)


def test_external_ram_paths_stay_restricted(tmp_path, tmpfs):
    config = tmp_path / "base.toml"
    config.write_text(TOML)
    rd = launcher.create_run_dir(config, tmp_path / "trial", qemu=launcher.FASTDYN_DEFAULT_QEMU,
                                 ram_root=tmpfs)
    target = tmp_path / "unowned"
    target.write_text("untouched")
    rd.toml.write_text(rd.toml.read_text().replace(str(rd.ram / "main.ram"), str(target)))
    with pytest.raises(launcher.RunDirError, match="ram_outside_run_dir"):
        launcher.reset_ram(rd)
    assert target.read_text() == "untouched"
