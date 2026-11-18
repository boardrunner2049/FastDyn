"""WP18: CacheRegion ``multi_tb`` rows and the typed schema items (design §3.9)."""

from __future__ import annotations

import json
import pytest

from scopt.formats.region_v1 import validate_region
from scopt.ids import region_id
from scopt.link import B3Plan
from scopt.validate import package_b3
from tests.synth import b3 as H


@pytest.fixture(scope="module")
def published(tmp_path_factory):
    s = H.snap("minimal")
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts[:2]]
    grow, vs = H.spec_pairs(s, hosts[:2], cs)
    gen = {k: v for k, v in H.dummy_specs(s, limit=8).items() if k not in grow}
    grow.update(dict(list(gen.items())[:2]))
    plan = B3Plan(grow=grow, variants=tuple(vs))
    mp = pytest.MonkeyPatch()
    ri6 = H.v6_identity(mp)
    store = tmp_path_factory.mktemp("store")
    r = package_b3(s, plan, store, candidate_id="sha256:" + "11" * 32, runtime=ri6,
                   v6_exes=[H.QINT_V6_SHA], allow_uncertified=True)
    yield s, plan, r
    mp.undo()


def _rows(r):
    from pathlib import Path

    return [json.loads(x) for x in (Path(r.artifact_dir) / "regions.jsonl").read_text()
            .splitlines()]


def test_published_with_multi_tb_rows(published):
    s, plan, r = published
    assert r.status == "published", (r.reason, r.validation and r.validation.fails())
    rows = _rows(r)
    multi = [x for x in rows if x["kind"] == "multi_tb"]
    assert len(multi) == len(plan.variants) == len(r.link.ext.links)
    for m in multi:
        assert validate_region(m) == []
        assert len(m["members"]) == 2 and m["members"][1]["origin"]["slot"] is None
        assert m["members"][1]["x_variant_of"] == m["members"][1]["unit"]
        e0, e1 = m["entries"]
        assert e0["precondition"] == {"full_key": True, "extra_guards": []}
        assert e1["kind"] == "specialized" and e1["precondition"]["full_key"] is False
        assert [a["atom"] for a in e1["precondition"]["extra_guards"]] == \
            ["g_bp", "g_thumb", "g_cond", "g_flags", "g_flags2", "g_pc"]
        assert e1["x_pred"] == {"member": 0, "slot": m["x_slot"]}
        assert len(m["native"]["members"]) == 2
        assert "stub_start" in m["native"]["members"][0]
        assert all(x.get("x_origin") == "variant_relocated" for x in m["relocations"]
                   if x["member"] == 1)
        assert all(p["state_fixups"] == [] for p in m["recovery"])
        assert m["dependencies"]["units"][0] == m["dependencies"]["units"][1]  # R, R_orig
        assert m["provenance"]["passes"][0]["name"] == "specialize-returns"
    sidecar = json.loads((__import__("pathlib").Path(r.artifact_dir) /
                          "cache.sc.scopt.json").read_text())
    assert sidecar["loader_requirement"] == "qemu-sc-v6"
    assert sidecar["x_ext_artifact_id8"] == bytes(r.link.ext.artifact_id8).hex()


def test_grown_singleton_rows_mark_guarded_slots(published):
    s, plan, r = published
    rows = {x["members"][0]["unit"]: x for x in _rows(r) if x["kind"] == "tb_singleton"}
    for uid, specs in plan.grow.items():
        x = rows[uid]
        gs = [e for e in x["exits"] if e["kind"] == "guarded_slot"]
        assert len(gs) == sum(len(sp.targets) for sp in specs)
        for e in gs:
            assert e["slot_kind"] in ("GENERAL", "SPEC") and e["patchable"] is not None
            assert e["reset"] == ("jmp_F" if e["slot_kind"] == "SPEC" else "exit_tb")
            assert e["guard_off"] == x["native"]["stub_start"]
        assert not any(e["kind"] == "goto_tb" and e["slot"] in [g["slot"] for g in gs]
                       for e in x["exits"])


def test_schema_types_the_new_items(published):
    _s, _p, r = published
    m = next(x for x in _rows(r) if x["kind"] == "multi_tb")

    def bad(mut):
        d = json.loads(json.dumps(m))
        mut(d)
        d["region_id"] = region_id(d)
        return validate_region(d)

    assert bad(lambda d: d["entries"][1]["precondition"]["extra_guards"][0]
               .update(atom="g_mpu"))
    assert bad(lambda d: d["entries"][1]["precondition"]["facts"].append(
        {"fact_id": "sha256:" + "0" * 64, "field": "pc", "value": "0x0", "def_insn": 0,
         "source": "patch", "cert_hash": "sha256:" + "0" * 64,
         "summary_row_hash": "sha256:" + "0" * 64}))
    assert bad(lambda d: d["recovery"][0]["state_fixups"].append({"x": 1})
               if d["recovery"] else d["recovery"].append({}))
    assert bad(lambda d: d.update(native=d["native"]["members"][0]))  # multi_tb needs members
    assert bad(lambda d: d["exits"][0].update(kind="guarded_slot", slot_kind=None))
    good = json.loads(json.dumps(m))
    assert validate_region(good) == []
    single = next(x for x in _rows(r) if x["kind"] == "tb_singleton")
    two = json.loads(json.dumps(single))
    two["members"].append(two["members"][0])
    two["region_id"] = region_id(two)
    assert validate_region(two)  # a tb_singleton has one member
    fact = {"fact_id": "sha256:" + "0" * 64, "field": "r0", "value": "0x0", "def_insn": 1,
            "source": "guest_code", "cert_hash": "sha256:" + "1" * 64,
            "summary_row_hash": "sha256:" + "2" * 64}
    ok = json.loads(json.dumps(m))
    ok["entries"][1]["precondition"]["facts"] = [fact]
    ok["dependencies"]["facts"] = [fact]
    ok["region_id"] = region_id(ok)
    assert validate_region(ok) == []
