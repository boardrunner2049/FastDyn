from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
import statistics
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[2] / "scripts" / "measure_sealed_whopr.py"
_SPEC = importlib.util.spec_from_file_location("measure_sealed_whopr", _PATH)
assert _SPEC and _SPEC.loader
_M = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _M
_SPEC.loader.exec_module(_M)


def _bundle(root: Path) -> Path:
    try:
        emit_bundle = _M._emit_bundle()
    except ImportError as exc:  # pragma: no cover - environment without FastDyn
        pytest.skip(f"fastdyn unavailable: {exc}")
    root.mkdir()
    (root / "cache").mkdir()
    cache = root / "cache" / "fw.sc"
    with cache.open("wb") as stream:
        stream.write(b"SC7" + bytes(range(200)))
        stream.seek(1 << 20)
        stream.write(b"tail")
    (root / "ram").mkdir()
    (root / "ram" / "input.ram").write_bytes(b"input" * 100)
    (root / "old.log").write_text("ignored")
    argv = ["/usr/bin/qemu-system-arm", "-kernel", "/fw/RTOSDemo.out",
            "-qmp", f"unix:{root}/qmp.sock,server,nowait",
            "-object", f"memory-backend-file,id=input,size=1M,share=off,"
                       f"mem-path={root}/ram/input.ram",
            "-accel", f"tcg,supercache={cache}",
            "--plugin", "/lib/libfastdyn.so,dev=/shared/dev.json"]
    env = {"FASTDYN_TIMING_FILE": f"{root}/assets/timing.jsonl",
           "FASTDYN_CONFIG": "/shared/run.toml"}
    return emit_bundle(root, argv, "/shared", env, disable_aslr=True, allow_existing=True,
                       metadata={"config": "/shared/run.toml"})


def test_clone_launcher_rebases_only_the_bundle(tmp_path):
    source = tmp_path / "baseline"
    run_sh = _bundle(source)
    before = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
    clone = _M.clone_launcher(run_sh, tmp_path / "out" / "aa-control")
    dest = (tmp_path / "out" / "aa-control").resolve()
    assert clone == dest / "run.sh" and os.access(clone, os.X_OK)
    doc = json.loads((dest / "launch.json").read_text())
    text = json.dumps(doc) + clone.read_text()
    assert str(source.resolve()) + "/" not in text.replace(f'"aa_clone_of": "{run_sh}"', "")
    assert f"tcg,supercache={dest}/cache/fw.sc" in doc["argv"]
    assert f"unix:{dest}/qmp.sock,server,nowait" in doc["argv"]
    assert doc["env_set"]["FASTDYN_TIMING_FILE"] == f"{dest}/assets/timing.jsonl"
    assert doc["env_set"]["FASTDYN_CONFIG"] == "/shared/run.toml"
    assert "/lib/libfastdyn.so,dev=/shared/dev.json" in doc["argv"]
    assert doc["disable_aslr"] is True and doc["metadata"]["aa_clone_of"] == str(run_sh)
    assert _M.sha256(dest / "cache/fw.sc") == _M.sha256(source / "cache/fw.sc")
    assert (dest / "ram/input.ram").read_bytes() == (source / "ram/input.ram").read_bytes()
    assert not (dest / "old.log").exists()
    assert _M.cache_path(clone) == dest / "cache/fw.sc"
    after = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert after == before


def test_clone_launcher_refuses_a_destination_inside_the_source(tmp_path):
    run_sh = _bundle(tmp_path / "baseline")
    with pytest.raises(ValueError):
        _M.clone_launcher(run_sh, tmp_path / "baseline" / "aa")


def _rows(pairs, times):
    rows = []
    for block in range(1, pairs + 1):
        for arm, ns in times(block).items():
            rows.append({"block": block, "arm": arm, "native_ns": ns})
    return rows


def test_summarize_keeps_legacy_keys_and_adds_the_floor():
    def times(block):
        return {"baseline": 1000 + block, "whopr7": 1200 + 3 * block,
                "baseline_aa": 1000 + block + (5 if block % 2 else -5)}
    rows = _rows(8, times)
    seed = 0x57484F50
    out = _M.summarize(rows, 8, seed)
    ratios = [(1000 + b) / (1200 + 3 * b) for b in range(1, 9)]
    assert out["paired_speedups"] == ratios
    assert out["median_paired_speedup"] == statistics.median(ratios)
    rng = random.Random(seed ^ 0x37)
    draws = sorted(statistics.median(rng.choices(ratios, k=8)) for _ in range(20_000))
    assert out["bootstrap_95"] == [draws[500], draws[19_500]]
    assert out["comparison"]["ci95"] == out["bootstrap_95"]
    assert out["comparison"]["verdict"] == "below_floor"
    assert out["noise_floor"]["ratio"] == "baseline/baseline_aa"
    assert set(out["median_native_ns"]) == {"baseline", "whopr7", "baseline_aa"}


def test_summarize_without_control_is_unchanged():
    rows = _rows(8, lambda b: {"baseline": 1000, "whopr7": 1000 + b})
    out = _M.summarize(rows, 8, 1)
    assert out["noise_floor"] is None
    assert out["comparison"]["within_noise"] is None


def test_six_pairs_need_allow_few_blocks(capsys):
    base = ["--baseline", "b", "--candidate", "c", "--out", "o"]
    with pytest.raises(SystemExit):
        _M.parse_args(base + ["--pairs", "6"])
    args, warning = _M.parse_args(base + ["--pairs", "6", "--allow-few-blocks"])
    assert args.pairs == 6 and "underpowered" in warning
    assert "WARNING" in capsys.readouterr().err
    args, warning = _M.parse_args(base)
    assert args.pairs == 8 and warning is None and args.aa_control == "auto"
