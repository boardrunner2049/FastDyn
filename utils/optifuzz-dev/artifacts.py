"""Build the English report and offline viewer from a completed mission capture."""

import argparse
import gzip
import json
import os
import shutil
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np
from checks import coordinate_checks
from mission_data import MAVLINK, TOOLS, Mission
from rover_viewer import write_viewer
from tracking import motion_signs, rover_tracking, write_rows


def rover_summary(data, out, baseline):
    details, legs = rover_tracking(data, out)
    summary = {"successful_run": details}
    if baseline:
        shared_layout = data.run / "layout.json" if baseline.resolve().parent == data.run else None
        other = Mission("rover", baseline, shared_layout=shared_layout)
        if not np.array_equal(data.wp, other.wp) or any(
            data.p[k] != other.p[k] for k in ("lat0", "lon0")
        ):
            raise ValueError("The comparison run needs the same waypoints and local origin")
        summary["conventional_run"], other_legs = rover_tracking(
            other, out, "Conventional steering", "conventional"
        )
        legs.extend(other_legs)
    write_rows(out / "tracking_by_leg.csv", legs)
    summary["failed_motion_checks"] = motion_signs(data, out, details["last_navigation_s"])
    rows, ne = data.telemetry()
    mask = (rows[:, 0] >= data.lo) & (rows[:, 0] <= details["last_navigation_s"])
    if mask.any():
        aligned = np.column_stack([np.interp(rows[mask, 0], data.time, data.pos[:, i]) for i in (0, 1)])
        delta = np.linalg.norm(aligned - ne[mask], axis=1)
        summary.update(ekf_horizontal_rms_m=np.sqrt(np.mean(delta**2)), ekf_horizontal_max_m=delta.max())
    idx = np.argmin(abs(data.time - data.hi))
    summary.update(
        ekf_matched_samples=int(mask.sum()),
        settle_end_s=data.hi,
        settled_distance_to_final_m=np.linalg.norm(
            data.pos[idx, :2] - data.wp_ne[details["final_target"]]
        ),
        roll_pitch_peak_deg=np.abs(np.degrees(data.v["rpy"][:, :2])).max(),
        down_position_peak_m=np.abs(data.pos[:, 2]).max(),
    )
    return summary


def copter_summary(data):
    closest = []
    for row in data.wp:
        seq = int(row[0])
        if seq and row[3] == 16:
            target = data.wp_ne[seq]
            dist = np.linalg.norm(data.pos[:, :2] - target, axis=1)
            j = np.argmin(dist)
            closest.append(
                {
                    "waypoint": seq,
                    "target_n_e_m": target.tolist(),
                    "distance_m": dist[j],
                    "time_s": data.time[j],
                }
            )
    j = np.argmin(data.v["accel_ned"][:, 2])
    return {
        "start_s": data.time[0],
        "end_s": data.time[-1],
        "max_alt_m": data.alt.max(),
        "last_alt_m": data.alt[-1],
        "last_horizontal_distance_to_home_m": np.linalg.norm(data.pos[-1, :2] - data.wp_ne[0]),
        "closest_approach": closest,
        "telemetry_position_samples": len(data.telemetry()[0]),
        "min_down_accel_mps2": data.v["accel_ned"][j, 2],
        "min_down_accel_time_s": data.time[j],
        "contact_force_at_peak_flu_N": data.v["plant.F_ground_b"][j].tolist(),
        "yaw_wraps": int(np.sum(np.abs(np.diff(data.v["yaw_deg"])) > 180)),
    }


def generate(vehicle, run, out, baseline=None):
    data = Mission(vehicle, run)
    failures = coordinate_checks(data, out)
    if failures:
        print("Coordinate checks outside tolerance: " + ", ".join(failures))
    if vehicle == "rover":
        from rover_plots import write_plots

        report = rover_summary(data, out, baseline)
        shutil.copyfile(run / "events.csv", out / "mission_events.csv")
    else:
        from copter_plots import write_plots

        report = copter_summary(data)
    report.update(
        scenario=f"Ardu{vehicle.title()} 4.6.2 AUTO",
        samples=len(data.time),
        period_s=data.period,
        capture_start_s=data.time[0],
        capture_end_s=data.time[-1],
        signal_count=len(data.v),
        parameter_count=len(data.p),
        failed_coordinate_checks=failures,
    )
    with (run / "trace.csv").open() as f:
        report["trace_columns"] = len(f.readline().split(","))
    (out / "mission_numbers.json").write_text(json.dumps(report, indent=2) + "\n")
    np.savez_compressed(out / "fmu_signals.npz", time_s=data.time, **data.v, **data.p)
    write_plots(data, out, report)
    write_viewer(data, out)
    shutil.copyfile(data.plan_file, out / (vehicle + "_auto.waypoints"))
    params = run / "mission.params"
    if not params.exists():
        params = run / "auto.params" if vehicle == "rover" else MAVLINK / "copter_init.param"
    shutil.copyfile(params, out / (vehicle + "_auto.params"))
    if (run / "mavproxy/mav.tlog").is_file():
        shutil.copyfile(run / "mavproxy/mav.tlog", out / (f"ardu{vehicle}_auto.tlog"))
    with (
        (run / "trace.csv").open("rb") as src,
        gzip.open(out / "optifuzz_trace.csv.gz", "wb", compresslevel=4) as dest,
    ):
        shutil.copyfileobj(src, dest)
    return failures + report.get("failed_motion_checks", [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vehicle", choices=("rover", "copter"))
    parser.add_argument("--run", required=True, type=Path, help="Raw FastDyn work directory")
    parser.add_argument("--out", type=Path, help="Destination (default: artifacts/<run-folder>)")
    parser.add_argument("--baseline", type=Path, help="Optional conventional-steering Rover capture")
    parser.add_argument("--zip", action="store_true", help="Also produce a shareable archive")
    args = parser.parse_args()
    run = args.run.resolve()
    out = (args.out or TOOLS.parents[1] / "artifacts" / run.name).resolve()
    if args.baseline and args.vehicle != "rover":
        parser.error("--baseline applies to Rover reports")
    if out == run or out in run.parents:
        parser.error("--out must be separate from the raw capture")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as scratch:
        stage = Path(scratch)
        failures = generate(args.vehicle, run, stage, args.baseline)
        archive_name = f"{args.vehicle}462_auto_english.zip"
        if args.zip:
            entries = sorted(stage.iterdir())
            with ZipFile(
                stage / archive_name, "w", compression=ZIP_DEFLATED, compresslevel=4
            ) as archive:
                for item in entries:
                    archive.write(item, arcname=item.name)
        out.mkdir(exist_ok=True)
        optional = (archive_name, "conventional_track_20hz.csv", f"ardu{args.vehicle}_auto.tlog")
        for name in optional:
            if not (stage / name).exists():
                (out / name).unlink(missing_ok=True)
        for item in stage.iterdir():
            os.replace(item, out / item.name)
    print(f"Artifacts: {out}")
    if failures:
        parser.exit(1, "See coordinate_checks.csv and, for Rover, motion_sign_checks.csv.\n")


if __name__ == "__main__":
    main()
