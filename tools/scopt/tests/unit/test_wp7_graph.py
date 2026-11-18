"""WP7 graph.json (§4.4, T-B1-4 synthetic part), analyses and CLI."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from scopt.analysis.api import ScopeRef
from scopt.analysis.cfg import ANALYSES as CFG_ANALYSES
from scopt.analysis.layout import ANALYSES as LAYOUT_ANALYSES
from scopt.analysis.refs import ANALYSES as REFS_ANALYSES
from scopt.canon import sha256_id
from scopt.cli import cli, load_commands
from scopt.graph import (check_graph, export_graph, graph_bytes, load_graph, roundtrip_ok,
                         validate_graph)
from scopt.importer.import_cache import import_snapshot
from tests.synth import builders as B
from tests.synth.rich_tb import rich_tb, with_meta, write_rich_pair


def _pair_snapshot(tmp: Path, *, dest_present: bool = True):
    k0 = B.DEFAULT_KEY
    k1 = B.DEFAULT_KEY.__class__(k0.page_addr0 + 8, None, k0.flags, k0.cs_base, k0.cflags)
    v1 = 0x08000000 | (k1.page_addr0 & 0x3FF)
    a = rich_tb(64, key=k0, goto_dest=v1 if dest_present else v1 + 0x80)
    b = rich_tb(B.round_up(B.resolve_unit(a).end_offset, 64), key=k1)
    tmp.mkdir(parents=True, exist_ok=True)
    write_rich_pair([a, b], str(tmp))
    return import_snapshot(str(tmp), store=str(tmp / "store"))


def test_two_tb_goto_tb_pair_roundtrip(tmp_path):
    res = _pair_snapshot(tmp_path / "p")
    snap = res.snapshot
    a, b = snap.units()
    doc = export_graph(snap)
    data = graph_bytes(doc)
    assert roundtrip_ok(data)
    assert graph_bytes(load_graph(data)) == data
    assert check_graph(load_graph(data), snap) == []
    gt = [e for e in doc["edges"] if e["src"] == a.uid and e["exit"]["kind"] == "goto_tb"]
    assert len(gt) == 1
    e = gt[0]
    assert e["candidates"] == [b.uid] and e["resolution"] == "candidates_unique"
    assert e["dest_ram"] == hex(b.key.page_addr0) and "dst" not in e
    for e in doc["edges"]:
        assert "dst" not in e
        if e["exit"]["kind"] in ("goto_ptr", "exit_null", "exit_requested"):
            assert e["resolution"] == "dynamic" and e["candidates"] == []
        assert e["exit"].get("id"), e  # every edge has a captured exit site
    # the subset export of just the pair
    sub = export_graph(snap, units=[a.uid, b.uid])
    assert roundtrip_ok(graph_bytes(sub)) and sub["scope"] == "subset"
    assert check_graph(sub, snap) == []


def test_seeded_destination_not_in_snapshot(tmp_path):
    snap = _pair_snapshot(tmp_path / "p", dest_present=False).snapshot
    doc = export_graph(snap)
    a = snap.units()[0]
    e = next(e for e in doc["edges"] if e["src"] == a.uid and e["exit"]["kind"] == "goto_tb")
    assert e["resolution"] == "not_in_snapshot" and e["candidates"] == []
    assert validate_graph(doc) == []


def test_profile_observed_edges(tmp_path):
    snap = _pair_snapshot(tmp_path / "p").snapshot
    a, b = snap.units()
    prof = {"profile_id": sha256_id(b"p"), "snapshot_id": snap.snapshot_id, "edges": [
        {"src": a.uid, "dst": b.uid, "kind": "chain0", "flags": [], "phase": 0, "count": 750,
         "status": "exact"},
        {"src": b.uid, "dst": a.uid, "kind": "lookup_hit", "flags": [], "phase": 1, "count": 3,
         "status": "exact"}]}
    doc = export_graph(snap, profiles=[prof])
    data = graph_bytes(doc)
    assert roundtrip_ok(data)
    gt = next(e for e in doc["edges"] if e["src"] == a.uid and e["exit"]["kind"] == "goto_tb")
    assert gt["observed"] == [{"dst": b.uid, "kind": "chain0", "count": 750, "status": "exact",
                               "phase": 0, "profile_id": prof["profile_id"]}]
    obs = [e for e in doc["edges"] if e["exit"]["kind"] == "observed"]
    assert len(obs) == 1 and obs[0]["src"] == b.uid and obs[0]["resolution"] == "observed"
    assert check_graph(doc, snap) == []
    assert doc["profiles"] == [prof["profile_id"]]


def test_validate_graph_rejects_guesses(tmp_path):
    snap = _pair_snapshot(tmp_path / "p").snapshot
    doc = export_graph(snap)
    bad = json.loads(graph_bytes(doc))
    bad["edges"][0]["dst"] = snap.units()[1].uid
    assert any("never asserts dst" in x for x in validate_graph(bad))
    bad = json.loads(graph_bytes(doc))
    g = next(e for e in bad["edges"] if e["exit"]["kind"] == "goto_tb")
    g["resolution"] = "candidates_multiple"
    assert validate_graph(bad)
    bad = json.loads(graph_bytes(doc))
    bad["edges"].append({"src": snap.units()[0].uid, "exit": {"kind": "observed"},
                         "candidates": [], "observed": [], "resolution": "observed"})
    assert any("without a profile observation" in x for x in validate_graph(bad))


def test_no_meta_units_have_nodes_but_no_edges(tmp_path):
    a = rich_tb(64)
    b = with_meta(rich_tb(B.round_up(B.resolve_unit(a).end_offset, 64),
                          key=B.DEFAULT_KEY.__class__(0x28098800, None, 0x110, 0x800400,
                                                      0xFF020000)), scm=False)
    (tmp_path / "p").mkdir()
    write_rich_pair([a, b], str(tmp_path / "p"))
    snap = import_snapshot(str(tmp_path / "p")).snapshot
    doc = export_graph(snap)
    assert len(doc["nodes"]) == 2 and doc["nodes"][1]["status"] == "no_meta"
    assert all(e["src"] == snap.units()[0].uid for e in doc["edges"])


# ---------------------------------------------------------------------------
# analyses
# ---------------------------------------------------------------------------

class _Ctx:
    def __init__(self, snap):
        self.s = snap
        self.header = snap.header

    def unit(self, uid):
        return self.s.unit(uid)

    def units(self):
        return self.s.units()

    def unit_ids(self):
        return self.s.unit_ids()

    def layout(self, uid=None):
        return self.s.layout() if uid is None else self.s.layout()[uid]


def test_analyses(tmp_path):
    snap = _pair_snapshot(tmp_path / "p").snapshot
    ctx = _Ctx(snap)
    specs = {s.name: s for s in (*REFS_ANALYSES, *LAYOUT_ANALYSES, *CFG_ANALYSES)}
    assert set(specs) == {"refs", "recovery", "layout", "cfg.known_edges"}
    a = snap.units()[0]
    r = specs["refs"].compute(ctx, ScopeRef("unit", a.uid))
    assert r["status"] == "ok_relocatable" and len(r["fixups"]) == 5
    rc = specs["recovery"].compute(ctx, ScopeRef("unit", a.uid))
    assert [p["insn"] for p in rc["points"]] == [0, 1, 3]
    lay = specs["layout"].compute(ctx, ScopeRef("snapshot", snap.snapshot_id))
    assert lay["overlaps"] == [] and lay["pinned"] == [] and lay["max_end"] == snap.max_end
    assert lay["prologue_floor"] == 64 and lay["slack"] > 0
    ke = specs["cfg.known_edges"].compute(ctx, ScopeRef("snapshot", snap.snapshot_id))
    assert ke["edges"] == export_graph(snap)["edges"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_import_and_graph(tmp_path):
    load_commands()
    p = tmp_path / "p"
    p.mkdir()
    write_rich_pair([rich_tb(64)], str(p))
    run = CliRunner()
    r = run.invoke(cli, ["import", str(p), "--store", str(tmp_path / "s")])
    assert r.exit_code == 0, r.output
    assert "ok_relocatable=1" in r.output and "(created)" in r.output
    sdir = next((tmp_path / "s" / "snapshots").iterdir())
    g = tmp_path / "g.json"
    r = run.invoke(cli, ["graph", "export", str(sdir), "--out", str(g)])
    assert r.exit_code == 0, r.output
    r = run.invoke(cli, ["graph", "import", str(g), "--check", str(sdir)])
    assert r.exit_code == 0 and "byte-identical" in r.output and "check: ok" in r.output
    g.write_bytes(g.read_bytes().replace(b'"scope":"snapshot"', b'"scope":"snapshot" '))
    r = run.invoke(cli, ["graph", "import", str(g)])
    assert r.exit_code == 1 and "DIFFERS" in r.output
    # reject -> exit 2
    (p / "rich.scm").write_bytes(B.swap_run((p / "rich.scm").read_bytes(),
                                            {"code_address": 0x7FFF00000000}))
    r = run.invoke(cli, ["import", str(p)])
    assert r.exit_code == 2 and "run_info_mismatch:code_address" in r.output


def test_one_shot_source_chains_to_ordinary_tb(tmp_path):
    """An io-recompile TB (CF_NOIRQ|CF_MEMI_ONLY|count 1) links to a TB looked up with
    curr_cflags (cpu-exec.c:935-953); found by the T-B1-4 Linking TBs oracle."""
    k0 = B.DEFAULT_KEY
    src_key = k0.__class__(k0.page_addr0, None, k0.flags, k0.cs_base,
                           k0.cflags | 0x10000 | 0x1000 | 1)
    k1 = k0.__class__(k0.page_addr0 + 8, None, k0.flags, k0.cs_base, k0.cflags)
    v1 = 0x08000000 | (k1.page_addr0 & 0x3FF)
    a = rich_tb(64, key=src_key, goto_dest=v1)
    a = with_meta(a, flags=0)  # CF_NOIRQ: no irq check flag (the bytes keep it; fine here)
    b = rich_tb(B.round_up(B.resolve_unit(a).end_offset, 64), key=k1)
    (tmp_path / "p").mkdir()
    write_rich_pair([a, b], str(tmp_path / "p"))
    snap = import_snapshot(str(tmp_path / "p")).snapshot
    ua, ub = snap.units()
    e = next(e for e in export_graph(snap)["edges"]
             if e["src"] == ua.uid and e["exit"]["kind"] == "goto_tb")
    assert e["candidates"] == [ub.uid] and e["resolution"] == "candidates_unique"
