"""WP9 acceptance: PassManager, ProposalBuilder, WorkingSet, ledger (pure Python).

The snapshot is synthetic: WP4 builders -> .sc/.scm -> ``tests.synth.fake_snapshot``.
"""

from __future__ import annotations

import hashlib
import time
from types import MappingProxyType

import pytest

from scopt.analysis.api import AnalysisResult, AnalysisSpec, ScopeRef
from scopt.analysis.registry import ANNOTATIONS, AnalysisRegistry
from scopt.ledger import Ledger
from scopt.manager import (
    PassManager, PassRegistry, PipelineError, default_pass_registry, load_pipeline,
)
from scopt.model.view import WorkingSetView, snapshot_view
from scopt.passes.base import (
    B3_EDIT_KINDS, Budgets, EditCapabilityError, EditKind, Outcome, PassDescriptor, PassResult,
)
from scopt.passes.identity import Identity
from scopt.passes.move_units import MoveUnits
from scopt.proposal import CapabilityNotEnabled, ProposalBuilder
from tests.synth.fake_snapshot import fake_snapshot

ALL_B2 = ["ANNOTATE", "PIN", "LAYOUT_ORDER", "PLACE"]


@pytest.fixture(scope="module")
def snap():
    return fake_snapshot(6, not_relocatable=[2], no_meta=[5])


def make_pass(name, run, *, version=1, **desc):
    cls = type(f"P_{name.replace('-', '_')}", (), {
        "descriptor": PassDescriptor(name=name, version=version, **desc),
        "run": lambda self, unit, ctx, edits: run(unit, ctx, edits),
    })
    return cls


def pipeline(*entries, allowed=ALL_B2, name="test"):
    rows = []
    for e in entries:
        if isinstance(e, tuple):
            rows.append({"name": e[0], "version": 1, "params": e[1]})
        else:
            rows.append({"name": e, "version": 1})
    return {"name": name, "allowed_capabilities": list(allowed), "pass": rows}


def run_passes(snap, classes, pipe, *, analyses=None, clock=None, **kw):
    reg = PassRegistry(classes)
    areg = analyses if analyses is not None else AnalysisRegistry([ANNOTATIONS])
    if clock is not None:
        kw["clock"] = clock
    return PassManager(snap, pipe, passes=reg, analyses=areg, **kw).execute()


class FakeProfile:
    def __init__(self, pid="sha256:" + "ab" * 32, phases=None, match=None):
        self.profile_id = pid
        self._phases = phases if phases is not None else {"boot": True, "steady": True}
        self._match = match if match is not None else {"boot": 1.0, "steady": 1.0}

    def phase_reached(self, phase):
        return self._phases.get(phase)

    def match_rate(self, phase):
        return self._match.get(phase)


# ---------------------------------------------------------------------------
# Acceptance 1: identity
# ---------------------------------------------------------------------------

def test_identity_pipeline_no_change_empty_edit_log(snap):
    cand = PassManager(snap, "identity").execute()
    assert [p.outcome for p in cand.passes] == [Outcome.NO_CHANGE]
    assert cand.passes[0].reason == "identity"
    assert cand.edits == () and cand.edit_log == () and cand.edit_log_jsonl() == b""
    assert cand.passes[0].edit_digest is None
    assert cand.working_set.generation == 0
    assert not cand.has_layout_changes
    assert dict(cand.working_set.layout()) == dict(snap.layout())
    assert cand.input_snapshot_id == snap.snapshot_id
    side = cand.pipeline_provenance()
    assert side[0]["outcome"] == "no_change" and side[0]["edit_digest"] is None


def test_identity_pipeline_validates_against_sidecar_schema(snap):
    import jsonschema

    from scopt.formats.schemas import load_schema
    cand = PassManager(snap, "identity").execute()
    schema = load_schema("artifact_sidecar.v1")
    for prop in ("pipeline", "pinned", "profile_ids"):
        item = {**schema["properties"][prop]["items"], "$defs": schema["$defs"]}
        rows = {"pipeline": cand.pipeline_provenance(), "pinned": cand.pinned(),
                "profile_ids": cand.profile_rows()}[prop]
        for row in rows:
            jsonschema.validate(row, item)
    assert cand.pinned()  # the fixture has two pinned units


def test_candidate_id_is_deterministic(snap):
    a = PassManager(snap, "identity").execute()
    b = PassManager(snap, "identity").execute()
    assert a.candidate_id == b.candidate_id
    c = PassManager(snap, "identity", disable=["identity"]).execute()
    assert c.candidate_id != a.candidate_id


# ---------------------------------------------------------------------------
# Acceptance 2: edit outside capabilities -> EditCapabilityError, FAILED
# ---------------------------------------------------------------------------

def test_edit_outside_capabilities_raises_and_fails(snap):
    seen = {}

    def run(view, ctx, edits):
        try:
            edits.place(view.units()[0].uid, 0x4000)
        except EditCapabilityError as exc:
            seen["exc"] = exc
            raise
        return PassResult(Outcome.CANDIDATE, "x")

    P = make_pass("annot-only", run, capabilities=frozenset({EditKind.ANNOTATE}))
    cand = run_passes(snap, [P], pipeline("annot-only"))
    assert isinstance(seen["exc"], EditCapabilityError)
    assert not isinstance(seen["exc"], CapabilityNotEnabled)
    rec = cand.passes[0]
    assert rec.outcome is Outcome.FAILED and rec.reason == "edit_capability"
    assert cand.edits == ()


def test_rewrapped_capability_error_still_reports_capability(snap):
    def run(view, ctx, edits):
        try:
            edits.place(view.unit_ids()[0], 0x4000)
        except EditCapabilityError as exc:
            raise RuntimeError("wrapped") from exc
        return PassResult(Outcome.CANDIDATE, "x")

    P = make_pass("wrapper", run, capabilities=frozenset({EditKind.ANNOTATE}))
    rec = run_passes(snap, [P], pipeline("wrapper")).passes[0]
    assert (rec.outcome, rec.reason) == (Outcome.FAILED, "edit_capability")


