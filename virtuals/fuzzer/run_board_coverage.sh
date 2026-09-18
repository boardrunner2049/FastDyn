#!/usr/bin/env bash
# Run hardware-assisted, non-fuzzing BoardRunner coverage demonstrations.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: run_board_coverage.sh {mqtt|lwip|sdcard|modbus} [options]

Options:
  --board-port PATH     ST-LINK VCP to associate with the attached board.
                        Auto-detected when omitted.
  --duration SECONDS    Run budget. MQTT: minimum 15 seconds (default: 25).
                        lwIP: minimum 45 seconds (default: 65).
                        sdcard: minimum 30 seconds (default: 45).
                        Modbus: minimum 20 seconds (default: 25).
  --work-dir PATH       New directory for logs, model, config, and bbl.txt.
  --broker-host HOST    Mosquitto host (default: 127.0.0.1).
  --broker-port PORT    Mosquitto TCP port (default: 1883).
  -h, --help            Show this help.

For mqtt, the elder model communicates through /tmp/usart1_pty. --board-port
identifies the attached board, but its VCP is not the elder-mode MQTT endpoint.

For lwip, the script uses the supplied TAP-capable Elder Ethernet model on
tap0 (host 192.168.8.1, target 192.168.8.2), with ST-LINK passthrough for all
other peripherals. It performs ICMP, TCP, two normal HTTP GETs, and a 404 GET;
it deliberately does not call the LED-changing CGI endpoint.

For sdcard, the script builds a fresh isolated FAT32 image and uses the saved
SDMMC1 Elder model. All non-SDMMC1 peripherals use ST-LINK passthrough.

For modbus, no board is required. The script uses the saved USART2 Elder
model and a PTY pair, then runs the documented Modbus host bridge's `core`
profile (FC01/02/03/04/05/06/0F). It reports the expected ~521 BBL result.
EOF
}

