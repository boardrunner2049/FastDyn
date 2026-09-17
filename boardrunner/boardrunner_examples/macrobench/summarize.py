"""
Read <results dir>/*.csv (default results/), print a markdown table + average speedup.

Backend labels used in the CSVs and CLI:
    halucinator  — unmodified HALucinator (Avatar2 + GDB backend)
    halucinator-fd   — FastDyn plugin with embedded Python interpreter
"""
import csv
import glob
import sys
import math
import statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
# results dir: argv[1] if given, else results/ (a fresh sweep); results/paper/ holds the paper's data
RESULTS = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "results"

BASELINE = "halucinator"
TARGET = "halucinator-fd"
BASELINE_LABEL = "HALucinator"
TARGET_LABEL = "HALucinator-FD"


def load():
    rows = []
    for path in sorted(glob.glob(str(RESULTS / "*.csv"))):
        name = Path(path).stem
        by = {BASELINE: [], TARGET: []}
        with open(path) as f:
            for r in csv.DictReader(f):
                if r.get("backend") not in (BASELINE, TARGET):
                    continue
                v = r["wall_s"]
                if v in ("", "NaN"):
                    continue
                by[r["backend"]].append(float(v))
        if by[BASELINE] and by[TARGET]:
            rows.append((name, by[BASELINE], by[TARGET]))
    return rows


def fmt_cell(values):
    m = st.mean(values)
    s = st.stdev(values) if len(values) > 1 else 0.0
    return f"{m:6.2f} ± {s:4.2f}"


def main():
    rows = load()
    if not rows:
        print(f"no usable results under {RESULTS}/.")
        return

    header = (f"| {'firmware':<22} | {BASELINE_LABEL+' (s)':>15} | "
              f"{TARGET_LABEL+' (s)':>15} | {'speedup':>9} |")
    sep = f"| {'-'*22} | {'-'*15} | {'-'*15} | {'-'*9} |"
    print(header)
    print(sep)

    speedups = []
    for name, baseline, target in rows:
        bm, tm = st.mean(baseline), st.mean(target)
        sp = bm / tm if tm > 0 else float("inf")
        speedups.append(sp)
        print(f"| {name:<22} | {fmt_cell(baseline):>15} | "
              f"{fmt_cell(target):>15} | {sp:7.2f}× |")

    if len(speedups) > 1:
        g = math.exp(sum(math.log(x) for x in speedups) / len(speedups))
        print()
        a = sum(speedups) / len(speedups)
        print(f"average speedup across {len(speedups)} workloads: {a:.2f}×  (the paper's figure; geomean {g:.2f}×)")


if __name__ == "__main__":
    main()