def test_swallowed_capability_error_still_fails(snap):
    def run(view, ctx, edits):
        try:
            edits.pin(view.units()[0].uid, "nope")
        except EditCapabilityError:
            pass
        edits.annotate(view.units()[0].uid, "k", 1)
        return PassResult(Outcome.CANDIDATE, "x")

    P = make_pass("sneaky", run, capabilities=frozenset({EditKind.ANNOTATE}))
    cand = run_passes(snap, [P], pipeline("sneaky"))
    assert cand.passes[0].outcome is Outcome.FAILED
    assert cand.passes[0].reason == "edit_capability"
    assert cand.edits == () and cand.working_set.generation == 0


# ---------------------------------------------------------------------------
# Acceptance 3: exception -> FAILED with a traceback hash
# ---------------------------------------------------------------------------

def test_exception_gives_failed_with_traceback_hash(snap):
    def run(view, ctx, edits):
        edits.annotate(view.units()[0].uid, "k", "v")
        raise RuntimeError("boom")

    P = make_pass("crash", run, capabilities=frozenset({EditKind.ANNOTATE}))
    cand = run_passes(snap, [P, Identity], pipeline("crash", "identity"))
    rec = cand.passes[0]
    assert rec.outcome is Outcome.FAILED and rec.reason == "exception"
    tb_id, msg = rec.detail.split(" ", 1)
    assert tb_id == "sha256:" + hashlib.sha256(rec.traceback.encode()).hexdigest()
    assert "RuntimeError: boom" in msg and "RuntimeError: boom" in rec.traceback
    assert cand.edits == ()  # edits discarded
    assert cand.passes[1].outcome is Outcome.NO_CHANGE  # pipeline continues
    assert cand.ledger[0].evidence_ref == tb_id


def test_bad_return_value_fails(snap):
    P = make_pass("noresult", lambda v, c, e: None)
    cand = run_passes(snap, [P], pipeline("noresult"))
    assert cand.passes[0].outcome is Outcome.FAILED
    assert cand.passes[0].reason == "bad_pass_result"


# ---------------------------------------------------------------------------
# Acceptance 4: budget overrun -> BUDGET_EXHAUSTED
# ---------------------------------------------------------------------------

def test_budget_wall_clock_cooperative(snap):
    def run(view, ctx, edits):
        edits.annotate(view.units()[0].uid, "k", 1)
        while True:
            ctx.budget()
            time.sleep(0.001)

    P = make_pass("spin", run, capabilities=frozenset({EditKind.ANNOTATE}),
                  budgets=Budgets(max_wall_s=0.05))
    cand = run_passes(snap, [P], pipeline("spin"))
    rec = cand.passes[0]
    assert rec.outcome is Outcome.BUDGET_EXHAUSTED and rec.reason == "budget_exhausted"
    assert cand.edits == ()


def test_budget_wall_clock_post_hoc(snap):
    def run(view, ctx, edits):
        time.sleep(0.06)
        return PassResult(Outcome.NO_CHANGE, "slow")

    P = make_pass("slow", run, budgets=Budgets(max_wall_s=0.01))
    cand = run_passes(snap, [P], pipeline("slow"))
    assert cand.passes[0].outcome is Outcome.BUDGET_EXHAUSTED


def test_budget_max_units(snap):
    def run(view, ctx, edits):
        for u in view.units():
            edits.annotate(u.uid, "k", 1)
        return PassResult(Outcome.CANDIDATE, "x")

    P = make_pass("many", run, capabilities=frozenset({EditKind.ANNOTATE}),
                  budgets=Budgets(max_units=2))
    cand = run_passes(snap, [P], pipeline("many"))
    rec = cand.passes[0]
    assert rec.outcome is Outcome.BUDGET_EXHAUSTED and "max_units=2" in rec.detail
    assert cand.edits == ()


# ---------------------------------------------------------------------------
# Acceptance 5: missing profile -> SKIPPED_UNKNOWN
# ---------------------------------------------------------------------------

def _profiled(**kw):
    ran = []

    def run(view, ctx, edits):
        ran.append(ctx.profile("steady"))
        return PassResult(Outcome.NO_CHANGE, "ran")

    return make_pass("profiled", run, requires_profile_phase="steady", **kw), ran


def test_missing_profile_skips_unknown(snap):
    P, ran = _profiled()
    cand = run_passes(snap, [P], pipeline("profiled"))
    rec = cand.passes[0]
    assert rec.outcome is Outcome.SKIPPED_UNKNOWN and rec.reason == "missing_profile"
    assert ran == []


def test_profile_without_phase_or_unreached_or_low_match(snap):
    P, ran = _profiled()
    c1 = run_passes(snap, [P], pipeline("profiled"), profiles=[FakeProfile(phases={"boot": True})])
    assert (c1.passes[0].outcome, c1.passes[0].reason) == (Outcome.SKIPPED_UNKNOWN,
                                                          "missing_profile")
    c2 = run_passes(snap, [P], pipeline("profiled"),
                    profiles=[FakeProfile(phases={"steady": False})])
    assert (c2.passes[0].outcome, c2.passes[0].reason) == (Outcome.SKIPPED_UNKNOWN,
                                                          "profile_phase_not_reached")
    c3 = run_passes(snap, [P], pipeline("profiled"),
                    profiles=[FakeProfile(match={"steady": 0.90})])
    assert (c3.passes[0].outcome, c3.passes[0].reason) == (Outcome.SKIPPED_UNKNOWN,
                                                          "profile_incompatible")
    assert "0.95" in c3.passes[0].detail  # default min_profile_match
    assert ran == []
    good = FakeProfile(match={"steady": 0.97})
    c4 = run_passes(snap, [P], pipeline("profiled"), profiles=[good])
    assert c4.passes[0].outcome is Outcome.NO_CHANGE and ran == [good]
    assert c4.profile_rows() == [{"profile_id": good.profile_id, "verdict": "used"}]
    P99, _ = _profiled(min_profile_match=0.99)
    c5 = run_passes(snap, [P99], pipeline("profiled"), profiles=[good])
    assert c5.passes[0].reason == "profile_incompatible"


