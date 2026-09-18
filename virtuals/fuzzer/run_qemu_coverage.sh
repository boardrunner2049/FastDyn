#!/usr/bin/env bash
# TOML-driven, cold QEMU-only basic-block baseline for four FastDyn demos.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: run_qemu_coverage.sh [all|mqtt|lwip|sdcard|modbus] [options]

Run the firmware using a minimal generated TOML. With no target argument,
`all` runs MQTT, lwIP, SD-card, then Modbus.

Options:
  --duration SECONDS  Per-target wall-clock ceiling (default: 5; minimum: 1).
  --work-dir PATH     New directory for TOMLs, logs, bbl.txt files, and summary.tsv.
  -h, --help          Show this help.

The TOML has no peripheral model or handler definitions. FastDyn is present
only to collect basic-block coverage.
EOF
}

die() { printf 'Error: %s\n' "$*" >&2; exit 2; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

run_seconds=5
work_dir=
selection=all
target_was_selected=0
while [[ $# -gt 0 ]]; do
    case $1 in
        all|mqtt|lwip|sdcard|modbus)
            (( target_was_selected == 0 )) || die "Only one target may be selected."
            selection=$1
            target_was_selected=1
            shift
            ;;
        --duration)
            [[ $# -ge 2 ]] || die "--duration requires seconds"
            run_seconds=$2
            shift 2
            ;;
        --work-dir)
            [[ $# -ge 2 ]] || die "--work-dir requires a path"
            work_dir=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *) die "Unknown option: $1" ;;
    esac
done
[[ $run_seconds =~ ^[1-9][0-9]*$ ]] || die "--duration must be a positive integer, got: $run_seconds"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fastdyn_root=$(cd -- "$script_dir/../.." && pwd)
workspace_root=$(cd -- "$fastdyn_root/.." && pwd)
template="$script_dir/reproducibility/configs/board/qemu_only.toml.in"
qemu="$workspace_root/qemu/build/qemu-system-arm"
plugin="$fastdyn_root/build/libfastdyn.so"

[[ -x $qemu ]] || die "Missing patched QEMU binary: $qemu"
[[ -f $plugin ]] || die "Missing FastDyn coverage plugin: $plugin"
[[ -f $template ]] || die "Missing QEMU-only TOML template: $template"
for cmd in timeout wc sed; do require_cmd "$cmd"; done

if [[ -n ${FASTDYN_BOARDRUNNER:-} ]]; then
    runner=$FASTDYN_BOARDRUNNER
elif [[ -x $fastdyn_root/fastdyn-env/bin/boardrunner ]]; then
    runner=$fastdyn_root/fastdyn-env/bin/boardrunner
elif command -v boardrunner >/dev/null 2>&1; then
    runner=$(command -v boardrunner)
else
    die "Set FASTDYN_BOARDRUNNER or install/activate BoardRunner."
fi

if [[ -z $work_dir ]]; then
    work_dir="$fastdyn_root/qemu-runs/qemu-coverage-$(date +%Y%m%d-%H%M%S)"
fi
[[ ! -e $work_dir ]] || die "Refusing to reuse an existing work directory: $work_dir"
mkdir -p "$work_dir"
summary="$work_dir/summary.tsv"
printf 'target\tbasic_blocks\tlauncher_status\tbbl_file\ttoml\n' > "$summary"

make_config() {
    local target_work=$1 firmware=$2 cpu=$3 monitor_port=$4
    local config="$target_work/qemu-only.toml"
    sed \
        -e "s|__MAIN_RAM__|$target_work/ram0|" \
        -e "s|__RAM1__|$target_work/ram1|" \
        -e "s|__QEMU_PATH__|$qemu|" \
        -e "s|__MONITOR_PORT__|$monitor_port|" \
        -e "s|__QMP_SOCKET__|$target_work/qmp.sock|" \
        -e "s|__QEMU_LOG__|$target_work/qemu.log|" \
        -e "s|__CPU__|$cpu|" \
        -e "s|__FASTDYN_PLUGIN_PATH__|$plugin|" \
        -e "s|__FIRMWARE__|$firmware|" \
        "$template" > "$config"
    printf '%s\n' "$config"
}

run_target() {
    local target=$1 firmware=$2 cpu=$3 monitor_port=$4
    local target_work="$work_dir/$target"
    local bbl_file="$target_work/bbl.txt"
    local runner_log="$target_work/boardrunner.log"
    local fastdyn_output="$target_work/fastdyn-output"
    local config run_status blocks

    [[ -f $firmware ]] || die "Missing $target firmware: $firmware"
    mkdir -p "$target_work"
    config=$(make_config "$target_work" "$firmware" "$cpu" "$monitor_port")

    printf '\n[%s] Starting TOML-configured QEMU-only baseline (%ss).\n' "$target" "$run_seconds"
    set +e
    (
        cd "$fastdyn_root"
        FASTDYN_COVERAGE=1 FASTDYN_FUZZING=0 FASTDYN_SNAPSHOT_SEGMENTS=all \
        FASTDYN_BBL_FILE="$bbl_file" \
        timeout --signal=INT --kill-after=5s "${run_seconds}s" "$runner" run \
            -c "$config" -o "$fastdyn_output"
    ) > "$runner_log" 2>&1
    run_status=$?
    set -e

    blocks=0
    [[ -f $bbl_file ]] && blocks=$(wc -l < "$bbl_file")
    case $run_status in
        0) printf '[%s] QEMU exited before the wall-clock budget.\n' "$target" ;;
        124) printf '[%s] Launcher-owned %ss cutoff reached (expected); QEMU received SIGINT.\n' "$target" "$run_seconds" ;;
        *) printf '[%s] Launcher exited with status %s; see %s.\n' "$target" "$run_status" "$runner_log" >&2 ;;
    esac
    printf '[%s] Basic blocks: %s (%s)\n' "$target" "$blocks" "$bbl_file"
    printf '%s\t%s\t%s\t%s\t%s\n' "$target" "$blocks" "$run_status" "$bbl_file" "$config" >> "$summary"

    # Keep running the other cold baselines even if one image cannot start.
    [[ $run_status == 0 || $run_status == 124 ]]
}

