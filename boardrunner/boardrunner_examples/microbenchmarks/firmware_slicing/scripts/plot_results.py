#!/usr/bin/env python3
"""
Bar chart comparing FastDyn against representative baselines on the
function-bypass, host-operation and HITL microbenchmarks (paper Figure 8a-c). Log-scale Y
(seconds per 1000-op run). Baseline bars are hatched gray, FastDyn bars solid.

Every bar is computed from a results file; nothing is typed in by hand:
  * FastDyn function bypass (modifier) / host operation (virtual): `elapsed_ns=` lines from
    the image's {modifier,virtual}_run.log (`elapsed_ns=` lines,
    or the shipped results/{modifier,virtual}_benchmark.txt).
  * HALucinator function bypass / host operation: per-iteration seconds from
    baseline/results/{pc_update,counter_update}_benchmark.txt (or the
    `iter,elapsed_s,per_op_ms` CSVs written by the halucinator-slice image).
  * HITL: per-iteration `total_s` from the hitl_comparison CSVs.
Each is reported as mean +/- stddev of seconds per one N-op run.

Usage:
    python3 plot_results.py                       # shipped results -> plot.pdf
    python3 plot_results.py -o results/plot.png --dpi 200
    python3 plot_results.py --fresh ./results -o fig8.pdf   # your own image runs (-v ./results:/results)
    python3 plot_results.py --fastdyn-modifier my_modifier.log  # override one input
"""
import argparse
import re
import statistics
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.patches import Patch

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent                       # firmware_slicing/
HITL = BENCH.parent / "hitl_comparison"

DEFAULT_FILES = {
    "fastdyn_modifier":   BENCH / "results" / "modifier_benchmark.txt",
    "fastdyn_virtual":    BENCH / "results" / "virtual_benchmark.txt",
    "halucinator_pc":     BENCH / "baseline" / "results" / "pc_update_benchmark.txt",
    "halucinator_counter": BENCH / "baseline" / "results" / "counter_update_benchmark.txt",
    "avatar2_reads":      HITL / "results" / "_avatar2_per_iter_reads.csv",
    "avatar2_writes":     HITL / "results" / "_avatar2_per_iter_writes.csv",
    "fastdyn_reads":      HITL / "results" / "_fastdyn_per_iter_reads.csv",
    "fastdyn_writes":     HITL / "results" / "_fastdyn_per_iter_writes.csv",
}


def _seconds_per_run(path: Path) -> list[float]:
    """Extract one seconds-per-run sample per measurement line, whatever the
    file's format: `elapsed_ns=N` (FastDyn logs), `iter k  T s` /
    `iter k: T s` (HALucinator logs, first seconds value on the line), or a
    CSV with a `total_s` column (HITL harness)."""
    text = path.read_text()
    samples: list[float] = []
    header = text.lstrip().splitlines()[0].split(",") if text.strip() else []
    # CSV: the HITL harness (`tool,direction,iters,total_s,...`) or the
    # HALucinator baseline scripts (`iter,elapsed_s,per_op_ms`).
    for col_name in ("total_s", "elapsed_s"):
        if col_name in header:
            col = header.index(col_name)
            for line in text.lstrip().splitlines()[1:]:
                if line.strip():
                    samples.append(float(line.split(",")[col]))
            return samples
    for line in text.splitlines():
        m = re.search(r"elapsed_ns=(\d+)", line)
        if m:
            samples.append(int(m.group(1)) / 1e9)
            continue
        m = re.match(r"\s*iter\s*\d+:?\s+([0-9.]+)\s*s", line)
        if m:
            samples.append(float(m.group(1)))
    if not samples:
        raise SystemExit(f"{path}: no measurements recognised")
    return samples


def _stats(samples: list[float]) -> tuple[float, float]:
    return statistics.mean(samples), (statistics.stdev(samples) if len(samples) > 1 else 0.0)


def build_data(files: dict) -> list:
    """(label, group, mean_seconds, stddev_seconds, is_baseline) per bar."""
    hal_pc = _stats(_seconds_per_run(files["halucinator_pc"]))
    fd_mod = _stats(_seconds_per_run(files["fastdyn_modifier"]))
    hal_ct = _stats(_seconds_per_run(files["halucinator_counter"]))
    fd_vir = _stats(_seconds_per_run(files["fastdyn_virtual"]))
    # HITL: average of the read and write directions, as in the paper.
    av = _stats(_seconds_per_run(files["avatar2_reads"]) + _seconds_per_run(files["avatar2_writes"]))
    fd = _stats(_seconds_per_run(files["fastdyn_reads"]) + _seconds_per_run(files["fastdyn_writes"]))
    return [
        ("HALucinator", "Function Bypass", *hal_pc, True),
        ("FastDyn",     "Function Bypass", *fd_mod, False),
        ("HALucinator", "Host Operation",  *hal_ct, True),
        ("FastDyn",     "Host Operation",  *fd_vir, False),
        ("Avatar2",     "HITL Execution",  *av,     True),
        ("FastDyn",     "HITL Execution",  *fd,     False),
    ]


BASELINE_COLOR = "#d9d9d9"
FASTDYN_COLOR  = "#2b6cb0"


