#!/usr/bin/env bash
# Internal target implementation used by run_board_coverage.sh's documented
# lwIP, SD-card, and Modbus dispatchers.
set -euo pipefail

die() { printf 'Error: %s\n' "$*" >&2; exit 2; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

target=${1:?missing target}; shift
case $target in
    lwip) default_seconds=65; model_name=ethernet_model.so; template_name=lwip.toml.in; model_source_name=lwip/base_model.c ;;
    sdcard) default_seconds=45; model_name=sdmmc1_model.so; template_name=sdcard.toml.in; model_source_name=sdcard/base_model.c ;;
    modbus) default_seconds=25; model_name=usart2_model.so; template_name=modbus.toml.in; model_source_name=modbus/base_model.c ;;
    *) die "Unknown board target: $target" ;;
esac

board_port=
run_seconds=$default_seconds
work_dir=
while [[ $# -gt 0 ]]; do
    case $1 in
        --board-port) [[ $# -ge 2 ]] || die "--board-port requires a path"; board_port=$2; shift 2 ;;
        --duration) [[ $# -ge 2 ]] || die "--duration requires seconds"; run_seconds=$2; shift 2 ;;
        --work-dir) [[ $# -ge 2 ]] || die "--work-dir requires a path"; work_dir=$2; shift 2 ;;
        -h|--help)
            printf 'Usage: run_board_coverage.sh %s [--board-port PATH] [--duration SECONDS] [--work-dir PATH]\n' "$target"
            exit 0
            ;;
        *) die "Unknown option for $target: $1" ;;
    esac
done
[[ $run_seconds =~ ^[1-9][0-9]*$ ]] || die "--duration must be a positive integer"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fastdyn_root=$(cd -- "$script_dir/../.." && pwd)
workspace_root=$(cd -- "$fastdyn_root/.." && pwd)
bundle="$script_dir/reproducibility"
template="$bundle/configs/board/$template_name"
model_source="$bundle/models/$model_source_name"
source "$bundle/scripts/build_model.sh"

[[ -f $template ]] || die "Missing saved configuration template: $template"
[[ -f $model_source ]] || die "Missing saved model source: $model_source"
[[ -f $fastdyn_root/build/libfastdyn.so ]] || die "Missing FastDyn runtime; run ninja -C FastDyn/build."
[[ -f $fastdyn_root/boardrunner/boardrunner_sdk/build/libboardrunner_vio.so ]] || die "Missing BoardRunner VIO runtime; build boardrunner_sdk first."
for cmd in cc pkg-config timeout; do require_cmd "$cmd"; done
pkg-config --exists glib-2.0 || die "glib-2.0 development files are required."

if [[ -n ${FASTDYN_BOARDRUNNER:-} ]]; then
    runner=$FASTDYN_BOARDRUNNER
elif [[ -x $fastdyn_root/fastdyn-env/bin/boardrunner ]]; then
    runner=$fastdyn_root/fastdyn-env/bin/boardrunner
elif command -v boardrunner >/dev/null 2>&1; then
    runner=$(command -v boardrunner)
else
    die "Set FASTDYN_BOARDRUNNER or install/activate BoardRunner."
fi

detect_board_port() {
    local candidates=()
    local path
    shopt -s nullglob
    for path in /dev/serial/by-id/*; do candidates+=("$(readlink -f -- "$path")"); done
    shopt -u nullglob
    case ${#candidates[@]} in
        1) printf '%s\n' "${candidates[0]}" ;;
        0) die "No ST-LINK VCP found; pass --board-port explicitly." ;;
        *) die "Multiple ST-LINK VCPs found; pass --board-port explicitly." ;;
    esac
}

if [[ $target != modbus ]]; then
    [[ -n $board_port ]] || board_port=$(detect_board_port)
    [[ -c $board_port ]] || die "Board port is not a character device: $board_port"
fi

if [[ -z $work_dir ]]; then
    work_dir="$fastdyn_root/board-runs/${target}-coverage-$(date +%Y%m%d-%H%M%S)"
fi
[[ ! -e $work_dir ]] || die "Refusing to reuse an existing work directory: $work_dir"
mkdir -p "$work_dir"

if [[ -d $workspace_root/qemu/build ]]; then PATH="$workspace_root/qemu/build:$PATH"; fi
require_cmd qemu-system-arm

model="$work_dir/$model_name"
config="$work_dir/${target}_board.toml"
fastdyn_output="$work_dir/fastdyn-output"
bbl_file="$work_dir/bbl.txt"
runner_log="$work_dir/boardrunner.log"
build_repro_model "$model_source" "$model" "$fastdyn_root" "$workspace_root" normal

case $target in
    lwip)
        sed -e "s|__LWIP_MODEL_PATH__|$model|g" \
            -e "s|__LWIP_MAIN_RAM__|$work_dir/ram0|g" \
            -e "s|__LWIP_RAM1__|$work_dir/ram1|g" "$template" > "$config"
        ;;
    sdcard)
        sed -e "s|__SDCARD_MODEL_PATH__|$model|g" \
            -e "s|__SDCARD_MAIN_RAM__|$work_dir/ram0|g" \
            -e "s|__SDCARD_RAM1__|$work_dir/ram1|g" "$template" > "$config"
        ;;
    modbus)
        qemu="$workspace_root/qemu/build/qemu-system-arm"
        firmware="$fastdyn_root/boardrunner/boardrunner_examples/examples/Nucleo-F103RB/Modbus/firmware/STM32F103RB_Modbus.elf"
        monitor="$workspace_root/qemu/ws/monitor.elf"
        [[ -x $qemu && -f $firmware && -f $monitor ]] || die "Missing Modbus QEMU, firmware, or monitor ELF."
        sed -e "s|__MODBUS_MODEL_PATH__|$model|g" \
            -e "s|__MODBUS_MAIN_RAM__|$work_dir/ram0|g" \
            -e "s|__MODBUS_RAM1__|$work_dir/ram1|g" \
            -e "s|__QEMU_PATH__|$qemu|g" \
            -e "s|__MODBUS_QMP_SOCKET__|$work_dir/qmp.sock|g" \
            -e "s|__MODBUS_QEMU_LOG__|$work_dir/qemu.log|g" \
            -e "s|__FASTDYN_PLUGIN_PATH__|$fastdyn_root/build/libfastdyn.so|g" \
            -e "s|__MONITOR_ELF__|$monitor|g" \
            -e "s|__MODBUS_FIRMWARE__|$firmware|g" "$template" > "$config"
        ;;
esac

runner_pid=
traffic_pid=
socat_pid=
card_link="$fastdyn_root/sdcard.img"
cleanup() {
    [[ -n $traffic_pid ]] && kill "$traffic_pid" 2>/dev/null || true
    [[ -n $runner_pid ]] && kill "$runner_pid" 2>/dev/null || true
    [[ -n $socat_pid ]] && kill "$socat_pid" 2>/dev/null || true
    [[ $target == sdcard && -L $card_link ]] && rm -f -- "$card_link"
    [[ $target == modbus && -L /tmp/usart1_pty ]] && rm -f -- /tmp/usart1_pty
    [[ $target == modbus && -L /tmp/host_modbus ]] && rm -f -- /tmp/host_modbus
}
trap cleanup EXIT

if [[ $target == sdcard ]]; then
    require_cmd truncate
    require_cmd mkfs.fat
    [[ ! -e $card_link && ! -L $card_link ]] || die "$card_link already exists; remove it or use the existing run that owns it."
    truncate -s 64M "$work_dir/sdcard.img"
    mkfs.fat -F 32 "$work_dir/sdcard.img" > "$work_dir/mkfs.log"
    ln -s "$work_dir/sdcard.img" "$card_link"
fi

if [[ $target == modbus ]]; then
    require_cmd socat
    require_cmd python3
    [[ ! -e /tmp/usart1_pty && ! -L /tmp/usart1_pty ]] || die "/tmp/usart1_pty is already in use."
    [[ ! -e /tmp/host_modbus && ! -L /tmp/host_modbus ]] || die "/tmp/host_modbus is already in use."
    socat -d -d PTY,link=/tmp/usart1_pty,raw,echo=0 PTY,link=/tmp/host_modbus,raw,echo=0 > "$work_dir/socat.log" 2>&1 &
    socat_pid=$!
    for _ in 1 2 3 4 5; do [[ -e /tmp/usart1_pty && -e /tmp/host_modbus ]] && break; sleep 1; done
    [[ -e /tmp/usart1_pty && -e /tmp/host_modbus ]] || die "socat did not create the Modbus PTY pair."
fi

printf '%s run directory: %s\n' "$target" "$work_dir"
[[ $target == modbus ]] || printf '%s board port: %s\n' "$target" "$board_port"
printf 'The launcher owns a %ss wall-clock budget; its final SIGINT is expected.\n' "$run_seconds"
(
    cd "$fastdyn_root"
    FASTDYN_COVERAGE=1 FASTDYN_FUZZING=0 FASTDYN_SNAPSHOT_SEGMENTS=all \
        FASTDYN_BBL_FILE="$bbl_file" FASTDYN_WORK_DIR="$fastdyn_output/work" \
        timeout --signal=INT --kill-after=10s "${run_seconds}s" "$runner" run -c "$config" -o "$fastdyn_output"
) > "$runner_log" 2>&1 &
runner_pid=$!

case $target in
    lwip)
        require_cmd ip; require_cmd ping; require_cmd nc; require_cmd curl
        ip link show tap0 >/dev/null 2>&1 || die "tap0 is not configured; follow the README setup commands."
        (
            sleep 10
            ping -c 1 -W 2 192.168.8.2 || true
            printf 'GET /index.html HTTP/1.0\r\nHost: 192.168.8.2\r\n\r\n' | nc -w 3 192.168.8.2 80 || true
            curl --connect-timeout 3 --max-time 5 http://192.168.8.2/STM32F7xxLED.html >/dev/null || true
            curl --connect-timeout 3 --max-time 5 http://192.168.8.2/missing-page >/dev/null || true
        ) > "$work_dir/traffic.log" 2>&1 &
        traffic_pid=$!
        ;;
    modbus)
        (
            sleep 6
            python3 "$fastdyn_root/boardrunner/boardrunner_examples/examples/Nucleo-F103RB/Modbus/host/modbus_bridge.py" \
                --serial /tmp/host_modbus --profile core --timeout 2
        ) > "$work_dir/modbus-bridge.log" 2>&1 &
        traffic_pid=$!
        ;;
esac

set +e
wait "$runner_pid"; run_status=$?
set -e
runner_pid=
[[ -n $traffic_pid ]] && wait "$traffic_pid" 2>/dev/null || true
traffic_pid=

case $run_status in
    0)
        final_status=0
        printf '%s run ended before its wall-clock budget.\n' "$target"
        ;;
    124)
        final_status=0
        printf '%s run reached the launcher-owned %ss wall-clock budget (expected).\n' "$target" "$run_seconds"
        ;;
    *)
        final_status=$run_status
        printf '%s run failed with status %s; inspect %s.\n' "$target" "$run_status" "$runner_log" >&2
        ;;
esac
if [[ $target == sdcard ]] && strings -a "$work_dir/sdcard.img" | grep -q 'STM32'; then
    printf 'SD-card image contains the demo output.\n'
fi
if [[ -f $bbl_file ]]; then
    printf '%s basic blocks: %s (%s)\n' "$target" "$(wc -l < "$bbl_file")" "$bbl_file"
else
    printf '%s produced no BBL report: %s\n' "$target" "$bbl_file" >&2
fi
exit "$final_status"
