"""WP13: timing arms (scopt.run.arms) without QEMU: schedule, config checks,
campaign files, run classification, and a whole campaign with a fake launcher."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from scopt.run import arms as A
from tests.synth.builders import build_sc, simple_units

# run directories made here name no QEMU; nothing is executed
pytestmark = pytest.mark.usefixtures("fake_runtime_qemu")

BASE_TOML = """\
# test base
[Memory]
[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size = "64K"
backend = "file"
memory_file = "/nonexistent/ram0.ram"

[[Memory.input]]
id = "input"
base_address = "0x30000000"
memory_size = "4K"
backend = "file"
memory_file = "input.bin"

[Machine]
platform = "generic-cortexm"
supercache = "/nonexistent/cache/x.sc"
supercache_flat = false
qmp_socket = "/nonexistent/qmp.sock"
enable_gdb = false

[CPU]
[[CPU.cpu0]]
arch = "arm"
log_options = "none"
"""


@pytest.fixture
def base_toml(tmp_path: Path) -> Path:
    p = tmp_path / "base.toml"
    p.write_text(BASE_TOML)
    return p


@pytest.fixture
def warm_sc(tmp_path: Path) -> Path:
    p = tmp_path / "warm.sc"
    build_sc(simple_units(4), path=str(p))
    return p


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------

def test_schedule_is_seeded_interleaved_and_complete():
    arms = ["nocache", "empty", "warm", "identity", "layout"]
    s1 = A.make_schedule(arms, 30, 0, 7)
    s2 = A.make_schedule(arms, 30, 0, 7)
    s3 = A.make_schedule(arms, 30, 0, 8)
    assert s1 == s2
    assert [x["arm"] for x in s1] != [x["arm"] for x in s3]
    assert len(s1) == 150
    for rep in range(30):
        block = [x for x in s1 if x["rep"] == rep]
        assert sorted(x["arm"] for x in block) == sorted(arms)  # every arm once per rep
        assert [x["pos"] for x in block] == list(range(rep * 5, rep * 5 + 5))
    # interleaved: the order differs between repetitions
    orders = {tuple(x["arm"] for x in s1 if x["rep"] == r) for r in range(30)}
    assert len(orders) > 1


def test_schedule_with_inputs():
    s = A.make_schedule(["a", "b"], 3, 2, 1)
    assert len(s) == 12
    c = Counter((x["rep"], x["input"]) for x in s)
    assert set(c.values()) == {2}


# --------------------------------------------------------------------------
# configuration checks
# --------------------------------------------------------------------------

def test_derive_nocache_comments_out_only_supercache():
    new = A.derive_nocache_toml(BASE_TOML)
    assert A.config_diff(BASE_TOML, new) == ["Machine.supercache"]
    assert "# scopt-nocache: supercache" in new


def test_production_problems():
    import tomllib
    cfg = tomllib.loads(BASE_TOML)
    assert A.production_problems(cfg) == []
    bad = BASE_TOML.replace("enable_gdb = false", "enable_gdb = true").replace(
        'log_options = "none"', 'log_options = "exec,nochain"')
    probs = A.production_problems(tomllib.loads(bad))
    assert any(p.startswith("debug_option:") for p in probs)
    assert any(p.startswith("nochain:") for p in probs)


def test_preflight_refusals(tmp_path, base_toml, warm_sc):
    out = tmp_path / "o"
    out.mkdir()
    # nocache config that differs beyond supercache
    noc = tmp_path / "noc.toml"
    noc.write_text(A.derive_nocache_toml(BASE_TOML).replace('platform = "generic-cortexm"',
                                                             'platform = "other"'))
    c = A.campaign_from_args(base_config=str(base_toml), arms=["nocache", "warm"],
                             out_name="t", warm=str(warm_sc), nocache_config=str(noc))
    with pytest.raises(A.CampaignError) as e:
        A.preflight(c, out)
    assert e.value.code == "nocache_config_differs"
    # debug option
    dbg = tmp_path / "dbg.toml"
    dbg.write_text(BASE_TOML.replace("enable_gdb = false", "enable_gdb = true"))
    c = A.campaign_from_args(base_config=str(dbg), arms=["warm"], out_name="t",
                             warm=str(warm_sc))
    with pytest.raises(A.CampaignError) as e:
        A.preflight(c, out)
    assert e.value.code == "refused_production_config"
    # frozen sha256 mismatch
    c = A.campaign_from_args(base_config=str(base_toml), arms=["warm"], out_name="t",
                             warm=str(warm_sc))
    c.arms[0].expect_sha256 = "sha256:" + "0" * 64
    with pytest.raises(A.CampaignError) as e:
        A.preflight(c, out)
    assert e.value.code == "freeze_violation"
    # reference arm missing
    c = A.campaign_from_args(base_config=str(base_toml), arms=["nocache"], out_name="t")
    with pytest.raises(A.CampaignError) as e:
        A.preflight(c, out)
    assert e.value.code == "reference_arm_missing"


def test_campaign_from_args_rows(base_toml, warm_sc, tmp_path):
    c = A.campaign_from_args(base_config=str(base_toml),
                             arms=["nocache", "empty", "warm", "identity", "layout", "semantic"],
                             out_name="t", warm=str(warm_sc),
                             artifacts={"identity": str(warm_sc)},
                             ablations={"cluster-hot": str(warm_sc)})
    names = [a.name for a in c.arms]
    assert names == ["nocache", "empty", "warm", "identity", "layout-no-cluster-hot"]
    assert c.not_run == {"layout": "no_artifact: no --artifact given for this arm"}
    assert set(c.unsupported) == {"semantic", "combined"}
    ab = c.arm("layout-no-cluster-hot")
    assert ab.parent == "layout" and ab.ablated_pass == "cluster-hot" and ab.role == "ablation"
    assert c.arm("warm").role == "reference" and c.arm("nocache").role == "baseline"
    # warm without a source records one (auto)
    c2 = A.campaign_from_args(base_config=str(base_toml), arms=["warm"], out_name="t")
    assert c2.arm("warm").source == "auto"


def test_load_campaign_toml(tmp_path, base_toml, warm_sc):
    from scopt.formats.sc_records import sha256_sparse
    f = tmp_path / "camp.toml"
    f.write_text(f"""
[campaign]
name = "c1"
base_config = "base.toml"
reps = 5
seed = 9
split = "final"
inputs = []

[[arm]]
name = "warm"
source = "warm.sc"
sha256 = "{sha256_sparse(warm_sc)}"

[[arm]]
name = "nocache"

[[arm]]
name = "layout-no-x"
source = "warm.sc"
parent = "warm"
ablated_pass = "x"
""")
    c = A.load_campaign(f)
    assert c.name == "c1" and c.reps == 5 and c.seed == 9 and c.split == "final"
    assert c.base_config == str(base_toml.resolve())
    assert c.arm("warm").source == str(warm_sc.resolve())
    assert c.arm("nocache").kind == "nocache"
    assert c.arm("layout-no-x").role == "ablation"
    ids = A.preflight(c, tmp_path / "o")
    assert ids["arms"]["warm"]["sha256"] == sha256_sparse(warm_sc)
    assert ids["nocache_config"]["derived"] is True
    c2 = A.load_campaign(f, reps=2)
    assert c2.reps == 2


# --------------------------------------------------------------------------
# run classification
# --------------------------------------------------------------------------

def _doc(**kw):
    d = {"exit_code": 0, "timed_out": False, "stray_capture_files": [], "verdict": "PASS",
         "bench": [{"tag": "m", "elapsed_ns": 100}]}
    d.update(kw)
    return d


def test_classify_run():
    assert A.classify_run("deploy", _doc()) == (True, "ok")
    assert A.classify_run("deploy", _doc(verdict="INVALID_artifact_not_used")) == (
        False, "INVALID_artifact_not_used")
    assert A.classify_run("deploy", _doc(exit_code=1))[0] is False
    assert A.classify_run("deploy", _doc(timed_out=True)) == (False, "timeout")
    assert A.classify_run("deploy", _doc(bench=[])) == (False, "no_bench")
    assert A.classify_run("deploy", _doc(stray_capture_files=["x.scm"]))[1] == \
        "stray_capture_files"
    assert A.classify_run("empty", _doc(verdict="NOT_PROVED")) == (True, "ok")
    assert A.classify_run("nocache", _doc(verdict="NOT_PROVED"), cache_present=True) == (
        False, "nocache_arm_has_cache")
    assert A.classify_run("nocache", _doc(verdict="PASS"))[0] is False


# --------------------------------------------------------------------------
# a whole campaign with a fake launcher (no QEMU)
# --------------------------------------------------------------------------

BENCH = {"nocache": 1000, "empty": 1300, "warm": 250, "identity": 252, "slow": 400}


def _fake(monkeypatch, *, fail_rep=None):
    from scopt.run import deploy as D
    from scopt.run import launcher as Lm

    calls = []

    def fake_deploy_new(src, base, root, **kw):
        rd = Lm.create_run_dir(base, root, quiet=kw.get("quiet", False), qemu=kw.get("qemu"),
                               label=kw.get("label"))
        (rd.root / "deploy.json").write_text("{}")
        return rd, {"state": {"n": 4}, "deploy_seconds": 0.01}

    def fake_launch(rd, *, timeout_s=0, prove=None, inputs=None, **kw):
        arm = rd.root.parent.name
        rep = int(rd.root.name[1:4])
        calls.append((arm, rep, dict(inputs or {})))
        b = BENCH.get(arm, 300) + rep
        doc = {"schema": "scopt.run/1", "exit_code": 0, "timed_out": False,
               "stray_capture_files": [],
               "verdict": "PASS" if (rd.root / "deploy.json").exists() else "NOT_PROVED",
               "bench": [{"tag": "m", "elapsed_ns": b}],
               "timings": {"wall_s": 0.2 + b / 1e6, "children_user_s": 0.1,
                           "children_sys_s": 0.01, "children_maxrss_kb": 1000},
               "appended": 0 if (rd.root / "deploy.json").exists() else None,
               "guest_norm": ["Reg 0:0x3"] if arm != "slow" or rep != fail_rep
               else ["Reg 0:0x4"], "qemu": "/q", "qemu_sha256": None,
               "inputs": {k: {"path": str(v)} for k, v in (inputs or {}).items()}}
        if arm == "empty":
            (rd.cache_dir / "x.sc").write_bytes(b"")
        (rd.root / "run.json").write_text(json.dumps(doc))
        return doc

    monkeypatch.setattr(D, "deploy_new", fake_deploy_new)
    monkeypatch.setattr(A, "launch", fake_launch)
    return calls


def test_run_campaign_fake(tmp_path, base_toml, warm_sc, monkeypatch):
    calls = _fake(monkeypatch, fail_rep=2)
    inp = [tmp_path / "in1.bin", tmp_path / "in2.bin"]
    for i, p in enumerate(inp):
        p.write_bytes(bytes([i]) * 10)
    c = A.campaign_from_args(base_config=str(base_toml),
                             arms=["nocache", "empty", "warm", "identity"], out_name="fake",
                             warm=str(warm_sc), artifacts={"identity": str(warm_sc),
                                                           "slow": str(warm_sc)},
                             reps=4, seed=3, inputs=[str(p) for p in inp], split="heldout")
    out = tmp_path / "camp"
    doc = A.run_campaign(c, out, log=None)
    assert doc["status"] == "done" and doc["freeze_ok"] is True
    assert doc["n_runs"] == 4 * 2 * 5
    rows = [json.loads(x) for x in (out / "runs.jsonl").read_text().splitlines()]
    assert len(rows) == 40
    assert all(r["valid"] for r in rows), [r for r in rows if not r["valid"]]
    # inputs reach the input bank
    assert all(cl[2] == {"input": str(inp[0])} or cl[2] == {"input": str(inp[1])}
               for cl in calls)
    # nocache runs used a TOML without supercache
    noc = json.loads((out / "runs" / "nocache" / "r000-i0" / "rundir.json").read_text())
    assert noc["cache_name"] is None
    # ledger: one measured row per run, one per deploy arm
    from scopt.ledger import Ledger
    led = Ledger.read(out / "ledger.jsonl")
    assert len(led.select(subject="run", stage="measured")) == 40
    arts = led.select(subject="artifact", stage="measured")
    assert {r.id.split(":")[1] for r in arts} == {"warm", "identity", "slow"}
    # existing out dir is refused
    with pytest.raises(A.CampaignError):
        A.run_campaign(c, out, log=None)

    # the report over this campaign
    from scopt.report.collect import collect
    from scopt.report.render import write_report
    m = collect([out], bootstrap=500)
    g = m["groups"][0]
    assert g["reference"] == "warm" and g["paired"]
    bench = {(c["arm"], c["input"]): c for c in g["comparisons"] if c["metric"] == "bench"}
    assert bench[("identity", "*")]["n_pairs"] == 8
    assert bench[("slow", "*")]["median"] < 1.0 and bench[("slow", "*")]["verdict"] == "slower"
    assert bench[("nocache", "*")]["verdict"] == "slower"
    kinds = Counter(r["kind"] for r in m["regressions"])
    assert kinds["performance"] >= 1          # slow arm
    assert kinds["output_mismatch"] == 2      # rep 2, both inputs
    assert kinds["expected_baseline_slower"] >= 1
    paths = write_report(m, out / "report.md")
    md = Path(paths["markdown"]).read_text()
    assert "BENCH comparison (paired speedup vs reference)" in md
    assert "| slow | warm |" in md
    assert "output_mismatch" in md


def test_run_campaign_records_invalid_deploy(tmp_path, base_toml, warm_sc, monkeypatch):
    from scopt.run import deploy as D
    _fake(monkeypatch)

    def refuse(*a, **k):
        raise D.DeployError("bundle_mismatch", "x")

    monkeypatch.setattr(D, "deploy_new", refuse)
    c = A.campaign_from_args(base_config=str(base_toml), arms=["nocache", "warm"],
                             out_name="r", warm=str(warm_sc), reps=2)
    doc = A.run_campaign(c, tmp_path / "o", log=None)
    rows = [json.loads(x) for x in (tmp_path / "o" / "runs.jsonl").read_text().splitlines()]
    warm = [r for r in rows if r["arm"] == "warm"]
    assert all(not r["valid"] and r["reason"] == "deploy_refused:bundle_mismatch"
               for r in warm)
    assert doc["n_valid"] == 2
    from scopt.report.collect import collect
    m = collect([tmp_path / "o"], bootstrap=200)
    st = {a["arm"]: a for a in m["arms"]}
    assert st["warm"]["status"] == "not_measured"
    assert st["nocache"]["status"] == "measured"


def test_load_inputs_into_bank(tmp_path, base_toml):
    from scopt.run.launcher import RunDirError, create_run_dir, load_inputs, reset_ram
    rd = create_run_dir(base_toml, tmp_path / "rd")
    reset_ram(rd)
    src = tmp_path / "in.bin"
    src.write_bytes(b"\x01\x02\x03")
    info = load_inputs(rd, {"input": src})
    bank = Path(info["input"]["bank_file"])
    assert bank.parent == rd.ram
    data = bank.read_bytes()
    assert len(data) == 4096 and data[:3] == b"\x01\x02\x03" and not any(data[3:])
    assert info["input"]["sha256"].startswith("sha256:")
    (tmp_path / "big.bin").write_bytes(b"\0" * 5000)
    with pytest.raises(RunDirError) as e:
        load_inputs(rd, {"input": tmp_path / "big.bin"})
    assert e.value.code == "input_too_large"
    with pytest.raises(RunDirError) as e:
        load_inputs(rd, {"nope": src})
    assert e.value.code == "input_bank_missing"


# --------------------------------------------------------------------------
# r2: input sets (WP12 MANIFEST), seed input, QEMU identity, full freeze check
# --------------------------------------------------------------------------

def _input_set(root: Path, split: str = "heldout", n: int = 2, profile: str | None = None
               ) -> Path:
    import hashlib
    d = root / "inputs" / split / "wl"
    d.mkdir(parents=True)
    entries = {}
    for i in range(n):
        name = f"{split}-{i + 1}"
        data = bytes([i + 1]) * (16 + i)
        (d / f"{name}.bin").write_bytes(data)
        entries[name] = {"file": f"{name}.bin", "sha256": hashlib.sha256(data).hexdigest(),
                         "size": len(data), "profile": profile or split}
    (d / "MANIFEST.json").write_text(json.dumps({"inputs": entries}))
    return d


def test_inputs_from_manifest(tmp_path):
    d = _input_set(tmp_path, "final", 3)
    paths, split, info = A.inputs_from_manifest(d)
    assert [Path(p).name for p in paths] == ["final-1.bin", "final-2.bin", "final-3.bin"]
    assert split == "final" and info["inputs"] == ["final-1", "final-2", "final-3"]
    # profile not a split name: the directory's parent decides
    d2 = _input_set(tmp_path / "x", "train", 1, profile="custom")
    assert A.inputs_from_manifest(d2)[1] == "train"
    # a modified input is refused
    (d / "final-2.bin").write_bytes(b"tampered")
    with pytest.raises(A.CampaignError) as e:
        A.inputs_from_manifest(d)
    assert e.value.code == "input_manifest_mismatch"
    with pytest.raises(A.CampaignError) as e:
        A.inputs_from_manifest(tmp_path)
    assert e.value.code == "input_manifest_unreadable"


def test_resolve_inputs_split_rules(tmp_path, base_toml):
    d = _input_set(tmp_path, "heldout", 2)
    c = A.campaign_from_args(base_config=str(base_toml), arms=["nocache"], out_name="t",
                             split=None, input_set=str(d))
    info = A.resolve_inputs(c)
    assert c.split == "heldout" and len(c.inputs) == 2 and info["split"] == "heldout"
    c = A.campaign_from_args(base_config=str(base_toml), arms=["nocache"], out_name="t",
                             split="final", input_set=str(d))
    with pytest.raises(A.CampaignError) as e:
        A.resolve_inputs(c)
    assert e.value.code == "split_mismatch"
    c = A.campaign_from_args(base_config=str(base_toml), arms=["nocache"], out_name="t",
                             split=None, input_set=str(d), inputs=[str(d / "heldout-1.bin")])
    with pytest.raises(A.CampaignError) as e:
        A.resolve_inputs(c)
    assert e.value.code == "inputs_conflict"
    # explicit files without a split are "unlabelled", never "fixed"
    c = A.campaign_from_args(base_config=str(base_toml), arms=["nocache"], out_name="t",
                             split=None, inputs=[str(d / "heldout-1.bin")])
    A.resolve_inputs(c)
    assert c.split == "unlabelled"
    c = A.campaign_from_args(base_config=str(base_toml), arms=["nocache"], out_name="t",
                             split=None)
    A.resolve_inputs(c)
    assert c.split == "fixed"


def test_classify_run_qemu_mismatch():
    good = "sha256:" + "a" * 64
    assert A.classify_run("deploy", _doc(qemu_sha256=good), expect_qemu_sha256=good) == (
        True, "ok")
    assert A.classify_run("deploy", _doc(qemu_sha256=good), expect_qemu_sha256="a" * 64)[0]
    v, why = A.classify_run("deploy", _doc(qemu_sha256="sha256:" + "b" * 64),
                            expect_qemu_sha256=good)
    assert not v and why.startswith("qemu_mismatch:")


def test_campaign_input_set_seed_input_and_freeze(tmp_path, base_toml, monkeypatch):
    """warm=auto seeds on --seed-input (a train input), times held-out inputs from a
    MANIFEST; an input changed during timing is a freeze violation; an exception row
    keeps the input in its run-dir tag."""
    from scopt.run import launcher as Lm
    calls = _fake(monkeypatch)
    ho = _input_set(tmp_path, "heldout", 2)
    tr = _input_set(tmp_path / "t", "train", 1)
    seeds = []

    def fake_record_warm(c, arm, out):
        seeds.append(c.seed_input)
        dst = out / "frozen" / "warm.sc"
        dst.parent.mkdir(parents=True, exist_ok=True)
        build_sc(simple_units(3), path=str(dst))
        return {"frozen": str(dst), "seed_input": c.seed_input, "records": 3}

    monkeypatch.setattr(A, "_record_warm", fake_record_warm)
    real_run_one = A._run_one

    def flaky_run_one(c, arm, src, step, out, *a, **k):
        if arm.name == "nocache" and step["rep"] == 1 and step["input"] == 1:
            (ho / "heldout-2.bin").write_bytes(b"changed during the campaign")
            raise RuntimeError("boom")
        return real_run_one(c, arm, src, step, out, *a, **k)

    monkeypatch.setattr(A, "_run_one", flaky_run_one)
    c = A.campaign_from_args(base_config=str(base_toml), arms=["nocache", "warm"],
                             out_name="ho", reps=2, seed=5, split=None, input_set=str(ho),
                             seed_input=str(tr / "train-1.bin"))
    doc = A.run_campaign(c, tmp_path / "o", log=None)
    assert seeds == [str(tr / "train-1.bin")]
    assert doc["campaign"]["split"] == "heldout"
    assert doc["identities"]["input_set"]["inputs"] == ["heldout-1", "heldout-2"]
    assert doc["identities"]["seed_input"]["also_timed"] is False
    assert doc["status"] == "freeze_violation" and doc["freeze_ok"] is False
    assert doc["freeze_check"]["input:heldout-2.bin"]["ok"] is False
    assert doc["freeze_check"]["config:base"]["ok"] is True
    rows = [json.loads(x) for x in (tmp_path / "o" / "runs.jsonl").read_text().splitlines()]
    bad = [r for r in rows if not r["valid"]]
    assert len(bad) == 1 and bad[0]["reason"] == "harness_exception"
    assert bad[0]["run_dir"].endswith("/nocache/r001-i1")
    assert all(cl[2]["input"].startswith(str(ho)) for cl in calls)
    from scopt.report.collect import collect
    from scopt.report.render import render_markdown
    m = collect([tmp_path / "o"], bootstrap=100)
    assert any(r["kind"] == "freeze_violation" for r in m["regressions"])
    md = render_markdown(m)
    assert "MANIFEST verified" in md and "train-1.bin" in md and "not a timed input" in md
    assert Lm  # launcher stays importable with the monkeypatches


def test_campaign_qemu_identity_enforced(tmp_path, base_toml, warm_sc, monkeypatch):
    """A run whose QEMU differs from the campaign's binary is not a sample, and a report
    over valid samples from two binaries flags mixed_qemu."""
    _fake(monkeypatch)
    q = tmp_path / "qemu-system-arm"
    q.write_bytes(b"\x7fELF fake qemu")
    import hashlib
    qsha = "sha256:" + hashlib.sha256(q.read_bytes()).hexdigest()
    real_launch = A.launch

    def launch_with_qemu(rd, **kw):
        doc = real_launch(rd, **kw)
        rep = int(rd.root.name[1:4])
        doc["qemu_sha256"] = qsha if rep == 0 else "sha256:" + "f" * 64
        (rd.root / "run.json").write_text(json.dumps(doc))
        return doc

    monkeypatch.setattr(A, "launch", launch_with_qemu)
    c = A.campaign_from_args(base_config=str(base_toml), arms=["nocache", "warm"],
                             out_name="q", warm=str(warm_sc), reps=2, qemu=str(q))
    doc = A.run_campaign(c, tmp_path / "o", log=None)
    rows = [json.loads(x) for x in (tmp_path / "o" / "runs.jsonl").read_text().splitlines()]
    assert {r["rep"] for r in rows if r["valid"]} == {0}
    assert all(r["reason"].startswith("qemu_mismatch:") for r in rows if not r["valid"])
    assert doc["freeze_check"]["qemu"]["ok"] is True
    assert len(doc["qemu_seen"]) == 2
    # without --qemu nothing is enforced at run time, but the report flags the mix
    _fake(monkeypatch)
    monkeypatch.setattr(A, "launch", launch_with_qemu)
    c2 = A.campaign_from_args(base_config=str(base_toml), arms=["nocache", "warm"],
                              out_name="q2", warm=str(warm_sc), reps=2)
    A.run_campaign(c2, tmp_path / "o2", log=None)
    from scopt.report.collect import collect
    m = collect([tmp_path / "o2"], bootstrap=100)
    assert any(r["kind"] == "mixed_qemu" for r in m["regressions"])
