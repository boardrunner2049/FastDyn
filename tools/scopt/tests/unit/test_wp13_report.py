"""WP13: ``scopt report`` on synthetic run.json and ledger files (never fails)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scopt.cli import cli, load_commands
from scopt.report import stats
from scopt.report.collect import STAGES, collect, hotset_metrics, sc_metrics
from scopt.report.render import render_markdown, write_report
from tests.synth.builders import build_sc, resolve_unit, simple_units


def _run_json(root: Path, *, bench_ns, verdict="PASS", exit_code=0, deployed=True,
              guest=("Reg 0:0x00000003",), appended=0, label=None, cache_name="x.sc"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "rundir.json").write_text(json.dumps({"schema": "scopt.rundir/1", "label": label,
                                                  "cache_name": cache_name}))
    if deployed:
        (root / "deploy.json").write_text(json.dumps({"artifact": {"sc": None}}))
    doc = {"schema": "scopt.run/1", "exit_code": exit_code, "timed_out": False,
           "verdict": verdict, "stray_capture_files": [],
           "bench": [] if bench_ns is None else [{"tag": "minimal", "elapsed_ns": bench_ns}],
           "timings": {"wall_s": 0.25, "children_user_s": 0.2, "children_sys_s": 0.03,
                       "children_maxrss_kb": 48000, "start_unix": 1.0},
           "appended": appended if deployed else None, "guest_norm": list(guest),
           "qemu": "/q", "qemu_sha256": "sha256:00"}
    (root / "run.json").write_text(json.dumps(doc))


def _cli(*args):
    load_commands()
    return CliRunner().invoke(cli, list(map(str, args)), catch_exceptions=False)


# --------------------------------------------------------------------------
# never fails
# --------------------------------------------------------------------------

def test_report_empty_and_missing_workspace(tmp_path):
    (tmp_path / "w").mkdir()
    r = _cli("report", tmp_path / "w", tmp_path / "does-not-exist", "--out",
             tmp_path / "r" / "report.md")
    assert r.exit_code == 0, r.output
    md = (tmp_path / "r" / "report.md").read_text()
    assert "## Outcome ledger (stages as columns)" in md
    # every stage is a column and reads not_run
    assert "| subject | id | " + " | ".join(STAGES) + " |" in md
    assert "| (none) | — | " + " | ".join(["not_run"] * len(STAGES)) + " |" in md
    for arm in ("nocache", "empty", "warm", "identity", "layout"):
        assert f"| {arm} | not_run |" in md
    assert "| semantic | unsupported |" in md and "| combined | unsupported |" in md
    assert (tmp_path / "r" / "report.json").is_file()
    assert (tmp_path / "r" / "timing.csv").is_file()


def test_report_every_stage_missing_for_a_known_artifact(tmp_path):
    """An artifact directory without any ledger rows: a row, every stage not_run."""
    art = tmp_path / "w" / "store" / "artifacts" / "sha256-abc"
    art.mkdir(parents=True)
    build_sc(simple_units(2), path=str(art / "cache.sc"))
    (art / "validation.json").write_text(json.dumps({"checks": [
        {"code": "V1", "verdict": "PASS"}, {"code": "V9", "ok": False, "detail": "x"}]}))
    m = collect([tmp_path / "w"])
    t = [x for x in m["ledger"]["table"] if x["subject"] == "artifact"]
    assert len(t) == 1 and t[0]["no_ledger_rows"]
    assert all(c["verdict"] == "not_run" for c in t[0]["stages"].values())
    a = m["artifacts"][0]
    assert [v["verdict"] for v in a["validation"]] == ["PASS", "FAIL"]
    assert a["sc"]["valid_records"] == 2
    md = render_markdown(m)
    assert "| V9 | FAIL | x |" in md


def test_report_malformed_inputs_are_problems_not_failures(tmp_path):
    w = tmp_path / "w"
    (w / "c").mkdir(parents=True)
    (w / "c" / "campaign.json").write_text("{not json")
    (w / "bad" / "r000").mkdir(parents=True)
    (w / "bad" / "r000" / "run.json").write_text("[]garbage")
    (w / "ledger.jsonl").write_text('{"subject":"artifact"}\nnot json\n')
    (w / "x.profile.json").write_text("{}")
    r = _cli("report", w)
    assert r.exit_code == 0, r.output
    m = json.loads((w / "report.json").read_text())
    where = " ".join(p["where"] for p in m["problems"])
    assert "campaign.json" in where and "run.json" in where and "ledger.jsonl" in where


def test_report_partial_model_renders():
    md = render_markdown({})
    assert "not_run" in md
    md = render_markdown({"groups": [], "ledger": None, "problems": [{"where": "x",
                                                                     "problem": "y"}]})
    assert "| x | y |" in md


# --------------------------------------------------------------------------
# ledger stages as columns, never inferred
# --------------------------------------------------------------------------

def test_ledger_columns_never_inferred(tmp_path):
    from scopt.ledger import Ledger
    led = Ledger(clock=lambda: "2026-11-16T00:00:00.000Z")
    led.emit("artifact", "sha256:a1", "inspected", "ok")
    led.emit("artifact", "sha256:a1", "measured", "measured", "valid=30/30")
    led.emit("pass", "identity@1", "optimized", "no_change", "identity")
    led.emit("artifact", "sha256:a2", "accepted", "rejected", "V3")
    for i in range(3):
        led.emit("unit", f"tb:{i}#0", "inspected", "ok_relocatable" if i else "pinned")
    for i in range(2):
        led.emit("run", f"c/warm/r{i:03d}", "measured", "measured")
    (tmp_path / "w").mkdir()
    led.write(tmp_path / "w" / "ledger.jsonl")
    m = collect([tmp_path / "w"])
    rows = {(t["subject"], t["id"]): t["stages"] for t in m["ledger"]["table"]}
    a1 = rows[("artifact", "sha256:a1")]
    assert a1["inspected"]["verdict"] == "ok" and a1["measured"]["verdict"] == "measured"
    # compiled/accepted/validated_dynamic are not inferred from 'measured'
    for s in ("optimized", "compiled", "accepted", "validated_dynamic"):
        assert a1[s]["verdict"] == "not_run"
    assert rows[("artifact", "sha256:a2")]["accepted"]["verdict"] == "rejected"
    assert rows[("artifact", "sha256:a2")]["compiled"]["verdict"] == "not_run"
    assert rows[("pass", "identity@1")]["optimized"]["verdict"] == "no_change"
    assert rows[("run", "c/warm/*")]["measured"]["verdict"] == "measured: 2"
    assert m["ledger"]["units"]["inspected"] == {"ok_relocatable": 2, "pinned": 1}
    md = render_markdown(m)
    assert "| artifact | sha256:a1 | ok | not_run | not_run | not_run | not_run | measured |" \
        in md


# --------------------------------------------------------------------------
# loose WP8 run directories (no campaign)
# --------------------------------------------------------------------------

def test_loose_runs_paired_by_rep(tmp_path):
    w = tmp_path / "w"
    for rep in range(6):
        _run_json(w / "warm" / f"r{rep:03d}", bench_ns=250_000 + rep)
        _run_json(w / "nocache" / f"r{rep:03d}", bench_ns=1_100_000 + rep, verdict="NOT_PROVED",
                  deployed=False, cache_name=None)
    _run_json(w / "nocache" / "r006", bench_ns=None, verdict="NOT_PROVED", deployed=False,
              cache_name=None)  # no BENCH: not a sample
    m = collect([w], bootstrap=300)
    g = m["groups"][0]
    assert g["reference"] == "warm" and g["paired"]
    assert g["per_arm"]["nocache"]["valid"] == 6 and g["per_arm"]["nocache"]["runs"] == 7
    c = [x for x in g["comparisons"] if x["metric"] == "bench"][0]
    assert c["arm"] == "nocache" and c["n_pairs"] == 6
    assert c["median"] == pytest.approx(250_002.5 / 1_100_002.5, rel=1e-3)
    assert c["verdict"] == "slower"
    kinds = {r["kind"] for r in m["regressions"]}
    assert "invalid_runs" in kinds and "expected_baseline_slower" in kinds
    md = render_markdown(m)
    assert "BENCH comparison" in md and "| nocache | warm | fixed | 6 |" in md


def test_loose_runs_unpaired(tmp_path):
    w = tmp_path / "w"
    _run_json(w / "warm", bench_ns=200)
    _run_json(w / "warm2", bench_ns=400, label=None)
    m = collect([w], bootstrap=100)
    g = m["groups"][0]
    assert not g["paired"]
    c = [x for x in g["comparisons"] if x["metric"] == "bench"][0]
    assert c["mode"] == "unpaired" and c["median"] == pytest.approx(0.5)


def test_output_mismatch_and_fallback_reported(tmp_path):
    w = tmp_path / "w"
    for rep in range(3):
        _run_json(w / "warm" / f"r{rep:03d}", bench_ns=100)
        _run_json(w / "layout" / f"r{rep:03d}", bench_ns=90, appended=2 if rep == 1 else 0,
                  guest=("Reg 0:0x00000004",) if rep == 2 else ("Reg 0:0x00000003",))
    m = collect([w], bootstrap=100)
    kinds = [r["kind"] for r in m["regressions"]]
    assert kinds.count("output_mismatch") == 1
    assert "fallback_translations" in kinds
    assert "nondeterministic_output" in kinds


# --------------------------------------------------------------------------
# sizes and the hot-set model
# --------------------------------------------------------------------------

def test_sc_metrics(tmp_path):
    units = simple_units(3)
    p = tmp_path / "a.sc"
    build_sc(units, path=str(p))
    m = sc_metrics(p)
    rs = [resolve_unit(u) for u in units]
    assert m["valid_records"] == 3
    assert m["populated_native_bytes"] == sum(r.end_offset - r.tc_offset for r in rs)
    assert m["host_code_bytes"] == sum(len(u.code) for u in units)
    assert m["logical_file_size"] == os.path.getsize(p)
    assert m["allocated_bytes"] == os.stat(p).st_blocks * 512
    assert m["arena_capacity_bytes"] >= 64 << 10


def _profile_doc(uids, entries, statuses=None, reached=True):
    statuses = statuses or ["exact"] * len(uids)
    return {"schema": "fastdyn.profile", "profile_id": "sha256:p", "snapshot_id": "sha256:s",
            "phases": [{"id": 0, "name": "boot", "reached": True},
                       {"id": 1, "name": "steady", "reached": reached}],
            "units": [{"unit": u, "status": s,
                       "phase": {"0": {"entries": 1}, "1": {"entries": n}}}
                      for u, n, s in zip(uids, entries, statuses)]}


@pytest.mark.parametrize("semantic", [False, True])
def test_hotset_metrics(tmp_path, semantic):
    from scopt.formats.sc_records import read_sc
    units = simple_units(4)
    p = tmp_path / "a.sc"
    build_sc(units, path=str(p))
    sc = read_sc(p)
    uids = sc.unit_ids()
    if semantic:
        from scopt.formats.sc_semantic import ExtTable
        from scopt.formats.supercache import write_semantic_cache

        write_semantic_cache(p, sc.header, [(u.record, u.body) for u in sc.units()], ExtTable(artifact_id8=b"footprnt"), rewritten=True)
    doc = _profile_doc(uids + ["tb:missing#0"], [100, 0, 5, 1, 0],
                       ["exact", "exact", "exact", "unknown", "exact"])
    h = hotset_metrics(doc, p, coverage=0.9)
    assert h["status"] == "ok" and h["join_coverage"] == 1.0
    assert h["non_exact_units"] == 1           # the unknown unit is not treated as zero
    assert h["hot"]["units"] == 1 and h["hot"]["entries_covered"] == 100
    assert h["executed"]["units"] == 2
    r0 = resolve_unit(units[0])
    assert h["hot"]["code_bytes"] == len(units[0].code)
    assert h["hot"]["lines64_code"] == (r0.tc_offset + len(units[0].code) - 1) // 64 \
        - r0.tc_offset // 64 + 1
    assert h["hot"]["pages4k_code"] == 1
    assert "model" in h["label"]
    nr = hotset_metrics(_profile_doc(uids, [1, 1, 1, 1], reached=False), p)
    assert nr["status"] == "not_run" and nr["reason"] == "phase_not_reached:steady"
    other = hotset_metrics(_profile_doc(["tb:x#0"], [5]), p)
    assert other["status"] == "not_run" and other["reason"] == "no_matching_entries"


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------

def test_paired_speedup_deterministic_and_ci():
    ref = [100.0 + i for i in range(30)]
    arm = [r / 1.25 for r in ref]
    a = stats.paired_speedup(ref, arm, reps=2000, seed=5)
    b = stats.paired_speedup(ref, arm, reps=2000, seed=5)
    assert a == b
    assert a["median"] == pytest.approx(1.25)
    assert a["ci"][0] == pytest.approx(1.25) and a["ci"][1] == pytest.approx(1.25)
    assert a["verdict"] == "faster"
    noisy = stats.paired_speedup([1.0, 1.1, 0.9, 1.05, 0.95, 1.0],
                                 [1.0, 0.9, 1.1, 0.95, 1.05, 1.0], reps=2000)
    assert noisy["ci"][0] <= noisy["median"] <= noisy["ci"][1]
    assert noisy["verdict"] == "no_significant_difference"
    one = stats.paired_speedup([1.0], [2.0])
    assert one["verdict"] == "insufficient_samples" and one["median"] == 0.5
    assert stats.paired_speedup([], [])["verdict"] == "no_data"
    with pytest.raises(ValueError):
        stats.paired_speedup([1.0], [1.0, 2.0])


def test_describe_and_quantile():
    d = stats.describe([1, 2, 3, 4])
    assert d["median"] == 2.5 and d["q1"] == 1.75 and d["q3"] == 3.25 and d["n"] == 4
    assert stats.describe([])["median"] is None


def test_write_report_files(tmp_path):
    w = tmp_path / "w"
    _run_json(w / "warm" / "r000", bench_ns=100)
    paths = write_report(collect([w], bootstrap=10), tmp_path / "out" / "r.md")
    assert Path(paths["json"]).name == "r.json"
    csv = Path(paths["csv"]).read_text().splitlines()
    assert csv[0].startswith("group,pos,rep,input,arm,valid")
    assert len(csv) == 2
