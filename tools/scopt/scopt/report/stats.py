"""Timing statistics for the report (WP13; spec §8, design §7 F-B2-7).

Everything here is deterministic: the bootstrap uses its own seeded
``random.Random``, so the same samples always give the same interval.

Conventions:

* A *speedup* is ``t_reference / t_arm``: above 1 the arm is faster than the
  reference, below 1 it is slower.
* *Paired* statistics use samples taken in the same interleaved repetition
  (and on the same input); the per-pair ratio removes slow drifts of the
  host. The reported value is the median of the per-pair ratios, with a
  percentile bootstrap CI over pairs (pairs resampled with replacement).
* *Unpaired* statistics (no pair keys) use the ratio of medians, with each
  arm resampled independently. They are labelled as such.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Sequence
from typing import Any

__all__ = ["quantile", "describe", "bootstrap_ci", "paired_speedup", "unpaired_speedup",
           "DEFAULT_BOOTSTRAP", "DEFAULT_CONFIDENCE"]

DEFAULT_BOOTSTRAP = 10000
DEFAULT_CONFIDENCE = 0.95


def quantile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (numpy's default, ``type 7``)."""
    if not xs:
        raise ValueError("quantile of an empty sample")
    s = sorted(xs)
    if len(s) == 1:
        return float(s[0])
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return float(s[lo] + (s[hi] - s[lo]) * (pos - lo))


def describe(xs: Sequence[float]) -> dict[str, Any]:
    """n, median, min, max, quartiles, MAD and mean of a sample (``None`` when empty)."""
    xs = [float(x) for x in xs if x is not None]
    if not xs:
        return {"n": 0, "median": None, "min": None, "max": None, "q1": None, "q3": None,
                "mad": None, "mean": None}
    med = statistics.median(xs)
    return {
        "n": len(xs), "median": med, "min": min(xs), "max": max(xs),
        "q1": quantile(xs, 0.25), "q3": quantile(xs, 0.75),
        "mad": statistics.median([abs(x - med) for x in xs]),
        "mean": statistics.fmean(xs),
    }


def bootstrap_ci(stat: Callable[..., float], samples: Sequence[Sequence[float]], *,
                 reps: int = DEFAULT_BOOTSTRAP, confidence: float = DEFAULT_CONFIDENCE,
                 seed: int = 0) -> tuple[float | None, float | None]:
    """Percentile bootstrap CI of ``stat(*samples)``; each sample resampled independently.

    Returns ``(None, None)`` when a sample has fewer than 2 values (no
    meaningful interval).
    """
    if any(len(s) < 2 for s in samples) or reps <= 0:
        return None, None
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        res = [rng.choices(s, k=len(s)) for s in samples]
        vals.append(stat(*res))
    a = (1.0 - confidence) / 2.0
    return quantile(vals, a), quantile(vals, 1.0 - a)


def _verdict(lo: float | None, hi: float | None, med: float | None) -> str:
    if med is None:
        return "no_data"
    if lo is None or hi is None:
        return "insufficient_samples"
    if hi < 1.0:
        return "slower"           # the whole CI is below 1: significant slowdown
    if lo > 1.0:
        return "faster"
    return "no_significant_difference"


def _sample_problem(v: Any) -> str | None:
    """Why a timing value cannot be used (None when it can)."""
    if v is None:
        return "missing"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "not_a_number"
    if math.isnan(x) or math.isinf(x):
        return "not_finite"
    if x <= 0.0:
        return "zero" if x == 0.0 else "negative"
    return None


def paired_speedup(ref: Sequence[float], arm: Sequence[float], *,
                   reps: int = DEFAULT_BOOTSTRAP, confidence: float = DEFAULT_CONFIDENCE,
                   seed: int = 0) -> dict[str, Any]:
    """Median paired speedup ``ref_i / arm_i`` with a bootstrap CI over pairs.

    A pair whose value on either side is missing, zero, negative or not finite
    cannot give a ratio: it is listed in ``invalid`` (index, values, reason)
    and left out; the remaining pairs are still analysed (``n_invalid``)."""
    if len(ref) != len(arm):
        raise ValueError("paired samples must have equal length")
    ratios: list[float] = []
    invalid: list[dict[str, Any]] = []
    for i, (r, a) in enumerate(zip(ref, arm)):
        pr, pa = _sample_problem(r), _sample_problem(a)
        if pr or pa:
            invalid.append({"index": i, "ref": r, "arm": a,
                            "reason": ", ".join(x for x in (pr and f"ref_{pr}",
                                                            pa and f"arm_{pa}") if x)})
            continue
        ratios.append(float(r) / float(a))
    if not ratios:
        return {"mode": "paired", "n_pairs": 0, "median": None, "ci": [None, None],
                "confidence": confidence, "verdict": "no_data", "ratios": [],
                "n_invalid": len(invalid), "invalid": invalid}
    med = statistics.median(ratios)
    lo, hi = bootstrap_ci(statistics.median, [ratios], reps=reps, confidence=confidence,
                          seed=seed)
    return {"mode": "paired", "n_pairs": len(ratios), "median": med, "ci": [lo, hi],
            "confidence": confidence, "bootstrap_reps": reps, "seed": seed,
            "geomean": math.exp(statistics.fmean(math.log(x) for x in ratios)),
            "frac_faster": sum(1 for x in ratios if x > 1.0) / len(ratios),
            "verdict": _verdict(lo, hi, med), "ratios": ratios,
            "n_invalid": len(invalid), "invalid": invalid}


def unpaired_speedup(ref: Sequence[float], arm: Sequence[float], *,
                     reps: int = DEFAULT_BOOTSTRAP, confidence: float = DEFAULT_CONFIDENCE,
                     seed: int = 0) -> dict[str, Any]:
    """``median(ref) / median(arm)``, both samples resampled independently.

    Missing, zero, negative or non-finite values are listed in ``invalid`` and
    left out (the rest are still analysed)."""
    invalid: list[dict[str, Any]] = []
    good: dict[str, list[float]] = {"ref": [], "arm": []}
    for side, xs in (("ref", ref), ("arm", arm)):
        for i, x in enumerate(xs):
            pb = _sample_problem(x)
            if pb:
                if x is not None:  # None = no sample at all (not an invalid value)
                    invalid.append({"side": side, "index": i, "value": x, "reason": pb})
                continue
            good[side].append(float(x))
    ref, arm = good["ref"], good["arm"]
    if not ref or not arm:
        return {"mode": "unpaired", "n_ref": len(ref), "n_arm": len(arm), "median": None,
                "ci": [None, None], "confidence": confidence, "verdict": "no_data",
                "n_invalid": len(invalid), "invalid": invalid}
    med = statistics.median(ref) / statistics.median(arm)
    lo, hi = bootstrap_ci(lambda r, a: statistics.median(r) / statistics.median(a),
                          [ref, arm], reps=reps, confidence=confidence, seed=seed)
    return {"mode": "unpaired", "n_ref": len(ref), "n_arm": len(arm), "median": med,
            "ci": [lo, hi], "confidence": confidence, "bootstrap_reps": reps, "seed": seed,
            "verdict": _verdict(lo, hi, med), "n_invalid": len(invalid), "invalid": invalid}
