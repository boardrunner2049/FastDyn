"""B2 fixtures acceptance (design §7; WP15A): F-B2-1, F-B2-2, F-B2-4, F-B2-5, F-B2-6.

Two kinds of tests, as in ``test_b1_gate.py``:

* ``test_*_files``: read the evidence a driver run wrote
  (``tests/qemu/b2_gate.py --workload W --out $B2_GATE_ROOT/W``; default root
  ``runs/wp15a``) and re-check the key facts from the artifact, validation,
  V10, profile, diag and rejection files themselves rather than only trusting
  ``b2-gate.json``. Skipped when a workload's directory is absent.
* ``test_b2_gate_end_to_end``: runs the whole driver on minimal and on the fault
  fixture into a temporary directory (real QEMU runs; a few minutes each).
  Opt-in with ``SCOPT_B2_GATE_E2E=1`` so the default suite stays fast.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scopt.formats.sc_v5 import read_sc, sha256_sparse

ROOT = Path(os.environ.get("FASTDYN_ROOT", "/data/extra_copy/fastdyn"))
GATE_ROOT = Path(os.environ.get("B2_GATE_ROOT", ROOT / "runs/wp15a"))
WT = Path(__file__).resolve().parents[2]
INT_QEMU_SHA = "sha256:8864c72671971cf4488b5afa76eeee02a3e2a472c45af81ec1314464e6035f41"
FIX = ["F-B2-1", "F-B2-2", "F-B2-4", "F-B2-5", "F-B2-6"]
WANT = {"minimal": FIX, "fault": FIX, "compute": ["F-B2-6"]}

pytestmark = [pytest.mark.real]


def _gate(wl: str) -> tuple[dict, Path]:
    out = GATE_ROOT / wl
    p = out / "b2-gate.json"
    if not p.is_file():
        pytest.skip(f"{p} not present (run tests/qemu/b2_gate.py --workload {wl})")
    return json.loads(p.read_text()), out


def _art(dyn: Path) -> Path:
    return Path(json.loads(dyn.read_text())["artifact_dir"])


def _side(art: Path) -> dict:
    return json.loads((art / "cache.sc.scopt.json").read_text())


def _v10_ok(out: Path, label: str, appended: int | None = 0) -> dict:
    d = json.loads((out / "v10" / label / "dynamic.json").read_text())
    assert d["verdict"] == "pass", (label, d["failures"])
    for r in d["runs"]:
        for arm in ("artifact", "baseline"):
            assert r[arm]["verdict"] == "PASS", (label, arm)
        assert r["artifact"]["output_norm_sha256"] == r["baseline"]["output_norm_sha256"]
        assert r["artifact"]["guest_norm"] == r["baseline"]["guest_norm"]
    if appended is not None:
        assert set(d["appended"]["artifact"] + d["appended"]["baseline"]) == {appended}, label
    return d


@pytest.mark.parametrize("wl", sorted(WANT))
def test_b2_gate_verdicts(wl):
    g, _ = _gate(wl)
    assert g["qemu_sha256"] == INT_QEMU_SHA
    assert g["fixtures_requested"] == WANT[wl]
    for f in WANT[wl]:
        r = g["fixtures"][f]
        assert r["verdict"] == "pass", (f, r["failed"], r["not_run_reason"])
        assert r["evidence"], f
        for e in r["evidence"]:
            assert Path(e).exists(), (f, e)
    assert g["verdict"] == "PASS"
    assert g.get("gate_source_unchanged", True)


@pytest.mark.parametrize("wl", ["minimal", "fault"])
def test_f_b2_1_files(wl):
    from scopt.formats.artifact import verify_manifest
    from scopt.formats.qprof_v1 import read_qprof

    g, out = _gate(wl)
    f1 = g["f1"]
    uid, slot = f1["unit"], f1["slot"]
    for label in ("max_end", "plus4MiB"):
        art = Path(f1[label]["artifact"])
        new_tb = int(f1[label]["new_tb_offset"], 16)
        side = _side(art)
        assert list(side["move_map"]) == [uid]
        assert int(side["move_map"][uid]["new_tb_offset"], 16) == new_tb
        val = json.loads((art / "validation.json").read_text())
        assert val["verdict"] == "accepted"
        assert {k: v for k, v in val["codes"].items()} == {
            **{f"V{i}": "pass" for i in range(1, 9)}, "V9": "skip"}
        assert verify_manifest(art) == []
        sc = read_sc(art / "cache.sc")
        assert sc.records[slot].tb_offset == new_tb and sc.unit_ids()[slot] == uid
        _v10_ok(out, f"f1-{label}")
        pr = json.loads((out / "prof-art" / label / "profile_run.json").read_text())
        q = read_qprof(pr["runs"][0]["qprof"])
        rows = [t for t in q.tbs if t.tb_offset == new_tb]
        assert len(rows) == 1 and sum(p.entries for p in rows[0].phases) > 0
        assert not [t for t in q.tbs if t.tb_offset == int(f1["old_tb_offset"], 16)]
        d = json.loads((out / "diag" / f"f1-{label}" / "diag.json").read_text())
        assert d["exit_code"] == 0 and d["all_traces_in_snapshot"]
        assert d["per_slot_entries"].get(str(slot), 0) > 0
        cs = json.loads((out / f"f1-{label}-capstone.json").read_text())
        assert cs["ok"] and not cs["changed_bytes_outside_fixups"]
        assert {c.split(".")[0] for c in cs["changed_insns_by_fixup_class"]} <= {"prologue", "ext"}


@pytest.mark.parametrize("wl", ["minimal", "fault"])
def test_f_b2_2_files(wl):
    g, out = _gate(wl)
    fx = json.loads((out / "f2-reverse-fixups.json").read_text())
    assert fx["applied_by_class"] == fx["snapshot_fixup_classes"]
    assert {"prologue.tb_ret", "prologue.epilogue", "ext.plugin", "ext.ptr_riprel"} <= \
        set(fx["applied_by_class"])
    assert not fx["bad_fixups"] and not fx["capstone_bad"]
    if wl == "minimal":
        assert fx["applied_by_class"] == {"prologue.tb_ret": 316, "prologue.epilogue": 3,
                                          "ext.plugin": 3, "ext.ptr_riprel": 3}
    art = _art(out / "v10/f2-reverse/dynamic.json")
    snapdir = out / "store/snapshots" / g["snapshot_id"].split(":", 1)[1]
    snapj = json.loads((snapdir / "snapshot.json").read_text())
    side = _side(art)
    assert len(side["move_map"]) == snapj["status"]["ok_relocatable"] == fx["moved"]
    assert read_sc(art / "cache.sc").unit_ids() == read_sc(snapdir / "cache.sc").unit_ids()
    _v10_ok(out, "f2-reverse")
    # pinned unit untouched (F-B2-4 tampered snapshot)
    art4 = _art(out / "v10/f4-reverse/dynamic.json")
    uid = g["f4"]["unit"]
    assert uid in {p["unit"] for p in _side(art4)["pinned"]}
    assert uid not in _side(art4)["move_map"]
    _v10_ok(out, "f4-reverse")
    # duplicate key on a real file: the later slot is executed, before and after the layout
    for arm in ("baseline", "artifact"):
        d = json.loads((out / "diag" / f"f2-lifo-{arm}" / "diag.json").read_text())
        n = len(read_sc(_art(out / "v10/f2-lifo/dynamic.json") / "cache.sc").unit_ids())
        assert d["per_slot_entries"].get(str(n - 1), 0) > 0
        assert d["per_slot_entries"].get(str(g["f1"]["slot"]), 0) == 0


@pytest.mark.parametrize("wl", ["minimal", "fault"])
def test_f_b2_4_files(wl):
    g, out = _gate(wl)
    uid = g["f4"]["unit"]
    snapj = json.loads((Path(g["f4"]["snapshot_dir"]) / "snapshot.json").read_text())
    assert snapj["status"]["pinned"] == 1
    assert any(k.startswith("unexplained_reference") for k in snapj["pin_reasons"])
    token = f"skipped_unsupported:unexplained_reference:{uid}"
    for label, passes in (("f4-b2-layout", ("cluster-hot", "order-cold")),
                          ("f4-no-cluster-hot", ("order-cold",))):
        art = _art(out / "v10" / label / "dynamic.json")
        side = _side(art)
        pp = {p["pass"]: p for p in side["pipeline"]}
        assert any(token in (pp[p].get("affected") or []) for p in passes), label
        assert uid not in side["move_map"] and side["move_map"]
        val = json.loads((art / "validation.json").read_text())
        assert val["verdict"] == "accepted"
        _v10_ok(out, label)


@pytest.mark.parametrize("wl", ["minimal", "fault"])
def test_f_b2_5_files(wl):
    g, out = _gate(wl)
    art = _art(out / "v10/f5-rollback/dynamic.json")
    side = _side(art)
    assert side["move_map"] == {}
    assert any(p["reasons"] == ["failed:rel32_range"] for p in side["pinned"])
    assert [r["reason"] for r in side["x_link"]["rollbacks"]] == ["rel32_range"]
    snapdir = out / "store/snapshots" / g["snapshot_id"].split(":", 1)[1]
    assert sha256_sparse(art / "cache.sc") == sha256_sparse(snapdir / "cache.sc")
    _v10_ok(out, "f5-rollback")
    rej = [json.loads(p.read_text()) for p in (out / "store/rejections").glob("*.json")]
    led = [(r["stage"], r["verdict"], r["reason"]) for d in rej for r in d.get("ledger", [])
           if r["subject"] == "artifact"]
    assert ("compiled", "fail", "rel32_range") in led
    assert ("accepted", "fail", "failed:rel32_range") in led


@pytest.mark.parametrize("wl", ["minimal", "fault", "compute"])
def test_f_b2_6_files(wl):
    g, out = _gate(wl)
    f6 = g["f6"]
    nm = f6["no_meta"]
    snapj = json.loads((Path(f6["snapshot_dir"]) / "snapshot.json").read_text())
    assert snapj["status"]["no_meta"] == len(nm) > 0
    for label in ("f6-reverse", "f6-b2-layout"):
        art = _art(out / "v10" / label / "dynamic.json")
        side = _side(art)
        assert side["move_map"] and not set(nm) & set(side["move_map"]), label
        assert set(nm) <= {p["unit"] for p in side["pinned"]}
        _v10_ok(out, label)
    pp = {p["pass"]: p for p in _side(_art(out / "v10/f6-b2-layout/dynamic.json"))["pipeline"]}
    aff = [x for p in ("cluster-hot", "order-cold") for x in pp[p].get("affected") or []]
    assert all(f"skipped_unsupported:no_meta:{u}" in aff for u in nm)


@pytest.mark.qemu
@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("SCOPT_B2_GATE_E2E") != "1",
                    reason="opt-in: SCOPT_B2_GATE_E2E=1 (real QEMU runs, a few minutes each)")
@pytest.mark.parametrize("wl", ["fault", "minimal"])
def test_b2_gate_end_to_end(tmp_path, wl):
    out = tmp_path / wl
    p = subprocess.run([sys.executable, str(WT / "tests/qemu/b2_gate.py"), "--workload", wl,
                        "--out", str(out), "--reps", "1"], cwd=str(WT), capture_output=True,
                       text=True, timeout=5400)
    g = json.loads((out / "b2-gate.json").read_text())
    for f in FIX:
        r = g["fixtures"][f]
        assert r["verdict"] == "pass", (f, r["failed"], p.stdout[-2000:])
    assert g["verdict"] == "PASS"
