"""WP9: AnalysisStore dependency tracking, invalidation, facts, persistence."""

from __future__ import annotations

import sqlite3
from types import MappingProxyType

import pytest

from scopt.analysis.api import AnalysisResult, AnalysisSpec, ScopeRef
from scopt.analysis.registry import ANNOTATIONS, AnalysisRegistry, default_registry
from scopt.analysis.store import (
    AnalysisCycle, AnalysisSchemaError, AnalysisStore, MissingAnalysis, UndeclaredInput,
    value_digest,
)
from scopt.model.snapshot import WorkingSet
from scopt.model.view import WorkingSetView
from scopt.passes.base import EditKind, StaleFact
from tests.synth.fake_snapshot import fake_snapshot


class _E:
    """Minimal edit for WorkingSet.apply."""

    def __init__(self, kind, **args):
        self.kind = kind
        self.args = args


@pytest.fixture(scope="module")
def snap():
    return fake_snapshot(4)


def U(snap, i):
    return ScopeRef("unit", snap.uids()[i])


def chain_registry(calls):
    """A(u)<-B(u)<-C(snap: B(u0)); D(u)<-E(snap: D(u1)); F(u, code)<-G(u)."""

    def a(ctx, s):
        calls.append(("A", s.id))
        return ctx.layout(s.id)

    def b(ctx, s):
        calls.append(("B", s.id))
        return ctx.analysis("A", s).value + 1

    def c(ctx, s):
        calls.append(("C", s.id))
        return ctx.analysis("B", ScopeRef("unit", ctx.unit_ids()[0])).value * 2

    def d(ctx, s):
        calls.append(("D", s.id))
        return ctx.layout(s.id) // 64

    def e(ctx, s):
        calls.append(("E", s.id))
        return ctx.analysis("D", ScopeRef("unit", ctx.unit_ids()[1])).value

    def f(ctx, s):
        calls.append(("F", s.id))
        return ctx.unit(s.id).tc_size

    def g(ctx, s):
        calls.append(("G", s.id))
        return -ctx.analysis("F", s).value

    def parity(ctx, s):
        calls.append(("P", s.id))
        return ctx.layout(s.id) % 128 == 0

    def q(ctx, s):
        calls.append(("Q", s.id))
        return not ctx.analysis("P", s).value

    L = frozenset({"layout"})
    return AnalysisRegistry([
        AnalysisSpec("A", 1, "unit", (), L, a, int),
        AnalysisSpec("B", 1, "unit", (("A", 1),), frozenset(), b, int),
        AnalysisSpec("C", 1, "snapshot", (("B", 1),), frozenset(), c, int),
        AnalysisSpec("D", 1, "unit", (), L, d, int),
        AnalysisSpec("E", 1, "snapshot", (("D", 1),), frozenset(), e, int),
        AnalysisSpec("F", 1, "unit", (), frozenset({"code"}), f, int),
        AnalysisSpec("G", 1, "unit", (("F", 1),), frozenset(), g, int),
        AnalysisSpec("P", 1, "unit", (), L, parity, bool),
        AnalysisSpec("Q", 1, "unit", (("P", 1),), frozenset(), q, bool),
    ])


def populate(store, snap):
    S = ScopeRef("snapshot", snap.snapshot_id)
    store.get("C", S)
    store.get("E", S)
    store.get("A", U(snap, 1))
    store.get("G", U(snap, 0))
    return {(r.name, r.scope_id) for r in store.cached()}


def moved(snap, i, delta):
    ws = WorkingSet.initial(snap)
    uid = snap.uids()[i]
    return ws.apply([_E(EditKind.PLACE, uid=uid, tb_offset=snap.units()[i].tb_offset + delta)],
                    owner="t")


