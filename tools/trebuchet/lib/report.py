#!/usr/bin/env python3
"""report.py CAMPAIGN.json: medians per arm and paired speedups between pipeline stages."""
import json, random, statistics, sys
d = json.load(open(sys.argv[1])); S = d["samples"]
arms = ["stock-O0", "fd-O0", "fd-fb", "fd-fb-warm", "fd-fb-scopt", "stock-fb", "fd-O0-warm"]
blocks = sorted({s["block"] for s in S})
by = {(s["block"], s["arm"]): s for s in S}
print("selection:", d["selection"], "| blocks:", len(blocks), "| launches:", len(S), "| failed checks:", sum(not s["ok"] for s in S))
def med(arm, key):
    return statistics.median(by[(b, arm)][key] for b in blocks) / 1e6
print(f"{'arm':14s} {'guest ms':>10s} {'process ms':>11s}")
for a in arms:
    print(f"{a:14s} {med(a, 'guest_ns'):10.1f} {med(a, 'process_ns'):11.1f}")
def ratio(a, b, key):
    """speedup of b over a: median of within-block time ratios a/b, bootstrap 95 % interval over blocks."""
    r = [by[(k, a)][key] / by[(k, b)][key] for k in blocks]
    rng = random.Random(20261121); bs = sorted(statistics.median(rng.choices(r, k=len(r))) for _ in range(10000))
    return statistics.median(r), bs[249], bs[9749]
pairs = [("stock-O0", "fd-O0"), ("fd-O0", "fd-fb"), ("fd-fb", "fd-fb-warm"), ("fd-fb-warm", "fd-fb-scopt"),
         ("fd-O0", "fd-fb-scopt"), ("stock-O0", "fd-fb-scopt"), ("stock-O0", "stock-fb"), ("fd-O0", "fd-O0-warm"), ("fd-O0-warm", "fd-fb-warm")]
for key in ("guest_ns", "process_ns"):
    print("\n" + key)
    for a, b in pairs:
        m, lo, hi = ratio(a, b, key); print(f"  {a:12s} -> {b:12s} {m:6.3f}x [{lo:.3f}, {hi:.3f}]")