mqtt_firmware="$fastdyn_root/boardrunner/boardrunner_examples/examples/STM32H753zi/MQTT/firmware/mqtt_h753.elf"
lwip_firmware="$fastdyn_root/boardrunner/boardrunner_examples/examples/STM32F769i-eval/LWIP/firmwares/STM32F769I_EVAL.elf"
sdcard_firmware="$fastdyn_root/boardrunner/boardrunner_examples/examples/STM32F769i-eval/SD_Card/Firmwares/STM32F769I_EVAL.elf"
modbus_firmware="$fastdyn_root/boardrunner/boardrunner_examples/examples/Nucleo-F103RB/Modbus/firmware/STM32F103RB_Modbus.elf"

printf 'QEMU-only result directory: %s\n' "$work_dir"
printf 'Each generated TOML contains no peripheral model or handler definitions.\n'
overall_status=0
case $selection in
    all)
        run_target mqtt "$mqtt_firmware" cortex-m7 15600 || overall_status=1
        run_target lwip "$lwip_firmware" cortex-m7 15601 || overall_status=1
        run_target sdcard "$sdcard_firmware" cortex-m7 15602 || overall_status=1
        run_target modbus "$modbus_firmware" cortex-m3 15603 || overall_status=1
        ;;
    mqtt) run_target mqtt "$mqtt_firmware" cortex-m7 15600 || overall_status=1 ;;
    lwip) run_target lwip "$lwip_firmware" cortex-m7 15601 || overall_status=1 ;;
    sdcard) run_target sdcard "$sdcard_firmware" cortex-m7 15602 || overall_status=1 ;;
    modbus) run_target modbus "$modbus_firmware" cortex-m3 15603 || overall_status=1 ;;
esac

printf '\nQEMU-only coverage summary: %s\n' "$summary"
column -t -s $'\t' "$summary" 2>/dev/null || sed 's/\t/  /g' "$summary"
exit "$overall_status"
