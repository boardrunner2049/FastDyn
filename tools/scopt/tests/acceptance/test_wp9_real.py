"""WP9 on real data: the pass framework over WP7 ``CacheSnapshot`` objects.

WP9's acceptance (IMPLEMENTATION-PLAN.md) is pure Python on the fake snapshot
(``tests/unit/test_pass_framework.py``, ``tests/unit/test_analysis_store.py``).
These tests check that the same machinery works unchanged on snapshots the
WP7 importer builds from real captures, with the real analyses
(``refs``, ``recovery``, ``layout``, ``cfg.known_edges``, ``profile.counts``).

Inputs (outside the repo; skipped when absent), the same as
``tests/acceptance/test_wp7_real.py``:

* ``runs/wp7/prep-minimal`` + ``minimal-bundle.json`` +
  ``minimal-runtime-identity.json`` (163 TBs, ``configs/b0-sc.toml``);
* ``runs/wp3/freertos-cap1`` (FreeRTOS MPS2, > 1000 TBs).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from scopt.analysis.api import AnalysisSpec, ScopeRef
from scopt.analysis.refs import recovery_value, refs_value
from scopt.analysis.registry import default_registry
from scopt.analysis.store import AnalysisStore
from scopt.formats.profile_v1 import ProfileView
from scopt.importer.import_cache import import_snapshot
from scopt.manager import PassManager, PassRegistry, default_pass_registry, load_pipeline
from scopt.model.snapshot import WorkingSet
from scopt.model.view import WorkingSetView
from scopt.passes.base import EditKind, Outcome, PassDescriptor, PassResult
from scopt.passes.identity import Identity
from scopt.passes.move_units import MoveUnits
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
W7 = ROOT / "runs/wp7"
PREP = W7 / "prep-minimal"
BUNDLE = W7 / "minimal-bundle.json"
RI = W7 / "minimal-runtime-identity.json"
FRT_SC = ROOT / "runs/wp3/freertos-cap1/cache/freertos.sc"
FRT_SCM = ROOT / "runs/wp3/freertos-cap1.scm"

pytestmark = [pytest.mark.real, pytest.mark.slow]


def _need(*paths: Path) -> None:
    for p in paths:
        if not p.exists():
            pytest.skip(f"{p} not present")


@pytest.fixture(scope="module")
def minimal(tmp_path_factory):
    _need(PREP / "capture.scm", BUNDLE, RI)
    store = tmp_path_factory.mktemp("wp9store")
    return import_snapshot(str(PREP), bundle=str(BUNDLE), runtime_identity=str(RI),
                           store=str(store)).snapshot


@pytest.fixture(scope="module")
def freertos():
    _need(FRT_SC, FRT_SCM)
    return import_snapshot(sc=str(FRT_SC), scms=[str(FRT_SCM)]).snapshot


def _pass(name, run, **desc):
    return type(f"P_{name.replace('-', '_')}", (), {
        "descriptor": PassDescriptor(name=name, version=1, **desc),
        "run": lambda self, unit, ctx, edits: run(unit, ctx, edits)})


def _pipe(*entries, allowed=("ANNOTATE", "PIN", "LAYOUT_ORDER", "PLACE")):
    rows = [{"name": e[0], "version": 1, "params": e[1]} if isinstance(e, tuple)
            else {"name": e, "version": 1} for e in entries]
    return {"name": "wp9-real", "allowed_capabilities": list(allowed), "pass": rows}


def _keys(store):
    return {(r.name, r.scope_id) for r in store.cached()}


# ---------------------------------------------------------------------------

def test_identity_pipeline_on_real_snapshot(minimal):
    assert minimal.capture_level == 1 and len(minimal.units()) == 163
    cand = PassManager(minimal, "identity").execute()
    (rec,) = cand.passes
    assert (rec.outcome, rec.reason, rec.n_edits) == (Outcome.NO_CHANGE, "identity", 0)
    assert cand.edits == () and cand.edit_log == () and cand.edit_log_jsonl() == b""
    assert not cand.has_layout_changes
    assert cand.input_snapshot_id == minimal.snapshot_id
    assert dict(cand.view.layout()) == dict(minimal.layout())
    assert cand.view.units() == minimal.units()  # same TBUnit objects, slot order
    assert [(r.subject, r.id, r.verdict, r.reason) for r in cand.ledger] == [
        ("pass", "identity@1", "no_change", "identity")]
    # deterministic candidate id over two runs
    assert PassManager(minimal, "identity").execute().candidate_id == cand.candidate_id


def test_b2_layout_pipeline_without_profile_changes_nothing(minimal):
    """Before WP11 the passes are unregistered; after it, a profile-requiring
    pass with no profile is SKIPPED_UNKNOWN. Either way: no edits, a ledger row
    per pass, nothing FAILED."""
    pipe = load_pipeline("b2-layout")
    cand = PassManager(minimal, pipe).execute()
    assert len(cand.passes) == len(cand.ledger) == len(pipe.passes)
    for rec in cand.passes:
        assert rec.outcome in (Outcome.SKIPPED_UNSUPPORTED, Outcome.SKIPPED_UNKNOWN,
                               Outcome.NO_CHANGE), rec
        if rec.outcome is Outcome.SKIPPED_UNSUPPORTED:
            assert rec.reason == "pass_not_registered"
    assert cand.edits == () and not cand.has_layout_changes
    # a not-collected ProfileView has no phases: a phase-requiring pass is
    # SKIPPED_UNKNOWN missing_profile
    need = _pass("needs-steady", lambda s, c, e: PassResult(Outcome.NO_CHANGE, "x"),
                 requires_profile_phase="steady")
    nc = ProfileView.not_collected(minimal)
    c2 = PassManager(minimal, _pipe("needs-steady"), profiles=[nc],
                     passes=PassRegistry([need])).execute()
    assert (c2.passes[0].outcome, c2.passes[0].reason) == (Outcome.SKIPPED_UNKNOWN,
                                                           "missing_profile")


def test_real_analyses_through_the_store(minimal):
    st = AnalysisStore(WorkingSetView(WorkingSet.initial(minimal)), default_registry())
    S = ScopeRef("snapshot", minimal.snapshot_id)
    lay = st.get("layout", S).value
    assert lay["overlaps"] == [] and lay["pinned"] == [] and lay["relocatable"] == 163
    assert lay["max_end"] == minimal.max_end and lay["slack"] > 0
    edges = st.get("cfg.known_edges", S).value
    assert edges["edges"]
    for u in minimal.units():
        r = st.get("refs", ScopeRef("unit", u.uid))
        assert r.value == refs_value(u) and r.value["status"] == "ok_relocatable"
        assert st.get("recovery", ScopeRef("unit", u.uid)).value == recovery_value(u)
        # every unit-scope result depends on exactly that unit's content facet
        assert r.dep_units == frozenset({(u.uid, u.content_hash)})
    # second get is a cache hit (same object)
    assert st.get("layout", S) is st.get("layout", S)


def test_invalidation_on_real_snapshot_drops_exactly_the_dependents(minimal):
    """ANNOTATE on unit a, then PLACE of unit b: only ``annotations``, then
    layout and an analysis that requires layout, are dropped; refs/recovery
    (content facet) and cfg.known_edges survive."""
    uids = [u.uid for u in minimal.units()]
    a, b = uids[3], uids[7]

    def slack(ctx, s):
        return ctx.analysis("layout", s).value["slack"]

    reg = default_registry()
    reg.register(AnalysisSpec("test.slack", 1, "snapshot", (("layout", 1),), frozenset(),
                              slack, int))
    st = AnalysisStore(WorkingSetView(WorkingSet.initial(minimal)), reg)
    S = ScopeRef("snapshot", minimal.snapshot_id)
    for name in ("layout", "cfg.known_edges", "test.slack"):
        st.get(name, S)
    for u in uids:
        st.get("refs", ScopeRef("unit", u))
        st.get("recovery", ScopeRef("unit", u))
    st.get("annotations", S)  # snapshot scope: reads every unit's annotation facet
    before = _keys(st)
    assert len(before) == 4 + 2 * len(uids)

    ann = _pass("ann", lambda s, c, e: (e.annotate(a, "hotness", "hot"),
                                        PassResult(Outcome.CANDIDATE, "annotated",
                                                   affected=(a,)))[1],
                capabilities=frozenset({EditKind.ANNOTATE}))
    lay = st.get("layout", S).value
    target = (lay["max_end"] + 63) // 64 * 64 + 64 * 4  # free, 64-aligned, inside the bound
    assert target + (minimal.unit(b).end_offset - minimal.unit(b).tb_offset) < lay["bound"]
    cand = PassManager(minimal, _pipe("ann", ("move-units", {"moves": {b: target}})),
                       passes=PassRegistry([ann, MoveUnits]), analyses=reg,
                       store=st).execute()
    r_ann, r_mv = cand.passes
    assert (r_ann.outcome, r_mv.outcome) == (Outcome.CANDIDATE, Outcome.CANDIDATE)
    assert r_ann.invalidated == 1 and r_mv.invalidated == 2
    sid = minimal.snapshot_id
    assert before - _keys(st) == {("annotations", sid), ("layout", sid), ("test.slack", sid)}

    ws = cand.working_set
    assert ws.generation == 2 and ws.layout()[b] == target
    assert ws.annotations(a)["hotness"] == "hot"
    assert [e.kind for e in cand.edits] == [EditKind.ANNOTATE, EditKind.PLACE]
    assert dict(minimal.layout())[b] == minimal.unit(b).tb_offset  # base untouched
    # recompute sees the move
    new = st.get("layout", S).value
    assert new["overlaps"] == [] and [b, target, target + minimal.unit(b).end_offset
                                      - minimal.unit(b).tb_offset] in new["intervals"]
    assert st.get("test.slack", S).value == new["slack"]
    assert st.get("annotations", S).value[a]["hotness"] == "hot"


def test_real_stale_edit_and_capability_errors(minimal):
    uids = [u.uid for u in minimal.units()]
    u0 = minimal.unit(uids[0])

    def stale(s, c, e):
        e.annotate(u0.uid, "k", 1, reads=[(u0.uid, "sha256:" + "0" * 64)])
        return PassResult(Outcome.CANDIDATE, "x")

    def outside(s, c, e):
        e.pin(u0.uid, "nope")
        return PassResult(Outcome.CANDIDATE, "x")

    def b3(s, c, e):
        e.clone(u0.uid)
        return PassResult(Outcome.CANDIDATE, "x")

    P = [_pass("stale", stale, capabilities=frozenset({EditKind.ANNOTATE})),
         _pass("outside", outside, capabilities=frozenset({EditKind.ANNOTATE})),
         _pass("b3", b3, capabilities=frozenset({EditKind.ANNOTATE}))]
    cand = PassManager(minimal, _pipe("stale", "outside", "b3"),
                       passes=PassRegistry(P)).execute()
    got = [(r.outcome, r.reason) for r in cand.passes]
    assert got == [(Outcome.FAILED, "stale_edit"), (Outcome.FAILED, "edit_capability"),
                   (Outcome.FAILED, "capability_not_enabled")]
    assert cand.edits == () and len(cand.ledger) == 3


def test_freertos_scale(freertos):
    """Identity pipeline and every real analysis on a > 1000-unit snapshot."""
    n = len(freertos.units())
    assert n > 1000
    t0 = time.monotonic()
    cand = PassManager(freertos, "identity").execute()
    assert cand.passes[0].outcome is Outcome.NO_CHANGE and cand.edits == ()
    st = AnalysisStore(WorkingSetView(WorkingSet.initial(freertos)), default_registry())
    S = ScopeRef("snapshot", freertos.snapshot_id)
    assert st.get("layout", S).value["overlaps"] == []
    st.get("cfg.known_edges", S)
    for u in freertos.units():
        st.get("refs", ScopeRef("unit", u.uid))
    assert len(st.cached()) == 2 + n
    # one PLACE invalidates layout only
    uid = freertos.units()[0].uid
    ws1, changed = WorkingSet.initial(freertos).apply(
        [type("E", (), {"kind": EditKind.PLACE,
                        "args": {"uid": uid, "tb_offset": freertos.units()[0].tb_offset}})()],
        owner="t")
    st.rebase(WorkingSetView(ws1))
    st.invalidate(changed, frozenset())
    assert len(st.cached()) >= 1 + n
    assert time.monotonic() - t0 < 120, "framework too slow on a real-size snapshot"
    assert default_pass_registry().versions("identity") == (1,)
