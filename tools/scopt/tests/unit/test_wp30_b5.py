"""WP30: the B5 campaign definition, the freeze checker, the derived ``scopt run``
campaigns and the B5 report section.

* the definition covers every arm of design §10.1 (built, reused or ``skipped_unsupported``
  with a reason), per-pass and per-class ablations, train/heldout/final splits, >= 30
  interleaved reps, distinct seeds, warm and every ablation parent in each set;
* ``check`` passes on an untouched synthetic frozen campaign and names every changed
  identity (artifact, profile, input, firmware mtime, QEMU, campaign file, reps < 30,
  parent missing, duplicate seed);
* a set's derived ``scopt run`` TOML loads through ``scopt.run.arms.load_campaign`` with
  frozen sha256 values, parents and the ``unsupported`` rows;
* ``run`` refuses more than 4 CPUs before taking the lock;
* ``scopt report --b5-campaign`` renders every B5 row from one command, including
  ``skipped_unsupported`` and excluded arms, and keeps foreign campaigns apart;
* on real gate-binary artifacts (skipped when absent) the lookup and store MODEL reproduce
  the passes' own counts.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scopt.ids import artifact_id
from scopt.report.b5 import b5_section, load_b5, render_b5
from scopt.paths import fastdyn_root

WT = Path(__file__).resolve().parents[2]
DEF = WT / "campaigns/b5-freertos.def.toml"
FROZEN = WT / "campaigns/b5-freertos.toml"
_SPEC = importlib.util.spec_from_file_location("b5_campaign",
                                               WT / "tests/qemu/b5_campaign.py")
bc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bc)

SECTION_10_1 = {"nocache", "empty", "warm", "identity", "identity-v6", "layout", "b3-chain",
                "b3-static", "b3-spec", "b4", "semantic", "combined"}
B3_PASSES = ("specialize-returns", "constant-propagation", "branch-folding", "dead-host-ops",
             "chain-returns", "chain-static")


# --------------------------------------------------------------------------
# the definition
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def defn() -> dict:
    return load_b5(DEF)


def test_definition_covers_section_10_1(defn):
    arms = {a["name"] for a in defn["arms"]}
    unsup = {u["name"]: u for u in defn["skipped_unsupported"]}
    assert SECTION_10_1 <= arms | set(unsup)
    for n in ("semantic", "combined"):
        assert unsup[n]["reason"] == "capability_not_enabled" and unsup[n]["detail"]
    # the task's arm list: nocache, empty, warm, identity, layout, semantic (B3), state
    # (B4), combined, per-pass ablations
    assert {"b3", "b4", "sfb"} <= arms


def test_definition_ablations(defn):
    by = {a["name"]: a for a in defn["arms"]}
    for p in B3_PASSES:
        a = by[f"b3-no-{p}"]
        assert a["parent"] == "b3" and a["ablated_pass"] == p and a["role"] == "ablation"
        assert a["source"] == f"build:b3 --disable {p}"
    for c in ("D1", "D2", "D3"):
        assert by[f"b4-{c}"]["parent"] == "b4"
    for p in ("cluster-hot", "order-cold"):
        assert by[f"layout-no-{p}"]["parent"] == "layout"
    unsup = {u["name"] for u in load_b5(DEF)["skipped_unsupported"]}
    for p in (*B3_PASSES, "eliminate-state-sync"):
        assert f"semantic-no-{p}" in unsup


def test_definition_sets_and_schedule(defn):
    sets = defn["sets"]
    assert {s["split"] for s in sets} == {"train", "heldout", "final"}
    assert all(int(s["reps"]) >= 30 for s in sets)
    assert len({s["seed"] for s in sets}) == len(sets)
    by = {a["name"]: a for a in defn["arms"]}
    for s in sets:
        assert "warm" in s["arms"]
        for n in s["arms"]:
            assert n in by, n
            if by[n].get("parent"):
                assert by[n]["parent"] in s["arms"], (s["name"], n)
        assert Path(s["input_set"]).name == "freertos-mps2-compute"
        assert f"/inputs/{s['split']}/" in s["input_set"]
    for sp in ("heldout", "final"):
        covered = set().union(*(s["arms"] for s in sets if s["split"] == sp))
        assert covered == set(by), set(by) - covered
    # final runs last
    order = [s["split"] for s in sets]
    assert order.index("final") > max(i for i, x in enumerate(order) if x != "final") - 2
    assert order[-1] == "final"


def test_definition_labels_and_priorities(defn):
    by = {a["name"]: a for a in defn["arms"]}
    assert by["sfb"]["category"] == "baseline_tcg_fix"
    assert "never counted as caching" in by["sfb"]["note"]
    # WP16 R11: lookup removal first; D1/B4 and b3-spec null-expected
    assert {by[n]["priority"] for n in ("b3-chain", "b3-static", "b3")} == {1}
    assert by["b4"]["priority"] == 3 and by["b3-spec"]["priority"] == 3
    raw = tomllib.loads(DEF.read_text())
    for a in raw["arm"]:
        if a.get("source", "").startswith("reuse:"):
            assert a.get("v10"), a["name"]


def test_resolve_ref(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"arms": {"b3-chain": {"publish": {"artifact_dir": "/a"}}},
                             "rows": [{"label": "sfb+b4", "v": 2}, {"label": "b4", "v": 1}]}))
    assert bc.resolve_ref(f"{p}#arms.b3-chain.publish.artifact_dir") == "/a"
    assert bc.resolve_ref(f"{p}#rows[label=sfb+b4].v") == 2
    with pytest.raises(KeyError):
        bc.resolve_ref(f"{p}#rows[label=none].v")


def test_parse_cpus_and_run_refuses_more_than_4(tmp_path, monkeypatch, capsys):
    assert bc.parse_cpus("8-11") == [8, 9, 10, 11]
    assert bc.parse_cpus("1,3,5-6") == [1, 3, 5, 6]
    c = tmp_path / "c.toml"
    c.write_text('schema = "scopt.b5_campaign/1"\n[timing]\nlock = "/nonexistent/lock"\n'
                 'cpu_list_default = "8-11"\nload_sample_s = 30\n')
    monkeypatch.delenv(bc.LOCK_ENV, raising=False)
    rc = bc.main(["run", "--campaign", str(c), "--expect-sha256", "0" * 64, "--out-root",
                  str(tmp_path / "o"), "--cpus", "0-4"])
    assert rc == 2 and "5 CPUs > 4" in capsys.readouterr().err


# --------------------------------------------------------------------------
# a synthetic frozen campaign and the freeze checker
# --------------------------------------------------------------------------

def _w(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _artifact(root: Path, payload: bytes) -> tuple[Path, str]:
    man = f"{payload.hex()}  cache.sc\n".encode()
    aid = artifact_id(man)
    d = root / "artifacts" / aid.split(":", 1)[1]
    _w(d / "cache.sc", payload)
    _w(d / "MANIFEST.sha256", man)
    return d, aid


@pytest.fixture
def camp(tmp_path: Path) -> dict:
    sha, sc = bc.sha256_file, bc.sha256_sc
    qemu = _w(tmp_path / "bin/qemu", b"qemu")
    rec = tmp_path / "gate-binary.json"
    rec.write_text(json.dumps({"exe_sha256": sha(qemu)[7:]}))
    plugin = _w(tmp_path / "bin/plugin.so", b"plugin")
    fw = _w(tmp_path / "fw/fw.elf", b"elf")
    base = tmp_path / "base.toml"
    base.write_text(f'[Machine]\nsupercache = "cache/x.sc"\n[CPU]\n[[CPU.cpu0]]\nbinary = "{fw}"\n')
    snap = tmp_path / "store/snapshots/s1"
    _w(snap / "cache.sc", b"warm")
    _w(snap / "snapshot.json", b"{}")
    prof = tmp_path / "prof/merged.profile.json"
    prof.parent.mkdir(parents=True)
    prof.write_text(json.dumps({"profile_id": "sha256:" + "ab" * 32, "collection": {
        "inputs": [{"name": "train-1.bin", "split": "train"}]}}))
    ri = _w(tmp_path / "ri.json", b"{}")
    cal = _w(tmp_path / "cal.json", b'{"constants": {}}')
    a1, id1 = _artifact(tmp_path / "s", b"b3")
    a2, id2 = _artifact(tmp_path / "s", b"b3-no-x")
    sets = []
    for split, seed in (("heldout", 1), ("final", 2)):
        d = tmp_path / "inputs" / split / "w"
        ins = [_w(d / f"{split}-{i}.bin", f"{split}{i}".encode()) for i in (1, 2)]
        _w(d / "MANIFEST.json", b"{}")
        sets.append({"name": f"w-{split}", "split": split, "input_set": str(d),
                     "input_manifest_sha256": sha(d / "MANIFEST.json"),
                     "inputs": [{"path": str(p), "sha256": sha(p)} for p in ins],
                     "arms": ["nocache", "warm", "b3", "b3-no-x"], "reps": 30, "seed": seed,
                     "timeout_s": 300.0, "out": f"w-{split}"})
    doc = {
        "schema": bc.SCHEMA, "name": "t", "definition": {"path": "x", "blob": "y"},
        "freeze": {},
        "runtime": {"qemu": str(qemu), "qemu_sha256": sha(qemu), "gate_tag": "t",
                    "gate_commit": "c", "gate_worktree": str(tmp_path),
                    "gate_binary_record": str(rec), "plugin": str(plugin),
                    "plugin_sha256": sha(plugin)},
        "schedule": {"min_reps": 30},
        "workload": {"base_config": str(base), "base_config_sha256": sha(base),
                     "firmware": {"path": str(fw), "sha256": sha(fw),
                                  "size": fw.stat().st_size, "mtime_ns": fw.stat().st_mtime_ns},
                     "snapshot": {"dir": str(snap), "cache_sc_sha256": sc(snap / "cache.sc"),
                                  "snapshot_json_sha256": sha(snap / "snapshot.json")},
                     "profile": {"path": str(prof), "sha256": sha(prof),
                                 "profile_id": "sha256:" + "ab" * 32},
                     "runtime_identity": {"path": str(ri), "sha256": sha(ri)},
                     "calibration": {"path": str(cal), "sha256": sha(cal)}},
        "arm": [{"name": "nocache", "kind": "nocache", "role": "baseline"},
                {"name": "warm", "kind": "deploy", "role": "reference", "source": str(snap),
                 "sha256": sc(snap / "cache.sc")},
                {"name": "b3", "kind": "deploy", "role": "candidate", "source": str(a1),
                 "sha256": sc(a1 / "cache.sc"), "artifact_id": id1,
                 "manifest_sha256": sha(a1 / "MANIFEST.sha256")},
                {"name": "b3-no-x", "kind": "deploy", "role": "ablation", "parent": "b3",
                 "ablated_pass": "x", "source": str(a2), "sha256": sc(a2 / "cache.sc"),
                 "artifact_id": id2, "manifest_sha256": sha(a2 / "MANIFEST.sha256")}],
        "skipped_unsupported": [{"name": "semantic", "reason": "capability_not_enabled",
                                 "detail": "no B3+B4 packager"}],
        "excluded": [{"name": "dup", "reason": "duplicate: of b3", "duplicate_of": "b3"}],
        "set": sets,
    }
    p = tmp_path / "camp.toml"
    p.write_text(bc.toml_dumps(doc))
    return {"path": p, "doc": doc, "a1": a1, "prof": prof, "fw": fw, "qemu": qemu,
            "sets": sets}


def test_check_passes_untouched(camp):
    r = bc.check(camp["path"], bc.sha256_file(camp["path"]), check_git=False)
    assert r["ok"], r["problems"]
    assert r["checked"] > 25


@pytest.mark.parametrize("what", ["artifact", "profile", "input", "fw_mtime", "qemu",
                                  "campaign", "reps", "parent", "seed"])
def test_check_refuses_any_change(camp, what):
    p = camp["path"]
    expect = bc.sha256_file(p)
    if what == "artifact":
        (camp["a1"] / "cache.sc").write_bytes(b"b3-changed")
    elif what == "profile":
        camp["prof"].write_text(json.dumps({"profile_id": "sha256:" + "cd" * 32}))
    elif what == "input":
        Path(camp["sets"][0]["inputs"][0]["path"]).write_bytes(b"other")
    elif what == "fw_mtime":
        os.utime(camp["fw"], ns=(1, 1))
    elif what == "qemu":
        camp["qemu"].write_bytes(b"other qemu")
    elif what in ("campaign", "reps", "parent", "seed"):
        doc = tomllib.loads(p.read_text())
        if what == "campaign":
            doc["name"] = "edited"
        elif what == "reps":
            doc["set"][0]["reps"] = 29
        elif what == "parent":
            doc["set"][0]["arms"] = ["warm", "b3-no-x"]
        else:
            doc["set"][1]["seed"] = doc["set"][0]["seed"]
        p.write_text(bc.toml_dumps(doc))
        if what != "campaign":
            expect = bc.sha256_file(p)
    r = bc.check(p, expect, check_git=False)
    assert not r["ok"]
    key = {"artifact": "b3: cache.sc", "profile": "training profile", "input": "input heldout-1",
           "fw_mtime": "firmware mtime", "qemu": "qemu (gate binary)",
           "campaign": "campaign file", "reps": "reps >= 30", "parent": "parent in the same set",
           "seed": "distinct set seeds"}[what]
    assert any(key in x for x in r["problems"]), r["problems"]


def test_set_campaign_loads(camp, tmp_path):
    from scopt.run.arms import load_campaign

    doc = tomllib.loads(camp["path"].read_text())
    t = tmp_path / "set.toml"
    t.write_text(bc.set_campaign_toml(doc, doc["set"][0], reps=30, name="w-heldout"))
    c = load_campaign(t)
    by = {a.name: a for a in c.arms}
    assert set(by) == {"nocache", "warm", "b3", "b3-no-x"}
    assert by["b3"].expect_sha256 == doc["arm"][2]["sha256"]
    assert by["b3-no-x"].parent == "b3" and by["b3-no-x"].role == "ablation"
    assert c.reps == 30 and c.seed == 1 and c.split == "heldout"
    assert "semantic" in c.unsupported and "dup" in c.not_run
    assert c.qemu == str(camp["qemu"])


# --------------------------------------------------------------------------
# the B5 report section
# --------------------------------------------------------------------------

def _campaign_dir(root: Path, name: str, split: str, arms: list[str], times: dict) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "campaign.json").write_text(json.dumps({
        "schema": "scopt.campaign/1", "status": "done", "freeze_ok": True,
        "campaign": {"name": name, "split": split, "reference": "warm",
                     "arms": [{"name": a, "kind": "deploy", "role": "reference" if a == "warm"
                               else "candidate", "parent": "b3" if a.startswith("b3-no")
                               else None} for a in arms]}}))
    rows = []
    for rep in range(3):
        for inp in range(2):
            for a in arms:
                rows.append({"arm": a, "rep": rep, "input": inp, "valid": True,
                             "bench_ns": times[a] * (1 + 0.001 * rep), "wall_s": 1.2,
                             "cpu_s": 1.1, "maxrss_kb": 48000, "appended": 200,
                             "run_dir": str(d / "runs" / a), "qemu_sha256": "sha256:aa",
                             "guest_norm_sha256": "sha256:same"})
    (d / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return d


def test_b5_report_one_command(tmp_path):
    root = tmp_path / "out"
    _campaign_dir(root, "compute-heldout-core", "heldout", ["warm", "b3-static", "b3"],
                  {"warm": 1.0e9, "b3-static": 0.95e9, "b3": 0.94e9})
    _campaign_dir(root, "timing", "heldout", ["warm", "b3-chain"],  # foreign (not a B5 set)
                  {"warm": 1.0e9, "b3-chain": 0.98e9})
    out = tmp_path / "rep" / "report.md"
    p = subprocess.run([sys.executable, "-m", "scopt", "report", str(root), "--out", str(out),
                        "--bootstrap", "200", "--b5-campaign", str(DEF)], cwd=str(WT),
                       capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(WT)})
    assert p.returncode == 0, p.stderr
    md = out.read_text()
    model = json.loads(out.with_suffix(".json").read_text())
    sec = model["b5"]
    for h in ("### Arms", "### Paired BENCH speedup", "### Removed lookups",
              "### Removed env stores", "### Code growth", "### Memory",
              "### Full-harness throughput", "### Cold path", "### Fallback frequency",
              "### Region coverage", "### Disarm, refusals", "### Preparation cost"):
        assert h in md, h
    rows = {r["arm"]: r for r in sec["arms"]}
    assert rows["semantic"]["status"] == "skipped_unsupported"
    assert rows["combined"]["per_split"]["final"]["status"] == "skipped_unsupported"
    assert rows["semantic-no-eliminate-state-sync"]["status"] == "skipped_unsupported"
    assert rows["cmov"]["status"] == "excluded"
    assert rows["b3-static"]["per_split"]["heldout"]["status"] == "measured"
    assert rows["b3-static"]["per_split"]["final"]["status"] == "not_run"
    assert rows["identity"]["per_split"]["train"]["status"] == "not_planned"
    # the foreign campaign never counts as a B5 measurement
    assert rows["b3-chain"]["per_split"]["heldout"]["status"] == "not_run"
    assert rows["b3-chain"]["per_split"]["heldout"]["foreign_sets"] == ["timing"]
    assert sec["foreign_sets"] == ["timing"]
    sp = {(s["set"], s["arm"]): s for s in sec["speedups"]}
    assert sp[("compute-heldout-core", "b3")]["r11"] is True
    assert sp[("timing", "b3-chain")]["b5"] is False
    assert "skipped_unsupported" in md and "(foreign)" in md


def test_b5_section_r11_flag_null():
    camp = load_b5(DEF)
    g = {"name": "campaign:compute-final-core",
         "meta": {"name": "compute-final-core", "split": "final"},
         "per_arm": {"warm": {"runs": 90, "valid": 90}, "b4": {"runs": 90, "valid": 90}},
         "comparisons": [{"arm": "b4", "reference": "warm", "why": "vs_reference",
                          "metric": "bench", "input": "*", "median": 1.004,
                          "ci": [0.998, 1.01], "verdict": "no_significant_difference",
                          "n_pairs": 90}], "raw": []}
    sec = b5_section({"groups": [g]}, camp, bootstrap=100)
    s = sec["speedups"][0]
    assert s["r11"] is False and s["b5"] is True
    assert "### Arms" in "\n".join(render_b5(sec))


# --------------------------------------------------------------------------
# MODEL on real gate-binary artifacts (skipped when the runs are not on this host)
# --------------------------------------------------------------------------

W28 = (fastdyn_root() / "runs/wp28/compute")
W29 = (fastdyn_root() / "runs/wp29/compute")


@pytest.mark.skipif(not (W28 / "prof/merged.profile.json").is_file(), reason=f"{W28 / 'prof/merged.profile.json'} not present")
def test_lookup_model_reproduces_pass_stats():
    from scopt.report.b5 import guarded_lookup_model, profile_view

    pv = profile_view(json.loads((W28 / "prof/merged.profile.json").read_text()))
    adir = next((W28 / "pubstore/b3/artifacts").iterdir())
    side = json.loads((adir / "cache.sc.scopt.json").read_text())
    want = sum(p["stats"].get("train_lookups_guarded", 0)
               for p in side["x_b3_manager"]["passes"])
    m = guarded_lookup_model(adir / "cache.sc", pv)
    assert m["unmapped_slots"] == 0
    assert round(m["guarded_lookups"] * pv["runs"]) == want  # merged counts
    assert 0.70 < m["share_of_lookups"] < 0.73


@pytest.mark.skipif(not (W29 / "store/artifacts").is_dir(), reason=f"{W29 / 'store/artifacts'} not present")
def test_store_model_reproduces_class_counts():
    from scopt.report.b5 import removed_store_model

    base = next((W29 / "store/snapshots").iterdir()) / "cache.sc"
    for adir in (W29 / "store/artifacts").iterdir():
        side = json.loads((adir / "cache.sc.scopt.json").read_text())
        xc = side["pipeline"][0].get("x_classes")
        if not xc:
            continue
        m = removed_store_model(adir / "cache.sc", base, None)
        assert m["static_stores"] == sum(xc.values()), (adir.name, xc)
        assert m["units_resized"] == 0