# ---------------------------------------------------------------------------
# Acceptance 6: B3 edit kinds -> capability_not_enabled
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(B3_EDIT_KINDS, key=lambda k: k.value))
def test_b3_capability_in_descriptor_is_not_admitted(snap, kind):
    P = make_pass("b3", lambda v, c, e: PassResult(Outcome.NO_CHANGE, "x"),
                  capabilities=frozenset({kind}))
    # Even a pipeline that lists the kind cannot enable it in B2.
    cand = run_passes(snap, [P], pipeline("b3", allowed=ALL_B2 + [kind.name]))
    rec = cand.passes[0]
    assert rec.outcome is Outcome.SKIPPED_UNSUPPORTED
    assert rec.reason == "capability_not_enabled" and kind.name in rec.detail


@pytest.mark.parametrize("method", ["clone", "redirect_exit", "add_entry", "pure_rewrite",
                                    "add_region", "add_recovery", "attach_fact"])
def test_b3_builder_methods_raise_capability_not_enabled(snap, method):
    seen = {}

    def run(view, ctx, edits):
        try:
            getattr(edits, method)(units=[view.units()[0].uid])
        except EditCapabilityError as exc:
            seen["exc"] = exc
            raise
        return PassResult(Outcome.CANDIDATE, "x")

    P = make_pass("b3call", run, capabilities=frozenset({EditKind.ANNOTATE}))
    cand = run_passes(snap, [P], pipeline("b3call"))
    assert isinstance(seen["exc"], CapabilityNotEnabled)
    assert (cand.passes[0].outcome, cand.passes[0].reason) == (Outcome.FAILED,
                                                              "capability_not_enabled")


def test_b3_builder_standalone(snap):
    view = snapshot_view(snap)
    d = PassDescriptor("x", 1, capabilities=frozenset({EditKind.CLONE}))
    b = ProposalBuilder(d, view)
    with pytest.raises(CapabilityNotEnabled):
        b.clone()
    assert len(b) == 0 and len(b.violations) == 1


def test_b2_kind_not_allowed_by_pipeline(snap):
    P = make_pass("placer", lambda v, c, e: PassResult(Outcome.NO_CHANGE, "x"),
                  capabilities=frozenset({EditKind.PLACE}))
    cand = run_passes(snap, [P], pipeline("placer", allowed=["ANNOTATE"]))
    assert (cand.passes[0].outcome, cand.passes[0].reason) == (Outcome.SKIPPED_UNSUPPORTED,
                                                              "capability_not_enabled")


# ---------------------------------------------------------------------------
# Acceptance 7: stale reads -> stale_edit (and stale facts -> stale_fact)
# ---------------------------------------------------------------------------

def test_stale_unit_read_gives_stale_edit(snap):
    def run(view, ctx, edits):
        u = view.units()[0]
        edits.annotate(u.uid, "k", 1, reads=[(u.uid, "sha256:" + "00" * 32)])
        return PassResult(Outcome.CANDIDATE, "x")

    P = make_pass("stale", run, capabilities=frozenset({EditKind.ANNOTATE}))
    cand = run_passes(snap, [P], pipeline("stale"))
    assert (cand.passes[0].outcome, cand.passes[0].reason) == (Outcome.FAILED, "stale_edit")
    assert cand.edits == () and cand.working_set.generation == 0


def test_stale_layout_and_analysis_reads(snap):
    uids = snap.uids()

    def run_layout(view, ctx, edits):
        edits.annotate(uids[0], "k", 1, reads=[(uids[1], "layout:" + "11" * 32)])
        return PassResult(Outcome.CANDIDATE, "x")

    def run_analysis(view, ctx, edits):
        r = ctx.analysis("annotations", view)
        edits.annotate(uids[0], "k", 1, reads=[(r.name, "sha256:" + "22" * 32)])
        return PassResult(Outcome.CANDIDATE, "x")

    PL = make_pass("stale-layout", run_layout, capabilities=frozenset({EditKind.ANNOTATE}))
    PA = make_pass("stale-analysis", run_analysis, capabilities=frozenset({EditKind.ANNOTATE}),
                   requires=(("annotations", 1),))
    c = run_passes(snap, [PL, PA], pipeline("stale-layout", "stale-analysis"))
    assert [(p.outcome, p.reason) for p in c.passes] == [(Outcome.FAILED, "stale_edit")] * 2


def test_reads_recorded_by_ctx_go_stale_when_analysis_changes(snap):
    """A read of an analysis result becomes stale once the result changes."""
    uids = snap.uids()
    holder = {}

    def run(view, ctx, edits):
        r = ctx.analysis("annotations", view)
        holder["r"] = r
        edits.annotate(uids[0], "k", 1)
        return PassResult(Outcome.CANDIDATE, "x")

    P = make_pass("reader", run, capabilities=frozenset({EditKind.ANNOTATE}),
                  requires=(("annotations", 1),))
    cand = run_passes(snap, [P], pipeline("reader"))
    assert cand.passes[0].outcome is Outcome.CANDIDATE
    e = cand.edits[0]
    assert any(r.kind == "analysis" and r.key == "annotations" for r in e.reads)
    assert any(r.kind == "unit" and r.key == uids[0] for r in e.reads)
    assert any(r.kind == "annotations" and r.key == uids[0] for r in e.reads)
    assert e.affected_origins and e.affected_origins[0].startswith("0x")

    # A second pass that cites the first pass's (pre-annotation) result is stale.
    def run2(view, ctx, edits):
        edits.annotate(uids[1], "k", 2, reads=[holder["r"]])
        return PassResult(Outcome.CANDIDATE, "x")

    P2 = make_pass("reader2", run2, capabilities=frozenset({EditKind.ANNOTATE}))
    cand2 = run_passes(snap, [P, P2], pipeline("reader", "reader2"))
    assert (cand2.passes[1].outcome, cand2.passes[1].reason) == (Outcome.FAILED, "stale_edit")


def _certified_registry():
    def compute(ctx, scope):
        ctx.certify()
        return ctx.unit(scope.id).tc_size

    spec = AnalysisSpec("size.fact", 1, "unit", (), frozenset({"code"}), compute, int)
    return AnalysisRegistry([ANNOTATIONS, spec])


