"""WP11 acceptance (synthetic snapshot + profile): B2 analyses and passes.

* ``hotness`` rule: unknown is never cold, lower_bound is priority only;
* ``cluster-hot``: the Pettis-Hansen order equals a hand-computed result on a
  6-unit graph (flagged edges ignored, chain edges preferred on ties, the
  tail->head merge rule);
* the edit log is deterministic (byte-identical over two runs);
* pinned / no_meta units never appear in an order request;
* ``--disable cluster-hot`` gives a different but valid order; every
  ablation of ``b2-layout`` gives a published, V1-V8-valid candidate;
* the locality model (labelled as a model) is in the sidecar and in the report;
* ``inspected`` unit rows of several candidates are counted once in the report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scopt.analysis.hotness import classify
from scopt.analysis.locality import LABEL, locality_model, snapshot_layout
from scopt.formats.profile_v1 import Count, Profile, ProfileTarget, ProfileView
from scopt.link import NativeLinker
from scopt.manager import PassManager, load_pipeline
from scopt.passes.base import Outcome
from scopt.passes.cluster import edge_weights, pettis_hansen
from scopt.report.collect import collect
from scopt.report.render import render_markdown
from scopt.validate import optimize
from tests.synth import builders as B
from tests.synth import rich_tb as RT
from tests.synth.fake_snapshot import fake_snapshot

PID = "sha256:" + "9" * 64


def C(v, st="exact", reason=None):
    return Count(v, st, reason)


# ---------------------------------------------------------------------------
# synthetic profile
# ---------------------------------------------------------------------------

def profile_view(snap, steady: dict, *, boot: dict | None = None, edges=(), status=None,
                 phases=("boot", "steady"), pid=PID):
    """A ProfileView over ``snap``'s units. ``steady``/``boot``: {uid: entries};
    ``status``: {uid: status} (default exact); a uid absent from ``steady`` has 0."""
    uids = [u.uid for u in snap.units()]
    boot = boot or {}
    status = status or {}
    units = []
    for u in uids:
        ph = {"0": {"entries": boot.get(u, 1), "aborted": 0, "longjmp": 0, "lookup_hits": 0}}
        if "steady" in phases:
            ph["1"] = {"entries": steady.get(u, 0), "aborted": 0, "longjmp": 0,
                       "lookup_hits": 0}
        units.append({"unit": u, "status": status.get(u, "exact"), "phase": ph,
                      "vaddrs": []})
    erows = []
    for e in edges:
        src, dst, kind, n = e[:4]
        flags = list(e[4]) if len(e) > 4 else []
        erows.append({"src": src, "dst": dst, "kind": kind, "flags": flags, "phase": 1,
                      "count": n, "status": "exact"})
    doc = {"schema": "fastdyn.profile", "version": 1, "profile_id": pid,
           "snapshot_id": snap.snapshot_id, "bundle_id": None, "contract_id": None,
           "runtime_identity_id": None,
           "collection": {"method": "exact_dispatch_unchained_v1", "complete": True},
           "phases": [{"id": i, "name": n, "reached": True} for i, n in enumerate(phases)],
           "domain": {"kind": "snapshot_units", "count": len(uids)},
           "units": units, "edges": erows, "profile_only": [], "transient": {"entries": 0}}
    prof = Profile(doc, validate=False)
    return ProfileView(prof, ProfileTarget(snap.snapshot_id, tuple(uids)), coverage=1.0,
                       coverage_phase="steady", same_snapshot=True)


def b2(**params):
    """The b2-layout pipeline with param overrides {pass: {...}}."""
    return load_pipeline("b2-layout"), params


def run_b2(snap, views, *, disable=(), only=None, params=None):
    return PassManager(snap, "b2-layout", views, params, disable=disable, only=only).execute()


# ---------------------------------------------------------------------------
# hotness rule
# ---------------------------------------------------------------------------

def test_classify_rules_unknown_never_cold_lower_bound_priority_only():
    uids = ["h1", "h2", "w", "b", "z", "lbp", "lb0", "unk", "nid"]
    steady = {"h1": C(90), "h2": C(9), "w": C(1), "b": C(0), "z": C(0),
              "lbp": C(500, "lower_bound"), "lb0": C(0, "lower_bound"),
              "unk": C(None, "unknown", "phase_not_reached")}
    boot = {"b": C(3), "z": C(0), "lb0": C(7)}
    h = classify(uids, steady, boot, coverage=0.99)
    assert h.classes == {"h1": "hot", "h2": "hot", "w": "warm", "b": "boot_only",
                         "z": "cold_zero", "lbp": "warm", "lb0": "unknown", "unk": "unknown",
                         "nid": "unknown"}
    # the hot prefix only uses exact counts: total 100, h1+h2 = 99 covers 0.99
    assert h.hot_order == ("h1", "h2") and h.exact_total == 100 and h.hot_entries == 99
    # no status other than an exact 0 is ever cold
    for u, cls in h.classes.items():
        if cls in ("cold_zero", "boot_only"):
            assert steady[u].status == "exact" and steady[u].value == 0
    assert classify(uids, steady, boot, coverage=0.9).hot_order == ("h1",)
    with pytest.raises(ValueError):
        classify(uids, steady, boot, coverage=0.0)


# ---------------------------------------------------------------------------
# Pettis-Hansen on a 6-unit graph (hand-computed)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def six():
    snap = fake_snapshot(6)
    A, Bu, Cu, D, E, F = [u.uid for u in snap.units()]
    names = {A: "A", Bu: "B", Cu: "C", D: "D", E: "E", F: "F"}
    steady = {A: 10, Bu: 100, Cu: 50, D: 80, E: 60, F: 30}
    edges = [
        (Bu, D, "chain0", 70),        # 1: [B,D]
        (D, Bu, "lookup_hit", 70),    # 2: tie on weight, chain first; same chain: skip
        (D, E, "chain1", 40),         # 3: [B,D,E]
        (Cu, F, "chain0", 30),        # 4: [C,F]
        (E, F, "chain0", 25),         # 5: F is not a chain head: skip
        (E, Cu, "lookup_hit", 25),    # 6: [B,D,E,C,F]
        (A, Cu, "chain0", 9),         # 7: C is not a head: skip -> A stays alone
        (A, Bu, "chain0", 500, ("irq",)),  # flagged: ignored (else A would lead)
        (None, A, "first", 1),        # first edge: ignored
        (F, F, "chain0", 99),         # self loop: ignored
    ]
    view = profile_view(snap, steady, edges=edges)
    return snap, view, names


def test_pettis_hansen_hand_computed(six):
    snap, view, names = six
    uids = [u.uid for u in snap.units()]
    w = edge_weights(view.edges("steady"), set(uids))
    heat = {u: c.value for u, c in view.phase("steady").items()}
    chains, merged = pettis_hansen(uids, w, heat)
    assert [[names[u] for u in c] for c in chains] == [["B", "D", "E", "C", "F"], ["A"]]
    assert merged == 70 + 40 + 30 + 25
    # through the pipeline: cluster-hot's order is the same
    cand = run_b2(snap, [view], params={"hotcold-partition": {"coverage": 1.0}})
    rec = {p.name: p for p in cand.passes}
    assert rec["hotcold-partition"].outcome is Outcome.CANDIDATE
    assert rec["cluster-hot"].outcome is Outcome.CANDIDATE, rec["cluster-hot"]
    assert [names[u] for u in cand.working_set.sections["hot"]] == list("BDECFA")
    assert rec["cluster-hot"].stats["chains"] == 2
    # order-cold has nothing left: every unit is hot and laid out already
    assert rec["order-cold"].outcome is Outcome.NO_CHANGE


def test_edit_log_is_deterministic(six):
    snap, view, _ = six
    p = {"hotcold-partition": {"coverage": 0.9}}
    a = run_b2(snap, [view], params=p)
    b = run_b2(snap, [view], params=p)
    assert a.edit_log_jsonl() == b.edit_log_jsonl() and len(a.edits) > 6
    assert a.candidate_id == b.candidate_id


def test_disable_cluster_gives_slot_order_hot_section(six):
    snap, view, names = six
    p = {"hotcold-partition": {"coverage": 1.0}}
    full = run_b2(snap, [view], params=p)
    nocl = run_b2(snap, [view], params=p, disable=["cluster-hot"])
    assert [names[u] for u in nocl.working_set.sections["hot"]] == list("ABCDEF")
    assert nocl.working_set.sections["hot"] != full.working_set.sections["hot"]
    assert NativeLinker(nocl, derive_captures=False).plan() != \
        NativeLinker(full, derive_captures=False).plan()


def test_sections_unknown_never_cold_and_pinned_never_ordered():
    # units 0..8; 2 not relocatable, 5 no_meta; both hot
    snap = fake_snapshot(9, not_relocatable=[2], no_meta=[5])
    u = [x.uid for x in snap.units()]
    steady = {u[0]: 500, u[1]: 300, u[2]: 400, u[5]: 350, u[3]: 2, u[4]: 0, u[6]: 0, u[7]: 5}
    status = {u[6]: "lower_bound", u[8]: "lower_bound"}  # u6 lb 0 -> unknown; u8 lb 0
    boot = {u[4]: 0, u[6]: 0, u[8]: 0}
    view = profile_view(snap, steady, boot=boot, status=status,
                        edges=[(u[0], u[1], "chain0", 100)])
    cand = run_b2(snap, [view], params={"hotcold-partition": {"coverage": 0.95}})
    ws = cand.working_set
    ann = {x: ws.annotations(x).get("hotness") for x in u}
    assert ann[u[2]] == "hot" and ann[u[5]] == "hot"
    assert ann[u[6]] == "unknown" and ann[u[8]] == "unknown"
    assert ann[u[4]] == "cold_zero"
    ordered = [x for s in ws.sections.values() for x in s]
    assert u[2] not in ordered and u[5] not in ordered
    for e in cand.edits:
        if e.kind.name == "LAYOUT_ORDER":
            assert u[2] not in e.units and u[5] not in e.units
    assert u[6] in ws.sections["unknown"] and u[8] in ws.sections["unknown"]
    assert not (set(ws.sections["cold"]) & {u[6], u[8]})
    ch = cand.outcome("cluster-hot")
    # F-B2-4: reported in ``affected`` as skipped_unsupported:<reason>:<UnitId>
    assert f"skipped_unsupported:unexplained_reference:{u[2]}" in ch.affected
    assert f"skipped_unsupported:no_meta:{u[5]}" in ch.affected
    assert "meta_no_meta" not in " ".join(ch.affected)
    assert "skipped_unsupported:unexplained_reference x1" in ch.detail
    # order-cold leaves the hot ones to cluster-hot's report only when it would not
    # order them; with its hot fallback it reports them too (it skips them as well)
    oc = cand.outcome("order-cold")
    assert f"skipped_unsupported:no_meta:{u[5]}" in oc.affected
    # with cluster-hot disabled, order-cold (hot fallback) reports them in affected
    nocl = run_b2(snap, [view], params={"hotcold-partition": {"coverage": 0.95}},
                  disable=["cluster-hot"])
    oc2 = nocl.outcome("order-cold")
    assert f"skipped_unsupported:unexplained_reference:{u[2]}" in oc2.affected
    assert f"skipped_unsupported:no_meta:{u[5]}" in oc2.affected
    for e in nocl.edits:
        if e.kind.name == "LAYOUT_ORDER":
            assert u[2] not in e.units and u[5] not in e.units
    # the hottest relocatable chain leads
    assert ws.sections["hot"][:2] == (u[0], u[1])
    plan = NativeLinker(cand, derive_captures=False).plan()
    assert plan[u[2]] == snap.unit(u[2]).tb_offset and plan[u[5]] == snap.unit(u[5]).tb_offset


def test_no_profile_or_no_exact_or_no_hotness():
    snap = fake_snapshot(4)
    c0 = run_b2(snap, [])
    assert [(p.outcome, p.reason) for p in c0.passes] == [
        (Outcome.SKIPPED_UNKNOWN, "missing_profile"), (Outcome.SKIPPED_UNKNOWN, "missing_profile"),
        (Outcome.SKIPPED_UNKNOWN, "missing_hotness")]
    assert not c0.has_layout_changes
    uids = [u.uid for u in snap.units()]
    lb = profile_view(snap, {x: 5 for x in uids}, status={x: "lower_bound" for x in uids})
    c1 = run_b2(snap, [lb])
    assert (c1.passes[0].outcome, c1.passes[0].reason) == (Outcome.SKIPPED_UNKNOWN,
                                                           "no_exact_counts")
    assert not c1.has_layout_changes
    ok = profile_view(snap, {uids[0]: 5})
    c2 = run_b2(snap, [ok], disable=["hotcold-partition"])
    assert [p.reason for p in c2.passes] == ["ablation_disabled", "missing_hotness",
                                             "missing_hotness"]
    assert not c2.has_layout_changes


# ---------------------------------------------------------------------------
# locality model
# ---------------------------------------------------------------------------

def test_locality_model_numbers_and_invariance():
    # three units of 256 B ([tb, end)), code 64 B at tb+192
    lay = {"a": (0, 192, 64, 256), "b": (8192, 8384, 64, 8448), "c": (256, 448, 64, 512)}
    counts = {"a": C(60), "b": C(30), "c": C(10), "d": C(None, "unknown", "not_in_domain")}
    lay["d"] = (512, 704, 64, 768)
    m = locality_model(lay, counts, edges=[
        {"src": "a", "dst": "b", "kind": "chain0", "flags": [], "count": 30, "status": "exact"},
        {"src": "a", "dst": "c", "kind": "chain1", "flags": [], "count": 10, "status": "exact"},
        {"src": "a", "dst": "c", "kind": "chain1", "flags": ["irq"], "count": 999}])
    assert m["label"] == LABEL and "MODEL" in m["label"]
    cov = {r["coverage"]: r for r in m["coverage"]}
    assert cov[0.5]["units"] == 1 and cov[0.5]["entries"] == 60
    assert cov[0.9]["units"] == 2 and cov[0.9]["pages4k"] == 2 and cov[0.9]["lines64"] == 8
    assert cov[0.99]["units"] == 3 and cov[0.99]["hot_bytes"] == 768
    assert cov[0.99]["hot_code_bytes"] == 192 and cov[0.99]["lines64_code"] == 3
    assert m["non_exact"]["unknown"] == {"units": 1, "reasons": {"not_in_domain": 1}}
    e = m["edges"]
    assert e["weight"] == 40 and e["adjacent_fraction"] == pytest.approx(0.25)
    assert e["weighted_mean_distance_bytes"] == pytest.approx((30 * (8192 - 256)) / 40)
    # b moved next to c: pages drop, lines (whole 64 B multiples) stay
    lay2 = dict(lay, b=(512, 704, 64, 768), d=(8192, 8384, 64, 8448))
    m2 = locality_model(lay2, counts)
    c2 = {r["coverage"]: r for r in m2["coverage"]}
    assert c2[0.9]["pages4k"] == 1 and c2[0.9]["lines64"] == cov[0.9]["lines64"]


def test_snapshot_layout_moves_whole_units():
    snap = fake_snapshot(3)
    u0 = snap.units()[0]
    lay = snapshot_layout(snap.units(), {u0.uid: u0.tb_offset + 4096})
    tb, tc, sz, end = lay[u0.uid]
    assert tb == u0.tb_offset + 4096 and tc - tb == u0.tc_offset - u0.tb_offset
    assert end - tb == u0.end_offset - u0.tb_offset and sz == u0.tc_size


# ---------------------------------------------------------------------------
# end to end: link, validate, publish, report
# ---------------------------------------------------------------------------

@pytest.fixture
def real_like(tmp_path):
    from tests.unit.test_wp10_link import make_snapshot

    units = RT.rich_units(4) + B.simple_units(4, start=64 * 120)
    snap = make_snapshot(tmp_path, units)
    u = [x.uid for x in snap.units()]
    steady = {u[0]: 5, u[1]: 0, u[2]: 900, u[3]: 3, u[4]: 0, u[5]: 400, u[6]: 0, u[7]: 600}
    boot = {u[1]: 4, u[4]: 0, u[6]: 0}
    view = profile_view(snap, steady, boot=boot, status={u[6]: "lower_bound"},
                        edges=[(u[7], u[2], "chain0", 500), (u[2], u[5], "lookup_hit", 350),
                               (u[5], u[7], "chain1", 300)])
    return snap, view, tmp_path


def _codes(res):
    return res.validation.codes()


def test_b2_layout_and_every_ablation_publish_valid_candidates(real_like):
    snap, view, tmp = real_like
    store = tmp / "store-b2"
    runs = {"full": (), "no-cluster": ("cluster-hot",), "no-order": ("order-cold",),
            "no-hotcold": ("hotcold-partition",)}
    res = {}
    for name, dis in runs.items():
        r = optimize(snap, "b2-layout", store, profiles=[view], disable=dis,
                     params={"hotcold-partition": {"coverage": 0.9}})
        assert r.published, (name, r.reason, r.validation and r.validation.fails())
        assert all(_codes(r)[f"V{i}"] == "pass" for i in range(1, 9)), (name, _codes(r))
        res[name] = r
    u = [x.uid for x in snap.units()]
    full = res["full"]
    moved = {lu.uid: lu.new_tb_offset for lu in full.link.units}
    floor = B.round_up(snap.header.prologue_end, 64)
    # hot: u2 (900), u7 (600), u5 (400) cover 0.9 of 1908 (1500 < 1717.2 needs u5);
    # edges u7->u2 (500), u2->u5 (350) -> chain [u7, u2, u5] first
    assert moved[u[7]] == floor
    assert moved[u[2]] > moved[u[7]]
    # slot order kept
    assert [lu.uid for lu in full.link.units] == u
    # ablations: different layouts (not just different ids), except hotcold (degenerate)
    assert res["no-hotcold"].link.move_map() == {}
    nocl = {lu.uid: lu.new_tb_offset for lu in res["no-cluster"].link.units}
    assert nocl[u[2]] == floor and nocl[u[7]] > nocl[u[2]]
    noord = {lu.uid: lu.new_tb_offset for lu in res["no-order"].link.units}
    assert len({tuple(sorted(m.items())) for m in (moved, nocl, noord)}) == 3
    # without order-cold only the hot units move; the rest stay at their offsets
    assert set(res["no-order"].link.move_map()) <= {u[2], u[5], u[7]}
    assert full.candidate.working_set.sections["hot"] == (u[7], u[2], u[5])
    # x_locality: the model is in the sidecar, input vs candidate
    side = json.loads((tmp / "store-b2" / "artifacts" / full.artifact_id.split(":")[1] /
                       "cache.sc.scopt.json").read_text())
    loc = side["x_locality"]
    assert loc["status"] == "ok" and "MODEL" in loc["label"]
    assert {r["coverage"] for r in loc["input"]["coverage"]} == {0.5, 0.9, 0.99}
    assert loc["input"]["non_exact"]["lower_bound"]["units"] == 1
    assert loc["candidate"]["exact_entries"] == loc["input"]["exact_entries"]
    assert [d["lines64"] for d in loc["delta"]] == [0, 0, 0]
    # report: locality section, inspected rows counted once over 4 candidates
    model = collect([str(store)])
    md = render_markdown(model)
    assert "Static locality model per artifact (MODEL" in md
    assert model["ledger"]["units"]["inspected"] == {"ok_relocatable": len(u)}
    assert len(model["ledger"]["units_by_source"]) == 4
    assert "per ledger file" in md
    locs = [a for a in model["artifacts"] if a.get("locality")]
    assert len(locs) == 4 and {a["pipeline_name"] for a in locs} == {"b2-layout"}


def test_b2_pass_records_in_sidecar_pipeline(real_like):
    snap, view, tmp = real_like
    r = optimize(snap, "b2-layout", tmp / "s", profiles=[view])
    assert r.published
    rows = {p.name: p for p in r.candidate.passes}
    assert rows["hotcold-partition"].outcome is Outcome.CANDIDATE
    assert rows["cluster-hot"].outcome is Outcome.CANDIDATE
    assert rows["order-cold"].outcome is Outcome.CANDIDATE
    # optimize without a profile: no x_locality, nothing moves, still valid
    r0 = optimize(snap, "b2-layout", tmp / "s0")
    assert r0.published and r0.link.move_map() == {}
    side = json.loads((tmp / "s0" / "artifacts" / r0.artifact_id.split(":")[1] /
                       "cache.sc.scopt.json").read_text())
    assert "x_locality" not in side


def test_hotness_and_locality_analyses_through_the_store(six):
    from scopt.analysis.api import ScopeRef
    from scopt.analysis.registry import default_registry
    from scopt.analysis.store import AnalysisStore
    from scopt.model.view import snapshot_view

    snap, view, names = six
    reg = default_registry()
    assert reg.has("hotness", 1) and reg.has("locality", 1)
    store = AnalysisStore(snapshot_view(snap), reg, profiles=[view])
    sref = ScopeRef("snapshot", snap.snapshot_id)
    h = store.get("hotness", sref).value
    # defaults: phase steady, coverage 0.99 over 330 entries -> every unit but A (10)?
    # 100+80+60+50+30 = 320 < 326.7, so A is needed too: all six hot
    assert set(h.classes.values()) == {"hot"} and len(h.hot_order) == 6
    loc = store.get("locality", sref).value
    assert "MODEL" in loc["label"] and loc["exact_entries"] == 330
    assert loc["edges"]["weight"] == 70 + 70 + 40 + 30 + 25 + 25 + 9


# ---------------------------------------------------------------------------
# review follow-ups: one profile for every B2 consumer; per-candidate pass ledger
# ---------------------------------------------------------------------------

PID_BOOT = "sha256:" + "8" * 64


def test_boot_phase_parameter_controls_admission_and_profile_receipts():
    """A boot-only capture must be usable when both profile consumers request boot."""
    snap = fake_snapshot(4)
    uids = [unit.uid for unit in snap.units()]
    boot_only = profile_view(snap, {}, boot={uid: 3 for uid in uids},
                             phases=("boot",), pid=PID_BOOT)
    candidate = run_b2(
        snap,
        [boot_only],
        params={
            "hotcold-partition": {"phase": "boot", "coverage": 1.0},
            "cluster-hot": {"phase": "boot"},
        },
    )

    hotcold = candidate.outcome("hotcold-partition")
    cluster = candidate.outcome("cluster-hot")
    assert hotcold.outcome is Outcome.CANDIDATE, (hotcold.reason, hotcold.detail)
    assert cluster.outcome is Outcome.CANDIDATE, (cluster.reason, cluster.detail)
    assert hotcold.profiles_used == (PID_BOOT,)
    assert cluster.profiles_used == (PID_BOOT,)
    assert set(candidate.working_set.sections["hot"]) == set(uids)


def test_several_profiles_every_b2_consumer_uses_the_phase_profile(real_like):
    """A boot-only profile attached first must not hide the steady one from
    hotcold-partition (profile.counts@1 is computed from the first profile)."""
    snap, view, tmp = real_like
    uids = [x.uid for x in snap.units()]
    boot_only = profile_view(snap, {}, boot={x: 3 for x in uids}, phases=("boot",),
                             pid=PID_BOOT)
    one = optimize(snap, "b2-layout", tmp / "s1", profiles=[view],
                   params={"hotcold-partition": {"coverage": 0.9}})
    two = optimize(snap, "b2-layout", tmp / "s2", profiles=[boot_only, view],
                   params={"hotcold-partition": {"coverage": 0.9}})
    assert one.published and two.published
    hc = two.candidate.outcome("hotcold-partition")
    assert hc.outcome is Outcome.CANDIDATE, (hc.reason, hc.detail)
    assert PID in hc.profiles_used and PID in two.candidate.outcome("cluster-hot").profiles_used
    assert two.link.move_map() == one.link.move_map()
    ws1, ws2 = one.candidate.working_set, two.candidate.working_set
    assert {x: ws2.annotations(x).get("hotness") for x in uids} == \
        {x: ws1.annotations(x).get("hotness") for x in uids}
    side = json.loads((Path(two.artifact_dir) / "cache.sc.scopt.json").read_text())
    assert side["x_locality"]["status"] == "ok" and side["x_locality"]["profile_id"] == PID
    # and in the other order
    three = optimize(snap, "b2-layout", tmp / "s3", profiles=[view, boot_only],
                     params={"hotcold-partition": {"coverage": 0.9}})
    assert three.link.move_map() == one.link.move_map()


def test_locality_failure_never_fails_packaging(real_like, monkeypatch):
    import scopt.validate as V

    snap, view, tmp = real_like

    def boom(*a, **k):
        raise RuntimeError("unexpected profile view")
    monkeypatch.setattr(V, "locality_sidecar", boom)
    r = optimize(snap, "b2-layout", tmp / "s", profiles=[view])
    assert r.published
    side = json.loads((Path(r.artifact_dir) / "cache.sc.scopt.json").read_text())
    assert side["x_locality"]["status"] == "not_run"
    assert "unexpected profile view" in side["x_locality"]["reason"]


def _matrix(model):
    """{pipeline label: (pass verdicts...)} from the report's per-candidate pass rows."""
    label = {}
    for a in model["artifacts"]:
        name = a.get("pipeline_name") or "?"
        dis = (a.get("ablation") or {}).get("disable")
        label[a["artifact_id"]] = name + (" -" + ",".join(dis) if dis else "")
    out = {}
    for sc, v in model["ledger"]["passes_by_scope"].items():
        out[label[sc]] = tuple((pid, c["verdict"], c["reason"]) for pid, c in v["passes"])
    return out


