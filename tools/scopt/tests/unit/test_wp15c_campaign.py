"""WP15C-PREP: the frozen F-B2-7 campaign checker and its derived ``scopt run`` campaigns.

* ``check`` passes on an untouched synthetic campaign and lists every frozen
  identity that changed (artifact ``cache.sc``, profile, input, firmware mtime,
  QEMU, the campaign file itself);
* the per-set ``scopt run`` campaign TOML loads through
  ``scopt.run.arms.load_campaign`` with every deploy arm's frozen sha256, the
  ablation parents and the excluded (degenerate) arm as a ``not_run`` row;
* ``preflight`` refuses a deploy arm whose ``cache.sc`` no longer matches;
* the TOML writer round-trips through ``tomllib``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tomllib
from pathlib import Path

import pytest

from scopt.ids import artifact_id

_SPEC = importlib.util.spec_from_file_location(
    "b2_campaign", Path(__file__).resolve().parents[1] / "qemu" / "b2_campaign.py")
bc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bc)


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
    qemu = _w(tmp_path / "bin/qemu", b"qemu")
    plugin = _w(tmp_path / "bin/plugin.so", b"plugin")
    fw = _w(tmp_path / "fw/fw.elf", b"elf")
    base = tmp_path / "base.toml"
    base.write_text('[Machine]\nsupercache = "cache/x.sc"\nsupercache_flat = false\n'
                    f'[CPU]\n[[CPU.cpu0]]\nbinary = "{fw}"\n')
    snap = tmp_path / "store/snapshots/s1"
    _w(snap / "cache.sc", b"warm")
    _w(snap / "snapshot.json", b"{}")
    prof = tmp_path / "store/profiles/p1/profile.json"
    prof.parent.mkdir(parents=True)
    prof.write_text(json.dumps({"profile_id": "sha256:" + "ab" * 32}))
    train = _w(tmp_path / "inputs/train/x/train-1.bin", b"t1")
    hd = tmp_path / "inputs/heldout/x"
    h1 = _w(hd / "heldout-1.bin", b"h1")
    _w(hd / "MANIFEST.json", b"{}")
    ident, ident_id = _artifact(tmp_path / "store", b"warm")
    lay, lay_id = _artifact(tmp_path / "store", b"layout")
    abl, abl_id = _artifact(tmp_path / "store", b"abl")
    st = os.stat(fw)
    s = bc.sha256_file
    doc = {
        "schema": bc.SCHEMA, "name": "t", "frozen_at": "now",
        "freeze": {"scopt_tree": "x", "pipelines_tree": "x", "driver_blob": "x"},
        "runtime": {"qemu": str(qemu), "qemu_sha256": s(qemu), "plugin": str(plugin),
                    "plugin_sha256": s(plugin)},
        "schedule": {"min_reps_per_arm_and_set": 30},
        "rules": {"min_valid_pairs": 30},
        "workload": [{
            "name": "w", "base_config": str(base), "base_config_sha256": s(base),
            "firmware": {"path": str(fw), "sha256": s(fw), "size": st.st_size,
                         "mtime_ns": st.st_mtime_ns},
            "snapshot": {"dir": str(snap), "cache_sc_sha256": bc.sha256_sc(snap / "cache.sc"),
                         "snapshot_json_sha256": s(snap / "snapshot.json")},
            "profile": {"path": str(prof), "sha256": s(prof),
                        "profile_id": "sha256:" + "ab" * 32,
                        "inputs": [{"path": str(train), "sha256": s(train)}]},
            "arm": [
                {"name": "nocache", "kind": "nocache", "role": "baseline"},
                {"name": "warm", "kind": "deploy", "role": "reference", "source": str(snap),
                 "sha256": bc.sha256_sc(snap / "cache.sc")},
                {"name": "identity", "kind": "deploy", "role": "candidate",
                 "source": str(ident), "sha256": bc.sha256_sc(ident / "cache.sc"),
                 "artifact_id": ident_id, "manifest_sha256": s(ident / "MANIFEST.sha256")},
                {"name": "layout", "kind": "deploy", "role": "candidate", "source": str(lay),
                 "sha256": bc.sha256_sc(lay / "cache.sc"), "artifact_id": lay_id,
                 "manifest_sha256": s(lay / "MANIFEST.sha256")},
                {"name": "layout-no-cluster-hot", "kind": "deploy", "role": "ablation",
                 "parent": "layout", "ablated_pass": "cluster-hot", "source": str(abl),
                 "sha256": bc.sha256_sc(abl / "cache.sc"), "artifact_id": abl_id,
                 "manifest_sha256": s(abl / "MANIFEST.sha256")},
            ],
            "excluded_arm": [{"name": "layout-no-hotcold-partition", "reason": "degenerate"}],
        }],
        "set": [{"name": "w-heldout", "workload": "w", "split": "heldout",
                 "input_set": str(hd), "input_manifest_sha256": s(hd / "MANIFEST.json"),
                 "inputs": [{"path": str(h1), "sha256": s(h1)}], "reps": 30, "seed": 7,
                 "timeout_s": 300.0, "out": "w-heldout"}],
    }
    path = tmp_path / "campaign.toml"
    path.write_text(bc.toml_dumps(doc))
    return {"path": path, "doc": doc, "tmp": tmp_path, "lay": lay, "prof": prof, "h1": h1,
            "fw": fw, "qemu": qemu}


def test_toml_round_trip(camp):
    assert tomllib.loads(camp["path"].read_text()) == camp["doc"]


def test_check_passes_untouched(camp):
    sha = bc.sha256_file(camp["path"])
    res = bc.check(camp["path"], sha, check_git=False)
    assert res["ok"], res["problems"]
    assert res["checked"] > 20


@pytest.mark.parametrize("what", ["artifact", "profile", "input", "firmware_mtime", "qemu",
                                  "campaign"])
def test_check_refuses_any_change(camp, what):
    sha = bc.sha256_file(camp["path"])
    if what == "artifact":
        (camp["lay"] / "cache.sc").write_bytes(b"retuned")
        key = "w/layout: cache.sc"
    elif what == "profile":
        camp["prof"].write_text(json.dumps({"profile_id": "sha256:" + "cd" * 32}))
        key = "w: profile"
    elif what == "input":
        camp["h1"].write_bytes(b"other")
        key = "w-heldout: input heldout-1.bin"
    elif what == "firmware_mtime":
        st = os.stat(camp["fw"])
        os.utime(camp["fw"], ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        key = "w: firmware mtime_ns"
    elif what == "qemu":
        camp["qemu"].write_bytes(b"qemu2")
        key = "qemu"
    else:
        with open(camp["path"], "a") as f:
            f.write("# edited\n")
        key = "campaign file"
    res = bc.check(camp["path"], sha, check_git=False)
    assert not res["ok"]
    assert any(p.startswith(key) for p in res["problems"]), res["problems"]


def test_set_campaign_loads_with_frozen_sha(camp, tmp_path):
    from scopt.run.arms import CampaignError, load_campaign, preflight

    doc = camp["doc"]
    text = bc.set_campaign_toml(doc, doc["set"][0], reps=30, name="w-heldout")
    p = tmp_path / "derived.toml"
    p.write_text(text)
    c = load_campaign(p)
    assert c.reps == 30 and c.seed == 7 and c.reference == "warm"
    assert c.input_set == doc["set"][0]["input_set"] and c.split == "heldout"
    arms = {a.name: a for a in c.arms}
    assert set(arms) == {"nocache", "warm", "identity", "layout", "layout-no-cluster-hot"}
    for a in doc["workload"][0]["arm"]:
        if a["kind"] == "deploy":
            assert arms[a["name"]].expect_sha256 == a["sha256"]
    assert arms["layout-no-cluster-hot"].parent == "layout"
    assert arms["layout-no-cluster-hot"].role == "ablation"
    assert c.not_run == {"layout-no-hotcold-partition": "degenerate"}
    # a retuned artifact is refused before any run
    (camp["lay"] / "cache.sc").write_bytes(b"retuned")
    c.input_set = None
    with pytest.raises(CampaignError) as e:
        preflight(c, tmp_path / "out")
    assert e.value.code == "freeze_violation"


def test_evaluate_set_rules(tmp_path):
    out = tmp_path / "set"
    (out / "report").mkdir(parents=True)
    (out / "campaign.json").write_text(json.dumps({"status": "done", "freeze_ok": True}))
    (out / "report" / "report.md").write_text("# r\n")
    comps = [{"why": "vs_reference", "metric": "bench", "input": "*", "arm": a,
              "n_pairs": n, "median": 1.0, "ci": [0.9, 1.1], "verdict": "x"}
             for a, n in (("layout", 30), ("identity", 29))]
    model = {"groups": [{"name": "campaign:t", "reference": "warm", "comparisons": comps,
                         "per_arm": {"warm": {"valid": 30}}, "correctness": [],
                         "qemu_sha256_valid": ["sha256:q"]}], "regressions": []}
    (out / "report" / "report.json").write_text(json.dumps(model))
    ev = bc.evaluate_set(out, 30)
    assert ev["failed"] == ["R2_samples"]
    assert ev["rules"]["R2_samples"]["short"] == {"identity": 29}
