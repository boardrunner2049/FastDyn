#!/usr/bin/env python3
"""dataset_report.py ROOT WORKLOAD...: the five numbers for every category of the dataset.

Reads ROOT/<workload>/results/campaign.json.  Guest work is the median over blocks; a step
is the median of the within-block ratios.  The summary row is the geometric mean over the
categories.  A category whose campaign has a failed or missing launch is not reported as a
number, and the exit status is 2.
"""
import json, math, random, statistics, sys
from pathlib import Path

ARMS = ["stock-O0", "fd-O0", "fd-fb", "fd-fb-warm", "fd-fb-scopt"]
HEAD = ["1 stock QEMU", "2 FastDyn", "3 +FirmBOLT", "4 +warm cache", "5 +scopt cache"]
STEPS = [("2→3", "fd-O0", "fd-fb"), ("3→4", "fd-fb", "fd-fb-warm"), ("4→5", "fd-fb-warm", "fd-fb-scopt"),
         ("2→5", "fd-O0", "fd-fb-scopt"), ("1→5", "stock-O0", "fd-fb-scopt")]
root = Path(sys.argv[1]); workloads = sys.argv[2:]

def load(w):
    path = root / w / "results" / "campaign.json"
    if not path.is_file():
        return None, f"no campaign ({path})"
    d = json.loads(path.read_text()); S = d["samples"]
    blocks = sorted({s["block"] for s in S}); by = {(s["block"], s["arm"]): s for s in S}
    bad = [s for s in S if not s.get("ok")] or [1 for b in blocks for a in ARMS if (b, a) not in by]
    if bad or not blocks:
        return None, f"invalid campaign: {len(bad)} failed or missing launches"
    return (d, blocks, by), None

def ratios(by, blocks, a, b, key):
    return [by[(k, a)][key] / by[(k, b)][key] for k in blocks]

rows = {}; problems = {}
for w in workloads:
    data, why = load(w)
    if data is None:
        problems[w] = why; continue
    d, blocks, by = data
    row = {"blocks": len(blocks), "selected": d["selection"]["candidate"].replace("sfb-b3", "sfb+b3")}
    for key, unit in (("guest_ns", "guest"), ("process_ns", "process")):
        row[unit + "_ms"] = {a: statistics.median(by[(b, a)][key] for b in blocks) / 1e6 for a in ARMS}
        row[unit + "_steps"] = {}
        for name, a, b in STEPS:
            r = ratios(by, blocks, a, b, key); rng = random.Random(20261121)
            boot = sorted(statistics.median(rng.choices(r, k=len(r))) for _ in range(10000))
            row[unit + "_steps"][name] = [statistics.median(r), boot[249], boot[9749]]
    rows[w] = row

def geomean(values):
    return math.exp(sum(math.log(v) for v in values) / len(values))

for unit, title in (("guest", "Guest work (ms, median)"), ("process", "Whole process (ms, median)")):
    print(f"\n{title}\n")
    print("| category | " + " | ".join(HEAD) + " | cache deployed in 5 |")
    print("|---|" + "---:|" * 5 + "---|")
    for w in workloads:
        if w in rows:
            print(f"| `{w}` | " + " | ".join(f"{rows[w][unit + '_ms'][a]:.0f}" for a in ARMS) + f" | `{rows[w]['selected']}` |")
        else:
            print(f"| `{w}` | " + " | ".join(["—"] * 5) + f" | {problems[w]} |")
    print(f"\nSpeedup of each step, {unit} (median of within-block ratios)\n")
    print("| category | " + " | ".join(name for name, _, _ in STEPS) + " |")
    print("|---|" + "---:|" * len(STEPS))
    for w in workloads:
        if w in rows:
            print(f"| `{w}` | " + " | ".join(f"{rows[w][unit + '_steps'][n][0]:.3f}×" for n, _, _ in STEPS) + " |")
    if rows:
        print("| **geometric mean** | " + " | ".join(
            f"**{geomean([rows[w][unit + '_steps'][n][0] for w in rows]):.3f}×**" for n, _, _ in STEPS) + " |")
(root / "dataset.json").write_text(json.dumps({"schema": "trebuchet.dataset/1", "categories": rows, "problems": problems}, indent=1) + "\n")
if problems:
    print("\nNOT A COMPLETE DATASET RESULT:", "; ".join(f"{w}: {why}" for w, why in problems.items()))
    sys.exit(2)
