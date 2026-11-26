"""WP6: Profile v1 ingest, status rules, compatibility, merge and flow (synthetic)."""

from __future__ import annotations

import gzip
import json

import pytest

from scopt.canon import canonical_json
from scopt.formats import profile_v1 as P
from scopt.formats import schemas
from scopt.formats.enums import QprofHeaderFlag
from scopt.formats.sc_records import read_sc
from tests.synth import builders as B
from tests.synth.profile_world import (
    PHASES, RUNTIME, SHA_BUNDLE, SHA_CONTRACT, TRANSIENT_OFF, make_world,
)


@pytest.fixture
def world(tmp_path):
    return make_world(str(tmp_path))


def uids(world):
    return read_sc(world.snap_sc).unit_ids()


def cnt(profile, uid, phase):
    return profile.unit_map()[uid]["phase"].get(str(phase))


# ---------------------------------------------------------------------------
# exact
# ---------------------------------------------------------------------------

def test_exact_ingest(world):
    p = world.ingest()
    assert schemas.errors(p.doc, "profile.v1") == []
    assert p.complete and not p.collection["overflow"] and p.collection["chain_disabled"]
    assert p.method == P.METHOD
    u0, u1, u2 = uids(world)
    assert [u["unit"] for u in p.units] == [u0, u1, u2]
    assert all(u["status"] == "exact" and "reason" not in u for u in p.units)
    assert cnt(p, u0, 0)["entries"] == 2 and cnt(p, u0, 1)["entries"] == 2
    assert cnt(p, u1, 0)["entries"] == 1 and cnt(p, u1, 1)["entries"] == 2
    assert cnt(p, u2, 0) == {"entries": 2, "aborted": 1, "longjmp": 1, "lookup_hits": 0}
    assert cnt(p, u2, 1) == {"entries": 0, "aborted": 0, "longjmp": 0, "lookup_hits": 0}
    assert p.phases[1] == {"id": 1, "name": "steady", "marker": "0x20000500", "reached": True,
                           "match": "exact_start", "first_dispatch_index": 7}
    assert p.phases[0]["marker"] is None
    assert p.domain == {"kind": "snapshot_units", "count": 3}
    assert p.collection["runs"][0]["total_dispatches"] == 12
    assert p.runtime == {k: RUNTIME[k] for k in P.RUNTIME_COMPAT_FIELDS}
    assert p.runtime_identity_id.startswith("sha256:")
    # the view: an exact 0 is a real zero
    v = P.ProfileView.open(p, world.target())
    assert v.count(u2, "steady") == P.Count(0, "exact")
    assert v.count(u2, "boot", "executions") == P.Count(1, "exact")
    assert v.phase("steady")[u0] == P.Count(2, "exact")
    assert P.check_flow(p).ok


def test_profile_id_is_canonical_and_deterministic(world):
    q = world.qprof()
    a, b = world.ingest(q), world.ingest(q)
    assert a.profile_id == b.profile_id and a.to_bytes() == b.to_bytes()
    assert a.to_bytes() == canonical_json(a.doc)
    from scopt.ids import profile_id
    assert profile_id(a.doc) == a.profile_id


def test_edges_status_and_dest_set(world):
    p = world.ingest()
    u0, u1, u2 = uids(world)
    kinds = {(e["src"], e["dst"], e["kind"], tuple(e["flags"]), e["phase"]): e for e in p.edges}
    assert kinds[(None, u0, "first", (), 0)]["count"] == 1
    assert kinds[(u0, u1, "chain0", (), 0)]["count"] == 1
    assert kinds[(u1, u0, "chain1", (), 0)]["count"] == 1
    assert kinds[(u2, u2, "exit_requested", (), 0)]["dest_set_complete"] is False
    assert kinds[(u2, u0, "exit_null", ("longjmp",), 1)]["count"] == 1
    assert all(e["status"] == "exact" for e in p.edges)
    assert "dest_set_complete" not in kinds[(u0, u1, "chain0", (), 0)]
    v = P.ProfileView.open(p, world.target())
    assert v.edge_count(u0, u1, "boot") == P.Count(1, "exact")
    assert v.edge_count(u1, u2, "boot") == P.Count(0, "exact")  # absent between exact units
    assert v.edge_count(u0, u1, "steady", kinds=["chain0"]) == P.Count(2, "exact")
    assert not P.ProfileView.dest_set_complete("lookup_hit")
    assert P.ProfileView.dest_set_complete("chain1")
    ext = {(e["src"], e["dst"], e["kind"]) for e in p.external_edges}
    assert (u0, "po:0", "lookup_hit") in ext and ("po:0", "transient", "exit_null") in ext
    assert ("transient", u2, "chain0") in ext and (u1, "po:1", "lookup_miss") in ext