def test_report_pass_rows_are_per_candidate_and_order_independent(real_like):
    snap, view, tmp = real_like
    order_a = [(), ("cluster-hot",), ("order-cold",), ("hotcold-partition",)]
    models = []
    for name, order in (("a", order_a), ("b", order_a[::-1])):
        store = tmp / f"store-{name}"
        for dis in order:
            r = optimize(snap, "b2-layout", store, profiles=[view], disable=dis,
                         params={"hotcold-partition": {"coverage": 0.9}})
            assert r.published
        models.append(collect([str(store)]))
    ma, mb = models
    # every pass row is scoped to one artifact; no cell aggregates candidates
    for m in models:
        arts = {a["artifact_id"] for a in m["artifacts"]}
        prow = [t for t in m["ledger"]["table"] if t["subject"] == "pass"]
        assert len(prow) == 12 and {t["scope"] for t in prow} == arts
        assert all(t["stages"]["optimized"]["rows"] == 1 for t in prow)
        # the report matches each artifact's own ledger.jsonl
        for a in m["artifacts"]:
            own = [json.loads(x) for x in (Path(a["path"]) / "ledger.jsonl").read_text()
                   .splitlines() if '"subject":"pass"' in x]
            got = m["ledger"]["passes_by_scope"][a["artifact_id"]]["passes"]
            assert [(r["id"], r["verdict"], r["reason"]) for r in own] == \
                [(pid, c["verdict"], c["reason"]) for pid, c in got]
    assert _matrix(ma) == _matrix(mb)
    mx = _matrix(ma)
    assert mx["b2-layout"] == (("hotcold-partition@1", "candidate", "classified"),
                               ("cluster-hot@1", "candidate", "clustered"),
                               ("order-cold@1", "candidate", "ordered"))
    assert mx["b2-layout -hotcold-partition"][0][1:] == ("skipped_unsupported",
                                                          "ablation_disabled")
    md = render_markdown(ma)
    assert "Pass outcomes per candidate" in md and "(4×)" not in md
    assert "b2-layout --disable cluster-hot" in md


