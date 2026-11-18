"""WP18 T-B3-28: graph and region round trip with ``guarded_slot`` / ``variant_link``
edges and ``multi_tb`` rows (design §3.9, §9.1 V19).

Export the graph of the re-imported artifact, import it back, and compare the
B3 edges with the ext table; re-derive the multi_tb rows from the published
artifact and compare with ``regions.jsonl``; unknowns stay explicit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scopt.graph import (b3_edges, check_graph, export_graph, graph_bytes, load_graph,
                         roundtrip_ok, validate_graph)
from scopt.link import B3Plan
from scopt.validate import multi_tb_rows, package_b3, reimport
from tests.synth import b3 as H


def _publish(tmp_path, name: str, n_hosts: int, n_general: int):
    s = H.snap(name)
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts[:n_hosts]]
    grow, vs = H.spec_pairs(s, hosts[:n_hosts], cs)
    gen = {k: v for k, v in H.dummy_specs(s).items() if k not in grow}
    grow.update(dict(list(gen.items())[:n_general]))
    plan = B3Plan(grow=grow, variants=tuple(vs), max_variants=max(64, len(vs)))
    mp = pytest.MonkeyPatch()
    try:
        ri6 = H.v6_identity(mp)
        r = package_b3(s, plan, tmp_path / "store", candidate_id="sha256:" + "22" * 32,
                       runtime=ri6, v6_exes=[H.QINT_V6_SHA], allow_uncertified=True)
        assert r.status == "published", r.reason
        res = reimport(r.artifact_dir, s)
        regs = [json.loads(x) for x in (Path(r.artifact_dir) / "regions.jsonl")
                .read_text().splitlines()]
        from scopt.formats.envlayout_v1 import find_envlayout

        again = multi_tb_rows(res, r.link, runtime=ri6,
                              layout=find_envlayout(ri6["qemu_sha256"]))
    finally:
        mp.undo()
    return s, plan, r, res, regs, again


def _check(s, plan, r, res, regs, again):
    ext = r.link.ext
    doc = export_graph(res.snapshot, variants=res.variants)
    data = graph_bytes(doc)
    assert validate_graph(doc) == []
    assert roundtrip_ok(data)
    back = load_graph(data)
    assert check_graph(back, res.snapshot, variants=res.variants) == []
    e = b3_edges(back)
    by_tb = {u.tb_offset: u.uid for u in res.snapshot.units()}
    want_gs = sorted(((by_tb[x.src_tb_offset], x.slot_n, "SPEC" if x.kind == 2 else "GENERAL",
                       hex(x.target_vaddr), True) for x in ext.slots), key=repr)
    assert e["guarded_slot"] == want_gs
    want_vl = sorted(((by_tb[ext.slots[lk.slot_index].src_tb_offset],
                       ext.slots[lk.slot_index].slot_n,
                       r.link.variants[lk.variant_index].key,
                       by_tb[ext.variants[lk.variant_index].general_tb_offset])
                      for lk in ext.links), key=repr)
    assert e["variant_link"] == want_vl
    # multi_tb rows: published == re-derived from the re-import, one per link
    multi = [x for x in regs if x["kind"] == "multi_tb"]
    assert multi == again and len(multi) == len(ext.links)
    # unknowns explicit: the fallback lookup of every guarded return exit
    for uid, specs in plan.grow.items():
        reg = res.snapshot.regions[uid]
        if any(sp.exit_class == "return" for sp in specs):
            assert any(x.startswith("goto_ptr") for x in reg["unknowns"]), uid
    # a guarded_slot edge never names a dst; observed rows only come from profiles
    assert all("dst" not in x for x in back["edges"])
    v19 = [row for row in r.validation.rows if row["code"] == "V19"]
    assert all(row["result"] in ("pass", "skip") for row in v19), v19


def test_t_b3_28_minimal(tmp_path):
    _check(*_publish(tmp_path, "minimal", 3, 3))


@pytest.mark.real
@pytest.mark.slow
def test_t_b3_28_compute(tmp_path):
    _check(*_publish(tmp_path, "compute", 32, 64))