# ---------------------------------------------------------------------------
# lower_bound
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flags,reason", [
    (0, "unclean_exit"),
    (int(QprofHeaderFlag.CLEAN_EXIT | QprofHeaderFlag.TB_OVERFLOW), "tb_overflow"),
    (int(QprofHeaderFlag.CLEAN_EXIT | QprofHeaderFlag.EDGE_OVERFLOW), "edge_overflow"),
    (int(QprofHeaderFlag.TB_OVERFLOW | QprofHeaderFlag.EDGE_OVERFLOW),
     "edge_overflow,tb_overflow,unclean_exit"),
])
def test_lower_bound(world, flags, reason):
    p = world.ingest(world.qprof(header={"flags": flags}))
    assert not p.complete
    assert p.collection["overflow"] == bool(flags & 6)
    assert {u["status"] for u in p.units} == {"lower_bound"}
    assert {u["reason"] for u in p.units} == {reason}
    u0 = uids(world)[0]
    v = P.ProfileView.open(p, world.target())
    c = v.count(u0, "steady")
    assert c.value == 2 and c.status == "lower_bound" and not c.exact  # observed values kept
    assert all(e["status"] == "lower_bound" for e in p.edges)
    assert not P.check_flow(p).exact


def test_lower_bound_never_exact_for_unclean_exit(world):
    p = world.ingest(world.qprof(header={"flags": 0}))
    v = P.ProfileView.open(p, world.target())
    assert all(c.status != "exact" for ph in ("boot", "steady") for c in v.phase(ph).values())
    u1, u2 = uids(world)[1:]
    assert v.edge_count(u1, u2, "boot").status == "lower_bound"


def test_edge_overflow_breaks_flow_but_is_flagged(world):
    # The producer dropped an edge row: the flow check fails and says it is not exact.
    path = world.qprof(header={"flags": int(QprofHeaderFlag.CLEAN_EXIT
                                            | QprofHeaderFlag.EDGE_OVERFLOW)})
    from scopt.formats import qprof_v1 as Q
    q = Q.read_qprof(path)
    data = Q.encode_qprof(q.header, q.phases, q.tbs, q.edges[1:])
    open(path, "wb").write(data)
    f = P.check_flow(world.ingest(path))
    assert not f.ok and not f.exact


# ---------------------------------------------------------------------------
# unknown reasons
# ---------------------------------------------------------------------------

def test_unknown_phase_not_reached(world):
    p = world.ingest(world.qprof(trace=world.trace()[:7], phase_starts={}))
    assert p.complete  # an unreached phase does not make the profile incomplete
    assert p.phases[1]["reached"] is False and p.phases[1]["match"] is None
    assert p.phases[1]["first_dispatch_index"] is None
    assert all("1" not in u["phase"] for u in p.units)  # unknown is not stored as zero
    v = P.ProfileView.open(p, world.target())
    u0 = uids(world)[0]
    assert v.count(u0, "steady") == P.Count(None, "unknown", "phase_not_reached")
    assert v.count(u0, "boot").exact
    assert v.edge_count(u0, u0, "steady") == P.Count(None, "unknown", "phase_not_reached")


def test_unknown_not_in_domain(world):
    p = world.ingest()
    # The staged .sc as a target: its two appended units were never in the domain.
    t = world.target(world.staged_sc)
    v = P.ProfileView.open(p, t)
    assert not v.same_snapshot and v.coverage == 1.0
    staged_uids = read_sc(world.staged_sc).unit_ids()
    assert v.count(staged_uids[3], "boot") == P.Count(None, "unknown", "not_in_domain")
    assert v.count(staged_uids[4], "steady") == P.Count(None, "unknown", "not_in_domain")
    assert v.count(staged_uids[0], "boot") == P.Count(2, "exact")
    assert v.count("tb:" + "0" * 32 + "#0", "boot").reason == "not_in_domain"