def test_pass_scopes_of_a_merged_ledger_and_mixed_cells(tmp_path):
    from scopt.report.collect import _ledger
    from scopt.report.render import _ledger_cell

    def row(subject, id_, stage, verdict, ts, reason=""):
        return json.dumps({"subject": subject, "id": id_, "stage": stage, "verdict": verdict,
                           "reason": reason, "evidence_ref": None, "ts": ts})
    ca, cb = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    lines = [row("pass", "p@1", "optimized", "candidate", "2026-01-01T00:00:01.000Z"),
             row("artifact", ca, "inspected", "pass", "2026-01-01T00:00:02.000Z"),
             row("pass", "p@1", "optimized", "skipped_unknown", "2026-01-01T00:00:03.000Z"),
             row("artifact", cb, "inspected", "pass", "2026-01-01T00:00:04.000Z"),
             row("pass", "p@1", "optimized", "no_change", "2026-01-01T00:00:05.000Z")]
    f = tmp_path / "merged-ledger.jsonl"
    f.write_text("\n".join(lines) + "\n")
    led = _ledger([str(f)], [], aliases={cb: ["sha256:" + "c" * 64]})
    pr = {t["scope"]: t["stages"]["optimized"]["verdict"] for t in led["table"]
          if t["subject"] == "pass"}
    assert pr == {ca: "candidate", "sha256:" + "c" * 64: "skipped_unknown", None: "no_change"}
    assert _ledger_cell({"verdict": "b", "rows": 3, "verdicts": {"a": 2, "b": 1}}) == \
        "a ×2, b ×1 (latest b)"
    assert _ledger_cell({"verdict": "a", "rows": 2, "verdicts": {"a": 2}}) == "a (2×)"
