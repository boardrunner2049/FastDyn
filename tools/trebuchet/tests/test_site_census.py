"""Correctness checks with synthetic captured code and an untimed test histogram."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/scopt"))
sys.path.insert(0, str(ROOT / "tools/trebuchet/lib"))
pytest.importorskip("capstone")

from scopt.analysis.hostops import OpKind
from scopt.canon import sha256_id
from scopt.importer.import_cache import import_snapshot
from site_census import at_most_once, census, weight
from tests.synth import builders as B
from tests.synth.rich_tb import rich_units
from tests.unit.test_wp7_r2_identity import _bundle, _manifest, _native, _pair, _ri


def test_unexecuted_conditional_and_repeated_sites():
    ops = [NS(kind=kind, target=target, off=i, block="insn") for i, (kind, target) in enumerate([
        (OpKind.JCC, 3), (OpKind.CALL, None), (OpKind.JCC, 1), (OpKind.NOP, None)])]
    host = NS(ops=ops, status="ok")
    graph = NS(succ_full=[(1, 3), (2,), (1, 3), ()], ok=True, exits={})
    store = NS(get=lambda name, scope: NS(value=host if name == "hostops" else graph))
    unit = NS(uid="source", native_fragments=[], stub_start=None)
    assert at_most_once(store, unit) == {0, 3}
    assert weight(("source", 3, "90"), {}, 17) == {"kind": "profile_upper_bound", "executions": 17}
    assert weight(("private", 3, "90"), {}, None)["kind"] == "unknown"
    ops[0].target = None
    assert at_most_once(store, unit) == set()


def test_imported_image_census_and_sparse_untimed_counts(tmp_path):
    elf = ROOT / "tests/binaries/world_rlc_gpio/world_rlc_gpio.elf"
    digest = sha256_id(elf.read_bytes())
    units = rich_units(1)
    source, scm = _pair(tmp_path / "prep", units)
    man = _manifest({"firmware": _native(elf)})
    Path(source + ".fastdyn.json").write_text(json.dumps(man))
    runtime, bundle = _ri(source, scm), _bundle(man, digest)
    result = import_snapshot(str(tmp_path / "prep"), store=str(tmp_path / "store"),
                             bundle=bundle, runtime_identity=runtime, use_manifest_firmware=False)
    from scopt.formats.profile_v1 import ingest, write_profile

    qprof = tmp_path / "run.qprof"
    B.simulate_qprof(B.qprof_rows_for(units), [units[0].tb_offset] * 7,
                     header=B.qprof_binding(source), path=str(qprof))
    prof = ingest(qprof, source, result.snapshot, bundle_id=bundle["bundle_id"],
                  contract_id=bundle["contract_id"], runtime_identity=runtime["runtime_identity_id"])
    profile_path = tmp_path / "profile.json"
    write_profile(profile_path, prof)
    uid = next(iter(result.snapshot.units())).uid
    args = NS(snapshot=result.store_dir, elf=str(elf), application="fixture", scenario="test",
              phase="boot", profile=profile_path, site_counts=tmp_path / "hits.json", optimizer_result=None)
    identity = dict(image_hash=digest, snapshot_id=result.snapshot.snapshot_id, phase=args.phase,
                    application=args.application, scenario=args.scenario, profile_id=prof.profile_id)
    sites = [{"body": uid, "offset": 41, "bytes": "6690", "executions": 3}]
    doc = {"schema": "trebuchet.untimed-site-counts/1", "identity": identity, "sites": sites}
    args.site_counts.write_text(json.dumps(doc))
    report = census(args)
    assert report["nop_volume"]["ordinary"]["observed_untimed_replay_bytes"] == 6
    assert {n["offset"] for n in report["nops"]} == {41, 103}
    calls = {c["offset"]: c for c in report["calls"] if c["physical_call_verified"]}
    assert calls.keys() == {29, 60, 92}
    assert calls[29]["cost_proxy"]["opmark_to_call_instructions"] == 1
    assert all(c["tb_entries_proxy"]["value"] == 7 for c in calls.values())
    assert all(c["weight"]["kind"] != "observed_untimed_replay" for c in calls.values())
    assert not report["encoding_capture_by_stage"]
    args.archived_ordinary = True
    archive_report = census(args)
    assert archive_report["import_check"]["units_checked"] == 1
    assert archive_report["nops"] == report["nops"]
    from archived_snapshot import archived_ordinary
    from scopt.ids import region_id

    # Even a correctly rehashed stored region cannot alter the physical NOP/
    # instruction boundary compared with the imported SCM and actual code.
    regions = Path(result.store_dir) / "units.jsonl"
    saved = regions.read_bytes()
    regions.chmod(0o600)
    edited = json.loads(saved)
    edited["native"]["pool_start"] -= 1
    edited["region_id"] = region_id(edited)
    regions.write_text(json.dumps(edited) + "\n")
    with pytest.raises(ValueError, match="physical region differs"):
        archived_ordinary(result.store_dir, elf)
    regions.write_bytes(saved)
    args.archived_ordinary = False
    sites[0]["bytes"] = "90"
    args.site_counts.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="unknown physical site"):
        census(args)
    doc["identity"]["scenario"] = "another"
    args.site_counts.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="identity"):
        census(args)


def test_site_capture_preserves_constant_propagation_decision():
    from scopt.passes.constant_propagation import ConstantPropagation
    from scopt.passes.branch_folding import BranchFolding

    code = bytes.fromhex("8b5d08")  # mov ebx,[rbp+8] cannot hold mov ebx,5
    op = NS(off=0, end=3, size=3, block="insn", log=None)
    facts = NS(ok=True, foldable={0: ("r0", 5, "ebx")}, decided={0: True})
    view = NS(src=NS(uid="target"), code=code, plan=NS(pred=("predecessor", 1)),
              analyses=lambda: (NS(ops=[op]), None, NS(live_out=[set()]), facts),
              factrefs=[], facts={}, variant_id="clone", weight=1)
    for cls in (ConstantPropagation, BranchFolding):
        def run(enabled):
            ctx = NS(params={"record_encoding_sites": enabled}, analysis=lambda *a: None)
            return cls().run(view, ctx, NS())
        before, after = run(False), run(True)
        assert before.reason == after.reason == "no_same_length_encoding"
        expected = "encoding_site|predecessor|1|target|0|8b5d08"
        assert expected not in before.stats and after.stats[expected] == 1


def test_private_site_weights_use_only_the_physical_copy(tmp_path, monkeypatch):
    import site_census as diag
    from scopt.formats.artifact import MANIFEST, SIDECAR, manifest_bytes, read_artifact
    from scopt.canon import canonical_json

    # The private-copy importer independently checks relocation and SCM patches.
    # Isolate the subsequent join: identical bytes occur in both physical bodies.
    unit = NS(uid="general", tc_offset=0, code=b"\x90", pool_start=None, ldst_end=1,
              inline_start=None, stub_start=None, tc_size=1, meta_status="ok")
    snap = NS(snapshot_id="generation", bundle={"image_hash": "elf", "bundle_id": "bundle"},
              header=NS(code_address=100), units=lambda: [unit])
    monkeypatch.setattr(diag, "open_snapshot", lambda _: snap)
    monkeypatch.setattr(diag, "load_guest_image", lambda _: NS(sha256="elf"))
    monkeypatch.setattr(diag, "prepare_native_images", lambda *_a, **kw:
                        NS(input_id="images", value=NS(bindings={}, capture_ids=[], reason=None)))
    monkeypatch.setattr(diag, "AnalysisStore", lambda *a, **kw: None)
    monkeypatch.setattr(diag, "_calls", lambda *a: ([], {}))
    monkeypatch.setattr(diag, "_inventory", lambda *a: ({}, {}, []))
    origin = ("pred", 0, unit.uid)
    bodies = [(unit.uid, unit, unit.code, 0, None), ("private", unit, unit.code, 32, origin)]
    monkeypatch.setattr(diag, "physical_bodies", lambda *_: (bodies, {origin: "private"}))
    art = tmp_path / "artifact"
    art.mkdir()
    (art / SIDECAR).write_bytes(canonical_json({"output_snapshot_id": snap.snapshot_id}))
    (art / MANIFEST).write_bytes(manifest_bytes(art))
    stage = {"pass": "constant-propagation", "outcome": "no_change",
             "params": {"record_encoding_sites": True}, "stats": {
                 "encoding_site|pred|0|general|0|90": 1,
                 "encoding_site|pred|0|general|0|91": 1,
                 "encoding_site|other|0|general|0|90": 1}}
    receipt = tmp_path / "result.json"
    receipt.write_text(json.dumps({"artifact_id": read_artifact(art).artifact_id,
                                   "artifact_dir": str(art), "passes": [stage]}))
    hits = tmp_path / "hits.json"
    identity = dict(application="test", scenario="branch", phase="boot", snapshot_id="generation",
                    image_hash="elf", profile_id=None)
    hits.write_text(json.dumps({"schema": "trebuchet.untimed-site-counts/1", "identity": identity,
                                "sites": [{"body": "private", "offset": 0, "bytes": "90",
                                           "executions": 9}]}))
    args = NS(snapshot="", elf="", application="test", scenario="branch", phase="boot", profile=None,
              site_counts=hits, optimizer_result=receipt)
    result = census(args)
    assert result["nop_volume"]["specialized"]["observed_untimed_replay_bytes"] == 9
    assert result["nop_volume"]["ordinary"]["unknown_sites"] == 1
    statuses = {r["status"]: r["weight"] for r in result["encoding_refusals"]}
    assert statuses["matched"]["executions"] == 9
    assert statuses["variant_absent"]["executions"] is None
    assert statuses["bytes_changed_or_uninspected"]["executions"] is None
    (art / SIDECAR).write_text("{}")
    with pytest.raises(ValueError, match="manifest differs"):
        census(args)


def test_unique_exit_nop_and_branch_bypass():
    from scopt.analysis.hcfg import HCfg
    from scopt.analysis.hostops import HostOp
    from site_census import exit_nop_evidence

    def op(off, kind, size=1, sites=()):
        return HostOp(off, size, "", kind, "insn", 0, sites=sites)

    host = NS(ops=(op(0, OpKind.JCC), op(1, OpKind.NOP, 3),
                   op(4, OpKind.JMP, 5, ("GOTO_TB",)), op(9, OpKind.JMP, 5, ("GOTO_TB",))))
    def cfg(succ):
        return HCfg("tb", True, (), 4, succ, succ, {}, {2: "goto_tb:0", 3: "goto_tb:1"})
    graph = cfg(((1, 3), (2,), (), ()))
    # 19 dispatches, of which 3 are not accounted for by these unflagged exits.
    assert exit_nop_evidence(host, graph, {1}, {0: 11, 1: 5}, 19) == {
        1: {"slot": 0, "jump_offset": 4, "minimum_executions": 11, "maximum_executions": 14}}
    assert exit_nop_evidence(host, cfg(((1, 2, 3), (2,), (), ())), {1}, {0: 11}, 19) == {}


def test_phase_transition_attribution():
    from site_census import whole_run_slot_counts
    phases = [dict(id=0, reached=True, first_dispatch_index=0), dict(id=1, reached=True, first_dispatch_index=5)]
    profile = NS(phases=phases, phase_id=lambda name: 1 if name == "steady" else 0)
    view = NS(profile=profile, edges=lambda phase: [
        {"status": "exact", "flags": [], "kind": "chain1", "src": "tb", "count": 5},
        {"status": "exact", "flags": ["irq"], "kind": "chain1", "src": "tb", "count": 2}])
    assert whole_run_slot_counts(view, "boot") == {}
    assert whole_run_slot_counts(view, "steady") == {}
    phases[1]["first_dispatch_index"] = 0
    assert whole_run_slot_counts(view, "steady") == {"tb": {1: 5}}


def test_calls_mandatory_for_some_observed_exits():
    from scopt.analysis.hcfg import HCfg
    from scopt.analysis.hostops import HostOp
    from site_census import mandatory_call_exits

    host = NS(status="ok", ops=[HostOp(i, 1, "", k, "insn", 0) for i, k in enumerate(
        (OpKind.JCC, OpKind.CALL, OpKind.JMP, OpKind.JMP))])
    succ = ((1, 3), (2,), (), ())
    cfg = HCfg("tb", True, (), 4, succ, succ, {}, {2: "goto_tb:0", 3: "goto_tb:1"})
    assert mandatory_call_exits(host, cfg, {0: 12, 1: 40}, {1}) == {
        1: {"guaranteed_by_slot": {"0": 12}, "minimum_executions": 12}}
    host.status = "pinned"
    assert mandatory_call_exits(host, cfg, {0: 12}, {1}) == {}


def test_slow_path_rejoins_earlier_offset_without_a_cycle():
    from scopt.analysis.hcfg import HCfg
    from scopt.analysis.hostops import HostOp

    ops = [HostOp(i, 1, "", kind, "insn", 0, target=target) for i, (kind, target) in enumerate(
        [(OpKind.JCC, 3), (OpKind.CALL, None), (OpKind.EXIT_JMP, None), (OpKind.JMP, 1)])]
    succ = ((1, 3), (2,), (), (1,))
    graph = HCfg("tb", True, (), 4, succ, succ, {}, {2: "exit_null"})
    host = NS(status="ok", ops=ops)
    store = NS(get=lambda name, scope: NS(value=host if name == "hostops" else graph))
    assert at_most_once(store, NS(uid="tb", native_fragments=[], stub_start=None)) == {0, 1, 2, 3}