def test_invalidation_drops_exactly_the_transitive_dependents(snap):
    calls = []
    st = AnalysisStore(snap, chain_registry(calls))
    before = populate(st, snap)
    u0, u1 = snap.uids()[0], snap.uids()[1]
    sid = snap.snapshot_id
    assert before == {("A", u0), ("B", u0), ("C", sid), ("D", u1), ("E", sid), ("A", u1),
                      ("F", u0), ("G", u0)}
    ws1, changed = moved(snap, 0, 0x1000)
    assert changed == {u0: frozenset({"layout"})}
    st.rebase(WorkingSetView(ws1))
    n = st.invalidate(changed, frozenset())
    after = {(r.name, r.scope_id) for r in st.cached()}
    assert before - after == {("A", u0), ("B", u0), ("C", sid)}
    assert n == 3
    # recompute gives the new value
    calls.clear()
    assert st.get("C", ScopeRef("snapshot", sid)).value == (snap.units()[0].tb_offset
                                                             + 0x1000 + 1) * 2
    assert sorted(c[0] for c in calls) == ["A", "B", "C"]


def test_invalidation_code_facet_and_preserved(snap):
    calls = []
    st = AnalysisStore(snap, chain_registry(calls))
    before = populate(st, snap)
    u0 = snap.uids()[0]
    n = st.invalidate({u0: frozenset({"code"})}, frozenset())
    assert before - {(r.name, r.scope_id) for r in st.cached()} == {("F", u0), ("G", u0)}
    assert n == 2

    st2 = AnalysisStore(snap, chain_registry([]))
    populate(st2, snap)
    ws1, changed = moved(snap, 0, 0x1000)
    st2.rebase(WorkingSetView(ws1))
    assert st2.invalidate(changed, frozenset({"*"})) == 0
    st3 = AnalysisStore(snap, chain_registry([]))
    b3 = populate(st3, snap)
    st3.rebase(WorkingSetView(ws1))
    # B preserved: only A(u0) is dropped (C depends on B, which is kept)
    assert st3.invalidate(changed, frozenset({"B"})) == 1
    assert b3 - {(r.name, r.scope_id) for r in st3.cached()} == {("A", u0)}
    # lazily, get() still notices that B's input A changed value and recomputes
    assert st3.get("B", U(snap, 0)).value == snap.units()[0].tb_offset + 0x1000 + 1


def test_unknown_facet_rejected(snap):
    st = AnalysisStore(snap, chain_registry([]))
    with pytest.raises(ValueError):
        st.invalidate({snap.uids()[0]: frozenset({"bytes"})}, frozenset())


def test_cache_hits_and_lazy_revalidation_with_early_cutoff(snap):
    calls = []
    st = AnalysisStore(snap, chain_registry(calls))
    st.get("Q", U(snap, 0))
    st.get("Q", U(snap, 0))
    assert sorted(c[0] for c in calls) == ["P", "Q"] and st.stats["hits"] >= 1
    # move by 256: parity unchanged -> P recomputed with the same hash -> Q stays valid
    ws1, _ = moved(snap, 0, 256)
    st.rebase(WorkingSetView(ws1))  # no invalidate: get() revalidates lazily
    calls.clear()
    st.get("Q", U(snap, 0))
    assert [c[0] for c in calls] == ["P"]
    # move by 64: parity flips -> Q recomputed
    ws2, _ = moved(snap, 0, 64)
    st.rebase(WorkingSetView(ws2))
    calls.clear()
    st.get("Q", U(snap, 0))
    assert sorted(c[0] for c in calls) == ["P", "Q"]


def test_check_fact(snap):
    st = AnalysisStore(snap, chain_registry([]))
    r = st.get("B", U(snap, 0))
    st.check_fact(r)
    ws1, changed = moved(snap, 0, 0x1000)
    st.rebase(WorkingSetView(ws1))
    with pytest.raises(StaleFact):
        st.check_fact(r)
    ws2, _ = moved(snap, 1, 0x1000)  # a different unit moved: u0's B is still current
    st.rebase(WorkingSetView(ws2))
    st.check_fact(r)
    forged = AnalysisResult("B", 1, r.scope_id, 0, frozenset(), frozenset({("A", 1, "x")}),
                            frozenset(), True, r.result_hash)
    with pytest.raises(StaleFact):
        st.check_fact(forged)
    with pytest.raises(StaleFact):
        st.check_fact("nope")  # type: ignore[arg-type]


