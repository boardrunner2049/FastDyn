import argparse
import matplotlib.pyplot as plt
import numpy as np
try:
    from .coverage_helpers import load_coverage_growth
except ImportError:  # Direct script execution.
    from coverage_helpers import load_coverage_growth

# ============================
# Global customization options
# ============================

FIGURE_SIZE      = (10, 6)
TIMEFRAME_HOURS  = 8.0
NUM_MARKERS      = 30

# Default style if a plot doesn't specify its own
DEFAULT_STYLE = {
    "line_color":  "steelblue",
    "line_width":  2.0,
    "fill_color":  "steelblue",
    "fill_alpha":  0.15,
    "marker_color": "steelblue",
    "marker_size": 40,
}

# ============================
# Block parsing function
# ============================

def parse_unique_blocks(log_path):
    return load_coverage_growth(log_path)

# ============================
# Multi‑plot overlay function
# ============================

def plot_block_growth_multi(plot_configs):
    plt.figure(figsize=FIGURE_SIZE)

    for cfg in plot_configs:
        # Load data
        block_dict = parse_unique_blocks(cfg["logfile"])
        items = sorted(block_dict.items(), key=lambda x: x[1])
        counts = np.array([c for c, t in items])
        times_ms = np.array([t for c, t in items])
        times_hours = times_ms / (1000 * 60 * 60)

        # Style (use defaults if missing)
        style = DEFAULT_STYLE.copy()
        style.update(cfg.get("style", {}))

        # Extend to full timeframe if needed
        if times_hours[-1] < TIMEFRAME_HOURS:
            extended_times = np.append(times_hours, TIMEFRAME_HOURS)
            extended_counts = np.append(counts, counts[-1])
        else:
            extended_times = times_hours
            extended_counts = counts

        # Main line
        plt.plot(
            extended_times,
            extended_counts,
            linewidth=style["line_width"],
            color=style["line_color"],
            label=cfg.get("label", cfg["logfile"])
        )

        # Fill under curve
        plt.fill_between(
            extended_times,
            extended_counts,
            color=style["fill_color"],
            alpha=style["fill_alpha"]
        )

        # Markers
        marker_times = np.linspace(0, TIMEFRAME_HOURS, NUM_MARKERS)
        marker_counts = np.interp(marker_times, times_hours, counts)

        plt.scatter(
            marker_times,
            marker_counts,
            color=style["marker_color"],
            s=style["marker_size"],
            zorder=3
        )

        # -------------------------------
        # NEW: faint vertical lines at each increase
        # -------------------------------
        for t in times_hours:
            plt.axvline(
                x=t,
                color=style["line_color"],
                alpha=0.08,      # very faint
                linewidth=0.8,
                zorder=1
            )

    # Shared plot settings
    plt.title("Unique Blocks Found Over Time")
    plt.xlabel("Time since start (hours)")
    plt.ylabel("Unique block count")
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.xlim(0, TIMEFRAME_HOURS)
    plt.legend()
    plt.tight_layout()
    plt.show()

# ============================
# CLI
# ============================

def main():
    parser = argparse.ArgumentParser(description="Overlay multiple block‑growth curves.")
    parser.add_argument("logfiles", nargs="+", help="Paths to fuzzing log files")
    args = parser.parse_args()

    # Auto‑generate simple configs if user just passes log paths
    plot_configs = []
    colors = ["steelblue", "crimson", "darkgreen", "orange", "purple"]

    for i, path in enumerate(args.logfiles):
        plot_configs.append({
            "logfile": path,
            "label": f"Run {i+1}",
            "style": {
                "line_color": colors[i % len(colors)],
                "fill_color": colors[i % len(colors)],
                "marker_color": colors[i % len(colors)],
            }
        })

    plot_block_growth_multi(plot_configs)

if __name__ == "__main__":
    main()
