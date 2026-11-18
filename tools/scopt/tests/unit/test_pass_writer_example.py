"""The pass-writer example of ``docs/pass-writer.md``, kept runnable.

A minimal ANNOTATE-only pass written against the public pass API
(``scopt.passes.base``), run through ``PassManager`` on a synthetic snapshot.
It checks the three things a pass writer relies on: the manager records the
pass's outcome and edits (and the ledger row), an ablation skips the pass, and
an edit outside the descriptor's capabilities fails the pass with its edits
discarded. The richer §5.10 example (``hotcold-partition``) is in
``tests/unit/test_pass_framework.py``.
"""

from __future__ import annotations

from scopt.analysis.registry import ANNOTATIONS, AnalysisRegistry
from scopt.manager import PassManager, PassRegistry
from scopt.passes.base import Budgets, EditKind, Outcome, PassDescriptor, PassResult
from tests.synth.fake_snapshot import fake_snapshot


class TagLargeUnits:
    """Annotate every unit whose host code is at least ``min_bytes`` long."""

    descriptor = PassDescriptor(
        name="tag-large-units", version=1, unit_scope="snapshot",
        requires_capture_level=0,                     # needs no .scm metadata
        capabilities=frozenset({EditKind.ANNOTATE}),  # the only edit kind it may propose
        budgets=Budgets(max_wall_s=5.0),
        preserves=frozenset({"*"}),                   # annotations change no code/layout
        params_schema={"min_bytes": int})

    def run(self, snap, ctx, edits) -> PassResult:
        limit = ctx.params.get("min_bytes", 64)
        big = [u.uid for u in snap.units() if u.tc_size >= limit]
        if not big:
            return PassResult(Outcome.NO_CHANGE, "no_large_units")
        for uid in big:
            edits.annotate(uid, "size_class", "large")
        return PassResult(Outcome.CANDIDATE, "tagged", affected=tuple(sorted(big)),
                          stats={"tagged": float(len(big))})


class TagThenReorder(TagLargeUnits):
    """Same pass, but it also tries a LAYOUT_ORDER edit it did not declare."""

    descriptor = PassDescriptor(
        name="tag-then-reorder", version=1, requires_capture_level=0,
        capabilities=frozenset({EditKind.ANNOTATE}), params_schema={"min_bytes": int})

    def run(self, snap, ctx, edits) -> PassResult:
        res = super().run(snap, ctx, edits)
        edits.layout_order("hot", [u.uid for u in snap.units()])  # not in capabilities
        return res


def _pipeline(name, caps=("ANNOTATE",), params=None):
    return {"schema": "scopt.pipeline", "version": 1, "name": "example",
            "allowed_capabilities": list(caps),
            "pass": [{"name": name, "version": 1, "params": params or {}}]}


def _run(snap, name, **kw):
    reg = PassRegistry([TagLargeUnits, TagThenReorder])
    return PassManager(snap, _pipeline(name, params={"min_bytes": 1}), passes=reg,
                       analyses=AnalysisRegistry([ANNOTATIONS]), **kw).execute()


def test_example_pass_proposes_annotations():
    snap = fake_snapshot(6)
    cand = _run(snap, "tag-large-units")
    (rec,) = cand.passes
    assert rec.outcome is Outcome.CANDIDATE and rec.reason == "tagged"
    assert rec.n_edits == len(snap.units()) == len(cand.edits)
    assert all(e.kind is EditKind.ANNOTATE for e in cand.edits)
    assert cand.working_set.annotations(snap.uids()[0]) == {"size_class": "large"}
    assert not cand.has_layout_changes
    (row,) = cand.ledger
    assert (row.stage, row.verdict, row.id) == ("optimized", "candidate", "tag-large-units@1")


def test_example_pass_ablation_skips_it():
    snap = fake_snapshot(6)
    cand = _run(snap, "tag-large-units", disable=("tag-large-units",))
    (rec,) = cand.passes
    assert rec.outcome is Outcome.SKIPPED_UNSUPPORTED and rec.reason == "ablation_disabled"
    assert cand.edits == ()


def test_edit_outside_capabilities_fails_and_discards_edits():
    snap = fake_snapshot(6)
    cand = _run(snap, "tag-then-reorder")
    (rec,) = cand.passes
    assert rec.outcome is Outcome.FAILED and rec.reason == "edit_capability"
    assert cand.edits == ()
