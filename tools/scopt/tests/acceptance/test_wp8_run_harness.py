"""WP8 acceptance (IMPLEMENTATION-PLAN.md, WP8) with today's B0 binary.

Each test creates fresh run directories under ``tmp_path`` and runs the real
FastDyn frontend and QEMU (``setarch x86_64 -R``). Run with
``python -m pytest -m qemu tests/acceptance/test_wp8_run_harness.py``.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from scopt.formats.bundle_v1 import (BundleMismatch, check_bundle, identity_bundle,
                                     runtime_identity)
from scopt.ids import runtime_identity_id
from scopt.formats.sc_records import ScHeader, sparse_copy
from scopt.run.deploy import DeployError, deploy_new, oracle
from scopt.run.diag import diag_exec
from scopt.run.launcher import (B0_QEMU, FASTDYN_SRC, RunDir, RunDirError, create_run_dir,
                                 launch)
from scopt.paths import fastdyn_root

pytestmark = [pytest.mark.qemu, pytest.mark.real, pytest.mark.slow]

ROOT = fastdyn_root()
B0_SC_TOML = FASTDYN_SRC / "configs/b0-sc.toml"
MINIMAL = ROOT / "runs/b0/cache/minimal.sc"
WARM1_NORM = ROOT / "runs/b0/warm1.norm"


@pytest.fixture(scope="module", autouse=True)
def _need_b0():
    for p in (B0_QEMU, B0_SC_TOML, MINIMAL, WARM1_NORM):
        if not p.exists():
            pytest.skip(f"{p} not present")


def _deploy(tmp_path, name, artifact=MINIMAL, base=B0_SC_TOML, **kw):
    return deploy_new(artifact, base, tmp_path / name, qemu=B0_QEMU, **kw)


def test_1_warm_identity_run_matches_b0(tmp_path):
    rd, dep = _deploy(tmp_path, "warm")
    assert dep["state"]["n"] == 163
    r = launch(rd, reference_norm=WARM1_NORM)
    assert r["exit_code"] == 0
    assert r["reference_norm"]["equal"], r["reference_norm"].get("diff")
    assert r["proof"]["verdict"] == "PASS" and r["verdict"] == "PASS"
    assert r["appended"] == 0
    assert not r["cache_invalidated_by_frontend"]
    assert rd.cache.is_file()  # not deleted: the manifest fingerprint matched
    assert r["bench"] and r["bench"][0]["tag"] == "minimal"
    run_json = json.loads((rd.root / "run.json").read_text())
    for k in ("argv", "env_diff", "timings", "bench", "proof", "appended"):
        assert k in run_json


def test_2_deploy_check_equal_fingerprints(tmp_path):
    rd, dep = _deploy(tmp_path, "chk", check=True)
    c = dep["check"]
    assert c["ok"] and c["fingerprints"][0] == c["fingerprints"][1] == c["manifest_fingerprint"]
    assert Path(c["scratch"], "a", "work").is_dir() and Path(c["scratch"], "b", "work").is_dir()


def test_3_wrong_manifest_is_invalid(tmp_path):
    wrong = tmp_path / "wrong.fastdyn.json"
    m = json.loads((ROOT / "runs/b0/cache/minimal.sc.fastdyn.json").read_text())
    m["fingerprint"] = "0" * 64
    wrong.write_text(json.dumps(m))
    rd, _ = _deploy(tmp_path, "wrongman", manifest_override=wrong)
    r = launch(rd)
    assert r["cache_invalidated_by_frontend"]  # the frontend deleted the .sc
    assert r["proof"]["verdict"] == "INVALID_artifact_not_used"
    assert "manifest" in r["proof"]["failed"] and "inode" in r["proof"]["failed"]
    # unknown, not zero: the rebuilt cache says nothing about the artifact
    assert r["appended"] is None and r["proof"]["record_count_after"] == 163


def test_4_v6_header_is_invalid(tmp_path):
    v6 = tmp_path / "v6.sc"
    sparse_copy(MINIMAL, v6, fsync=False)
    fd = os.open(v6, os.O_RDWR)
    h = ScHeader.from_bytes(os.pread(fd, 80, 0)).replace(version=6).with_checksum()
    os.pwrite(fd, h.to_bytes(), 0)
    os.close(fd)
    with pytest.raises(DeployError) as exc:
        _deploy(tmp_path, "v6", artifact=v6)
    assert exc.value.code == "legacy_cache_version"


def test_5_bundle_stable_and_mismatch(tmp_path):
    bundles = []
    for name in ("a", "b"):
        rd = create_run_dir(B0_SC_TOML, tmp_path / name, qemu=B0_QEMU)
        oracle(rd)
        bundles.append(identity_bundle(rd.toml, rd.manifest))
    assert bundles[0] == bundles[1]
    a_fp = json.loads((tmp_path / "a/cache/minimal.sc.fastdyn.json").read_text())["fingerprint"]
    b_fp = json.loads((tmp_path / "b/cache/minimal.sc.fastdyn.json").read_text())["fingerprint"]
    assert a_fp != b_fp  # the per-run TOMLs really differ

    # a copy of the config whose modifiers come from a file; edit only that file
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "modifiers.txt").write_text("0x800397e r0 <- 3\n")
    cfg = tmp_path / "ext.toml"
    text = B0_SC_TOML.read_text()
    assert text.count("existing_config_path = false") == 1
    cfg.write_text(text.replace("existing_config_path = false", f'existing_config_path = "{ext}"'))
    rd1 = create_run_dir(cfg, tmp_path / "ext1", qemu=B0_QEMU)
    oracle(rd1)
    b1 = identity_bundle(rd1.toml, rd1.manifest)
    (ext / "modifiers.txt").write_text("0x800397e r0 <- 4\n")
    rd2 = create_run_dir(cfg, tmp_path / "ext2", qemu=B0_QEMU)
    oracle(rd2)
    with pytest.raises(BundleMismatch) as e:
        check_bundle(b1, identity_bundle(rd2.toml, rd2.manifest))
    assert e.value.code == "bundle_mismatch"
    assert "patch_plan_hash" in e.value.info["fields"]

    # deploy refuses an artifact whose bundle does not match the run's
    art = tmp_path / "artifact"
    art.mkdir()
    sparse_copy(MINIMAL, art / "cache.sc", fsync=False)
    (art / "bundle.json").write_text(json.dumps(b1))
    # an artifact directory must carry both identity documents
    with pytest.raises(DeployError) as e:
        deploy_new(art, cfg, tmp_path / "dep-noid", qemu=B0_QEMU)
    assert e.value.code == "artifact_identity_missing"
    assert not (tmp_path / "dep-noid/cache/minimal.sc").exists()
    (art / "runtime_identity.json").write_text(json.dumps(_runtime_identity(rd1)))
    with pytest.raises(DeployError) as e:
        deploy_new(art, cfg, tmp_path / "dep-ext", qemu=B0_QEMU)
    assert e.value.code == "bundle_mismatch"
    assert not (tmp_path / "dep-ext/cache/minimal.sc").exists()
    # launching the refused run directory is refused too (nothing was deployed)
    rd_refused = RunDir.open(tmp_path / "dep-ext")
    with pytest.raises(RunDirError) as e2:
        launch(rd_refused)
    assert e2.value.code == "deploy_refused"
    assert not (tmp_path / "dep-ext/run.json").exists()
    assert not (tmp_path / "dep-ext/cache/minimal.sc").exists()


def _runtime_identity(rd):
    """RuntimeIdentity of the runtime a run dir's oracle recorded (as scopt bundle does)."""
    argv = json.loads((rd.root / "oracle.json").read_text())["argv"]
    plugin = Path(argv[argv.index("--plugin") + 1].split(",", 1)[0])
    plugin = plugin if plugin.is_absolute() else (FASTDYN_SRC / plugin).resolve()
    with open(MINIMAL, "rb") as f:
        h = ScHeader.from_bytes(f.read(80))
    return runtime_identity(qemu=argv[0], plugin=plugin, config_text=rd.toml.read_text(),
                            sc_header=h, fastdyn_src=FASTDYN_SRC)


