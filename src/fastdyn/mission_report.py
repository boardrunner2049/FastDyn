"""Create a mission track and altitude report from MAVProxy's binary log."""

from __future__ import annotations

import argparse
from bisect import bisect_right
import csv
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Iterable

from pymavlink import mavutil, mavwp

GLOBAL_FRAMES = {0, 5}
RELATIVE_FRAMES = {3, 6}
EARTH_RADIUS_M = 6378137.0
VEHICLE_NAMES = {"copter": "ArduCopter", "plane": "ArduPlane", "rover": "ArduRover"}


@dataclass
class Telemetry:
    positions: list[dict] = field(default_factory=list)
    targets: list[dict] = field(default_factory=list)
    control_targets: list[tuple[float, float]] = field(default_factory=list)
    home_altitude_m: float | None = None
    armed_time_s: float | None = None


def read_telemetry(messages: Iterable, system_id: int = 1, vehicle: str = "copter") -> Telemetry:
    result = Telemetry()
    last_time = 0.0
    mission_item = 0
    for msg in messages:
        if msg.get_srcSystem() != system_id:
            continue
        kind = msg.get_type()
        if hasattr(msg, "time_boot_ms"):
            last_time = max(last_time, msg.time_boot_ms / 1000.0)
        if kind == "HOME_POSITION":
            result.home_altitude_m = msg.altitude / 1000.0
        elif kind == "HEARTBEAT":
            if (
                msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                and result.armed_time_s is None
            ):
                result.armed_time_s = last_time
        elif kind == "MISSION_CURRENT":
            mission_item = msg.seq
        elif kind == "GLOBAL_POSITION_INT":
            if msg.lat == 0 and msg.lon == 0:
                continue  # No position fix yet.
            result.positions.append(
                dict(
                    boot_time_s=msg.time_boot_ms / 1000.0,
                    latitude_deg=msg.lat / 1e7,
                    longitude_deg=msg.lon / 1e7,
                    relative_altitude_m=msg.relative_alt / 1000.0,
                    mission_item=mission_item,
                    ground_speed_mps=math.hypot(msg.vx, msg.vy) / 100.0
                    if hasattr(msg, "vx") and hasattr(msg, "vy") else None,
                )
            )
        elif kind == "POSITION_TARGET_GLOBAL_INT":
            if msg.type_mask & 4 or not math.isfinite(msg.alt):
                continue  # Z is ignored or invalid; never substitute a mission-file altitude.
            if msg.coordinate_frame not in GLOBAL_FRAMES | RELATIVE_FRAMES:
                continue  # Terrain-relative targets require a terrain reference.
            result.targets.append(
                dict(
                    boot_time_s=msg.time_boot_ms / 1000.0,
                    altitude_m=msg.alt,
                    frame=msg.coordinate_frame,
                    home_altitude_m=result.home_altitude_m,
                )
            )
        elif (
            kind == "NAV_CONTROLLER_OUTPUT"
            and vehicle != "rover"
            and result.positions
            and math.isfinite(msg.alt_error)
        ):
            # Copter and fixed-wing Plane report target minus measured altitude.
            # Rover sends zero in alt_error; that is not an altitude setpoint.
            # Pair it with the latest GLOBAL_POSITION_INT in the telemetry stream.
            position = result.positions[-1]
            if last_time - position["boot_time_s"] <= 1.0:
                result.control_targets.append(
                    (
                        position["boot_time_s"],
                        position["relative_altitude_m"] + msg.alt_error,
                    )
                )
    result.positions.sort(key=lambda p: p["boot_time_s"])
    result.targets.sort(key=lambda p: p["boot_time_s"])
    result.control_targets.sort(key=lambda target: target[0])
    return result


def log_messages(path: Path):
    connection = mavutil.mavlink_connection(str(path))
    try:
        while (msg := connection.recv_match()) is not None:
            yield msg
    finally:
        connection.close()


def altitude_targets(telemetry: Telemetry) -> list[tuple[float, float]]:
    targets = []
    for target in telemetry.targets:
        altitude = target["altitude_m"]
        if target["frame"] in GLOBAL_FRAMES:
            home = target["home_altitude_m"]
            if home is None:
                home = telemetry.home_altitude_m
            if home is None:
                continue
            altitude -= home
        targets.append((target["boot_time_s"], altitude))
    return targets


