"""WP21 integration fix: ``specialize-returns`` admits variants against the §3.4 growth
budget in rank order (``scopt.passes.specialize_returns._fit_growth``).

Before the fix the pass proposed every variant that passed the per-C/per-artifact
budgets and the manager refused the whole proposal when its total growth exceeded
``max_code_growth_bytes``: on WP24 phase F-B3-6 (two return units, one continuation,
two variants of 790 bytes against a 619-byte budget) the pass ended with no variant at
all. Now the lower-ranked variants that do not fit are dropped (``growth_budget``,
counted) and the rest are proposed; the manager's check is unchanged and still a
backstop.

Inputs (read-only, tests skip when absent): the F-B3-1 base snapshot of WP20's
integration stage (as ``test_wp21_b3_passes``) and the F-B3-6 base snapshot captured on
the gate binary by WP21's integration driver (``runs/wp21-int/base/icount/6``).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from scopt.manager_b3 import B3Env, B3State, stub_bytes_estimate
from scopt.passes.base import Outcome
from scopt.passes.specialize_returns import _fit_growth, growth_budget

ROOT = Path(os.environ.get("FASTDYN_ROOT", "/data/extra_copy/fastdyn"))
FX1 = ROOT / "runs/wp20-int/fx/icount/1"
FX1_SNAP = FX1 / "store/snapshots/7b2d553cc9c895236c5ed2f037ad120367284334cb186769478a620f4499e959"
FX6 = ROOT / "runs/wp21-int/base/icount/6"


# ---------------------------------------------------------------------------
# _fit_growth on fakes
# ---------------------------------------------------------------------------

def _unit(size):
    return SimpleNamespace(tb_offset=0, end_offset=size)


def _plan(uid, tail, cuid, weight):
    return {"row": SimpleNamespace(uid=uid, tail_off=tail), "cu": SimpleNamespace(
        uid=cuid, tb_offset=0, end_offset=_UNITS[cuid].end_offset), "weight": weight}


_UNITS = {"C1": _unit(400), "C2": _unit(300), "C3": _unit(100)}
STUB1 = stub_bytes_estimate(1)  # one SPEC slot on an exit


def _fit(plans, budget):
    st = B3State()
    b3 = SimpleNamespace(store=None)
    return _fit_growth(plans, st, _UNITS, b3, {"max_code_growth_bytes": budget})


def test_fit_growth_rank_order():
    plans = [_plan("R1", 10, "C1", 900), _plan("R2", 10, "C2", 800), _plan("R3", 10, "C3", 1)]
    # everything fits
    kept, dropped = _fit(plans, 10_000)
    assert [d["cu"].uid for d in kept] == ["C1", "C2", "C3"] and dropped == 0
    # only the first fits; the third (smaller) still fits after the second is dropped
    b = 400 + STUB1 + 100 + STUB1
    kept, dropped = _fit(plans, b)
    assert [d["cu"].uid for d in kept] == ["C1", "C3"] and dropped == 1
    # nothing fits
    kept, dropped = _fit(plans, 50)
    assert kept == [] and dropped == 3


def test_fit_growth_two_targets_on_one_exit_share_a_stub():
    plans = [_plan("R1", 10, "C1", 900), _plan("R1", 10, "C3", 800)]
    need = 400 + 100 + stub_bytes_estimate(2)
    kept, dropped = _fit(plans, need)
    assert len(kept) == 2 and dropped == 0
    kept, dropped = _fit(plans, need - 1)
    assert len(kept) == 1 and dropped == 1


def test_fit_growth_without_budget_keeps_all():
    plans = [_plan("R1", 10, "C1", 900)]
    st = B3State()
    # no hot set (no profile) -> no budget, as the manager
    b3 = SimpleNamespace(store=SimpleNamespace(get=lambda *a: (_ for _ in ()).throw(KeyError())))
    assert growth_budget({}, b3, _UNITS) is None
    assert _fit_growth(plans, st, _UNITS, b3, {}) == (plans, 0)


def test_fit_growth_counts_existing_spec_exits():
    from scopt.manager_b3 import ExitPlan, SlotPlan  # noqa: F401 - shapes only

    ex = SimpleNamespace(slots=(SimpleNamespace(kind="SPEC"),))
    st = SimpleNamespace(exits=MappingProxyType({("R0", 10): ex}),
                         variants=MappingProxyType({"v": SimpleNamespace(src_uid="C3")}),
                         growth_bytes=lambda units: 100 + STUB1)
    plans = [_plan("R1", 10, "C2", 900)]
    b3 = SimpleNamespace(store=None)
    need = 100 + STUB1 + 300 + STUB1
    assert _fit_growth(plans, st, _UNITS, b3, {"max_code_growth_bytes": need})[1] == 0
    assert _fit_growth(plans, st, _UNITS, b3, {"max_code_growth_bytes": need - 1})[1] == 1


# ---------------------------------------------------------------------------
# on real snapshots
# ---------------------------------------------------------------------------

def _open(snapdir: Path, prep: Path, profile: Path):
    from scopt.analysis.b3_inputs import resolve_for_snapshot
    from scopt.formats.profile_v1 import ProfileView, read_profile
    from scopt.importer.import_cache import open_snapshot

    s = open_snapshot(snapdir)
    resolve_for_snapshot(s, prep=str(prep))
    return s, ProfileView.open(read_profile(profile), s)


def _run(s, pv, params):
    from scopt.manager import PassManager

    pm = PassManager(s, "b3", [pv], params, b3=B3Env(), only="specialize-returns")
    return pm, pm.execute()


def test_fb3_1_growth_budget_exact():
    if not (FX1_SNAP / "snapshot.json").is_file():
        pytest.skip(f"{FX1_SNAP} not present")
    s, pv = _open(FX1_SNAP, FX1 / "prep", FX1 / "prof/merged.profile.json")
    c = next(u for u in s.units() if u.vaddrs and u.vaddrs[0] == 0x2F4)
    need = c.end_offset - c.tb_offset + STUB1
    pm, cand = _run(s, pv, {"specialize-returns": {"max_code_growth_bytes": need}})
    p = cand.outcome("specialize-returns")
    assert p.outcome is Outcome.CANDIDATE and p.stats["variants"] == 1
    assert len(cand.b3_state.variants) == 1
    pm, cand = _run(s, pv, {"specialize-returns": {"max_code_growth_bytes": need - 1}})
    p = cand.outcome("specialize-returns")
    # dropped by the pass itself (counted), never a manager refusal of the proposal
    assert p.outcome is Outcome.SKIPPED_UNSUPPORTED
    assert p.stats.get("refused:growth_budget") == 1 and not cand.b3_state.variants
    assert "> " not in (p.detail or "")  # the manager's "g > budget bytes" text


def test_fb3_6_keeps_the_variant_that_fits():
    snaps = sorted((FX6 / "store/snapshots").glob("*")) if FX6.is_dir() else []
    if len(snaps) != 1 or not (FX6 / "prof/merged.profile.json").is_file():
        pytest.skip(f"{FX6} not present")
    s, pv = _open(snaps[0], FX6 / "prep", FX6 / "prof/merged.profile.json")
    _pm, cand = _run(s, pv, None)
    p = cand.outcome("specialize-returns")
    assert p.outcome is Outcome.CANDIDATE
    assert p.stats["variants"] == 1 and p.stats["refused:growth_budget"] == 1
    assert len(cand.b3_state.variants) == 1
    # with room for both (a params change only) both return units get their variant
    _pm, cand = _run(s, pv, {"specialize-returns": {"growth_fraction": 1.0}})
    p = cand.outcome("specialize-returns")
    assert p.stats["variants"] == 2 and len(cand.b3_state.variants) == 2