def test_cited_fact_valid_and_stale(snap):
    uids = snap.uids()

    def good(view, ctx, edits):
        f = ctx.fact("size.fact", uids[0])
        edits.annotate(uids[0], "size", f.value)
        return PassResult(Outcome.CANDIDATE, "x")

    def stale(view, ctx, edits):
        f = ctx.fact("size.fact", uids[0])
        forged = AnalysisResult(f.name, f.version, f.scope_id, f.value,
                                frozenset({(uids[0], "sha256:" + "33" * 32)}), frozenset(),
                                f.dep_inputs, True, f.result_hash)
        edits.annotate(uids[1], "size", f.value, cites=[forged])
        return PassResult(Outcome.CANDIDATE, "x")

    kw = dict(capabilities=frozenset({EditKind.ANNOTATE}), requires=(("size.fact", 1),))
    G = make_pass("good", good, **kw)
    S = make_pass("stalefact", stale, **kw)
    cand = run_passes(snap, [G, S], pipeline("good", "stalefact"),
                      analyses=_certified_registry())
    assert cand.passes[0].outcome is Outcome.CANDIDATE
    assert cand.edits[0].facts and cand.edits[0].facts[0].name == "size.fact"
    assert (cand.passes[1].outcome, cand.passes[1].reason) == (Outcome.FAILED, "stale_fact")


# ---------------------------------------------------------------------------
# Acceptance 9: ledger rows for every pass
# ---------------------------------------------------------------------------

def test_ledger_row_for_every_pass(snap, tmp_path):
    def boom(v, c, e):
        raise ValueError("x")

    P = make_pass("boom", boom)
    reg = [Identity, MoveUnits, P]
    pipe = pipeline("identity", "boom", "missing-pass", "move-units", "identity")
    ts = iter(f"2026-01-01T00:00:0{i}.000Z" for i in range(10))
    cand = run_passes(snap, reg, pipe, disable=["move-units"], clock=lambda: next(ts))
    assert len(cand.passes) == 5 and len(cand.ledger) == 5
    assert [(r.subject, r.id, r.stage, r.verdict, r.reason) for r in cand.ledger] == [
        ("pass", "identity@1", "optimized", "no_change", "identity"),
        ("pass", "boom@1", "optimized", "failed", "exception"),
        ("pass", "missing-pass@1", "optimized", "skipped_unsupported", "pass_not_registered"),
        ("pass", "move-units@1", "optimized", "skipped_unsupported", "ablation_disabled"),
        ("pass", "identity@1", "optimized", "no_change", "identity"),
    ]
    led = Ledger(cand.ledger)
    p = tmp_path / "ledger.jsonl"
    led.write(p)
    back = Ledger.read(p)
    assert back.rows == led.rows
    assert p.read_bytes().count(b"\n") == 5


def test_b2_layout_pipeline_loads_and_records_unregistered_passes(snap):
    pipe = load_pipeline("b2-layout")
    assert [p.name for p in pipe.passes] == ["hotcold-partition", "cluster-hot", "order-cold"]
    assert pipe.allowed_capabilities == {EditKind.ANNOTATE, EditKind.PIN, EditKind.LAYOUT_ORDER}
    reg = default_pass_registry()
    missing = [p.name for p in pipe.passes if not reg.versions(p.name)]
    cand = PassManager(snap, pipe, profiles=[FakeProfile()]).execute()
    assert len(cand.ledger) == 3
    for p in cand.passes:
        if p.name in missing:
            assert (p.outcome, p.reason) == (Outcome.SKIPPED_UNSUPPORTED, "pass_not_registered")


# ---------------------------------------------------------------------------
# Layout edits, WorkingSet, conflicts, ablation
# ---------------------------------------------------------------------------

def test_move_units_place_and_working_set_cow(snap):
    uids = snap.uids()
    base_layout = dict(snap.layout())
    pipe = pipeline(("move-units", {"deltas": {uids[0]: 0x10000}, "moves": {uids[1]: "0x20000"}}))
    cand = run_passes(snap, [MoveUnits], pipe)
    rec = cand.passes[0]
    assert rec.outcome is Outcome.CANDIDATE and rec.n_edits == 2
    assert rec.edit_digest and rec.edit_digest.startswith("sha256:")
    ws = cand.working_set
    assert ws.generation == 1 and ws.provenance[0]["pass"] == "move-units"
    assert ws.layout()[uids[0]] == base_layout[uids[0]] + 0x10000
    assert ws.layout()[uids[1]] == 0x20000
    assert ws.layout()[uids[3]] == base_layout[uids[3]]
    assert dict(snap.layout()) == base_layout  # base untouched
    assert ws.units()[0] is snap.units()[0]  # TBUnits shared, not copied
    assert cand.has_layout_changes
    assert [e.kind for e in cand.edits] == [EditKind.PLACE, EditKind.PLACE]


def test_layout_edit_on_non_relocatable_or_no_meta_unit(snap):
    uids = snap.uids()
    for idx in (2, 5):
        cand = run_passes(snap, [MoveUnits],
                          pipeline(("move-units", {"deltas": {uids[idx]: 64}})))
        assert (cand.passes[0].outcome, cand.passes[0].reason) == (Outcome.FAILED,
                                                                  "not_relocatable")
    pinned = {row["unit"]: row["reasons"] for row in PassManager(snap, "identity").execute()
              .pinned()}
    assert pinned == {uids[2]: ["unexplained_reference"], uids[5]: ["meta_no_meta", "no_meta"]}


def test_misaligned_place_is_precondition_failure(snap):
    uids = snap.uids()
    cand = run_passes(snap, [MoveUnits], pipeline(("move-units", {"deltas": {uids[0]: 32}})))
    assert (cand.passes[0].outcome, cand.passes[0].reason) == (Outcome.FAILED,
                                                              "edit_precondition")


def _orderer(name, section, idxs, *, overrides=frozenset(), pin=None):
    def run(view, ctx, edits):
        us = view.unit_ids()
        if pin is not None:
            edits.pin(us[pin], "hint")
        if idxs:
            edits.layout_order(section, [us[i] for i in idxs])
        return PassResult(Outcome.CANDIDATE, "ordered")

    return make_pass(name, run, overrides=overrides,
                     capabilities=frozenset({EditKind.LAYOUT_ORDER, EditKind.PIN}),
                     invalidates=frozenset({"layout"}))


