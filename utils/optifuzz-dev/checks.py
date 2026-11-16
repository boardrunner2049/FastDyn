"""Compare the ordinary OptiFuzz CSV with direct FMI samples and frame identities."""

import csv

import numpy as np
from mission_data import FRAME_SIGN, mult
from scipy.spatial.transform import Rotation


def coordinate_checks(data, out):
    run, time, v, p = data.run, data.time, data.v, data.p
    rot, pos, gps, gps_pos, s = data.rot, data.pos, data.gps, data.gps_pos, FRAME_SIGN
    with (run / "trace.csv").open() as f:
        header = next(f).strip().split(",")
        tab = np.loadtxt(f, delimiter=",")
    if len(tab) != len(time):
        raise ValueError(f"Trace has {len(tab)} rows, FMI capture has {len(time)}")
    if not np.isfinite(tab).all() or not all(np.isfinite(arr).all() for arr in v.values()):
        raise ValueError("Non-finite values in the trace or FMU capture")
    trace = {k: tab[:, i] for i, k in enumerate(header)}
    checks = []

    def check(name, residual, unit, tol):
        a = np.abs(residual)
        peak = float(np.max(a))
        checks.append((name, unit, peak, float(np.sqrt(np.mean(np.square(a)))), tol, peak <= tol))

    check("CSV clock vs FMU clock", trace["sim_time"] / 1000 - time, "s", 1e-9)

    def cols(names):
        return np.column_stack([trace[n] for n in names])

    trace_groups = {
        "pos_ned": (["pos_n_m", "pos_e_m", "pos_d_m"], "m"),
        "vel_ned": (["gps_vel_n", "gps_vel_e", "gps_vel_d"], "m/s"),
        "vel_frd": (["vel_frd_" + c for c in "xyz"], "m/s"),
        "accel_ned": (["accel_" + c + "_mps2" for c in "ned"], "m/s²"),
        "accel_frd": (["accel_frd_" + c for c in "xyz"], "m/s²"),
        "rpy": (["roll_rad", "pitch_rad", "yaw_rad"], "rad"),
        "omega_frd": (["omega_frd_" + c for c in "xyz"], "rad/s"),
        "alpha_frd": (["alpha_frd_" + c for c in "xyz"], "rad/s²"),
        "rotation": ([f"rotation_{i}{j}" for i in range(1, 4) for j in range(1, 4)], "1"),
        "mag": (["mag_" + c for c in "xyz"], "G"),
    }
    for name, (keys, unit) in trace_groups.items():
        check("CSV vs FMI " + name, cols(keys) - v[name].reshape(len(time), -1), unit, 1e-10)
    units = ["normalized", "normalized", "m/s", "rad/s"] if data.vehicle == "rover" else ["rad/s"] * 4
    for j, unit in enumerate(units):
        check(
            f"CSV vs FMI motor_cmd[{j + 1}]",
            trace[f"motor_cmd_{j + 1}"] - v["motor_cmd"][:, j],
            unit,
            1e-10,
        )
    if data.vehicle == "rover":
        check("Motor layout steering", v["motor_cmd"][:, 0] - v["plant.steering"], "normalized", 1e-10)
        check("Motor layout throttle", v["motor_cmd"][:, 1] - v["plant.throttle"], "normalized", 1e-10)
        check("Motor layout forward speed", v["motor_cmd"][:, 2] - v["plant.v_b"][:, 0], "m/s", 1e-10)
        check("Motor layout FRD yaw rate", v["motor_cmd"][:, 3] + v["plant.omega"][:, 2], "rad/s", 1e-10)
    check(
        "Yaw angle positive clockwise",
        v["yaw_deg"] - np.degrees(np.arctan2(rot[:, 1, 0], rot[:, 0, 0])),
        "deg",
        1e-8,
    )
    for name, unit in [
        ("baro_altitude_m", "m"),
        ("baro_pressure_pa", "Pa"),
        ("baro_climb_rate_mps", "m/s"),
        ("baro_temperature_c", "°C"),
    ]:
        check("CSV vs FMI " + name, trace[name] - v[name], unit, 1e-10)
    for i, (name, unit) in enumerate([("lat", "deg"), ("lon", "deg"), ("z", "m")]):
        check("CSV vs FMI pos_gps " + name, trace[name] - gps[:, i], unit, 1e-10)
    check("CSV vs FMI yaw_deg (float sensor)", trace["gps_yaw_deg"] - v["yaw_deg"], "deg", 2e-5)
    check("Position NWU to NED", v["plant.p"] * s - pos, "m", 1e-9)
    check("Position from GPS vs NED", gps_pos - pos, "m", 1e-6)
    check("Velocity NWU to NED", v["plant.v_w"] * s - v["vel_ned"], "m/s", 1e-9)
    check("Velocity FLU to FRD", v["plant.v_b"] * s - v["vel_frd"], "m/s", 1e-9)
    check("Velocity FRD to NED", mult(rot, v["vel_frd"]) - v["vel_ned"], "m/s", 1e-9)
    check("Acceleration FRD to NED", mult(rot, v["accel_frd"]) - v["accel_ned"], "m/s²", 1e-8)
    gravity = np.array([0, 0, p["plant.g"]])
    check(
        "Body specific force plus gravity",
        mult(rot, v["plant.accel"]) + gravity - v["accel_ned"],
        "m/s²",
        1e-8,
    )
    inertial_flu = v["der(plant.v_b)"] + np.cross(v["plant.omega"], v["plant.v_b"])
    check("Body derivative plus transport term", inertial_flu * s - v["accel_frd"], "m/s²", 1e-8)
    check("Rotation from plant matrix", v["plant.R"] * s[:, None] * s[None, :] - rot, "1", 1e-10)
    check("Rotation from plant quaternion", data.attitude_from_quat() - rot, "1", 1e-10)
    check(
        "Rotation from output Euler angles",
        Rotation.from_euler("ZYX", v["rpy"][:, ::-1]).as_matrix() - rot,
        "1",
        1e-10,
    )
    check("Rotation orthogonality", np.swapaxes(rot, 1, 2) @ rot - np.eye(3), "1", 1e-10)
    check("Rotation determinant", np.linalg.det(rot) - 1, "1", 1e-10)
    check("Angular rate FLU to FRD", v["plant.omega"] * s - v["omega_frd"], "rad/s", 1e-10)
    check("Angular acceleration FLU to FRD", v["der(plant.omega)"] * s - v["alpha_frd"], "rad/s²", 1e-9)
    torque = v["plant.M_b"] - np.cross(v["plant.omega"], v["plant.omega"] @ p["plant.J"].T)
    check(
        "Angular acceleration from moment and inertia",
        np.linalg.solve(p["plant.J"], torque.T).T * s - v["alpha_frd"],
        "rad/s²",
        1e-9,
    )
    check("Magnetometer back to NED", mult(rot, v["mag"]) - p["plant.mag_world_ned"], "G", 1e-9)
    with (out / "coordinate_checks.csv").open("w") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["check", "unit", "max_absolute_component", "rms_all_components", "tolerance", "pass"]
        )
        writer.writerows(checks)
    return [row[0] for row in checks if not row[-1]]
