"""Generate a self-contained English FMU and trajectory viewer for either mission."""

import base64
import html
import json
from importlib.resources import files

import numpy as np
from mission_data import TOOLS, rover_legs

public = {
    "pos_ned",
    "pos_gps",
    "vel_ned",
    "vel_frd",
    "accel_ned",
    "accel_frd",
    "rpy",
    "rotation",
    "yaw_deg",
    "omega_frd",
    "alpha_frd",
    "mag",
    "baro_altitude_m",
    "baro_pressure_pa",
    "baro_temperature_c",
    "baro_climb_rate_mps",
    "motor_cmd",
}
known = {
    "plant.p": "m",
    "plant.position": "m",
    "plant.v_b": "m/s",
    "plant.v_w": "m/s",
    "plant.velocity": "m/s",
    "plant.omega": "rad/s",
    "plant.q": "1",
    "plant.R": "1",
    "rotation": "1",
    "plant.M_b": "N·m",
    "plant.F_b": "N",
    "plant.F_ground_b": "N",
    "plant.omega_m": "rad/s",
    "plant.a_b": "m/s²",
    "plant.accel": "m/s²",
    "plant.mag": "G",
    "plant.mag_b_flu": "G",
    "plant.gyro": "rad/s",
    "pwm": "µs",
    "der(plant.omega)": "rad/s²",
    "der(plant.v_b)": "m/s²",
    "der(plant.p)": "m/s",
    "plant.T": "N",
    "plant.M_rotor": "N·m",
    "plant.V": "m/s",
    "plant.F_m": "N",
    "plant.omega_cmd": "rad/s",
    "plant.gravity_w": "m/s²",
    "plant.gravity_b": "m/s²",
    "plant.attitude.q": "1",
}
known.update(
    {
        "plant.attitude.omega": "rad/s",
        "der(plant.attitude.q)": "1/s",
        "plant.H_b": "kg·m²/s",
        "plant.M_body": "N·m",
        "plant.gravity_b_local": "m/s²",
        "plant.mag_world_w": "G",
        "gps_lat_lon": "deg",
        "angular_momentum_flu": "kg·m²/s",
        "plant.M_gyro": "N·m",
        "net_torque_flu": "N·m",
        "plant.steering": "normalized",
        "plant.throttle": "normalized",
        "plant.target_speed": "m/s",
        "plant.target_yaw_rate": "rad/s",
        "steering": "normalized",
        "throttle": "normalized",
        "plant.attitude.q_norm_err": "1",
        "plant.wheel_omega": "rad/s",
        "plant.attitude.R": "1",
    }
)


def encode(arr):
    return base64.b64encode(np.ascontiguousarray(arr, dtype="<f8").tobytes()).decode("ascii")


def write_viewer(data, out):
    time, v, p, layout = data.time, data.v, data.p, data.layout
    stride = max(1, round(0.05 / data.period))
    ix = [np.arange(0, len(time), stride), np.flatnonzero(time - time[0] < 0.27), [len(time) - 1]]
    for val in v.values():
        vec = val.reshape(len(time), -1)
        ix.extend([np.argmin(vec, axis=0), np.argmax(vec, axis=0)])
    ix = np.unique(np.concatenate(ix))
    signals = {}
    for row in layout["signals"]:
        k = row["name"]
        val = v[k]
        shape = row["shape"]
        unit = known.get(k) or row.get("unit") or ""
        channels = []
        for j, coord in enumerate(np.ndindex(*shape)):
            label = ",".join(str(x + 1) for x in coord)
            u = (
                ["deg", "deg", "m"][j]
                if k == "pos_gps"
                else ["normalized", "normalized", "m/s", "rad/s"][j]
                if k == "motor_cmd" and data.vehicle == "rover"
                else unit
            )
            channels.append(
                {
                    "label": k + ("[" + label + "]" if shape else ""),
                    "unit": u,
                    "data": encode(val[(ix, *coord)]),
                }
            )
        signals[k] = {
            "group": "Model outputs"
            if k in public
            else "Inputs"
            if k == "pwm"
            else "Internal variables",
            "channels": channels,
        }

    rover = data.vehicle == "rover"
    names = list(data.wp_ne)
    legs = rover_legs(data.wp) if rover else {}
    if rover:
        track = np.genfromtxt(out / "auto_track_20hz.csv", delimiter=",", names=True, ndmin=1)
        north, east, elapsed, seq = (
            track[k] for k in ("north_m", "east_m", "auto_elapsed_s", "active_nav_target")
        )
    else:
        north, east = data.pos[ix, 0], data.pos[ix, 1]
        elapsed, seq = time[ix] - time[0], np.zeros(len(ix), dtype=int)
    trackdata = {
        "rover": rover,
        "reverse": [n for n in legs if legs[n]],
        "n": north.tolist(),
        "e": east.tolist(),
        "t": elapsed.tolist(),
        "sequence": seq.astype(int).tolist(),
        "planN": [data.wp_ne[n][0] for n in names],
        "planE": [data.wp_ne[n][1] for n in names],
        "labels": ["Home" if n == 0 else f"WP {n}" for n in names],
    }
    meta = {"track": trackdata, "t": encode(time[ix]), "flight": [data.lo, data.hi], "signals": signals}
    params = "".join(
        "<tr><td>"
        + html.escape(k)
        + "</td><td>"
        + html.escape(np.array2string(np.asarray(val), precision=7, separator=", "))
        + "</td></tr>"
        for k, val in sorted(p.items())
    )
    js = files("plotly").joinpath("package_data", "plotly.min.js").read_text(encoding="utf-8")
    template = (TOOLS / "viewer.html").read_text()
    result = (
        template.replace("VEHICLE_NAME", data.vehicle.title())
        .replace("MISSION_INTERVAL", "AUTO interval" if rover else "Flight interval")
        .replace("PARAMS", params)
        .replace("PAYLOAD", json.dumps(meta, separators=(",", ":")).replace("<", r"\u003c"))
        .replace("PLOT_LIB", js.replace("</script", r"<\/script"))
    )
    (out / "fmu_explorer_en.html").write_text(result, encoding="utf-8")
