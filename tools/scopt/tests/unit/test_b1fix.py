"""B1FIX: regression tests for the B1 review follow-ups that B2 depends on.

One section per review item (a-p). The tests use synthetic builders (WP4/WP7
rich TBs imported by the WP7 importer, fake run directories, fake launchers);
the real-data checks are in ``tests/acceptance/test_b1fix_real.py``.
"""

from __future__ import annotations

import errno
import json
import os
from dataclasses import replace
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from scopt.formats import artifact as A
from scopt.formats import scm_v1 as S
from scopt.formats.sc_v5 import RECORD_SIZE, SC_CODE_OFFSET, ScHeader, read_sc, write_sc
from scopt.importer.import_cache import import_snapshot
from scopt.ledger import Ledger, LedgerError, LedgerRow
from scopt.manager import PassManager
from scopt.validate import Inspection, ValidateAndPackage, reimport, validate_artifact
from tests.synth import builders as B
from tests.synth import rich_tb as RT
from tests.unit.test_wp10_link import (_identity, _maps, codes, free_offset, make_snapshot,
                                       pipeline, registry, run)
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()


def _ledger_rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# ---------------------------------------------------------------------------
# (a) non-canonical input: a dropped record before other units
# ---------------------------------------------------------------------------

def _flip_record_checksum(sc_path: Path, slot: int) -> None:
    """Flip one guest_hash byte of record ``slot`` (covered by the record checksum):
    the replicated ``load_records`` skips it with ``record_checksum``."""
    h = read_sc(sc_path).header
    off = SC_CODE_OFFSET + h.code_size + slot * RECORD_SIZE + 64
    fd = os.open(sc_path, os.O_RDWR)
    try:
        b = os.pread(fd, 1, off)
        os.pwrite(fd, bytes([b[0] ^ 0x01]), off)
    finally:
        os.close(fd)


def _noncanonical_snapshot(tmp: Path, bad_slot: int, n: int = 3):
    units = RT.rich_units(n)
    prep = tmp / "prep"
    prep.mkdir(parents=True)
    h = B.sc_header_fields(units, {})
    h.pop("checksum")
    hdr = ScHeader(**h, checksum=0).with_checksum()
    ru = [B.resolve_unit(u) for u in units]
    write_sc(prep / "s.sc", hdr, [(r.record_bytes, r.arena_bytes) for r in ru])
    B.build_scm(units, sc_header={"code_size": hdr.code_size}, path=str(prep / "s.scm"),
                maps=_maps(hdr.code_size), helpers=RT.RICH_HELPERS)
    _flip_record_checksum(prep / "s.sc", bad_slot)
    sc = read_sc(prep / "s.sc")
    assert [(s.slot, s.reason) for s in sc.skipped] == [(bad_slot, "record_checksum")]
    res = import_snapshot(str(prep), store=str(tmp / "store"))
    b, ri = _identity(res.snapshot.header)
    return replace(res.snapshot, bundle=b, runtime=ri)


@pytest.mark.parametrize("bad_slot", [0, 2])
def test_identity_on_noncanonical_input_is_identity_canonicalized(tmp_path, bad_slot):
    snap = _noncanonical_snapshot(tmp_path, bad_slot)
    in_slots = [u.slot for u in snap.units()]
    assert in_slots == [s for s in range(3) if s != bad_slot]
    res = run(snap, tmp_path / "store", "identity", caps=())
    assert res.published, (res.reason, res.validation and res.validation.fails())
    assert codes(res) == {f"V{i}": "pass" for i in range(1, 10)}
    # UnitLink.slot is the output slot; input_slot keeps the input's
    assert [lu.slot for lu in res.link.units] == [0, 1]
    assert [lu.in_slot for lu in res.link.units] == in_slots
    # V9: identity_canonicalized with the dropped list (structured and in the text)
    v9 = [r for r in res.validation.rows if r["code"] == "V9"]
    assert len(v9) == 1 and v9[0]["result"] == "pass"
    assert v9[0]["status"] == "identity_canonicalized"
    assert v9[0]["dropped"] == [{"slot": bad_slot, "reason": "record_checksum"}]
    assert v9[0]["canonical_sha256"] == v9[0]["out_sha256"]
    assert f"dropped [({bad_slot}, 'record_checksum')]" in v9[0]["detail"]
    # the derived capture binds the output slots 0..1 and re-imports every unit
    art = Path(res.artifact_dir)
    scm = S.read_scm(art / "cache.scm")
    assert sorted(scm.tbs) == [0, 1]
    again = import_snapshot(sc=str(art / "cache.sc"), scms=[str(art / "cache.scm")])
    assert again.join.histogram() == {"bound": 2}
    assert [u.uid for u in again.snapshot.units()] == [u.uid for u in snap.units()]
    assert again.snapshot.snapshot_id == res.output_snapshot_id
    # stored validation.json carries the V9 row; re-validation agrees
    vj = json.loads((art / A.VALIDATION).read_text())
    assert [r["status"] for r in vj["rows"] if r["code"] == "V9"] == ["identity_canonicalized"]
    rv = validate_artifact(art, snap)
    assert rv["verdict"] == "accepted" and rv["stored_codes_agree"], rv["codes"]