def test_unknown_not_collected(world):
    v = P.ProfileView.not_collected(world.target())
    u0 = uids(world)[0]
    assert v.count(u0, "steady") == P.Count(None, "unknown", "not_collected")
    assert all(c.reason == "not_collected" for c in v.phase("boot").values())
    assert v.verdict() == {"profile_id": None, "verdict": "not_collected"}
    # a phase the profile does not define is not collected either
    p = world.ingest()
    v2 = P.ProfileView.open(p, world.target())
    assert v2.count(u0, "warmup") == P.Count(None, "unknown", "not_collected")


# ---------------------------------------------------------------------------
# TRANSIENT and PROFILE_ONLY
# ---------------------------------------------------------------------------

def test_transient_rows_do_not_affect_completeness(world):
    p = world.ingest()
    assert p.transient["entries"] == 1 and p.transient["x_rows"] == 1
    assert p.collection["runs"][0]["transient_entries"] == 1
    assert p.complete and all(u["status"] == "exact" for u in p.units)
    assert p.transient["x_phase"]["0"]["entries"] == 1
    assert P.check_flow(p).ok
    # without the transient TB the units' statuses are the same
    tr = [d for d in world.trace() if d.tb_offset != TRANSIENT_OFF]
    p2 = world.ingest(world.qprof("nt.qprof", trace=tr))
    assert p2.complete and p2.transient["entries"] == 0
    assert [u["status"] for u in p2.units] == [u["status"] for u in p.units]


def test_profile_only_rows_are_never_merged_into_units(world):
    p = world.ingest()
    u0 = uids(world)[0]
    staged = read_sc(world.staged_sc)
    assert staged.records[3].tb_uid == staged.records[0].tb_uid  # same identity...
    po = p.profile_only
    assert [r["x_slot"] for r in po] == [3, 4]
    assert po[0]["guest_hash"] == hex(staged.records[0].guest_hash)
    assert po[0]["phase"]["0"] == {"entries": 1, "aborted": 0, "longjmp": 0, "lookup_hits": 1}
    # ...but its entry is not attributed to slot 0's UnitId
    assert cnt(p, u0, 0)["entries"] == 2 and cnt(p, u0, 0)["lookup_hits"] == 0
    assert all(not e["dst"].startswith("po:") for e in p.edges)
    # merging two runs concatenates profile-only rows; never folds them
    p2 = world.ingest(world.qprof("r2.qprof", header={"created_realtime_ns": 7}))
    m = P.merge([p, p2])
    assert len(m.profile_only) == 4
    assert sorted(r["x_run"] for r in m.profile_only) == [0, 0, 1, 1]
    assert cnt(m, u0, 0)["entries"] == 4
    assert P.check_flow(m).ok


# ---------------------------------------------------------------------------
# compatibility rejects (§3.8)
# ---------------------------------------------------------------------------

def test_compat_bundle(world):
    p = world.ingest()
    with pytest.raises(P.ProfileIncompatible) as ei:
        P.ProfileView.open(p, world.target(bundle_id="sha256:" + "e" * 64))
    assert ei.value.reason == "profile_incompatible:bundle" and ei.value.what == "bundle"
    with pytest.raises(P.ProfileIncompatible) as ei:  # unknown target bundle: fail closed
        P.ProfileView.open(p, P.ProfileTarget.from_any(world.snap_sc))
    assert ei.value.reason == "profile_incompatible:bundle"


def test_compat_contract(world):
    p = world.ingest()
    with pytest.raises(P.ProfileIncompatible) as ei:
        P.ProfileView.open(p, world.target(contract_id="sha256:" + "e" * 64))
    assert ei.value.reason == "profile_incompatible:contract"


@pytest.mark.parametrize("field", P.RUNTIME_COMPAT_FIELDS)
def test_compat_runtime(world, field):
    p = world.ingest()
    rt = dict(RUNTIME, **{field: "sha256:" + "f" * 64})
    with pytest.raises(P.ProfileIncompatible) as ei:
        P.ProfileView.open(p, world.target(runtime_identity=rt))
    assert ei.value.reason == "profile_incompatible:runtime" and field in ei.value.detail


