"""B3 gate acceptance (WP28; design §11, plan ``## WP28``): checks the evidence the gate run
``tests/qemu/wp28_gate.py`` wrote under ``runs/wp28`` (``WP28_RUNS`` overrides).

Each test names the design test id it covers. The run itself needs the frozen gate binary
and takes about an hour; these tests only read its JSON, so they skip when it is absent.
F-B3-23 is the one item the gate run found failing; its test asserts the recorded failure
(and its reason) instead of hiding it, and is marked ``xfail(strict=True)`` on the pass
condition so a later fix shows up.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

RUNS = Path(os.environ.get("WP28_RUNS", "/data/extra_copy/fastdyn/runs/wp28"))
GATE_JSON = Path("/data/extra_copy/fastdyn/runs/wp22/gate-binary.json")
pytestmark = pytest.mark.skipif(not (RUNS / "verdict.json").is_file(),
                                reason=f"no B3 gate run in {RUNS}")


def J(name: str):
    p = RUNS / name
    if not p.is_file():
        pytest.skip(f"{p} missing")
    return json.loads(p.read_text())


@pytest.fixture(scope="module")
def v():
    return J("verdict.json")


def test_gate_binary_frozen(v):
    g = json.loads(GATE_JSON.read_text())
    assert v["gate_binary"]["exe_sha256"] == g["exe_sha256"]
    assert v["gate_binary"]["tag_commit"] == v["gate_binary"]["head"] == g["commit"]
    assert v["gate_binary"]["tree_clean"] is True


def test_bases_on_gate_binary():
    b = J("bases.json")
    assert b["ok"] and b["n_ok"] == b["n"] >= 60


def test_t_b3_6_pass_separation():
    """T-B3-6: CP/BF/DHO each with its own non-zero, distinct byte delta and ledger row."""
    t = J("t-b3-6.json")
    assert t["pass"]
    for m in ("icount", "noicount"):
        full = t["rows"][m]["cases"]["full"]["pass_byte_delta"]
        assert full == {"constant-propagation": 3, "branch-folding": 6, "dead-host-ops": 4}


def test_t_b3_8_two_tb_dynamic(v):
    """T-B3-8: F-B3-1 O1/O2a/O2b equal, O3 C' hits > 0, O4 falsified fact detected."""
    assert v["spec_gate"]["1 two-TB example (T-B3-6, T-B3-8)"]["result"] == "pass"


def test_every_fixture_artifact_static_rows():
    """§11 item 3: V1-V21 and V23 on every accepted artifact (V22 not_run: WP23)."""
    a = J("artifacts.json")
    pub = [r for r in a["rows"].values() if r.get("status") == "published"]
    assert len(pub) == a["n"] == 62
    assert all(r["static_rows_ok"] for r in pub)
    assert all(r.get("publish", {}).get("exit") == 0 for r in pub)


@pytest.mark.parametrize("cfg", ["2", "3", "3b", "4", "5", "6", "6t", "6r", "6h", "7", "20", "1p",
                                 "8", "8p", "9", "10", "11", "12"])
def test_t_b3_9_to_25_and_31_to_37_subcases(v, cfg):
    """T-B3-9…T-B3-25, T-B3-31…T-B3-37: each sub-case O1 + O2a/O2b per mode, non-vacuity."""
    s = v["spec_gate"]["2 sub-cases"]["detail"][cfg]
    assert s["ok"], s


def test_f_b3_12_invalidates_an_armed_link():
    """F-B3-12 (§9.5): armed link, debug write cbz -> cbnz, C' hits stop after K, armed
    unchanged, slot reset observed, output = warm with the same write (PAUSED stop on the
    F-B3-13 kernel; WP24's breakpoint script never arms a link and is not counted)."""
    g = J("gdbdiag.json")["rows"]
    for m in ("icount", "noicount"):
        r = g[f"F-B3-12/{m}"]
        assert r["verdict"] == "PASS" and "PAUSED" in r["scenario"], r
        assert r["stats"]["links_armed"] == 1
        assert r["variant_hits_total"] == 299 and r["variant_hits_after_write"] == 0
        assert r["slot_before"]["target_variant"] == 0 and r["slot_before"]["dest"]
        assert r["slot_after"]["at_reset"] is True and not r["slot_after"]["dest"]
        assert r["pairs"] == {"0x520000": 300, "0x520001": 700}
        warm = next(c for c in r["checks"] if c["check"].startswith("output equals warm"))
        assert warm["ok"] and warm["detail"]["warm"] == r["pairs"]
        # the old script is kept and marked vacuous
        assert g[f"F-B3-12-wp24bp/{m}"]["stats"]["links_armed"] == 0


def test_f_b3_17_smc_with_a_link_into_ram(v):
    """F-B3-17: production artifact (no SPEC link) plus the test-only SPEC link into the RAM
    continuation: C' stops at the guest's rewrite, outputs equal warm, nothing bypassed."""
    s = J("smc17.json")
    assert s["pass"]
    for m in ("icount", "noicount"):
        r = s["rows"][m]
        assert r["package"]["rows_bypassed"] == [] and r["plan"]["C_origin"] == "ram_resident"
        assert r["c_prime_hits"] == [299] and all(x > 0 for x in r["f_hits"])
        assert all(r["items"].values())
    assert v["hardening"]["F-B3-17"]["result"] == "pass"