def mission_waypoints(path: Path) -> list[dict]:
    loader = mavwp.MAVWPLoader()
    loader.load(str(path))
    waypoints = []
    for index in range(loader.count()):
        wp = loader.wp(index)
        if (
            wp.frame not in GLOBAL_FRAMES | RELATIVE_FRAMES
            or not 16 <= wp.command <= 95
        ):
            continue
        if wp.x == 0 and wp.y == 0:
            continue
        waypoints.append(dict(sequence=wp.seq, latitude_deg=wp.x, longitude_deg=wp.y))
    if not waypoints:
        raise ValueError("mission file contains no geographic waypoints")
    return waypoints


def east_north(latitude: float, longitude: float, origin: dict) -> tuple[float, float]:
    north = EARTH_RADIUS_M * math.radians(latitude - origin["latitude_deg"])
    east = (
        EARTH_RADIUS_M
        * math.cos(math.radians(origin["latitude_deg"]))
        * math.radians(longitude - origin["longitude_deg"])
    )
    return east, north


def report_rows(telemetry: Telemetry, origin: dict) -> tuple[list[dict], float, str]:
    if not telemetry.positions:
        raise ValueError("mission log contains no GLOBAL_POSITION_INT telemetry")
    start = telemetry.armed_time_s
    time_label = "Time since arming (s)"
    if start is None:
        start = telemetry.positions[0]["boot_time_s"]
        time_label = "Time since first position (s)"
    targets = altitude_targets(telemetry)
    target_times = [t for t, _ in targets]
    control_times = [t for t, _ in telemetry.control_targets]
    rows = []
    for position in telemetry.positions:
        if position["boot_time_s"] < start - 2.0:
            continue
        east, north = east_north(
            position["latitude_deg"], position["longitude_deg"], origin
        )
        index = bisect_right(target_times, position["boot_time_s"]) - 1
        control_index = bisect_right(control_times, position["boot_time_s"]) - 1
        rows.append(
            dict(
                time_s=position["boot_time_s"] - start,
                **position,
                east_m=east,
                north_m=north,
                navigation_altitude_target_m=targets[index][1] if index >= 0 else None,
                altitude_setpoint_m=telemetry.control_targets[control_index][1]
                if control_index >= 0
                else None,
            )
        )
    if not rows:
        raise ValueError("mission log contains no position telemetry after arming")
    return rows, start, time_label