def test_compat_runtime_other_fields_may_differ(world):
    p = world.ingest()
    rt = dict(RUNTIME, qemu_exe_stat={"size": 9, "mtime_ns": 9})  # another copy of the binary
    v = P.ProfileView.open(p, world.target(runtime_identity=rt))
    assert v.verdict()["verdict"] == "compatible"


def test_compat_runtime_unverifiable_rejects(world):
    p = world.ingest(runtime_identity="sha256:" + "9" * 64)  # id only, no x_runtime
    assert p.runtime is None
    with pytest.raises(P.ProfileIncompatible) as ei:
        P.ProfileView.open(p, world.target())
    assert ei.value.reason == "profile_incompatible:runtime"
    # same id: fine without fields
    v = P.ProfileView.open(p, world.target(runtime_identity=None,
                                           runtime_identity_id="sha256:" + "9" * 64))
    assert v.same_snapshot
    # or the document is supplied by id
    v = P.ProfileView.open(p, world.target(), runtime_identities={"sha256:" + "9" * 64: RUNTIME})
    assert v.same_snapshot


def test_compat_coverage(world):
    p = world.ingest()
    # other.sc keeps units 0 and 1 and replaces unit 2. Steady entries: u0 2, u1 2, u2 0
    # -> 100 % coverage in steady; boot entries u0 2, u1 1, u2 2 -> 3/5 = 60 %.
    v = P.ProfileView.open(p, world.target(world.other_sc), phase="steady")
    assert v.coverage == 1.0 and not v.same_snapshot
    with pytest.raises(P.ProfileIncompatible) as ei:
        P.ProfileView.open(p, world.target(world.other_sc), phase="boot")
    assert ei.value.reason == "profile_incompatible:coverage"
    assert ei.value.coverage == pytest.approx(3 / 5)
    v = P.ProfileView.open(p, world.target(world.other_sc), phase="boot", min_profile_match=0.5)
    other_uids = read_sc(world.other_sc).unit_ids()
    assert v.count(other_uids[2], "boot").reason == "not_in_domain"
    assert v.count(other_uids[0], "boot") == P.Count(2, "exact")
    assert v.verdict()["x_coverage"] == 0.6


def test_artifact_dir_target(world, tmp_path):
    import os
    import shutil
    p = world.ingest()
    art = tmp_path / "art"
    art.mkdir()
    shutil.copy(world.snap_sc, art / "cache.sc")
    (art / "cache.sc.scopt.json").write_text(json.dumps({
        "input_snapshot_id": p.snapshot_id, "bundle_id": SHA_BUNDLE,
        "runtime_identity_id": "sha256:" + "4" * 64}))
    (art / "bundle.json").write_text(json.dumps({"bundle_id": SHA_BUNDLE,
                                                 "contract_id": SHA_CONTRACT}))
    (art / "runtime_identity.json").write_text(json.dumps(RUNTIME))
    t = P.ProfileTarget.from_any(str(art))
    assert t.kind == "artifact" and t.input_snapshot_ids == (p.snapshot_id,)
    v = P.ProfileView.open(p, str(art))
    assert v.count(uids(world)[0], "boot").exact
    assert os.path.exists(art / "cache.sc")


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

def test_merge_rules(world):
    inp = [{"name": "a", "sha256": "sha256:" + "a" * 64, "split": "train"}]
    p1 = world.ingest(world.qprof("1.qprof"), inputs=inp, time_dilation=2.0)
    p2 = world.ingest(world.qprof("2.qprof", header={"created_realtime_ns": 5}),
                      inputs=[dict(inp[0], name="b")], time_dilation=4.0)
    m = P.merge([p1, p2])
    assert P.merge([p2, p1]) == m  # order independent
    u0, u1, u2 = uids(world)
    assert cnt(m, u0, 1)["entries"] == 4 and cnt(m, u2, 0)["aborted"] == 2
    assert m.collection["repetitions"] == 2 and len(m.collection["runs"]) == 2
    assert sorted(i["name"] for i in m.collection["inputs"]) == ["a", "b"]
    assert m.collection["time_dilation"] == 3.0 and m.complete
    assert m.transient["entries"] == 2
    assert {e["count"] for e in m.edges if e["kind"] == "first"} == {2}
    assert m.phases[1]["reached"] and m.phases[1]["x_reached_runs"] == 2
    f = P.check_flow(m)
    assert f.ok and f.runs == 2 and f.first_edges == 2
    assert schemas.errors(m.doc, "profile.v1") == []
    assert P.merge([m]) == m


