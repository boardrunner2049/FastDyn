"""WP18 coverage fix: package_b3 / package_b4 / ``scopt publish`` publish an edited
artifact only when hostsym verdicts COVER the plan (B3/B4 design §9.1): V12 every
changed unit (grown R', every variant C' per entering link, every same-length
rewrite), V13 guard entailment every grown R' with a guarded slot, V14 every fact of
every SPEC link. Coverage is computed from the link, never from what hostsym
returned; labels for units not in the plan never count.

Synthetic verdicts (the wiring, not the kernel: the kernel is WP19's tests).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scopt.link import B3Plan, NativeLinkerV2
from tests.synth import b3 as H
from scopt.paths import fastdyn_root

pytestmark = pytest.mark.real


def _selftest_doc():
    import capstone

    from scopt.validate_sym.selftest import kernel_id

    return {"kernel_id": kernel_id(), "capstone_version": capstone.__version__,
            "native": {"status": "pass", "forms": 1, "trials": 1, "n_failures": 0},
            "mutants": {"all_rejected": True, "controls_ok": True, "rejected": 1,
                        "mutants": 1}}


def _eq():
    from scopt.validate_sym.equiv import Verdict

    return Verdict("equivalent", None, "synthetic", 1, 1)


def _fact(label, value=0):
    from scopt.validate_sym.equiv import FactCert

    return {"label": label, "cert": FactCert((0, 4), "certified", value, 1),
            "proposed": value, "provenance": {"synthetic": True}}


def _hostsym(link, *, drop=(), extra_equiv=(), facts=True):
    from scopt.validate_sym.link_check import required_hostsym

    req = required_hostsym(link)
    return {"equiv": [(lab, _eq()) for lab in req.equiv if lab not in drop] +
            [(lab, _eq()) for lab in extra_equiv],
            "entailment": [(lab, _eq()) for lab in req.entailment if lab not in drop],
            "facts": [_fact(lab) for lab in req.facts if facts and lab not in drop],
            "selftest": _selftest_doc()}


@pytest.fixture(scope="module")
def spec():
    """``_spec_case(minimal)`` of test_link_v2: one grown host R, two SPEC targets,
    two variants; ``plan_f`` is the same plan with an ``r0 == 0`` fact on each
    variant."""
    from scopt.validate_sym.link_mutants import synthetic_fact

    s = H.snap("minimal")
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts[:2]]
    grow, vs = H.spec_pairs(s, hosts[:1], cs)
    uid = next(iter(grow))
    assert len(vs) == 2
    plan = B3Plan(grow=grow, variants=tuple(vs))
    plan_f = B3Plan(grow=grow, variants=tuple(
        replace(v, facts=(synthetic_fact("r0", 0, f"cov{i}"),)) for i, v in enumerate(vs)))
    off = H.offsets_for(s)
    link = NativeLinkerV2(s, plan, offsets=off).link()
    link_f = NativeLinkerV2(s, plan_f, offsets=off).link()
    assert link.ok and link_f.ok
    return s, uid, vs, plan, link, plan_f, link_f


def _pkg(tmp_path, monkeypatch, s, plan, hs, name, **kw):
    import scopt.formats.callee_summary_v1 as CS
    from scopt.validate import package_b3

    if any(v.facts for v in plan.variants):
        # the synthetic facts cite no CalleeSummary row (V19 stale_fact otherwise);
        # V19 is not under test here
        monkeypatch.setattr(CS, "check_factref", lambda ref, rows: None)

    ri6 = H.v6_identity(monkeypatch)
    return package_b3(s, plan, tmp_path / name, candidate_id="sha256:" + name * 32,
                      runtime=ri6, v6_exes=[H.QINT_V6_SHA], hostsym=hs, **kw)


def _fail_reasons(r):
    return [x["reason"] for x in r.validation.fails()]


def test_required_labels_from_the_link(spec):
    from scopt.validate_sym.link_check import required_hostsym

    s, uid, vs, plan, link, plan_f, link_f = spec
    req = required_hostsym(link, plan)
    assert req.equiv == (f"R':{uid}",) + tuple(
        f"C':{v.src_uid}@v{i}<-{uid}|{v.slot_n}" for i, v in enumerate(vs))
    assert req.entailment == (uid,)
    assert [p for p, _ in req.links] == [f"{uid}|{v.slot_n}->v{i}" for i, v in enumerate(vs)]
    assert req.facts == ()  # spec_pairs variants declare no fact
    rf = required_hostsym(link_f, plan_f)
    assert rf.facts == tuple(f"{uid}|{v.slot_n}->v{i}:r0" for i, v in enumerate(vs))


def test_reviewer_reproduction_rejected(spec, tmp_path, monkeypatch):
    """3rd WP18 review: one unrelated V12 label, no entailment, a fact for v0 only,
    a valid self-test, allow_uncertified=False -> was 'published' with V12/V13/V14/V18
    all pass. Now rejected with a coverage reason."""
    s, uid, vs, plan, link, *_ = spec
    hs = {"equiv": [("unrelated-label", _eq())], "entailment": [],
          "facts": [_fact("v0")], "selftest": _selftest_doc()}
    r = _pkg(tmp_path, monkeypatch, s, plan, hs, "a1")
    assert r.status == "rejected", r.reason
    assert r.reason == f"V12:hostsym_coverage_missing:R':{uid}", r.reason
    reasons = _fail_reasons(r)
    for v_i, v in enumerate(vs):
        assert f"hostsym_coverage_missing:C':{v.src_uid}@v{v_i}<-{uid}|{v.slot_n}" in reasons
    assert f"hostsym_coverage_missing:{uid}" in reasons  # V13 entailment
    codes = r.validation.codes()
    assert codes["V12"] == codes["V13"] == "fail"
    # the 'no guarded slot' skip is not written: the plan has one
    assert not any(x["code"] == "V13" and x["check"] == "guard_entailment" and
                   x["result"] == "skip" for x in r.validation.rows)
    # allow_uncertified does not turn a coverage failure into a publish
    r2 = _pkg(tmp_path, monkeypatch, s, plan, hs, "a2", allow_uncertified=True)
    assert r2.status == "rejected" and "hostsym_coverage_missing" in r2.reason


def test_missing_variant_equiv_rejected(spec, tmp_path, monkeypatch):
    s, uid, vs, plan, link, *_ = spec
    lab = f"C':{vs[1].src_uid}@v1<-{uid}|{vs[1].slot_n}"
    r = _pkg(tmp_path, monkeypatch, s, plan, _hostsym(link, drop=(lab,)), "a3")
    assert r.status == "rejected" and r.reason == f"V12:hostsym_coverage_missing:{lab}", \
        r.reason


def test_missing_entailment_for_spec_slot_rejected(spec, tmp_path, monkeypatch):
    s, uid, vs, plan, link, *_ = spec
    hs = _hostsym(link)
    hs["entailment"] = []
    r = _pkg(tmp_path, monkeypatch, s, plan, hs, "a4")
    assert r.status == "rejected" and r.reason == f"V13:hostsym_coverage_missing:{uid}", \
        r.reason
    # an entailment verdict for another unit does not count either
    hs["entailment"] = [("not-" + uid, _eq())]
    r = _pkg(tmp_path, monkeypatch, s, plan, hs, "a5")
    assert r.status == "rejected" and r.reason == f"V13:hostsym_coverage_missing:{uid}", \
        r.reason


def test_fact_for_only_one_of_two_spec_links_rejected(spec, tmp_path, monkeypatch):
    s, uid, vs, plan, link, plan_f, link_f = spec
    lab1 = f"{uid}|{vs[1].slot_n}->v1:r0"
    r = _pkg(tmp_path, monkeypatch, s, plan_f, _hostsym(link_f, drop=(lab1,)), "a6")
    assert r.status == "rejected" and r.reason == f"V14:hostsym_coverage_missing:{lab1}", \
        r.reason
    # a certified fact whose proposer differs is not coverage either
    hs = _hostsym(link_f)
    hs["facts"][1] = dict(hs["facts"][1], proposed=1)
    r = _pkg(tmp_path, monkeypatch, s, plan_f, hs, "a7")
    assert r.status == "rejected" and r.reason == "V14:fact_derivation_mismatch", r.reason
    assert f"hostsym_coverage_uncertified:{lab1}" in _fail_reasons(r)


def test_unrelated_label_padding_rejected(spec, tmp_path, monkeypatch):
    """Many passing verdicts for labels not in the plan never replace the one missing."""
    s, uid, vs, plan, link, *_ = spec
    pad = [f"R':pad{i}" for i in range(8)] + [f"U':{uid}", uid, "C':x@v0",
                                              f"R':{vs[0].src_uid}"]
    r = _pkg(tmp_path, monkeypatch, s, plan,
             _hostsym(link, drop=(f"R':{uid}",), extra_equiv=pad), "a8")
    assert r.status == "rejected" and r.reason == f"V12:hostsym_coverage_missing:R':{uid}", \
        r.reason


def test_full_coverage_publishes_and_scopt_publish_rechecks(spec, tmp_path, monkeypatch):
    from scopt.commands.run_b3 import publish_directory
    from scopt.formats import artifact as A
    from scopt.validate import certification_problems

    s, uid, vs, plan, link, plan_f, link_f = spec
    r = _pkg(tmp_path, monkeypatch, s, plan_f, _hostsym(link_f), "b1")
    assert r.status == "published", r.reason
    codes = r.validation.codes()
    assert codes["V12"] == codes["V13"] == codes["V14"] == codes["V18"] == "pass"
    cov = {x["code"]: x for x in r.validation.rows if x["check"] == "coverage"}
    assert len(cov["V12"]["required"]) == 3 and cov["V13"]["required"] == [uid]
    assert len(cov["V14"]["required"]) == 2
    art = Path(r.artifact_dir)
    side = json.loads((art / A.SIDECAR).read_text())
    val = json.loads((art / A.VALIDATION).read_text())
    assert certification_problems(side, val) == []
    doc = publish_directory(str(art), str(tmp_path / "pub"))
    assert doc["artifact_id"] == r.artifact_id
    # the publish re-check: a validation whose V12 coverage omits a variant context,
    # or a sidecar naming a unit the coverage does not name, is refused
    v2 = json.loads(json.dumps(val))
    for row in v2["rows"]:
        if row["code"] == "V12" and row["check"] == "coverage":
            row["required"] = row["required"][:2]
    assert any("C' verdicts" in p for p in certification_problems(side, v2))
    s2 = dict(side, x_b3=dict(side["x_b3"], rewritten=["sha256:" + "ab" * 32]))
    assert any("coverage does not name U':" in p for p in certification_problems(s2, val))
    v3 = json.loads(json.dumps(val))
    v3["rows"] = [x for x in v3["rows"] if not (x["code"] == "V13" and
                                                x["check"] == "coverage")]
    assert "V13: no passing coverage row" in certification_problems(side, v3)
    # an identity artifact needs nothing
    assert certification_problems(dict(side, x_b3={"grown": [], "rewritten": [],
                                                   "variants": []}), {"rows": []}) == []


def test_validate_b3_coverage_without_package(spec, tmp_path, monkeypatch):
    """validate_b3 alone writes the coverage rows (pass with full coverage)."""
    from scopt.validate import uncertified_rows, validate_b3, write_b3_files

    s, uid, vs, plan, link, plan_f, link_f = spec
    d = tmp_path / "v"
    write_b3_files(d, s, link_f)
    v, _ = validate_b3(s, link_f, d, plan=plan_f, runtime=H.v6_identity(monkeypatch),
                       v6_exes=[H.QINT_V6_SHA], hostsym=_hostsym(link_f), skip_reimport=True)
    assert [x["result"] for x in v.rows if x["check"] == "coverage"] == ["pass"] * 3
    assert uncertified_rows(v, link_f, plan_f) == []
    # the same rows judged against a link with one more changed unit: not covered
    other = next(u.uid for u in s.units() if u.uid not in link_f.grown)
    bigger = replace(link_f, rewritten={other: ()})
    assert uncertified_rows(v, bigger) == ["V12"]


# ---------------------------------------------------------------------------
# B4 (eliminate-state-sync, v5 nop-fill): every rewritten unit needs its own U'
# ---------------------------------------------------------------------------

ROOT = fastdyn_root()
FAULT = ROOT / ("runs/wp15a/fault/store/snapshots/"
                "4e404d5e7335399768249ef95453594fe8098647ef7927c1234c5a27dee16841")
IDENT = ROOT / "runs/wp17/admission/fault.runtime_identity.v2.json"


@pytest.fixture(scope="module")
def b4():
    if not (FAULT / "snapshot.json").is_file() or not IDENT.is_file():
        pytest.skip("fault snapshot / identity not present")
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot
    from scopt.passes.eliminate_state_sync import propose_b4

    s = open_snapshot(FAULT)
    ri = json.loads(IDENT.read_text())
    lay = find_envlayout(ri["qemu_sha256"])
    prop = propose_b4(s, layout=lay, runtime=ri)
    assert len(prop.patches) >= 2
    return s, lay, ri, prop


def _synthetic_b4_hostsym(monkeypatch, drop=()):
    """Replace the kernel behind package_b4 with synthetic verdicts for every
    required label (from the link package_b4 built) minus ``drop``."""
    import scopt.validate_sym.link_check as LC

    def inputs(**_kw):
        return lambda snapshot, link: _hostsym(link, drop=drop)
    monkeypatch.setattr(LC, "hostsym_inputs", inputs)


def test_b4_unit_without_verdict_rejected(b4, tmp_path, monkeypatch):
    from scopt.passes.eliminate_state_sync import package_b4, plan_of
    from scopt.validate_sym.link_check import required_hostsym

    s, lay, ri, prop = b4
    uids = sorted(prop.patches)
    req = required_hostsym(type("L", (), {"rewritten": prop.patches})(), plan_of(prop))
    assert req.equiv == tuple(f"U':{u}" for u in uids) and not req.entailment
    _synthetic_b4_hostsym(monkeypatch, drop=(f"U':{uids[-1]}",))
    r = package_b4(s, prop, tmp_path / "s", candidate_id="t-b4-cov-miss", layout=lay,
                   runtime=ri, selftest=_selftest_doc())
    assert r.status == "rejected", r.reason
    assert r.reason == f"V12:hostsym_coverage_missing:U':{uids[-1]}", r.reason
    # positive control: every rewritten unit has its verdict -> published (v5)
    _synthetic_b4_hostsym(monkeypatch)
    ok = package_b4(s, prop, tmp_path / "s2", candidate_id="t-b4-cov-ok", layout=lay,
                    runtime=ri, selftest=_selftest_doc())
    assert ok.status == "published", ok.reason
    cov = next(x for x in ok.validation.rows if x["code"] == "V12" and
               x["check"] == "coverage")
    assert cov["result"] == "pass" and cov["required"] == [f"U':{u}" for u in uids]
