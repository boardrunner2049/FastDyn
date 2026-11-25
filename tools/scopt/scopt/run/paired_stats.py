"""Paired block statistics shared by the timing drivers.

Every driver measures arms in randomized blocks of fresh processes.  The
primary statistic of a comparison is the median of its per-block ratios, with
a seeded percentile bootstrap interval and an exact sign test.  Per-arm table
medians are secondary.

An A/A control arm runs a byte-identical copy of one reference arm under a
different name in the same shuffled blocks.  Its paired ratio is the harness
noise floor: a comparison whose interval overlaps the floor's band is
``within_noise`` whatever its point estimate.  Fewer than
``DEFAULT_MIN_BLOCKS`` blocks are refused unless the caller explicitly allows
them, and such results are labelled ``underpowered``.

The bootstrap is bit-compatible with the intervals previously published by
``measure_sealed_whopr.py`` (20,000 rounds) and
``measure_firmware_inline_ablations.py`` (10,000 rounds) for the same
generator state.
"""

from __future__ import annotations

import argparse
import math
import random
import statistics
from typing import Any, Iterable, Sequence

SCHEMA = "scopt.paired-protocol/1"
DEFAULT_MIN_BLOCKS = 8
DEFAULT_ROUNDS = 20_000
PRIMARY_STATISTIC = "paired_block_ratio_median"

VERDICTS = ("underpowered", "within_noise", "above_floor", "below_floor",
            "not_significant", "above_one", "below_one")


class UnderpoweredError(ValueError):
    """Fewer blocks than the protocol minimum, and not explicitly allowed."""


def _quantiles(level: float) -> tuple[float, float]:
    if not 0 < level < 1:
        raise ValueError(f"confidence level {level!r} must be in (0, 1)")
    if level == .95:
        return .025, .975
    return (1 - level) / 2, (1 + level) / 2


def bootstrap_median_ci(values: Sequence[float], rng: random.Random,
                        rounds: int = DEFAULT_ROUNDS,
                        level: float = .95) -> list[float]:
    """Percentile bootstrap interval of the median of ``values``.

    Resamples whole blocks with ``rng.choices``; ``[nan, nan]`` for no data.
    """
    lo_q, hi_q = _quantiles(level)
    values = list(values)
    if not values:
        return [math.nan, math.nan]
    if rounds < 1:
        raise ValueError("bootstrap rounds must be positive")
    draws = sorted(statistics.median(rng.choices(values, k=len(values)))
                   for _ in range(rounds))
    return [draws[int(rounds * lo_q)], draws[min(rounds - 1, int(rounds * hi_q))]]


def sign_test_p(values: Iterable[float], null: float = 1.0) -> float:
    """Exact two-sided binomial sign test against ``null``; ties are dropped."""
    above = below = 0
    for value in values:
        if value > null:
            above += 1
        elif value < null:
            below += 1
    n = above + below
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(above, below) + 1))
    return min(1.0, 2 * tail / 2 ** n)


def noise_band(aa_ci: Sequence[float]) -> list[float]:
    """Ratio band covered by an A/A interval, symmetric under inversion.

    Always contains 1, so it can be compared with ratios of either
    orientation (reference/candidate or candidate/reference).
    """
    lo, hi = aa_ci
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo <= 0 or hi <= 0:
        return [math.nan, math.nan]
    return [min(1.0, lo, 1 / hi), max(1.0, hi, 1 / lo)]


def _overlaps(ci: Sequence[float], band: Sequence[float]) -> bool | None:
    if not all(math.isfinite(x) for x in (*ci, *band)):
        return None
    return ci[0] <= band[1] and ci[1] >= band[0]


def summarize_pair(ratios: Sequence[float], *, rng: random.Random,
                   floor: dict[str, Any] | None = None,
                   min_blocks: int = DEFAULT_MIN_BLOCKS,
                   rounds: int = DEFAULT_ROUNDS) -> dict[str, Any]:
    """Primary paired summary of one comparison's per-block ratios.

    ``floor`` is an :func:`aa_floor` result.  The verdict is ``underpowered``
    below ``min_blocks``; otherwise ``within_noise`` when the interval
    overlaps the floor band, else ``above_floor``/``below_floor``.  Without a
    floor it is ``not_significant`` when the interval contains 1, else
    ``above_one``/``below_one``.
    """
    ratios = list(ratios)
    n = len(ratios)
    ci = bootstrap_median_ci(ratios, rng, rounds)
    median = statistics.median(ratios) if ratios else math.nan
    band = floor.get("band") if floor else None
    if band is not None and not all(math.isfinite(x) for x in band):
        band = None
    within = _overlaps(ci, band) if band is not None else None
    underpowered = n < min_blocks
    if underpowered:
        verdict = "underpowered"
    elif within:
        verdict = "within_noise"
    elif band is not None:
        verdict = "above_floor" if ci[0] > band[1] else "below_floor"
    elif not ratios or ci[0] <= 1.0 <= ci[1]:
        verdict = "not_significant"
    else:
        verdict = "above_one" if ci[0] > 1.0 else "below_one"
    return {
        "median": median,
        "ci95": ci,
        "n_blocks": n,
        "blocks_above_one": sum(r > 1.0 for r in ratios),
        "blocks_below_one": sum(r < 1.0 for r in ratios),
        "sign_test_p": sign_test_p(ratios),
        "underpowered": underpowered,
        "within_noise": within,
        "verdict": verdict,
    }


