"""WP29: the B4 gate (design §11, plan ``## WP29``, T-B4-1 ... T-B4-13) checked on the
evidence of ``tests/qemu/wp29_gate.py`` (``runs/wp29/verdict.json``; regenerate it with
``wp29_gate.py ... verdict``). Every dynamic item ran on the frozen gate binary.

Items that the gate run found failing are ``xfail(strict=True)`` with the reason, so
the suite records the known result and a later fix shows up as an XPASS failure that
forces this file (and ``reports/B4-gate.md``) to be updated.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
VERDICT = ROOT / "runs/wp29/verdict.json"
GATE_SHA = "56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90"

pytestmark = pytest.mark.real

I1 = "1 T-B4-1/2/2b + mutants (static + O4)"
I2 = "2 F-B4-1..F-B4-9 O1/O2a/O2b per mode, D1/D2 > 0"
I3 = "3 compute V12 + host_abs on every rewritten unit, F-B4-8 V10"
I4 = "4 T-B4-13 F-B4-10 (O6)"
I5 = "5 T-B4-11 ablations + timing (reported)"


@pytest.fixture(scope="module")
def v():
    if not VERDICT.is_file():
        pytest.skip(f"{VERDICT} not present (run tests/qemu/wp29_gate.py)")
    return json.loads(VERDICT.read_text())


def test_gate_binary_is_the_frozen_one(v):
    g = v["gate_binary"]
    assert g["ok"] and g["exe_sha256"] == GATE_SHA and g["tree_clean"]


def test_t_b4_1_model_counts_on_the_gate_base(v):
    t = v["gate"][I1]["t_b4_1"]
    assert t["pass"] and t["m0_d1"] == 0 and t["d2_prefix"] == 678


def test_t_b4_2_every_mutant_rejected_statically(v):
    s = v["gate"][I1]["static"]
    assert s["pass"] and s["total"] == s["rejected"] > 0
    assert s["synthetic"]["all_rejected"]


def test_t_b4_2b_brcond_reload_detected_dynamically(v):
    assert v["gate"][I1]["t_b4_2b_dynamic"] is True


def test_design_93_mutants_detected_except_the_known_gap(v):
    by = v["gate"][I1]["o4_by_kind"]
    gaps = {"flag_store_before_helper"}
    assert all(by[k] == "pass" for k in by if k not in gaps), by


def test_flag_store_before_helper_every_executed_instance_tried(v):
    row = v["gate"][I1]["design_93"]["delete a flag store before a flags-0 helper"][
        "flag_store_before_helper"]
    c = row["o4_coverage"]
    assert c["executed_instances"] == c["executed_instances_tried"] > 0, c


def test_host_abs_in_env_real_instance_on_f_b4_7(v):
    """Design §9.3 "put a host_abs pointer inside env" on the real F-B4-7 UPDATE_MEM
    operand: V12 rejects it (with and without the process env bounds) and O4 detects it;
    gdb shows the pointer inside env at the patched store of the control's run."""
    row = v["gate"][I1]["design_93"]["put a host_abs pointer inside env"]["host_abs_in_env"]
    assert row["o4"] == "pass" and row["static_real"][0] == row["static_real"][1] > 0
    ha = json.loads((ROOT / "runs/wp29/host-abs.json").read_text())
    assert ha["config"] == "b4-7" and ha["verdict"] == "pass"
    assert ha["env_probe_warm"]["ptr_equals_captured_abs"]
    for s in ha["static"]:
        for k in ("without_env_abs", "with_env_abs"):
            assert s[k]["rejected"]
            assert any(r["code"] == "V12" and r["reason"] == "host_abs_aliases_env"
                       for r in s[k]["failed_rows"]), (s["uid"], k)
    for a in ha["attempts"]:
        assert a["verdict"] == "pass" and a["runs_qemu_sha256"] == ["sha256:" + GATE_SHA]
        p = a["env_probe_artifact"]
        assert p["hit"] and p["ptr_inside_env"] and "vfp.zregs" in p["field_written"]


@pytest.mark.xfail(strict=True, reason="known gap of the WP29 run (reports/B4-gate.wp29.md; "
                   "closed by the close-out re-gate, runs/closeout/b4): "
                   "flag_store_before_helper is not detected by O4 in any of the 18 executed "
                   "instances (every one tried); the image has no mrs/msr of APSR/xPSR, so the "
                   "flags-0 helpers in these units do not read NZCV")
def test_every_design_93_mutant_detected_dynamically(v):
    assert v["gate"][I1]["pass"] is True


def test_t_b4_3_to_10_and_12_fixtures(v):
    i = v["gate"][I2]
    assert i["pass"] is True
    for r in i["rows"]:
        assert set(r["runs_qemu_sha256"]) == {"sha256:" + GATE_SHA}, r["config"]
        assert r["runs_on_gate_binary"] is True, r["config"]
        if r["config"] == "b4-9":
            assert r["cpu_abort_gdb_calls"] == 0
        if r["mode"] == "icount":
            assert all(r["d1_d2_executed"].values()), r["config"]


def test_item3_compute_v12_host_abs_and_v10(v):
    i = v["gate"][I3]
    assert i["pass"] is True
    assert i["v12_host_abs"]["rewritten_units"] == i["v12_host_abs"][
        "rewritten_units_named_in_v12"] > 0
    ha = json.loads((ROOT / "runs/wp29/compute/host_abs_ops.json").read_text())
    assert ha["producer"] == "tests/qemu/wp29_gate.py compute-host-abs"
    assert ha["by_arm"]["b4"]["rewritten_units"] == i["v12_host_abs"]["rewritten_units"]
    assert ha["by_arm"]["b4"]["units_not_in_base"] == 0


@pytest.mark.xfail(strict=True, reason="known failure of the WP29 run (reports/B4-gate.wp29.md; "
                   "closed by the close-out re-gate, runs/closeout/b4): under coverage every "
                   "unit's LOG_REG block has an unexplained host store and a backward spin "
                   "branch, so B4 deletes nothing on F-B4-10 (vacuous)")
def test_t_b4_13_f_b4_10_coverage(v):
    assert v["gate"][I4]["pass"] is True


def test_t_b4_13_o6_oracle_itself_runs_on_the_gate(v):
    assert v["gate"][I4]["o6_control_warm_vs_warm"] == "pass"


def test_t_b4_11_ablations_validated(v):
    i = v["gate"][I5]
    assert i["reported_outcome"] is True and i["ablations_validated"] is True
    labels = {a["label"] for a in i["ablations"]}
    assert {"b4", "b4-d1", "b4-d2", "b4-d3", "sfb", "sfb+b4"} <= labels
    rc = json.loads((ROOT / "runs/wp29/recheck-package-compute/compare.json").read_text())
    assert rc["pass"] and {r["label"] for r in rc["rows"]} >= {"b4", "sfb"}


def test_t_b4_11_timing_null_for_b4_and_seed_stability_recorded(v):
    arms = v["gate"][I5]["timing"]["arms"]
    for k in ("b4_vs_warm", "b4-d1_vs_warm", "b4-d2_vs_warm", "b4-d3_vs_warm"):
        assert arms[k]["n_pairs"] == 90 and arms[k]["verdict"] == "no_significant_difference"
        assert arms[k]["ci_bound_signs_seed_stable"] is True
    assert arms["sfb_vs_warm"]["verdict"] == "faster"
    assert len(arms["sfb-b4_vs_sfb"]["ci95_by_seed"]) == 6
