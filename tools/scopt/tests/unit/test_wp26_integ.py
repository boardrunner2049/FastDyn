"""Offline tests of the WP26 integration driver's pure helpers
(tests/qemu/wp26_integ.py): the launch-style verdict of a run started under a
guest debugger, and the F-B3-18 judges (gate side / qint side). No QEMU."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from scopt.paths import fastdyn_root

QEMU_DIR = Path(__file__).resolve().parents[1] / "qemu"
FIX = (fastdyn_root() / "fixtures/b3")

pytestmark = pytest.mark.skipif(not (FIX / "expected.py").is_file(),
                                reason=f"{FIX / 'expected.py'} not present")


@pytest.fixture(scope="module")
def W():
    sys.path.insert(0, str(QEMU_DIR))
    import wp26_integ

    return wp26_integ


def test_launch_style_verdict_follows_the_launcher(W):
    ok = {"verdict": "PASS"}
    assert W.launch_style_verdict(ok, 0, []) == "PASS"
    assert W.launch_style_verdict(ok, 1, []) == "INVALID_exit_code"
    assert W.launch_style_verdict(None, 0, []) == "NOT_PROVED"
    assert W.launch_style_verdict(None, 3, []) == "INVALID_exit_code"
    assert W.launch_style_verdict(ok, 0, ["x.scm"]) == "INVALID_stray_capture_files"
    bad = {"verdict": "INVALID_artifact_not_used"}
    assert W.launch_style_verdict(bad, 0, []) == "INVALID_artifact_not_used"
    assert W.launch_style_verdict(bad, 1, []) == "INVALID_artifact_not_used"


def _gate(**kw):
    r = {"qemu_ok": True, "verdict": "PASS", "proof": "PASS", "exit_code": 0,
         "proof_rows": {"header": True, "records": True, "v6_header": True, "ext": True}}
    r.update(kw)
    return r


def test_judge_gate(W):
    assert W.judge_gate(_gate()) == []
    assert W.judge_gate(_gate(qemu_ok=False))
    assert W.judge_gate(_gate(verdict="INVALID_artifact_not_used"))
    # a v5 proof (no v6 rows) is not a proved v6 run
    assert W.judge_gate(_gate(proof_rows={"header": True, "records": True,
                                          "v6_header": None, "ext": None}))
    assert W.judge_gate(_gate(proof_rows={"header": True, "records": True,
                                          "v6_header": True, "ext": False}))


def _qint(**kw):
    r = {"qemu_ok": True, "verdict": "INVALID_artifact_not_used",
         "proof": "INVALID_artifact_not_used", "failed": ["header", "v6_header"],
         "exit_code": 0}
    r.update(kw)
    return r


def test_judge_qint(W):
    assert W.judge_qint(_qint()) == []
    # the run must have been on qint (FastDyn takes qemu_path from the TOML)
    assert W.judge_qint(_qint(qemu_ok=False))
    assert W.judge_qint(_qint(proof="PASS", verdict="PASS", failed=[]))
    # INVALID for another reason (e.g. only a runtime_signature header change)
    assert W.judge_qint(_qint(failed=["header"]))
    assert W.judge_qint({"deploy_refused": "runtime_mismatch:qemu_sha256"})
    assert W.judge_qint(_qint(exit_code=1))


def test_field_differences(W):
    def log(recs):
        lines = ["Reg 0:0x00500000", "Reg 1:0x00000203"]
        for r in recs:
            for i, v in enumerate(r):
                lines += [f"Reg 0:0x{0x530000 | i:08x}", f"Reg 1:{v:#010x}"]
        return lines

    a = log([[1, 2, 3, 4, 5, 6, 7, 8], [9, 10, 11, 12, 13, 14, 15, 16]])
    b = log([[1, 2, 3, 4, 5, 6, 7, 8], [9, 99, 11, 12, 13, 14, 15, 17], [1] * 8])
    fd = W.field_differences(a, b)
    assert fd["paired"] == 2 and fd["artifact_records"] == 3
    assert fd["pc"] == 1 and fd["iter"] == 1 and fd["cvr"] == 0