die() { printf 'Error: %s\n' "$*" >&2; exit 2; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

check_seconds() {
    [[ $1 =~ ^[1-9][0-9]*$ ]] || die "--duration must be a positive integer, got: $1"
    (( $1 >= 15 )) || die "--duration must be at least 15 seconds for the MQTT command sequence."
}

detect_board_port() {
    local path resolved
    local -a candidates=()
    local -A seen=()

    shopt -s nullglob
    for path in /dev/serial/by-id/*STMicroelectronics*STLINK* /dev/serial/by-id/*STMicroelectronics*ST-LINK*; do
        [[ -e $path ]] || continue
        resolved=$(readlink -f -- "$path")
        [[ -n ${seen[$resolved]:-} ]] && continue
        seen[$resolved]=1
        candidates+=("$resolved")
    done
    if (( ${#candidates[@]} == 0 )); then
        for path in /dev/ttyACM*; do
            [[ -e $path ]] && candidates+=("$path")
        done
    fi
    shopt -u nullglob

    case ${#candidates[@]} in
        1) printf '%s\n' "${candidates[0]}" ;;
        0) die "No ST-LINK VCP was detected; pass --board-port /dev/ttyACM<N>." ;;
        *)
            printf 'Multiple possible board ports were found:\n  %s\n' "${candidates[*]}" >&2
            die "Pass --board-port explicitly."
            ;;
    esac
}

report_coverage() {
    if [[ -f $1 ]]; then
        printf 'MQTT basic blocks: %s (%s)\n' "$(wc -l < "$1")" "$1"
    else
        printf 'MQTT produced no BBL report: %s\n' "$1" >&2
    fi
}

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
    usage
    exit 0
fi

target=${1:-}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
case $target in
    lwip|sdcard|modbus)
        shift
        exec "$script_dir/run_board_target.sh" "$target" "$@"
        ;;
    mqtt) ;;
    *) usage >&2; exit 2 ;;
esac
shift

board_port=
run_seconds=25
work_dir=
broker_host=127.0.0.1
broker_port=1883
while [[ $# -gt 0 ]]; do
    case $1 in
        --board-port) [[ $# -ge 2 ]] || die "--board-port requires a path"; board_port=$2; shift 2 ;;
        --duration) [[ $# -ge 2 ]] || die "--duration requires seconds"; run_seconds=$2; shift 2 ;;
        --work-dir) [[ $# -ge 2 ]] || die "--work-dir requires a path"; work_dir=$2; shift 2 ;;
        --broker-host) [[ $# -ge 2 ]] || die "--broker-host requires a host"; broker_host=$2; shift 2 ;;
        --broker-port) [[ $# -ge 2 ]] || die "--broker-port requires a port"; broker_port=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

check_seconds "$run_seconds"
[[ $broker_port =~ ^[1-9][0-9]*$ ]] && (( broker_port <= 65535 )) || die "--broker-port must be in 1..65535"

fastdyn_root=$(cd -- "$script_dir/../.." && pwd)
workspace_root=$(cd -- "$fastdyn_root/.." && pwd)
bundle="$script_dir/reproducibility"
source "$bundle/scripts/build_model.sh"
template="$bundle/configs/board/mqtt.toml.in"
model_source="$bundle/models/mqtt/base_model.c"

[[ -f $template ]] || die "Missing MQTT configuration template: $template"
[[ -f $model_source ]] || die "Missing saved MQTT USART3 model: $model_source"
[[ -f $fastdyn_root/build/libfastdyn.so ]] || die "Missing FastDyn build: $fastdyn_root/build/libfastdyn.so"
[[ -f $fastdyn_root/boardrunner/boardrunner_sdk/build/libboardrunner_vio.so ]] || die "Missing BoardRunner VIO runtime; build boardrunner_sdk first."
for cmd in cc pkg-config mosquitto mosquitto_pub mosquitto_sub socat timeout; do require_cmd "$cmd"; done
pkg-config --exists glib-2.0 || die "glib-2.0 development files are required to build the saved USART3 model."

if [[ -z $board_port ]]; then board_port=$(detect_board_port); fi
[[ -c $board_port ]] || die "Board port is not a character device: $board_port"
# MQTT elder mode uses the model PTY, not this VCP.  Therefore VCP visibility
# identifies the board without requiring serial-group access; the ST-LINK
# backend independently opens the debug probe for hardware passthrough.

if [[ -z $work_dir ]]; then work_dir="$fastdyn_root/board-runs/mqtt-coverage-$(date +%Y%m%d-%H%M%S)"; fi
[[ ! -e $work_dir ]] || die "Refusing to reuse an existing work directory: $work_dir"
mkdir -p "$work_dir"

if [[ -d $workspace_root/qemu/build ]]; then PATH="$workspace_root/qemu/build:$PATH"; fi
require_cmd qemu-system-arm
if [[ -n ${FASTDYN_BOARDRUNNER:-} ]]; then
    runner=$FASTDYN_BOARDRUNNER
elif [[ -x $fastdyn_root/fastdyn-env/bin/boardrunner ]]; then
    runner=$fastdyn_root/fastdyn-env/bin/boardrunner
elif command -v boardrunner >/dev/null 2>&1; then
    runner=$(command -v boardrunner)
else
    die "Set FASTDYN_BOARDRUNNER or install/activate BoardRunner."
fi

model="$work_dir/usart3_model.so"
config="$work_dir/mqtt_board.toml"
fastdyn_output="$work_dir/fastdyn-output"
bbl_file="$work_dir/bbl.txt"
pty_link=/tmp/usart1_pty

build_repro_model "$model_source" "$model" "$fastdyn_root" "$workspace_root" normal
sed "s|__MQTT_MODEL_PATH__|$model|" "$template" > "$config"
printf '%s\n' "$board_port" > "$work_dir/board-port.txt"

[[ ! -e $pty_link && ! -L $pty_link ]] || die "$pty_link already exists; stop the process using it before starting this run."
broker_pid=
socat_pid=
subscriber_pid=
publisher_pid=
cleanup() {
    local pid
    for pid in "$publisher_pid" "$subscriber_pid" "$socat_pid" "$broker_pid"; do
        [[ -n $pid ]] && kill "$pid" 2>/dev/null || true
    done
    [[ -L $pty_link ]] && rm -f -- "$pty_link"
}
trap cleanup EXIT

if ! mosquitto_pub -h "$broker_host" -p "$broker_port" -t fastdyn/probe -m probe >/dev/null 2>&1; then
    mosquitto -p "$broker_port" -v > "$work_dir/mosquitto.log" 2>&1 &
    broker_pid=$!
    sleep 1
    kill -0 "$broker_pid" 2>/dev/null || die "Could not start Mosquitto; see $work_dir/mosquitto.log"
fi

socat -d -d PTY,link="$pty_link",raw,echo=0 TCP:"$broker_host":"$broker_port" > "$work_dir/socat.log" 2>&1 &
socat_pid=$!
for _ in 1 2 3 4 5; do [[ -L $pty_link ]] && break; sleep 1; done
[[ -L $pty_link ]] || die "socat did not create $pty_link; see $work_dir/socat.log"

timeout "$((run_seconds + 5))s" mosquitto_sub -h "$broker_host" -p "$broker_port" -v -t stm32/sensor > "$work_dir/sensor.log" 2> "$work_dir/subscriber.log" &
subscriber_pid=$!
(
    sleep 6
    mosquitto_pub -h "$broker_host" -p "$broker_port" -t stm32/cmd -m on
    sleep 2
    mosquitto_pub -h "$broker_host" -p "$broker_port" -t stm32/cmd -m off
    sleep 2
    mosquitto_pub -h "$broker_host" -p "$broker_port" -t stm32/cmd -m on
    sleep 2
    mosquitto_pub -h "$broker_host" -p "$broker_port" -t stm32/cmd -m off
) > "$work_dir/publish.log" 2>&1 &
publisher_pid=$!

printf 'MQTT board port: %s\nMQTT run directory: %s\n' "$board_port" "$work_dir"
printf 'The launcher owns a %ss wall-clock budget; its final SIGINT is expected.\n' "$run_seconds"
set +e
cd "$fastdyn_root"
FASTDYN_COVERAGE=1 FASTDYN_FUZZING=0 FASTDYN_SNAPSHOT_SEGMENTS=all \
    FASTDYN_BBL_FILE="$bbl_file" FASTDYN_WORK_DIR="$fastdyn_output/work" \
    timeout --signal=INT --kill-after=10s "${run_seconds}s" "$runner" run -c "$config" -o "$fastdyn_output" > "$work_dir/boardrunner.log" 2>&1
run_status=$?
set -e
wait "$publisher_pid" 2>/dev/null || true
publisher_pid=

case $run_status in
    0) printf 'MQTT board run ended before its wall-clock budget.\n' ;;
    124) printf 'MQTT board run reached the launcher-owned %ss wall-clock budget (expected).\n' "$run_seconds" ;;
    *)
        printf 'MQTT board run failed with status %s; inspect %s/boardrunner.log.\n' "$run_status" "$work_dir" >&2
        report_coverage "$bbl_file"
        exit "$run_status"
        ;;
esac
sensor_count=0
[[ -f $work_dir/sensor.log ]] && sensor_count=$(wc -l < "$work_dir/sensor.log")
printf 'MQTT sensor messages: %s (%s/sensor.log)\n' "$sensor_count" "$work_dir"
report_coverage "$bbl_file"
