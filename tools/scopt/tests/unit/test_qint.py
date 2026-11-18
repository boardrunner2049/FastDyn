"""WP-QINT: the T-B1-0 driver's helpers and the integrated binary's provenance.

* ``overhead_stats``/``bootstrap_ci`` on synthetic samples;
* ``freertos_guest_norm`` reproduces ``fixtures/freertos-mps2/run.sh``'s
  ``guest.norm`` on a qualified B0 run (marker ``real``);
* ``with_qemu_path`` changes exactly ``[Machine].qemu_path``;
* the ``qint`` QEMU branch is compat + base + capture + profile, with the same
  patch-ids as the patch files, and the built binary is the one RUNTIME.md
  records (markers ``real``, ``qemu``).
"""

from __future__ import annotations

import importlib.util
import random
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
from scopt.paths import fastdyn_root

HERE = Path(__file__).resolve().parent
DRIVER = HERE.parent / "qemu" / "t_b1_0.py"
ROOT = fastdyn_root()
QINT = ROOT / "wt/qemu-int"
PATCHES = [ROOT / "src/fastdyn/patches/qemu-fastdyn-plugin-icount.patch",
           ROOT / "patches/qemu-sc-base.patch",
           ROOT / "patches/qemu-sc-capture.patch",
           ROOT / "patches/qemu-sc-profile.patch"]


def _load():
    spec = importlib.util.spec_from_file_location("t_b1_0", DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _load()


def test_overhead_stats_known_ratio():
    rng = random.Random(5)
    old = [100.0 + rng.gauss(0, 1) for _ in range(200)]
    new = [x * 1.05 for x in old]
    st = T.overhead_stats(old, new, list(zip(old, new)), reps=2000)
    assert st["overhead_median"] == pytest.approx(0.05, abs=1e-9)
    lo, hi = st["overhead_ci95"]
    assert lo < 0.05 < hi and hi - lo < 0.02
    assert st["flagged"] is True
    assert st["paired"]["median_ratio_minus_1"] == pytest.approx(0.05)
    assert st["paired"]["new_slower_count"] == 200


def test_overhead_stats_not_flagged_and_empty():
    old = [10.0, 11.0, 12.0, 13.0]
    st = T.overhead_stats(old, old, reps=500)
    assert st["overhead_median"] == 0.0 and st["flagged"] is False
    assert "paired" not in st
    assert T.overhead_stats([], [1.0])["overhead_median"] is None


def test_bootstrap_ci_deterministic():
    s = [1.0, 2.0, 3.0, 4.0, 5.0]
    f = lambda x: sum(x) / len(x)  # noqa: E731
    assert T.bootstrap_ci(f, [s], reps=300, seed=3) == T.bootstrap_ci(f, [s], reps=300, seed=3)


def test_with_qemu_path_changes_only_that_key(tmp_path):
    src = tmp_path / "a.toml"
    src.write_text('[Machine]\nplatform = "x"\nqemu_path = "/old/qemu"\nsupercache = "c.sc"\n')
    dst = T.with_qemu_path(src, tmp_path / "out/b.toml", Path("/new/qemu"))
    a, b = tomllib.loads(src.read_text()), tomllib.loads(dst.read_text())
    assert b["Machine"].pop("qemu_path") == "/new/qemu"
    a["Machine"].pop("qemu_path")
    assert a == b


def test_with_qemu_path_requires_exactly_one(tmp_path):
    src = tmp_path / "a.toml"
    src.write_text('[Machine]\nplatform = "x"\n')
    with pytest.raises(SystemExit):
        T.with_qemu_path(src, tmp_path / "b.toml", Path("/q"))


@pytest.mark.real
def test_freertos_guest_norm_matches_run_sh():
    d = ROOT / "runs/b0-freertos/qual-icount/nocache-1"
    if not (d / "stdout.log").is_file():
        pytest.skip("qualified FreeRTOS run not present")
    rc = int((d / "exit_code").read_text())
    got = T.freertos_guest_norm((d / "stdout.log").read_text(errors="replace"), rc)
    assert got == (d / "guest.norm").read_text().splitlines()


def _patch_id(text: str) -> str:
    p = subprocess.run(["git", "patch-id", "--stable"], input=text, capture_output=True,
                       text=True, check=True)
    return p.stdout.split()[0]


@pytest.mark.real
@pytest.mark.qemu
def test_qint_branch_is_the_patch_stack():
    if not (QINT / ".git").exists() or not all(p.is_file() for p in PATCHES):
        pytest.skip("qemu-int worktree or patches not present")
    log = subprocess.run(["git", "-C", str(QINT), "log", "--format=%H", "-4", "qint"],
                         capture_output=True, text=True, check=True).stdout.split()
    commits = list(reversed(log))
    base = subprocess.run(["git", "-C", str(QINT), "rev-parse", f"{commits[0]}~1"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert base.startswith("12f5e68807")
    for c, p in zip(commits, PATCHES):
        show = subprocess.run(["git", "-C", str(QINT), "show", "--format=email", c],
                              capture_output=True, text=True, check=True).stdout
        assert _patch_id(show) == _patch_id(p.read_text()), (c, p.name)


@pytest.mark.real
@pytest.mark.qemu
def test_runtime_md_records_the_built_binary():
    runtime = ROOT / "RUNTIME.md"
    binary = QINT / "build/qemu-system-arm"
    if not runtime.is_file() or not binary.is_file():
        pytest.skip("RUNTIME.md or the integrated binary not present")
    import hashlib
    h = hashlib.sha256(binary.read_bytes()).hexdigest()
    m = re.search(r"qemu-system-arm sha256: `([0-9a-f]{64})`", runtime.read_text())
    assert m, "RUNTIME.md has no 'qemu-system-arm sha256: `...`' line"
    assert m.group(1) == h


@pytest.mark.real
@pytest.mark.qemu
def test_runtime_module_matches_runtime_md():
    from scopt.run import runtime

    if not runtime.RUNTIME_QEMU.is_file():
        pytest.skip("integrated binary not present")
    assert runtime.runtime_qemu() == runtime.RUNTIME_QEMU
    runtime_md = ROOT / "RUNTIME.md"
    if runtime_md.is_file():
        assert runtime.RUNTIME_QEMU_SHA256.split(":", 1)[1] in runtime_md.read_text()