def test_merge_status_is_minimum(world):
    p1 = world.ingest(world.qprof("1.qprof"))
    p2 = world.ingest(world.qprof("2.qprof", header={"flags": 0}))
    m = P.merge([p1, p2])
    assert not m.complete
    assert {u["status"] for u in m.units} == {"lower_bound"}
    assert {u["reason"] for u in m.units} == {"unclean_exit"}
    assert {e["status"] for e in m.edges} == {"lower_bound"}


def test_merge_phase_reached_by_all(world):
    p1 = world.ingest(world.qprof("1.qprof"))
    p2 = world.ingest(world.qprof("2.qprof", trace=world.trace()[:7], phase_starts={}))
    m = P.merge([p1, p2])
    assert not m.phases[1]["reached"] and m.phases[1]["x_reached_runs"] == 1
    v = P.ProfileView.open(m, world.target())
    assert v.count(uids(world)[0], "steady").reason == "phase_not_reached"
    assert v.count(uids(world)[0], "boot") == P.Count(4, "exact")
    assert P.check_flow(m).ok


def test_merge_rejections(world):
    p1 = world.ingest()
    other_phases = ({"name": "boot", "marker": 0}, {"name": "steady", "marker": 0x20000600})
    p2 = world.ingest(world.qprof("2.qprof", phases=other_phases))
    with pytest.raises(P.ProfileMergeError) as ei:
        P.merge([p1, p2])
    assert ei.value.reason == "merge_incompatible:phases"
    p3 = world.ingest(bundle_id="sha256:" + "d" * 64)
    with pytest.raises(P.ProfileMergeError) as ei:
        P.merge([p1, p3])
    assert ei.value.reason == "merge_incompatible:bundle_id"
    d = p1.doc
    d["snapshot_id"] = "sha256:" + "5" * 64
    p4 = P.build_profile_doc(d)
    with pytest.raises(P.ProfileMergeError) as ei:
        P.merge([p1, p4])
    assert ei.value.reason == "merge_incompatible:snapshot_id"
    d = p1.doc
    d["collection"]["method"] = "sampled_v0"
    with pytest.raises(P.ProfileFormatError):  # only one method exists in v1
        P.build_profile_doc(d)
    with pytest.raises(P.ProfileMergeError):
        P.merge([])


# ---------------------------------------------------------------------------
# flow conservation
# ---------------------------------------------------------------------------

def test_broken_flow_is_detected_in_a_profile(world):
    p = world.ingest()
    d = p.doc
    d["edges"][1]["count"] += 1
    f = P.check_flow(P.build_profile_doc(d))
    assert not f.ok and f.exact
    assert any(v.startswith("in-flow") for v in f.violations)
    d = p.doc
    d["units"][0]["phase"]["0"]["entries"] += 1
    f = P.check_flow(P.build_profile_doc(d))
    assert not f.ok


def test_broken_flow_is_detected_from_a_qprof(world, tmp_path):
    o = world.offs
    rows = B.qprof_rows_for(world.units[:3])
    rows[0]["phase"] = [{"entries": 2}, {}]
    rows[1]["phase"] = [{"entries": 1}, {}]
    edges = [{"src": None, "dst": o[0], "kind": "first"},
             {"src": o[0], "dst": o[1], "kind": "chain0"},
             {"src": o[1], "dst": o[0], "kind": "chain0"},
             {"src": o[0], "dst": o[1], "kind": "chain1"}]  # a 4th entry that never happened
    path = str(tmp_path / "bad.qprof")
    B.build_qprof(rows, edges, phases=PHASES, header=world.binding(), path=path)
    # A complete profile with broken conservation is a producer defect: refused.
    with pytest.raises(P.ProfileIngestError) as ei:
        world.ingest(path)
    assert ei.value.reason == "flow_conservation" and "in-flow" in ei.value.detail
    # Opting out returns it, and check_flow reports the same violation.
    p = world.ingest(path, require_flow=False)
    f = P.check_flow(p)
    assert p.complete and not f.ok and f.exact
    assert any("in-flow" in v for v in f.violations)
    # After an overflow the same file is lower_bound and is not refused.
    path2 = str(tmp_path / "bad-ovf.qprof")
    hdr = dict(world.binding(), flags=int(QprofHeaderFlag.CLEAN_EXIT | QprofHeaderFlag.EDGE_OVERFLOW))
    B.build_qprof(rows, edges, phases=PHASES, header=hdr, path=path2)
    p2 = world.ingest(path2)
    assert not p2.complete and not P.check_flow(p2).ok


