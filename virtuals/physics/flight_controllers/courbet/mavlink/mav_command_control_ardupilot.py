#!/usr/bin/env python3
"""Upload a small ArduPilot mission through an existing MAVLink endpoint."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import re
import struct
import time

from pymavlink import mavutil, mavwp

try:
    from fastdyn import timing
except Exception:  # pragma: no cover - keeps the script usable outside FastDyn installs.
    class _NoTiming:
        @staticmethod
        @contextmanager
        def phase(*_args, **_kwargs):
            yield

        @staticmethod
        def mark(*_args, **_kwargs):
            return None

    timing = _NoTiming()


DEFAULT_CONNECT = "udpin:127.0.0.1:14552"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("param_file", type=Path, help="ArduPilot parameter file to load")
    parser.add_argument("mission_file", type=Path, help="QGC WPL mission file to upload")
    parser.add_argument(
        "--connect",
        default=DEFAULT_CONNECT,
        help=f"MAVLink connection string (default: {DEFAULT_CONNECT})",
    )
    parser.add_argument("--heartbeat-timeout", type=float, default=120.0)
    parser.add_argument("--ready-timeout", type=float, default=180.0)
    parser.add_argument("--mission-timeout", type=float, default=60.0)
    parser.add_argument("--arm-timeout", type=float, default=120.0)
    parser.add_argument("--mode-timeout", type=float, default=180.0)
    parser.add_argument("--gps-fusion-timeout", type=float, default=180.0)
    parser.add_argument("--monitor-sec", type=float, default=180.0)
    parser.add_argument(
        "--exit-on-complete",
        dest="exit_on_complete",
        action="store_true",
        default=True,
        help="Stop monitoring once the final mission item has landed near ground",
    )
    parser.add_argument(
        "--no-exit-on-complete",
        dest="exit_on_complete",
        action="store_false",
        help="Keep monitoring until --monitor-sec expires",
    )
    parser.add_argument("--mode", default="AUTO", help="Mode to enter after upload")
    parser.add_argument(
        "--arm-mode",
        default="STABILIZE",
        help="Mode used while arming before switching to the mission mode",
    )
    parser.add_argument("--no-arm", action="store_true", help="Upload mission but do not arm")
    parser.add_argument(
        "--force-arm",
        dest="force_arm",
        action="store_true",
        default=True,
        help="Use ArduPilot's force-arm code after simulated sensors are ready",
    )
    parser.add_argument(
        "--no-force-arm",
        dest="force_arm",
        action="store_false",
        help="Use a normal arm request instead of ArduPilot's force-arm code",
    )
    parser.add_argument(
        "--mutation-bin",
        default=os.environ.get("FASTDYN_OPTIFUZZ_MUTATION_BIN"),
        help="Optional OptiFuzz binary float32 parameter values.",
    )
    parser.add_argument(
        "--mutation-params",
        default=os.environ.get("FASTDYN_OPTIFUZZ_PARAM_NAMES", ""),
        help="Comma-separated parameter names matching --mutation-bin float32 values.",
    )
    return parser.parse_args()


def require_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)


def connect(endpoint: str, heartbeat_timeout: float) -> mavutil.mavfile:
    print(f"[mission] connecting to {endpoint}", flush=True)
    mav = mavutil.mavlink_connection(endpoint)
    deadline = time.monotonic() + heartbeat_timeout
    heartbeat = None
    while time.monotonic() < deadline:
        msg = mav.recv_match(type="HEARTBEAT", blocking=True, timeout=1.0)
        if msg is None:
            continue
        source_system = int(msg.get_srcSystem())
        if source_system == 0:
            continue
        mav_type = int(getattr(msg, "type", -1))
        if mav_type == mavutil.mavlink.MAV_TYPE_GCS:
            continue
        heartbeat = msg
        break
    if heartbeat is None:
        raise TimeoutError(f"timed out waiting for ArduPilot heartbeat on {endpoint}")
    mav.target_system = int(heartbeat.get_srcSystem())
    mav.target_component = int(heartbeat.get_srcComponent())
    print(
        f"[mission] heartbeat from system={mav.target_system} component={mav.target_component}",
        flush=True,
    )
    timing.mark(
        "mission.heartbeat",
        echo=True,
        system=mav.target_system,
        component=mav.target_component,
    )
    mav.mav.heartbeat_send(
        mavutil.mavlink.MAV_TYPE_GCS,
        mavutil.mavlink.MAV_AUTOPILOT_INVALID,
        0,
        0,
        mavutil.mavlink.MAV_STATE_ACTIVE,
    )
    for stream_id in (
        mavutil.mavlink.MAV_DATA_STREAM_POSITION,
        mavutil.mavlink.MAV_DATA_STREAM_EXTRA1,
        mavutil.mavlink.MAV_DATA_STREAM_EXTENDED_STATUS,
    ):
        mav.mav.request_data_stream_send(
            mav.target_system,
            mav.target_component,
            stream_id,
            4,
            1,
        )
    return mav


def iter_params(path: Path) -> list[tuple[str, float]]:
    params: list[tuple[str, float]] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [part for part in re.split(r"[\s,]+", line) if part]
        if len(parts) < 2:
            continue
        params.append((parts[0], float(parts[1])))
    return params


def load_params(mav: mavutil.mavfile, path: Path) -> None:
    params = iter_params(path)
    if not params:
        print(f"[mission] no params in {path}", flush=True)
        return
    print(f"[mission] loading {len(params)} params from {path}", flush=True)
    for name, value in params:
        mav.mav.param_set_send(
            mav.target_system,
            mav.target_component,
            name.encode("ascii"),
            value,
            mavutil.mavlink.MAV_PARAM_TYPE_REAL32,
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            msg = mav.recv_match(type=["PARAM_VALUE", "STATUSTEXT"], blocking=True, timeout=0.5)
            if msg is None:
                continue
            if msg.get_type() == "STATUSTEXT":
                handle_runtime_message(mav, msg)
                continue
            param_id = getattr(msg, "param_id", "")
            if isinstance(param_id, bytes):
                param_id = param_id.decode("ascii", errors="ignore")
            if param_id.rstrip("\x00") == name:
                print(f"[mission] param {name}={msg.param_value:g}", flush=True)
                timing.mark("mission.param_confirmed", name=name, value=msg.param_value)
                break
        else:
            print(f"[mission] param {name} sent without confirmation", flush=True)
    print("[mission] params sent", flush=True)


def load_mutation_params(mav: mavutil.mavfile, names_csv: str, mutation_bin: str | None) -> None:
    names = [name.strip() for name in names_csv.split(",") if name.strip()]
    if not names:
        return
    if not mutation_bin:
        raise RuntimeError("mutation parameter names were provided without --mutation-bin")

    path = Path(mutation_bin).expanduser()
    require_file(path)
    data = path.read_bytes()
    needed = 4 * len(names)
    if len(data) < needed:
        raise RuntimeError(
            f"mutation file {path} has {len(data)} bytes, need {needed} for {len(names)} float32 values"
        )

    print(f"[mission] loading {len(names)} OptiFuzz mutation params from {path}", flush=True)
    for idx, name in enumerate(names):
        value = struct.unpack_from("<f", data, idx * 4)[0]
        mav.mav.param_set_send(
            mav.target_system,
            mav.target_component,
            name.encode("ascii"),
            float(value),
            mavutil.mavlink.MAV_PARAM_TYPE_REAL32,
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            msg = mav.recv_match(type=["PARAM_VALUE", "STATUSTEXT"], blocking=True, timeout=0.5)
            if msg is None:
                continue
            if msg.get_type() == "STATUSTEXT":
                handle_runtime_message(mav, msg)
                continue
            param_id = getattr(msg, "param_id", "")
            if isinstance(param_id, bytes):
                param_id = param_id.decode("ascii", errors="ignore")
            if param_id.rstrip("\x00") == name:
                print(f"[mission] mutation param {name}={msg.param_value:g}", flush=True)
                timing.mark("mission.mutation_param_confirmed", name=name, value=msg.param_value)
                break
        else:
            print(f"[mission] mutation param {name}={value:g} sent without confirmation", flush=True)
    timing.mark("mission.mutation_params_sent", echo=True, count=len(names))


def wait_ack(mav: mavutil.mavfile, command: int, timeout: float = 10.0) -> object | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = mav.recv_match(type="COMMAND_ACK", blocking=True, timeout=1.0)
        if msg and msg.command == command:
            return msg
    return None


def upload_mission(mav: mavutil.mavfile, path: Path, timeout: float) -> int:
    loader = mavwp.MAVWPLoader()
    loader.load(str(path))
    items = [loader.wp(i) for i in range(loader.count())]
    if not items:
        raise RuntimeError(f"mission file has no waypoints: {path}")

    first = items[0]
    if (
        len(items) > 1
        and int(first.current) == 1
        and int(first.command) == mavutil.mavlink.MAV_CMD_NAV_WAYPOINT
    ):
        print("[mission] keeping QGC home row for ArduPilot mission indexing", flush=True)

    for seq, item in enumerate(items):
        item.seq = seq
        item.current = 1 if seq == 0 else 0
        item.target_system = mav.target_system
        item.target_component = mav.target_component

    count = len(items)
    print(f"[mission] uploading {count} mission items from {path}", flush=True)
    mav.mav.mission_clear_all_send(mav.target_system, mav.target_component)
    clear_ack_deadline = time.monotonic() + 3.0
    while time.monotonic() < clear_ack_deadline:
        msg = mav.recv_match(type=["MISSION_ACK", "STATUSTEXT"], blocking=True, timeout=0.5)
        if msg is None:
            continue
        if msg.get_type() == "STATUSTEXT":
            handle_runtime_message(mav, msg)
            continue
        if msg.type != mavutil.mavlink.MAV_MISSION_ACCEPTED:
            raise RuntimeError(f"mission clear rejected with ack type {msg.type}")
        print("[mission] previous mission cleared", flush=True)
        break
    mav.mav.mission_count_send(mav.target_system, mav.target_component, count)

    sent: set[int] = set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = mav.recv_match(
            type=["MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK"],
            blocking=True,
            timeout=1.0,
        )
        if msg is None:
            continue
        msg_type = msg.get_type()
        if msg_type == "MISSION_ACK":
            if msg.type != mavutil.mavlink.MAV_MISSION_ACCEPTED:
                raise RuntimeError(f"mission upload rejected with ack type {msg.type}")
            if len(sent) != count:
                print(
                    f"[mission] ignoring early mission ack after {len(sent)}/{count} items",
                    flush=True,
                )
                continue
            print("[mission] upload accepted", flush=True)
            timing.mark("mission.upload_accepted", echo=True, count=count)
            return count

        seq = int(msg.seq)
        if seq < 0 or seq >= count:
            raise RuntimeError(f"autopilot requested out-of-range mission item {seq}/{count}")
        item = items[seq]
        print(f"[mission] sending mission item {seq}/{count - 1}", flush=True)
        if msg_type == "MISSION_REQUEST_INT":
            mav.mav.mission_item_int_send(
                mav.target_system,
                mav.target_component,
                item.seq,
                item.frame,
                item.command,
                item.current,
                item.autocontinue,
                item.param1,
                item.param2,
                item.param3,
                item.param4,
                int(item.x * 1e7),
                int(item.y * 1e7),
                item.z,
            )
        else:
            mav.mav.send(item)
        sent.add(seq)

    raise TimeoutError(f"timed out uploading mission; sent {len(sent)}/{count} items")


def set_mode(mav: mavutil.mavfile, mode: str) -> None:
    mapping = mav.mode_mapping()
    if mode not in mapping:
        raise RuntimeError(f"mode {mode!r} not available; modes: {sorted(mapping)}")
    print(f"[mission] setting mode {mode}", flush=True)
    mav.mav.set_mode_send(
        mav.target_system,
        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        mapping[mode],
    )


def request_arm(mav: mavutil.mavfile, *, force: bool) -> None:
    print("[mission] arming" + (" with force" if force else ""), flush=True)
    mav.mav.command_long_send(
        mav.target_system,
        mav.target_component,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
        0,
        1,
        21196 if force else 0,
        0,
        0,
        0,
        0,
        0,
    )


def handle_runtime_message(
    mav: mavutil.mavfile,
    msg: object,
    *,
    count: int | None = None,
    state: dict[str, object] | None = None,
) -> None:
    msg_type = msg.get_type()
    if msg_type == "MISSION_CURRENT" and count is not None:
        seq = int(msg.seq)
        if state is None or state.get("last_seq") != seq:
            if state is not None:
                state["last_seq"] = seq
                if seq >= count - 1:
                    state["final_item_seen"] = True
                    state.setdefault("final_item_seen_at", time.monotonic())
            print(f"[mission] current item {seq}/{count - 1}", flush=True)
            timing.mark("mission.current_item", seq=seq, count=count)
    elif msg_type == "STATUSTEXT":
        text = getattr(msg, "text", "").strip()
        if text:
            print(f"[mission] STATUSTEXT: {text}", flush=True)
            if state is not None:
                if "ArduPilot Ready" in text and not state.get("autopilot_ready"):
                    state["autopilot_ready"] = True
                    timing.mark("mission.autopilot_ready", echo=True)
                if ("GPS 1: detected" in text or "GPS lock" in text) and not state.get("gps_ready"):
                    state["gps_ready"] = True
                    timing.mark("mission.gps_ready", echo=True, text=text)
                if (
                    "EKF3 IMU0 origin set" in text
                    or "EKF3 IMU0 tilt alignment complete" in text
                    or "EKF3 IMU0 MAG0 initial yaw alignment complete" in text
                    or "AHRS: EKF3 active" in text
                ) and not state.get("ekf_ready"):
                    state["ekf_ready"] = True
                    timing.mark("mission.ekf_ready", echo=True, text=text)
                if "EKF3 IMU0 is using GPS" in text and not state.get("gps_fusing"):
                    state["gps_fusing"] = True
                    timing.mark("mission.gps_fusing", echo=True)
    elif msg_type == "GLOBAL_POSITION_INT":
        rel_alt_m = msg.relative_alt / 1000.0
        if state is not None:
            state["rel_alt_m"] = rel_alt_m
            now = time.monotonic()
            last_print_at = float(state.get("last_rel_alt_print_at", 0.0) or 0.0)
            last_print_alt = state.get("last_rel_alt_print_m")
            if (
                now - last_print_at >= 1.0
                or last_print_alt is None
                or abs(rel_alt_m - float(last_print_alt)) >= 1.0
            ):
                print(f"[mission] rel_alt={rel_alt_m:.1f}m", flush=True)
                state["last_rel_alt_print_at"] = now
                state["last_rel_alt_print_m"] = rel_alt_m
        else:
            print(f"[mission] rel_alt={rel_alt_m:.1f}m", flush=True)
    elif msg_type == "GPS_RAW_INT" and state is not None:
        if int(getattr(msg, "fix_type", 0)) >= 3:
            if not state.get("gps_ready"):
                timing.mark("mission.gps_ready", echo=True, fix_type=int(getattr(msg, "fix_type", 0)))
            state["gps_ready"] = True
    elif msg_type == "HEARTBEAT":
        if int(msg.get_srcSystem()) == mav.target_system:
            armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            if state is not None:
                state["armed"] = armed
                state["mode"] = mavutil.mode_string_v10(msg)


def wait_for_autopilot_ready(mav: mavutil.mavfile, timeout: float) -> None:
    print(f"[mission] waiting up to {timeout:g}s for ArduPilot readiness", flush=True)
    state: dict[str, object] = {
        "autopilot_ready": False,
        "gps_ready": False,
        "ekf_ready": False,
        "gps_fusing": False,
    }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = mav.recv_match(blocking=True, timeout=1.0)
        if msg is None:
            continue
        handle_runtime_message(mav, msg, state=state)
        if (
            state["autopilot_ready"]
            and state["gps_ready"]
            and state["ekf_ready"]
        ):
            print("[mission] ArduPilot ready with GPS and EKF initialized", flush=True)
            timing.mark("mission.ready", echo=True)
            return
    print(f"[mission] readiness wait timed out with state={state}", flush=True)


def ensure_armed_and_started(
    mav: mavutil.mavfile,
    mode: str,
    arm_mode: str,
    count: int,
    timeout: float,
    mode_timeout: float,
    gps_fusion_timeout: float,
    force_arm: bool,
) -> None:
    if timeout <= 0:
        raise ValueError("arm timeout must be positive")

    print(f"[mission] ensuring armed state in {arm_mode}", flush=True)
    state: dict[str, object] = {"armed": False, "last_seq": -1}
    deadline = time.monotonic() + timeout
    next_mode_request = 0.0
    next_arm_request = 0.0
    next_safety_request = 0.0

    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_mode_request:
            set_mode(mav, arm_mode)
            next_mode_request = now + 10.0
        if now >= next_safety_request:
            mav.mav.command_long_send(
                mav.target_system,
                mav.target_component,
                mavutil.mavlink.MAV_CMD_DO_SET_SAFETY_SWITCH_STATE,
                0,
                mavutil.mavlink.SAFETY_SWITCH_STATE_DANGEROUS,
                0,
                0,
                0,
                0,
                0,
                0,
            )
            print("[mission] safety switch released", flush=True)
            next_safety_request = now + 10.0
        if now >= next_arm_request:
            request_arm(mav, force=force_arm)
            next_arm_request = now + 5.0

        msg = mav.recv_match(blocking=True, timeout=1.0)
        if msg is None:
            continue
        handle_runtime_message(mav, msg, count=count, state=state)

        if msg.get_type() == "COMMAND_ACK" and msg.command == mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM:
            print(f"[mission] arm ack result={msg.result}", flush=True)
        elif (
            msg.get_type() == "COMMAND_ACK"
            and msg.command == mavutil.mavlink.MAV_CMD_DO_SET_SAFETY_SWITCH_STATE
        ):
            print(f"[mission] safety ack result={msg.result}", flush=True)

        if state.get("armed"):
            print("[mission] armed", flush=True)
            timing.mark("mission.armed", echo=True)
            break
    else:
        raise RuntimeError(f"timed out arming after {timeout:g}s")

    wait_for_gps_fusion(mav, count, gps_fusion_timeout)
    wait_for_mode(mav, mode, count, mode_timeout)
    mav.mav.mission_set_current_send(
        mav.target_system,
        mav.target_component,
        0,
    )
    print("[mission] current mission item set to 0", flush=True)
    timing.mark("mission.current_item_set", seq=0)
    mav.mav.command_long_send(
        mav.target_system,
        mav.target_component,
        mavutil.mavlink.MAV_CMD_MISSION_START,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    print("[mission] mission start requested", flush=True)
    timing.mark("mission.start_requested", echo=True)


def wait_for_gps_fusion(mav: mavutil.mavfile, count: int, timeout: float) -> None:
    print(f"[mission] waiting up to {timeout:g}s for EKF GPS fusion", flush=True)
    state: dict[str, object] = {"last_seq": -1, "gps_fusing": False}
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        msg = mav.recv_match(blocking=True, timeout=1.0)
        if msg is None:
            continue
        handle_runtime_message(mav, msg, count=count, state=state)
        if state.get("gps_fusing"):
            print("[mission] EKF GPS fusion confirmed", flush=True)
            timing.mark("mission.gps_fusion_confirmed", echo=True)
            return

    print("[mission] EKF GPS fusion wait timed out; starting mission anyway", flush=True)


def wait_for_mode(mav: mavutil.mavfile, mode: str, count: int, timeout: float) -> None:
    print(f"[mission] ensuring mode {mode}", flush=True)
    state: dict[str, object] = {"last_seq": -1, "mode": None, "armed": False}
    deadline = time.monotonic() + timeout
    next_mode_request = 0.0

    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_mode_request:
            set_mode(mav, mode)
            next_mode_request = now + 5.0

        msg = mav.recv_match(blocking=True, timeout=1.0)
        if msg is None:
            continue
        handle_runtime_message(mav, msg, count=count, state=state)

        if state.get("mode") == mode:
            print(f"[mission] mode {mode} confirmed", flush=True)
            timing.mark("mission.mode_confirmed", echo=True, mode=mode)
            return

    raise RuntimeError(f"timed out switching to {mode} after {timeout:g}s")


def monitor(mav: mavutil.mavfile, count: int, duration: float, *, exit_on_complete: bool) -> None:
    print(f"[mission] monitoring for {duration:g}s", flush=True)
    deadline = time.monotonic() + duration
    state: dict[str, object] = {
        "last_seq": -1,
        "final_item_seen": False,
        "rel_alt_m": None,
    }
    while time.monotonic() < deadline:
        msg = mav.recv_match(blocking=True, timeout=1.0)
        if msg is None:
            continue
        handle_runtime_message(mav, msg, count=count, state=state)
        if exit_on_complete and state.get("final_item_seen"):
            rel_alt = state.get("rel_alt_m")
            if rel_alt is not None and float(rel_alt) <= 1.0:
                print("[mission] final landing confirmed near ground", flush=True)
                timing.mark("mission.completed", echo=True, rel_alt_m=float(rel_alt))
                return
    print("[mission] monitor timeout reached", flush=True)


def main() -> int:
    args = parse_args()
    require_file(args.param_file)
    require_file(args.mission_file)

    with timing.phase("mission.total"):
        with timing.phase("mission.connect"):
            mav = connect(args.connect, args.heartbeat_timeout)
        with timing.phase("mission.wait_autopilot_ready"):
            wait_for_autopilot_ready(mav, args.ready_timeout)
        with timing.phase("mission.load_params"):
            load_params(mav, args.param_file)
            load_mutation_params(mav, args.mutation_params, args.mutation_bin)
        with timing.phase("mission.upload"):
            count = upload_mission(mav, args.mission_file, args.mission_timeout)
        if not args.no_arm:
            with timing.phase("mission.arm_and_start"):
                ensure_armed_and_started(
                    mav,
                    args.mode,
                    args.arm_mode,
                    count,
                    args.arm_timeout,
                    args.mode_timeout,
                    args.gps_fusion_timeout,
                    args.force_arm,
                )
        else:
            with timing.phase("mission.set_mode"):
                set_mode(mav, args.mode)
        with timing.phase("mission.monitor", duration=args.monitor_sec):
            monitor(mav, count, args.monitor_sec, exit_on_complete=args.exit_on_complete)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
