"""WP28 (B3 gate) driver: the pure judgement helpers, offline (no QEMU).

The gate run itself is ``tests/qemu/wp28_gate.py`` (evidence in ``runs/wp28``); its results
are checked by ``tests/acceptance/test_b3_gate.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from scopt.paths import fastdyn_root

HERE = Path(__file__).resolve().parent
QDIR = HERE.parent / "qemu"
FIX = (fastdyn_root() / "fixtures/b3")
pytestmark = pytest.mark.skipif(not (FIX / "b3conf.py").is_file(),
                                reason=f"{FIX / 'b3conf.py'} not present")
if str(QDIR) not in sys.path:
    sys.path.insert(0, str(QDIR))


@pytest.fixture(scope="module")
def G():
    import wp28_gate

    return wp28_gate


@pytest.fixture(scope="module")
def S():
    import wp28_steps

    return wp28_steps


def _val(codes, verdict="accepted"):
    return {"verdict": verdict, "codes": codes}


def _v6_codes(**over):
    c = {f"V{i}": "pass" for i in range(1, 24)}
    c.update(V9="skip", V22="skip", V10=None)
    c.update(over)
    return c


def test_static_rows_ok_v6(G):
    ok, why = G.static_rows_ok(_val(_v6_codes()), 6, 1)
    assert ok and "V9, V22 skip" in why
    # V14 may skip only without a SPEC link
    assert G.static_rows_ok(_val(_v6_codes(V14="skip")), 6, 0)[0]
    assert not G.static_rows_ok(_val(_v6_codes(V14="skip")), 6, 1)[0]
    # any failing row, or a rejected artifact, fails
    assert not G.static_rows_ok(_val(_v6_codes(V13="fail")), 6, 1)[0]
    assert not G.static_rows_ok(_val(_v6_codes(), "rejected"), 6, 1)[0]
    # V22 is never required to pass (WP23 not approved) but must not fail
    assert not G.static_rows_ok(_val(_v6_codes(V22="fail")), 6, 1)[0]


def test_static_rows_ok_v5_identity(G):
    c = {f"V{i}": "pass" for i in range(1, 10)}
    c["V8"] = "skip"
    ok, why = G.static_rows_ok(_val(c), 5, 0)
    assert ok and why.startswith("v5")
    c["V9"] = "fail"
    assert not G.static_rows_ok(_val(c), 5, 0)[0]


def test_fixture_table_is_consistent(G):
    import b3conf

    for cfg, fx in G.FIXTURES.items():
        assert cfg in b3conf.BY_NAME, cfg
        elf = b3conf.BY_NAME[cfg].elf
        for s in fx.vc:
            G.sym(s, elf)  # every continuation symbol resolves
        for s, _code in fx.exit_reasons:
            G.sym(s, elf)
        assert fx.spec == bool(fx.vc) or cfg.startswith("24-") or cfg == "23", cfg
    # the gate covers every §11 sub-case and hardening fixture that has a config
    for cfg in ("1", "1p", "2", "3", "3b", "4", "5", "6", "6t", "6r", "6h", "7", "8", "8p", "9",
                "10", "11", "12", "13", "14", "17", "20", "21", "22", "23"):
        assert cfg in G.FIXTURES
    assert G.FIXTURES["11"].inputs == ("11-train", "11-heldout")


def test_jcc_flip(S):
    jne = bytes.fromhex("0f8510000000")
    nops = S.nop(6)
    j = S.jcc_flip(jne, nops)
    assert j[0] == 0xE9 and len(j) == 6
    # rel32 of the jmp is one more (5-byte jmp + nop vs a 6-byte jcc): same target
    assert int.from_bytes(j[1:5], "little", signed=True) == 0x11
    assert S.jcc_flip(jne, j) == S.nop(6)
    assert S.jcc_flip(bytes.fromhex("7405"), S.nop(2)) == bytes.fromhex("eb05")
    with pytest.raises(ValueError):
        S.jcc_flip(bytes.fromhex("9090"), bytes.fromhex("9090"))


def _dyn_doc(tmp: Path, rows: dict, aa: dict | None = None) -> Path:
    (tmp / "dynamic.json").write_text(json.dumps({"rows": rows}))
    (tmp / "aa.json").write_text(json.dumps({"o2a_differs_aa": aa or {}}))
    (tmp / "artifacts.json").write_text(json.dumps({"rows": {}}))
    return tmp


def _case(o2a="pass", hits=None, inp="7"):
    return {"fixture_input": inp, "oracles": {"O1": "pass", "O2a": o2a, "O2b": "pass",
                                              "O3": "pass", "O5": "reported"},
            "hits": hits or {"variant 0 C'": 5}, "stats": {"artifact": {"links_armed": 1}}}


def test_dyn_case_ok_noicount_timer_o2a(S, tmp_path):
    # noicount timer phase: O2a fail is reported only when the A/A run differs too
    out = _dyn_doc(tmp_path, {"noicount/7": {"cases": [_case("fail")], "gate_binary": True,
                                             "verdict": "fail"}}, {"7": True})
    ok, det = S.dyn_case_ok(out, "noicount", "7")
    assert ok and det["cases"][0]["o2a"].startswith("reported only")
    out = _dyn_doc(tmp_path, {"noicount/7": {"cases": [_case("fail")], "gate_binary": True}},
                   {"7": False})
    assert not S.dyn_case_ok(out, "noicount", "7")[0]
    # icount: O2a must pass; a non-timer noicount phase too
    out = _dyn_doc(tmp_path, {"icount/7": {"cases": [_case("fail")], "gate_binary": True},
                              "noicount/1": {"cases": [_case("fail", inp="1")],
                                             "gate_binary": True}}, {"7": True, "1": True})
    assert not S.dyn_case_ok(out, "icount", "7")[0]
    assert not S.dyn_case_ok(out, "noicount", "1")[0]
    # a run on another binary never passes
    out = _dyn_doc(tmp_path, {"icount/1": {"cases": [_case(inp="1")], "gate_binary": False}})
    assert not S.dyn_case_ok(out, "icount", "1")[0]


def test_extra_nonvacuity_thresholds(S, tmp_path):
    out = _dyn_doc(tmp_path, {})
    det = {"cases": [{"input": "14", "hits": {"variant 0 C'": 499, "slot 4 F (SPEC": 501}}]}
    assert S.extra_nonvac(out, "icount", "14", det)[0]
    det = {"cases": [{"input": "14", "hits": {"variant 0 C'": 999}}]}
    assert not S.extra_nonvac(out, "icount", "14", det)[0]  # C' ran after the MPU enable
    det = {"cases": [{"input": "1", "hits": {"variant 0 C'": 0}}]}
    assert not S.extra_nonvac(out, "icount", "1", det)[0]  # vacuous
    det = {"cases": [{"input": "11-train", "hits": {"slot 3 F (SPEC, R": 1}},
                     {"input": "11-heldout", "hits": {"slot 3 F (SPEC, R": 601}}]}
    assert S.extra_nonvac(out, "icount", "11", det)[0]


def test_mutant_table_covers_the_dynamic_set(S):
    names = {m.name for m in S.MUTANTS}
    # §9.3 B3 mutants with a dynamic detector (B4 kinds are WP29's)
    for k in ("fold_flip", "falsified_fact_link", "fold_without_fact", "drop_g_pc",
              "drop_g_thumb", "drop_g_bp", "guard_before_effect", "guard_stub_store",
              "nop_dead_result_ldst", "icount_immediate", "search_row",
              "shared_variant_suffix", "dho_deletes_faulting_ldst"):
        assert k in names, k
    assert set(S.STATIC_ONLY) == {"drop_g_cond", "drop_g_flags", "drop_g_flags2"}
    assert not names & set(S.STATIC_ONLY)


def test_report_renders_from_minimal_evidence(tmp_path):
    import wp28_report as R

    v = {"date": "x", "gate": "FAIL", "blocking": ["4a F-B3-23 (O6)"],
         "gate_binary": {"tag": "t", "tag_commit": "c", "head": "c", "tree_clean": True,
                         "exe": "e", "exe_sha256": "s", "checked_at": "now"},
         "fixture_elf_sha256": "f",
         "spec_gate": {
             "1 two-TB example (T-B3-6, T-B3-8)": {
                 "result": "pass", "evidence": [], "why": "",
                 "detail": {"dynamic": {}, "o4_falsified_fact_and_fold_flip": [True],
                            "callee_summary_rows_with_patch_fact": 1}},
             "2 sub-cases": {"result": "pass", "evidence": [], "detail": {}, "why": ""},
             "4a F-B3-23 (O6)": {"result": "fail", "evidence": [], "detail": {}, "why": "w"},
             "4b F-B3-24 (-O0/-Os)": {"result": "pass", "evidence": [], "detail": {}, "why": ""},
             "5a T-B3-27 (v5 regression on the gate binary)": {"result": "pass", "evidence": [],
                                                               "detail": {}, "why": "cited"},
             "5b T-B3-28 (graph/summary round trip, multi_tb rows)": {
                 "result": "pass", "evidence": [], "detail": {"pass": 1, "artifacts": 1},
                 "why": ""}},
         "hardening": {n: {"result": "pass", "evidence": [], "why": "w",
                           "detail": {"n": 1} if n == "T-B3-26" else {}}
                       for n in ("F-B3-13a", "F-B3-13b", "F-B3-13c", "F-B3-13d", "F-B3-14",
                                 "F-B3-17", "F-B3-21", "F-B3-22", "F-B3-15", "F-B3-18",
                                 "T-B3-26")},
         "ledger_stages": {"artifacts": {}, "dynamic_runs": {}}}
    (tmp_path / "verdict.json").write_text(json.dumps(v))
    out = tmp_path / "B3-gate.md"
    sys.argv = ["wp28_report.py", "--runs", str(tmp_path), "--out", str(out)]
    assert R.main() == 0
    text = out.read_text()
    assert "**B3 gate: FAIL.**" in text and "4a F-B3-23 (O6)" in text
    assert "not_run: WP23 not approved" in text


# --------------------------------------------------------------------------- review fixes

def _gate_json(p: Path, verdict: str, failing: tuple[str, ...] = ()) -> Path:
    tests = {t: {"checks": [{"check": "c", "ok": t not in failing}]}
             for t in ("T-B1-1", "T-B1-9")}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"verdict": verdict, "tests": tests}))
    return p


def test_t_b3_27_judged_from_evidence_files(S, tmp_path):
    ok = _gate_json(tmp_path / "a/gate.json", "PASS")
    bad = _gate_json(tmp_path / "b/gate.json", "FAIL", ("T-B1-9",))
    rerun = tmp_path / "b/t-b1-9-rerun/pytest.out"
    rerun.parent.mkdir(parents=True)
    rerun.write_text("....\n24 passed in 15.01s\n")
    full_bad = _gate_json(tmp_path / "c/b2-gate.json", "FAIL")
    full_ok = _gate_json(tmp_path / "c-r2/b2-gate.json", "PASS")
    r = S.judge_t_b3_27({
        "plain": {"verdict": "anything", "evidence": str(ok)},
        "rerun_of_failing_test": {"verdict": "FAIL on first run ...", "evidence":
                                  [str(bad), str(rerun)]},
        "full_rerun": {"verdict": "first run FAIL ...", "evidence": [str(full_bad),
                                                                     str(full_ok)]}})
    assert r["ok"], r
    assert r["rows"]["rerun_of_failing_test"]["evidence"][0]["superseded_by"] == \
        "re-run of the failing tests"
    assert r["rows"]["full_rerun"]["evidence"][0]["superseded_by"] == "full re-run"
    # the verdict *text* no longer decides: a 'PASS' text over a failing file fails
    r = S.judge_t_b3_27({"x": {"verdict": "PASS (24 passed)", "evidence": str(bad)}})
    assert not r["ok"]
    # an early failure that no later re-run names is not superseded
    other = tmp_path / "d/other/pytest.out"
    other.parent.mkdir(parents=True)
    other.write_text("3 passed\n")
    r = S.judge_t_b3_27({"x": {"evidence": [str(bad), str(other)]}})
    assert not r["ok"] and r["rows"]["x"]["problems"]
    # a failing pytest summary and a missing file fail
    f = tmp_path / "e/pytest.out"
    f.parent.mkdir(parents=True)
    f.write_text("1 failed, 23 passed in 14.72s\n")
    assert not S.judge_t_b3_27({"x": {"evidence": str(f)}})["ok"]
    assert not S.judge_t_b3_27({"x": {"evidence": str(tmp_path / "nope.json")}})["ok"]
    assert not S.judge_t_b3_27({})["ok"]


def test_o4_detection_kind(S):
    div = {"detected": True, "cases": [{"details": {"O1": "fixture oracle: result[0]: got 1"}}]}
    crash = {"detected": True, "cases": [{"details": {"O1": "artifact run verdict "
                                                            "INVALID_exit_code; ..."}}]}
    assert S.o4_detection_kind(div) == "divergence"
    assert S.o4_detection_kind(crash) == "crash"
    assert S.o4_detection_kind({"detected": False, "cases": [{}]}) == "not_detected"
    assert S.o4_detection_kind({"detected": None}) == "not_run"
    assert S.o4_detection_kind({"detected": True, "scenario_mutant": {}}) == "scenario"


def test_timing_cpu_budget_parse(tmp_path):
    import wp28_timing_analyze as T

    lg = tmp_path / "timing-load.log"
    lg.write_text("1 2.0 2.1 2.2 1/1 1 | qemu=0\n"
                  "start 2026-11-17T19:25:01 load 2.7 cores 24-27 reps 30 gate_sha x\n"
                  "lock acquired 2026-11-17T19:25:01 load 2.7\n"
                  "1794961531 3.13 2.93 3.11 3/3009 2605597 | qemu=1\n"
                  "1794961561 4.95 3.31 3.22 3/3042 2608601 | qemu=0\n"
                  "end 2026-11-17T19:38:27 rc 0 load 1.79\n")
    cb = T.cpu_budget(lg)
    assert cb["cores"] == "24-27" and cb["n_cores"] == 4 and cb["reps"] == 30
    assert cb["lock_acquired"].startswith("lock acquired") and cb["load1_max"] == 4.95
    assert T.cpu_budget(tmp_path / "missing.log") is None


def _gd_row(verdict="PASS", armed=1, total=299, after=0):
    return {"scenario": "F-B3-12 (PAUSED-stop invalidation of an armed C)", "verdict": verdict,
            "stats": {"links_armed": armed}, "variant_hits_total": total,
            "variant_hits_after_write": after, "checks": [], "pairs": {}}


def test_f_b3_12_needs_the_paused_scenario(S, tmp_path, monkeypatch):
    """Sub-case 12 passes only with the PAUSED-stop invalidation row; the plain run (and
    WP24's vacuous breakpoint script) is not enough."""
    rows = {f"{m}/12": {"cases": [_case(inp="12")], "gate_binary": True, "verdict": "pass"}
            for m in ("icount", "noicount")}
    out = _dyn_doc(tmp_path, rows)
    (tmp_path / "artifacts.json").write_text(json.dumps({"rows": {
        f"{m}/12": {"ok": True, "status": "published", "artifact_dir": str(tmp_path / "none")}
        for m in ("icount", "noicount")}}))
    monkeypatch.setattr(S, "SPEC_SUBCASES", ["12"])
    monkeypatch.setattr(S, "sha256", lambda p: "x")

    def sub12(gd_rows):
        (tmp_path / "gdbdiag.json").write_text(json.dumps({"rows": gd_rows}))
        return S.step_verdict(out)["spec_gate"]["2 sub-cases"]["detail"]["12"]

    vac = {f"F-B3-12-wp24bp/{m}": {"verdict": "PASS", "stats": {"links_armed": 0},
                                   "variant_hits_total": 0} for m in ("icount", "noicount")}
    assert not sub12(vac)["ok"]
    good = {**vac, **{f"F-B3-12/{m}": _gd_row() for m in ("icount", "noicount")}}
    s = sub12(good)
    assert s["ok"] and "before the write" in s["modes"]["icount"]["nonvacuity"]
    assert s["modes"]["icount"]["wp24_bp_script_vacuous"]["links_armed"] == 0
    bad = {**good, "F-B3-12/noicount": _gd_row("FAIL", after=5)}
    assert not sub12(bad)["ok"]


# --------------------------------------------------------------------------- close-out

def _cov(tmp: Path, ok: bool = True, n_spec: int = 1) -> None:
    (tmp / "coverage.json").write_text(json.dumps({"rows": {m: {
        "ok": ok, "n_spec": n_spec, "artifact_version": 6, "o6": ["pass"],
        "c_prime_hits": [{"variant 0 C'": 999}]} for m in ("icount", "noicount")},
        "pass": ok}))


def _cneg(tmp: Path, modes=("icount", "noicount"), ok: bool = True) -> None:
    (tmp / "coverage-neg.json").write_text(json.dumps({"rows": {m: {
        "ok": ok, "o6": ["fail"], "c_prime_hits": [999]} for m in modes}}))


def test_4a_needs_coverage_and_the_coverage_neg_control(S, tmp_path):
    _cov(tmp_path)
    r = S.item_4a(tmp_path)
    assert r["result"] == "fail" and "coverage-neg step not run" in r["why"]
    _cneg(tmp_path, modes=("icount",))
    assert S.item_4a(tmp_path)["result"] == "fail"  # both modes required
    _cneg(tmp_path, ok=False)
    assert S.item_4a(tmp_path)["result"] == "fail"
    _cneg(tmp_path)
    r = S.item_4a(tmp_path)
    assert r["result"] == "pass" and set(r["coverage_neg"]) == {"icount", "noicount"}


def test_4a_coverage_failure_is_never_rescued_by_coverage_neg(S, tmp_path):
    _cov(tmp_path, ok=False, n_spec=0)
    _cneg(tmp_path)
    assert S.item_4a(tmp_path)["result"] == "fail"
    assert S.item_4a(tmp_path / "missing")["result"] == "not_run"


def test_report_appends_hand_written_sections_verbatim(tmp_path):
    import wp28_report as R

    test_report_renders_from_minimal_evidence(tmp_path)  # writes verdict.json
    sec = tmp_path / "audit6.md"
    sec.write_text("## 6. Audit corrections\n\nAdded by hand; carried **verbatim**.\n")
    sec2 = tmp_path / "closeout.md"
    sec2.write_text("## 7. Close-out (2026-11-18)\n\n- what changed\n")
    out = tmp_path / "B3.md"
    assert R.main(["--runs", str(tmp_path), "--out", str(out), "--append-section", str(sec),
                   "--append-section", str(sec2)]) == 0
    text = out.read_text()
    assert sec.read_text() in text and sec2.read_text() in text
    assert text.index("## 6. Audit") < text.index("## 7. Close-out") and \
        text.index("## 5. Evidence index") < text.index("## 6. Audit")


def test_close_out_tables_are_consistent(S):
    assert S.EXPECTED_CHANGES == {"icount/23", "noicount/23"}
    assert "dynamic.json" not in S.CARRY_FILES  # carried row by row (byte-equal only)
    assert {"coverage.json", "coverage-neg.json", "mutants.json", "roundtrip.json",
            "t-b3-6.json", "artifacts.json"}.isdisjoint(S.CARRY_FILES)