# ---------------------------------------------------------------------------
# review follow-ups (wp6-r2)
# ---------------------------------------------------------------------------

def test_sc_binding_recorded(world):
    p = world.ingest(runtime_identity="sha256:" + "9" * 64)  # opaque id: no x_runtime
    hdr = read_sc(world.snap_sc).header
    assert p.runtime is None
    assert p.sc_binding == {"code_address": hex(hdr.code_address),
                            "runtime_signature": hex(hdr.runtime_signature)}
    assert p.runtime_signature == hdr.runtime_signature
    assert schemas.errors(p.doc, "profile.v1") == []


def test_compat_runtime_signature_of_target_sc(world, tmp_path):
    """Same runtime_identity_id on both sides, but the target .sc was written by
    another runtime (header runtime_signature differs): reject."""
    rid = "sha256:" + "9" * 64
    p = world.ingest(runtime_identity=rid)
    other = str(tmp_path / "other-rt.sc")
    B.build_sc(B.simple_units(3), {"code_size": world.code_size,
                                   "runtime_signature": 0x1234_5678_9abc_def0}, path=other)
    # the unit ids do not depend on runtime_signature: the join alone would pass
    assert read_sc(other).unit_ids() == uids(world)
    with pytest.raises(P.ProfileIncompatible) as ei:
        P.ProfileView.open(p, world.target(other, runtime_identity=None, runtime_identity_id=rid))
    assert ei.value.reason == "profile_incompatible:runtime"
    assert "runtime_signature" in ei.value.detail
    # same .sc header: fine
    v = P.ProfileView.open(p, world.target(runtime_identity=None, runtime_identity_id=rid))
    assert v.same_snapshot
    # a merged profile keeps the binding and is rejected the same way
    m = P.merge([p, world.ingest(world.qprof("r2.qprof", header={"created_realtime_ns": 3}),
                                 runtime_identity=rid)])
    assert m.sc_binding == p.sc_binding
    with pytest.raises(P.ProfileIncompatible):
        P.ProfileView.open(m, world.target(other, runtime_identity=None, runtime_identity_id=rid))


def test_merge_rejects_duplicate_runs(world):
    p = world.ingest()
    with pytest.raises(P.ProfileMergeError) as ei:
        P.merge([p, p])
    assert ei.value.reason == "merge_duplicate_run"
    # a different file but a reused run_id is also rejected
    p2 = world.ingest(world.qprof("2.qprof", header={"created_realtime_ns": 9}),
                      run_id=p.collection["runs"][0]["run_id"])
    with pytest.raises(P.ProfileMergeError) as ei:
        P.merge([p, p2])
    assert ei.value.reason == "merge_duplicate_run"
    # a merged profile cannot take one of its runs again
    p3 = world.ingest(world.qprof("3.qprof", header={"created_realtime_ns": 11}))
    m = P.merge([p, p3])
    with pytest.raises(P.ProfileMergeError) as ei:
        P.merge([m, p3])
    assert ei.value.reason == "merge_duplicate_run"


def test_coverage_of_unreached_phase_uses_reached_phases(world):
    p = world.ingest(world.qprof("unreached.qprof", phase_starts={}))
    assert not p.phase_reached(1)
    # boot (the only reached phase): u0 4, u1 3, u2 2 on snap.sc; other.sc lacks u2
    with pytest.raises(P.ProfileIncompatible) as ei:
        P.ProfileView.open(p, world.target(world.other_sc), phase="steady")
    assert ei.value.reason == "profile_incompatible:coverage"
    assert ei.value.coverage < 1.0
    v = P.ProfileView.open(p, world.target(world.other_sc), phase="steady",
                           min_profile_match=0.0)
    assert v.coverage_phase is None and v.coverage == ei.value.coverage
    assert v.count(uids(world)[0], "steady").reason == "phase_not_reached"


