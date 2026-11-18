"""WP10 synthetic acceptance: NativeLinker, ValidateAndPackage, artifact publish.

Snapshots are synthetic ``.sc`` + ``.scm`` pairs (WP4 builders, the WP7 rich TB)
imported into a store by WP7's importer; the identity documents attached to
them are minimal stand-ins (their ids are real content ids), since V7 needs a
bundle and a RuntimeIdentity.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from scopt.canon import content_id, hexaddr
from scopt.formats import artifact as A
from scopt.formats import scm_v1 as S
from scopt.formats.sc_v5 import ScHeader, read_sc, sha256_sparse, write_sc
from scopt.ids import bundle_id
from scopt.importer.import_cache import import_snapshot
from scopt.link import (INT32_MAX, INT32_MIN, LinkError, NativeLinker, apply_fixup,
                        int32_ok)
from scopt.manager import PassManager, PassRegistry, default_pass_registry
from scopt.model.unit import Fixup, RelocInfo
from scopt.validate import ValidateAndPackage, optimize, validate_artifact
from tests.synth import builders as B
from tests.synth import rich_tb as RT

CODE = B.DEFAULT_CODE_ADDRESS


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _maps(code_size: int) -> bytes:
    lines = RT.RICH_MAPS.decode().splitlines(keepends=True)
    arena = f"{CODE:x}-{CODE + code_size:x} rwxp 00000000 00:00 0 \n"
    return "".join(arena if "rwxp" in ln else ln for ln in lines).encode()


def _identity(header: ScHeader) -> tuple[dict, dict]:
    b = {"schema_version": 1, "x_synthetic": True, "contract_id": "sha256:" + "1" * 64}
    b["bundle_id"] = bundle_id(b)
    ri = {"schema": "scopt.runtime_identity/1", "code_address": hexaddr(header.code_address),
          "code_size": hexaddr(header.code_size), "prologue_end": hexaddr(header.prologue_end),
          "runtime_signature": hexaddr(header.runtime_signature), "tb_size": header.tb_size,
          "host_page_size": header.host_page_size,
          "target_page_bits": header.target_page_bits, "supercache_flat": False}
    ri["runtime_identity_id"] = content_id(ri, "runtime_identity_id")
    return b, ri


def make_snapshot(tmp: Path, units, *, code_size: int | None = None, helpers=None,
                  identity: bool = True, name: str = "s"):
    """Write a sparse synthetic .sc + .scm, import it into ``tmp/store``."""
    prep = tmp / f"prep-{name}"
    prep.mkdir(parents=True)
    hdr_over = {} if code_size is None else {"code_size": code_size}
    h = B.sc_header_fields(units, hdr_over)
    h.pop("checksum")
    hdr = ScHeader(**h, checksum=0).with_checksum()
    res_units = [B.resolve_unit(u) for u in units]
    write_sc(prep / "s.sc", hdr, [(r.record_bytes, r.arena_bytes) for r in res_units])
    B.build_scm(units, sc_header={"code_size": hdr.code_size}, path=str(prep / "s.scm"),
                maps=_maps(hdr.code_size), helpers=helpers or RT.RICH_HELPERS)
    res = import_snapshot(str(prep), store=str(tmp / "store"))
    snap = res.snapshot
    if identity:
        b, ri = _identity(snap.header)
        snap = replace(snap, bundle=b, runtime=ri)
    return snap


def pipeline(*entries, caps=("PLACE",)):
    passes = []
    for e in entries:
        name, params = (e, {}) if isinstance(e, str) else e
        passes.append({"name": name, "version": 1, "params": params})
    return {"schema": "scopt.pipeline", "version": 1, "name": "test",
            "allowed_capabilities": list(caps), "pass": passes}


def registry() -> PassRegistry:
    return default_pass_registry()


def run(snap, store, *entries, caps=("PLACE",), **kw):
    cand = PassManager(snap, pipeline(*entries, caps=caps), passes=registry()).execute()
    return ValidateAndPackage(cand, store, **kw).run()


def codes(res):
    return res.validation.codes() if res.validation is not None else None


def free_offset(snap, extra: int = 0) -> int:
    return B.round_up(snap.max_end, 64) + extra


# ---------------------------------------------------------------------------
# fixup arithmetic (rel32 exactly at the int32 bounds)
# ---------------------------------------------------------------------------

def _fx(rule="rel_minus_delta", width=4):
    return Fixup(field_off=1, width=width, rule=rule, site_kind=0, target_class="ext.plugin",
                 target_abs=None)


@pytest.mark.parametrize("field,d,ok", [
    (0, -INT32_MAX, True),              # -> +2^31-1: largest int32, accepted
    (0, -(INT32_MAX + 1), False),       # -> +2^31: rejected
    (0, 1 << 31, True),                 # -> -2^31: smallest int32, accepted
    (-1, 1 << 31, False),               # -> -2^31-1: rejected
    (-0x4CFDA98B, 0x33025675, True),    # B0-like plugin distance, -2^31 exactly
    (-0x4CFDA98B, 0x33025676, False),
])
def test_rel32_bounds_exact(field, d, ok):
    code = bytearray(b"\xe8" + field.to_bytes(4, "little", signed=True))
    assert int32_ok(field - d) is ok
    if ok:
        apply_fixup(code, _fx(), d)
        assert int.from_bytes(code[1:5], "little", signed=True) == field - d
        assert field - d in (INT32_MAX, INT32_MIN) or abs(field - d) < 1 << 31
    else:
        with pytest.raises(LinkError) as e:
            apply_fixup(code, _fx(), d)
        assert e.value.reason == "rel32_range"
        assert code[1:5] == field.to_bytes(4, "little", signed=True)  # untouched


def test_abs_plus_delta_and_bad_fields():
    code = bytearray(10)
    code[1:9] = (0x7FFF00001000).to_bytes(8, "little")
    apply_fixup(code, _fx("abs_plus_delta", 8), 0x40)
    assert int.from_bytes(code[1:9], "little") == 0x7FFF00001040
    with pytest.raises(LinkError) as e:
        apply_fixup(bytearray(3), _fx(), 64)
    assert e.value.reason == "fixup_field"


# ---------------------------------------------------------------------------
# moves by +-64*k: V1-V8 pass
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sign,k", [(+1, 1), (+1, 7), (-1, 1), (-1, 5)])
def test_move_by_64k_passes(tmp_path, sign, k):
    # leave a gap below the first unit so it can move down as well as up
    units = RT.rich_units(3, start=64 * 16) + B.simple_units(1, start=64 * 64)
    snap = make_snapshot(tmp_path, units)
    uid = snap.units()[0].uid if sign < 0 else snap.units()[1].uid
    if sign > 0:  # move unit 1 past the end, then k lines further
        delta = free_offset(snap) - snap.unit(uid).tb_offset + 64 * k
    else:
        delta = -64 * k
    res = run(snap, tmp_path / "store", ("move-units", {"deltas": {uid: delta}}))
    assert res.published, (res.reason, res.validation and res.validation.fails())
    c = codes(res)
    assert all(c[f"V{i}"] == "pass" for i in range(1, 9)), c
    assert c["V9"] == "skip"
    side = json.loads((Path(res.artifact_dir) / A.SIDECAR).read_text())
    assert side["move_map"] == {uid: {"slot": snap.unit(uid).slot,
                                      "old_tb_offset": hexaddr(snap.unit(uid).tb_offset),
                                      "new_tb_offset": hexaddr(snap.unit(uid).tb_offset + delta),
                                      "delta": delta}}
    # the rich unit's fixups were applied (prologue + ext fields changed)
    lu = res.link.by_uid()[uid]
    assert lu.moved and len(lu.patched) == len(snap.unit(uid).reloc.fixups) > 0
    # MANIFEST verifies and the artifact is read-only
    art = Path(res.artifact_dir)
    assert A.verify_manifest(art) == []
    assert not os.stat(art / "cache.sc").st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    assert not os.stat(art).st_mode & stat.S_IWUSR
    assert res.artifact_id == "sha256:" + art.name


def test_identity_candidate_is_byte_identical_and_reimport_closes(tmp_path):
    units = RT.rich_units(4) + B.simple_units(2, start=64 * 200)
    snap = make_snapshot(tmp_path, units)
    res = run(snap, tmp_path / "store", "identity", caps=())
    assert res.published, res.reason
    c = codes(res)
    assert c == {**{f"V{i}": "pass" for i in range(1, 10)}}
    art = Path(res.artifact_dir)
    assert sha256_sparse(art / "cache.sc") == sha256_sparse(Path(snap.path) / "cache.sc")
    assert res.output_snapshot_id == snap.snapshot_id
    # the derived capture binds every slot again (V8 closure, also by hand)
    again = import_snapshot(sc=str(art / "cache.sc"), scms=[str(art / "cache.scm")])
    assert again.snapshot.snapshot_id == snap.snapshot_id
    assert again.join.histogram() == {"bound": len(units)}
    # standalone re-validation agrees with the stored verdicts
    rv = validate_artifact(art, snap)
    assert rv["verdict"] == "accepted" and rv["stored_codes_agree"], rv["codes"]
    assert {r["check"]: r["result"] for r in rv["rows"] if r["code"] == "V8"}["manifest"] == "pass"
    # ledger: pass rows + compiled + accepted
    rows = [json.loads(x) for x in (art / A.LEDGER).read_text().splitlines()]
    stages = [(r["subject"], r["stage"], r["verdict"]) for r in rows]
    assert ("pass", "optimized", "no_change") in stages
    assert ("artifact", "compiled", "pass") in stages and ("artifact", "accepted", "pass") in stages


def test_derived_scm_sites_follow_the_move(tmp_path):
    units = RT.rich_units(2)
    snap = make_snapshot(tmp_path, units)
    u = snap.units()[0]
    d = free_offset(snap) - u.tb_offset + 128
    res = run(snap, tmp_path / "store", ("move-units", {"deltas": {u.uid: d}}))
    assert res.published, res.reason
    scm = S.read_scm(Path(res.artifact_dir) / "cache.scm")
    tb = scm.tbs[u.slot]
    old = S.read_scm(next(Path(snap.path).glob("capture-*.scm"))).tbs[u.slot]
    lo, hi = CODE + u.tb_offset, CODE + u.end_offset
    for a, b in zip(old.sites, tb.sites):
        assert b.value == (a.value + d if lo <= a.value < hi else a.value)
    assert tb.sc_record.tb_offset == u.tb_offset + d
    assert tb.guest_bytes == old.guest_bytes and tb.vaddr == old.vaddr
    assert scm.run_info == S.read_scm(next(Path(snap.path).glob("capture-*.scm"))).run_info


# ---------------------------------------------------------------------------
# negatives
# ---------------------------------------------------------------------------

def test_omitted_prologue_fixup_fails_v2_or_v3(tmp_path):
    units = RT.rich_units(2)
    snap = make_snapshot(tmp_path, units)
    u = snap.units()[0]
    drop = next(f for f in u.reloc.fixups if f.target_class == "prologue.tb_ret")
    bad = replace(u, reloc=RelocInfo(True, (), tuple(f for f in u.reloc.fixups if f != drop)))
    tampered = replace(snap, _units=(bad,) + snap.units()[1:])
    d = free_offset(snap) - u.tb_offset + 64
    res = run(tampered, tmp_path / "store", ("move-units", {"deltas": {u.uid: d}}))
    assert not res.published
    c = codes(res)
    assert c["V2"] == "fail" or c["V3"] == "fail", c
    assert c["V3"] == "fail"  # the tb_ret jmp no longer reaches the prologue
    rej = json.loads(Path(res.rejection_path).read_text())
    assert rej["reason"].startswith(("V2", "V3", "V4")) and rej["candidate_id"] == res.candidate_id
    assert not list((tmp_path / "store" / "artifacts").glob("[0-9a-f]*"))


def test_extra_byte_change_fails_v2(tmp_path, monkeypatch):
    units = RT.rich_units(2)
    snap = make_snapshot(tmp_path, units)
    u = snap.units()[1]
    import scopt.link as L
    real = L.relocate_unit

    def corrupt(unit, new_tb):
        lu = real(unit, new_tb)
        if lu.moved:
            body = bytearray(lu.body)
            body[0] ^= 0x01  # outside every fixup field
            lu = replace(lu, body=bytes(body))
        return lu
    monkeypatch.setattr(L, "relocate_unit", corrupt)
    res = run(snap, tmp_path / "store",
              ("move-units", {"deltas": {u.uid: free_offset(snap) - u.tb_offset}}))
    assert not res.published and codes(res)["V2"] == "fail"


def test_missing_identity_documents_reject(tmp_path):
    snap = make_snapshot(tmp_path, RT.rich_units(1), identity=False)
    res = run(snap, tmp_path / "store", "identity", caps=())
    assert not res.published
    assert codes(res)["V7"] == "fail" and res.reason == "V7:identity_missing:bundle"


def _plugin_exact_case(tmp_path, extra_lines: int):
    """A rich unit whose plugin call rel32 is exactly -2^31 after the move
    (``extra_lines`` = 0), or one line past it (1)."""
    pf = RT.PLUGIN_FUNC + 11  # makes plugin - next_ip + 2^31 a multiple of 64
    helpers = ({"helper_id": 0, "name": None, "func": pf, "tcg_flags": 0},
               RT.RICH_HELPERS[1])
    old = RT.PLUGIN_FUNC
    RT.PLUGIN_FUNC = pf
    try:
        units = RT.rich_units(1, start=64 * 4)
    finally:
        RT.PLUGIN_FUNC = old
    snap = make_snapshot(tmp_path, units, code_size=0x80000000, helpers=helpers)
    u = snap.units()[0]
    call_field = 30
    next_ip = CODE + u.tc_offset + call_field + 4
    d = pf - next_ip + (1 << 31)
    assert d % 64 == 0 and d > 0
    d += 64 * extra_lines
    return snap, u, d


def test_rel32_exact_bound_end_to_end_accepted(tmp_path):
    snap, u, d = _plugin_exact_case(tmp_path, 0)
    res = run(snap, tmp_path / "store", ("move-units", {"deltas": {u.uid: d}}))
    assert res.published, (res.reason, res.validation and res.validation.fails())
    body = res.link.by_uid()[u.uid].body
    assert int.from_bytes(body[30:34], "little", signed=True) == INT32_MIN
    assert codes(res)["V4"] == "pass"


def test_rel32_one_line_past_bound_rolls_back(tmp_path):
    snap, u, d = _plugin_exact_case(tmp_path, 1)
    res = run(snap, tmp_path / "store", ("move-units", {"deltas": {u.uid: d}}))
    assert res.published
    assert [(f.uid, f.reason) for f in res.link.rollbacks] == [(u.uid, "rel32_range")]
    assert res.link.move_map() == {}


def test_fb2_5_forced_0x60000000_rolls_back(tmp_path):
    units = RT.rich_units(3)
    snap = make_snapshot(tmp_path, units, code_size=0x80000000)
    u = snap.units()[1]
    res = run(snap, tmp_path / "store", ("move-units", {"moves": {u.uid: 0x60000000}}))
    assert res.published, res.reason
    assert res.link.attempts == 2
    assert [(f.uid, f.reason) for f in res.link.rollbacks] == [(u.uid, "rel32_range")]
    side = json.loads((Path(res.artifact_dir) / A.SIDECAR).read_text())
    assert side["move_map"] == {}
    assert {"unit": u.uid, "reasons": ["failed:rel32_range"]} in side["pinned"]
    assert side["x_link"]["rollbacks"][0]["reason"] == "rel32_range"
    # the baseline is retained: the artifact equals the input byte for byte
    assert sha256_sparse(Path(res.artifact_dir) / "cache.sc") == \
        sha256_sparse(Path(snap.path) / "cache.sc")


def test_fb2_5_rollback_slot_taken_rejects(tmp_path):
    units = RT.rich_units(3)
    snap = make_snapshot(tmp_path, units, code_size=0x80000000)
    a, b = snap.units()[0], snap.units()[1]
    span_ok = (b.end_offset - b.tb_offset) <= (a.end_offset - a.tb_offset) + 64
    assert span_ok
    res = run(snap, tmp_path / "store",
              ("move-units", {"moves": {a.uid: 0x60000000, b.uid: a.tb_offset}}))
    assert not res.published
    assert res.reason == "failed:rel32_range"
    rej = json.loads(Path(res.rejection_path).read_text())
    reasons = [f["reason"] for f in rej["link"]["failures"]]
    assert "rel32_range" in reasons and "rollback_slot_taken" in reasons
    assert Path(res.rejection_path).name == res.candidate_id.split(":")[1] + ".json"
    led = [(r["stage"], r["verdict"], r["reason"]) for r in rej["ledger"]]
    assert ("compiled", "fail", "rel32_range") in led and ("accepted", "fail",
                                                          "failed:rel32_range") in led


def test_overlapping_placement_rolls_back(tmp_path):
    units = RT.rich_units(3)
    snap = make_snapshot(tmp_path, units)
    a, b = snap.units()[0], snap.units()[2]
    res = run(snap, tmp_path / "store", ("move-units", {"moves": {b.uid: a.tb_offset}}))
    assert res.published
    assert [(f.uid, f.reason) for f in res.link.rollbacks] == [(b.uid, "place_overlap")]


# ---------------------------------------------------------------------------
# section placement, LIFO duplicate keys
# ---------------------------------------------------------------------------

def test_reverse_layout_all_units_moved_and_valid(tmp_path):
    units = RT.rich_units(4) + B.simple_units(3, start=64 * 120)
    snap = make_snapshot(tmp_path, units)
    res = run(snap, tmp_path / "store", "reverse-layout", caps=("LAYOUT_ORDER",))
    assert res.published, (res.reason, res.validation and res.validation.fails())
    c = codes(res)
    assert all(c[f"V{i}"] == "pass" for i in range(1, 9))
    links = res.link.units
    # slot order kept; placement reversed (last slot lowest), packed from the prologue
    assert [lu.uid for lu in links] == [u.uid for u in snap.units()]
    offs = [lu.new_tb_offset for lu in links]
    assert offs == sorted(offs, reverse=True)
    assert min(offs) == B.round_up(snap.header.prologue_end, 64)


def test_duplicate_key_lifo_order_preserved(tmp_path):
    key = B.DEFAULT_KEY
    u0 = RT.rich_tb(64, key=key)
    off = B.round_up(B.resolve_unit(u0).end_offset, 64)
    u1 = RT.rich_tb(off, key=key)  # same key and guest bytes -> same tb_uid, #0 and #1
    off2 = B.round_up(B.resolve_unit(u1).end_offset, 64)
    u2 = RT.rich_tb(off2, key=replace(key, page_addr0=key.page_addr0 + 8))
    snap = make_snapshot(tmp_path, [u0, u1, u2])
    uids = [u.uid for u in snap.units()]
    assert uids[0].endswith("#0") and uids[1].endswith("#1") and \
        uids[0].split("#")[0] == uids[1].split("#")[0]
    res = run(snap, tmp_path / "store", "reverse-layout", caps=("LAYOUT_ORDER",))
    assert res.published, res.reason
    out = read_sc(Path(res.artifact_dir) / "cache.sc")
    # records stay in slot order: the newer duplicate (#1) is still the later slot,
    # so QEMU's load-in-slot-order "newest wins" picks the same TB
    assert out.unit_ids() == uids
    assert out.records[1].tb_offset < out.records[0].tb_offset  # placement did reverse
    again = import_snapshot(sc=str(Path(res.artifact_dir) / "cache.sc"),
                            scms=[str(Path(res.artifact_dir) / "cache.scm")])
    assert [u.uid for u in again.snapshot.units()] == uids


def test_pinned_units_are_obstacles(tmp_path):
    units = RT.rich_units(3)
    snap = make_snapshot(tmp_path, units)
    # pin the middle unit at import level (as if a reference were unexplained)
    mid = snap.units()[1]
    pinned = replace(mid, reloc=RelocInfo(False, ("unexplained_reference@0x5",), ()))
    snap2 = replace(snap, _units=(snap.units()[0], pinned, snap.units()[2]))
    cand = PassManager(snap2, pipeline("reverse-layout", caps=("LAYOUT_ORDER",)),
                       passes=registry()).execute()
    link = NativeLinker(cand, derive_captures=False).link()
    assert link.ok
    by = link.by_uid()
    assert not by[mid.uid].moved
    # [tb', end') intervals (struct + code + search) are disjoint and the
    # pinned unit kept its interval
    spans = sorted((lu.new_tb_offset, lu.new_tb_offset + 192 + len(lu.body)) for lu in link.units)
    for (a0, a1), (b0, _b1) in zip(spans, spans[1:]):
        assert b0 >= a1
    assert (mid.tb_offset, mid.end_offset) in spans
    assert [lu.new_tb_offset for lu in link.units if lu.uid != mid.uid] != \
        [u.tb_offset for u in snap2.units() if u.uid != mid.uid]


def test_optimize_function_and_cli(tmp_path):
    from click.testing import CliRunner

    from scopt.cli import cli, load_commands

    snap = make_snapshot(tmp_path, RT.rich_units(2))
    res = optimize(snap, "identity", tmp_path / "store")
    assert res.published
    load_commands()
    r = CliRunner().invoke(cli, ["validate", res.artifact_dir, "--snapshot", snap.path,
                                 "--json"])
    # the CLI re-opens the snapshot from the store, which has no identity documents
    # (they were attached in memory here), so V7 fails there: exit 1
    assert r.exit_code == 1, r.output
    doc = json.loads(r.output)
    assert doc["codes"]["V7"] == "fail"
    assert {k: v for k, v in doc["codes"].items() if k != "V7"} == \
        {f"V{i}": "pass" for i in range(1, 10) if i != 7}
    # `scopt optimize` on the store snapshot (no identity documents): rejected, exit 2
    r = CliRunner().invoke(cli, ["optimize", snap.path, "--pipeline", "identity",
                                 "--store", str(tmp_path / "store2"), "--json"])
    assert r.exit_code == 2, r.output
    doc = json.loads(r.output)
    assert doc["status"] == "rejected" and doc["reason"] == "V7:identity_missing:bundle"
    assert Path(doc["rejection_path"]).is_file()
    assert not list((tmp_path / "store2" / "artifacts").glob("[0-9a-f]*"))
    # with a move: published through the CLI pipeline file and --params
    snap2 = make_snapshot(tmp_path, RT.rich_units(2), name="t")
    pf = tmp_path / "p.json"
    u = snap2.units()[0]
    pf.write_text(json.dumps({"move-units": {"deltas": {u.uid: 64 * 50}}}))
    pipe = Path(__file__).resolve().parents[1] / "data/pipelines/test-move-units.toml"
    res2 = optimize(snap2, pipe, tmp_path / "store3", params=json.loads(pf.read_text()))
    assert res2.published and res2.link.by_uid()[u.uid].delta == 64 * 50