def _artifact(tmp_path, name, bundle, ri):
    art = tmp_path / name
    art.mkdir()
    sparse_copy(MINIMAL, art / "cache.sc", fsync=False)
    (art / "bundle.json").write_text(json.dumps(bundle))
    ri = dict(ri)
    ri["runtime_identity_id"] = runtime_identity_id(ri)
    (art / "runtime_identity.json").write_text(json.dumps(ri))
    return art


def test_6_runtime_identity_refusals(tmp_path):
    """Deploy refuses an artifact whose RuntimeIdentity does not match the run (§4.2, §4.8)."""
    rd = create_run_dir(B0_SC_TOML, tmp_path / "base", qemu=B0_QEMU)
    oracle(rd)
    bundle = identity_bundle(rd.toml, rd.manifest)
    ri = _runtime_identity(rd)
    assert ri["qemu_exe_stat"]["ino"] == B0_QEMU.stat().st_ino
    assert ri["plugin_stat"]["size"] > 0

    rdo, dep = deploy_new(_artifact(tmp_path, "ok", bundle, ri), B0_SC_TOML, tmp_path / "d-ok",
                          qemu=B0_QEMU)
    assert dep["identity"]["checked"] and dep["identity"]["reasons"] == []
    assert dep["identity"]["bundle_id"] == bundle["bundle_id"]
    assert dep["identity"]["runtime_identity_id"] == ri["runtime_identity_id"]
    r = launch(rdo, reference_norm=WARM1_NORM)
    assert r["verdict"] == "PASS" and r["appended"] == 0

    touched = {**ri, "qemu_exe_stat": {**ri["qemu_exe_stat"],
                                       "mtime_ns": ri["qemu_exe_stat"]["mtime_ns"] + 1}}
    cases = {
        "runtime_mismatch:qemu_exe_stat": touched,
        "runtime_mismatch:qemu_sha256": {**ri, "qemu_sha256": "sha256:" + "0" * 64},
        "runtime_mismatch:plugin_sha256": {**ri, "plugin_sha256": "sha256:" + "1" * 64},
        "config_mismatch": {**ri, "normalized_config_sha256": "sha256:" + "2" * 64},
    }
    for code, bad in cases.items():
        name = code.replace(":", "_")
        with pytest.raises(DeployError) as e:
            deploy_new(_artifact(tmp_path, name, bundle, bad), B0_SC_TOML,
                       tmp_path / f"d-{name}", qemu=B0_QEMU)
        assert e.value.code == code, (code, e.value.code, e.value.detail)
        assert not (tmp_path / f"d-{name}/cache/minimal.sc").exists()
        dj = json.loads((tmp_path / f"d-{name}/deploy.json").read_text())
        assert dj["refused"] == [code]


def test_seed_fallback_and_diag_exec(tmp_path):
    rd, dep = _deploy(tmp_path, "seed", method="seed")
    assert dep["method"] == "seed"
    r = launch(rd, reference_norm=WARM1_NORM)
    assert r["verdict"] == "PASS" and r["appended"] == 0 and r["reference_norm"]["equal"]

    rd2, _ = _deploy(tmp_path, "fordiag")
    assert launch(rd2)["verdict"] == "PASS"
    d = diag_exec(rd2.root, tmp_path / "diag")
    assert d["exit_code"] == 0
    assert d["all_traces_in_snapshot"] and d["unique_tc"] == 163
    assert d["trace_lines"] > 0 and d["links"] > 0
    # the source run directory was not touched by the diagnostic run
    assert json.loads((rd2.root / "run.json").read_text())["verdict"] == "PASS"
    shutil.rmtree(tmp_path / "diag")
