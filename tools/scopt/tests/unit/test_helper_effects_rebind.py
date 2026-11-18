"""``scopt helper-effects rebind``: copy a helper effect table to a rebuilt QEMU only when
the helper sources it was checked against are byte-identical in the new tree."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from scopt.analysis import helper_effects as HE
from scopt.analysis.helper_effects import (HelperEffectsError, SourceMismatch,
                                           find_helper_effects, read_helper_effects,
                                           rebind_table, recorded_source_hashes)
from scopt.cli import cli, load_commands

GATE = "sha256:56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90"


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


@pytest.fixture
def tree(tmp_path):
    """A fake QEMU tree whose helper sources match a synthetic table, plus an 'exe'."""
    gate = find_helper_effects(GATE)
    files = list(gate.qemu_source["files"])
    src = tmp_path / "qemu"
    for i, f in enumerate(files):
        p = src / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"/* {f} {i} */\n".encode())
    hashes = {f: _sha((src / f).read_bytes()) for f in files}
    old_exe = "sha256:" + "1" * 64
    table = HE.HelperEffectTable.build(
        old_exe, gate.rows, {"commit": "0" * 40, "files": files, "file_sha256": hashes})
    tdir = tmp_path / "tables"
    tdir.mkdir()
    (tdir / f"{'1' * 64}.json").write_text(HE.helper_effects_text(table))
    (src / "build").mkdir()
    exe = src / "build" / "qemu-system-arm"
    exe.write_bytes(b"\x7fELF new build")
    return {"src": src, "table": table, "tdir": tdir, "exe": exe, "files": files,
            "new": _sha(exe.read_bytes())}


def test_packaged_tables_record_source_hashes():
    for p in HE.packaged_helper_effects():
        t = read_helper_effects(p)
        rec = recorded_source_hashes(t)
        assert set(rec) == set(t.qemu_source["files"]) and all(rec.values()), p


def test_rebind_match_keeps_rows_and_recomputes_id(tree):
    t = rebind_table(tree["table"], tree["new"], tree["src"])
    assert t.exe_sha256 == tree["new"] and t.rows == tree["table"].rows
    assert t.qemu_source["rebound_from"] == tree["table"].exe_sha256
    assert t.table_id != tree["table"].table_id
    assert read_helper_effects(t.to_json()).table_id == t.table_id


def test_rebind_mismatch_lists_differing_files(tree):
    a, b = tree["files"][0], tree["files"][3]
    (tree["src"] / a).write_text("changed\n")
    (tree["src"] / b).unlink()
    with pytest.raises(SourceMismatch) as ei:
        rebind_table(tree["table"], tree["new"], tree["src"])
    assert set(ei.value.differing) == {a, b}
    assert ei.value.differing[b][1] is None


def test_rebind_without_hashes_uses_the_recorded_commit(tmp_path, tree):
    src = tree["src"]
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "add", *tree["files"]], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "base"], check=True)
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    legacy = HE.HelperEffectTable.build(tree["table"].exe_sha256, tree["table"].rows,
                                        {"commit": head, "files": tree["files"]})
    t = rebind_table(legacy, tree["new"], src)
    assert set(t.qemu_source["file_sha256"]) == set(tree["files"])
    (src / tree["files"][1]).write_text("edited after the commit\n")
    with pytest.raises(SourceMismatch) as ei:
        rebind_table(legacy, tree["new"], src)
    assert list(ei.value.differing) == [tree["files"][1]]
    no_commit = HE.HelperEffectTable.build(tree["table"].exe_sha256, tree["table"].rows,
                                           {"commit": "f" * 40, "files": tree["files"]})
    with pytest.raises(HelperEffectsError):
        rebind_table(no_commit, tree["new"], src)


def _run(args, env):
    load_commands()
    return CliRunner().invoke(cli, args, env=env, catch_exceptions=False)


def test_cli_rebind_match_writes_to_search_path(tree, tmp_path):
    out_dir = tmp_path / "mine"
    env = {"SCOPT_HELPER_EFFECTS_PATH": f"{out_dir}:{tree['tdir']}"}
    HE._FOUND.clear()
    try:
        r = _run(["helper-effects", "rebind", "--from-exe-sha", "1" * 64,
                  "--exe", str(tree["exe"])], env)  # --qemu-src inferred from build/
        assert r.exit_code == 0, r.output
        p = out_dir / f"{tree['new'].split(':')[1]}.json"
        doc = json.loads(p.read_text())
        assert doc["exe_sha256"] == tree["new"]
        assert doc["qemu_source"]["rebound_from"] == tree["table"].exe_sha256
        assert read_helper_effects(p).rows == tree["table"].rows
    finally:
        HE._FOUND.clear()


def test_cli_rebind_mismatch_writes_nothing(tree, tmp_path):
    (tree["src"] / tree["files"][2]).write_text("different\n")
    out = tmp_path / "out.json"
    env = {"SCOPT_HELPER_EFFECTS_PATH": str(tree["tdir"])}
    HE._FOUND.clear()
    try:
        r = _run(["helper-effects", "rebind", "--from-exe-sha", "sha256:" + "1" * 64,
                  "--exe", str(tree["exe"]), "--qemu-src", str(tree["src"]),
                  "--out", str(out)], env)
        assert r.exit_code == 1
        assert tree["files"][2] in r.output and "helper_sources_differ" in r.output
        assert not out.exists()
    finally:
        HE._FOUND.clear()


def test_cli_rebind_unknown_source_table(tree):
    r = _run(["helper-effects", "rebind", "--from-exe-sha", "2" * 64,
              "--exe", str(tree["exe"]), "--qemu-src", str(tree["src"]),
              "--out", "/nonexistent/x.json"], {})
    assert r.exit_code == 2
