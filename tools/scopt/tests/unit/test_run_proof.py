"""WP8: output normalization, environment scrub and the §4.8 post-run proof."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scopt.formats.layouts import SC_CODE_OFFSET
from scopt.formats.sc_v5 import RECORD_SIZE, ScHeader, sparse_copy
from scopt.run.launcher import norm_b0, norm_guest, parse_bench, scrub_env
from scopt.run.proof import (VERDICT_INVALID, VERDICT_PASS, deploy_refusal, deployed_state,
                             prove, prove_run)

B0 = Path("/data/extra_copy/fastdyn/runs/b0")


def test_scrub_env():
    base = {"PATH": "/bin", "QEMU_SC_CAPTURE": "/x.scm", "QEMU_SC_PROFILE": "/y",
            "FASTDYN_QEMU_ICOUNT": "shift=0", "FASTDYN_QEMU": "/q", "FASTDYN_PERF_MODE": "stat"}
    env, diff = scrub_env(base)
    assert env == {"PATH": "/bin", "FASTDYN_QEMU": "/q"}
    assert diff["unset"] == ["FASTDYN_PERF_MODE", "FASTDYN_QEMU_ICOUNT", "QEMU_SC_CAPTURE",
                             "QEMU_SC_PROFILE"]
    env, diff = scrub_env(base, qemu="/other", extra={"QEMU_SC_CAPTURE": "/z.scm"})
    assert env["FASTDYN_QEMU"] == "/other" and env["QEMU_SC_CAPTURE"] == "/z.scm"
    assert diff["set"] == {"FASTDYN_QEMU": "/other", "QEMU_SC_CAPTURE": "/z.scm"}


LONG = "/a/very/long/run/directory/path/that/exceeds/sixty/characters/rd"
LOG = f"""fastdyn.runner|INFO|  Creating output directory at path: {LONG}/work
fastdyn.timing|INFO|  [timing] fastdyn:x 0.001s ok
fastdyn.targets.qemu_target|INFO|  QEMU executable: /q/qemu-system-arm (from $FASTDYN_QEMU)
fastdyn.targets.qemu_target|INFO|  Running following qemu command:
/q/qemu-system-arm \\
  -M cortexm,memory-backend=ram0 \\
  -qmp unix:{LONG}/qmp.sock,
    server,
    nowait \\
  --semihosting \\
  -object memory-backend-file,
    id=ram0,
    share=on,
    mem-path={LONG}/ram/ram0.ram \\
  -accel tcg,
    supercache={LONG}/cache/m.sc \\
  --plugin build/libfastdyn.so,
    dev={LONG}/work/dev_config.json,
    log_level=20,
    finline=None
fastdyn.targets.qemu_target|INFO|  Letting QEMU run.
Reg 0:0x00000003
[BENCH][minimal] elapsed_ns=1234 elapsed_us=1.234 elapsed_s=0.000001234
fastdyn.targets.qemu_target|INFO|  QEMU exited with code 0
"""
# what B0's line-dropping normalization makes of the same run with short paths
B0_STYLE = """fastdyn.targets.qemu_target|INFO|  Running following qemu command:
  -M cortexm,memory-backend=ram0 \\
  --semihosting \\
  -object memory-backend-file,
    id=ram0,
    share=on,
  --plugin build/libfastdyn.so,
    log_level=20,
    finline=None
