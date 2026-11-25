from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_PATH = (Path(__file__).parents[2] / "scripts" /
         "measure_firmware_inline_ablations.py")
_SPEC = importlib.util.spec_from_file_location("measure_firmware_inline_ablations", _PATH)
assert _SPEC and _SPEC.loader
_M = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _M
_SPEC.loader.exec_module(_M)


def _artifact(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir()
    (path / "cache.sc").write_bytes(b"cache")
    return path


def test_parse_arms_env_and_comparisons(tmp_path):
    warm = _artifact(tmp_path, "warm")
    trusted = _artifact(tmp_path, "trusted")
    arms = _M.parse_arms(
        [f"warm={warm}", f"trusted={trusted}"],
        ["trusted:QEMU_SC_V7_EXPERIMENT=index,no-tree"],
        ["trusted:QEMU_SC_RESTORE_STATS={run_dir}/restore.json"],
    )
    assert list(arms) == ["warm", "trusted"]
    assert arms["trusted"].env == {
        "QEMU_SC_V7_EXPERIMENT": "index,no-tree",
    }
    assert arms["trusted"].diagnostic_env == {
        "QEMU_SC_RESTORE_STATS": "{run_dir}/restore.json",
    }
    assert _M.parse_comparisons(["trusted:warm"], arms) == [("trusted", "warm")]


@pytest.mark.parametrize("row", ["missing", "bad/name=/tmp", "a=/tmp", "a=/tmp"])
def test_parse_arms_rejects_bad_or_duplicate(tmp_path, row):
    good = _artifact(tmp_path, "good")
    values = [f"warm={good}", row]
    if row == "a=/tmp":
        values.append(row)
    with pytest.raises(ValueError):
        _M.parse_arms(values, [])


def test_summarize_preserves_process_pairing(tmp_path):
    arms = {
        "base": _M.Arm("base", _artifact(tmp_path, "base")),
        "candidate": _M.Arm("candidate", _artifact(tmp_path, "candidate")),
    }
    rows = [
        {"block": 0, "arm": "base", "native_ns": 100},
        {"block": 0, "arm": "candidate", "native_ns": 50},
        {"block": 1, "arm": "base", "native_ns": 120},
        {"block": 1, "arm": "candidate", "native_ns": 80},
    ]
    result = _M.summarize(rows, arms, [("candidate", "base")], 2, 7)
    comparison = result["comparisons"]["candidate_vs_base"]
    assert comparison["speedup_per_block"] == [2.0, 1.5]
    assert comparison["median_speedup"] == 1.75
    assert result["median_native_ns"] == {"base": 110.0, "candidate": 65.0}


def test_validate_experiment_requires_exact_runtime_flags(tmp_path):
    arm = _M.Arm("candidate", _artifact(tmp_path, "candidate"), env={
        "QEMU_SC_V7_EXPERIMENT": "source-index,no-tree",
    })
    flags = {field: token in {"source-index", "no-tree"}
             for token, field in _M._EXPERIMENT_KEYS.items()}
    _M.validate_experiment(arm, {"experiment": flags})
    flags["no_tree"] = False
    with pytest.raises(RuntimeError):
        _M.validate_experiment(arm, {"experiment": flags})


@pytest.mark.parametrize("mode", ["safe", "trusted"])
@pytest.mark.parametrize("linear", [False, True])
def test_validate_experiment_checks_source_index_policy(tmp_path, mode, linear):
    tokens = {"no-tree", "linear-source-scan"} if linear else {"no-tree"}
    arm = _M.Arm("candidate", _artifact(tmp_path, "candidate"), env={
        "QEMU_SC_V7_EXPERIMENT": ",".join(sorted(tokens)),
    })
    flags = {field: token in tokens
             for token, field in _M._EXPERIMENT_KEYS.items()}
    flags["source_index"] = not linear
    stats = {"loaded": True, "mode": mode, "experiment": flags}
    _M.validate_experiment(arm, stats)
    flags["source_index"] = linear
    with pytest.raises(RuntimeError):
        _M.validate_experiment(arm, stats)


def test_no_trusted_exit_chain_token_matches_qemu_experiment_key(tmp_path):
    assert _M._EXPERIMENT_KEYS["no-trusted-exit-chain"] == "no_trusted_exit_chain"
    arm = _M.Arm("unchained", _artifact(tmp_path, "unchained"), env={
        "QEMU_SC_V7_EXPERIMENT": "no-trusted-exit-chain",
    })
    # The experiment object QEMU writes for this token, trusted and loaded.
    flags = {"source_index": True, "linear_source_scan": False, "trusted_set": False,
             "no_tree": False, "incoming_chain": False, "disable_chaining": False,
             "no_regions": False, "no_trusted_exit_chain": True}
    _M.validate_experiment(arm, {"loaded": True, "mode": "trusted", "experiment": flags})
    with pytest.raises(RuntimeError):
        _M.validate_experiment(arm, {"loaded": True, "mode": "trusted",
                                     "experiment": {**flags, "no_trusted_exit_chain": False}})