def test_undeclared_inputs_and_requires(snap):
    def reads_layout(ctx, s):
        return ctx.layout(s.id)

    def reads_other(ctx, s):
        return ctx.analysis("annotations", ScopeRef("snapshot", ctx.snapshot_id)).value

    def reads_code(ctx, s):
        return ctx.unit(s.id).tc_size

    def reads_profile(ctx, s):
        return ctx.profile("steady")

    reg = AnalysisRegistry([
        ANNOTATIONS,
        AnalysisSpec("x1", 1, "unit", (), frozenset({"code"}), reads_layout, int),
        AnalysisSpec("x2", 1, "unit", (), frozenset(), reads_other, object),
        AnalysisSpec("x3", 1, "unit", (), frozenset({"layout"}), reads_code, int),
        AnalysisSpec("x4", 1, "unit", (), frozenset({"profile:boot"}), reads_profile, object),
    ])
    st = AnalysisStore(snap, reg)
    for n in ("x1", "x2", "x3", "x4"):
        with pytest.raises(UndeclaredInput):
            st.get(n, U(snap, 0))


def test_missing_cycle_schema_scope(snap):
    def a(ctx, s):
        return ctx.analysis("cy-b", s).value

    def b(ctx, s):
        return ctx.analysis("cy-a", s).value

    reg = AnalysisRegistry([
        AnalysisSpec("cy-a", 1, "unit", (("cy-b", 1),), frozenset(), a, int),
        AnalysisSpec("cy-b", 1, "unit", (("cy-a", 1),), frozenset(), b, int),
        AnalysisSpec("str", 1, "unit", (), frozenset(), lambda c, s: "x", int),
        AnalysisSpec("needs", 1, "unit", (("absent", 1),), frozenset(), lambda c, s: 1, int),
        AnalysisSpec("v", 1, "unit", (), frozenset(), lambda c, s: 1, int),
        AnalysisSpec("v", 2, "unit", (), frozenset(), lambda c, s: 2, int),
    ])
    st = AnalysisStore(snap, reg)
    with pytest.raises(AnalysisCycle):
        st.get("cy-a", U(snap, 0))
    with pytest.raises(AnalysisSchemaError):
        st.get("str", U(snap, 0))
    with pytest.raises(MissingAnalysis):
        st.get("needs", U(snap, 0))
    with pytest.raises(MissingAnalysis):
        st.get("nothing", U(snap, 0))
    with pytest.raises(MissingAnalysis):
        st.get("v", U(snap, 0), min_version=3)
    assert st.get("v", U(snap, 0)).version == 2
    with pytest.raises(ValueError):
        st.get("v", ScopeRef("snapshot", snap.snapshot_id))
    with pytest.raises(KeyError):
        st.get("v", ScopeRef("unit", "tb:" + "0" * 32 + "#0"))


def test_profile_and_bundle_inputs(snap):
    class Prof:
        def __init__(self, pid):
            self.profile_id = pid

        def phase_reached(self, phase):
            return True if phase == "steady" else None

        def match_rate(self, phase):
            return 1.0

    def spec_fn(ctx, s):
        p = ctx.profile("steady")
        return None if p is None else p.profile_id

    reg = AnalysisRegistry([AnalysisSpec("prof", 1, "snapshot", (),
                                         frozenset({"profile:steady"}), spec_fn, object)])
    p1, p2 = Prof("sha256:" + "01" * 32), Prof("sha256:" + "02" * 32)
    S = ScopeRef("snapshot", snap.snapshot_id)
    st = AnalysisStore(snap, reg, profiles=[p1])
    r = st.get("prof", S)
    assert r.value == p1.profile_id and p1.profile_id in r.dep_inputs
    assert snap.snapshot_id in r.dep_inputs
    st.set_profiles([p2])
    assert st.get("prof", S).value == p2.profile_id
    st.set_profiles([])
    r0 = st.get("prof", S)
    assert r0.value is None and "no_profile:steady" in r0.dep_inputs
    hits = st.stats["hits"]
    assert st.get("prof", S) is r0 and st.stats["hits"] == hits + 1
    st.set_profiles([p1])  # a profile appears: the "no profile" result is stale
    assert st.get("prof", S).value == p1.profile_id


