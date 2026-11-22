#!/usr/bin/env python3
"""Scripted waypoint "mission" for the FastDyn Betaflight F405 rehost.

Betaflight is a stabiliser, not an autopilot: there is no onboard waypoint
executor in the default build. This helper therefore acts as the pilot. It flies
in ANGLE mode (AUX2, mapped by the FASTDYNF405 config.c) and closes the outer
loops on the host from GLOBAL_POSITION_INT (position + NED velocity from the
virtual GPS):

    position error -> desired velocity (P, capped)
    velocity error -> desired tilt     (P, capped)  -> roll/pitch sticks
    altitude error -> desired climb    (P, capped)  -> throttle around hover (PI)

Sequence: wait for heartbeat + 3D fix -> arm (AUX1) -> climb to --alt ->
visit --waypoints (north,east metres from home) -> return home -> land ->
disarm -> exit 0. Any stage timeout exits non-zero, so the script doubles as the
`terminate_run_on_exit` process of a FastDyn run.

This controller assumes the plant starts facing north and yaw input stays neutral.
Roll stick > 1500 moves east; pitch stick > 1500 moves north. The plant and
Betaflight use FLU/NWU internally; MAVLink ATTITUDE negates Betaflight's pitch.
Without a compass, GPS course can correct the reported yaw even during sideways
flight, so ATTITUDE.yaw is not used to rotate the navigation commands.
Override the axis directions with --roll-sign/--pitch-sign for other setups.

Example:
  fastdyn-env/bin/python .../mav_mission_betaflight.py --connect udpout:127.0.0.1:15551 \
      --alt 15 --waypoints "30,0;30,30;0,30" --report-interval 2
"""
import argparse
import json
import math
import os
from pathlib import Path
import socket
import struct
import sys
import time
from urllib.request import build_opener, ProxyHandler

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from pymavlink import mavutil

ANGLE_MODE = 1  # BF_MAV_MODE_ANGLE in Betaflight telemetry/mavlink.c
# The pinned firmware's minimal.xml assigns 22; older pymavlink releases
# decode the heartbeat but do not expose this newer enum name.
BETAFLIGHT_AUTOPILOT = getattr(mavutil.mavlink, "MAV_AUTOPILOT_BETAFLIGHT", 22)