def test_f_b3_13a_substituted_criterion_is_recorded(v):
    """F-B3-13(a): refused[bp] > 0 is unreachable (CF_BP_PAGE); the substitution is explicit."""
    h = v["hardening"]["F-B3-13a"]
    assert h["criterion"]["met"] is False and h["substituted"] is True
    assert set(h["criterion"]["observed_refused_bp"].values()) == {0}
    assert v["hardening"]["F-B3-13b"]["result"] == "pass"


def test_timing_cpu_budget_recorded(v):
    cb = v["timing_cpu_budget"]
    assert cb["n_cores"] == 4 and cb["cores"] == "24-27" and "timing.lock" in cb["lock"]
    t = J("timing-analysis.json")
    assert t["cpu_budget"] == cb


def test_b3_spec_variants_ran_on_the_timed_inputs():
    """The b3-spec null timing result is not a coverage gap: every variant was entered."""
    h = J("compute-hits.json")["arms"]["b3-spec"]
    assert h["ok"] and h["every_variant_entered"] and h["c_prime_total"] > 100000


def test_t_b3_40_stale(v):
    """T-B3-40 / F-B3-16 (a)-(e)."""
    assert v["spec_gate"]["2 sub-cases"]["detail"]["16"]["ok"]


def test_t_b3_5_mutation_suite_static():
    """T-B3-5: §9.3 mutation suite rejected 100 % statically (hostsym or the full validator)."""
    m = J("mutants.json")
    assert m["synthetic_all_rejected"] and m["all_rejected"]
    assert m["totals"]["real"]["mutants"] == m["totals"]["real"]["rejected"] > 0


def test_o4_every_dynamic_set_mutant_detected():
    """§11 item 3: every mutant outside the static-only set detected dynamically (O4)."""
    o = J("o4.json")
    assert o["all_dynamic_detected"] and o["all_static_rejected"]
    assert set(o["static_only"]) == {"drop_g_cond", "drop_g_flags", "drop_g_flags2"}


def test_t_b3_42_ucond(v):
    """T-B3-42: condexec == 0 at every return-exit slot (fixtures and compute)."""
    assert v["spec_gate"]["3b T-B3-42 U-COND (drop_g_cond static-only argument)"]["result"] == \
        "pass"


@pytest.mark.xfail(strict=True, reason="WP28 run (reports/B3-gate.wp28.md; closed by the "
                                       "close-out re-gate, runs/closeout/b3): "
                                       "F-B3-23: no validated B3 artifact has a variant under "
                                       "a coverage configuration (LOG_REG block pins HostOps; "
                                       "WP17/WP19 contract change needed)")
def test_t_b3_38_coverage_o6(v):
    """T-B3-38 / F-B3-23 (O6): coverage equal and C' hits > 0."""
    assert v["spec_gate"]["4a F-B3-23 (O6)"]["result"] == "pass"


def test_t_b3_38_failure_is_recorded(v):
    """F-B3-23 is computed from coverage.json (not hard-coded): O6 equal, but no C' hit."""
    x = v["spec_gate"]["4a F-B3-23 (O6)"]
    c = J("coverage.json")
    assert x["result"] == ("pass" if c["pass"] else "fail")
    assert all(r["o6_equal"] for r in c["rows"].values())
    if x["result"] == "fail":
        assert "exit_not_goto_ptr" in x["why"] and "4a F-B3-23 (O6)" in v["blocking"]
        assert not any(r["c_prime_hits_gt_0"] for r in c["rows"].values())


def test_t_b3_39_opt_levels(v):
    """T-B3-39 / F-B3-24: -O0/-Os builds simplify or give a stable skip reason."""
    assert v["spec_gate"]["4b F-B3-24 (-O0/-Os)"]["result"] == "pass"


def test_t_b3_27_and_28(v):
    t27 = v["spec_gate"]["5a T-B3-27 (v5 regression on the gate binary)"]
    assert t27["result"] == "pass"
    # judged from the evidence files: every row's last file passes
    assert all(r["ok"] and r["evidence"][-1][2] for r in t27["detail"].values())
    assert v["spec_gate"]["5b T-B3-28 (graph/summary round trip, multi_tb rows)"]["result"] == \
        "pass"


@pytest.mark.parametrize("item", ["F-B3-13a", "F-B3-13b", "F-B3-13c", "F-B3-13d", "F-B3-14",
                                  "F-B3-15", "F-B3-17", "F-B3-18", "F-B3-21", "F-B3-22",
                                  "T-B3-26"])
def test_hardening(v, item):
    """§11 hardening: a hardening item that runs and fails blocks the gate."""
    assert v["hardening"][item]["result"] == "pass", v["hardening"][item]


def test_reported_outcomes(v):
    r = v["reported"]
    assert r["T-B3-29 (B3+, Tier 1)"]["result"] == "not_run"
    assert "WP23 not approved" in r["T-B3-29 (B3+, Tier 1)"]["why"]
    assert r["F-B3-19 compute B3 artifacts accepted, V10-equal on held-out"]["result"] == "pass"


def test_gate_verdict_names_only_real_blockers(v):
    assert v["gate"] == ("PASS" if not v["blocking"] else "FAIL")
    for k in v["blocking"]:
        x = v["spec_gate"].get(k) or v["hardening"].get(k)
        assert x and x["result"] != "pass"