def test_move_on_noncanonical_input_uses_output_slots(tmp_path):
    snap = _noncanonical_snapshot(tmp_path, 0)
    u = snap.units()[1]  # input slot 2, output slot 1
    assert u.slot == 2
    d = free_offset(snap) - u.tb_offset + 64
    res = run(snap, tmp_path / "store", ("move-units", {"deltas": {u.uid: d}}))
    assert res.published, (res.reason, res.validation and res.validation.fails())
    c = codes(res)
    assert all(c[f"V{i}"] == "pass" for i in range(1, 9)) and c["V9"] == "skip", c
    side = json.loads((Path(res.artifact_dir) / A.SIDECAR).read_text())
    assert side["move_map"][u.uid]["slot"] == 1
    scm = S.read_scm(Path(res.artifact_dir) / "cache.scm")
    assert scm.tbs[1].sc_record.tb_offset == u.tb_offset + d
    rv = validate_artifact(Path(res.artifact_dir), snap)
    assert rv["verdict"] == "accepted" and rv["stored_codes_agree"], rv["codes"]


# ---------------------------------------------------------------------------
# (b) re-import uses derive order with >= 2 derived files
# ---------------------------------------------------------------------------

def _two_capture_snapshot(tmp: Path):
    units = RT.rich_units(4)
    prep = tmp / "prep"
    prep.mkdir(parents=True)
    h = B.sc_header_fields(units, {})
    h.pop("checksum")
    hdr = ScHeader(**h, checksum=0).with_checksum()
    ru = [B.resolve_unit(u) for u in units]
    write_sc(prep / "s.sc", hdr, [(r.record_bytes, r.arena_bytes) for r in ru])
    kw = dict(sc_header={"code_size": hdr.code_size}, maps=_maps(hdr.code_size),
              helpers=RT.RICH_HELPERS, all_units=units)
    B.build_scm(units[:2], path=str(prep / "a.scm"), first_slot=0, **kw)
    B.build_scm(units[2:], path=str(prep / "b.scm"), first_slot=2, **kw)
    res = import_snapshot(str(prep), store=str(tmp / "store"))
    assert res.join.histogram() == {"bound": 4}
    b, ri = _identity(res.snapshot.header)
    return replace(res.snapshot, bundle=b, runtime=ri)


def test_derived_captures_order():
    names = ["cache.10.scm", "cache.scm", "cache.2.scm", "cache.1.scm", "other.scm"]
    key = sorted(names, key=lambda n: (A.capture_index(n) is None, A.capture_index(n) or 0, n))
    assert key == ["cache.scm", "cache.1.scm", "cache.2.scm", "cache.10.scm", "other.scm"]
    assert A.capture_index("cache.01.scm") is None and A.capture_index("cache.0.scm") is None


def test_reimport_uses_derive_order_with_two_derived_files(tmp_path, monkeypatch):
    snap = _two_capture_snapshot(tmp_path)
    import scopt.importer.import_cache as IC

    seen: list[list[str]] = []
    real = IC.ImportCache

    class Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, sc, scms, *a, **k):
            seen.append([Path(x).name for x in scms])
            super().__init__(sc, scms, *a, **k)
    monkeypatch.setattr(IC, "ImportCache", Spy)
    res = run(snap, tmp_path / "store", "identity", caps=())
    assert res.published, (res.reason, res.validation and res.validation.fails())
    art = Path(res.artifact_dir)
    assert sorted(p.name for p in art.glob("*.scm")) == ["cache.1.scm", "cache.scm"]
    assert [n for n, _ in res.link.derived_scms] == ["cache.scm", "cache.1.scm"]
    assert seen and seen[-1] == ["cache.scm", "cache.1.scm"]
    assert A.read_artifact(art).captures == ["cache.scm", "cache.1.scm"]
    seen.clear()
    r2 = reimport(art, snap)
    assert seen == [["cache.scm", "cache.1.scm"]]
    assert [Path(c.path).name for c in r2.join.captures] == ["cache.scm", "cache.1.scm"]
    assert r2.join.histogram() == {"bound": 4}


# ---------------------------------------------------------------------------
# (c) publish race
# ---------------------------------------------------------------------------

def _staging_with(store: Path, content: bytes = b"x") -> Path:
    st = A.staging_dir(store)
    (st / "a.txt").write_bytes(content)
    return st


def test_publish_race_enotempty_becomes_exists(tmp_path, monkeypatch):
    store = tmp_path / "store"
    real_rename = os.rename

    def racing_rename(src, dst):
        # another publisher of the same bytes wins between the check and the rename
        other = _staging_with(store)
        A._write_bytes(other / A.MANIFEST, A.manifest_bytes(other))
        real_rename(other, dst)
        raise OSError(errno.ENOTEMPTY, "Directory not empty", str(dst))
    monkeypatch.setattr(A.os, "rename", racing_rename)
    aid, final, status = A.publish(_staging_with(store), store)
    assert status == "exists" and final.is_dir() and final.name == A.id_hex(aid)
    assert not list((store / "artifacts").glob(".tmp-*"))


