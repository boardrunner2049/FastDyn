#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
config="${FASTDYN_BF_SMOKE_CONFIG:-configs/betaflight_f405.toml}"
log_file="${FASTDYN_BF_SMOKE_LOG:-out/ci/betaflight_f405_mission.log}"
work_dir="${FASTDYN_BF_SMOKE_WORK_DIR:-out/ci/betaflight_f405_work}"
timeout_sec="${FASTDYN_BF_SMOKE_TIMEOUT_SEC:-240}"
min_alt="${FASTDYN_BF_SMOKE_MIN_ALT:-10}"
min_waypoints="${FASTDYN_BF_SMOKE_MIN_WAYPOINTS:-3}"
landing_radius="${FASTDYN_BF_SMOKE_LANDING_RADIUS:-3}"
# Interactive runs retain the viewer after landing; automation exits immediately.
export FASTDYN_BF_VIEW_SECONDS=0

if [[ -f fastdyn-env/bin/activate ]]; then
  # shellcheck disable=SC1091
  source fastdyn-env/bin/activate
fi
mkdir -p -- "$(dirname -- "$log_file")"
printf '[ci] starting Betaflight F405 mission: config=%s log=%s\n' "$config" "$log_file"
if timeout --signal=INT --kill-after=10 "$timeout_sec" \
    fastdyn run -c "$config" -o "$work_dir" >"$log_file" 2>&1; then
  run_status=0
else
  run_status=$?
fi
if (( run_status != 0 )); then
  printf '[ci] FastDyn exited with status %s; mission failed\n' "$run_status" >&2
  tail -100 -- "$log_file" >&2
  exit 1
fi

python3 - "$log_file" "$min_alt" "$min_waypoints" "$landing_radius" "$work_dir" <<'PY'
import json
import math
from pathlib import Path
import re
import sys

log = Path(sys.argv[1]).read_text(errors="replace")
min_alt, min_waypoints, landing_radius = float(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
summary = re.search(
    r"landed and disarmed: mission complete; waypoints=(\d+)/(\d+) "
    r"max_alt=([-+\d.]+)m final_n=([-+\d.]+) final_e=([-+\d.]+) alt=([-+\d.]+)m", log)
checks = {
    "FMU ready": "FMU ready:" in log,
    "FMU loaded": "FMU backend loaded:" in log,
    # The old banner is printed by ArduPilot's start_advancing_sim callback.
    # Betaflight steps from its SysTick rule; check actual advancing FMU time.
    "FMU advancement": re.search(r"FMU timing: sim=[1-9][\d.]*s .*advance_calls=[1-9]\d*", log) is not None,
    "1 ms SysTick": re.search(r"periodic IRQ 15 every 1000000 ns", log, re.IGNORECASE) is not None,
    "MAVCesium": "MAVCesium web viewer:" in log,
    "Betaflight heartbeat": re.search(r"bf-mission: .*heartbeat sys=", log) is not None,
    "viewer HTTP ready": "bf-mission: viewer ready:" in log,
    "completion": summary is not None,
    "no firmware fault": "betaflight: FAULT" not in log and "betaflight: failureMode(" not in log,
}
for stage in ("CLIMB", "NAV", "RTH", "LAND", "SETTLE", "DISARM"):
    checks[stage] = f"stage -> {stage}" in log
recording_path = Path(sys.argv[5]) / "betaflight_flight.json"
try:
    recording = json.loads(recording_path.read_text())
    checks["viewer recording"] = recording["mission"]["phase"] == "COMPLETE" and len(recording["history"]) > 50
except (OSError, ValueError, KeyError):
    checks["viewer recording"] = False
if summary:
    done, total = int(summary[1]), int(summary[2])
    altitude, north, east, final_altitude = map(float, summary.group(3, 4, 5, 6))
    reached = re.findall(r"waypoint (\d+)/(\d+) reached", log)
    checks.update({
        "waypoints": done == total and total >= min_waypoints,
        "ordered waypoint arrivals": reached == [(str(i), str(total)) for i in range(1, total + 1)],
        "flight altitude": altitude >= min_alt,
        "landing near home": math.hypot(north, east) <= landing_radius,
        "ground altitude": abs(final_altitude) <= 0.25,
    })
missing = [name for name, ok in checks.items() if not ok]
if missing:
    print(f"[ci] Betaflight smoke failed: {', '.join(missing)}", file=sys.stderr)
    print(log[-12000:], file=sys.stderr)
    raise SystemExit(1)
print(f"[ci] Betaflight mission passed: waypoints={done}/{total} max_alt={altitude:.1f}m "
      f"landing_distance={math.hypot(north, east):.2f}m final_alt={final_altitude:.2f}m")
PY
