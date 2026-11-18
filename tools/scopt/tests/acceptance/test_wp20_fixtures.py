"""WP20 integration acceptance (plan §WP20, WP19 + WP24) on captured fixture snapshots.

The snapshots come from ``tests/qemu/wp20_fixture_snapshots.py`` (QEMU, untimed);
``WP20_FX_ROOT`` overrides the default ``runs/wp20-int/fx``. Skipped when absent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.acceptance.wp20_fixtures import REQUIRED, evaluate
from scopt.paths import fastdyn_root

FX_ROOT = Path(os.environ.get("WP20_FX_ROOT", str(fastdyn_root() / "runs/wp20-int/fx")))

pytestmark = [pytest.mark.real, pytest.mark.slow]

_V: dict = {}


def verdict():
    if not any(FX_ROOT.glob("*/*/snap.json")):
        pytest.skip(f"no fixture snapshots under {FX_ROOT}")
    if "v" not in _V:
        _V["v"] = evaluate(FX_ROOT, configs=REQUIRED)
    return _V["v"]


def test_required_snapshots_present():
    v = verdict()
    assert not v["missing"], v["missing"]
    got = {(r["mode"], r["config"]) for r in v["per_snapshot"]}
    for c in REQUIRED:
        assert any(cfg == c for _m, cfg in got), c


def test_patch_fact_certified():
    """F-B3-1/1p/2/3/3b/4/8: r0 == 0, source = patch, certified by WP19."""
    v = verdict()
    assert v["criteria"]["patch_fact"] and v["patch_fact_pass"], {
        k: x for k, x in v["criteria"]["patch_fact"].items() if not x["pass"]}


def test_f5_gets_no_patch_fact():
    v = verdict()
    assert v["criteria"]["f5"] and v["f5_pass"], v["criteria"]["f5"]


def test_helper_kills_fact():
    v = verdict()
    assert v["criteria"]["killed"] and v["killed_pass"], v["criteria"]["killed"]


def test_suffix_unit_refused():
    v = verdict()
    assert v["criteria"]["suffix"] and v["suffix_pass"], v["criteria"]["suffix"]
    # non-vacuous in icount: the budget-boundary suffix units are in the snapshot
    ic = [x for k, x in v["criteria"]["suffix"].items() if k.startswith("icount/")]
    assert ic and all(x["non_vacuous"] for x in ic)


def test_proposer_and_certifier_agree():
    v = verdict()
    a = v["agree"]
    assert a["pass"] and a["certifier_fact_derivation_mismatch"] == 0 and \
        a["xcheck_mismatch"] == 0, a
    assert a["facts_certified"] > 0