def write_report(
    log: Path, mission: Path, output: Path, system_id: int = 1,
    vehicle: str = "copter",
) -> dict:
    # Import only for plotting; telemetry parsing and tests do not need a GUI.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    vehicle_name = VEHICLE_NAMES[vehicle]
    telemetry = read_telemetry(log_messages(log), system_id, vehicle)
    is_rover = vehicle == "rover"
    waypoints = mission_waypoints(mission)
    origin = waypoints[0]
    rows, start, time_label = report_rows(telemetry, origin)
    targets = altitude_targets(telemetry)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.with_suffix(".csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    fig, (track, altitude) = plt.subplots(
        1, 2, figsize=(13, 5.8), gridspec_kw={"width_ratios": [1, 1.45]}
    )
    fig.suptitle(
        f"{vehicle_name} mission summary", fontsize=18, fontweight="bold", x=0.06, ha="left"
    )
    wp_xy = [
        east_north(w["latitude_deg"], w["longitude_deg"], origin) for w in waypoints
    ]
    track.plot(
        *zip(*wp_xy),
        "--o",
        color="#d08028",
        linewidth=1.4,
        markersize=5,
        label="Mission waypoints",
    )
    track.plot(
        [r["east_m"] for r in rows],
        [r["north_m"] for r in rows],
        color="#2563a6",
        linewidth=2,
        label="Vehicle track",
    )
    labels = {}
    for waypoint, xy in zip(waypoints, wp_xy):
        labels.setdefault((round(xy[0], 2), round(xy[1], 2)), []).append(
            str(waypoint["sequence"])
        )
    for xy, sequences in labels.items():
        track.annotate(
            " / ".join(sequences),
            xy,
            xytext=(7, 8),
            textcoords="offset points",
            fontsize=9,
            bbox=dict(facecolor="white", edgecolor="none", alpha=0.85, pad=1.5),
        )
    track.set(
        title="Top-down track",
        xlabel="East of mission home (m)",
        ylabel="North of mission home (m)",
    )
    track.set_aspect("equal", adjustable="datalim")
    track.margins(0.18)
    track.legend(loc="upper right", fontsize=9)

    altitude.plot(
        [r["time_s"] for r in rows],
        [r["ground_speed_mps" if is_rover else "relative_altitude_m"] for r in rows],
        color="#2563a6",
        linewidth=2,
        label="Measured ground speed" if is_rover else "Measured altitude",
    )
    visible_targets = [
        (t - start, a) for t, a in targets if t >= rows[0]["boot_time_s"]
    ]
    if visible_targets and not is_rover:
        altitude.step(
            *zip(*visible_targets),
            where="post",
            color="#73816b",
            linestyle=":",
            linewidth=1.5,
            label="Navigation altitude target",
        )
    control_targets = [
        (t - start, a)
        for t, a in telemetry.control_targets
        if t >= rows[0]["boot_time_s"]
    ]
    if control_targets:
        altitude.plot(
            *zip(*control_targets),
            color="#d08028",
            linestyle="--",
            linewidth=1.6,
            label="Control altitude setpoint (derived)",
        )
    elif not is_rover:
        altitude.text(
            0.5,
            0.9,
            "Altitude target telemetry unavailable",
            transform=altitude.transAxes,
            ha="center",
            color="#8b4513",
        )
    altitude.axhline(0, color="#94a3b8", linewidth=0.8)
    altitude.set(
        title="Ground speed" if is_rover else "Altitude tracking",
        xlabel=time_label,
        ylabel="Ground speed (m/s)" if is_rover else "Altitude above home (m)",
    )
    altitude.legend(loc="upper left", fontsize=9)
    for axis in (track, altitude):
        axis.grid(alpha=0.2)
        axis.spines[["top", "right"]].set_visible(False)
    fig.text(
        0.06,
        0.045,
        "Clock: firmware simulation time  •  "
        + ("Ground speed from GPS velocity" if is_rover else "Altitudes relative to home"),
        fontsize=9,
        color="#475569",
    )
    fig.text(
        0.06,
        0.015,
        "Rover has no altitude controller. Ground speed = hypot(GLOBAL_POSITION_INT.vx, vy) / 100."
        if is_rover else
        "Control setpoint = measured altitude + NAV_CONTROLLER_OUTPUT.alt_error; navigation target = POSITION_TARGET_GLOBAL_INT.",
        fontsize=8,
        color="#475569",
    )
    fig.subplots_adjust(left=0.075, right=0.98, top=0.84, bottom=0.17, wspace=0.28)
    fig.savefig(output, dpi=180)
    fig.savefig(output.with_suffix(".svg"))
    plt.close(fig)
    summary = dict(
        vehicle=vehicle,
        log=str(log),
        mission=str(mission),
        position_samples=len(rows),
        altitude_target_samples=len(telemetry.control_targets),
        navigation_target_samples=len(targets),
        max_relative_altitude_m=max(r["relative_altitude_m"] for r in rows),
        final_relative_altitude_m=rows[-1]["relative_altitude_m"],
        final_mission_item=rows[-1]["mission_item"],
        duration_s=rows[-1]["time_s"],
        time_reference=time_label,
        altitude_setpoint_source=None if is_rover else
        "GLOBAL_POSITION_INT.relative_alt + NAV_CONTROLLER_OUTPUT.alt_error",
        max_ground_speed_mps=max(
            (r["ground_speed_mps"] for r in rows if r["ground_speed_mps"] is not None),
            default=None,
        ),
        navigation_target_source="POSITION_TARGET_GLOBAL_INT",
        home_altitude_m=telemetry.home_altitude_m,
    )
    output.with_suffix(".json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--mission", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Summary PNG; also writes SVG, CSV, and JSON alongside it",
    )
    parser.add_argument("--system-id", type=int, default=1)
    parser.add_argument("--vehicle", choices=VEHICLE_NAMES, default="copter")
    parser.add_argument("--require-setpoint", action="store_true")
    args = parser.parse_args()
    summary = write_report(args.log, args.mission, args.output, args.system_id, args.vehicle)
    print(json.dumps(summary, indent=2))
    if args.require_setpoint and summary["altitude_target_samples"] == 0:
        parser.error("no usable altitude setpoint telemetry was recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
