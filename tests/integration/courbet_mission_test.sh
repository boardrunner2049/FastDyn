#!/usr/bin/env bash
set -Eeuo pipefail

config="${FASTDYN_COURBET_CONFIG:-configs/copter462.toml}"
log_file="${FASTDYN_COURBET_LOG:-out/ci/courbet_mission.log}"
timeout_sec="${FASTDYN_COURBET_TIMEOUT_SEC:-420}"
min_alt_m="${FASTDYN_COURBET_MIN_ALT_M:-10.0}"
min_item="${FASTDYN_COURBET_MIN_ITEM:-5}"
require_completion="${FASTDYN_COURBET_REQUIRE_COMPLETION:-true}"
work_dir="${FASTDYN_COURBET_WORK_DIR:-fastdyn_work}"

mkdir -p "$(dirname "$log_file")"
# Keep binary telemetry beside the console log, independent of MAVProxy's cwd.
export FASTDYN_MAVLINK_LOG="$(realpath -m "${log_file%.log}.tlog")"
: >"$log_file"
phase_file="${log_file}.phases"
: >"$phase_file"
start_seconds=$SECONDS

log_ci() {
  printf '[ci] +%ss %s\n' "$((SECONDS - start_seconds))" "$*" | tee -a "$log_file"
}

mark_phase() {
  local key="$1"
  local message="$2"
  if ! grep -qxF "$key" "$phase_file"; then
    printf '%s\n' "$key" >>"$phase_file"
    log_ci "$message"
  fi
}

if [[ -f fastdyn-env/bin/activate ]]; then
  # shellcheck disable=SC1091
  source fastdyn-env/bin/activate
fi

run_pid=""
cleanup() {
  if [[ -n "$run_pid" ]] && kill -0 "$run_pid" 2>/dev/null; then
    kill -INT -- "-$run_pid" 2>/dev/null || kill -INT "$run_pid" 2>/dev/null || true
    sleep 3
    kill -TERM -- "-$run_pid" 2>/dev/null || kill -TERM "$run_pid" 2>/dev/null || true
    wait "$run_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

log_ci "starting Courbet mission integration test"
setsid fastdyn run -c "$config" -o "$work_dir" >>"$log_file" 2>&1 &
run_pid=$!

mission_check() {
  python3 - "$log_file" "$min_alt_m" "$min_item" "$require_completion" <<'PY'
from __future__ import annotations

from pathlib import Path
import re
import sys

log_path = Path(sys.argv[1])
min_alt_m = float(sys.argv[2])
min_item = int(sys.argv[3])
require_completion = sys.argv[4].strip().lower() not in {
    "", "0", "false", "off", "no", "none"
}
text = log_path.read_text(errors="replace")

max_alt = max([float(x) for x in re.findall(r"\[mission\] rel_alt=([-+0-9.]+)m", text)] or [0.0])
max_item = max([int(x) for x in re.findall(r"\[mission\] current item (\d+)/", text)] or [-1])
completed = (
    "[mission] final landing confirmed near ground" in text
    or "[mission] final waypoint reached" in text
    or "[timing] mission:mission.completed" in text
)

checks = [
    "FMU ready:" in text,
    "FMU backend loaded:" in text,
    "FMU backend advances synchronously from QEMU timer ticks" in text,
    "Periodic IRQ 66 every 1000000 ns" in text,
    "MAVCesium web viewer:" in text,
    "[mission] upload accepted" in text,
    "[mission] armed" in text,
    max_alt >= min_alt_m,
    max_item >= min_item,
]
if require_completion:
    checks.append(completed)

if all(checks):
    print(
        "[ci] flight observed: "
        f"max_rel_alt={max_alt:.1f}m max_mission_item={max_item} "
        f"completed={completed}"
    )
    raise SystemExit(0)

missing = []
labels = [
    "fmu_ready",
    "fmu_backend_loaded",
    "lockstep_clock",
    "timer_irq_period",
    "mavcesium",
    "mission_upload",
    "armed",
    f"max_alt>={min_alt_m:g}",
    f"max_item>={min_item}",
]
if require_completion:
    labels.append("mission_completed")
for label, ok in zip(labels, checks):
    if not ok:
        missing.append(label)
print(
    "[ci] waiting: "
    f"max_rel_alt={max_alt:.1f}m max_mission_item={max_item} "
    f"completed={completed} missing={','.join(missing)}"
)
raise SystemExit(1)
PY
}

deadline=$((SECONDS + timeout_sec))
while (( SECONDS < deadline )); do
  if mission_check | tee -a "$log_file"; then
    log_ci "Courbet mission integration test passed"
    exit 0
  fi

  grep -q "FMU backend loaded:" "$log_file" && mark_phase "fmu_loaded" "FMU backend loaded"
  grep -q "FMU backend advances synchronously from QEMU timer ticks" "$log_file" && mark_phase "lockstep_clock" "FMU/QEMU lockstep clock active"
  grep -q "MAVCesium web viewer:" "$log_file" && mark_phase "mavcesium_ready" "MAVCesium web viewer ready"
  grep -q "\\[mission\\] heartbeat from" "$log_file" && mark_phase "heartbeat" "ArduPilot heartbeat received"
  grep -q "\\[mission\\] ArduPilot ready with GPS and EKF" "$log_file" && mark_phase "ready" "ArduPilot ready with GPS/EKF"
  grep -q "\\[mission\\] upload accepted" "$log_file" && mark_phase "upload" "mission upload accepted"
  grep -q "\\[mission\\] armed" "$log_file" && mark_phase "armed" "vehicle armed"
  grep -q "\\[mission\\] final landing confirmed near ground" "$log_file" && mark_phase "landed" "final landing confirmed"

  if ! kill -0 "$run_pid" 2>/dev/null; then
    wait "$run_pid" || true
    if mission_check | tee -a "$log_file"; then
      log_ci "Courbet mission integration test passed after fastdyn exit"
      exit 0
    fi
    log_ci "fastdyn exited before mission completion criteria passed" >&2
    tail -200 "$log_file" >&2
    exit 1
  fi

  sleep 2
done

log_ci "timed out waiting for mission completion criteria" >&2
tail -200 "$log_file" >&2
exit 1