def test_layout_conflict_and_overrides(snap):
    A = _orderer("order-a", "hot", [0, 1])
    B = _orderer("order-b", "cold", [1, 3])
    C = _orderer("order-c", "cold", [1, 3], overrides=frozenset({"layout"}))
    c1 = run_passes(snap, [A, B], pipeline("order-a", "order-b"))
    assert c1.passes[0].outcome is Outcome.CANDIDATE
    assert (c1.passes[1].outcome, c1.passes[1].reason) == (Outcome.FAILED, "conflict")
    assert c1.working_set.sections["hot"] == (snap.uids()[0], snap.uids()[1])
    c2 = run_passes(snap, [A, C], pipeline("order-a", "order-c"))
    assert c2.passes[1].outcome is Outcome.CANDIDATE
    ws = c2.working_set
    assert ws.sections["hot"] == (snap.uids()[0],)
    assert ws.sections["cold"] == (snap.uids()[1], snap.uids()[3])
    assert ws.layout_owner(snap.uids()[1]) == "order-c"


def test_layout_order_twice_in_one_pass_and_pinned(snap):
    def twice(view, ctx, edits):
        us = view.unit_ids()
        edits.layout_order("hot", [us[0]])
        edits.layout_order("warm", [us[0]])
        return PassResult(Outcome.CANDIDATE, "x")

    T = make_pass("twice", twice, capabilities=frozenset({EditKind.LAYOUT_ORDER}))
    c = run_passes(snap, [T], pipeline("twice"))
    assert (c.passes[0].outcome, c.passes[0].reason) == (Outcome.FAILED, "conflict")
    P = _orderer("pin-then-order", "hot", [0], pin=0)
    c2 = run_passes(snap, [P], pipeline("pin-then-order"))
    assert (c2.passes[0].outcome, c2.passes[0].reason) == (Outcome.FAILED, "pinned")
    pin_only = _orderer("pinner", "hot", [], pin=1)
    later = _orderer("later", "hot", [1], overrides=frozenset({"layout"}))
    c3 = run_passes(snap, [pin_only, later], pipeline("pinner", "later"))
    assert c3.passes[0].outcome is Outcome.CANDIDATE
    assert (c3.passes[1].outcome, c3.passes[1].reason) == (Outcome.FAILED, "pinned")
    assert c3.working_set.edit_pin_reasons(snap.uids()[1]) == ("hint",)


def test_annotations_visible_to_later_pass(snap):
    uids = snap.uids()

    def annotate(view, ctx, edits):
        for i, u in enumerate(view.units()):
            edits.annotate(u.uid, "hotness", "hot" if i == 0 else "cold_zero")
        return PassResult(Outcome.CANDIDATE, "classified")

    got = {}

    def read(view, ctx, edits):
        got["a"] = dict(ctx.analysis("annotations", view).value)
        got["unit"] = dict(view.unit(uids[0]).annotations)
        return PassResult(Outcome.NO_CHANGE, "read")

    A = make_pass("annot", annotate, capabilities=frozenset({EditKind.ANNOTATE}))
    Rd = make_pass("read", read, requires=(("annotations", 1),))
    cand = run_passes(snap, [Rd, A, Rd], pipeline("read", "annot", "read"))
    assert [p.outcome for p in cand.passes] == [Outcome.NO_CHANGE, Outcome.CANDIDATE,
                                                Outcome.NO_CHANGE]
    assert got["a"][uids[0]] == {"hotness": "hot"}
    assert got["unit"] == {"hotness": "hot"}
    assert cand.passes[1].invalidated == 1  # the first read's annotations result
    assert snap.unit(uids[0]).annotations == {}  # base untouched


def test_unit_scope_pass(snap):
    def run(uv, ctx, edits):
        if uv.unit_id == snap.uids()[0]:
            edits.annotate(uv.unit_id, "first", True)
            return PassResult(Outcome.CANDIDATE, "tagged", affected=(uv.unit_id,))
        assert uv.entries()[0].kind == "general" and uv.effects() is None
        return PassResult(Outcome.NO_CHANGE, "untouched")

    P = make_pass("per-unit", run, unit_scope="unit", capabilities=frozenset({EditKind.ANNOTATE}))
    cand = run_passes(snap, [P], pipeline("per-unit"))
    rec = cand.passes[0]
    assert rec.outcome is Outcome.CANDIDATE and rec.affected == (snap.uids()[0],)
    assert rec.stats["units_candidate"] == 1 and rec.stats["units_no_change"] == 5


def test_region_scope_and_capture_level_and_missing_analysis(snap):
    R_ = make_pass("region", lambda *a: None, unit_scope="region")
    L = make_pass("lvl2", lambda *a: None, requires_capture_level=2)
    M = make_pass("needs-refs", lambda *a: None, requires=(("refs", 1),))
    cand = run_passes(snap, [R_, L, M], pipeline("region", "lvl2", "needs-refs"))
    assert [(p.outcome, p.reason) for p in cand.passes] == [
        (Outcome.SKIPPED_UNSUPPORTED, "region_scope_not_enabled"),
        (Outcome.SKIPPED_UNSUPPORTED, "capture_level"),
        (Outcome.SKIPPED_UNSUPPORTED, "missing_analysis"),
    ]


def test_ablation_only_and_bad_names(snap):
    uids = snap.uids()
    pipe = pipeline("identity", ("move-units", {"deltas": {uids[0]: 64}}))
    c = run_passes(snap, [Identity, MoveUnits], pipe, only="move-units")
    assert [(p.name, p.reason) for p in c.passes] == [("identity", "ablation_disabled"),
                                                      ("move-units", "moved")]
    with pytest.raises(PipelineError):
        run_passes(snap, [Identity], pipeline("identity"), disable=["nope"])
    with pytest.raises(PipelineError):
        run_passes(snap, [Identity], pipeline("identity"), only="nope")


def test_invalid_params(snap):
    c = run_passes(snap, [MoveUnits], pipeline(("move-units", {"bogus": 1})))
    assert (c.passes[0].outcome, c.passes[0].reason) == (Outcome.FAILED, "invalid_params")
    c2 = run_passes(snap, [MoveUnits], pipeline("move-units"),
                    params={"move-units": {"moves": "x"}})
    assert c2.passes[0].reason == "invalid_params"