def test_artifact_via_input_snapshot(world, tmp_path):
    p = world.ingest()
    v = P.ProfileView.open(p, world.target(kind="artifact",
                                           input_snapshot_ids=(p.snapshot_id,)))
    assert v.via_input_snapshot is True and v.verdict()["x_via_input_snapshot"] is True
    v = P.ProfileView.open(p, world.target(kind="artifact",
                                           input_snapshot_ids=("sha256:" + "6" * 64,)))
    assert v.via_input_snapshot is False
    assert "x_via_input_snapshot" not in P.ProfileView.open(p, world.target()).verdict()


# ---------------------------------------------------------------------------
# ingest join failures (fail closed)
# ---------------------------------------------------------------------------

def _reason(fn):
    with pytest.raises(P.ProfileIngestError) as ei:
        fn()
    return ei.value.reason


def test_ingest_rejections(world, tmp_path):
    q = world.qprof()
    assert _reason(lambda: world.ingest(world.qprof("c.qprof", header={
        "loaded_record_count": 0, "loaded_header_checksum": 0, "loaded_max_end": 0}))) \
        == "not_warm"
    # other.sc has an identical header (same count, sizes, checksum), so only the
    # record/arena comparison with the staged copy can tell it apart
    assert _reason(lambda: world.ingest(q, snapshot=world.other_sc)) == "staged_mismatch"
    for hdr in ({"loaded_record_count": 2}, {"loaded_header_checksum": 1},
                {"loaded_max_end": 0x40}):
        assert _reason(lambda: world.ingest(world.qprof("m.qprof", header=hdr))) \
            == "snapshot_mismatch"
    assert _reason(lambda: world.ingest(world.qprof("s.qprof", header={
        "runtime_signature": 1}))) == "runtime_signature_mismatch"
    assert _reason(lambda: world.ingest(world.qprof("a.qprof", header={
        "code_address": 0x7FFF00000000}))) == "code_address_mismatch"
    # staged copy that is not this snapshot plus appends
    assert _reason(lambda: world.ingest(q, staged=world.other_sc)) == "staged_mismatch"
    # a row whose key differs from its slot
    rows = [dict(r) for r in world.rows]
    rows[1]["icount"] = 9
    bad = str(tmp_path / "k.qprof")
    B.simulate_qprof(rows, world.trace(), phases=PHASES, phase_starts={1: 7},
                     header=world.binding(), path=bad)
    assert _reason(lambda: world.ingest(bad)) == "row_key_mismatch"
    # a row that matches nothing
    rows = world.rows + [{"tb_offset": 0x7000}]
    tr = world.trace() + [B.Dispatch(0x7000)]
    bad = str(tmp_path / "u.qprof")
    B.simulate_qprof(rows, tr, phases=PHASES, phase_starts={1: 7}, header=world.binding(),
                     path=bad)
    assert _reason(lambda: world.ingest(bad)) == "unjoined_row"
    # counts in a phase the run never reached
    r0 = dict(world.rows[0], phase=[{"entries": 1}, {"entries": 1}])
    bad = str(tmp_path / "p.qprof")
    B.build_qprof([r0], [{"src": None, "dst": r0["tb_offset"], "kind": "first"}],
                  phases=[PHASES[0], dict(PHASES[1], reached=0)], header=world.binding(),
                  path=bad)
    assert _reason(lambda: world.ingest(bad)) == "counts_in_unreached_phase"
    # an edge to a row that does not exist (no overflow flag)
    bad = str(tmp_path / "e.qprof")
    B.build_qprof([dict(world.rows[0], phase=[{"entries": 1}, {}])],
                  [{"src": None, "dst": 0x9999, "kind": "first"}], phases=PHASES,
                  header=world.binding(), path=bad)
    assert _reason(lambda: world.ingest(bad)) == "edge_unknown_row"
    # phase table must start with boot
    bad = str(tmp_path / "ph.qprof")
    B.build_qprof([], [], phases=[{"name": "steady", "marker": 0}], header=world.binding(),
                  path=bad)
    assert _reason(lambda: world.ingest(bad)) == "phase_table"
    with pytest.raises(ValueError):
        world.ingest(q, bundle_id="nope")


