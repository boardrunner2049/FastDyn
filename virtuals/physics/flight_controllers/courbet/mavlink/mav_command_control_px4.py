#!/usr/bin/env python3
"""Upload and run a PX4 mission through an existing MAVLink endpoint.

The endpoint must already expose PX4 telemetry and accept commands. This helper
neither boots PX4 modules nor creates the QEMU UART/UDP bridge.
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import re
import struct
import time

from pymavlink import mavutil, mavwp


MAV = mavutil.mavlink
DEFAULT_CONNECT = "udpin:127.0.0.1:14552"
GLOBAL_FRAMES = {
    MAV.MAV_FRAME_GLOBAL: MAV.MAV_FRAME_GLOBAL_INT,
    MAV.MAV_FRAME_GLOBAL_RELATIVE_ALT: MAV.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
    MAV.MAV_FRAME_GLOBAL_TERRAIN_ALT: MAV.MAV_FRAME_GLOBAL_TERRAIN_ALT_INT,
}


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return seconds


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("param_file", type=Path, help="PX4 NAME VALUE file; comments-only is allowed")
    parser.add_argument("mission_file", type=Path, help="QGC WPL mission file to upload")
    parser.add_argument("--connect", default=DEFAULT_CONNECT)
    for name, default in (("heartbeat", 120), ("ready", 180), ("mission", 60),
                          ("arm", 120), ("mode", 180)):
        parser.add_argument(f"--{name}-timeout", type=positive_seconds, default=float(default))
    parser.add_argument("--monitor-sec", type=positive_seconds, default=180.0)
    parser.add_argument("--no-arm", action="store_true", help="Upload only, without arming or changing mode")
    parser.add_argument("--keep-home", action="store_true",
                        help="Keep an initial current=1 GLOBAL WAYPOINT as a real mission item")
    parser.add_argument("--exit-on-complete", dest="exit_on_complete", action="store_true", default=True)
    parser.add_argument("--no-exit-on-complete", dest="exit_on_complete", action="store_false",
                        help="Observe until --monitor-sec expires instead of requiring landing")
    parser.add_argument("--mutation-bin", default=os.environ.get("FASTDYN_OPTIFUZZ_MUTATION_BIN"))
    parser.add_argument("--mutation-params", default=os.environ.get("FASTDYN_OPTIFUZZ_PARAM_NAMES", ""))
    args = parser.parse_args(argv)
    for path in (args.param_file, args.mission_file):
        if not path.is_file():
            parser.error(f"file does not exist: {path}")
    return args


class PX4Link:
    """One receive loop keeps the GCS heartbeat alive and filters vehicle replies."""

    def __init__(self, mav):
        self.mav = mav
        self.system = 0
        self.component = 0
        self.next_heartbeat = 0.0
        self.messages: dict[str, tuple[object, float]] = {}
        self.airborne = False
        self.max_mission_seq = -1

    def receive(self):
        now = time.monotonic()
        if now >= self.next_heartbeat:
            self.mav.mav.heartbeat_send(MAV.MAV_TYPE_GCS, MAV.MAV_AUTOPILOT_INVALID,
                                        0, 0, MAV.MAV_STATE_ACTIVE)
            self.next_heartbeat = now + 1.0
        msg = self.mav.recv_match(blocking=True, timeout=0.5)
        if msg is None:
            return None
        if self.system and (msg.get_srcSystem(), msg.get_srcComponent()) != (self.system, self.component):
            return None
        if self.system:
            for name, own in (("target_system", self.mav.source_system),
                              ("target_component", self.mav.source_component)):
                if getattr(msg, name, 0) not in (0, own):
                    return None
            kind = msg.get_type()
            self.messages[kind] = (msg, time.monotonic())
            if kind == "STATUSTEXT":
                print(f"[px4 mission] {msg.text}", flush=True)
            elif kind == "EXTENDED_SYS_STATE" and msg.landed_state == MAV.MAV_LANDED_STATE_IN_AIR:
                self.airborne = True
            elif kind in ("MISSION_CURRENT", "MISSION_ITEM_REACHED"):
                if msg.seq != 65535:
                    self.max_mission_seq = max(self.max_mission_seq, int(msg.seq))
        return msg

    def latest(self, kind: str, max_age: float = 5.0):
        entry = self.messages.get(kind)
        if entry is not None and time.monotonic() - entry[1] <= max_age:
            return entry[0]
        return None

    def command(self, command: int, *params: float) -> None:
        self.mav.mav.command_long_send(self.system, self.component, command, 0,
                                       *(list(params) + [0] * (7 - len(params))))

    def wait_ack(self, command: int, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self.receive()
            if msg is None or msg.get_type() != "COMMAND_ACK" or msg.command != command:
                continue
            if msg.result == MAV.MAV_RESULT_ACCEPTED:
                return
            if msg.result != MAV.MAV_RESULT_IN_PROGRESS:
                raise RuntimeError(f"PX4 rejected command {command}: result={msg.result}")
        raise TimeoutError(f"PX4 command {command} acknowledgement timed out")


def connect(endpoint: str, timeout: float) -> PX4Link:
    print(f"[px4 mission] connecting to {endpoint}", flush=True)
    mav = mavutil.mavlink_connection(endpoint, source_system=255,
                                     source_component=MAV.MAV_COMP_ID_MISSIONPLANNER)
    link = PX4Link(mav)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = link.receive()
            if msg is None or msg.get_type() != "HEARTBEAT" or msg.get_srcSystem() == 0:
                continue
            if msg.type == MAV.MAV_TYPE_GCS or msg.autopilot == MAV.MAV_AUTOPILOT_INVALID:
                continue
            if msg.autopilot != MAV.MAV_AUTOPILOT_PX4:
                raise RuntimeError("Expected PX4; use mav_command_control_ardupilot.py for ArduPilot")
            link.system, link.component = msg.get_srcSystem(), msg.get_srcComponent()
            mav.target_system, mav.target_component = link.system, link.component
            print(f"[px4 mission] PX4 system={link.system} component={link.component}", flush=True)
            return link
        raise TimeoutError(f"No PX4 heartbeat on {endpoint}; initialize PX4 and connect its MAVLink bridge first")
    except BaseException:
        mav.close()
        raise


def request_streams(link: PX4Link) -> None:
    for message_id, hz in ((MAV.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, 4),
                           (MAV.MAVLINK_MSG_ID_ATTITUDE, 4),
                           (MAV.MAVLINK_MSG_ID_GPS_RAW_INT, 2),
                           (MAV.MAVLINK_MSG_ID_ESTIMATOR_STATUS, 2),
                           (MAV.MAVLINK_MSG_ID_EXTENDED_SYS_STATE, 2),
                           (MAV.MAVLINK_MSG_ID_MISSION_CURRENT, 2)):
        link.command(MAV.MAV_CMD_SET_MESSAGE_INTERVAL, message_id, 1_000_000 / hz)


def wait_ready(link: PX4Link, timeout: float) -> None:
    print("[px4 mission] waiting for GPS, global position and estimator validity", flush=True)
    required = (MAV.ESTIMATOR_ATTITUDE | MAV.ESTIMATOR_VELOCITY_HORIZ |
                MAV.ESTIMATOR_VELOCITY_VERT | MAV.ESTIMATOR_POS_HORIZ_ABS |
                MAV.ESTIMATOR_POS_VERT_ABS)
    gps = estimate = position = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        link.receive()
        gps = link.latest("GPS_RAW_INT")
        estimate = link.latest("ESTIMATOR_STATUS")
        position = link.latest("GLOBAL_POSITION_INT")
        if (gps is not None and gps.fix_type >= 3 and position is not None and
                estimate is not None and estimate.flags & required == required and
                not estimate.flags & (MAV.ESTIMATOR_GPS_GLITCH | MAV.ESTIMATOR_ACCEL_ERROR)):
            print("[px4 mission] position estimate ready; arming checks remain enforced by PX4", flush=True)
            return
    raise TimeoutError(
        "PX4 readiness timed out: "
        f"GPS fix={getattr(gps, 'fix_type', None)}, global_position={position is not None}, "
        f"estimator_flags={getattr(estimate, 'flags', None)}, required_flags={required}")


def parameter_name(msg) -> str:
    name = msg.param_id
    if isinstance(name, bytes):
        name = name.decode("ascii")
    return name.rstrip("\x00")


def encode_parameter(value: float, param_type: int) -> float:
    if not math.isfinite(value):
        raise ValueError("PX4 parameter values must be finite")
    if param_type == MAV.MAV_PARAM_TYPE_REAL32:
        wire_value = struct.unpack("<f", struct.pack("<f", value))[0]
        if not math.isfinite(wire_value):
            raise ValueError("PX4 parameter does not fit REAL32")
        return wire_value
    if param_type == MAV.MAV_PARAM_TYPE_INT32:
        if int(value) != value or not -(2**31) <= value < 2**31:
            raise ValueError(f"PX4 INT32 parameter requires an integer in range: {value}")
        payload = struct.pack("<i", int(value))
        wire_value = struct.unpack("<f", payload)[0]
        if struct.pack("<f", wire_value) != payload:
            raise ValueError("INT32 bit pattern cannot be preserved by Python float encoding")
        return wire_value
    raise ValueError(f"Unsupported PX4 parameter type {param_type}; expected INT32 or REAL32")


def set_parameter(link: PX4Link, name: str, value: float, timeout: float = 5.0) -> None:
    encoded_name = name.encode("ascii")
    if not 1 <= len(encoded_name) <= 16:
        raise ValueError(f"Invalid MAVLink parameter name: {name!r}")
    deadline = time.monotonic() + timeout
    next_request = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_request:
            link.mav.mav.param_request_read_send(link.system, link.component, encoded_name, -1)
            next_request = time.monotonic() + 1.0
        msg = link.receive()
        if msg is not None and msg.get_type() == "PARAM_VALUE" and parameter_name(msg) == name:
            param_type = msg.param_type
            break
    else:
        raise TimeoutError(f"PX4 parameter {name} was not found")

    wire_value = encode_parameter(float(value), param_type)
    deadline = time.monotonic() + timeout
    next_request = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_request:
            link.mav.mav.param_set_send(link.system, link.component, encoded_name, wire_value, param_type)
            next_request = time.monotonic() + 1.0
        msg = link.receive()
        if (msg is not None and msg.get_type() == "PARAM_VALUE" and parameter_name(msg) == name
                and msg.param_type == param_type
                and struct.pack("<f", msg.param_value) == struct.pack("<f", wire_value)):
            print(f"[px4 mission] param {name}={value:g}", flush=True)
            return
    raise TimeoutError(f"PX4 parameter {name}={value:g} was not confirmed")


def load_params(link: PX4Link, path: Path, names_csv: str, mutation_bin: str | None) -> None:
    for line_number, raw_line in enumerate(path.read_text().splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = re.split(r"[\s,]+", line)
        if len(fields) != 2:
            raise ValueError(f"{path}:{line_number}: expected NAME VALUE")
        set_parameter(link, fields[0], float(fields[1]))
    names = [name.strip() for name in names_csv.split(",") if name.strip()]
    if names:
        if not mutation_bin:
            raise ValueError("--mutation-params requires --mutation-bin")
        data = Path(mutation_bin).expanduser().read_bytes()
        if len(data) < 4 * len(names):
            raise ValueError("Mutation file has too few float32 values")
        for index, name in enumerate(names):
            set_parameter(link, name, struct.unpack_from("<f", data, 4 * index)[0])


def load_mission(path: Path, *, keep_home: bool = False) -> list:
    loader = mavwp.MAVWPLoader()
    loader.load(str(path))
    items = [loader.wp(index) for index in range(loader.count())]
    if not items:
        raise ValueError(f"Mission has no items: {path}")
    first = items[0]
    drop_home = (not keep_home and first.current == 1 and first.frame == MAV.MAV_FRAME_GLOBAL
                 and first.command == MAV.MAV_CMD_NAV_WAYPOINT)
    if drop_home:
        print("[px4 mission] removing QGC home row; PX4 mission starts at the next item", flush=True)
        items = items[1:]
    if not items:
        raise ValueError("Mission contains only a QGC home row")
    supported_frames = set(GLOBAL_FRAMES) | set(GLOBAL_FRAMES.values()) | {MAV.MAV_FRAME_MISSION}
    for seq, item in enumerate(items):
        if item.frame not in supported_frames:
            raise ValueError(f"Unsupported PX4 mission frame {item.frame} at item {seq}")
        if drop_home and item.command == MAV.MAV_CMD_DO_JUMP:
            if item.param1 < 1:
                raise ValueError("DO_JUMP targets the removed QGC home row")
            item.param1 -= 1
        item.seq = seq
        item.current = 1 if seq == 0 else 0
    return items


def send_mission_item(link: PX4Link, item, *, use_int: bool) -> None:
    frame = item.frame
    if use_int:
        frame = GLOBAL_FRAMES.get(frame, frame)
        scale = 1e7 if frame in GLOBAL_FRAMES.values() else 1
        link.mav.mav.mission_item_int_send(
            link.system, link.component, item.seq, frame, item.command, item.current,
            item.autocontinue, item.param1, item.param2, item.param3, item.param4,
            round(item.x * scale), round(item.y * scale), item.z)
    else:
        frame = {v: k for k, v in GLOBAL_FRAMES.items()}.get(frame, frame)
        link.mav.mav.mission_item_send(
            link.system, link.component, item.seq, frame, item.command, item.current,
            item.autocontinue, item.param1, item.param2, item.param3, item.param4,
            item.x, item.y, item.z)


def upload_mission(link: PX4Link, items: list, timeout: float) -> None:
    link.mav.mav.mission_clear_all_send(link.system, link.component)
    deadline = time.monotonic() + min(timeout, 5.0)
    while time.monotonic() < deadline:
        msg = link.receive()
        if (msg is not None and msg.get_type() == "MISSION_ACK"
                and getattr(msg, "mission_type", 0) == MAV.MAV_MISSION_TYPE_MISSION):
            if msg.type != MAV.MAV_MISSION_ACCEPTED:
                raise RuntimeError(f"PX4 mission clear rejected: {msg.type}")
            break
    else:
        raise TimeoutError("PX4 mission clear acknowledgement timed out")

    count = len(items)
    sent = set()
    last_request = None
    retry_at = 0.0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if time.monotonic() >= retry_at:
            if last_request is None:
                link.mav.mav.mission_count_send(link.system, link.component, count)
            else:
                seq, use_int = last_request
                send_mission_item(link, items[seq], use_int=use_int)
            retry_at = time.monotonic() + 1.0
        msg = link.receive()
        if msg is None or getattr(msg, "mission_type", 0) != MAV.MAV_MISSION_TYPE_MISSION:
            continue
        kind = msg.get_type()
        if kind == "MISSION_ACK":
            if msg.type != MAV.MAV_MISSION_ACCEPTED:
                raise RuntimeError(f"PX4 mission upload rejected: {msg.type}")
            if len(sent) == count:
                link.max_mission_seq = -1
                link.airborne = False
                print(f"[px4 mission] upload accepted: {count} items", flush=True)
                return
        elif kind in ("MISSION_REQUEST", "MISSION_REQUEST_INT"):
            seq = int(msg.seq)
            if not 0 <= seq < count:
                raise RuntimeError(f"PX4 requested out-of-range mission item {seq}/{count}")
            use_int = kind == "MISSION_REQUEST_INT"
            send_mission_item(link, items[seq], use_int=use_int)
            sent.add(seq)
            last_request = (seq, use_int)
            retry_at = time.monotonic() + 1.0
    raise TimeoutError(f"PX4 mission upload timed out: {len(sent)}/{count} items sent")


def wait_transition(link: PX4Link, command: int, params: tuple, predicate, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    next_request = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_request:
            link.command(command, *params)
            next_request = time.monotonic() + 2.0
        msg = link.receive()
        if msg is None:
            continue
        if msg.get_type() == "COMMAND_ACK" and msg.command == command:
            if msg.result not in (MAV.MAV_RESULT_ACCEPTED, MAV.MAV_RESULT_IN_PROGRESS,
                                  MAV.MAV_RESULT_TEMPORARILY_REJECTED):
                raise RuntimeError(f"PX4 {label} rejected: result={msg.result}")
        if msg.get_type() == "HEARTBEAT" and predicate(msg):
            print(f"[px4 mission] {label} confirmed", flush=True)
            return
    raise TimeoutError(f"PX4 {label} not confirmed after {timeout:g}s")


def start_mission(link: PX4Link, count: int, *, mode_timeout: float, arm_timeout: float) -> None:
    # PX4 uses (base_mode, main_mode, sub_mode), not ArduPilot's integer mode ID.
    base, main, sub = mavutil.px4_map["MISSION"]
    wait_transition(link, MAV.MAV_CMD_DO_SET_MODE, (base, main, sub),
                    lambda hb: bool(hb.base_mode & MAV.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED)
                    and (hb.custom_mode >> 16) & 0xff == main
                    and (hb.custom_mode >> 24) & 0xff == sub,
                    mode_timeout, "AUTO.MISSION")
    wait_transition(link, MAV.MAV_CMD_COMPONENT_ARM_DISARM, (1, 0),
                    lambda hb: bool(hb.base_mode & MAV.MAV_MODE_FLAG_SAFETY_ARMED),
                    arm_timeout, "arming")
    link.command(MAV.MAV_CMD_MISSION_START, 0, count - 1)
    link.wait_ack(MAV.MAV_CMD_MISSION_START)
    print("[px4 mission] mission start accepted", flush=True)


def monitor(link: PX4Link, count: int, duration: float, *, exit_on_complete: bool) -> None:
    deadline = time.monotonic() + duration
    last_seq = -1
    while time.monotonic() < deadline:
        link.receive()
        if link.max_mission_seq != last_seq:
            last_seq = link.max_mission_seq
            print(f"[px4 mission] current/reached item {last_seq}/{count - 1}", flush=True)
        hb = link.latest("HEARTBEAT")
        landed = link.latest("EXTENDED_SYS_STATE")
        if (exit_on_complete and link.airborne and last_seq >= count - 1 and hb is not None
                and not hb.base_mode & MAV.MAV_MODE_FLAG_SAFETY_ARMED and landed is not None
                and landed.landed_state == MAV.MAV_LANDED_STATE_ON_GROUND):
            print("[px4 mission] mission completed, landed and disarmed", flush=True)
            return
    if exit_on_complete:
        raise TimeoutError("PX4 mission did not finish with landing and disarming before --monitor-sec")
    print("[px4 mission] observation period ended", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    items = load_mission(args.mission_file, keep_home=args.keep_home)
    link = connect(args.connect, args.heartbeat_timeout)
    try:
        request_streams(link)
        load_params(link, args.param_file, args.mutation_params, args.mutation_bin)
        upload_mission(link, items, args.mission_timeout)
        if args.no_arm:
            print("[px4 mission] upload only; no arming or mode change requested", flush=True)
            return 0
        wait_ready(link, args.ready_timeout)
        start_mission(link, len(items), mode_timeout=args.mode_timeout, arm_timeout=args.arm_timeout)
        monitor(link, len(items), args.monitor_sec, exit_on_complete=args.exit_on_complete)
    finally:
        link.mav.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
