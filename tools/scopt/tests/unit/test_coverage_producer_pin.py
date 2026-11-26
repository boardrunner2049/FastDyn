"""The translation-coverage overlays must build on FastDyn's current QEMU gitlink."""

import json
import os
from pathlib import Path
import subprocess

import pytest


SCOPT = Path(__file__).resolve().parents[2]
ROOT = SCOPT.parents[1]


def pinned_qemu():
    return subprocess.check_output(
        ["git", "-C", ROOT, "rev-parse", "HEAD:third_party/common/qemu"], text=True
    ).strip()


def test_host_fixture_uses_runtime_gitlink():
    lock = json.loads((SCOPT / "fixtures/host-inline/source-lock.json").read_text())
    assert lock["qemu"] == pinned_qemu()


def test_coverage_overlays_apply_in_order(tmp_path):
    source = Path(os.environ.get("SCOPT_QEMU_SRC", ROOT / "third_party/common/qemu"))
    files = ("accel/tcg/cpu-exec.c", "tcg/supercache.c", "include/tcg/supercache.h")
    if not (source / files[0]).is_file():
        pytest.skip("set SCOPT_QEMU_SRC or initialize the pinned QEMU submodule")
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        data = subprocess.check_output(["git", "-C", source, "show", f"{pinned_qemu()}:{name}"])
        target.write_bytes(data)
    for name in ("qemu-fixture-pretranslate.patch", "qemu-coverage-revocation.patch"):
        subprocess.run(["git", "apply", str(SCOPT / "patches" / name)],
                       cwd=tmp_path, check=True)
