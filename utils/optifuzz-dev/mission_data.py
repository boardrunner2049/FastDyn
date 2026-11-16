"""Read a completed FastDyn FMU capture and its mission inputs."""

import csv
import json
import math
from pathlib import Path

import numpy as np
from pymavlink import mavutil
from scipy.spatial.transform import Rotation

TOOLS = Path(__file__).resolve().parent
MAVLINK = TOOLS.parents[1] / "virtuals/physics/flight_controllers/courbet/mavlink"
FRAME_SIGN = np.array([1, -1, -1])


def read_waypoints(path):
    return np.loadtxt(path, skiprows=1, ndmin=2)


def rover_legs(waypoints):
    """Map each navigation target to the drive direction set by the last DO item."""
    targets = {}
    reverse = False
    for row in waypoints:
        sequence, command = int(row[0]), int(row[3])
        if command == 194:
            reverse = bool(row[4])
        elif command == 16 and sequence:
            targets[sequence] = reverse
    return targets


def read_group(run, layout, key):
    width = 1 + sum(math.prod(row["shape"]) for row in layout[key])
    path = run / (key + ".bin")
    size = path.stat().st_size
    if size == 0 or size % (width * 8):
        raise ValueError(f"Empty or incomplete {key} capture: {path}")
    table = np.memmap(path, dtype=np.float64, mode="r").reshape(-1, width)
    values = {}
    start = 1
    for row in layout[key]:
        shape = row["shape"]
        end = start + math.prod(shape)
        values[row["name"]] = table[:, start:end].reshape(len(table), *shape)
        start = end
    return table[:, 0], values


def mult(matrix, vector):
    return np.einsum("nij,nj->ni", matrix, vector)


class Mission:
    def __init__(self, vehicle, run, *, shared_layout=None):
        self.vehicle, self.run = vehicle, Path(run)
        layout_path = self.run / "layout.json"
        # The original Rover comparison stored both interfaces under one common layout.
        if not layout_path.exists() and shared_layout is not None:
            layout_path = shared_layout
        self.layout = json.loads(layout_path.read_text())
        self.time, self.v = read_group(self.run, self.layout, "signals")
        _, params = read_group(self.run, self.layout, "parameters")
        self.p = {k: value[0] for k, value in params.items()}
        actual = self.layout.get("vehicle") or ("rover" if "plant.wheel_omega" in self.v else "copter")
        if actual != vehicle:
            raise ValueError(f"Expected {vehicle} data in {run}, found {actual}")
        if len(self.time) < 2 or not np.all(np.diff(self.time) > 0):
            raise ValueError(f"Expected increasing FMU timestamps in {run}")
        self.period = float(np.median(np.diff(self.time)))
        self.pos, self.gps, self.rot = (self.v[k] for k in ("pos_ned", "pos_gps", "rotation"))
        self.alt = -self.pos[:, 2]
        self.plan_file = self.run / "mission.waypoints"
        if not self.plan_file.exists():  # Earlier captures used the bundled mission.
            self.plan_file = (
                TOOLS / "rover_auto.waypoints"
                if vehicle == "rover"
                else MAVLINK / "copter_mission.waypoints"
            )
        self.wp = read_waypoints(self.plan_file)
        self.wp_ne = {
            int(row[0]): self.gps_ne([row[8]], [row[9]])[0]
            for row in self.wp
            if int(row[3]) in (16, 21, 22)
        }
        self.gps_pos = np.column_stack(
            (self.gps_ne(self.gps[:, 0], self.gps[:, 1]), self.p["ground_alt_wgs84"] - self.gps[:, 2])
        )
        self.events = []
        if vehicle == "rover":
            with (self.run / "events.csv").open() as f:
                self.events = list(csv.DictReader(f))
            self.lo = self.event_time("start")
            self.hi = self.event_time("complete", default=float(self.time[-1]))
        else:
            air = np.flatnonzero(self.alt > 1)
            self.lo = max(self.time[0], self.time[air[0]] - 4) if len(air) else self.time[0]
            self.hi = float(self.time[-1])

    def event_time(self, event, default=None):
        for row in self.events:
            if row["event"] == event:
                return float(row["fmu_s"])
        if default is None:
            raise ValueError(f"Missing {event} in {self.run / 'events.csv'}")
        return default

    def gps_ne(self, lat, lon):
        lat0, lon0 = np.radians([self.p["lat0"], self.p["lon0"]])
        lat, lon = np.radians(lat), np.radians(lon)
        delta = lon - lon0
        north = np.cos(lat0) * np.sin(lat) - np.sin(lat0) * np.cos(lat) * np.cos(delta)
        east = np.cos(lat) * np.sin(delta)
        cos_angle = np.sin(lat0) * np.sin(lat) + np.cos(lat0) * np.cos(lat) * np.cos(delta)
        hyp = np.hypot(north, east)
        scale = self.p["earth_radius_m"] * np.divide(
            np.arctan2(hyp, cos_angle), hyp, out=np.ones_like(hyp), where=hyp != 0
        )
        return np.column_stack((north * scale, east * scale))

    def attitude_from_quat(self):
        q = self.v["plant.q"]
        return (
            Rotation.from_quat(q[:, [1, 2, 3, 0]]).as_matrix()
            * FRAME_SIGN[:, None]
            * FRAME_SIGN[None, :]
        )

    def telemetry(self):
        path = self.run / "mavproxy/mav.tlog"
        samples = []
        if path.is_file():
            reader = mavutil.mavlink_connection(str(path))
            try:
                while (msg := reader.recv_match(type="GLOBAL_POSITION_INT")) is not None:
                    if msg.lat and msg.lon:
                        samples.append(
                            [
                                msg.time_boot_ms / 1000,
                                msg.lat / 1e7,
                                msg.lon / 1e7,
                                msg.relative_alt / 1000,
                            ]
                        )
            finally:
                reader.close()
        rows = np.array(samples).reshape(-1, 4)
        return rows, self.gps_ne(rows[:, 1], rows[:, 2])