def test_no_change_with_edits_is_failure(snap):
    def run(view, ctx, edits):
        edits.annotate(view.unit_ids()[0], "k", 1)
        return PassResult(Outcome.NO_CHANGE, "liar")

    P = make_pass("liar", run, capabilities=frozenset({EditKind.ANNOTATE}))
    c = run_passes(snap, [P], pipeline("liar"))
    assert (c.passes[0].outcome, c.passes[0].reason) == (Outcome.FAILED, "edits_with_no_change")


def test_pipeline_toml_errors(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text('name = "x"\nallowed_capabilities = ["TELEPORT"]\n')
    with pytest.raises(PipelineError):
        load_pipeline(p)
    p.write_text('name = "x"\nfoo = 1\n')
    with pytest.raises(PipelineError):
        load_pipeline(p)
    with pytest.raises(PipelineError):
        load_pipeline(tmp_path / "missing.toml")
    p.write_text('name = "x"\n[[pass]]\nname = "identity"\n')
    with pytest.raises(PipelineError):
        load_pipeline(p)


def test_view_is_fresh_per_pass_and_readonly(snap):
    views = []

    def run(view, ctx, edits):
        views.append(view)
        assert isinstance(view, WorkingSetView)
        with pytest.raises(AttributeError):
            view.units()[0].tb_offset = 0  # frozen
        return PassResult(Outcome.NO_CHANGE, "x")

    P = make_pass("v", run)
    run_passes(snap, [P], pipeline("v", "v"))
    assert len(views) == 2 and views[0] is not views[1]


def test_unit_view_exits_from_capture(snap):
    v = snapshot_view(snap)
    uv = v.unit_view(snap.uids()[0])
    kinds = [x.kind.value for x in uv.exits()]
    # simple_tb: exit_tb(tb+0) with no goto_tb, then the irq-check exit_tb(tb+3)
    assert kinds == ["exit_null", "exit_requested"]
    assert [x.id for x in uv.exits()] == ["x0", "x1"]
    o = uv.origins()[0]
    assert o.size == 4 and o.vaddrs == snap.units()[0].vaddrs


def test_mappings_are_readonly(snap):
    ws = PassManager(snap, "identity").execute().working_set
    with pytest.raises(TypeError):
        ws.sections["hot"] = ()  # type: ignore[index]
    assert isinstance(ws.layout(), MappingProxyType)


def test_scope_ref_for_unit_scope_analysis(snap):
    reg = _certified_registry()
    from scopt.analysis.store import AnalysisStore
    st = AnalysisStore(snap, reg)
    r = st.get("size.fact", ScopeRef("unit", snap.uids()[0]))
    assert r.value == snap.units()[0].tc_size and r.certified


# ---------------------------------------------------------------------------
# The §5.10 pass-writer example runs unchanged against this framework
# ---------------------------------------------------------------------------

class _Count:
    def __init__(self, value, status="exact"):
        self.value, self.status = value, status


class _Counts:
    def __init__(self, phases):
        self._p = phases

    def phase(self, name):
        return self._p.get(name, {})


def _design_registry(snap):
    uids = snap.uids()
    steady = {uids[0]: _Count(900), uids[1]: _Count(100), uids[3]: _Count(0),
              uids[4]: _Count(5, "lower_bound")}
    boot = {uids[3]: _Count(7)}

    def counts(ctx, s):
        ctx.profile("steady")
        return _Counts({"steady": steady, "boot": boot})

    return AnalysisRegistry([
        ANNOTATIONS,
        AnalysisSpec("profile.counts", 1, "snapshot", (), frozenset({"profile:*"}), counts,
                     object),
        AnalysisSpec("refs", 1, "unit", (), frozenset({"code", "meta"}), lambda c, s: (), tuple),
    ])


class HotColdPartition:
    """Copied from design §5.10 (only the descriptor's params_schema is a plain dict)."""
    descriptor = PassDescriptor(
        name="hotcold-partition", version=1, unit_scope="snapshot",
        requires=(("profile.counts", 1), ("refs", 1)), requires_profile_phase="steady",
        min_profile_match=0.95, capabilities=frozenset({EditKind.ANNOTATE}),
        budgets=Budgets(max_wall_s=10),
        preserves=frozenset({"refs", "layout", "cfg.known_edges", "profile.counts", "recovery"}),
        params_schema={"phase": str, "coverage": float})

    def run(self, snap, ctx, edits):
        phase = ctx.params.get("phase", "steady"); cov = ctx.params.get("coverage", 0.99)  # noqa: E702
        steady = ctx.analysis("profile.counts", snap).value.phase(phase)
        boot = ctx.analysis("profile.counts", snap).value.phase("boot")
        exact = {u: c.value for u, c in steady.items() if c.status == "exact"}
        if not exact:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "no_exact_counts")
        total, acc, hot = sum(exact.values()), 0, set()
        for u, v in sorted(exact.items(), key=lambda kv: (-kv[1], kv[0])):
            if total and acc / total >= cov:
                break
            if v > 0:
                hot.add(u); acc += v  # noqa: E702
        for u in snap.units():
            c = steady.get(u.uid)
            if u.uid in hot:
                cls = "hot"
            elif c is None or c.status != "exact":
                cls = "unknown"
            elif c.value > 0:
                cls = "warm"
            elif boot.get(u.uid) and boot[u.uid].status == "exact" and boot[u.uid].value > 0:
                cls = "boot_only"
            else:
                cls = "cold_zero"
            edits.annotate(u.uid, "hotness", cls)
        return PassResult(Outcome.CANDIDATE, "classified", affected=tuple(sorted(hot)),
                          stats={"hot": len(hot), "coverage": acc / max(total, 1)})


