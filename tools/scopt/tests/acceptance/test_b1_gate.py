"""B1 gate acceptance (design §6, WP14).

Two kinds of tests:

* ``test_gate_*``: read the evidence a gate run wrote
  (``tests/qemu/b1_gate.py --workload W --out $B1_GATE_ROOT/W``; default root
  ``runs/wp14/gate``) and re-check the key facts from the files themselves
  rather than only trusting ``gate.json``: per-test verdicts, the V9 sha256
  equality, the derived capture, the ledger stages of the identity artifact
  (read from the ledger files, not the report), the diag tc.ptr check, the
  profile flow conservation and the B0 golden. Skipped when a workload's gate
  directory is absent.
* ``test_b1_gate_end_to_end_minimal``: runs the whole driver on minimal into a
  temporary directory (QEMU; about three minutes). Opt-in with
  ``SCOPT_B1_GATE_E2E=1`` so the default suite stays fast.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scopt.formats.sc_records import sha256_sparse
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
GATE_ROOT = Path(os.environ.get("B1_GATE_ROOT", ROOT / "runs/wp14/gate"))
WT = Path(__file__).resolve().parents[2]
INT_QEMU_SHA = "sha256:8864c72671971cf4488b5afa76eeee02a3e2a472c45af81ec1314464e6035f41"
GATED = {"minimal": [f"T-B1-{i}" for i in range(10)],
         "freertos": ["T-B1-0", "T-B1-1", "T-B1-3", "T-B1-5"],
         "compute": ["T-B1-0", "T-B1-1", "T-B1-3", "T-B1-5"]}

pytestmark = [pytest.mark.real]


def _gate(wl: str) -> dict:
    p = GATE_ROOT / wl / "gate.json"
    if not p.is_file():
        pytest.skip(f"{p} not present (run tests/qemu/b1_gate.py --workload {wl})")
    return json.loads(p.read_text())


@pytest.mark.parametrize("wl", sorted(GATED))
def test_gate_verdicts(wl):
    g = _gate(wl)
    assert g["qemu_sha256"] == INT_QEMU_SHA
    assert g["gated_tests"] == GATED[wl]
    for t in GATED[wl]:
        r = g["tests"][t]
        assert r["verdict"] == "pass", (t, r["failed"], r["not_run_reason"])
        assert r["evidence"], t
        for e in r["evidence"]:
            assert Path(e).exists(), (t, e)
    assert g["b0_golden_unchanged"]
    assert g["verdict"] == "PASS"


@pytest.mark.parametrize("wl", sorted(GATED))
def test_gate_identity_artifact_files(wl):
    """T-B1-5 static facts, recomputed from the store."""
    g = _gate(wl)
    art = Path(g["artifact_dir"])
    snap = art.parent.parent / "snapshots" / g["snapshot_id"].split(":", 1)[1]
    assert sha256_sparse(art / "cache.sc") == sha256_sparse(snap / "cache.sc")  # V9
    side = json.loads((art / "cache.sc.scopt.json").read_text())
    assert side["input_snapshot_id"] == side["output_snapshot_id"] == g["snapshot_id"]
    assert side["move_map"] == {}
    val = json.loads((art / "validation.json").read_text())
    assert val["verdict"] == "accepted"
    from scopt.formats.artifact import verify_manifest

    assert verify_manifest(art) == []


@pytest.mark.parametrize("wl", sorted(GATED))
def test_gate_ledger_stages_from_ledger_files(wl):
    """inspected/compiled/accepted (artifact ledger, candidate id), validated_dynamic
    (V10 ledger, artifact id) and measured (campaign ledger) for the identity artifact."""
    g = _gate(wl)
    art = Path(g["artifact_dir"])
    side = json.loads((art / "cache.sc.scopt.json").read_text())
    cid, aid = side["x_candidate_id"], g["artifact_id"]
    out = GATE_ROOT / wl
    rows = []
    for p in (art / "ledger.jsonl", out / "v10/ledger.jsonl", out / "timing/ledger.jsonl"):
        rows += [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    mine = {(r["stage"], r["verdict"]) for r in rows if r["subject"] == "artifact"
            and r["id"] in (cid, aid)}
    for st in ("inspected", "compiled", "accepted", "validated_dynamic"):
        assert (st, "pass") in mine, (st, sorted(mine))
    assert ("measured", "measured") in mine
    units = [r for r in rows if r["subject"] == "unit" and r["stage"] == "inspected"]
    snapj = json.loads((art.parent.parent / "snapshots" / g["snapshot_id"].split(":", 1)[1]
                        / "snapshot.json").read_text())
    assert len(units) == snapj["records"]["valid"]  # one inspected row per unit
    hist: dict[str, int] = {}
    for r in units:
        hist[r["verdict"]] = hist.get(r["verdict"], 0) + 1
    assert hist == {k: v for k, v in snapj["status"].items() if v}
    # the report joins the candidate id to the artifact id: one row, every stage
    rep = json.loads((out / "report/report.json").read_text())
    row = [t for t in rep["ledger"]["table"] if t["subject"] == "artifact" and t["id"] == aid]
    assert len(row) == 1 and cid in row[0].get("aliases", [])
    st = {k: v["verdict"] for k, v in row[0]["stages"].items()}
    assert {k: st[k] for k in ("inspected", "compiled", "accepted", "validated_dynamic",
                               "measured")} == {"inspected": "pass", "compiled": "pass",
                                                "accepted": "pass", "validated_dynamic": "pass",
                                                "measured": "measured"}


@pytest.mark.parametrize("wl", sorted(GATED))
def test_gate_runs_and_diag(wl):
    """T-B1-5 dynamic: warm vs identity rows and the diag exec tc.ptr check."""
    out = GATE_ROOT / wl
    _gate(wl)
    rows = [json.loads(x) for x in (out / "timing/runs.jsonl").read_text().splitlines()]
    assert {r["arm"] for r in rows} == {"warm", "identity"}
    assert all(r["valid"] and r["verdict"] == "PASS" and r["appended"] == 0 for r in rows)
    assert len({r["output_norm_sha256"] for r in rows}) == 1
    d = json.loads((out / "diag-identity/diag.json").read_text())
    assert d["exit_code"] == 0 and d["all_traces_in_snapshot"] and d["tc_not_in_snapshot"] == []


def test_gate_minimal_profile_and_graph():
    """T-B1-4/6/7 facts on minimal, recomputed from the files."""
    from scopt.formats.profile_v1 import check_flow, read_profile

    g = _gate("minimal")
    out = GATE_ROOT / "minimal"
    p = read_profile(g["profile"])
    assert check_flow(p).ok and p.complete
    q = json.loads((out / "t-b1-6/nochain/diag.json").read_text())
    assert q["trace_lines"] == p.collection["runs"][0]["total_dispatches"]
    ph = {x["name"]: x for x in p.phases}
    assert ph["steady"]["reached"] and ph["steady"]["marker"] == "0x8003972"
    pr = json.loads((out / "prof/profile_run.json").read_text())
    assert pr["isolation"]["ok"] and not pr["isolation"]["changed"]
    tr = json.loads((out / "prof-trunc/profile_run.json").read_text())
    assert tr["runs"][0]["appended"] == tr["runs"][0]["profile_only"] > 0
    graph = json.loads((out / "graph/graph.json").read_text())
    assert all("dst" not in e and e["exit"].get("id") for e in graph["edges"])
    tg = json.loads((out / "trunc/graph.json").read_text())
    assert any(e["resolution"] == "not_in_snapshot" for e in tg["edges"])


@pytest.mark.qemu
@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("SCOPT_B1_GATE_E2E") != "1",
                    reason="opt-in: SCOPT_B1_GATE_E2E=1 (about three minutes of QEMU runs)")
def test_b1_gate_end_to_end_minimal(tmp_path):
    out = tmp_path / "gate"
    p = subprocess.run([sys.executable, str(WT / "tests/qemu/b1_gate.py"), "--workload",
                        "minimal", "--out", str(out), "--reps", "2", "--profile-reps", "2",
                        "--skip-t9"], cwd=str(WT), capture_output=True, text=True,
                       timeout=3600)
    g = json.loads((out / "gate.json").read_text())
    for t in [f"T-B1-{i}" for i in range(1, 9)]:
        assert g["tests"][t]["verdict"] == "pass", (t, g["tests"][t]["failed"], p.stdout[-2000:])
    assert g["ledger"]["verdict"] == "pass"
