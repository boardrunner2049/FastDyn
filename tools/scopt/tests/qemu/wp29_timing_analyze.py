#!/usr/bin/env python3
"""WP29: paired speedups of the B4 gate timing rows on the gate binary (reported outcome).

    wp29_timing_analyze.py TIMING_DIR [--out FILE]

(Same method as ``wp25_timing_analyze.py``; arms b4, b4-d1..d3 and sfb against warm,
sfb-b4 against sfb = B4's marginal effect on top of the icount-entry-sfb fix.)

Pairs are (rep, input) with both arms valid; speedup = t_ref / t_arm - 1 on the
guest-reported ``bench_ns``; 95 % percentile bootstrap CI of the median (10 000
resamples over pairs, seed 0), as WP16's ``analyze.py`` does. The R11 threshold
(>= 1.0 % with the CI excluding 0) is reported per arm, never used as a gate.

Seed sensitivity: the CI is recomputed for seeds 0..``--seeds``-1 (default 6). When the
sign of a CI bound differs between seeds, the arm's verdict is ``borderline`` (the seed-0
verdict is kept as ``verdict_seed0``).
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

B = 10000
THRESH = 0.01
#: (arm, reference) pairs: every arm vs warm
PAIRS = [("b4", "warm"), ("b4-d1", "warm"), ("b4-d2", "warm"), ("b4-d3", "warm"),
         ("sfb", "warm"), ("sfb-b4", "sfb"), ("sfb-b4", "warm")]


def boot(vals, seed=0):
    rnd = random.Random(seed)
    n = len(vals)
    bs = sorted(statistics.median([vals[rnd.randrange(n)] for _ in range(n)])
                for _ in range(B))
    return statistics.median(vals), bs[int(0.025 * B)], bs[int(0.975 * B) - 1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("timing", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--seeds", type=int, default=6)
    a = ap.parse_args()
    rows = [json.loads(x) for x in (a.timing / "runs.jsonl").read_text().splitlines() if x]
    by: dict = defaultdict(dict)
    invalid = []
    for r in rows:
        if not r.get("valid"):
            invalid.append({k: r.get(k) for k in ("arm", "rep", "input", "reason", "verdict")})
            continue
        by[(r["rep"], r["input"])][r["arm"]] = r
    res: dict = {"timing_dir": str(a.timing), "runs": len(rows), "invalid": invalid,
                 "method": "median of paired ratios t_ref/t_arm over (rep, input); 95% "
                           "percentile bootstrap, 10000 resamples, seed 0; seed "
                           f"sensitivity over seeds 0..{a.seeds - 1}",
                 "verdicts_seen": sorted({str(r.get("verdict")) for r in rows}),
                 "appended_seen": sorted({r.get("appended") for r in rows
                                          if r.get("appended") is not None}),
                 "arms": {}}
    for arm, ref in PAIRS:
        s = [(by[k][ref]["bench_ns"], by[k][arm]["bench_ns"]) for k in sorted(by)
             if arm in by[k] and ref in by[k]]
        if not s:
            res["arms"][f"{arm}_vs_{ref}"] = {"n_pairs": 0}
            continue
        ratios = [tr / ta for tr, ta in s]
        med, lo, hi = boot(ratios)
        by_seed = [boot(ratios, seed)[1:] for seed in range(a.seeds)]
        lo_signs = {(x[0] > 1) - (x[0] < 1) for x in by_seed}
        hi_signs = {(x[1] > 1) - (x[1] < 1) for x in by_seed}
        v0 = "faster" if lo > 1 else "slower" if hi < 1 else "no_significant_difference"
        per_input: dict = defaultdict(list)
        for k in sorted(by):
            if arm in by[k] and ref in by[k]:
                per_input[k[1]].append(by[k][ref]["bench_ns"] / by[k][arm]["bench_ns"])
        res["arms"][f"{arm}_vs_{ref}"] = {
            "n_pairs": len(s), "speedup_median": med - 1, "ci95": [lo - 1, hi - 1],
            "geomean": statistics.geometric_mean(ratios) - 1,
            "faster_share": sum(r > 1 for r in ratios) / len(ratios),
            "median_ms_ref": statistics.median(tr for tr, _ in s) / 1e6,
            "median_ms_arm": statistics.median(ta for _, ta in s) / 1e6,
            "per_input_median_speedup": {str(i): statistics.median(v) - 1
                                         for i, v in sorted(per_input.items())},
            "meets_r11_threshold": (lo - 1) > 0 and (med - 1) >= THRESH,
            "ci95_by_seed": [[x[0] - 1, x[1] - 1] for x in by_seed],
            "ci_bound_signs_seed_stable": len(lo_signs) == 1 and len(hi_signs) == 1,
            "verdict_seed0": v0,
            "verdict": v0 if len(lo_signs) == 1 and len(hi_signs) == 1 else "borderline"}
    txt = json.dumps(res, indent=1, sort_keys=True)
    if a.out:
        a.out.write_text(txt + "\n")
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
