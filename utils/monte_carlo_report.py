#!/usr/bin/env python3
"""Plot every recorded Monte Carlo trajectory using a TOML run manifest."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fastdyn.mission_report import (  # noqa: E402
    east_north, log_messages, mission_waypoints, read_telemetry, report_rows,
)

STATUSES = {"reference", "passed", "flight_failure", "mission_incomplete", "run_error"}
COLORS = {"reference": "#172b4d", "passed": "#2374ab",
          "flight_failure": "#c83249", "mission_incomplete": "#b56c00", "run_error": "#8e5baf"}


def collect(config):
    """Use one common origin; retain failed and missing-telemetry runs in the manifest."""
    waypoints = mission_waypoints(Path(config["mission"]))
    origin = waypoints[0]
    points = []
    for waypoint in waypoints:
        east, north = east_north(waypoint["latitude_deg"], waypoint["longitude_deg"], origin)
        points.append({"east": east, "north": north, "seq": waypoint["sequence"]})
    runs, ids = [], set()
    for entry in config["runs"]:
        name, status, scale = entry["id"], entry["status"], float(entry["payload_mass_kg"])
        if not name or name in ids:
            raise ValueError(f"Empty or duplicate run id: {name!r}")
        if status not in STATUSES or not math.isfinite(scale) or scale < 0:
            raise ValueError(f"Invalid status or payload mass for {name}")
        ids.add(name)
        rows = []
        log = Path(entry["log"]) if entry.get("log") else None
        if log and log.is_file():
            telemetry = read_telemetry(log_messages(log))
            if telemetry.positions:
                rows, _, _ = report_rows(telemetry, origin)
                rows = [row for row in rows if row["time_s"] >= 0]
        if status in {"passed", "reference"} and not rows:
            raise ValueError(f"{name} is marked {status} but has no trajectory")
        runs.append({**entry, "payload_mass_kg": scale, "rows": rows,
                     "trajectory_available": bool(rows)})
    if not runs:
        raise ValueError("The study manifest contains no runs")
    return {"study": config["study"], "waypoints": points, "runs": runs}


def write_report(data, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    output.mkdir(parents=True, exist_ok=True)
    (output / "trajectories.json").write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    fig, (track, altitude) = plt.subplots(1, 2, figsize=(13, 5.8))
    points = data["waypoints"]
    track.plot([p["east"] for p in points], [p["north"] for p in points],
               "--s", color="#bf7900", markersize=4, linewidth=1.5)
    for point in points:
        track.annotate(str(point["seq"]), (point["east"], point["north"]),
                       xytext=(4, 5), textcoords="offset points", fontsize=8)
    # Draw the nominal reference last so it stays visible in a dense ensemble.
    runs = sorted(data["runs"], key=lambda run: run["status"] == "reference")
    for index, run in enumerate(runs):
        rows = run["rows"]
        if not rows:
            continue
        color = COLORS[run["status"]]
        width = 2.2 if run["status"] == "reference" else 1.2
        alpha = .95 if run["status"] in {"reference", "flight_failure"} else .45
        line, = track.plot([r["east_m"] for r in rows], [r["north_m"] for r in rows],
                          color=color, lw=width, alpha=alpha)
        line.set_gid(f"trajectory-{index}")
        line, = altitude.plot([r["time_s"] for r in rows],
                             [r["relative_altitude_m"] for r in rows],
                             color=color, lw=width, alpha=alpha)
        line.set_gid(f"altitude-{index}")
        if run["status"] in {"flight_failure", "mission_incomplete", "run_error"}:
            last = rows[-1]
            track.plot(last["east_m"], last["north_m"], "x", color=color, ms=7)
            altitude.plot(last["time_s"], last["relative_altitude_m"], "x", color=color, ms=7)
    track.set(xlabel="East of mission home (m)", ylabel="North of mission home (m)",
              title="Every recorded trajectory")
    track.set_aspect("equal", adjustable="datalim")
    altitude.set(xlabel="Time since arming (s)", ylabel="Altitude above home (m)",
                 title="Every recorded altitude trace")
    for axis in (track, altitude):
        axis.grid(alpha=.2)
        axis.spines[["top", "right"]].set_visible(False)
    missing = sum(not run["rows"] for run in runs)
    legend = [Line2D([0], [0], color=COLORS[status], label=status.replace("_", " "))
              for status in ("reference", "passed", "flight_failure", "mission_incomplete", "run_error")
              if any(run["status"] == status and run["rows"] for run in runs)]
    legend.append(Line2D([0], [0], color="#bf7900", linestyle="--", label="mission waypoints"))
    fig.legend(handles=legend, loc="lower center", ncol=len(legend), frameon=False)
    fig.suptitle(data["study"]["title"], fontsize=16)
    fig.text(.5, .08, f"{len(runs)} runs; {len(runs)-missing} trajectories; "
             f"{missing} runs without position telemetry. Crosses mark truncated failed runs.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0, .11, 1, .94))
    fig.savefig(output / "trajectories.png", dpi=180)
    fig.savefig(output / "trajectories.svg")
    plt.close(fig)
    return {"runs": len(runs), "trajectories": len(runs)-missing,
            "without_trajectory": missing}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="TOML study and run manifest")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = tomllib.loads(args.config.read_text())
    # Published manifests use paths relative to the manifest, so downloads can
    # be replotted outside the original checkout.
    if not Path(config["mission"]).is_file():
        config["mission"] = str(args.config.parent / config["mission"])
    for run in config["runs"]:
        if run.get("log") and not Path(run["log"]).is_file():
            run["log"] = str(args.config.parent / run["log"])
    print(json.dumps(write_report(collect(config), args.output), indent=2))


if __name__ == "__main__":
    main()