class OrderBySection:
    """A minimal LAYOUT_ORDER pass driven by the annotations analysis."""
    descriptor = PassDescriptor(
        name="order-by-section", version=1, requires=(("annotations", 1),),
        capabilities=frozenset({EditKind.LAYOUT_ORDER}), invalidates=frozenset({"layout"}))

    def run(self, snap, ctx, edits):
        ann = ctx.analysis("annotations", snap).value
        sec = {"hot": "hot", "warm": "warm", "unknown": "unknown", "boot_only": "boot",
               "cold_zero": "cold"}
        groups: dict[str, list[str]] = {}
        for u in snap.units():
            if not snap.is_relocatable(u.uid) or snap.is_pinned(u.uid):
                continue
            h = ann.get(u.uid, {}).get("hotness")
            if h in sec:
                groups.setdefault(sec[h], []).append(u.uid)
        for s, us in groups.items():
            edits.layout_order(s, us)
        return PassResult(Outcome.CANDIDATE if groups else Outcome.NO_CHANGE, "ordered")


def test_design_example_hotcold_then_order(snap):
    uids = snap.uids()
    pipe = pipeline(("hotcold-partition", {"phase": "steady", "coverage": 0.99}),
                    "order-by-section", allowed=["ANNOTATE", "LAYOUT_ORDER"])
    prof = FakeProfile()
    cand = run_passes(snap, [HotColdPartition, OrderBySection], pipe,
                      analyses=_design_registry(snap), profiles=[prof])
    assert [(p.outcome, p.reason) for p in cand.passes] == [
        (Outcome.CANDIDATE, "classified"), (Outcome.CANDIDATE, "ordered")]
    ws = cand.working_set
    assert ws.annotations(uids[0])["hotness"] == "hot"
    assert ws.annotations(uids[1])["hotness"] == "hot"  # 900/1000 < 0.99 -> uid1 also hot
    assert ws.annotations(uids[3])["hotness"] == "boot_only"
    assert ws.annotations(uids[4])["hotness"] == "unknown"
    assert ws.annotations(uids[2])["hotness"] == "unknown"
    assert ws.sections["hot"] == (uids[0], uids[1])
    assert ws.sections["boot"] == (uids[3],)
    assert ws.sections["unknown"] == (uids[4],)  # uid2/uid5 are pinned, not ordered
    assert cand.profile_rows() == [{"profile_id": prof.profile_id, "verdict": "used"}]
    assert cand.passes[0].stats["hot"] == 2
    # without a profile the first pass is SKIPPED_UNKNOWN and the second finds nothing
    c2 = run_passes(snap, [HotColdPartition, OrderBySection], pipe,
                    analyses=_design_registry(snap))
    assert [(p.outcome, p.reason) for p in c2.passes] == [
        (Outcome.SKIPPED_UNKNOWN, "missing_profile"), (Outcome.NO_CHANGE, "ordered")]


# ---------------------------------------------------------------------------
# Real WP6 ProfileView / profile.counts (scopt.formats.profile_v1, scopt.analysis.profile)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pworld(tmp_path_factory):
    from tests.synth.profile_world import make_world
    return make_world(str(tmp_path_factory.mktemp("wp9-pworld")))


@pytest.fixture(scope="module")
def pview(pworld):
    from scopt.formats.profile_v1 import ProfileView
    return ProfileView.open(pworld.ingest(), pworld.target())


@pytest.fixture(scope="module")
def psnap(pworld):
    return fake_snapshot(pworld.units[:3])


def _real_registry():
    from scopt.analysis.registry import default_registry
    reg = default_registry()
    if not reg.has("refs", 1):  # stub only when WP7's refs.v1 is not discovered
        reg.register(AnalysisSpec("refs", 1, "unit", (), frozenset({"code", "meta"}),
                                  lambda c, s: (), tuple))
    return reg


def test_profileview_phase_info(pview, pworld, psnap):
    from scopt.analysis.store import profile_for_phase, profile_phase_info
    from scopt.formats.profile_v1 import ProfileView
    assert psnap.uids() == list(pview.target.unit_ids)
    assert (pview.coverage, pview.coverage_phase) == (1.0, "steady")
    assert profile_phase_info(pview, "steady") == (True, True, 1.0)
    assert profile_phase_info(pview, "boot") == (True, True, 1.0)  # recomputed for boot
    assert profile_phase_info(pview, "nope") == (False, None, None)
    assert profile_for_phase([pview], "steady") is pview
    assert profile_for_phase([pview], None) is pview
    nc = ProfileView.not_collected(pworld.target())
    assert profile_phase_info(nc, "steady") == (False, None, None)
    assert profile_for_phase([nc], "steady") is None and profile_for_phase([nc], None) is None
    # measured against another unit set: steady entries are 2 on unit 0 and 2 on unit 1
    other = [u for u in psnap.uids()[1:]]
    present, reached, match = profile_phase_info(pview, "steady", other)
    assert (present, reached, match) == (True, True, 0.5)


def test_profileview_admits_profile_pass_and_ctx_profile(pview, psnap):
    P, ran = _profiled()
    cand = run_passes(psnap, [P], pipeline("profiled"), profiles=[pview])
    assert (cand.passes[0].outcome, cand.passes[0].reason) == (Outcome.NO_CHANGE, "ran")
    assert ran == [pview]
    assert cand.profile_rows() == [{"profile_id": pview.profile_id, "verdict": "used"}]


def test_profileview_unreached_and_low_match(pworld, psnap):
    from scopt.formats.profile_v1 import ProfileView
    P, ran = _profiled()
    unreached = ProfileView.open(pworld.ingest(pworld.qprof("unreached.qprof", phase_starts={})),
                                 pworld.target())
    assert unreached.profile.phase_reached(unreached.profile.phase_id("steady")) is False
    c1 = run_passes(psnap, [P], pipeline("profiled"), profiles=[unreached])
    assert (c1.passes[0].outcome, c1.passes[0].reason) == (Outcome.SKIPPED_UNKNOWN,
                                                          "profile_phase_not_reached")
    # the snapshot being optimized lacks unit 0: half the steady entries match
    small = fake_snapshot(pworld.units[1:3])
    v = ProfileView.open(pworld.ingest(), pworld.target())
    c2 = run_passes(small, [P], pipeline("profiled"), profiles=[v])
    assert (c2.passes[0].outcome, c2.passes[0].reason) == (Outcome.SKIPPED_UNKNOWN,
                                                          "profile_incompatible")
    assert "0.5000" in c2.passes[0].detail
    assert ran == []


