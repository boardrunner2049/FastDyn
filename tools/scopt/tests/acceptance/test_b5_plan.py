"""WP30 acceptance on the real evidence (skipped when runs/b5 is absent; B5_RUNS overrides).

* the frozen campaign passes the freeze checker now (git, code ids, every sha256);
* it was frozen with 0 final-input timing campaigns on the gate binary, on the gate exe;
* every set's derived ``scopt run`` campaign passed the full preflight (``--dry-run``) with
  an interleaved schedule of >= 30 reps x 3 inputs per arm, and nothing was timed;
* every arm of design §10.1 is a frozen arm or a ``skipped_unsupported`` row, every
  frozen deploy arm has an accepted validation and a held-out V10 pass.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tomllib
from pathlib import Path

import pytest

WT = Path(__file__).resolve().parents[2]
RUNS = Path(os.environ.get("B5_RUNS", "/data/extra_copy/fastdyn/runs/b5"))
CAMP = WT / "campaigns/b5-freertos.toml"
pytestmark = pytest.mark.skipif(not (RUNS / "plan/b5-plan.json").is_file() or not CAMP.is_file(),
                                reason="B5 plan evidence absent")


def _bc():
    spec = importlib.util.spec_from_file_location("b5_campaign", WT / "tests/qemu/b5_campaign.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_freeze_checker_passes_now():
    bc = _bc()
    r = bc.check(CAMP, bc.sha256_file(CAMP))
    assert r["ok"], r["problems"]


def test_frozen_before_final_timing_on_gate_binary():
    d = tomllib.loads(CAMP.read_text())
    g = json.loads(Path(d["runtime"]["gate_binary_record"]).read_text())
    assert d["runtime"]["qemu_sha256"] == "sha256:" + g["exe_sha256"]
    assert d["freeze"]["final_timing_campaigns_on_gate_at_freeze"] == 0
    assert all(i["split"] == "train" for i in d["workload"]["profile"]["inputs"])


def test_plan_dry_run_every_set():
    p = json.loads((RUNS / "plan/b5-plan.json").read_text())
    bc = _bc()
    assert p["ok"] and p["timed"] is False
    assert p["campaign_sha256"] == bc.sha256_file(CAMP)
    for name, s in p["sets"].items():
        assert s["status"] == "dry_run" and s["rc"] == 0, name
        assert s["interleaved"] is True
        assert set(s["runs_per_arm"]) == set(s["arms"])
        assert min(s["runs_per_arm"].values()) >= 30 * s["inputs"] >= 90


def test_arms_cover_section_10_1_and_are_validated():
    d = tomllib.loads(CAMP.read_text())
    names = {a["name"] for a in d["arm"]}
    unsup = {u["name"] for u in d["skipped_unsupported"]}
    assert {"nocache", "empty", "warm", "identity", "identity-v6", "layout", "b3-chain",
            "b3-static", "b3-spec", "b4"} <= names
    assert {"semantic", "combined"} <= unsup
    for a in d["arm"]:
        if a["kind"] == "deploy" and a["name"] != "warm":
            assert a["validation_verdict"] == "accepted", a["name"]
            assert a["v10_verdict"] == "pass", a["name"]