def aa_floor(aa_ratios: Sequence[float], *, rng: random.Random,
             min_blocks: int = DEFAULT_MIN_BLOCKS,
             rounds: int = DEFAULT_ROUNDS) -> dict[str, Any]:
    """Noise floor from the reference/A-A per-block ratios.

    Adds ``band`` (see :func:`noise_band`) and ``harness_bias``: true when the
    A/A interval itself excludes 1, i.e. two identical arms measured apart.
    """
    out = summarize_pair(aa_ratios, rng=rng, min_blocks=min_blocks, rounds=rounds)
    ci = out["ci95"]
    out["band"] = noise_band(ci)
    out["harness_bias"] = (all(math.isfinite(x) for x in ci) and
                           not ci[0] <= 1.0 <= ci[1])
    return out


def check_min_blocks(n: int, min_blocks: int = DEFAULT_MIN_BLOCKS,
                     allow: bool = False) -> str | None:
    """None when ``n`` meets the minimum; the warning text when it is allowed.

    Raises :class:`UnderpoweredError` when ``n`` is below the minimum and
    ``allow`` is false.
    """
    if min_blocks < 1:
        raise ValueError("the minimum block count must be positive")
    if n >= min_blocks:
        return None
    text = f"{n} paired blocks is below the protocol minimum of {min_blocks}"
    if not allow:
        raise UnderpoweredError(f"{text}. Use more blocks, or pass --allow-few-blocks "
                                "to run anyway with results labelled underpowered.")
    return f"{text}; results are labelled underpowered"


def aa_arm_name(reference: str, existing: Iterable[str],
                name: str | None = None) -> str:
    """Name of the A/A copy of ``reference``; refuses a collision."""
    name = name or f"{reference}-aa"
    if name in set(existing):
        raise ValueError(f"A/A control name {name!r} collides with an existing arm")
    return name


def add_protocol_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--min-blocks", type=int, default=DEFAULT_MIN_BLOCKS,
                        help="refuse fewer paired blocks than this "
                             f"(default {DEFAULT_MIN_BLOCKS})")
    parser.add_argument("--allow-few-blocks", action="store_true",
                        help="run below --min-blocks anyway; results are "
                             "labelled underpowered")


def protocol_record(*, blocks: int, min_blocks: int, allow_few_blocks: bool,
                    rounds: int, seed: Any, aa_control: dict[str, Any] | None,
                    ratio: str, warning: str | None = None) -> dict[str, Any]:
    """What a published summary needs to state about how it was computed."""
    return {
        "schema": SCHEMA,
        "primary_statistic": PRIMARY_STATISTIC,
        "ratio": ratio,
        "interval": f"seeded percentile bootstrap of the block-ratio median, "
                    f"{rounds} resamples, 95%",
        "sign_test": "exact two-sided binomial on blocks, ties dropped",
        "noise_floor": ("reference / A-A copy paired ratio; a comparison is "
                        "within_noise when its interval overlaps the floor band"
                        if aa_control else "none (no A/A control arm)"),
        "secondary": "per-arm medians are descriptive only",
        "blocks": blocks,
        "min_blocks": min_blocks,
        "allow_few_blocks": allow_few_blocks,
        "underpowered": blocks < min_blocks,
        "warning": warning,
        "seed": seed,
        "aa_control": aa_control,
    }


def format_row(name: str, summary: dict[str, Any]) -> str:
    lo, hi = summary["ci95"]
    return (f"{name:28s} {summary['median']:.4f}x [{lo:.4f}, {hi:.4f}] "
            f"{summary['blocks_above_one']}/{summary['n_blocks']} >1 "
            f"p={summary['sign_test_p']:.3g} {summary['verdict']}")