def test_default_registry_has_wp6_profile_counts(pview, psnap):
    from scopt.analysis.registry import default_registry
    from scopt.analysis.store import AnalysisStore
    reg = default_registry()
    assert "profile.counts" in reg.names()
    st = AnalysisStore(psnap, reg)  # no profile: every count unknown/not_collected
    r0 = st.get("profile.counts", psnap.snapshot_id)
    assert {c.status for c in r0.value.phase("steady").values()} == {"unknown"}
    assert "no_profile:*" in r0.dep_inputs
    st.set_profiles([pview])  # attaching a profile makes the no-profile result stale
    assert not st.is_current(r0)
    r1 = st.get("profile.counts", psnap.snapshot_id)
    u = psnap.uids()
    steady = r1.value.phase("steady")
    assert [(steady[x].value, steady[x].status) for x in u] == [(2, "exact"), (2, "exact"),
                                                               (0, "exact")]
    assert pview.profile_id in r1.dep_inputs and r1.result_hash != r0.result_hash


def test_profile_input_declarations(pview, psnap):
    from scopt.analysis.store import AnalysisStore, UndeclaredInput
    seen = {}

    def mk(name, inputs, phase):
        def compute(ctx, s):
            seen[name] = ctx.profile(phase)
            return 1
        return AnalysisSpec(name, 1, "snapshot", (), frozenset(inputs), compute, int)

    reg = AnalysisRegistry([ANNOTATIONS, mk("any", {"profile"}, None),
                            mk("star", {"profile:*"}, "boot"),
                            mk("one", {"profile:steady"}, "steady"),
                            mk("bad_none", {"profile:steady"}, None),
                            mk("bad_phase", {"profile:steady"}, "boot"),
                            mk("bad_undeclared", {"code"}, "steady")])
    st = AnalysisStore(psnap, reg, profiles=[pview])
    for n in ("any", "star", "one"):
        st.get(n, psnap.snapshot_id)
        assert seen[n] is pview
    for n in ("bad_none", "bad_phase", "bad_undeclared"):
        with pytest.raises(UndeclaredInput):
            st.get(n, psnap.snapshot_id)


def test_design_example_hotcold_with_real_profile_counts(pview, psnap):
    uids = psnap.uids()
    pipe = pipeline(("hotcold-partition", {"phase": "steady", "coverage": 0.99}),
                    "order-by-section", allowed=["ANNOTATE", "LAYOUT_ORDER"])
    cand = run_passes(psnap, [HotColdPartition, OrderBySection], pipe,
                      analyses=_real_registry(), profiles=[pview])
    assert [(p.outcome, p.reason) for p in cand.passes] == [
        (Outcome.CANDIDATE, "classified"), (Outcome.CANDIDATE, "ordered")]
    ws = cand.working_set
    assert [ws.annotations(u)["hotness"] for u in uids] == ["hot", "hot", "boot_only"]
    assert ws.sections["hot"] == (uids[0], uids[1]) and ws.sections["boot"] == (uids[2],)
    assert cand.profile_rows() == [{"profile_id": pview.profile_id, "verdict": "used"}]
    # the edits cite the profile.counts result they were computed from
    reads = {(r.kind, r.key) for e in cand.edits if e.kind is EditKind.ANNOTATE for r in e.reads}
    assert ("analysis", "profile.counts") in reads


# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------

def test_ledger_evidence_ref_for_rewrapped_capability_error(snap):
    def run(view, ctx, edits):
        try:
            edits.place(view.unit_ids()[0], 0x4000)
        except EditCapabilityError as exc:
            raise RuntimeError("wrapped") from exc
        return PassResult(Outcome.CANDIDATE, "x")

    P = make_pass("bad", run, capabilities=frozenset({EditKind.ANNOTATE}))
    cand = run_passes(snap, [P], pipeline("bad"))
    rec, row = cand.passes[0], cand.ledger[0]
    assert (rec.outcome, rec.reason) == (Outcome.FAILED, "edit_capability")
    assert row.evidence_ref == "sha256:" + hashlib.sha256(rec.traceback.encode()).hexdigest()


def test_identity_without_capture_level_or_meta_is_no_change():
    s = fake_snapshot(3, no_meta=[0, 1, 2], capture_level=None)
    cand = PassManager(s, "identity").execute()
    assert (cand.passes[0].outcome, cand.passes[0].reason) == (Outcome.NO_CHANGE, "identity")
    assert cand.edits == ()


def test_named_pipeline_ignores_cwd_and_honours_env(tmp_path, monkeypatch):
    (tmp_path / "identity").write_text("not toml [[[")
    monkeypatch.chdir(tmp_path)
    assert [p.name for p in load_pipeline("identity").passes] == ["identity"]
    d = tmp_path / "pipes"
    d.mkdir()
    (d / "identity.toml").write_text('name = "identity"\nallowed_capabilities = []\n'
                                     '[[pass]]\nname = "move-units"\nversion = 1\n')
    monkeypatch.setenv("SCOPT_PIPELINES_DIR", str(d))
    assert [p.name for p in load_pipeline("identity").passes] == ["move-units"]


def test_edit_args_and_annotations_are_deep_frozen(snap):
    held = {}

    def run(view, ctx, edits):
        uids = [u for u in view.unit_ids() if view.is_relocatable(u)][:2]
        val = {"xs": [1, 2]}
        edits.annotate(uids[0], "k", val)
        edits.layout_order("hot", uids)
        held["uids"], held["val"] = uids, val
        uids.append("tb:" + "0" * 32 + "#0")  # mutate after proposing
        val["xs"].append(3)
        return PassResult(Outcome.CANDIDATE, "x")

    P = make_pass("mut", run, capabilities=frozenset({EditKind.ANNOTATE,
                                                      EditKind.LAYOUT_ORDER}))
    cand = run_passes(snap, [P], pipeline("mut"))
    assert cand.passes[0].outcome is Outcome.CANDIDATE
    ws = cand.working_set
    assert ws.sections["hot"] == tuple(held["uids"][:2])
    assert ws.annotations(held["uids"][0])["k"]["xs"] == (1, 2)
    lo = [e for e in cand.edits if e.kind is EditKind.LAYOUT_ORDER][0]
    assert lo.args["uids"] == tuple(held["uids"][:2])
    with pytest.raises(TypeError):
        lo.args["uids"] = ()  # type: ignore[index]