fastdyn.targets.qemu_target|INFO|  Letting QEMU run.
Reg 0:0x00000003
fastdyn.targets.qemu_target|INFO|  QEMU exited with code 0
WALL 0.22 s RSS 48740 KB
"""


def test_norm_b0_is_independent_of_path_wrapping():
    assert norm_b0(LOG) == norm_b0(B0_STYLE)
    assert "  --plugin build/libfastdyn.so,log_level=20,finline=None" in norm_b0(LOG)
    assert norm_b0(LOG.replace("log_level=20", "log_level=30")) != norm_b0(B0_STYLE)
    assert parse_bench(LOG) == [{"tag": "minimal", "elapsed_ns": 1234}]
    assert norm_guest(LOG)[1] == "[BENCH][minimal] elapsed=<host-time>"


@pytest.mark.real
def test_norm_b0_matches_b0_files():
    if not (B0 / "warm1.log").exists():
        pytest.skip("B0 runs not present")
    for name in ("warm1", "warm2", "cold", "nocache"):
        assert norm_b0((B0 / f"{name}.log").read_text()) == norm_b0((B0 / f"{name}.norm").read_text())
    assert norm_b0((B0 / "warm1.log").read_text()) == norm_b0((B0 / "nocache.log").read_text())


# ---------------------------------------------------------------- proof

@pytest.fixture()
def deployed(tmp_path, minimal_sc):
    sc = tmp_path / "cache" / "m.sc"
    sc.parent.mkdir()
    sparse_copy(minimal_sc, sc, fsync=False)
    man = tmp_path / "cache" / "m.sc.fastdyn.json"
    man.write_text(json.dumps({"version": 1, "fingerprint": "ab" * 32, "inputs": {}}))
    return sc, man, deployed_state(sc, man)


def _hdr(sc):
    with open(sc, "rb") as f:
        return ScHeader.from_bytes(f.read(80))


def _put_hdr(sc, h):
    fd = os.open(sc, os.O_RDWR)
    os.pwrite(fd, h.with_checksum().to_bytes(), 0)
    os.close(fd)


@pytest.mark.real
def test_proof_pass_and_appended(deployed):
    sc, man, st = deployed
    assert st["n"] == 163
    r = prove(st)
    assert r["verdict"] == VERDICT_PASS and r["appended"] == 0
    # QEMU appends one record and rewrites the header: still PASS, appended = 1
    h = _hdr(sc)
    fd = os.open(sc, os.O_RDWR)
    os.pwrite(fd, b"\x11" * RECORD_SIZE, SC_CODE_OFFSET + h.code_size + 163 * RECORD_SIZE)
    os.close(fd)
    _put_hdr(sc, h.replace(record_count=164))
    r = prove(st)
    assert r["verdict"] == VERDICT_PASS and r["appended"] == 1


@pytest.mark.real
@pytest.mark.parametrize("change,failed", [
    ("version", ["header"]),
    ("code_address", ["header"]),
    ("runtime_signature", ["header"]),
    ("record_count_down", ["header"]),
    ("record_byte", ["records"]),
    ("manifest", ["manifest"]),
    ("manifest_missing", ["manifest"]),
    ("recreated", ["inode"]),
    ("deleted", ["sc_exists", "manifest"]),
])
def test_proof_detects(deployed, change, failed):
    sc, man, st = deployed
    h = _hdr(sc)
    if change == "version":
        _put_hdr(sc, h.replace(version=6))
    elif change == "code_address":
        _put_hdr(sc, h.replace(code_address=h.code_address + 0x1000))
    elif change == "runtime_signature":
        _put_hdr(sc, h.replace(runtime_signature=h.runtime_signature ^ 1))
    elif change == "record_count_down":
        _put_hdr(sc, h.replace(record_count=100))
    elif change == "record_byte":
        fd = os.open(sc, os.O_RDWR)
        off = SC_CODE_OFFSET + h.code_size + 5 * RECORD_SIZE + 3
        b = os.pread(fd, 1, off)
        os.pwrite(fd, bytes([b[0] ^ 1]), off)
        os.close(fd)
    elif change == "manifest":
        man.write_text(json.dumps({"version": 1, "fingerprint": "cd" * 32, "inputs": {}}))
    elif change == "manifest_missing":
        man.unlink()
    elif change == "recreated":
        tmp = sc.with_suffix(".tmp")
        sparse_copy(sc, tmp, fsync=False)
        os.replace(tmp, sc)
    elif change == "deleted":
        sc.unlink()
        man.write_text(json.dumps({"version": 1, "fingerprint": "cd" * 32, "inputs": {}}))
    r = prove(st)
    assert r["verdict"] == VERDICT_INVALID
    assert r["failed"] == failed
    # unknown, never zero: an INVALID run has no appended count
    assert r["appended"] is None


def test_deploy_refusal_and_launch_guard(tmp_path):
    """A refused deploy is never launched or proved (review finding on WP8)."""
    from types import SimpleNamespace

    from scopt.run.launcher import RunDirError, create_run_dir, launch

    rd = SimpleNamespace(root=tmp_path)
    assert deploy_refusal(rd) is None
    assert prove_run(rd)["failed"] == ["deployed"]
    (tmp_path / "deploy.json").write_text(json.dumps({"refused": ["bundle_mismatch"]}))
    assert "bundle_mismatch" in deploy_refusal(rd)
    r = prove_run(rd)
    assert r["verdict"] == VERDICT_INVALID and r["appended"] is None
    (tmp_path / "deploy.json").write_text(json.dumps({"refused": "check_fingerprint_mismatch"}))
    assert "check_fingerprint_mismatch" in deploy_refusal(rd)
    (tmp_path / "deploy.json").write_text(json.dumps({"schema": "x"}))
    assert deploy_refusal(rd) == "deploy.json has no deployed state"

    base = Path("/data/extra_copy/fastdyn/src/fastdyn/configs/b0-sc.toml")
    if not base.exists():
        pytest.skip("b0-sc.toml not present")
    real = create_run_dir(base, tmp_path / "rd")
    (real.root / "deploy.json").write_text(json.dumps({"refused": ["config_mismatch"]}))
    with pytest.raises(RunDirError) as e:
        launch(real)  # raises before FastDyn/QEMU would start
    assert e.value.code == "deploy_refused"
    assert not (real.root / "run.json").exists() and not (real.root / "stdout.log").exists()
