"""Upload the Rover mission and record navigation events on the FMU clock."""

import csv
import json
import math
import os
import re
import struct
import sys
import time
from pathlib import Path

from mission_data import MAVLINK, read_waypoints, rover_legs

sys.path.insert(0, str(MAVLINK))
import mav_command_and_control as ctl


def last_timestamp(stream, row_bytes):
    count = stream.seek(0, 2) // row_bytes
    if not count:
        raise RuntimeError("The FMU recorder has not written its first sample")
    stream.seek((count - 1) * row_bytes)
    return struct.unpack("d", stream.read(8))[0]


def run_mission(run):
    mission = run / "mission.waypoints"
    targets = list(rover_legs(read_waypoints(mission)))
    if not targets:
        raise ValueError("The Rover mission needs at least one navigation waypoint")
    layout = json.loads((run / "layout.json").read_text())
    row_bytes = 8 * (1 + sum(math.prod(r["shape"]) for r in layout["signals"]))
    port = os.environ.get("FASTDYN_MAVLINK_GCS_PORT", "14552")
    mav = ctl.connect(f"udpin:127.0.0.1:{port}", 40)
    try:
        ctl.wait_for_autopilot_ready(mav, 40)
        ctl.load_params(mav, run / "mission.params")
        count = ctl.upload_mission(mav, mission, 30)
        ctl.ensure_armed_and_started(mav, "AUTO", "HOLD", count, 45, 25, 20, True)
        with (
            (run / "signals.bin").open("rb", buffering=0) as stream,
            (run / "events.csv").open("w", newline="") as f,
        ):
            writer = csv.writer(f)
            writer.writerow(["fmu_s", "event", "value"])

            def log(now, event, value):
                writer.writerow([now, event, value])
                f.flush()

            start = last_timestamp(stream, row_bytes)
            log(start, "start", count)
            last_seq = None
            active, reached = set(), set()
            last_position = start - 5
            finish_time = None
            previous = start
            last_advance = time.monotonic()
            while True:
                msg = mav.recv_match(blocking=True, timeout=0.35)
                now = last_timestamp(stream, row_bytes)
                if now != previous:
                    previous, last_advance = now, time.monotonic()
                elif time.monotonic() - last_advance > 60:
                    raise RuntimeError("No new FMU samples for 60 seconds")
                if finish_time is not None and now >= finish_time:
                    break
                if now >= start + 185 and finish_time is None:
                    break
                if msg is None:
                    continue
                kind = msg.get_type()
                if kind == "MISSION_CURRENT" and msg.seq != last_seq:
                    last_seq = msg.seq
                    active.add(msg.seq)
                    log(now, "current", msg.seq)
                elif kind == "MISSION_ITEM_REACHED":
                    reached.add(msg.seq)
                    log(now, "reached", msg.seq)
                elif kind == "STATUSTEXT":
                    log(now, "status", msg.text)
                    match = re.search(
                        r"Reached (?:command|waypoint)\s*#?\s*(\d+)", msg.text, re.IGNORECASE
                    )
                    if match and int(match[1]) not in reached:
                        reached.add(int(match[1]))
                        log(now, "reached", int(match[1]))
                elif kind == "GLOBAL_POSITION_INT" and now - last_position >= 4:
                    log(now, "position", f"{msg.lat / 1e7:.7f} {msg.lon / 1e7:.7f}")
                    last_position = now
                if targets[-1] in reached and finish_time is None:
                    finish_time = now + 4
            log(now, "reached_items", ",".join(map(str, sorted(reached))))
            if targets[-1] not in reached or set(targets) - (active | reached):
                raise RuntimeError(
                    f"Incomplete AUTO mission. Active items {sorted(active)}, reached {sorted(reached)}"
                )
            log(now, "complete", targets[-1])
    finally:
        mav.close()


if __name__ == "__main__":
    run_mission(Path(os.environ["FASTDYN_WORK_DIR"]))
