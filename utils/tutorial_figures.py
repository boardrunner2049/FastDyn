#!/usr/bin/env python3
"""Render the tutorial's vector diagrams and package mission data for the book."""

import argparse
import csv
import json
from pathlib import Path
import shutil
import sys
import tomllib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fastdyn.mission_report import east_north, mission_waypoints, write_report  # noqa: E402


def geometry(output):
    fig, axes = plt.subplots(1, 2, figsize=(10, 5))
    for ax, diagonal, label, prop in zip(axes, (.5, .22), ("Baseline · 500 mm", "QAV-R 5-inch · 220 mm"), (.254, .127)):
        d = diagonal / np.sqrt(8)
        ax.plot([-d, d], [-d, d], color="#475569", lw=7)
        ax.plot([-d, d], [d, -d], color="#475569", lw=7)
        for number, (x, y) in enumerate(((d,d),(-d,-d),(-d,d),(d,-d)), 1):
            ax.add_patch(Circle((x, y), prop/2, fc="#dbeafe", ec="#2374ab", alpha=.75))
            ax.text(x, y, str(number), ha="center", va="center", weight="bold")
        ax.annotate("front", (0, .29), (0, .34), ha="center",
                    arrowprops=dict(arrowstyle="->", color="#334155"))
        ax.set(title=label, xlim=(-.34,.34), ylim=(-.34,.38), aspect="equal",
               xlabel="Right (m)", ylabel="Forward (m)")
        ax.grid(alpha=.2)
    fig.suptitle("Motor geometry at the same scale", fontsize=15)
    fig.text(.5,.02,"QAV-R propeller diameter: 127 mm; square-X motor layout assumption.",ha="center",fontsize=10)
    fig.tight_layout(rect=(0,.05,1,.95))
    fig.savefig(output / "geometry.svg")
    plt.close(fig)


def payload(output):
    fig, (top, thrust) = plt.subplots(1, 2, figsize=(11, 4.8))
    d = .11 / np.sqrt(2)
    locations = ((d, d), (-d, -d), (-d, d), (d, -d))
    top.plot([-d,d],[-d,d],lw=7,color="#475569")
    top.plot([-d,d],[d,-d],lw=7,color="#475569")
    for number, (right, forward) in enumerate(locations, 1):
        top.add_patch(Circle((right, forward), .034, fc="#eaf4f6", ec="#2374ab"))
        top.text(right, forward, str(number), ha="center", va="center", weight="bold")
    top.scatter([0],[0],s=55,color="#334155",zorder=5)
    top.annotate("CG",(0,0),(-.045,-.015),fontsize=10)
    top.annotate("Payload at motor 1\nWeight acts downward",(d,d),(-.13,.15),
                 arrowprops=dict(arrowstyle="->",color="#c76b00",lw=2),fontsize=10,color="#985100")
    top.annotate("",(d,-.13),(0,-.13),arrowprops=dict(arrowstyle="<->",color="#475569"))
    top.text(d/2,-.146,"77.8 mm",ha="center",fontsize=9)
    top.set(xlim=(-.16,.16),ylim=(-.17,.19),aspect="equal",xlabel="Right (m)",ylabel="Forward (m)",title="Attachment · top view")
    top.grid(alpha=.15)
    motors = np.arange(4)
    for offset, payload_mass, label, color in ((-.25,0.,"No payload","#8798ab"),(0,.05,"50 g payload","#2374ab"),(.25,.50,"500 g payload","#d68a26")):
        w,p = .5*9.8,payload_mass*9.8
        thrust.bar(motors+offset,[(w+3*p)/4,(w-p)/4,(w+p)/4,(w+p)/4],width=.24,label=label,color=color)
    thrust.set(xticks=motors,xticklabels=["1 · FR","2 · RL","3 · FL","4 · RR"],ylabel="Thrust per motor (N)",title="Analytic level-hover allocation")
    thrust.legend(frameon=False,fontsize=9)
    thrust.grid(axis="y",alpha=.2)
    thrust.spines[["top","right"]].set_visible(False)
    fig.text(.5,.015,"Square-X geometry; force and moment balance including zero net rotor yaw torque.",ha="center",fontsize=9)
    fig.tight_layout(rect=(0,.04,1,1))
    fig.savefig(output / "payload.svg")
    plt.close(fig)


def package_mission(csv_path, mission, output, name):
    with csv_path.open() as handle:
        rows = [{key: float(value) if value else None for key,value in row.items()}
                for row in csv.DictReader(handle)]
    waypoints = mission_waypoints(mission)
    origin = waypoints[0]
    points = []
    for waypoint in waypoints:
        east, north = east_north(waypoint["latitude_deg"], waypoint["longitude_deg"], origin)
        points.append(dict(east=east,north=north,seq=waypoint["sequence"]))
    summary = json.loads(csv_path.with_suffix(".json").read_text())
    data = dict(rows=rows, waypoints=points, summary=summary)
    (output / f"{name}.json").write_text(json.dumps(data, allow_nan=False))
    for suffix in (".png", ".svg", ".csv"):
        source = csv_path.with_suffix(suffix)
        if source.exists():
            shutil.copyfile(source, output / (name + suffix))


def package_recordings(config_path):
    """Archive reports with the exact wrapper revision that produced the logs."""
    config = tomllib.loads(config_path.read_text())
    output = Path(config["output"])
    output.mkdir(parents=True, exist_ok=True)
    for item in config["missions"]:
        name = item["name"]
        log, mission = Path(item["log"]), Path(item["mission"])
        report = Path("out/book-reports") / (name + ".png")
        summary = write_report(log, mission, report, vehicle=item["vehicle"])
        package_mission(report.with_suffix(".csv"), mission, output, name)
        model = Path(item["model"]).read_bytes()
        (output / f"{name}.mo").write_bytes(model)
        shutil.copyfile(log, output / f"{name}.tlog")
        shutil.copyfile(Path(item["console"]), output / f"{name}-console.txt")
        shutil.copyfile(mission, output / f"{name}.waypoints")
        provenance = json.loads(Path(item["provenance"]).read_text())
        for key in ("revision", "rumoca_revision", "modelica_models_revision",
                    "model", "model_sha256", "completion"):
            if key in provenance:
                summary[key] = provenance[key]
        (output / f"{name}-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(f"{item['vehicle']}: {summary['position_samples']} samples, "
              f"{summary['duration_s']:.1f} s, final item {summary['final_mission_item']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=Path("docs/book/assets"))
    parser.add_argument("--mission-csv",type=Path)
    parser.add_argument("--mission",type=Path,default=Path("virtuals/physics/flight_controllers/courbet/mavlink/copter_mission.waypoints"))
    parser.add_argument("--name",default="baseline-mission")
    parser.add_argument("--missions", type=Path, help="TOML manifest of recorded vehicle missions")
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    geometry(args.output)
    payload(args.output)
    if args.mission_csv:
        package_mission(args.mission_csv,args.mission,args.output,args.name)
    if args.missions:
        package_recordings(args.missions)


if __name__ == "__main__":
    main()
