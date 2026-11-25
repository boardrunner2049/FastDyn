from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]
_PATH = Path(__file__).resolve().parents[2] / "experiments/hot-loop-inline/compare_rtos.py"


def _load():
    try:
        import fastdyn.compiled_launch  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(_REPO / "src"))
    spec = importlib.util.spec_from_file_location("compare_rtos", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # pragma: no cover - environment without FastDyn
        pytest.skip(f"compare_rtos dependencies unavailable: {exc}")
    return module


_M = _load()


def _plan(comparisons, **extra):
    arms = {name: {"launch": f"/x/{name}.json", "cache": f"/x/{name}.sc", "sealed": True}
            for name in ("sealed", "pgo", "looper", "v2")}
    return {"arms": arms, "comparisons": comparisons, "cases": {"a": {}, "b": {}},
            "blocks": 8, "seed": 11, **extra}


def test_resolve_aa_picks_most_frequent_reference():
    plan = _plan([["v2", "sealed"], ["pgo", "sealed"], ["v2", "looper"]])
    assert _M.resolve_aa(plan) == ("v2-aa", "v2")
    plan = _plan([["sealed", "pgo"], ["v2", "looper"]])
    assert _M.resolve_aa(plan) == ("sealed-aa", "sealed")  # tie: first occurrence


def test_resolve_aa_options():
    comparisons = [["sealed", "pgo"]]
    assert _M.resolve_aa(_plan(comparisons, aa_control=False)) is None
    assert _M.resolve_aa(_plan(comparisons), disabled=True) is None
    assert _M.resolve_aa(_plan(comparisons, aa_control="pgo")) == ("pgo-aa", "pgo")
    assert _M.resolve_aa(_plan(comparisons, aa_name="twin")) == ("twin", "sealed")
    with pytest.raises(ValueError):
        _M.resolve_aa(_plan(comparisons, aa_name="pgo"))
    with pytest.raises(ValueError):
        _M.resolve_aa(_plan(comparisons, aa_control="missing"))


def test_resolve_aa_reuses_a_written_plan():
    plan = _plan([["sealed", "pgo"]])
    plan["arms"]["sealed-aa"] = dict(plan["arms"]["sealed"])
    plan["aa_control_resolved"] = {"arm": "sealed-aa", "reference": "sealed"}
    assert _M.resolve_aa(plan) == ("sealed-aa", "sealed")
    plan["arms"]["sealed-aa"]["cache"] = "/other.sc"
    with pytest.raises(ValueError):
        _M.resolve_aa(plan)


def _times(plan, scale):
    times = {}
    for b in range(plan["blocks"]):
        for arm in plan["arms"]:
            for i, case in enumerate(plan["cases"]):
                times[b, arm, case] = int((1000 + 10 * i + (b % 3)) * scale.get(arm, 1.0))
    return times


def test_summarize_keeps_legacy_keys_and_adds_protocol_fields():
    plan = _plan([["sealed", "pgo"], ["sealed", "v2"]])
    plan["arms"]["sealed-aa"] = dict(plan["arms"]["sealed"])
    times = _times(plan, {"pgo": 1.05, "v2": 1.001})
    for (b, arm, case), ns in times.items():
        if arm == "sealed-aa":  # a +/-2% harness floor
            times[b, arm, case] = int(ns * (1.02 if b % 2 else 0.98))
    report = _M.summarize(times, plan, ("sealed-aa", "sealed"))
    pgo = report["paired"]["sealed/pgo"]
    assert set(pgo) >= {"median", "interval", "blocks", "n_blocks", "blocks_above_one",
                        "sign_test_p", "underpowered", "within_noise", "verdict"}
    assert len(pgo["blocks"]) == 8 and pgo["median"] < 1
    assert pgo["verdict"] == "below_floor"
    assert report["paired"]["sealed/v2"]["verdict"] == "within_noise"
    floor = report["noise_floor"]
    assert floor["arm"] == "sealed-aa" and floor["band"][0] < 0.99 < 1.01 < floor["band"][1]
    assert report["primary_statistic"] == "paired_block_ratio_median"
    assert set(report["case_ms"]) == {"a", "b"} and "sealed-aa" in report["mean_median_ms"]
    # Deterministic for a given plan seed.
    assert _M.summarize(times, plan, ("sealed-aa", "sealed")) == report


def test_summarize_marks_six_blocks_underpowered():
    plan = _plan([["sealed", "pgo"]], blocks=6)
    report = _M.summarize(_times(plan, {"pgo": 1.05}), plan)
    assert report["noise_floor"] is None
    assert report["paired"]["sealed/pgo"]["verdict"] == "underpowered"


def test_check_aa_identity():
    ident = {"launch": {"path": "/l", "sha256": "1"}, "qemu": {"path": "/q", "sha256": "2"},
             "cache_copy": {"path": "/a/cache.sc", "sha256": "3"}}
    good = {"sealed": ident, "sealed-aa": dict(ident, cache_copy={"path": "/b/cache.sc",
                                                                  "sha256": "3"})}
    _M.check_aa_identity(good, ("sealed-aa", "sealed"))
    for bad in ({"path": "/a/cache.sc", "sha256": "3"}, {"path": "/b/cache.sc", "sha256": "4"}):
        with pytest.raises(RuntimeError):
            _M.check_aa_identity({"sealed": ident, "sealed-aa": dict(ident, cache_copy=bad)},
                                 ("sealed-aa", "sealed"))
