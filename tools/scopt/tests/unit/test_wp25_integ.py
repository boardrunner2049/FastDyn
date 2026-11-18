"""Offline tests of the WP25 integration helpers (``tests/qemu/wp25_integ.py``,
``tests/qemu/wp25_timing_analyze.py``). No QEMU run."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from scopt.paths import fastdyn_root

HERE = Path(__file__).resolve().parents[1] / "qemu"
FX = (fastdyn_root() / "fixtures/b3")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def integ():
    if not (FX / "b3conf.py").is_file():
        pytest.skip(f"{FX / 'b3conf.py'} not present")
    return _load("wp25_integ")


def test_unit_pcs_from_insn_words(integ):
    ins = [SimpleNamespace(words=(w, 0, 0)) for w in (0x58, 0x5c, 0x5e, 0x6a)]
    u = SimpleNamespace(vaddrs=(0xc58,), insns=ins)
    assert integ.unit_pcs(u) == [0xc58, 0xc5c, 0xc5e, 0xc6a]
    assert integ.unit_pcs(SimpleNamespace(vaddrs=(0x100,), insns=())) == [0x100]


def test_kernel_labels_resolve(integ):
    for cfg, lab in integ.KERNEL_LABEL.items():
        assert cfg in integ.CONFIGS
        assert integ.sym(lab) > 0


def test_noicount_timer_phase_has_no_gated_o2a(integ):
    # b4-3 is the only timer phase among F-B4-1..7 (fixtures/b3/b3conf.py)
    timers = [c for c in integ.CONFIGS if integ.b3conf.BY_NAME[c].timing]
    assert timers == ["b4-3"]


def _runs(tmp: Path, arm_ns: dict[str, list[int]]) -> Path:
    rows = []
    for arm, vals in arm_ns.items():
        for i, v in enumerate(vals):
            rows.append({"arm": arm, "rep": i // 3, "input": i % 3, "valid": True,
                         "verdict": "PASS", "appended": 0, "bench_ns": v})
    rows.append({"arm": "b4", "rep": 99, "input": 0, "valid": False, "verdict": "INVALID",
                 "reason": "x", "bench_ns": 1})
    d = tmp / "timing"
    d.mkdir()
    (d / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return d


def test_timing_analysis_paired_speedup(tmp_path):
    n = 30
    d = _runs(tmp_path, {"warm": [1000] * n, "b4": [1000] * n, "b4-d1": [1000] * n,
                         "b4-d2": [1000] * n, "b4-d3": [1000] * n, "sfb": [900] * n})
    out = tmp_path / "a.json"
    p = subprocess.run([sys.executable, str(HERE / "wp25_timing_analyze.py"), str(d),
                        "--out", str(out)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    doc = json.loads(out.read_text())
    assert len(doc["invalid"]) == 1
    b4 = doc["arms"]["b4_vs_warm"]
    assert b4["n_pairs"] == n and b4["speedup_median"] == 0.0
    assert b4["verdict"] == "no_significant_difference" and not b4["meets_r11_threshold"]
    sfb = doc["arms"]["sfb_vs_warm"]
    assert abs(sfb["speedup_median"] - (1000 / 900 - 1)) < 1e-12
    assert sfb["verdict"] == "faster" and sfb["meets_r11_threshold"]
