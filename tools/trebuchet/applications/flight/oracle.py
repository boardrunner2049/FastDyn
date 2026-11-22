"""Check the declared Courbet mission independently against FMI output and MAVLink."""
import csv
import math

from pymavlink import mavutil


def distance_m(lat, lon, target_lat, target_lon):
    north = math.radians(lat - target_lat)
    east = math.radians(lon - target_lon) * math.cos(math.radians(target_lat))
    return 6_371_000 * math.hypot(north, east)


def check_track(points, waypoints, manifest, ground_alt):
    """Visit the airborne targets in mission order, then land at the home waypoint."""
    if not points:
        raise ValueError("no positions")
    index = 0
    for lat, lon, relative_alt in waypoints:
        for index_in_track in range(index, len(points)):
            actual_lat, actual_lon, actual_alt = points[index_in_track]
            if (distance_m(actual_lat, actual_lon, lat, lon) <= manifest["waypoint_radius_m"]
                    and abs(actual_alt - ground_alt - relative_alt)
                    <= manifest["waypoint_altitude_tolerance_m"]):
                index = index_in_track + 1
                break
        else:
            raise ValueError(f"missed waypoint at {lat}, {lon}, {relative_alt} m")
    lat, lon, alt = points[-1]
    home_lat, home_lon, _ = waypoints[0]
    if (distance_m(lat, lon, home_lat, home_lon) > manifest["waypoint_radius_m"]
            or abs(alt - ground_alt) > manifest["landing_height_m"]):
        raise ValueError("final position outside landing envelope")


def validate(family, mission, manifest, plant_trace, telemetry_file, console):
    lines = [row.split() for row in mission.read_text().splitlines()[1:]]
    if [int(row[3]) for row in lines] != [16, 22, 16, 16, 16, 21]:
        raise ValueError("unexpected mission commands")
    targets = [tuple(map(float, row[8:11])) for row in lines[1:-1]]
    with plant_trace.open() as stream:
        reader = csv.DictReader(stream)
        fields = ["sim_s", "lat_deg", "lon_deg", "alt_wgs84_m", "vn_mps", "ve_mps",
                  "vd_mps", "yaw_deg", "pwm0", "pwm1", "pwm2", "pwm3"]
        if reader.fieldnames != fields:
            raise ValueError("unexpected plant trace header")
        samples = []
        for row in reader:
            if None in row or any(row[k] is None for k in fields):
                raise ValueError("truncated or malformed plant trace")
            samples.append({key: float(value) for key, value in row.items()})
    if len(samples) < 2 or not all(math.isfinite(v) for row in samples for v in row.values()):
        raise ValueError("missing or invalid FMI samples")
    if any(b["sim_s"] <= a["sim_s"] for a, b in zip(samples, samples[1:])):
        raise ValueError("non-increasing plant time")
    ground = samples[0]["alt_wgs84_m"]
    check_track([(s["lat_deg"], s["lon_deg"], s["alt_wgs84_m"]) for s in samples],
                targets, manifest, ground)
    for channel in range(4):
        pwm = [row[f"pwm{channel}"] for row in samples]
        if max(pwm) - min(pwm) < 25:
            raise ValueError(f"actuator {channel} did not respond")

    # The MAVProxy stream also contains messages from the GCS.
    autopilot = {"ardupilot": mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
                 "px4": mavutil.mavlink.MAV_AUTOPILOT_PX4}[family]
    link = mavutil.mavlink_connection(str(telemetry_file))
    positions = []
    attitudes = 0
    vehicle = None
    armed = disarmed_after_arming = False
    try:
        while (msg := link.recv_match()) is not None:
            kind = msg.get_type()
            if kind == "HEARTBEAT" and msg.autopilot == autopilot:
                source = (msg.get_srcSystem(), msg.get_srcComponent())
                if vehicle is not None and vehicle != source:
                    raise ValueError("multiple autopilot systems")
                vehicle = source
                if msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED:
                    armed = True
                elif armed:
                    disarmed_after_arming = True
            if vehicle != (msg.get_srcSystem(), msg.get_srcComponent()):
                continue
            if kind == "GLOBAL_POSITION_INT":
                positions.append((msg.lat * 1e-7, msg.lon * 1e-7, msg.relative_alt * 0.001))
            if kind == "ATTITUDE" and all(math.isfinite(v) for v in (msg.roll, msg.pitch, msg.yaw)):
                attitudes += 1
    finally:
        link.close()
    check_track(positions, targets, manifest, 0.0)
    if not armed or attitudes < 50 or (family == "px4" and not disarmed_after_arming):
        raise ValueError("missing telemetry for arming, attitude, or landing")
    required = {
        "ardupilot": ["[mission] ArduPilot ready with GPS and EKF initialized",
                      "[mission] upload accepted", "[mission] armed", "[mission] mode AUTO confirmed",
                      "[mission] mission start requested", "[mission] final landing confirmed near ground"],
        "px4": ["upload accepted: 5 items", "position estimate ready;", "AUTO.MISSION confirmed",
                "arming confirmed", "mission start accepted", "mission completed, landed and disarmed"],
    }
    for marker in required[family]:
        _, found, console = console.partition(marker)
        if not found:
            raise ValueError(f"mission event missing or out of order: {marker}")
    return {"plant_samples": len(samples), "plant_elapsed_s": samples[-1]["sim_s"] - samples[0]["sim_s"],
            "telemetry_positions": len(positions), "telemetry_attitudes": attitudes,
            "waypoints_checked_per_track": len(targets), "disarmed_after_arming": disarmed_after_arming}


if __name__ == "__main__":
    import argparse
    import hashlib
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Recheck the raw cold mission, including archived failures.")
    parser.add_argument("run_directory", type=Path, help="directory containing receipt.json and cold/")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[4]
    manifest = json.loads(Path(__file__).with_name("manifest.json").read_text())
    receipt = json.loads((args.run_directory / "receipt.json").read_text())
    mission = root / manifest["mission"]
    with mission.open("rb") as stream:
        expected = receipt["inputs"]["mission"]
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected or expected != manifest["mission_sha256"]:
            parser.exit(1, "mission digest differs from frozen input\n")
    cold = args.run_directory / "cold"
    for filename, expected in receipt.get("cold", {}).get("raw_outputs", {}).items():
        with (cold / filename).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                parser.exit(1, f"raw output digest differs: {filename}\n")
    try:
        result = validate(receipt["family"], mission, manifest, cold / "plant.csv",
                          cold / "run/mav.tlog", (cold / "console.log").read_text(errors="replace"))
    except ValueError as error:
        parser.exit(1, f"mission oracle failed: {error}\n")
    if receipt.get("cold", {}).get("oracle") != result:
        parser.exit(1, "mission oracle differs from recorded result\n")
    print(json.dumps(result, indent=2))
