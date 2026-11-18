"""``scopt meta dump`` / ``merge-check`` (WP4)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from scopt.cli import cli, load_commands
from scopt.formats.enums import ChunkType
from tests.synth import builders as B


def _run(*args):
    load_commands()
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def test_meta_dump_jsonl(tmp_path):
    units = B.simple_units(3)
    p = tmp_path / "x.scm"
    B.build_scm(units, helpers=[{"name": "foo"}], path=str(p))
    r = _run("meta", "dump", "--jsonl", str(p))
    assert r.exit_code == 0
    rows = [json.loads(line) for line in r.output.splitlines()]
    assert rows[0]["kind"] == "file_header" and rows[0]["magic"] == "QSCMETA1"
    types = [row.get("type") for row in rows if row["kind"] == "chunk"]
    assert types == ["RUN_INFO", "HELPER", "TB", "MAPS", "RAMBLOCKS", "TB", "TB", "MAPS", "END"]
    tb = next(row["tb"] for row in rows if row.get("type") == "TB")
    assert tb["slot"] == 0 and tb["record"]["checksum_ok"] is True
    assert tb["sites"][0]["kind"] == "LABEL_JCC" and tb["sites"][0]["insn_idx"] is None
    assert tb["vaddr"].startswith("0x") and tb["flags"] == ["irq_check"]
    assert rows[-1]["kind"] == "summary" and rows[-1]["tbs"] == 3
    assert rows[-1]["incomplete"] is False and rows[-1]["rejected_chunks"] == 0
    # filters
    r = _run("meta", "dump", "--jsonl", "--slot", "2", "--no-arrays", str(p))
    chunk_rows = [json.loads(x) for x in r.output.splitlines() if '"kind":"chunk"' in x]
    assert len(chunk_rows) == 1 and chunk_rows[0]["tb"]["slot"] == 2
    assert "sites" not in chunk_rows[0]["tb"]
    r = _run("meta", "dump", "--jsonl", "--type", "end", "--type", str(int(ChunkType.HELPER)),
             str(p))
    assert [json.loads(x)["type"] for x in r.output.splitlines()
            if '"kind":"chunk"' in x] == ["HELPER", "END"]


def test_meta_dump_strict_and_errors(tmp_path):
    x = B.build_scm(B.simple_units(3))
    good = tmp_path / "g.scm"
    good.write_bytes(x)
    torn = tmp_path / "t.scm"
    torn.write_bytes(B.truncate_chunk(x, 4, 40))
    bad = tmp_path / "b.scm"
    bad.write_bytes(B.flip_byte(x, 0))
    assert _run("meta", "dump", "--strict", str(good)).exit_code == 0
    r = _run("meta", "dump", "--strict", str(torn))
    assert r.exit_code == 1 and "truncated (prefix)" in r.output
    r = _run("meta", "dump", "--jsonl", str(bad))
    assert r.exit_code == 0 and json.loads(r.output)["kind"] == "file_error"


def test_meta_merge_check(tmp_path):
    units = B.simple_units(4)
    a, b, c = tmp_path / "a.scm", tmp_path / "b.scm", tmp_path / "c.scm"
    B.build_scm(units[:2], path=str(a), all_units=units)
    B.build_scm(units[2:], first_slot=2, path=str(b), all_units=units)
    B.build_scm(B.simple_units(2, start=0x2000), path=str(c))
    r = _run("meta", "merge-check", str(a), str(b))
    assert r.exit_code == 0 and json.loads(r.output)["slots"] == 4
    r = _run("meta", "merge-check", str(a), str(c))
    assert r.exit_code == 1 and "snapshot_merge_conflict: slot 0" in r.output
