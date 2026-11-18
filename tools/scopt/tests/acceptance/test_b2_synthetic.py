"""B2 fixtures, synthetic half (design §7; WP15A).

* F-B2-2: duplicate keys keep their LIFO order. QEMU indexes the records in slot
  order and its bucket lists are newest-first (``tcg/supercache.c`` ``index_record``
  / ``tcg_supercache_lookup``), so of several records with one key the highest slot
  is the one executed. A layout must therefore keep the slot order of duplicates
  whatever it does to their placement. The real snapshots (minimal, fault,
  FreeRTOS blinky and compute) have no duplicate key; ``tests/qemu/b2_gate.py``
  additionally appends a duplicate to a real file and checks the executed slot
  with ``diag exec``.
* F-B2-6: a ``no_meta`` unit in the *middle* of the arena (on real data the
  appended record always sits at the top, above the packed region) is a fixed
  obstacle: the placer packs the other units around it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scopt.formats import artifact as A
from scopt.formats.sc_v5 import read_sc
from scopt.importer.import_cache import import_snapshot
from scopt.link import NativeLinker
from scopt.manager import PassManager
from tests.synth import builders as B
from tests.synth import rich_tb as RT
from tests.unit.test_wp10_link import codes, make_snapshot, pipeline, registry, run


def lifo_winners(sc) -> dict:
    """Key -> UnitId of the record QEMU keeps (the later slot replaces an earlier one)."""
    win = {}
    for uid, u in zip(sc.unit_ids(), sc.units()):
        win[u.record.key] = uid
    return win


def _dup_units():
    key = B.DEFAULT_KEY
    units, off = [], 64
    for _ in range(3):  # three records with one key and the same guest bytes: #0, #1, #2
        u = RT.rich_tb(off, key=key)
        units.append(u)
        off = B.round_up(B.resolve_unit(u).end_offset, 64)
    for i in (1, 2):  # two others with distinct keys
        u = RT.rich_tb(off, key=replace(key, page_addr0=key.page_addr0 + 8 * i))
        units.append(u)
        off = B.round_up(B.resolve_unit(u).end_offset, 64)
    return units


@pytest.mark.parametrize("entries,caps", [
    (("reverse-layout",), ("LAYOUT_ORDER",)),
    # #2 explicitly below #0 and #1, #0 at the top: placement order 2 < 1 < 0
    ("moves", ("PLACE",)),
], ids=["reverse-layout", "move-units"])
def test_f_b2_2_duplicate_key_lifo(tmp_path, entries, caps):
    snap = make_snapshot(tmp_path, _dup_units())
    uids = [u.uid for u in snap.units()]
    base = uids[0].split("#")[0]
    assert uids[:3] == [f"{base}#0", f"{base}#1", f"{base}#2"]
    insc = read_sc(Path(snap.path) / "cache.sc")
    win_in = lifo_winners(insc)
    assert win_in[insc.units()[0].record.key] == f"{base}#2"  # the newest record wins
    if entries == "moves":
        top = B.round_up(snap.max_end, 64)
        sizes = [B.round_up(u.end_offset - u.tb_offset, 64) for u in snap.units()[:3]]
        moves = {uids[2]: top, uids[1]: top + sizes[2], uids[0]: top + sizes[2] + sizes[1]}
        entries = (("move-units", {"moves": moves}),)
    res = run(snap, tmp_path / "store", *entries, caps=caps)
    assert res.published, (res.reason, res.validation and res.validation.fails())
    c = codes(res)
    assert all(c[f"V{i}"] == "pass" for i in range(1, 9)), c
    art = Path(res.artifact_dir)
    out = read_sc(art / "cache.sc")
    # records stay in slot order, so the duplicates keep their LIFO order ...
    assert out.unit_ids() == uids
    assert lifo_winners(out) == win_in
    # ... although their placement order was reversed
    offs = [r.tb_offset for r in out.records[:3]]
    assert offs[2] < offs[1] < offs[0], offs
    side = json.loads((art / A.SIDECAR).read_text())
    # (with reverse-layout the middle of five equal units is the mirror's fixed point)
    assert len(set(uids[:3]) & set(side["move_map"])) >= 2
    # the derived capture binds every slot to the same UnitIds again
    again = import_snapshot(sc=str(art / "cache.sc"), scms=[str(art / "cache.scm")])
    assert [u.uid for u in again.snapshot.units()] == uids
    assert again.summary["status"]["ok_relocatable"] == len(uids)


def test_f_b2_6_no_meta_obstacle_in_the_middle(tmp_path):
    snap = make_snapshot(tmp_path, RT.rich_units(5))
    mid = snap.units()[2]
    nm = replace(mid, meta_status="no_meta")
    us = list(snap.units())
    us[2] = nm
    snap2 = replace(snap, _units=tuple(us))
    assert snap2.status(mid.uid) == "no_meta"
    cand = PassManager(snap2, pipeline("reverse-layout", caps=("LAYOUT_ORDER",)),
                       passes=registry()).execute()
    rl = next(p for p in cand.passes if p.name == "reverse-layout")
    assert mid.uid not in rl.affected  # never in an order request
    link = NativeLinker(cand, derive_captures=False).link()
    assert link.ok
    by = link.by_uid()
    assert not by[mid.uid].moved  # stays fixed
    others = [lu for lu in link.units if lu.uid != mid.uid]
    assert all(lu.moved for lu in others)
    spans = sorted((lu.new_tb_offset, lu.new_tb_offset + 192 + len(lu.body)) for lu in link.units)
    for (a0, a1), (b0, _) in zip(spans, spans[1:]):
        assert b0 >= a1, spans  # disjoint
    # the packed region reaches the obstacle: some units below it, some bumped above it
    assert any(lu.new_tb_offset < mid.tb_offset for lu in others)
    assert any(lu.new_tb_offset >= mid.end_offset for lu in others)


def test_f_b2_5_rejected_candidate_is_a_report_row(tmp_path):
    """A candidate rejected with ``failed:rel32_range`` publishes nothing; its ledger
    rows live only inside ``store/rejections/<candidate>.json``. ``scopt report`` must
    still show it (design §5.11: rejected stages are rows; WP15A fix)."""
    from scopt.report.collect import collect

    snap = make_snapshot(tmp_path, RT.rich_units(3), code_size=0x80000000)
    a, b = snap.units()[0], snap.units()[1]
    res = run(snap, tmp_path / "store",
              ("move-units", {"moves": {a.uid: 0x60000000, b.uid: a.tb_offset}}))
    assert not res.published and res.reason == "failed:rel32_range"
    (tmp_path / "store" / "rejections" / "junk.json").write_text('{"schema": "other"}')
    m = collect([tmp_path / "store"])
    rows = {(t["subject"], t["id"]): t for t in m["ledger"]["table"]}
    st = rows[("artifact", res.candidate_id)]["stages"]
    assert (st["compiled"]["verdict"], st["compiled"]["reason"]) == ("fail", "rel32_range")
    assert (st["accepted"]["verdict"], st["accepted"]["reason"]) == ("fail", "failed:rel32_range")
    # its pass row is scoped to the rejected candidate, not left unscoped
    assert ("pass", f"move-units@1 @ {res.candidate_id}") in rows or any(
        t["subject"] == "pass" and t.get("scope") == res.candidate_id
        for t in m["ledger"]["table"])
    # a non-rejection JSON in rejections/ is a collection problem, not a crash
    assert any("junk.json" in p["where"] for p in m["problems"])
