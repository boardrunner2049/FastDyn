"""WP6: profile.counts.v1 analysis and the ``scopt profile`` CLI."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import scopt
from scopt.analysis import profile as A
from scopt.analysis.api import AnalysisSpec, B1_ANALYSES, ScopeRef
from scopt.cli import cli, load_commands
from scopt.formats import profile_v1 as P
from scopt.formats.sc_records import read_sc
from tests.synth.profile_world import RUNTIME, SHA_BUNDLE, SHA_CONTRACT, make_world


@pytest.fixture
def world(tmp_path):
    return make_world(str(tmp_path / "w"))


def test_imports_this_tree():
    assert hasattr(scopt, "__file__")
    import scopt.formats.profile_v1 as m
    assert m.METHOD == "exact_dispatch_unchained_v1"


def test_spec_matches_registry():
    assert isinstance(A.SPEC, AnalysisSpec)
    assert (A.SPEC.name, A.SPEC.version) == ("profile.counts", B1_ANALYSES["profile.counts"])
    assert A.SPEC.scope == "snapshot" and A.SPEC.result_schema is A.ProfileCounts


def test_counts_with_profile(world):
    p = world.ingest()
    v = P.ProfileView.open(p, world.target())
    pc = A.compute_profile_counts(v)
    u0, u1, u2 = read_sc(world.snap_sc).unit_ids()
    assert pc.phases == ("boot", "steady") and pc.profile_id == p.profile_id
    assert pc.phase("steady")[u0] == P.Count(2, "exact")
    assert pc.phase("steady")[u2] == P.Count(0, "exact")
    assert pc.executions("boot")[u2] == P.Count(1, "exact")
    assert pc.counts("boot", "longjmp")[u2] == P.Count(1, "exact")
    assert pc.phase("warmup")[u0].reason == "not_collected"
    with pytest.raises(KeyError):
        pc.counts("boot", "nope")
    assert pc.result_hash() == A.compute_profile_counts(v).result_hash()
    json.dumps(pc.to_json())


def test_counts_without_profile(world):
    uids = read_sc(world.snap_sc).unit_ids()
    pc = A.compute_profile_counts(None, uids, snapshot_id="sha256:" + "0" * 64)
    assert pc.profile_id is None and pc.phases == ()
    assert all(c == P.Count(None, "unknown", "not_collected")
               for c in pc.phase("steady").values())
    assert list(pc.phase("steady")) == uids


def test_counts_unreached_phase(world):
    p = world.ingest(world.qprof(trace=world.trace()[:7], phase_starts={}))
    pc = A.compute_profile_counts(P.ProfileView.open(p, world.target()))
    assert {c.reason for c in pc.phase("steady").values()} == {"phase_not_reached"}


def test_spec_compute_through_a_context(world):
    p = world.ingest()
    v = P.ProfileView.open(p, world.target())
    snapv = SimpleNamespace(snapshot_id=p.snapshot_id,
                            units=lambda: [SimpleNamespace(uid=u) for u in v.target.unit_ids])
    ctx = SimpleNamespace(profile_view=v, snapshot=snapv)
    out = A.SPEC.compute(ctx, ScopeRef("snapshot", p.snapshot_id))
    assert out.phase("boot") == A.compute_profile_counts(v).phase("boot")
    ctx2 = SimpleNamespace(profile=lambda phase: None, snapshot=snapv)
    out2 = A.SPEC.compute(ctx2, ScopeRef("snapshot", p.snapshot_id))
    assert {c.reason for c in out2.phase("boot").values()} == {"not_collected"}
    with pytest.raises(ValueError):
        A.SPEC.compute(ctx, ScopeRef("unit", "x"))


def _run(args):
    load_commands()
    r = CliRunner().invoke(cli, args, catch_exceptions=False)
    return r.exit_code, r.output


def test_cli_ingest_check_merge_show(world, tmp_path):
    rt = tmp_path / "rt.json"
    rt.write_text(json.dumps(RUNTIME))
    common = ["--staged-sc", world.staged_sc, "--snapshot", world.snap_sc,
              "--bundle-id", SHA_BUNDLE, "--contract-id", SHA_CONTRACT,
              "--runtime-identity", str(rt)]
    q1 = world.qprof("1.qprof")
    q2 = world.qprof("2.qprof", header={"created_realtime_ns": 3})
    code, out = _run(["profile", "ingest", q1, *common, "--out", str(tmp_path / "p1.json"),
                      "--store", str(tmp_path / "store")])
    assert code == 0, out
    res = json.loads(out)
    assert res["complete"] and res["flow"]["ok"] and res["units"] == 3
    assert res["profile_only"] == 2 and res["transient_entries"] == 1
    code, out = _run(["profile", "ingest", q2, *common, "--out", str(tmp_path / "p2.json")])
    assert code == 0
    code, out = _run(["profile", "check", str(tmp_path / "p1.json"), str(tmp_path / "p2.json")])
    assert code == 0 and [json.loads(x)["ok"] for x in out.splitlines()] == [True, True]
    code, out = _run(["profile", "merge", str(tmp_path / "p1.json"), str(tmp_path / "p2.json"),
                      "--out", str(tmp_path / "m.json")])
    assert code == 0 and json.loads(out)["runs"] == 2
    code, out = _run(["profile", "show", str(tmp_path / "m.json"), "--phase", "steady"])
    assert code == 0
    lines = [json.loads(x) for x in out.splitlines()]
    assert lines[0]["verdict"] == "compatible" and lines[1]["value"] == 4
    code, out = _run(["profile", "show", str(tmp_path / "m.json"), "--target", world.snap_sc,
                      "--bundle-id", "sha256:" + "e" * 64, "--contract-id", SHA_CONTRACT])
    assert code == 2 and "profile_incompatible:bundle" in out
    code, out = _run(["profile", "qprof-dump", q1])
    assert code == 0 and json.loads(out)["header"]["total_dispatches"] == 12
    bad = tmp_path / "bad.qprof"
    bad.write_bytes(open(q1, "rb").read()[:-1])
    code, out = _run(["profile", "qprof-dump", str(bad)])
    assert code == 2 and json.loads(out)["rejected"] == "size"
    code, out = _run(["profile", "ingest", q1, *common[:2], "--snapshot", world.other_sc,
                      *common[4:]])
    assert code == 2 and json.loads(out)["rejected"] == "staged_mismatch"


def test_cli_merge_duplicate_and_flow_refusal(world, tmp_path):
    from tests.synth import builders as B
    from tests.synth.profile_world import PHASES

    rt = tmp_path / "rt.json"
    rt.write_text(json.dumps(RUNTIME))
    common = ["--staged-sc", world.staged_sc, "--snapshot", world.snap_sc,
              "--bundle-id", SHA_BUNDLE, "--contract-id", SHA_CONTRACT,
              "--runtime-identity", str(rt)]
    code, out = _run(["profile", "ingest", world.qprof("1.qprof"), *common,
                      "--out", str(tmp_path / "p1.json")])
    assert code == 0, out
    code, out = _run(["profile", "merge", str(tmp_path / "p1.json"), str(tmp_path / "p1.json"),
                      "--out", str(tmp_path / "dup.json")])
    assert code == 2 and json.loads(out)["rejected"] == "merge_duplicate_run"
    # a clean, non-overflowed qprof whose edges break conservation
    o = world.offs
    rows = B.qprof_rows_for(world.units[:3])
    rows[0]["phase"] = [{"entries": 1}, {}]
    edges = [{"src": None, "dst": o[0], "kind": "first"},
             {"src": o[0], "dst": o[0], "kind": "chain0"}]
    bad = str(tmp_path / "bad.qprof")
    B.build_qprof(rows, edges, phases=PHASES, header=world.binding(), path=bad)
    code, out = _run(["profile", "ingest", bad, *common])
    assert code == 2 and json.loads(out)["rejected"] == "flow_conservation"
    code, out = _run(["profile", "ingest", bad, *common, "--no-require-flow"])
    assert code == 0 and not json.loads(out)["flow"]["ok"]