def test_sqlite_persistence(snap, tmp_path):
    db = str(tmp_path / "analyses.sqlite")
    calls = []
    st = AnalysisStore(snap, chain_registry(calls), sqlite_path=db)
    st.get("C", ScopeRef("snapshot", snap.snapshot_id))
    st.close()
    n = len(calls)
    st2 = AnalysisStore(snap, chain_registry(calls), sqlite_path=db)
    assert st2.get("C", ScopeRef("snapshot", snap.snapshot_id)).value > 0
    assert len(calls) == n and st2.stats["sqlite_hits"] >= 1
    # a stale persisted row is not used
    ws1, _ = moved(snap, 0, 0x1000)
    st3 = AnalysisStore(WorkingSetView(ws1), chain_registry(calls), sqlite_path=db)
    calls.clear()
    st3.get("C", ScopeRef("snapshot", snap.snapshot_id))
    assert sorted(c[0] for c in calls) == ["A", "B", "C"]
    st2.close()
    st3.close()
    # schema version mismatch -> table dropped, everything recomputed
    con = sqlite3.connect(db)
    con.execute("UPDATE meta SET v='999' WHERE k='schema_version'")
    con.commit()
    con.close()
    calls.clear()
    st4 = AnalysisStore(snap, chain_registry(calls), sqlite_path=db)
    st4.get("C", ScopeRef("snapshot", snap.snapshot_id))
    assert sorted(c[0] for c in calls) == ["A", "B", "C"]
    st4.close()


def test_value_digest():
    assert value_digest({"a": 1, "b": {2, 3}}) == value_digest({"b": {3, 2}, "a": 1})
    assert value_digest([1, 2]) != value_digest([2, 1])
    assert value_digest(1) != value_digest(True) != value_digest("1")
    assert value_digest(MappingProxyType({"x": b"\x00"})) == value_digest({"x": b"\x00"})
    with pytest.raises(TypeError):
        value_digest(object())

    class Obj:
        def __init__(self, x):
            self.x = x

    class Slotted:
        __slots__ = ("x",)

        def __init__(self, x):
            self.x = x

    class Own:
        def content_digest(self):
            return "fixed"

    assert value_digest(Obj(1)) == value_digest(Obj(1)) != value_digest(Obj(2))
    assert value_digest(Slotted(1)) != value_digest(Slotted(2))
    assert value_digest(Own()) == value_digest(Own())


def test_default_registry_has_builtin_annotations():
    reg = default_registry()
    assert reg.has("annotations", 1)


def test_working_set_changed_facets_on_section_shift(snap):
    uids = snap.uids()
    ws0 = WorkingSet.initial(snap)
    ws1, ch1 = ws0.apply([_E(EditKind.LAYOUT_ORDER, section="hot", uids=uids[:3])], owner="a")
    assert set(ch1) == set(uids[:3]) and all(f == {"layout"} for f in ch1.values())
    # moving uids[0] to 'cold' shifts the indices of uids[1:3] inside 'hot'
    ws2, ch2 = ws1.apply([_E(EditKind.LAYOUT_ORDER, section="cold", uids=[uids[0]])], owner="b")
    assert set(ch2) == set(uids[:3])
    assert ws2.sections["hot"] == tuple(uids[1:3]) and ws2.sections["cold"] == (uids[0],)
    ws3, ch3 = ws2.apply([_E(EditKind.ANNOTATE, uid=uids[3], key="k", value=1)], owner="c")
    assert ch3 == {uids[3]: frozenset({"annotations"})}
    ws4, ch4 = ws3.apply([_E(EditKind.ANNOTATE, uid=uids[3], key="k", value=1)], owner="c")
    assert ch4 == {}  # same value: nothing changed
    assert ws0.generation == 0 and ws4.generation == 4
    assert ws0.sections["hot"] == ()  # earlier generations unchanged
    with pytest.raises(ValueError):
        ws0.apply([_E(EditKind.CLONE)], owner="x")
