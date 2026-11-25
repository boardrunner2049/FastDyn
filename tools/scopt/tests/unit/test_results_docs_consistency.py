"""Published timing numbers must recompute from their stored per-block ratios."""

from __future__ import annotations

import json
import random
import statistics
from pathlib import Path

import pytest

from scopt.run.paired_stats import bootstrap_median_ci

_ROOT = Path(__file__).parents[2]
_WHOPR = json.loads((_ROOT / "docs/sealed-whopr-7-current-results.json").read_text())
_SWEET = json.loads(
    (_ROOT / "experiments/helper-inline-sweetspot/results.json").read_text())
_SEED = _WHOPR["seed"] ^ 0x37


def _entries():
    yield "main", _WHOPR["paired_speedups_baseline_over_whopr7"], _WHOPR
    helper = _WHOPR["helper_only_ablation"]
    yield "helper_only", helper["paired_speedups_baseline_over_helper_only"], helper
    for name, control in _WHOPR["bottleneck_controls"].items():
        if "paired_speedups" in control:
            yield name, control["paired_speedups"], control


@pytest.mark.parametrize("name,ratios,entry", list(_entries()),
                         ids=[e[0] for e in _entries()])
def test_whopr_intervals_recompute(name, ratios, entry):
    assert len(ratios) == _WHOPR["pairs"]
    assert bootstrap_median_ci(ratios, random.Random(_SEED)) == entry["bootstrap_95"]
    median = entry.get("median_paired_speedup", entry.get("median_control_over_original"))
    assert median == statistics.median(ratios)


def test_new_controls_are_published():
    controls = _WHOPR["bottleneck_controls"]
    assert round(controls["same_layout_lowering_control"]["median_paired_speedup"], 6) == 0.973415
    assert round(controls["executed_clone_control"]["median_paired_speedup"], 6) == 0.863282
    assert controls["shared_copied_graphs_vs_original_baseline"]["label"] == "layout_plus_lowering"
    caveat = controls["decomposition_caveat"]
    products = caveat["products"]
    shared = controls["shared_copied_graphs_vs_private_copied_graphs"]["median_paired_speedup"]
    layout = controls["shared_copied_graphs_vs_original_baseline"]["median_paired_speedup"]
    footprint = controls["same_large_file_original_records"]["median_paired_speedup"]
    clone = controls["executed_clone_control"]["median_paired_speedup"]
    assert round(shared * layout, 4) == products["placement_x_layout_lowering"]
    assert round(footprint * clone, 4) == products["footprint_x_executed_clone"]
    assert caveat["non_additive"] is True


def test_sweetspot_exhaustive_row_uses_the_valid_campaign():
    row = next(r for r in _SWEET["results"] if r["policy"] == "exhaustive_graph")
    helper = _WHOPR["helper_only_ablation"]
    assert row["paired_speedup"] == round(helper["median_paired_speedup"], 6)
    assert row["ci95"] == [round(x, 6) for x in helper["bootstrap_95"]]
    assert row["pairs"] == _WHOPR["pairs"]
    assert row["superseded"]["paired_speedup"] == 0.720162
