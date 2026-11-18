"""WP29 unit tests: the B4 gate verdict logic (``tests/qemu/verdict_wp29.py``) on synthetic
evidence directories, and the driver's static tables (``tests/qemu/wp29_gate.py``).
No QEMU run."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

QDIR = Path(__file__).resolve().parents[1] / "qemu"


@pytest.fixture(scope="module")
def V():
    sys.path.insert(0, str(QDIR))
    return importlib.import_module("verdict_wp29")


@pytest.fixture(scope="module")
def G():
    fx = Path("/data/extra_copy/fastdyn/fixtures/b3/b3conf.py")
    if not fx.is_file():
        pytest.skip("fixtures/b3 not present")
    sys.path.insert(0, str(QDIR))
    return importlib.import_module("wp29_gate")


def _w(p: Path, doc) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc))


SHA = "ab" * 32
ON_GATE = [f"sha256:{SHA}"]


def _good(tmp: Path, V) -> Path:
    _w(tmp / "gate.json", {"ok": True, "exe_sha256": SHA})
    _w(tmp / "compute" / "base.json", {
        "census_all_units": {"dead": {"D1": 469, "D2": 1665, "D3": 5602},
                             "d2_split": {"prefix": 678}},
        "census_tcg_m0": {"dead": {"D1": 0}}})
    (tmp / "pytest-t-b4-static.log").write_text("....\n30 passed in 1.0s\n")
    req = sorted({k for ks in V.DESIGN_93_B4.values() for k in ks})
    kinds = req + ["d2_before_lookup"]
    _w(tmp / "mutants.json", {
        "static": {"total": len(kinds), "rejected": len(kinds), "kinds": kinds,
                   "rows": [{"kind": k, "check": {"rejected": True}} for k in kinds],
                   "synthetic_b4": {"mutants": 1, "rejected": 1, "all_rejected": True,
                                    "results": [{"name": "host_abs_in_env", "rejected": True}]}},
        "o4": [{"kind": k, "verdict": "pass",
                "attempts": [{"verdict": "pass", "runs_qemu_sha256": ON_GATE}]}
               for k in req],
        # close-out: flag_store_before_helper is judged on F-B4-11, every icount instance
        # with its predicted divergence (noicount reported)
        "o4_extra": [{"kind": "flag_store_before_helper", "verdict": "pass", "attempts": [
            {"config": "b4-11", "mode": m, "sub": s, "flag": f, "uid": f"u{s}",
             "verdict": "pass", "predicted_exact": True, "runs_qemu_sha256": ON_GATE}
            for m in ("icount", "noicount") for s in "ABC" for f in "NZCV"]}]})
    pk, dy, de = [], [], []
    for cfg in V.FIXTURES_ITEM2 + ["b4-10"]:
        for mode in V.MODES:
            pk.append({"mode": mode, "config": cfg, "status": "published",
                       "by_class": {"D1": 1, "D2": 1, "D3": 0}, "static": {"check": None}})
            dy.append({"mode": mode, "config": cfg, "pass": True,
                       "gated_oracles": ["O1", "O2b", "O6"], "runs_qemu_sha256": ON_GATE,
                       "cpu_abort_seen": [False],
                       "cpu_abort_calls_artifact": {"resolved": True, "hits": 0}})
        de.append({"config": cfg, "deleted_in_entered_units": {"D1": 1, "D2": 2, "D3": 0}})
    _w(tmp / "package.json", {"rows": pk})
    _w(tmp / "dynamic.json", {"rows": dy})
    _w(tmp / "deletions.json", {"rows": de})
    _w(tmp / "compute" / "package.json", {"rows": [
        {"label": "b4", "status": "published", "by_class": {"D1": 1},
         "v12_host_abs": {"pass": True}}]})
    _w(tmp / "compute-v10.json", {"rows": [{"label": "b4", "verdict": "pass"}]})
    return tmp


def test_all_evidence_passing_gives_a_gate_pass(tmp_path, V):
    d = V.build_verdict(_good(tmp_path, V))
    assert d["b4_gate_pass"] is True, d["blocking"]
    assert d["gate"]["5 T-B4-11 ablations + timing (reported)"]["reported_outcome"] is True


def test_missing_evidence_is_never_a_pass(tmp_path, V):
    _w(tmp_path / "gate.json", {"ok": True})
    d = V.build_verdict(tmp_path)
    assert d["b4_gate_pass"] is False
    assert all(v["pass"] is not True for k, v in d["gate"].items() if not k.startswith("5"))


def test_undetected_dynamic_mutant_fails_item1(tmp_path, V):
    t = _good(tmp_path, V)
    mu = json.loads((t / "mutants.json").read_text())
    next(r for r in mu["o4"] if r["kind"] == "guest_store_delete")["verdict"] = "fail"
    _w(t / "mutants.json", mu)
    i1 = V.item1(t)
    assert i1["pass"] is False and i1["o4_not_detected"] == ["guest_store_delete"]


def test_static_only_kinds_need_no_o4(tmp_path, V):
    # d2_before_lookup is static-only (b4_mutants.STATIC_ONLY): no O4 row needed
    assert V.item1(_good(tmp_path, V))["pass"] is True


def test_a_second_round_detection_counts(tmp_path, V):
    t = _good(tmp_path, V)
    mu = json.loads((t / "mutants.json").read_text())
    next(r for r in mu["o4"] if r["kind"] == "flag_store_before_ldst")["verdict"] = "fail"
    _w(t / "mutants.json", mu)
    assert V.item1(t)["pass"] is False
    mu["o4_extra"] += [{"kind": "flag_store_before_ldst", "verdict": "pass",
                       "attempts": [{"verdict": "pass", "runs_qemu_sha256": ON_GATE}]}]
    _w(t / "mutants.json", mu)
    i1 = V.item1(t)
    assert i1["pass"] is True
    assert i1["design_93"]["delete a flag store before an LDST"]["flag_store_before_ldst"][
        "o4_round"] == 2


def test_a_later_round_record_replaces_an_undetected_round_one(tmp_path, V):
    t = _good(tmp_path, V)
    mu = json.loads((t / "mutants.json").read_text())
    next(r for r in mu["o4"] if r["kind"] == "flag_store_before_helper")["verdict"] = "fail"
    cov = {"instances": 18, "executed_instances": 18, "executed_instances_tried": 18}
    mu["o4_extra"] = [{"kind": "flag_store_before_helper", "verdict": "fail",
                       "attempts": [{"verdict": "fail"}] * 3, "coverage": cov}]
    _w(t / "mutants.json", mu)
    row = V.item1(t)["design_93"]["delete a flag store before a flags-0 helper"][
        "flag_store_before_helper"]
    assert row["o4"] == "fail" and row["o4_round"] == 2 and row["o4_coverage"] == cov
    assert row["o4_attempts_both_rounds"] == 4  # 1 (round 1) + 3


def test_a_detection_off_the_gate_binary_does_not_count(tmp_path, V):
    t = _good(tmp_path, V)
    mu = json.loads((t / "mutants.json").read_text())
    r = next(r for r in mu["o4"] if r["kind"] == "host_abs_in_env")
    r["attempts"] = [{"verdict": "pass", "runs_qemu_sha256": ["sha256:" + "cd" * 32]}]
    _w(t / "mutants.json", mu)
    i1 = V.item1(t)
    assert i1["pass"] is False
    assert i1["o4_by_kind"]["host_abs_in_env"] == "fail_not_on_gate_binary"


def test_item2_row_off_the_gate_binary_fails(tmp_path, V):
    t = _good(tmp_path, V)
    dy = json.loads((t / "dynamic.json").read_text())
    dy["rows"][3]["runs_qemu_sha256"] = ["sha256:" + "cd" * 32]
    _w(t / "dynamic.json", dy)
    i2 = V.item2(t)
    assert i2["pass"] is False
    assert [r["runs_on_gate_binary"] for r in i2["rows"]].count(False) == 1


@pytest.mark.parametrize("ca", [{"resolved": True, "hits": 1}, {"resolved": False, "hits": None},
                                None])
def test_f_b4_9_needs_a_resolved_zero_gdb_cpu_abort_count(tmp_path, V, ca):
    t = _good(tmp_path, V)
    dy = json.loads((t / "dynamic.json").read_text())
    for r in dy["rows"]:
        if r["config"] == "b4-9" and r["mode"] == "icount":
            r["cpu_abort_calls_artifact"] = ca
    _w(t / "dynamic.json", dy)
    assert V.item2(t)["pass"] is False


def test_driver_cpu_abort_gdb_zero(G):
    assert G.cpu_abort_gdb_zero({"cpu_abort_calls_artifact": {"resolved": True, "hits": 0}})
    assert not G.cpu_abort_gdb_zero({"cpu_abort_calls_artifact": {"resolved": True, "hits": 2}})
    assert not G.cpu_abort_gdb_zero({})


def test_sfb_row_reads_the_path_key(G, tmp_path):
    a = tmp_path / "art"
    a.mkdir()
    (a / "cache.sc").write_bytes(b"x")
    _w(tmp_path / "compute" / "sfb.json", {"status": "published", "path": str(a),
                                           "artifact_id": "sha256:1", "validation": [
                                               {"result": "pass"}, {"result": "fail", "c": 1}]})
    r = G.sfb_row(tmp_path, 0)
    assert r["artifact_dir"] == str(a) and r["status"] == "published" and r["rc"] == 0
    assert r["validation_rows"] == 2 and len(r["validation_failed"]) == 1
    assert r["cache_sc_sha256"] == G.sha_file(a / "cache.sc")


def test_timing_bootstrap_seed_sensitivity_marks_borderline(tmp_path):
    sys.path.insert(0, str(QDIR))
    ta = importlib.import_module("wp29_timing_analyze")
    rows = []
    # sfb-b4 vs sfb: ratios centred near 1 with one-sided spread -> CI lower bound ~ 0
    ratios = [1.0 + d for d in (-0.002, -0.001, 0.0, 0.0, 0.0005, 0.001, 0.002, 0.003,
                                0.004, 0.006)] * 3
    for i, r in enumerate(ratios):
        for arm, ns in (("warm", 1_000_000), ("sfb", 900_000), ("sfb-b4", 900_000 / r),
                        ("b4", 1_000_000 / 1.05), ("b4-d1", 1_000_000), ("b4-d2", 1_000_000),
                        ("b4-d3", 1_000_000)):
            rows.append({"arm": arm, "rep": i, "input": 0, "valid": True, "bench_ns": ns})
    (tmp_path / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = tmp_path / "a.json"
    sys.argv = ["x", str(tmp_path), "--out", str(out), "--seeds", "6"]
    ta.main()
    d = json.loads(out.read_text())["arms"]
    assert d["b4_vs_warm"]["verdict"] == "faster" and d["b4_vs_warm"][
        "ci_bound_signs_seed_stable"]
    a = d["sfb-b4_vs_sfb"]
    assert len(a["ci95_by_seed"]) == 6
    stable = a["ci_bound_signs_seed_stable"]
    assert a["verdict"] == (a["verdict_seed0"] if stable else "borderline")


@pytest.mark.real
def test_host_abs_sites_on_the_gate_f_b4_7_base(G):
    out = Path("/data/extra_copy/fastdyn/runs/wp29")
    if not (out / "fx" / "icount" / "b4-7" / "snap.json").is_file():
        pytest.skip("gate F-B4-7 base not present")
    _sj, s, lay, _ri = G.load(out, "icount", "b4-7")
    sites = G.host_abs_sites(s, lay)
    assert len(sites) == 2
    for x in sites:
        u = next(u for u in s.units() if u.uid == x["uid"])
        assert x["movabs"].startswith("movabs ") and x["store"].endswith(", 5")
        assert int.from_bytes(bytes(u.code[x["movabs_off"] + 2:x["movabs_off"] + 10]),
                              "little") == x["abs_addr"]
        assert x["store_off"] == x["movabs_off"] + 10
    # the repointed pointer's field and its margins are guest-dead (vfp.zregs on a CM3)
    for d in (-G.HOST_ABS_MARGIN, 0, G.HOST_ABS_MARGIN):
        assert "vfp.zregs" in lay.classify(G.HOST_ABS_ENV_DISP + d, 1).name
    lo, hi = lay.env_bounds()
    assert lo < G.HOST_ABS_ENV_DISP < hi


def test_t_b4_1_outside_5_percent_fails(tmp_path, V):
    t = _good(tmp_path, V)
    b = json.loads((t / "compute" / "base.json").read_text())
    b["census_all_units"]["dead"]["D1"] = 400
    _w(t / "compute" / "base.json", b)
    assert V.item1(t)["t_b4_1"]["pass"] is False


def test_vacuous_f_b4_10_fails_item4(tmp_path, V):
    t = _good(tmp_path, V)
    pk = json.loads((t / "package.json").read_text())
    for r in pk["rows"]:
        if r["config"] == "b4-10":
            r.update(status="no_change", by_class={"D1": 0, "D2": 0, "D3": 0})
    _w(t / "package.json", pk)
    i4 = V.item4(t)
    assert i4["pass"] is False and i4["vacuous"] is True
    assert V.build_verdict(t)["b4_gate_pass"] is False


def test_no_executed_d1_fails_item2(tmp_path, V):
    t = _good(tmp_path, V)
    de = json.loads((t / "deletions.json").read_text())
    de["rows"][0]["deleted_in_entered_units"]["D1"] = 0
    _w(t / "deletions.json", de)
    assert V.item2(t)["pass"] is False


def _fsbh(t: Path) -> tuple[dict, list]:
    mu = json.loads((t / "mutants.json").read_text())
    return mu, mu["o4_extra"][0]["attempts"]


def test_flag_store_before_helper_needs_every_f_b4_11_instance_as_predicted(tmp_path, V):
    t = _good(tmp_path, V)
    assert V.item1(t)["o4_specific_detector"]["flag_store_before_helper"]["pass"] is True
    mu, at = _fsbh(t)
    at[0]["predicted_exact"] = False  # O1 differs, but not exactly where predicted
    at[0]["verdict"] = "anomaly"
    _w(t / "mutants.json", mu)
    i1 = V.item1(t)
    assert i1["pass"] is False and "flag_store_before_helper" in i1["o4_not_detected"]


def test_flag_store_before_helper_round_one_detection_elsewhere_does_not_count(tmp_path, V):
    t = _good(tmp_path, V)
    mu, _ = _fsbh(t)
    mu["o4_extra"] = []  # round 1 says "pass" on another fixture; no F-B4-11 record
    _w(t / "mutants.json", mu)
    assert "flag_store_before_helper" in V.item1(t)["o4_not_detected"]


def test_flag_store_before_helper_noicount_is_reported_only(tmp_path, V):
    t = _good(tmp_path, V)
    mu, at = _fsbh(t)
    for a in at:
        if a["mode"] == "noicount":
            a["verdict"] = "fail"
    _w(t / "mutants.json", mu)
    sd = V.item1(t)["o4_specific_detector"]["flag_store_before_helper"]
    assert sd["pass"] is True and sd["noicount_reported"]["detected_as_predicted"] == 0


def test_flag_store_before_helper_off_the_gate_binary_fails(tmp_path, V):
    t = _good(tmp_path, V)
    mu, at = _fsbh(t)
    at[1]["runs_qemu_sha256"] = ["sha256:" + "cd" * 32]
    _w(t / "mutants.json", mu)
    assert V.item1(t)["pass"] is False


def test_item4_needs_executed_d1_on_f_b4_10(tmp_path, V):
    t = _good(tmp_path, V)
    assert V.item4(t)["pass"] is True
    de = json.loads((t / "deletions.json").read_text())
    next(r for r in de["rows"] if r["config"] == "b4-10")["deleted_in_entered_units"]["D1"] = 0
    _w(t / "deletions.json", de)
    i4 = V.item4(t)
    assert i4["pass"] is False and i4["executed_d1_gt_0"] is False


def test_item4_needs_o6_gated_on_the_gate_binary(tmp_path, V):
    t = _good(tmp_path, V)
    dy = json.loads((t / "dynamic.json").read_text())
    for r in dy["rows"]:
        if r["config"] == "b4-10" and r["mode"] == "noicount":
            r["gated_oracles"] = ["O1", "O2b"]
    _w(t / "dynamic.json", dy)
    assert V.item4(t)["pass"] is False


def test_f_b4_11_real_pass_row_is_reported(tmp_path, V):
    t = _good(tmp_path, V)
    d = V.build_verdict(t)
    assert d["reported_f_b4_11_real_pass"]["pass"] is False  # no b4-11 rows: never a pass
    assert d["b4_gate_pass"] is True  # reported, not gating


def test_driver_tables_are_consistent(G, V):
    assert G.DESIGN_93_B4 == V.DESIGN_93_B4
    for kind, cfgs in G.O4_PLAN:
        assert all(c in G.CONFIGS for c in cfgs), kind
    assert set(G.KERNEL_LABEL) == set(G.CONFIGS)
    for cfg in G.CONFIGS:
        assert G.sym(G.KERNEL_LABEL[cfg]) > 0
    # close-out: F-B4-11 is the flag_store_before_helper detector (first O4_EXTRA fixture,
    # every instance with its predicted divergence); not an item-2 fixture
    assert G.O4_EXTRA["flag_store_before_helper"][0] == G.B411 == "b4-11"
    assert G.O4_SPECIFIC == V.SPECIFIC_DETECTOR
    assert G.B411 in G.CONFIGS and G.B411 not in V.FIXTURES_ITEM2
    assert {s: G.sym(s) for s in G.B411_SUBPHASE_SYMS} == {
        "b4_11_a": 0xdf2, "b4_11_b": 0xe26, "b4_11_c": 0xe72}
    for k in ("log_reg_ring_store", "log_reg_index_store", "log_reg_value_field",
              "log_reg_delete"):
        assert G.o6_detects(k)
    assert not G.o6_detects("flag_store_before_helper")


def test_compute_toml_pins_the_gate_binary(G, tmp_path):
    p = G.compute_toml(tmp_path)
    t = p.read_text()
    assert f'qemu_path = "{G.GATE}"' in t and "qemu-int" not in t.split("qemu_path")[1][:80]


def test_report_appends_hand_written_sections_verbatim(tmp_path):
    """wp29_report.py on the WP29 evidence (real files; skipped without them): a carried
    section is appended verbatim, after the generated text."""
    ref = Path("/data/extra_copy/fastdyn/runs/wp29")
    if not (ref / "verdict.json").is_file():
        pytest.skip("no WP29 gate run")
    sys.path.insert(0, str(QDIR))
    R = importlib.import_module("wp29_report")
    sec = tmp_path / "closeout.md"
    sec.write_text("## Close-out (2026-11-18)\n\nHand-written, carried **verbatim**.\n")
    out = tmp_path / "B4.md"
    assert R.main(["--out", str(ref), "--report", str(out), "--append-section", str(sec),
                   "--date", "2026-11-17"]) == 0
    text = out.read_text()
    assert text.endswith(sec.read_text()) and text.index("## Reproduce") < text.index(
        "## Close-out")
    assert "**The B4 gate does not pass**" in text  # WP29's verdict (items 1 and 4)
