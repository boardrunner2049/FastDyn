from pathlib import Path
import sys

import pytest

pytest.importorskip("scopt")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import compare
from scopt.run.paired_stats import UnderpoweredError


def test_aa_arm_copies_the_base_artifact():
    arms, aa = compare.arms_with_aa({"sfb": "/a/sfb", "b3": "/a/b3"})
    assert aa == ("sfb-aa", "sfb")
    assert list(arms) == ["sfb", "b3", "sfb-aa"] and arms["sfb-aa"] == "/a/sfb"
    assert compare.arms_with_aa({"sfb": "/a"}, enabled=False) == ({"sfb": "/a"}, None)
    with pytest.raises(ValueError):
        compare.arms_with_aa({"sfb": "/a", "sfb-aa": "/b"})


def test_two_reps_are_refused_without_override():
    with pytest.raises(UnderpoweredError):
        compare.protocol_check(2, {})
    assert "underpowered" in compare.protocol_check(2, {"TREBUCHET_ALLOW_FEW_BLOCKS": "1"})
    assert compare.protocol_check(3, {}) is None
    assert "underpowered" in compare.protocol_check(
        3, {"TREBUCHET_MIN_BLOCKS": "12", "TREBUCHET_ALLOW_FEW_BLOCKS": "1"})


def test_summarize_excludes_the_control_from_speedup_rows():
    samples = []
    for block in range(9):
        samples += [{"block": block, "arm": "sfb", "guest_ns": 1000},
                    {"block": block, "arm": "b3", "guest_ns": 900 + block},
                    {"block": block, "arm": "sfb-aa", "guest_ns": 1010 if block % 2 else 990}]
    out = compare.summarize(samples, ["sfb", "b3", "sfb-aa"], ("sfb-aa", "sfb"))
    assert list(out["paired"]) == ["b3"]
    assert out["paired"]["b3"]["verdict"] == "above_floor"
    assert out["noise_floor"]["arm"] == "sfb-aa"
    assert out["median_guest_ms"]["sfb"] == pytest.approx(0.001)
