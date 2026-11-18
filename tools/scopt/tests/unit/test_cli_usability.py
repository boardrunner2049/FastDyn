"""CLI usability: envlayout lookup by bare hex, import progress and interrupt
cleanup, and the store fsync helper."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from scopt.cli import cli, load_commands
from scopt.importer import import_cache as IC
from tests.synth.rich_tb import rich_tb, write_rich_pair

GATE = "56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90"


@pytest.fixture(autouse=True)
def _cmds():
    load_commands()


@pytest.mark.parametrize("arg", [GATE, f"sha256:{GATE}", GATE.upper()])
def test_envlayout_show_accepts_bare_hex(arg):
    r = CliRunner().invoke(cli, ["envlayout", "show", "--exe-sha256", arg])
    assert r.exit_code == 0, r.output
    assert f'"exe_sha256": "sha256:{GATE}"' in r.output


def test_envlayout_show_bad_sha_is_a_usage_error():
    r = CliRunner().invoke(cli, ["envlayout", "show", "--exe-sha256", "abc"])
    assert r.exit_code == 2
    assert "is not a sha256" in r.output and "guard_layout_unbound" not in r.output


def test_envlayout_show_unknown_names_the_sha():
    r = CliRunner().invoke(cli, ["envlayout", "show", "--exe-sha256", "0" * 64])
    assert r.exit_code == 2
    assert f"guard_layout_unbound: no EnvLayout for sha256:{'0' * 64}" in r.output


def _prep(tmp_path):
    p = tmp_path / "p"
    p.mkdir()
    write_rich_pair([rich_tb(64)], str(p))
    return p


def test_import_progress_lines(tmp_path):
    p = _prep(tmp_path)
    r = CliRunner().invoke(cli, ["import", str(p), "--store", str(tmp_path / "s"),
                                 "--progress"])
    assert r.exit_code == 0, r.output
    assert "import: reading" in r.output and "store: syncing" in r.output
    r = CliRunner().invoke(cli, ["import", str(p), "--store", str(tmp_path / "s2"),
                                 "--no-progress"])
    assert r.exit_code == 0, r.output
    assert "store: syncing" not in r.output


def test_interrupted_import_leaves_no_partial_snapshot(tmp_path, monkeypatch):
    p = _prep(tmp_path)

    def boom(_paths):
        raise KeyboardInterrupt

    monkeypatch.setattr(IC, "_fsync_all", boom)
    r = CliRunner().invoke(cli, ["import", str(p), "--store", str(tmp_path / "s")])
    assert r.exit_code == 130, r.output
    assert "the store holds no partial snapshot" in r.output
    assert list((tmp_path / "s" / "snapshots").iterdir()) == []


def test_fsync_all_syncs_every_file_and_raises(tmp_path, monkeypatch):
    files = []
    for i in range(40):
        f = tmp_path / f"f{i}"
        f.write_bytes(b"x")
        files.append(f)
    seen = []
    real = IC._fsync_path
    monkeypatch.setattr(IC, "_fsync_path", lambda q: (seen.append(q), real(q)))
    IC._fsync_all(files)
    assert sorted(seen) == sorted(files)
    with pytest.raises(OSError):
        IC._fsync_all([tmp_path / "missing"])