def test_bad_qprof_file_is_rejected_by_ingest(world):
    q = world.qprof()
    data = bytearray(open(q, "rb").read())
    data[-20] ^= 1
    open(q, "wb").write(bytes(data))
    from scopt.formats.qprof_v1 import QprofFormatError
    with pytest.raises(QprofFormatError) as ei:
        world.ingest(q)
    assert ei.value.reason == "trailer"


# ---------------------------------------------------------------------------
# file I/O
# ---------------------------------------------------------------------------

def test_write_read_publish(world, tmp_path):
    p = world.ingest()
    path = P.write_profile(tmp_path / "profile.json", p)
    assert open(path, "rb").read() == p.to_bytes()
    assert P.read_profile(path) == p
    gz = P.write_profile(tmp_path / "profile.json.gz", p)
    assert gzip.decompress(open(gz, "rb").read()) == p.to_bytes()
    assert P.read_profile(gz) == p
    store = tmp_path / "store"
    pub = P.publish_profile(store, p)
    assert pub == str(store / "profiles" / p.profile_id.split(":", 1)[1] / "profile.json")
    assert P.publish_profile(store, p) == pub  # idempotent
    assert P.read_profile(pub).profile_id == p.profile_id


def test_read_rejects_tampering(world, tmp_path):
    p = world.ingest()
    d = p.doc
    d["units"][0]["phase"]["0"]["entries"] += 1
    path = tmp_path / "t.json"
    path.write_bytes(canonical_json(d))
    with pytest.raises(P.ProfileIdMismatch):
        P.read_profile(path)
    path.write_bytes(json.dumps(p.doc, indent=1).encode())
    with pytest.raises(P.ProfileFormatError) as ei:
        P.read_profile(path)
    assert ei.value.reason == "not_canonical"
    assert P.read_profile(path, require_canonical=False) == p
    d = p.doc
    d["units"][0]["status"] = "maybe"
    path.write_bytes(canonical_json(d))
    with pytest.raises(P.ProfileFormatError) as ei:
        P.read_profile(path)
    assert ei.value.reason == "schema"


def test_status_min():
    assert P.status_min("exact", "lower_bound") == "lower_bound"
    assert P.status_min("exact", "exact") == "exact"
    assert P.status_min("lower_bound", "unknown", "exact") == "unknown"


def test_snapshot_view_like_target(world):
    """A SnapshotView-like object (slotted dataclasses, as WP7/WP8 will pass)."""
    from dataclasses import dataclass

    @dataclass(frozen=True, slots=True)
    class Bundle:
        bundle_id: str
        contract_id: str

    @dataclass(frozen=True, slots=True)
    class Runtime:
        runtime_identity_id: str
        qemu_sha256: str
        plugin_sha256: str
        runtime_signature: str
        prologue_sha256: str
        cpuinfo: str

    sc = read_sc(world.snap_sc)

    class Unit:
        def __init__(self, uid, u):
            r = u.record
            self.uid, self.slot, self.tb_offset, self.end_offset = uid, u.slot, r.tb_offset, \
                r.end_offset
            self.key, self.guest_size, self.icount, self.guest_hash = r.key, r.guest_size, \
                r.icount, r.guest_hash
            self.record, self.code, self.search = u.record_bytes, u.code, u.search

    class View:
        snapshot_id = sc.snapshot_id()
        header = sc.header
        bundle = Bundle(SHA_BUNDLE, SHA_CONTRACT)
        runtime = Runtime("sha256:" + "7" * 64, *(RUNTIME[k] for k in P.RUNTIME_COMPAT_FIELDS))

        def units(self):
            return [Unit(uid, u) for uid, u in zip(sc.unit_ids(), sc.units())]

    p = world.ingest(snapshot=View())
    assert p.snapshot_id == sc.snapshot_id()
    t = P.ProfileTarget.from_any(View())
    assert t.bundle_id == SHA_BUNDLE and t.runtime_identity["qemu_sha256"] == RUNTIME["qemu_sha256"]
    v = P.ProfileView.open(p, View())
    assert v.same_snapshot and v.count(sc.unit_ids()[0], "boot").exact