def positive_float(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return value


def nonnegative_float(text):
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return value


def parse_waypoints(text):
    wps = []
    for item in text.split(";"):
        item = item.strip()
        if not item:
            continue
        n, e = item.split(",")
        point = (float(n), float(e))
        if not all(math.isfinite(value) for value in point):
            raise ValueError("waypoint coordinates must be finite")
        wps.append(point)
    return wps


def horizontal_sticks(err_n, err_e, vn, ve, args):
    """Brake on NED velocity; map north/east demand to pitch/roll sticks."""
    def stick(error, velocity, sign):
        desired = max(-args.max_speed, min(args.max_speed, args.position_gain * error))
        tilt = max(-args.max_tilt, min(args.max_tilt, args.velocity_gain * (desired - velocity)))
        if getattr(args, 'angle_response', 'actual') == 'linear':
            return int(round(1500 + sign * 500 * tilt / args.angle_limit))
        # pidLevel() scales the acro rate curve by angle_limit/max_rate.
        # Invert the pinned board's ACTUAL rates with zero expo:
        # rate(u) = center*u + (max_rate-center)*u*abs(u).
        rate = abs(tilt) / args.angle_limit * args.max_rate
        span = args.max_rate - args.center_sensitivity
        if span:
            deflection = 2 * rate / (args.center_sensitivity + math.sqrt(args.center_sensitivity ** 2 + 4 * span * rate))
        else:
            deflection = rate / args.center_sensitivity
        return int(round(1500 + sign * math.copysign(500.0 * deflection, tilt)))

    return stick(err_e, ve, args.roll_sign), stick(err_n, vn, args.pitch_sign)


def send_rc(m, sysid, compid, roll=1500, pitch=1500, thr=1000, yaw=1500, aux1=1000, aux2=1800):
    receiver = getattr(m, 'fastdyn_msp_receiver', None)
    if receiver is not None:
        payload = struct.pack('<8H', roll, pitch, thr, yaw, aux1, aux2, 1000, 1000)
        frame = bytes([len(payload), 200]) + payload  # MSP_SET_RAW_RC, MSP v1
        checksum = 0
        for value in frame:
            checksum ^= value
        receiver.send(b'$M<' + frame + bytes([checksum]))
        return
    m.mav.rc_channels_override_send(sysid, compid, roll, pitch, thr, yaw, aux1, aux2, 1000, 1000)


def legacy_position(message, previous):
    """Old BF sends zero velocity and microseconds in time_boot_ms.

    Differentiate received GPS positions using the firmware timestamp. This
    uses telemetry only, including its sampling delay and coordinate rounding.
    """
    point = (message.lat / 1e7, message.lon / 1e7, message.alt / 1000.0)
    velocity = (0.0, 0.0, 0.0)
    if previous is not None:
        tick, lat, lon, alt = previous
        dt = ((message.time_boot_ms - tick) & 0xffffffff) / 1e6
        if 0 < dt < 5:
            velocity = ((point[0]-lat)*math.pi/180*6378137/dt,
                        (point[1]-lon)*math.pi/180*6378137*math.cos(math.radians(lat))/dt,
                        -(point[2]-alt)/dt)
    return point + velocity, (message.time_boot_ms,) + point


def pause_completed_simulation():
    """Pause this run's QEMU after landing while the viewer serves its recording."""
    config = os.environ.get("FASTDYN_CONFIG")
    if not config:
        return False
    try:
        with Path(config).open("rb") as handle:
            path = tomllib.load(handle).get("Machine", {}).get("qmp_socket")
        if not path:
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(1.0)
            connection.connect(path)
            with connection.makefile("rwb") as stream:
                if "QMP" not in json.loads(stream.readline()):
                    return False
                for identifier, command in enumerate(("qmp_capabilities", "stop"), 1):
                    stream.write((json.dumps({"execute": command, "id": identifier}) + "\n").encode())
                    stream.flush()
                    while True:
                        reply = json.loads(stream.readline())
                        if reply.get("id") == identifier:
                            if "error" in reply:
                                return False
                            break
        return True
    except (OSError, ValueError):
        return False


def wait_for_viewer(url, timeout, phase=None):
    """Check the actual server, including its final recording acknowledgement."""
    deadline = time.monotonic() + timeout
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=0.5) as response:
                state = json.load(response)
            if state.get("snapshot") and (phase is None or state.get("mission", {}).get("phase") == phase):
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"flight viewer unavailable at {url}" if phase is None else "flight viewer did not retain the completed recording")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--connect", default="udpout:127.0.0.1:15551")
    ap.add_argument("--alt", type=positive_float, default=15.0, help="mission altitude above home [m]")
    ap.add_argument("--waypoints", default="30,0;30,30;0,30", help="'north,east;...' metres from home")
    ap.add_argument("--accept-radius", type=positive_float, default=3.0)
    ap.add_argument("--arrival-speed", type=positive_float, default=0.75, help="maximum speed when accepting a waypoint [m/s]")
    ap.add_argument("--settle-seconds", type=positive_float, default=1.5, help="wall seconds of stationary ground readings before disarming")
    ap.add_argument("--hover-us", type=positive_float, default=1630.0, help="initial hover throttle guess [us]")
    ap.add_argument("--max-tilt", type=positive_float, default=10.0, help="limit on tilt demand before stick conversion [deg]")
    ap.add_argument("--max-speed", type=positive_float, default=3.0, help="horizontal speed demand limit per axis [m/s]")
    ap.add_argument("--position-gain", type=positive_float, default=0.3, help="position error to velocity demand [1/s]")
    ap.add_argument("--velocity-gain", type=positive_float, default=6.0, help="velocity error to tilt demand [deg/(m/s)]")
    ap.add_argument("--max-climb", type=positive_float, default=2.5, help="vertical speed demand limit [m/s]")
    ap.add_argument("--angle-limit", type=positive_float, default=60.0, help="firmware angle_limit [deg]")
    ap.add_argument("--center-sensitivity", type=positive_float, default=70.0, help="ACTUAL rates center sensitivity [deg/s]; zero expo required")
    ap.add_argument("--max-rate", type=positive_float, default=670.0, help="ACTUAL rates maximum rate [deg/s]")
    ap.add_argument("--roll-sign", type=int, choices=(-1, 1), default=1, help="+1: stick>1500 moves east")
    ap.add_argument("--pitch-sign", type=int, choices=(-1, 1), default=1, help="+1: stick>1500 moves north")
    ap.add_argument("--rc-rate", type=positive_float, default=100.0)
    ap.add_argument("--stage-timeout", type=positive_float, default=120.0, help="wall seconds per stage/waypoint")
    ap.add_argument("--heartbeat-timeout", type=positive_float, default=40.0)
    ap.add_argument("--heartbeat-autopilot", type=int, default=BETAFLIGHT_AUTOPILOT,
                    help="expected MAV_AUTOPILOT enum (older Betaflight releases use 0)")
    ap.add_argument("--angle-mode-id", type=int, default=ANGLE_MODE,
                    help="ANGLE custom_mode identifier (older releases use 0)")
    ap.add_argument("--rc-msp", help="send MSP_SET_RAW_RC to this host:UDP-port instead of MAVLink RC")
    ap.add_argument("--legacy-position-velocity", action="store_true",
                    help="derive velocity from GPS telemetry for Betaflight 4.x")
    ap.add_argument("--angle-response", choices=('actual', 'linear'), default='actual',
                    help="ANGLE stick mapping: actual rates for 4.5+, linear for 4.4 and older")
    ap.add_argument("--telemetry-timeout", type=positive_float, default=5.0, help="maximum age of heartbeat, GPS fix, position and attitude [wall s]")
    ap.add_argument("--preflight", type=positive_float, default=8.0, help="wall seconds of RC before arming (boot grace)")
    ap.add_argument("--report-interval", type=positive_float, default=2.0)
    ap.add_argument("--viewer-seconds", type=nonnegative_float, default=0.0,
                    help="keep the flight view available after successful landing [wall s]")
    ap.add_argument("--viewer-url", help="check this flight view state URL before flying")
    ap.add_argument("--no-land", action="store_true", help="hover at the last waypoint instead of landing")
    args = ap.parse_args(argv)
    try:
        waypoints = parse_waypoints(args.waypoints)
    except ValueError as exc:
        ap.error(f"invalid --waypoints: {exc}")
    if args.alt <= 1.0:
        ap.error("--alt must exceed 1 m for takeoff/landing detection")
    if args.max_tilt > args.angle_limit or args.angle_limit > 90:
        ap.error("require --max-tilt <= --angle-limit <= 90")
    if args.center_sensitivity > args.max_rate:
        ap.error("--center-sensitivity must not exceed --max-rate")
    if not 1150 <= args.hover_us <= 1850:
        ap.error("--hover-us must be between 1150 and 1850")

    m = mavutil.mavlink_connection(args.connect, source_system=250, source_component=190)
    receiver = None
    if args.rc_msp:
        host, port = args.rc_msp.rsplit(':', 1)
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.connect((host, int(port)))
        m.fastdyn_msp_receiver = receiver
    completed = False
    try:
        if args.viewer_url:
            wait_for_viewer(args.viewer_url, args.heartbeat_timeout)
            print(f"bf-mission: viewer ready: {args.viewer_url}", flush=True)
        result = fly(m, args, waypoints)
        completed = result == 0
        if completed and args.viewer_url:
            wait_for_viewer(args.viewer_url, 5.0, phase="COMPLETE")
        if completed and args.viewer_seconds:
            if pause_completed_simulation():
                print("bf-mission: simulation paused after landing; replay remains available", flush=True)
            print(f"bf-mission: flight complete; viewer remains available for {args.viewer_seconds:g} s "
                  "for replay. Ctrl+C closes the session.", flush=True)
            until = time.monotonic() + args.viewer_seconds
            while time.monotonic() < until:
                time.sleep(min(0.2, max(0, until - time.monotonic())))
        return result
    except KeyboardInterrupt:
        print("bf-mission: closing completed flight" if completed else "bf-mission: interrupted", flush=True)
        return 0 if completed else 130
    except (OSError, RuntimeError) as exc:
        print(f"bf-mission: failed: {exc}", flush=True)
        return 1
    finally:
        # Standalone failures must stop driving the simulated motors too. FastDyn
        # also stops QEMU when this helper is its terminate_run_on_exit process.
        try:
            if m.target_system or m.target_component:
                for _ in range(5):
                    send_rc(m, m.target_system, m.target_component)
                    time.sleep(0.01)
        except OSError:
            pass
        finally:
            if receiver is not None:
                receiver.close()
            m.close()


