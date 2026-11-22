#!/usr/bin/env python3
"""dataset_report.py ROOT WORKLOAD...: the five numbers for every category of the dataset.

Reads ROOT/<workload>/results/campaign.json.  Guest work is the median over blocks; a step
is the median of the within-block ratios.  The summary row is the geometric mean over the
categories.  A category whose campaign has a failed or missing launch is not reported as a
number, and the exit status is 2.
"""
import json, math, os, random, statistics, sys
from uuid import uuid4
from campaign import campaign_errors, campaign_plan, write_json
from pathlib import Path

ARMS = ["stock-O0", "fd-O0", "fd-fb", "fd-fb-warm", "fd-fb-scopt"]
HEAD = ["1 stock QEMU", "2 FastDyn", "3 +FirmBOLT", "4 +warm cache", "5 +scopt cache"]
STEPS = [("2→3", "fd-O0", "fd-fb"), ("3→4", "fd-fb", "fd-fb-warm"), ("4→5", "fd-fb-warm", "fd-fb-scopt"),
         ("2→5", "fd-O0", "fd-fb-scopt"), ("1→5", "stock-O0", "fd-fb-scopt")]
root = Path(sys.argv[1]); workloads = sys.argv[2:]
dataset_path = root / "dataset.json"
invocation_id = os.environ.get("TREBUCHET_DATASET_ID")
if workloads[0] == "--begin":
    repetitions = int(workloads[1])
    if repetitions < 1:
        sys.exit("repetitions must be positive")
    run_id = str(uuid4())
    write_json(dataset_path, {"schema": "trebuchet.dataset/2", "state": "running", "run_id": run_id,
                              "plan": campaign_plan(repetitions), "workloads": workloads[2:],
                              "categories": {}, "problems": {}})
    print(run_id)
    sys.exit(0)

previous = json.loads(dataset_path.read_text()) if dataset_path.is_file() else {}
run_id = previous.get("run_id")
if workloads[0] == "--failed":
    if invocation_id != run_id or not run_id:
        sys.exit("dataset run identity changed")
    previous.update(state="failed")
    write_json(dataset_path, previous)
    sys.exit(0)
if run_id and previous["workloads"] != workloads:
    sys.exit("dataset workload list differs from the run plan")
if invocation_id and invocation_id != run_id:
    sys.exit("dataset run identity changed")
if run_id and previous["state"] != "completed" and invocation_id != run_id:
    print(f"INCOMPLETE DATASET: run {run_id} is {previous['state']}")
    sys.exit(2)


def load(w):
    path = root / w / "results" / "campaign.json"
    if not path.is_file():
        return None, f"no campaign ({path})"
    d = json.loads(path.read_text()); S = d["samples"]
    errors = campaign_errors(d)
    if d.get("workload", "compute") != w:
        errors.append(f"campaign workload does not match {w}")
    if run_id != d.get("dataset_id"):
        errors.append("campaign belongs to a different dataset run")
    if run_id and previous["plan"] != d.get("plan"):
        errors.append("campaign plan differs from the dataset plan")
    if errors:
        return None, "invalid campaign: " + errors[0]
    blocks = sorted({s["block"] for s in S}); by = {(s["block"], s["arm"]): s for s in S}
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
    if d.get("run_id"):
        row["campaign_run_id"] = d["run_id"]
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
    if rows and not problems:
        print("| **geometric mean** | " + " | ".join(
            f"**{geomean([rows[w][unit + '_steps'][n][0] for w in rows]):.3f}×**" for n, _, _ in STEPS) + " |")
if previous != {"schema": "trebuchet.dataset/1", "categories": rows, "problems": {}} or problems:
    write_json(dataset_path, {"schema": "trebuchet.dataset/2", "run_id": run_id,
                              "state": "failed" if problems else "completed", "workloads": workloads,
                              "plan": previous.get("plan"), "categories": rows, "problems": problems})
if problems:
    print("\nNOT A COMPLETE DATASET RESULT:", "; ".join(f"{w}: {why}" for w, why in problems.items()))
    sys.exit(2)
