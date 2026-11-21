#!/usr/bin/env python3
"""report.py CAMPAIGN.json [--diagnostic]: medians per arm and paired speedups between pipeline stages.

A campaign in which any launch failed a result or cache-replay check is not a valid
performance comparison: the report names the failed launches and exits 2 without printing
a speedup.  The rule covers the whole campaign, because every step is a ratio inside a
block and a failed launch means the block did not run the same work in every arm.
`--diagnostic` prints the numbers anyway, labeled, for debugging; it still exits 2.
"""
import json, random, statistics, sys
paths = [a for a in sys.argv[1:] if not a.startswith("--")]
diagnostic = "--diagnostic" in sys.argv
d = json.load(open(paths[0])); S = d["samples"]
arms = ["stock-O0", "fd-O0", "fd-fb", "fd-fb-warm", "fd-fb-scopt", "stock-fb", "fd-O0-warm"]
blocks = sorted({s["block"] for s in S})
by = {(s["block"], s["arm"]): s for s in S}
bad = [s for s in S if not s.get("ok")]
missing = [(b, a) for b in blocks for a in arms if (b, a) not in by]
print("selection:", d["selection"], "| blocks:", len(blocks), "| launches:", len(S), "| failed checks:", len(bad))
if bad or missing:
    print("\nINVALID CAMPAIGN: not a performance comparison.")
    for s in bad:
        print(f"  failed: {s['arm']} {s['input']} block {s['block']}: " + "; ".join(s.get("failures") or ["check failed"]))
    for b, a in missing:
        print(f"  missing: {a} in block {b}")
    if not diagnostic:
        sys.exit(2)
    print("\nDIAGNOSTIC ONLY: the numbers below include the failed launches.\n")
def med(arm, key):
    return statistics.median(by[(b, arm)][key] for b in blocks if (b, arm) in by) / 1e6
print(f"{'arm':14s} {'guest ms':>10s} {'process ms':>11s}")
for a in arms:
    print(f"{a:14s} {med(a, 'guest_ns'):10.1f} {med(a, 'process_ns'):11.1f}")
def ratio(a, b, key):
    """speedup of b over a: median of within-block time ratios a/b, bootstrap 95 % interval over blocks."""
    r = [by[(k, a)][key] / by[(k, b)][key] for k in blocks if (k, a) in by and (k, b) in by]
    rng = random.Random(20261121); bs = sorted(statistics.median(rng.choices(r, k=len(r))) for _ in range(10000))
    return statistics.median(r), bs[249], bs[9749]
pairs = [("stock-O0", "fd-O0"), ("fd-O0", "fd-fb"), ("fd-fb", "fd-fb-warm"), ("fd-fb-warm", "fd-fb-scopt"),
         ("fd-O0", "fd-fb-scopt"), ("stock-O0", "fd-fb-scopt"), ("stock-O0", "stock-fb"), ("fd-O0", "fd-O0-warm"), ("fd-O0-warm", "fd-fb-warm")]
for key in ("guest_ns", "process_ns"):
    print("\n" + key)
    for a, b in pairs:
        m, lo, hi = ratio(a, b, key); print(f"  {a:12s} -> {b:12s} {m:6.3f}x [{lo:.3f}, {hi:.3f}]")
if bad or missing:
    sys.exit(2)
