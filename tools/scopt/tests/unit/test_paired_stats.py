from __future__ import annotations

import json
import math
import random
import statistics
from pathlib import Path

import pytest

from scopt.run import paired_stats as ps

_RESULTS = Path(__file__).parents[2] / "docs" / "sealed-whopr-7-current-results.json"
_SEED = 0x57484F50


def _legacy_whopr_interval(values, seed, rounds=20_000):
    # measure_sealed_whopr.interval before the shared module.
    rng = random.Random(seed)
    draws = sorted(statistics.median(rng.choices(values, k=len(values)))
                   for _ in range(rounds))
    return [draws[int(rounds * .025)], draws[int(rounds * .975)]]


def _legacy_ablation_interval(values, rng, rounds=10_000):
    draws = sorted(statistics.median(rng.choices(values, k=len(values)))
                   for _ in range(rounds))
    return [draws[int(.025 * rounds)], draws[min(rounds - 1, int(.975 * rounds))]]


def test_bootstrap_reproduces_published_whopr_interval():
    doc = json.loads(_RESULTS.read_text())
    ci = ps.bootstrap_median_ci(doc["paired_speedups_baseline_over_whopr7"],
                                random.Random(_SEED ^ 0x37))
    assert ci == [0.8263290413666876, 0.8766157776894329]


def test_bootstrap_reproduces_lowering_control_interval():
    ratios = [0.9674867566337778, 0.7937452249373115, 0.8381420959483689,
              1.007558863634083, 0.9793437038844826, 1.0066617521946393,
              0.9993483202815032, 0.9295540830596818]
    assert ps.bootstrap_median_ci(ratios, random.Random(_SEED ^ 0x37)) == [
        0.8381420959483689, 1.0066617521946393]


@pytest.mark.parametrize("seed", [1, 7, 0xF17E, 0x57484F50])
def test_bootstrap_equals_legacy_driver_intervals(seed):
    gen = random.Random(seed * 31)
    values = [gen.uniform(.8, 1.2) for _ in range(gen.randint(2, 15))]
    assert (ps.bootstrap_median_ci(values, random.Random(seed))
            == _legacy_whopr_interval(values, seed))
    assert (ps.bootstrap_median_ci(values, random.Random(seed), 10_000)
            == _legacy_ablation_interval(values, random.Random(seed)))


def test_bootstrap_empty_is_nan():
    assert all(math.isnan(x) for x in ps.bootstrap_median_ci([], random.Random(0)))


def test_sign_test_exact_values():
    assert ps.sign_test_p([1.1] * 6) == 0.03125
    assert ps.sign_test_p([0.9] * 3) == 0.25
    assert ps.sign_test_p([1.0, 1.0]) == 1.0
    # ties are dropped: 5 of 5 above.
    assert ps.sign_test_p([1.0, 1.1, 1.2, 1.3, 1.4, 1.5]) == 0.0625
    assert ps.sign_test_p([1.1, 0.9, 1.1, 0.9]) == 1.0


@pytest.mark.parametrize("ci", [(0.98, 1.03), (1.01, 1.04), (0.95, 0.99), (0.9, 1.2)])
def test_noise_band_contains_one_and_is_inversion_symmetric(ci):
    lo, hi = ps.noise_band(ci)
    assert lo <= 1.0 <= hi
    inv_lo, inv_hi = ps.noise_band((1 / ci[1], 1 / ci[0]))
    assert inv_lo == pytest.approx(lo) and inv_hi == pytest.approx(hi)


def _floor(band):
    return {"band": list(band)}


def test_verdicts():
    rng = random.Random(3)
    six = ps.summarize_pair([1.03] * 6, rng=rng)
    assert six["verdict"] == "underpowered" and six["underpowered"]
    assert six["n_blocks"] == 6 and six["blocks_above_one"] == 6

    within = ps.summarize_pair([1.02, 1.05] * 4, rng=rng, floor=_floor((0.98, 1.02)))
    assert within["ci95"][0] <= 1.02
    assert within["within_noise"] is True and within["verdict"] == "within_noise"

    above = ps.summarize_pair([1.03, 1.05] * 4, rng=rng, floor=_floor((0.98, 1.02)))
    assert above["within_noise"] is False and above["verdict"] == "above_floor"

    below = ps.summarize_pair([0.90, 0.92] * 4, rng=rng, floor=_floor((0.98, 1.02)))
    assert below["verdict"] == "below_floor"

    ns = ps.summarize_pair([0.98, 1.03] * 4, rng=rng)
    assert ns["within_noise"] is None and ns["verdict"] == "not_significant"
    assert ps.summarize_pair([1.1, 1.2] * 4, rng=rng)["verdict"] == "above_one"
    assert ps.summarize_pair([0.8, 0.9] * 4, rng=rng)["verdict"] == "below_one"


def test_nan_floor_band_is_ignored():
    out = ps.summarize_pair([1.1, 1.2] * 4, rng=random.Random(0),
                            floor=_floor((math.nan, math.nan)))
    assert out["within_noise"] is None and out["verdict"] == "above_one"


def test_aa_floor_flags_harness_bias():
    biased = ps.aa_floor([1.03, 1.04] * 4, rng=random.Random(1))
    assert biased["harness_bias"] is True
    assert biased["band"][0] <= 1.0 <= biased["band"][1]
    assert biased["band"][1] >= 1.03
    clean = ps.aa_floor([0.99, 1.01] * 4, rng=random.Random(1))
    assert clean["harness_bias"] is False


def test_check_min_blocks():
    assert ps.check_min_blocks(8) is None
    with pytest.raises(ps.UnderpoweredError):
        ps.check_min_blocks(6)
    assert "underpowered" in ps.check_min_blocks(6, allow=True)
    with pytest.raises(ValueError):
        ps.check_min_blocks(6, 0)


def test_aa_arm_name():
    assert ps.aa_arm_name("sealed", ["sealed", "pgo"]) == "sealed-aa"
    assert ps.aa_arm_name("sealed", ["sealed"], "copy") == "copy"
    with pytest.raises(ValueError):
        ps.aa_arm_name("sealed", ["sealed", "sealed-aa"])


def test_protocol_record_marks_underpowered():
    record = ps.protocol_record(blocks=6, min_blocks=8, allow_few_blocks=True,
                                rounds=ps.DEFAULT_ROUNDS, seed=1, aa_control=None,
                                ratio="a/b")
    assert record["schema"] == ps.SCHEMA and record["underpowered"] is True
    assert record["primary_statistic"] == ps.PRIMARY_STATISTIC
