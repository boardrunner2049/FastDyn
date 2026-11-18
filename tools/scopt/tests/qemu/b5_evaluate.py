#!/usr/bin/env python3
"""B5 evaluation supplement: robustness and correctness tables over a finished B5 run.

    b5_evaluate.py --out-root runs/b5/timing [--out FILE] [--seeds 6] [--load-max 4.0]

Untimed and read-only. It reads each set's ``runs.jsonl`` and ``campaign.json``, the
driver's ``timing-load.log`` and ``report/report.json`` (from ``b5_campaign.py report``)
and writes one JSON file. It adds what the one-command report does not print:

* **Seed sensitivity** (same rule as ``wp28_timing_analyze.py``): the 95 % percentile
  bootstrap CI of the median paired speedup is recomputed for bootstrap seeds
  0..``--seeds``-1 (10 000 resamples each, ``scopt.report.stats.bootstrap_ci``). When the
  sign of a CI bound differs between seeds, the comparison is ``borderline``.
  ``report_ci`` is the CI the report printed (its seed is ``0 + k``, one per comparison).
* **Schedule-seed sensitivity:** an arm timed in two sets of the same split (core and
  ablation sets use different schedule seeds) is listed with both medians.
* **Per input and leave-one-input-out:** the median per input and the verdict with each
  input left out.
* **Drift:** the median over repetitions 0-14 and over 15-29.
* **Load filter:** the median and CI over pairs whose runs all started and finished while
  every 30 s 1-min load sample in that interval was <= ``--load-max``.
* **Correctness per arm and set:** proof verdicts, exit codes, timeouts, guest output
  (``output_norm_sha256``) equal to warm of the same (rep, input), distinct guest outputs per
  input, appended records per input.

Speedup = t_ref / t_arm on ``bench_ns`` (above 1 = the arm is faster), as in
``scopt.report.stats.paired_speedup``. Nothing here is a pass criterion.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tomllib
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2]))
from scopt.report import stats  # noqa: E402

B = stats.DEFAULT_BOOTSTRAP
SETS = ["compute-train-calib", "compute-heldout-core", "compute-heldout-ablation",
        "compute-final-core", "compute-final-ablation"]


def verdict(lo, hi):
    if lo is None or hi is None:
        return "insufficient_samples"
    return "faster" if lo > 1 else "slower" if hi < 1 else "no_significant_difference"


def ci(ratios, seed):
    return stats.bootstrap_ci(statistics.median, [ratios], reps=B, seed=seed)


def load_samples(path: Path) -> list[tuple[float, float]]:
    out = []
    if path.is_file():
        for line in path.read_text().splitlines():
            p = line.split()
            if len(p) >= 2:
                try:
                    out.append((float(p[0]), float(p[1])))
                except ValueError:
                    pass
    return sorted(out)


def max_load(samples, t0, t1):
    """Largest 1-min load of the samples in [t0, t1 + 30 s] (the next sample covers t1)."""
    vals = [v for t, v in samples if t0 - 30.0 <= t <= t1 + 30.0]
    return max(vals) if vals else None


def report_cis(report_json: Path) -> dict:
    """(set, ref, arm) -> CI the report printed (bench, all inputs)."""
    out = {}
    if not report_json.is_file():
        return out
    d = json.loads(report_json.read_text())
    for g in d.get("groups", []):
        if not g["name"].startswith("campaign:"):
            continue
        s = g["name"].split(":", 1)[1]
        for c in g["comparisons"]:
            if c["metric"] == "bench" and c["input"] == "*":
                out[(s, c["reference"], c["arm"])] = {"median": c.get("median"),
                                                      "ci": c.get("ci"),
                                                      "seed": c.get("seed")}
    return out


def analyse_pair(by, ref, arm, seeds, samples, load_max, rep_split):
    keys = sorted(k for k in by if ref in by[k] and arm in by[k])
    if not keys:
        return {"n_pairs": 0}
    ratios = [by[k][ref]["bench_ns"] / by[k][arm]["bench_ns"] for k in keys]
    deltas = [(by[k][ref]["bench_ns"] - by[k][arm]["bench_ns"]) / 1e6 for k in keys]
    med = statistics.median(ratios)
    cis = [ci(ratios, s) for s in range(seeds)]
    lo_s = {(x[0] > 1) - (x[0] < 1) for x in cis}
    hi_s = {(x[1] > 1) - (x[1] < 1) for x in cis}
    stable = len(lo_s) == 1 and len(hi_s) == 1
    v0 = verdict(*cis[0])
    per_input = defaultdict(list)
    for k, r in zip(keys, ratios):
        per_input[k[1]].append(r)
    loo = {}
    for i in sorted(per_input):
        rest = [r for k, r in zip(keys, ratios) if k[1] != i]
        lo, hi = ci(rest, 0)
        loo[str(i)] = {"median": statistics.median(rest) - 1, "ci": [lo - 1, hi - 1],
                       "verdict": verdict(lo, hi)}
    first = [r for k, r in zip(keys, ratios) if k[0] < rep_split]
    second = [r for k, r in zip(keys, ratios) if k[0] >= rep_split]
    calm = []
    for k, r in zip(keys, ratios):
        a, b = by[k][ref], by[k][arm]
        t0 = min(a["started"], b["started"])
        t1 = max(a["finished"], b["finished"])
        m = max_load(samples, t0, t1)
        if m is not None and m <= load_max:
            calm.append(r)
    calm_ci = ci(calm, 0) if len(calm) >= 2 else (None, None)
    return {
        "n_pairs": len(keys), "median": med - 1,
        "delta_ms_median": statistics.median(deltas),
        "ci_seed0": [cis[0][0] - 1, cis[0][1] - 1],
        "ci_by_seed": [[x[0] - 1, x[1] - 1] for x in cis],
        "ci_envelope": [min(x[0] for x in cis) - 1, max(x[1] for x in cis) - 1],
        "seed_stable": stable, "verdict_seed0": v0,
        "verdict": v0 if stable else "borderline",
        "r11": med - 1 >= 0.01 and cis[0][0] > 1,
        "faster_share": sum(r > 1 for r in ratios) / len(ratios),
        "per_input_median": {str(i): statistics.median(v) - 1 for i, v in
                             sorted(per_input.items())},
        "leave_one_input_out": loo,
        "drift": {"reps_lt_%d" % rep_split: statistics.median(first) - 1 if first else None,
                  "reps_ge_%d" % rep_split: statistics.median(second) - 1 if second else None},
        "load_filtered": {"load_max": load_max, "n_pairs": len(calm),
                          "median": statistics.median(calm) - 1 if calm else None,
                          "ci": [calm_ci[0] - 1, calm_ci[1] - 1] if calm_ci[0] else None,
                          "verdict": verdict(*calm_ci)},
    }


def correctness(rows, by):
    out = {}
    arms = sorted({r["arm"] for r in rows})
    for arm in arms:
        mine = [r for r in rows if r["arm"] == arm]
        eq = ne = 0
        mism = []
        for r in mine:
            w = by.get((r["rep"], r["input"]), {}).get("warm")
            if w is None or arm == "warm":
                continue
            if r.get("output_norm_sha256") == w.get("output_norm_sha256"):
                eq += 1
            else:
                ne += 1
                mism.append([r["rep"], r["input"]])
        per_in = defaultdict(set)
        app = defaultdict(set)
        for r in mine:
            per_in[r["input"]].add(r.get("output_norm_sha256"))
            app[r["input"]].add(r.get("appended"))
        out[arm] = {
            "runs": len(mine), "valid": sum(1 for r in mine if r.get("valid")),
            "verdicts": dict(Counter(str(r.get("verdict")) for r in mine)),
            "exit_codes": dict(Counter(str(r.get("exit_code")) for r in mine)),
            "timed_out": sum(1 for r in mine if r.get("timed_out")),
            "qemu_sha256": sorted({str(r.get("qemu_sha256")) for r in mine}),
            "output_equal_warm_pair": eq, "output_differs_warm_pair": ne,
            "mismatch_pairs": mism[:20],
            "distinct_outputs_per_input": {str(i): len(v) for i, v in sorted(per_in.items())},
            "appended_per_input": {str(i): sorted(x for x in v if x is not None)
                                   for i, v in sorted(app.items())},
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-root", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--seeds", type=int, default=6)
    ap.add_argument("--load-max", type=float, default=4.0)
    a = ap.parse_args()
    root = a.out_root
    samples = load_samples(root / "timing-load.log")
    rcis = report_cis(root / "report" / "report.json")
    res: dict = {"schema": "scopt.b5_evaluate/1", "out_root": str(root),
                 "method": ("median of paired ratios t_ref/t_arm over (rep, input) on bench_ns; "
                            f"95% percentile bootstrap, {B} resamples (scopt.report.stats); "
                            f"seed sensitivity over seeds 0..{a.seeds - 1}; borderline when a "
                            "CI bound changes sign between seeds"),
                 "load_samples": len(samples), "sets": {}, "cross_set": {}}
    split_arms: dict = defaultdict(dict)
    for s in SETS:
        d = root / s
        if not (d / "runs.jsonl").is_file():
            res["sets"][s] = {"status": "missing"}
            continue
        rows = [json.loads(x) for x in (d / "runs.jsonl").read_text().splitlines() if x]
        cj = json.loads((d / "campaign.json").read_text())
        arms = cj["campaign"]["arms"]
        split = rows[0].get("split")
        ctoml = root / "campaigns" / f"{s}.toml"
        seed = (tomllib.loads(ctoml.read_text()).get("campaign", {}).get("seed")
                if ctoml.is_file() else None)
        reps = sorted({r["rep"] for r in rows})
        by: dict = defaultdict(dict)
        for r in rows:
            if r.get("valid"):
                by[(r["rep"], r["input"])][r["arm"]] = r
        t0 = min(r["started"] for r in rows)
        t1 = max(r["finished"] for r in rows)
        win = [v for t, v in samples if t0 <= t <= t1]
        comps = [("warm", x["name"], "vs_warm") for x in arms if x["name"] != "warm"]
        comps += [(x["parent"], x["name"], "vs_parent") for x in arms if x.get("parent")]
        out = {"split": split, "schedule_seed": seed, "runs": len(rows),
               "valid": sum(1 for r in rows if r.get("valid")),
               "reps": len(reps), "window": [t0, t1], "window_s": t1 - t0,
               "load1": {"n": len(win), "mean": statistics.fmean(win) if win else None,
                         "median": statistics.median(win) if win else None,
                         "max": max(win) if win else None,
                         "n_above": sum(1 for v in win if v > a.load_max)},
               "comparisons": [], "correctness": correctness(rows, by)}
        for ref, arm, why in comps:
            c = analyse_pair(by, ref, arm, a.seeds, samples, a.load_max, len(reps) // 2)
            c.update({"reference": ref, "arm": arm, "why": why,
                      "report": rcis.get((s, ref, arm))})
            out["comparisons"].append(c)
            if ref == "warm":
                split_arms[split].setdefault(arm, []).append(
                    {"set": s, "seed": seed, "median": c.get("median"),
                     "ci_seed0": c.get("ci_seed0")})
        res["sets"][s] = out
        print(f"{s}: {out['valid']}/{out['runs']} valid, {len(comps)} comparisons",
              file=sys.stderr)
    for split, arms in split_arms.items():
        res["cross_set"][split] = {arm: v for arm, v in arms.items() if len(v) > 1}
    txt = json.dumps(res, indent=1, sort_keys=True)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(txt + "\n")
        print(f"-> {a.out}", file=sys.stderr)
    else:
        print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
