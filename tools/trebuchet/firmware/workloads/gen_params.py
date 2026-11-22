#!/usr/bin/env python3
"""gen_params.py --out DIR: run parameters of the four fixed-work workloads.

Writes DIR/<split>/<workload>/<split>-N.bin for train, heldout and final (N = 1..3).
A file is three little-endian words: magic "TBPR", PRNG seed, iteration count.  The
splits use different seeds; `final` also runs longer than `train`.
"""
import argparse, struct
from pathlib import Path

MAGIC = 0x52504254
# iteration counts per workload, sized for a guest window of roughly one second at -O0
COUNTS = {"fwbench": {"train": 24000, "heldout": 30000, "final": 36000},
          "kernbench": {"train": 20000, "heldout": 25000, "final": 30000},
          "libbench": {"train": 8000, "heldout": 10000, "final": 12000},
          "algobench": {"train": 4000, "heldout": 5000, "final": 6000}}
SEEDS = {"train": (0x1234ABCD, 0x2B7E1516, 0x0F1E2D3C),
         "heldout": (0x9E3779B1, 0x7F4A7C15, 0x85EBCA6B),
         "final": (0x00C0FFEE, 0xC2B2AE35, 0x27D4EB2F)}

args = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
args.add_argument("--out", required=True)
out = Path(args.parse_args().out)
for workload, counts in COUNTS.items():
    for split, seeds in SEEDS.items():
        d = out / split / workload; d.mkdir(parents=True, exist_ok=True)
        for n, seed in enumerate(seeds, 1):
            (d / f"{split}-{n}.bin").write_bytes(struct.pack("<III", MAGIC, seed, counts[split]))
print(sum(1 for _ in out.glob("*/*/*.bin")), "parameter files")
