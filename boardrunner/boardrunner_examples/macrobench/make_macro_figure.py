"""
Render the RQ1.b macrobench figure in the same visual style as the
existing RQ1 microbench figure (figure/usenix_execution_modern.pdf).

Reads per-firmware CSVs from results/, computes per-backend mean, and
writes a PDF into the paper's figure/ directory.
"""
import csv
import glob
import sys
import statistics as st
from pathlib import Path

import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
# results dir: argv[1] if given, else results/ (a fresh sweep); results/paper/ holds the paper's data
RESULTS = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "results"
# output: argv[2] if given, else figure/macrobench.pdf next to this script
PAPER_FIG = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "figure" / "macrobench.pdf"

# Ordered so the figure reads left-to-right from smallest to largest
# workload, ending on the 36x speedup.
# Third field: runtime intercept dispatch count (from HALucinator's
# per-BP counters in tmp/*/stats.yaml), used to annotate workload
# complexity directly in the x-axis label.
ORDER = [
    ("stm32_uart_it", "STM32 UART",             8),
    ("zephyr_k64f",   "Zephyr UART\n(FRDM-K64F)",  65),
    ("zephyr_h103",   "Zephyr UART\n(Olimex-H103)", 72),
    ("zephyr_fs",     "Zephyr\nFilesystem",     1177),
]

# Colors picked to match the existing microbench figure.
BASELINE_FACE = "#DDDDDD"
FD_FACE      = "#2A5E9E"
EDGE          = "black"
HATCH         = "//"

plt.rcParams.update({
    "font.family": "serif",
    "font.size":   12,
    "axes.linewidth": 0.8,
    "pdf.fonttype": 42,  # embed real fonts, not paths
    "ps.fonttype":  42,
})


def load_stats():
    """Return {firmware: {'halucinator': (mean, stdev), 'halucinator-fd': (mean, stdev)}}."""
    data = {}
    for path in glob.glob(str(RESULTS / "*.csv")):
        key = Path(path).stem
        by = {"halucinator": [], "halucinator-fd": []}
        with open(path) as f:
            for row in csv.DictReader(f):
                if row.get("backend") not in ("halucinator", "halucinator-fd"):
                    continue
                if row["wall_s"] in ("", "NaN"):
                    continue
                by[row["backend"]].append(float(row["wall_s"]))
        if by["halucinator"] and by["halucinator-fd"]:
            data[key] = {
                "halucinator": (st.mean(by["halucinator"]),
                          st.stdev(by["halucinator"]) if len(by["halucinator"]) > 1 else 0.0),
                "halucinator-fd": (st.mean(by["halucinator-fd"]),
                          st.stdev(by["halucinator-fd"]) if len(by["halucinator-fd"]) > 1 else 0.0),
            }
    return data


def main():
    stats = load_stats()

    labels, base_mean, base_sd, fd_mean, fd_sd, speedups = [], [], [], [], [], []
    for key, label, n_int in ORDER:
        if key not in stats:
            continue
        bm, bs = stats[key]["halucinator"]
        hm, hs = stats[key]["halucinator-fd"]
        labels.append(f"{label}\n({n_int:,} dispatches)")
        base_mean.append(bm); base_sd.append(bs)
        fd_mean.append(hm); fd_sd.append(hs)
        speedups.append(bm / hm)

    if not labels:
        raise SystemExit(f"no CSVs to plot under {RESULTS}/")

    n = len(labels)
    x = list(range(n))
    w = 0.38

    fig, ax = plt.subplots(figsize=(7.5, 4.0))

    bars_b = ax.bar(
        [xi - w / 2 for xi in x], base_mean, w,
        yerr=base_sd, capsize=3, error_kw=dict(elinewidth=0.8, ecolor="black"),
        facecolor=BASELINE_FACE, hatch=HATCH, edgecolor=EDGE,
        linewidth=0.8, label="HALucinator",
    )
    bars_fd = ax.bar(
        [xi + w / 2 for xi in x], fd_mean, w,
        yerr=fd_sd, capsize=3, error_kw=dict(elinewidth=0.8, ecolor="black"),
        facecolor=FD_FACE, edgecolor=EDGE,
        linewidth=0.8, label="HALucinator-FD",
    )

    # Annotate each bar with its value.
    def annotate(bars, means, sds):
        for bar, m, s in zip(bars, means, sds):
            ax.annotate(
                f"{m:.2f}s",
                (bar.get_x() + bar.get_width() / 2, m + s),
                xytext=(0, 3), textcoords="offset points",
                ha="center", va="bottom", fontsize=9,
            )

    annotate(bars_b, base_mean, base_sd)
    annotate(bars_fd, fd_mean, fd_sd)

    # Speedup annotation centered above each firmware pair.
    top_of_pair = [max(b + bs, h + hs) for b, bs, h, hs in
                   zip(base_mean, base_sd, fd_mean, fd_sd)]
    for xi, top, sp in zip(x, top_of_pair, speedups):
        ax.annotate(
            f"{sp:.2f}×",
            (xi, top * 1.55),
            ha="center", va="bottom", fontsize=10, fontweight="bold",
            color="#1f4e79",
        )

    ax.set_yscale("log")
    ax.set_ylabel("Execution Time (s) — Log Scale")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.yaxis.grid(True, linestyle="--", alpha=0.5)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    # Expand the y range so speedup annotations don't clip against the top.
    ymin, ymax = ax.get_ylim()
    ax.set_ylim(ymin, ymax * 4.0)

    ax.legend(loc="upper center", ncol=2, frameon=True,
              bbox_to_anchor=(0.5, 1.06))

    PAPER_FIG.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(PAPER_FIG, bbox_inches="tight")
    print(f"wrote {PAPER_FIG}")


if __name__ == "__main__":
    main()
