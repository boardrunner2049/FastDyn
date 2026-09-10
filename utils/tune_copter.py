#!/usr/bin/env python3
"""Compare TOML-defined Copter gains using attitude steps in emulated firmware."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import tomllib

import numpy as np
import tomli_w
from pymavlink import mavutil
from experiment_process import ExperimentProcess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "virtuals/physics/flight_controllers/courbet/mavlink"))
import mav_command_and_control as mission  # noqa: E402


def read_toml(path):
    with Path(path).open("rb") as handle:
        return tomllib.load(handle)


def set_parameter(mav, name, value):
    for _ in range(3):
        mav.mav.param_set_send(mav.target_system, mav.target_component,
                               name.encode(), value, mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
        end = time.monotonic() + 2
        while time.monotonic() < end:
            msg = mav.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.2)
            if msg is not None and str(msg.param_id).rstrip("\0") == name:
                if not math.isclose(msg.param_value, value, rel_tol=1e-5, abs_tol=1e-6):
                    raise RuntimeError(f"{name}: requested {value}, firmware returned {msg.param_value}")
                print(f"[tuning] {name}={msg.param_value:g}", flush=True)
                return
    raise TimeoutError(f"No parameter confirmation: {name}")


def quaternion(roll, pitch, yaw):
    cr, cp, cy = (math.cos(x / 2) for x in (roll, pitch, yaw))
    sr, sp, sy = (math.sin(x / 2) for x in (roll, pitch, yaw))
    return [cr*cp*cy + sr*sp*sy, sr*cp*cy - cr*sp*sy,
            cr*sp*cy + sr*cp*sy, cr*cp*sy - sr*sp*cy]


def fly(trial_path):
    trial = read_toml(trial_path)
    experiment = trial["experiment"]
    mav = mission.connect(experiment["connect"], 120)
    if mav.mav_type != mavutil.mavlink.MAV_TYPE_QUADROTOR:
        raise RuntimeError("The tuning experiment requires ArduCopter Quad-X")
    # This client owns stream rates during the experiment. MAVProxy's periodic
    # default request would otherwise reset the requested attitude rate to 4 Hz.
    mav.mav.request_data_stream_send(
        mav.target_system, mav.target_component, mavutil.mavlink.MAV_DATA_STREAM_ALL,
        experiment["startup_stream_rate_hz"], 1)
    for name, value in trial["parameters"].items():
        set_parameter(mav, name, float(value))
    mission.wait_for_autopilot_ready(mav, 120)
    mission.wait_for_mode(mav, "STABILIZE", 0, 30)
    arm_deadline = time.monotonic() + 30
    next_request = 0
    while time.monotonic() < arm_deadline:
        if time.monotonic() >= next_request:
            mav.mav.command_long_send(mav.target_system, mav.target_component,
                                     mavutil.mavlink.MAV_CMD_DO_SET_SAFETY_SWITCH_STATE,
                                     0, 1, 0, 0, 0, 0, 0, 0)
            mission.request_arm(mav, force=True)
            next_request = time.monotonic() + 3
        msg = mav.recv_match(blocking=True, timeout=1)
        if msg and msg.get_type() == "STATUSTEXT":
            print(f"[tuning] {msg.text}", flush=True)
        if msg and msg.get_type() == "HEARTBEAT" and msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED:
            break
    else:
        raise TimeoutError("ArduCopter did not arm")
    mission.wait_for_gps_fusion(mav, 0, 30)
    mission.wait_for_mode(mav, "GUIDED", 0, 30)
    print("[tuning] armed in GUIDED", flush=True)
    for message_id, rate in ((30, experiment["attitude_rate_hz"]),
                             (83, experiment["attitude_rate_hz"]), (36, 20), (33, 10)):
        mav.mav.command_long_send(mav.target_system, mav.target_component,
                                 mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                                 0, message_id, 1e6 / rate, 0, 0, 0, 0, 0)
    altitude = float(experiment["altitude_m"])
    mav.mav.command_long_send(mav.target_system, mav.target_component,
                             mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                             0, 0, 0, 0, 0, 0, 0, altitude)
    deadline = time.monotonic() + experiment["timeout_s"]
    origin = None
    yaw = None
    rel_alt = 0.0
    desired_rates = [math.nan] * 3
    pwm = [math.nan] * 4
    rows = []
    commands = experiment["steps"]
    duration = sum(step["duration_s"] for step in commands)
    landing = False
    complete = False
    try:
        while time.monotonic() < deadline:
            msg = mav.recv_match(blocking=True, timeout=0.5)
            if msg is None or msg.get_srcSystem() != mav.target_system:
                continue
            kind = msg.get_type()
            if kind == "STATUSTEXT":
                print(f"[tuning] {msg.text}", flush=True)
                if origin is not None and ("Failsafe" in msg.text or "Crash" in msg.text):
                    raise RuntimeError(msg.text)
            elif kind == "GLOBAL_POSITION_INT":
                rel_alt = msg.relative_alt / 1000
                if origin is None and rel_alt >= altitude * 0.95:
                    origin = msg.time_boot_ms / 1000 + experiment["settle_s"]
                    print(f"[tuning] takeoff complete; steps start at {origin:.3f}s", flush=True)
                if landing and rel_alt < 0.3:
                    complete = True
                    break
            elif kind == "ATTITUDE_TARGET":
                desired_rates = [math.degrees(msg.body_roll_rate),
                                 math.degrees(msg.body_pitch_rate), math.degrees(msg.body_yaw_rate)]
            elif kind == "SERVO_OUTPUT_RAW":
                pwm = [getattr(msg, f"servo{i}_raw") for i in range(1, 5)]
            elif kind == "ATTITUDE" and origin is not None and not landing:
                now = msg.time_boot_ms / 1000
                elapsed = now - origin
                if elapsed < 0:
                    continue
                if elapsed >= duration:
                    mission.set_mode(mav, "LAND")
                    landing = True
                    continue
                if yaw is None:
                    yaw = msg.yaw
                step_start = 0.0
                for index, step in enumerate(commands):
                    if elapsed < step_start + step["duration_s"]:
                        break
                    step_start += step["duration_s"]
                roll, pitch = step["roll_deg"], step["pitch_deg"]
                if abs(msg.roll) > math.radians(45) or abs(msg.pitch) > math.radians(45):
                    raise RuntimeError("Attitude exceeded the experiment's 45 degree limit")
                if not 2 < rel_alt < 2 * altitude:
                    raise RuntimeError(f"Altitude outside experiment bounds: {rel_alt}")
                mav.mav.set_attitude_target_send(
                    msg.time_boot_ms, mav.target_system, mav.target_component, 7,
                    quaternion(math.radians(roll), math.radians(pitch), yaw), 0, 0, 0, 0.5)
                rows.append(dict(
                    time_s=elapsed, step=index, step_time_s=elapsed-step_start,
                    roll_command_deg=roll, pitch_command_deg=pitch,
                    roll_deg=math.degrees(msg.roll), pitch_deg=math.degrees(msg.pitch),
                    roll_rate_dps=math.degrees(msg.rollspeed),
                    pitch_rate_dps=math.degrees(msg.pitchspeed),
                    roll_target_rate_dps=desired_rates[0], pitch_target_rate_dps=desired_rates[1],
                    altitude_m=rel_alt, **{f"pwm{i+1}": v for i, v in enumerate(pwm)}))
        if not complete:
            raise TimeoutError("Tuning flight did not finish and land before timeout")
    finally:
        if rows:
            with Path(trial["telemetry"]).open("w") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        mav.close()
    print("[tuning] attitude experiment completed and landed", flush=True)


def analyze(csv_path, minimum_rate_hz):
    data = np.atleast_1d(np.genfromtxt(csv_path, delimiter=",", names=True))
    if len(data) < 100:
        raise RuntimeError("Insufficient attitude telemetry")
    duration = float(data["time_s"][-1] - data["time_s"][0])
    rate = (len(data) - 1) / duration if duration > 0 else 0
    if rate < minimum_rate_hz:
        raise RuntimeError(f"Attitude telemetry was {rate:.1f} Hz; require {minimum_rate_hz:g} Hz")
    result = {"samples": len(data), "duration_s": float(data["time_s"][-1]),
              "attitude_rate_hz": rate}
    for axis in ("roll", "pitch"):
        error = data[f"{axis}_deg"] - data[f"{axis}_command_deg"]
        result[f"{axis}_rmse_deg"] = float(np.sqrt(np.mean(error**2)))
        settled = data["step_time_s"] > 1.0
        result[f"{axis}_settled_rmse_deg"] = float(np.sqrt(np.mean(error[settled]**2)))
        rate_error = data[f"{axis}_rate_dps"] - data[f"{axis}_target_rate_dps"]
        finite = np.isfinite(rate_error)
        result[f"{axis}_rate_rmse_dps"] = (
            float(np.sqrt(np.mean(rate_error[finite]**2))) if finite.any() else None)
    result["altitude_min_m"] = float(data["altitude_m"].min())
    result["altitude_max_m"] = float(data["altitude_m"].max())
    return result


def run(args):
    settings = read_toml(args.config)
    candidates = settings["candidates"]
    names = args.candidate or list(candidates)
    results = {}
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    for name in names:
        candidate = candidates[name]
        directory = output / name
        directory.mkdir(parents=True, exist_ok=True)
        parameters = {**settings["parameters"], **candidate}
        trial = {"experiment": settings["experiment"], "parameters": parameters,
                 "telemetry": str(directory / "attitude.csv")}
        trial_path = directory / "trial.toml"
        trial_path.write_text(tomli_w.dumps(trial))
        config = read_toml(args.run_config)
        config["Machine"]["monitor_port"] = settings["experiment"]["monitor_port"]
        config["Machine"]["qmp_socket"] = str(directory.resolve() / "qmp.sock")
        config["Machine"]["log_file"] = str(directory / "qemu.log")
        for group in config["Memory"].values():
            for bank in group if isinstance(group, list) else [group]:
                if bank.get("backend") == "file":
                    bank["memory_file"] = str(directory / (bank["id"] + ".ram"))
        config["Run"]["processes"]["mission"] = {
            "enabled": True, "terminate_run_on_exit": True,
            "command": [sys.executable, str(Path(__file__).resolve()), "--fly", str(trial_path)]}
        proxy = config["Run"]["processes"]["mavproxy"]
        proxy["command"] = [f"--logfile={directory.resolve() / 'mission.tlog'}"
                            if arg.startswith("--logfile=") else arg for arg in proxy["command"]]
        proxy["command"] = [arg for arg in proxy["command"] if not arg.startswith("--streamrate=")]
        proxy["command"].append("--streamrate=-1")
        config_path = directory / "fastdyn.toml"
        config_path.write_text(tomli_w.dumps(config))
        print(f"[tuning] starting {name}", flush=True)
        with (directory / "console.log").open("w") as log:
            process = ExperimentProcess(["fastdyn", "run", "-c", str(config_path),
                                        "-o", str(directory / "work")],
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + settings["experiment"]["timeout_s"] + 240
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(1)
            finally:
                process.stop()
        console = (directory / "console.log").read_text(errors="replace")
        passed = ("helper.mission.exited exit_code=0" in console
                  and "attitude experiment completed and landed" in console)
        result = {"passed": passed, "parameters": parameters}
        if (directory / "attitude.csv").exists():
            try:
                result.update(analyze(directory / "attitude.csv",
                                      settings["experiment"]["minimum_attitude_rate_hz"]))
            except (RuntimeError, ValueError) as error:
                result.update(passed=False, reason=str(error))
        results[name] = result
        (output / "results.json").write_text(json.dumps(results, indent=2, allow_nan=False) + "\n")
        print(json.dumps({name: result}), flush=True)
    return 0 if all(result["passed"] for result in results.values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/tuning/qavr.toml"))
    parser.add_argument("--run-config", type=Path, default=Path("out/qavr.toml"))
    parser.add_argument("--output", type=Path, default=Path("out/tuning"))
    parser.add_argument("--candidate", action="append")
    parser.add_argument("--export-parameters", type=Path,
                        help="Write one candidate's complete ArduPilot parameter file and exit")
    parser.add_argument("--fly", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.export_parameters:
        if not args.candidate or len(args.candidate) != 1:
            parser.error("--export-parameters requires exactly one --candidate")
        settings = read_toml(args.config)
        candidate = args.candidate[0]
        if candidate not in settings["candidates"]:
            parser.error(f"Unknown candidate: {candidate}")
        parameters = {**settings["parameters"], **settings["candidates"][candidate]}
        args.export_parameters.parent.mkdir(parents=True, exist_ok=True)
        args.export_parameters.write_text("".join(f"{name} {value}\n" for name, value in parameters.items()))
        print(f"Wrote {args.export_parameters} ({len(parameters)} parameters)")
        return 0
    if args.fly:
        fly(args.fly)
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