def fmt_time(seconds: float) -> str:
    """Human-friendly time label for the bar tops."""
    if seconds >= 1:
        return f"{seconds:.2f}s"
    if seconds >= 1e-3:
        return f"{seconds * 1e3:.2f}ms"
    if seconds >= 1e-6:
        return f"{seconds * 1e6:.2f}us"
    return f"{seconds * 1e9:.2f}ns"


def _draw_group(ax, rows, title):
    """One subplot per benchmark group. Y-limits fit each group's range."""
    xs, means, errs, colors, hatches, xlabels = [], [], [], [], [], []
    for i, (lbl, _, m, s, is_base) in enumerate(rows):
        xs.append(i)
        means.append(m)
        errs.append(s)
        colors.append(BASELINE_COLOR if is_base else FASTDYN_COLOR)
        hatches.append("//" if is_base else "")
        xlabels.append(lbl)

    # Error bars: chunky caps so they read on log scale where +/-10% is a
    # tiny visual delta. Thick lines with visible caps mimic the Figure 8
    # style the reviewers asked for.
    bars = ax.bar(xs, means, width=0.7, yerr=errs, capsize=6,
                  ecolor="black", error_kw={"elinewidth": 1.5, "capthick": 1.5},
                  color=colors, edgecolor="black", linewidth=0.9)
    for bar, hatch in zip(bars, hatches):
        bar.set_hatch(hatch)

    ax.set_yscale("log")
    ax.set_xticks(xs)
    ax.set_xticklabels(xlabels, fontsize=9)
    ax.set_title(title, fontweight="bold", pad=10)

    # Tight y-limits per subplot: one decade below min, two decades above max
    # (extra headroom on top for the value labels).
    lo = min(means) / 10
    hi = max(means) * 100
    ax.set_ylim(lo, hi)
    ax.yaxis.set_major_locator(mticker.LogLocator(base=10, numticks=10))
    ax.grid(True, axis="y", which="major", linestyle="--", alpha=0.4)
    ax.set_axisbelow(True)

    # Bar-top label: mean, plus "+/- sigma" when we have variance data. The
    # +/- carries the info the log-compressed error bar cannot.
    for x, m, s in zip(xs, means, errs):
        if s > 0:
            txt = f"{fmt_time(m)}\n(+/- {fmt_time(s)})"
        else:
            txt = fmt_time(m)
        ax.text(x, m * 1.6, txt, ha="center", va="bottom", fontsize=9)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="plot.pdf",
                    help="Output path; extension chooses format (pdf/png/svg).")
    ap.add_argument("--dpi", type=int, default=150)
    for key, default in DEFAULT_FILES.items():
        ap.add_argument(f"--{key.replace('_', '-')}", type=Path, default=default,
                        help=f"results file for {key} (default: {default.relative_to(BENCH.parent)})")
    ap.add_argument("--fresh", metavar="DIR", type=Path,
                    help="directory with {modifier,virtual}_run.log and "
                         "{pcupdate,counter}_run.csv written by the images "
                         "(-v DIR:/results); missing files fall back to the shipped ones")
    args = ap.parse_args()
    files = {key: getattr(args, key) for key in DEFAULT_FILES}
    if args.fresh:
        for key, name in (("fastdyn_modifier", "modifier_run.log"),
                          ("fastdyn_virtual", "virtual_run.log"),
                          ("halucinator_pc", "pcupdate_run.csv"),
                          ("halucinator_counter", "counter_run.csv")):
            if (args.fresh / name).is_file():
                files[key] = args.fresh / name
    data = build_data(files)
    for lbl, grp, m, sd, _ in data:
        print(f"{grp:22s} {lbl.replace(chr(10), ' '):20s} mean={m:.6g} s  stddev={sd:.3g} s")

    plt.rcParams.update({
        "font.family":       "serif",
        "font.size":         11,
        "axes.spines.top":   False,
        "axes.spines.right": False,
    })

    # Group rows in DATA order, one subplot per group.
    groups = {}
    for row in data:
        groups.setdefault(row[1], []).append(row)
    group_names = list(groups.keys())

    n = len(group_names)
    # Width scales with total bar count; small side-by-side.
    total_bars = sum(len(rs) for rs in groups.values())
    fig, axes = plt.subplots(1, n, figsize=(1.6 * total_bars + 1.5, 4.5),
                             gridspec_kw={"width_ratios": [len(rs) for rs in groups.values()]})
    if n == 1:
        axes = [axes]

    for ax, name in zip(axes, group_names):
        _draw_group(ax, groups[name], name)

    axes[0].set_ylabel("Execution Time (s) - Log Scale")

    # Single legend at the top, shared across subplots.
    fig.legend(
        handles=[
            Patch(facecolor=BASELINE_COLOR, edgecolor="black",
                  hatch="//", label="Baseline"),
            Patch(facecolor=FASTDYN_COLOR, edgecolor="black",
                  label="FastDyn"),
        ],
        loc="upper center", ncol=2, frameon=True,
        bbox_to_anchor=(0.5, 1.02),
    )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    plt.savefig(out, bbox_inches="tight", dpi=args.dpi)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