def test_publish_race_with_other_manifest_is_artifact_conflict(tmp_path, monkeypatch):
    store = tmp_path / "store"

    def racing_rename(src, dst):
        os.makedirs(dst)
        Path(dst, A.MANIFEST).write_bytes(b"0" * 64 + b"  other\n")
        raise OSError(errno.EEXIST, "File exists", str(dst))
    monkeypatch.setattr(A.os, "rename", racing_rename)
    with pytest.raises(A.ArtifactError) as e:
        A.publish(_staging_with(store), store)
    assert e.value.code == "artifact_conflict"
    assert not list((store / "artifacts").glob(".tmp-*"))


def test_publish_other_rename_error_is_artifact_error(tmp_path, monkeypatch):
    def bad_rename(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(A.os, "rename", bad_rename)
    with pytest.raises(A.ArtifactError) as e:
        A.publish(_staging_with(tmp_path / "store"), tmp_path / "store")
    assert e.value.code == "publish_rename"


# ---------------------------------------------------------------------------
# (d) ledger: publish failure after accepted:pass, rolled-back units
# ---------------------------------------------------------------------------

def test_publish_failure_supersedes_accepted_pass(tmp_path, monkeypatch):
    snap = make_snapshot(tmp_path, RT.rich_units(2))

    def failing_publish(stage, store, **kw):
        A.remove_tree(stage)
        raise A.ArtifactError("manifest_verify", "hash_mismatch: cache.sc")
    monkeypatch.setattr(A, "publish", failing_publish)
    res = run(snap, tmp_path / "store", "identity", caps=())
    assert not res.published and res.reason == "V8:manifest_verify"
    rej = json.loads(Path(res.rejection_path).read_text())
    acc = [r for r in rej["ledger"] if r["subject"] == "artifact" and r["stage"] == "accepted"]
    assert [r["verdict"] for r in acc] == ["pass", "fail"]
    assert acc[1]["reason"].startswith("V8:manifest_verify; supersedes accepted:pass@")
    assert acc[0]["ts"] in acc[1]["reason"] and acc[1]["ts"] >= acc[0]["ts"]
    # nothing published
    assert not list((tmp_path / "store" / "artifacts").glob("[0-9a-f]*"))


def test_rolled_back_unit_gets_compiled_fail_row(tmp_path):
    snap = make_snapshot(tmp_path, RT.rich_units(3), code_size=0x80000000)
    u = snap.units()[1]
    res = run(snap, tmp_path / "store", ("move-units", {"moves": {u.uid: 0x60000000}}))
    assert res.published and [f.uid for f in res.link.rollbacks] == [u.uid]
    rows = _ledger_rows(Path(res.artifact_dir) / A.LEDGER)
    comp = [(r["id"], r["verdict"], r["reason"], r["evidence_ref"]) for r in rows
            if r["subject"] == "unit" and r["stage"] == "compiled"]
    assert comp == [(u.uid, "fail", "rel32_range", "rolled_back")]
    art = [r for r in rows if r["subject"] == "artifact" and r["stage"] == "compiled"]
    assert [r["verdict"] for r in art] == ["pass"]


def test_failed_second_attempt_logs_rollbacks_and_failures(tmp_path):
    snap = make_snapshot(tmp_path, RT.rich_units(3), code_size=0x80000000)
    a, b = snap.units()[0], snap.units()[1]
    res = run(snap, tmp_path / "store",
              ("move-units", {"moves": {a.uid: 0x60000000, b.uid: a.tb_offset}}))
    assert not res.published and res.reason == "failed:rel32_range"
    rej = json.loads(Path(res.rejection_path).read_text())
    comp = {(r["id"], r["reason"]) for r in rej["ledger"]
            if r["subject"] == "unit" and r["stage"] == "compiled" and r["verdict"] == "fail"}
    assert (a.uid, "rel32_range") in comp


# ---------------------------------------------------------------------------
# (e) inspected rows: once per invocation, structured artifact counts
# ---------------------------------------------------------------------------

def test_inspected_unit_rows_once_across_candidates(tmp_path):
    snap = make_snapshot(tmp_path, RT.rich_units(3))
    store = tmp_path / "store"
    insp = Inspection(snap)
    c1 = PassManager(snap, pipeline("identity", caps=()), passes=registry()).execute()
    u = snap.units()[0]
    c2 = PassManager(snap, pipeline(("move-units", {"deltas": {
        u.uid: free_offset(snap) - u.tb_offset}})), passes=registry()).execute()
    r1 = ValidateAndPackage(c1, store, inspection=insp).run()
    r2 = ValidateAndPackage(c2, store, inspection=insp).run()
    assert r1.published and r2.published
    rows1 = _ledger_rows(Path(r1.artifact_dir) / A.LEDGER)
    rows2 = _ledger_rows(Path(r2.artifact_dir) / A.LEDGER)
    u1 = [r for r in rows1 if r["subject"] == "unit" and r["stage"] == "inspected"]
    u2 = [r for r in rows2 if r["subject"] == "unit" and r["stage"] == "inspected"]
    assert len(u1) == 3 and u2 == []
    for rows, cid in ((rows1, r1.candidate_id), (rows2, r2.candidate_id)):
        a = [r for r in rows if r["subject"] == "artifact" and r["stage"] == "inspected"]
        assert len(a) == 1 and a[0]["id"] == cid and a[0]["verdict"] == "pass"
        assert a[0]["counts"] == {"units": 3, "ok_relocatable": 3, "pinned": 0, "no_meta": 0}
        assert a[0]["evidence_ref"] == snap.snapshot_id
    # the report counts each unit once
    from scopt.report.collect import collect
    m = collect([store], bootstrap=50)
    assert m["ledger"]["units"]["inspected"] == {"ok_relocatable": 3}
    cells = [t["stages"]["inspected"] for t in m["ledger"]["table"]
             if t["subject"] == "artifact"]
    assert len(cells) == 2 and all(c["counts"]["units"] == 3 for c in cells)
    with pytest.raises(ValueError):
        other = make_snapshot(tmp_path, RT.rich_units(2), name="o")
        ValidateAndPackage(PassManager(other, pipeline("identity", caps=()),
                                       passes=registry()).execute(), store, inspection=insp)


def test_report_dedups_inspected_rows_across_invocations(tmp_path):
    snap = make_snapshot(tmp_path, RT.rich_units(2))
    store = tmp_path / "store"
    r1 = run(snap, store, "identity", caps=())
    u = snap.units()[0]
    r2 = run(snap, store, ("move-units", {"deltas": {u.uid: free_offset(snap) - u.tb_offset}}))
    assert r1.published and r2.published
    from scopt.report.collect import collect
    m = collect([store], bootstrap=50)
    assert m["ledger"]["units"]["inspected"] == {"ok_relocatable": 2}


def test_inspected_verdict_partial_with_pinned_units(tmp_path):
    units = RT.rich_units(2)
    snap = make_snapshot(tmp_path, units)
    scm = tmp_path / "prep-s" / "s.scm"
    scm.write_bytes(B.drop_site(scm.read_bytes(), 1, 3))
    res0 = import_snapshot(str(tmp_path / "prep-s"), store=str(tmp_path / "store2"))
    s2 = replace(res0.snapshot, bundle=snap.bundle, runtime=snap.runtime)
    res = run(s2, tmp_path / "store2")
    rows = _ledger_rows(Path(res.artifact_dir) / A.LEDGER)
    a = [r for r in rows if r["subject"] == "artifact" and r["stage"] == "inspected"]
    assert a[0]["verdict"] == "partial"
    assert a[0]["counts"] == {"units": 2, "ok_relocatable": 1, "pinned": 1, "no_meta": 0}


def test_ledger_counts_field_roundtrip_and_validation(tmp_path):
    led = Ledger()
    led.emit("artifact", "c", "inspected", "pass", "", None, counts={"units": 1})
    led.emit("unit", "u", "inspected", "ok_relocatable")
    led.write(tmp_path / "l.jsonl")
    back = Ledger.read(tmp_path / "l.jsonl")
    assert back.rows[0].counts == {"units": 1} and back.rows[1].counts is None
    assert "counts" not in back.rows[1].to_json()  # §5.9 rows stay byte-identical
    with pytest.raises(LedgerError):
        LedgerRow("unit", "u", "inspected", "x", "", None, "t", {"a": "1"})  # type: ignore


# ---------------------------------------------------------------------------
# (f) validate_artifact: the directory name is the ArtifactId
# ---------------------------------------------------------------------------

def test_validate_artifact_checks_directory_name(tmp_path):
    import shutil
    snap = make_snapshot(tmp_path, RT.rich_units(2))
    res = run(snap, tmp_path / "store", "identity", caps=())
    art = Path(res.artifact_dir)
    rv = validate_artifact(art, snap)
    row = [r for r in rv["rows"] if r["check"] == "artifact_dir_name"]
    assert row[0]["result"] == "pass" and rv["verdict"] == "accepted"
    other = tmp_path / "store" / "artifacts" / ("0" * 64)
    shutil.copytree(art, other)
    rv2 = validate_artifact(other, snap)
    row = [r for r in rv2["rows"] if r["check"] == "artifact_dir_name"]
    assert row[0]["result"] == "fail" and rv2["verdict"] == "rejected"
    assert rv2["codes"]["V8"] == "fail"


# ---------------------------------------------------------------------------
# (g)/(h) campaign preflight: input bank, seed QEMU, status never "running"
# ---------------------------------------------------------------------------

NO_BANK_TOML = """\
[Memory]
[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size = "64K"
backend = "file"
memory_file = "/nonexistent/ram0.ram"

[Machine]
platform = "generic-cortexm"
supercache = "/nonexistent/cache/x.sc"
supercache_flat = false
qmp_socket = "/nonexistent/qmp.sock"
"""


@pytest.fixture
def base_toml(tmp_path):
    from tests.unit.test_wp13_arms import BASE_TOML
    p = tmp_path / "base.toml"
    p.write_text(BASE_TOML)
    return p


def test_preflight_refuses_missing_input_bank(tmp_path):
    from scopt.run import arms as AR
    base = tmp_path / "nobank.toml"
    base.write_text(NO_BANK_TOML)
    inp = tmp_path / "in.bin"
    inp.write_bytes(b"\1\2")
    c = AR.campaign_from_args(base_config=str(base), arms=["empty"], out_name="t",
                              inputs=[str(inp)], reference="empty")
    with pytest.raises(AR.CampaignError) as e:
        AR.run_campaign(c, tmp_path / "o", log=None)
    assert e.value.code == "input_bank_missing"
    assert not (tmp_path / "o" / "campaign.json").exists()
    # seed input alone also needs the bank
    c = AR.campaign_from_args(base_config=str(base), arms=["empty"], out_name="t",
                              seed_input=str(inp), reference="empty")
    with pytest.raises(AR.CampaignError) as e:
        AR.preflight(c, tmp_path / "o2")
    assert e.value.code == "input_bank_missing"


def test_preflight_refuses_input_larger_than_bank(tmp_path, base_toml):
    from scopt.run import arms as AR
    big = tmp_path / "big.bin"
    big.write_bytes(b"\0" * 5000)  # bank is 4K
    c = AR.campaign_from_args(base_config=str(base_toml), arms=["empty"], out_name="t",
                              inputs=[str(big)], reference="empty")
    with pytest.raises(AR.CampaignError) as e:
        AR.preflight(c, tmp_path / "o")
    assert e.value.code == "input_too_large"


def test_cli_run_missing_bank_exit_2_no_traceback(tmp_path):
    from scopt.cli import cli, load_commands
    load_commands()
    base = tmp_path / "nobank.toml"
    base.write_text(NO_BANK_TOML)
    inp = tmp_path / "in.bin"
    inp.write_bytes(b"\1")
    r = CliRunner().invoke(cli, ["run", "-c", str(base), "--arms", "empty",
                                 "--reference", "empty", "--inputs", str(inp),
                                 "--reps", "1", "--out", str(tmp_path / "o")])
    assert r.exit_code == 2, r.output
    assert "refused: input_bank_missing" in r.output
    assert "Traceback" not in r.output and r.exception is None or \
        isinstance(r.exception, SystemExit)


def _fake_qemu(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    p.chmod(0o755)
    return p


def test_warm_seed_qemu_mismatch_refused_and_status_recorded(tmp_path, base_toml, monkeypatch):
    from scopt.run import arms as AR
    q = _fake_qemu(tmp_path, "qemu-campaign", b"campaign binary")

    def fake_launch(rd, **kw):
        doc = {"exit_code": 0, "qemu": "/other/qemu",
               "qemu_sha256": "sha256:" + "ab" * 32, "stray_capture_files": []}
        (rd.root / "run.json").write_text(json.dumps(doc))
        return doc
    monkeypatch.setattr(AR, "launch", fake_launch)
    c = AR.campaign_from_args(base_config=str(base_toml), arms=["warm"], out_name="t",
                              reps=1, qemu=str(q))
    with pytest.raises(AR.CampaignError) as e:
        AR.run_campaign(c, tmp_path / "o", log=None)
    assert e.value.code == "warm_seed_qemu_mismatch"
    doc = json.loads((tmp_path / "o" / "campaign.json").read_text())
    assert doc["status"] == "refused" and doc["error"]["code"] == "warm_seed_qemu_mismatch"
    assert not (tmp_path / "o" / "runs.jsonl").exists()


def test_campaign_status_error_on_crash(tmp_path, base_toml, monkeypatch):
    from scopt.run import arms as AR

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(AR, "_record_warm", boom)
    c = AR.campaign_from_args(base_config=str(base_toml), arms=["warm"], out_name="t",
                              reps=1)
    with pytest.raises(RuntimeError):
        AR.run_campaign(c, tmp_path / "o", log=None)
    doc = json.loads((tmp_path / "o" / "campaign.json").read_text())
    assert doc["status"] == "error" and "disk on fire" in doc["error"]["detail"]


def test_preflight_refuses_pinned_qemu_path_conflict(tmp_path, base_toml):
    from scopt.run import arms as AR
    q1 = _fake_qemu(tmp_path, "q1", b"one")
    q2 = _fake_qemu(tmp_path, "q2", b"two")
    t = tmp_path / "pinned.toml"
    t.write_text(base_toml.read_text().replace(
        "[Machine]\n", f'[Machine]\nqemu_path = "{q2}"\n'))
    c = AR.campaign_from_args(base_config=str(t), arms=["empty"], out_name="t",
                              reference="empty", qemu=str(q1))
    with pytest.raises(AR.CampaignError) as e:
        AR.preflight(c, tmp_path / "o")
    assert e.value.code == "qemu_path_conflict"
    c.qemu = str(q2)
    AR.preflight(c, tmp_path / "o")  # the pinned binary itself is fine


# ---------------------------------------------------------------------------
# (i) sc_metrics never reports 0 for an unreadable cache; empty validation.json
# ---------------------------------------------------------------------------

def test_sc_metrics_unknown_for_unloadable_and_truncated(tmp_path):
    from scopt.report.collect import sc_metrics
    empty = tmp_path / "empty.sc"
    empty.write_bytes(b"")
    m = sc_metrics(empty)
    assert m["status"] == "unknown" and m["populated_native_bytes"] is None
    assert m["valid_records"] is None and m["records"] is None
    assert m["logical_file_size"] == 0
    missing = sc_metrics(tmp_path / "nope.sc")
    assert missing["status"] == "unknown" and missing["reason"].startswith("unreadable")
    # truncated record array
    good = tmp_path / "g.sc"
    B.build_sc(B.simple_units(3), path=str(good))
    data = good.read_bytes()
    trunc = tmp_path / "t.sc"
    trunc.write_bytes(data[:-RECORD_SIZE // 2])
    t = sc_metrics(trunc)
    assert t["status"] == "unknown" and "truncated" in t["reason"]
    assert t["populated_native_bytes"] is None
    ok = sc_metrics(good)
    assert ok["status"] == "ok" and ok["valid_records"] == 3 and ok["populated_native_bytes"] > 0


def test_report_renders_unknown_sc_and_empty_validation(tmp_path):
    from scopt.report.collect import collect
    from scopt.report.render import render_markdown
    art = tmp_path / "store" / "artifacts" / "x"
    art.mkdir(parents=True)
    (art / "cache.sc").write_bytes(b"")
    (art / "validation.json").write_text("{}")
    m = collect([tmp_path], bootstrap=50)
    a = m["artifacts"][0]
    assert a["validation"] is None and a["sc"]["status"] == "unknown"
    md = render_markdown(m)
    assert "validation: not_run (validation.json has no check rows)." in md
    # explicit .sc source with no records known: 'unknown', not 0
    m2 = collect([tmp_path / "none"], scs={"warm": str(art / "cache.sc")}, bootstrap=50)
    md2 = render_markdown(m2)
    row = next(x for x in md2.splitlines() if x.startswith("| warm | unknown ("))
    assert "| 0/0 |" not in row and "None" not in row


def test_validation_rows_use_result_field(tmp_path):
    snap = make_snapshot(tmp_path, RT.rich_units(1))
    res = run(snap, tmp_path / "store", "identity", caps=())
    from scopt.report.collect import collect
    m = collect([tmp_path / "store"], bootstrap=50)
    a = [x for x in m["artifacts"] if x.get("artifact_id") == res.artifact_id][0]
    assert {v["verdict"] for v in a["validation"]} <= {"pass", "skip"}


# ---------------------------------------------------------------------------
# (j) paired statistics with zero / negative values
# ---------------------------------------------------------------------------

def test_paired_speedup_marks_nonpositive_samples_invalid():
    from scopt.report.stats import paired_speedup, unpaired_speedup
    ref = [100.0, 0.0, 102.0, 101.0, -5.0]
    arm = [50.0, 51.0, 0.0, 50.0, 49.0]
    s = paired_speedup(ref, arm, reps=200)
    assert s["n_pairs"] == 2 and s["n_invalid"] == 3
    assert [x["index"] for x in s["invalid"]] == [1, 2, 4]
    assert [x["reason"] for x in s["invalid"]] == ["ref_zero", "arm_zero", "ref_negative"]
    assert s["median"] == pytest.approx((2.0 + 2.02) / 2)
    u = unpaired_speedup([100.0, 0.0, 99.0], [50.0, -1.0, 51.0], reps=200)
    assert u["n_ref"] == 2 and u["n_arm"] == 2 and u["n_invalid"] == 2
    allbad = paired_speedup([0.0], [1.0], reps=10)
    assert allbad["verdict"] == "no_data" and allbad["n_invalid"] == 1


def test_report_group_survives_zero_bench(tmp_path):
    from scopt.report.collect import collect
    from scopt.report.render import render_markdown
    rows = []
    for rep in range(4):
        for arm, b in (("warm", 200 + rep), ("identity", 0 if rep == 1 else 198 + rep)):
            rows.append({"schema": "scopt.runrow/1", "campaign": "z", "arm": arm,
                         "kind": "deploy", "rep": rep, "input": None, "valid": True,
                         "reason": "ok", "verdict": "PASS", "bench_ns": b, "wall_s": 1.0,
                         "cpu_s": 1.0, "run_dir": f"/r/{arm}/{rep}", "pos": rep})
    out = tmp_path / "camp"
    out.mkdir()
    (out / "campaign.json").write_text(json.dumps({
        "schema": "scopt.campaign/1", "status": "done",
        "campaign": {"name": "z", "reference": "warm", "arms": [
            {"name": "warm", "kind": "deploy"}, {"name": "identity", "kind": "deploy"}]}}))
    (out / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    m = collect([out], bootstrap=100)
    assert not [p for p in m["problems"] if "timing" in p["where"]], m["problems"]
    g = m["groups"][0]
    c = [x for x in g["comparisons"] if x["metric"] == "bench"][0]
    assert c["n_pairs"] == 3 and c["n_invalid"] == 1
    assert "(1 invalid: arm_zero)" in render_markdown(m)


# ---------------------------------------------------------------------------
# (k) arms not requested by a campaign
# ---------------------------------------------------------------------------

def test_arms_not_requested_are_distinct_from_not_run():
    from scopt.report.collect import _arm_status
    groups = [{"name": "campaign:c", "arms": [{"name": "warm", "kind": "deploy"},
                                               {"name": "identity", "kind": "deploy"}],
               "per_arm": {"warm": {"runs": 2, "valid": 2},
                           "identity": {"runs": 2, "valid": 2}}}]
    camps = [{"name": "c", "arm_names": ["warm", "identity"],
              "not_run": {"layout": "no_artifact: no --artifact given for this arm"},
              "unsupported": {}}]
    st = {a["arm"]: a for a in _arm_status(groups, camps)}
    assert st["nocache"]["status"] == "not_requested"
    assert st["empty"]["status"] == "not_requested"
    assert st["layout"]["status"] == "not_run" and st["layout"]["reason"].startswith(
        "no_artifact")
    assert st["semantic"]["status"] == "unsupported"
    assert st["warm"]["status"] == "measured"
    # no campaign at all: nothing could have been requested -> not_run, no runs found
    st2 = {a["arm"]: a for a in _arm_status([], [])}
    assert st2["nocache"] == {"arm": "nocache", "status": "not_run", "reason": "no runs found"}


# ---------------------------------------------------------------------------
# (l) two artifacts with one candidate id
# ---------------------------------------------------------------------------

def test_report_keeps_both_artifacts_of_one_candidate(tmp_path):
    snap = make_snapshot(tmp_path, RT.rich_units(2))
    store = tmp_path / "store"
    clock = iter(f"2026-11-16T00:00:{i:02d}.000Z" for i in range(60))
    c1 = PassManager(snap, pipeline("identity", caps=()), passes=registry()).execute()
    r1 = ValidateAndPackage(c1, store, clock=lambda: next(clock)).run()
    c2 = PassManager(snap, pipeline("identity", caps=()), passes=registry()).execute()
    r2 = ValidateAndPackage(c2, store, clock=lambda: next(clock)).run()
    assert r1.candidate_id == r2.candidate_id and r1.artifact_id != r2.artifact_id
    from scopt.report.collect import collect
    m = collect([store], bootstrap=50)
    rows = {t["id"]: t for t in m["ledger"]["table"] if t["subject"] == "artifact"}
    assert set(rows) == {r1.artifact_id, r2.artifact_id}
    for aid in (r1.artifact_id, r2.artifact_id):
        st = rows[aid]["stages"]
        assert rows[aid]["aliases"] == [r1.candidate_id]
        # each artifact gets the rows of its own ledger, once
        assert st["accepted"]["verdict"] == "pass" and st["accepted"]["rows"] == 1
        assert st["compiled"]["rows"] == 1 and st["inspected"]["rows"] == 1


# ---------------------------------------------------------------------------
# (m) diag exec refuses an input larger than its bank
# ---------------------------------------------------------------------------

def test_diag_exec_refuses_input_larger_than_bank(tmp_path, base_toml):
    from scopt.run.diag import DiagError, diag_exec
    from scopt.run.launcher import FASTDYN_DEFAULT_QEMU, create_run_dir
    rd = create_run_dir(base_toml, tmp_path / "rd", qemu=FASTDYN_DEFAULT_QEMU)
    (rd.root / "oracle.json").write_text(json.dumps({"argv": ["/q/qemu"]}))
    rd.work.mkdir()
    big = tmp_path / "big.bin"
    big.write_bytes(b"\1" * 4097)  # bank is 4K
    with pytest.raises(DiagError) as e:
        diag_exec(rd.root, tmp_path / "diag", inputs={"input": big})
    assert e.value.code == "input_too_large"
    assert not (tmp_path / "diag").exists()  # refused before anything was written
    with pytest.raises(DiagError) as e:
        diag_exec(rd.root, tmp_path / "diag2", inputs={"nope": big})
    assert e.value.code == "input_bank_missing"
    # the recorded inputs of the run are checked the same way
    (rd.root / "run.json").write_text(json.dumps({"inputs": {"input": {"path": str(big)}}}))
    with pytest.raises(DiagError) as e:
        diag_exec(rd.root, tmp_path / "diag3")
    assert e.value.code == "input_too_large"


# ---------------------------------------------------------------------------
# (n) the launcher defaults to the integrated runtime
# ---------------------------------------------------------------------------

def test_create_run_dir_defaults_to_runtime_qemu(tmp_path, base_toml):
    from scopt.run import runtime
    from scopt.run.launcher import FASTDYN_DEFAULT_QEMU, B0_QEMU, create_run_dir
    if not runtime._DEFAULT.is_file():
        pytest.skip(f"{runtime._DEFAULT} not present (the maintainer's WP-QINT build)")
    rd = create_run_dir(base_toml, tmp_path / "a")
    m = rd.meta
    assert m["qemu"] == str(runtime._DEFAULT.resolve()) and rd.qemu == m["qemu"]
    assert m["qemu_selection"] == "runtime_default"
    assert m["qemu_sha256"] == runtime.RUNTIME_QEMU_SHA256
    # explicit opt-out: FastDyn's own selection (B0 comparisons)
    rd = create_run_dir(base_toml, tmp_path / "b", qemu=FASTDYN_DEFAULT_QEMU)
    assert rd.meta["qemu"] is None and rd.meta["qemu_selection"] == "fastdyn"
    if B0_QEMU.is_file():
        rd = create_run_dir(base_toml, tmp_path / "c", qemu=B0_QEMU)
        assert rd.meta["qemu_selection"] == "explicit"
        assert rd.meta["qemu_sha256"].startswith("sha256:")


def test_runtime_override_is_still_checked(tmp_path, base_toml, monkeypatch):
    import hashlib

    from scopt.run import runtime
    from scopt.run.launcher import RunDirError, create_run_dir
    fake = _fake_qemu(tmp_path, "qemu-other", b"another build")
    monkeypatch.setenv("SCOPT_RUNTIME_QEMU", str(fake))
    monkeypatch.delenv("SCOPT_RUNTIME_QEMU_SHA256", raising=False)
    with pytest.raises(runtime.RuntimeMismatch, match="SCOPT_RUNTIME_QEMU_SHA256"):
        runtime.runtime_qemu()
    with pytest.raises(RunDirError) as e:
        create_run_dir(base_toml, tmp_path / "x")
    assert e.value.code == "runtime_qemu_unavailable"
    h = hashlib.sha256(b"another build").hexdigest()
    monkeypatch.setenv("SCOPT_RUNTIME_QEMU_SHA256", h)
    info = runtime.runtime_qemu_info()
    assert info["override"] and info["sha256"] == "sha256:" + h
    rd = create_run_dir(base_toml, tmp_path / "y")
    assert rd.meta["qemu"] == str(fake.resolve()) and rd.meta["qemu_runtime_override"] is True
    assert rd.meta["qemu_sha256"] == "sha256:" + h
    monkeypatch.setenv("SCOPT_RUNTIME_QEMU_SHA256", "0" * 64)
    with pytest.raises(runtime.RuntimeMismatch):
        runtime.runtime_qemu()


# ---------------------------------------------------------------------------
# (o) T-B1-0 flag rule and Part A tc sizes
# ---------------------------------------------------------------------------

def _t_b1_0():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "t_b1_0", Path(__file__).resolve().parents[1] / "qemu" / "t_b1_0.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("med,lo,hi,want", [
    (0.050, 0.040, 0.060, "flagged"),          # whole CI above 1%
    (0.0150, 0.0038, 0.0293, "unresolved"),    # B1 blinky: straddles, median > 1%
    (-0.0085, -0.1406, 0.1079, "unresolved"),  # B1 minimal: straddles, CI 25 points wide
    (0.0084, -0.0003, 0.0130, "not_flagged"),  # B1 compute: straddles narrowly, median < 1%
    (0.002, -0.004, 0.008, "not_flagged"),     # CI below 1%
    (0.010, 0.0101, 0.02, "flagged"),
])
def test_t_b1_0_flag_status(med, lo, hi, want):
    T = _t_b1_0()
    assert T.flag_status(med, lo, hi) == want


def test_t_b1_0_overhead_stats_flag_fields():
    T = _t_b1_0()
    old = [100.0 + (i % 7) for i in range(40)]
    st = T.overhead_stats(old, [x * 1.05 for x in old], reps=500)
    assert st["flag"] == "flagged" and st["flagged"] is True
    st = T.overhead_stats(old, old, reps=500)
    assert st["flag"] == "not_flagged" and st["flagged"] is False
    assert T.overhead_stats([], [1.0])["flag"] == "no_data"


def test_t_b1_0_part_a_fails_on_tc_size_difference(tmp_path):
    T = _t_b1_0()
    units = B.simple_units(3)
    B.build_sc(units, path=str(tmp_path / "gold.sc"))
    gold = read_sc(tmp_path / "gold.sc")
    extra, fails = T.golden_checks(gold, gold)
    assert fails == [] and extra["tc_sizes_equal_b0_golden"] is True

    class _Rec:
        def __init__(self, r, tc):
            self._r, self.tc_size = r, tc

    class _U:
        def __init__(self, u, bump):
            self.tb_uid = u.tb_uid
            self.record = _Rec(u.record, u.record.tc_size + bump)

    class _Sc:
        def __init__(self, sc):
            self._sc = sc

        def units(self):
            return [_U(u, 8 if i == 1 else 0) for i, u in enumerate(self._sc.units())]
    extra, fails = T.golden_checks(gold, _Sc(gold))
    assert extra["tb_uids_equal_b0_golden"] is True
    assert extra["tc_sizes_equal_b0_golden"] is False
    assert any("host code sizes" in f for f in fails)


# ---------------------------------------------------------------------------
# (p) `scopt profile run` registration is order independent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", [("profile", "profile_run"), ("profile_run", "profile"),
                                   ("profile_run",)])
def test_profile_run_registration_any_order(order):
    import importlib
    grp = click.Group("scopt-test")
    for name in order:
        importlib.import_module(f"scopt.commands.{name}").register(grp)
    prof = grp.commands["profile"]
    assert isinstance(prof, click.Group) and "run" in prof.commands
    assert {"ingest", "check", "merge", "show"} <= set(prof.commands)
    r = CliRunner().invoke(grp, ["profile", "run", "--help"])
    assert r.exit_code == 0 and "--snapshot" in r.output
