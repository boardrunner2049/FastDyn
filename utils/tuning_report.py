#!/usr/bin/env python3
"""Plot recorded attitude experiments without rerunning the firmware."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="Experiment directory containing results.json and candidate CSVs")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = json.loads((args.input / "results.json").read_text())
    candidates = [name for name, result in results.items() if result["passed"]]
    if not candidates:
        parser.error("No completed attitude experiments to plot")
    fig, axes = plt.subplots(2, len(candidates), figsize=(7 * len(candidates), 6),
                             squeeze=False, sharex=True, sharey="row", layout="constrained")
    for column, name in enumerate(candidates):
        data = np.genfromtxt(args.input / name / "attitude.csv", delimiter=",", names=True)
        for row, axis in enumerate(("roll", "pitch")):
            plot = axes[row, column]
            plot.plot(data["time_s"], data[f"{axis}_command_deg"], color="#dd6b20",
                      linestyle="--", linewidth=1.7, label="Command")
            plot.plot(data["time_s"], data[f"{axis}_deg"], color="#147d92",
                      linewidth=1.1, label="Measured")
            plot.set_ylabel(f"{axis.capitalize()} (degrees)")
            error = results[name][f"{axis}_settled_rmse_deg"]
            plot.set_title(f"{name}: settled {axis} RMSE {error:.3f}°")
            plot.grid(alpha=.25)
            if row == 1:
                plot.set_xlabel("Time after maneuver start (s)")
            plot.legend(loc="upper right")
    fig.suptitle("QAV-R attitude response — ArduCopter 4.6.2 in FastDyn")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=160)
    fig.savefig(args.output.with_suffix(".svg"))
    plt.close(fig)
    print(f"Wrote {args.output} and {args.output.with_suffix('.svg')}")


if __name__ == "__main__":
    main()
