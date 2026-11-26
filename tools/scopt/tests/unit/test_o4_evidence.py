"""scripts/o4_evidence.py: the only writer of docs/o4-evidence.json.

Every row is derived from pinned sources (summary.json, plan.json, the decide.json computed
from them, and for a campaign that timed O4 under another name, the identity record), and
re-running the generator on those sources reproduces the file byte for byte.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[2] / "scripts" / "o4_evidence.py"
_SPEC = importlib.util.spec_from_file_location("o4_evidence", _PATH)
assert _SPEC and _SPEC.loader
E = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = E
_SPEC.loader.exec_module(E)

EVIDENCE = Path(__file__).parents[2] / "docs" / "o4-evidence.json"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _campaign(tmp_path, *, identity_equal=True):
    """A two-workload campaign in which O4 was timed as ``c7b3``."""
    runs, inputs, best_rows = [], [], []
    for w in ("packet", "uart"):
        src = tmp_path / "src" / w
        src.mkdir(parents=True)
        (src / "summary.json").write_text(json.dumps({"paired": {
            "s6/c7b3": {"median": 1.2, "interval": [1.1, 1.3], "verdict": "above_floor",
                        "n_blocks": 10}}}))
        (src / "plan.json").write_text(json.dumps({"arms": {"s6": {}, "c7b3": {}}}))
        (src / "observations.jsonl").write_text(f'{{"block": 0, "arm": "s6", "w": "{w}"}}\n')
        dst = tmp_path / "decide" / w
        E.adapt_round2(src, dst, "c7b3")
        inputs.append({"dir": str(dst), "workload": w, "reference": "c7b3", "chosen": True,
                       "plan_sha256": _sha(dst / "plan.json"),
                       "observations_sha256": _sha(dst / "observations.jsonl")})
        runs.append({"workload": w, "summary": str(src / "summary.json"),
                     "plan": str(src / "plan.json"),
                     "observations": str(src / "observations.jsonl"), "decide_input": str(dst)})
        best_rows.append({"arm": "s6", "scope": w, "d": -0.15, "se": 0.01, "lb95": -0.17,
                          "ub95": -0.13, "p": 1.0, "holm_rank": 3, "holm_family_size": 3,
                          "reference_bias": None, "verdict": "slower_than_o4"})
    best_rows.append({"arm": "s6", "scope": "pooled", "d": -0.15, "se": 0.007, "lb95": -0.16,
                      "ub95": -0.14, "p": 1.0, "holm_rank": 2, "holm_family_size": 3,
                      "reference_bias": None, "verdict": "slower_than_o4"})
    decide = tmp_path / "decide.json"
    decide.write_text(json.dumps({"registered": False, "inputs": inputs,
                                  "best": {"A": {"rows": best_rows}}}))
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"workloads": {
        w: {"o4_cache_sha256": "a" * 64,
            "reference_cache_sha256": "a" * 64 if identity_equal else "b" * 64}
        for w in ("uart", "packet")}}))
    return {"schema": E.SPEC_SCHEMA, "campaigns": [{
        "id": "r2", "revision": "O4-r1", "timing_md": "TIMING.md", "section": "## x",
        "reference_arm": "c7b3", "via": "c7b3, same bytes", "decide": str(decide),
        "decide_campaign": "A", "runs": runs, "identity": str(identity)}]}


def test_build_pins_every_source_and_orients_the_driver_ratio(tmp_path):
    spec = _campaign(tmp_path)
    doc = E.build(spec)
    assert (doc["schema"], doc["generator"], doc["spec"]) == (E.SCHEMA, E.GENERATOR, spec)
    pinned = {s["sha256"] for s in doc["sources"]}
    kinds = sorted(s["kind"] for s in doc["sources"])
    assert kinds == sorted(["decide", "identity"] + ["summary", "plan", "decide_plan",
                                                     "observations", "observations_original"] * 2)
    assert all(len(s["sha256"]) == 64 for s in doc["sources"])
    rows = doc["rows"]
    assert [(r["workload"], r["arm"], r["verdict"]) for r in rows] == [
        ("packet", "s6", "slower_than_o4"), ("uart", "s6", "slower_than_o4"),
        ("pooled", "s6", "slower_than_o4"),
        ("packet", "c7b3", "same_bytes"), ("uart", "c7b3", "same_bytes")]
    for r in rows:
        assert r["reference"] == "O4" and r["revision"] == "O4-r1" and r["via"]
        assert r["summary_sha256"].split(",")[0] in pinned and r["decide_sha256"] in pinned
    packet = rows[0]
    assert packet["holm_family"] == "r2:BEST" and packet["holm_rank"] == 3
    assert packet["driver"]["comparison"] == "s6/c7b3" and packet["driver"]["median"] == 1.2
    assert packet["driver"]["o4_over_arm"] == pytest.approx(1 / 1.2)
    assert rows[2]["driver"] is None
    # Deterministic: the same sources render the same bytes.
    assert E.render(E.build(spec)) == E.render(doc)


def test_build_refuses_inconsistent_sources(tmp_path):
    spec = _campaign(tmp_path)
    obs = Path(spec["campaigns"][0]["runs"][0]["decide_input"]) / "observations.jsonl"
    obs.write_text(obs.read_text() + "\n")
    with pytest.raises(ValueError, match="other inputs"):
        E.build(spec)
    spec = _campaign(tmp_path / "2", identity_equal=False)
    with pytest.raises(ValueError, match="caches differ"):
        E.build(spec)
    spec = _campaign(tmp_path / "3")
    Path(spec["campaigns"][0]["runs"][1]["summary"]).unlink()
    with pytest.raises(E.SourceMissing):
        E.build(spec)


def test_adapt_round2_keeps_the_observations_and_makes_the_arm_its_own_reference(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "plan.json").write_text(json.dumps({"arms": {"c7b3": {}, "s6": {}}, "blocks": 10,
                                               "x_same_cache_bytes": [["s6", "fl2"]],
                                               "aa_control_resolved": {"reference": "s6",
                                                                       "arm": "s6-aa"}}))
    (src / "observations.jsonl").write_bytes(b'{"x": 1}\n')
    E.adapt_round2(src, tmp_path / "dst", "c7b3")
    plan = json.loads((tmp_path / "dst" / "plan.json").read_text())
    assert plan["aa_control_resolved"] == {"reference": "c7b3", "arm": "c7b3"}
    assert (plan["x_campaign"], plan["burn_in_blocks"]) == ("A", 0)
    assert "x_same_cache_bytes" not in plan
    assert plan["x_o4_seed"]["source_plan_sha256"] == _sha(src / "plan.json")
    assert (tmp_path / "dst" / "observations.jsonl").read_bytes() == b'{"x": 1}\n'
    with pytest.raises(SystemExit):
        E.adapt_round2(src, tmp_path / "dst2", "o4")


def test_the_checked_in_evidence_is_what_the_generator_makes():
    doc = json.loads(EVIDENCE.read_text())
    assert doc["schema"] == E.SCHEMA and doc["generator"] == E.GENERATOR
    try:
        again = E.render(E.build(doc["spec"]))
    except E.SourceMissing as exc:
        pytest.skip(f"evidence source absent here: {exc}")
    assert again == EVIDENCE.read_text()