def fly(m, args, waypoints):

    def gcs_heartbeat():
        m.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS, mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)

    def log(msg):
        print(f"bf-mission: t={time.monotonic() - t0:6.1f} {msg}", flush=True)

    t0 = time.monotonic()
    previous_legacy_position = None
    gcs_heartbeat()
    hb = None
    while time.monotonic() - t0 < args.heartbeat_timeout:
        hb = m.recv_match(type="HEARTBEAT", blocking=True, timeout=1.0)
        if hb is not None and hb.autopilot == getattr(args, 'heartbeat_autopilot', BETAFLIGHT_AUTOPILOT):
            break
        hb = None
        gcs_heartbeat()
    if hb is None:
        log("no heartbeat")
        return 2
    sysid, compid = hb.get_srcSystem(), hb.get_srcComponent()
    m.target_system, m.target_component = sysid, compid
    log(f"heartbeat sys={sysid} comp={compid}")
    # Ask for position/attitude at 10 Hz (Betaflight honours SET_MESSAGE_INTERVAL).
    for msgid, hz in ((mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, 10),
                      (mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE, 10),
                      (mavutil.mavlink.MAVLINK_MSG_ID_GPS_RAW_INT, 5)):
        m.mav.command_long_send(sysid, compid, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                                msgid, int(1e6 / hz), 0, 0, 0, 0, 0)

    # --- state -------------------------------------------------------------
    stage = "PREFLIGHT"
    stage_t = time.monotonic()
    armed = bool(hb.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
    mode = hb.custom_mode
    if armed:
        log("refusing to start a mission on an already armed vehicle")
        return 1
    gps_fix = 0
    pos = None            # (lat_deg, lon_deg, alt_m, vn, ve, vd)
    att = (0.0, 0.0, 0.0)
    home = None           # (lat, lon, alt)
    wp_index = 0
    target_ne = (0.0, 0.0)
    target_alt = args.alt
    hover = args.hover_us
    alt_i = 0.0
    last_rc = 0.0
    last_hb = 0.0
    last_report = 0.0
    last_ctl = None
    cmd = {"roll": 1500, "pitch": 1500, "thr": 1000, "yaw": 1500, "aux1": 1000, "aux2": 1800}
    disarm_t = None
    last_seen = {"HEARTBEAT": stage_t}
    max_alt = 0.0
    last_view = -math.inf
    view_path = Path(os.environ["FASTDYN_WORK_DIR"]) / "betaflight_mission.json" if os.environ.get("FASTDYN_WORK_DIR") else None

    def publish_view(force=False, phase=None):
        nonlocal last_view, view_path
        now = time.monotonic()
        if view_path is None or (not force and now - last_view < 0.2):
            return
        last_view = now
        state = {"phase": phase or stage, "home": home, "altitude": args.alt,
                 "waypoints": waypoints, "waypoint_index": wp_index,
                 "target": [*target_ne, target_alt], "armed": armed,
                 "max_altitude": max_alt, "viewer_seconds": args.viewer_seconds,
                 "elapsed": now - t0}
        try:
            temporary = view_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state))
            temporary.replace(view_path)
        except OSError as exc:
            log(f"viewer state unavailable: {exc}")
            view_path = None

    def set_stage(name):
        nonlocal stage, stage_t
        stage = name
        stage_t = time.monotonic()
        log(f"stage -> {name}")
        publish_view(force=True)

    def ne_from_home(lat, lon):
        dlat = (lat - home[0]) * 111320.0
        dlon = (lon - home[1]) * 111320.0 * math.cos(math.radians(home[0]))
        return dlat, dlon

    def arrived(target):
        n, e = ne_from_home(pos[0], pos[1])
        return (math.hypot(target[0] - n, target[1] - e) <= args.accept_radius
                and math.hypot(pos[3], pos[4]) <= args.arrival_speed
                and abs(pos[2] - home[2] - target_alt) <= 1.0)

    def on_ground():
        return (abs(pos[2] - home[2]) <= 0.25 and abs(pos[5]) <= 0.3
                and math.hypot(pos[3], pos[4]) <= args.arrival_speed)

    def control(dt):
        """Outer loops; writes cmd. dt in wall seconds."""
        nonlocal alt_i
        if pos is None or home is None:
            return
        lat, lon, alt, vn, ve, vd = pos
        n, e = ne_from_home(lat, lon)
        alt_rel = alt - home[2]
        v_up = -vd
        # horizontal
        if stage in ("NAV", "RTH", "CLIMB", "LAND", "HOVER"):
            err_n, err_e = target_ne[0] - n, target_ne[1] - e
            cmd["roll"], cmd["pitch"] = horizontal_sticks(err_n, err_e, vn, ve, args)
        # vertical
        if stage in ("CLIMB", "NAV", "RTH", "HOVER"):
            alt_err = target_alt - alt_rel
            vdes_up = max(-args.max_climb, min(args.max_climb, 0.6 * alt_err))
        elif stage == "LAND":
            vdes_up = -1.0 if alt_rel > 3.0 else -0.5
            # Arrest lateral drift before the last few metres of descent.
            if alt_rel < 3.0 and (math.hypot(n, e) > args.accept_radius
                                 or math.hypot(vn, ve) > args.arrival_speed):
                vdes_up = 0.0
        else:
            return
        v_err = vdes_up - v_up
        alt_i = max(-150.0, min(150.0, alt_i + 8.0 * v_err * dt))
        thr = hover + 60.0 * v_err + alt_i
        cmd["thr"] = int(max(1150, min(1850, thr)))

    def rc_send():
        send_rc(m, sysid, compid, **cmd)

    deadline = time.monotonic() + args.stage_timeout * (6 + len(waypoints))
    while time.monotonic() < deadline:
        now = time.monotonic()
        required = ("HEARTBEAT", "GPS_RAW_INT", "GLOBAL_POSITION_INT", "ATTITUDE")
        fresh = all(now - last_seen.get(kind, -math.inf) <= args.telemetry_timeout for kind in required)
        if stage != "PREFLIGHT" and (not fresh or gps_fix < 3):
            log("lost fresh heartbeat/GPS/position/attitude")
            publish_view(force=True, phase="FAILED")
            return 1
        if stage in ("CLIMB", "NAV", "RTH", "LAND", "SETTLE", "HOVER") and (not armed or mode != getattr(args, 'angle_mode_id', ANGLE_MODE)):
            log(f"flight interrupted: armed={armed} custom_mode={mode} (ANGLE required)")
            publish_view(force=True, phase="FAILED")
            return 1
        if now - last_hb >= 1.0:
            gcs_heartbeat()
            last_hb = now
        # ---- stage machine ---------------------------------------------------
        if stage == "PREFLIGHT":
            cmd.update(thr=1000, aux1=1000, roll=1500, pitch=1500)
            if fresh and gps_fix >= 3 and pos is not None and now - stage_t >= args.preflight:
                home = (pos[0], pos[1], pos[2])
                log(f"home lat={home[0]:.7f} lon={home[1]:.7f} alt={home[2]:.1f}")
                set_stage("ARM")
        elif stage == "ARM":
            cmd.update(thr=1000, aux1=1800)
            if armed and mode == getattr(args, 'angle_mode_id', ANGLE_MODE):
                target_ne = (0.0, 0.0)
                target_alt = args.alt
                alt_i = 0.0
                set_stage("CLIMB")
        elif stage == "CLIMB":
            if arrived((0.0, 0.0)) and abs(pos[5]) <= args.arrival_speed:
                wp_index = 0
                if waypoints:
                    target_ne = waypoints[0]
                    set_stage("NAV")
                else:
                    set_stage("HOVER" if args.no_land else "RTH")
        elif stage == "NAV":
            n, e = ne_from_home(pos[0], pos[1])
            if arrived(target_ne):
                log(f"waypoint {wp_index + 1}/{len(waypoints)} reached at n={n:.1f} e={e:.1f}")
                wp_index += 1
                if wp_index < len(waypoints):
                    target_ne = waypoints[wp_index]
                    stage_t = now
                else:
                    if not args.no_land:
                        target_ne = (0.0, 0.0)
                    set_stage("HOVER" if args.no_land else "RTH")
        elif stage == "RTH":
            n, e = ne_from_home(pos[0], pos[1])
            if arrived((0.0, 0.0)):
                log(f"home reached at n={n:.1f} e={e:.1f}")
                set_stage("LAND")
        elif stage == "LAND":
            n, e = ne_from_home(pos[0], pos[1])
            if abs(pos[2] - home[2]) <= 0.25 and math.hypot(n, e) <= args.accept_radius:
                cmd.update(thr=1000, roll=1500, pitch=1500)
                set_stage("SETTLE")
        elif stage == "SETTLE":
            cmd.update(thr=1000, roll=1500, pitch=1500)
            if on_ground():
                if disarm_t is None:
                    disarm_t = now
                elif now - disarm_t >= args.settle_seconds:
                    cmd.update(aux1=1000)
                    set_stage("DISARM")
            else:
                disarm_t = None
        elif stage == "DISARM":
            cmd.update(thr=1000, aux1=1000)
            if not armed and now - stage_t > 1.0 and on_ground():
                n, e = ne_from_home(pos[0], pos[1])
                log(f"landed and disarmed: mission complete; waypoints={wp_index}/{len(waypoints)} "
                    f"max_alt={max_alt:.1f}m final_n={n:.1f} final_e={e:.1f} alt={pos[2] - home[2]:.2f}m")
                set_stage("COMPLETE")
                return 0
        elif stage == "HOVER":
            pass
        if stage != "HOVER" and now - stage_t > args.stage_timeout:
            log(f"stage {stage} timed out")
            publish_view(force=True, phase="FAILED")
            return 1

        # ---- control + RC ----------------------------------------------------
        if now - last_rc >= 1.0 / args.rc_rate:
            dt = 0.0 if last_ctl is None else now - last_ctl
            if stage not in ("PREFLIGHT", "ARM", "SETTLE", "DISARM"):
                control(dt)
            last_ctl = now
            rc_send()
            last_rc = now

        # ---- telemetry -------------------------------------------------------
        msg = m.recv_match(blocking=True, timeout=0.005)
        if msg is not None and msg.get_srcSystem() == sysid and msg.get_srcComponent() == compid:
            mt = msg.get_type()
            last_seen[mt] = time.monotonic()
            if mt == "HEARTBEAT":
                armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                mode = msg.custom_mode
            elif mt == "GLOBAL_POSITION_INT":
                pos = (msg.lat / 1e7, msg.lon / 1e7, msg.alt / 1000.0, msg.vx / 100.0, msg.vy / 100.0, msg.vz / 100.0)
                if getattr(args, 'legacy_position_velocity', False):
                    pos, previous_legacy_position = legacy_position(msg, previous_legacy_position)
                if home is not None:
                    max_alt = max(max_alt, pos[2] - home[2])
            elif mt == "GPS_RAW_INT":
                gps_fix = msg.fix_type
            elif mt == "ATTITUDE":
                att = (math.degrees(msg.roll), math.degrees(msg.pitch), math.degrees(msg.yaw))
            elif mt == "STATUSTEXT":
                log(f"STATUSTEXT {msg.text}")

        publish_view()

        if now - last_report >= args.report_interval and pos is not None:
            last_report = now
            if home is not None:
                n, e = ne_from_home(pos[0], pos[1])
                log(f"{stage:9s} armed={int(armed)} n={n:6.1f} e={e:6.1f} alt={pos[2] - home[2]:6.1f} "
                    f"v=({pos[3]:5.2f},{pos[4]:5.2f},{-pos[5]:5.2f}) att=({att[0]:5.1f},{att[1]:5.1f},{att[2]:5.1f}) "
                    f"rc r/p/t={cmd['roll']}/{cmd['pitch']}/{cmd['thr']} tgt n/e/alt={target_ne[0]:.0f}/{target_ne[1]:.0f}/{target_alt:.0f}")
            else:
                log(f"{stage:9s} fix={gps_fix} alt={pos[2]:.1f} waiting")
    log("overall timeout")
    publish_view(force=True, phase="FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
