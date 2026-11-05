import argparse
import matplotlib.pyplot as plt
import numpy as np
try:
    from utils.coverage_helpers import load_coverage_growth
except ModuleNotFoundError:  # Direct execution from this directory.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
    from utils.coverage_helpers import load_coverage_growth

# ============================
# Global customization options
# ============================

LINE_COLOR       = "steelblue"
LINE_WIDTH       = 2.0
FILL_COLOR       = "steelblue"
FILL_ALPHA       = 0.15
MARKER_COLOR     = "steelblue"
MARKER_SIZE      = 40
NUM_MARKERS      = 30          # evenly spaced along x-axis
FIGURE_SIZE      = (10, 6)

# ============================
# Plotting function
# ============================

def plot_block_growth(block_dict):
    # Sort by time so the curve moves left→right correctly
    items = sorted(block_dict.items(), key=lambda x: x[1])
    counts = np.array([c for c, t in items])
    times_ms = np.array([t for c, t in items])

    # Convert milliseconds to hours
    times_hours = times_ms / (1000 * 60 * 60)

    plt.figure(figsize=FIGURE_SIZE)

    # Main line
    plt.plot(times_hours, counts, linewidth=LINE_WIDTH, color=LINE_COLOR)

    # Gradient fill under the line
    plt.fill_between(times_hours, counts, color=FILL_COLOR, alpha=FILL_ALPHA)

    # Evenly spaced markers along the x-axis
    marker_times = np.linspace(times_hours.min(), times_hours.max(), NUM_MARKERS)
    marker_counts = np.interp(marker_times, times_hours, counts)

    plt.scatter(marker_times, marker_counts,
                color=MARKER_COLOR,
                s=MARKER_SIZE,
                zorder=3)

    plt.xlabel("Time (hours)", fontsize=14)
    plt.ylabel("Basic Blocks", fontsize=14)
    plt.title("Number of Unique Blocks Hit vs. Time", fontsize=16)
    plt.grid(True, linestyle="--", alpha=0.5)

    plt.tight_layout()
    # plt.show()
    # Save
    plt.savefig("coverage-over-time.png", dpi=200, bbox_inches="tight")

# ============================
# Block parsing function
# ============================

def parse_unique_blocks(log_path):
    # Regex for lines like:
    # [TB] Unique blocks = 1763 at time 511508035
    return load_coverage_growth(log_path)

# Example usage:py
# d = parse_unique_blocks("/data/Code/rehosting/FastDyn/fdyn-out-lwip")
# plot_block_growth(d)


def main():
    parser = argparse.ArgumentParser(description="Plot unique block discovery over time.")
    parser.add_argument("logfile", help="Path to the fuzzing log file")
    args = parser.parse_args()
    d = parse_unique_blocks(args.logfile)
    plot_block_growth(d)

if __name__ == "__main__":
    main()
