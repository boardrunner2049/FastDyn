#!/usr/bin/env bash
# Start one of the stateful FastDyn/AFLNet campaigns with an isolated output
# directory. These campaigns collect basic-block coverage only.
set -euo pipefail

usage() {
    printf 'Usage: %s {lwip|mqtt|modbus} [new-work-directory]\n' "$0" >&2
}

if [[ $# -eq 1 && ( "$1" == "-h" || "$1" == "--help" ) ]]; then
    usage
    exit 0
fi
if [[ $# -lt 1 || $# -gt 2 ]]; then
    usage
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fastdyn_root=$(cd -- "$script_dir/../.." && pwd)
workspace_root=$(cd -- "$fastdyn_root/.." && pwd)
bundle="$script_dir/reproducibility"
seed_root="$bundle/seeds"
source "$bundle/scripts/build_model.sh"
target=$1

case "$target" in
    lwip)
        protocol=ETHERNET
        config_template="$bundle/configs/aflnet/lwip.toml"
        model_source="$bundle/models/lwip/fuzzing_model.c"
        corpus="$seed_root/lwip/baseline"
        lwip_seed_root="$seed_root/lwip"
        model_name=lwip_fuzz
        ;;
    mqtt)
        protocol=MQTT_CLIENT
        config_template="$bundle/configs/aflnet/mqtt.toml"
        model_source="$bundle/models/mqtt/fuzzing_model.c"
        corpus="$seed_root/mqtt"
        snapshot_ranges="$bundle/configs/aflnet/mqtt_snapshot_ranges.txt"
        model_name=mqtt_fuzz
        default_run_seconds=25
        ;;
    modbus)
        protocol=MODBUS
        config_template="$bundle/configs/aflnet/modbus.toml"
        model_source="$bundle/models/modbus/fuzzing_model.c"
        corpus="$seed_root/modbus"
        model_name=modbus_fuzz
        default_run_seconds=100
        ;;
    *)
        usage
        exit 2
        ;;
esac

if [[ -n "${FASTDYN_AFLNET_CORPUS:-}" ]]; then
    corpus=$FASTDYN_AFLNET_CORPUS
fi

if [[ ! -d "$corpus" ]]; then
    printf 'Seed corpus directory does not exist: %s\n' "$corpus" >&2
    exit 2
fi

work_dir=${2:-"$fastdyn_root/aflnet-runs/${target}-coverage-$(date +%Y%m%d-%H%M%S)"}
if [[ -e "$work_dir" ]]; then
    printf 'Refusing to reuse existing campaign directory: %s\n' "$work_dir" >&2
    exit 2
fi
stage_dir="${work_dir}.inputs"
if [[ -e "$stage_dir" ]]; then
    printf 'Refusing to reuse existing campaign staging directory: %s\n' "$stage_dir" >&2
    exit 2
fi

if [[ ! -f "$config_template" || ! -f "$model_source" ]]; then
    printf 'Missing bundled %s AFLNet config or model source.\n' "$target" >&2
    exit 2
fi

if [[ -d "$workspace_root/qemu/build" ]]; then
    PATH="$workspace_root/qemu/build:$PATH"
fi
if [[ -d "$workspace_root/aflnet" ]]; then
    PATH="$workspace_root/aflnet:$PATH"
fi
if ! command -v qemu-system-arm >/dev/null || ! command -v afl-fuzz >/dev/null; then
    printf 'qemu-system-arm and afl-fuzz must be in PATH (or in sibling qemu/build and aflnet directories).\n' >&2
    exit 2
fi

if [[ -n "${FASTDYN_BOARDRUNNER:-}" ]]; then
    runner=$FASTDYN_BOARDRUNNER
elif [[ -x "$fastdyn_root/fastdyn-env/bin/boardrunner" ]]; then
    runner="$fastdyn_root/fastdyn-env/bin/boardrunner"
elif command -v boardrunner >/dev/null; then
    runner=$(command -v boardrunner)
else
    printf 'Set FASTDYN_BOARDRUNNER or activate the BoardRunner environment.\n' >&2
    exit 2
fi

# Each run owns its model artifact.  BoardRunner clears its requested output
# directory before launch, so keep the generated config/model in a sibling
# staging directory rather than relying on a target-specific CMake artifact.
mkdir -p "$stage_dir/models"
built_model="$stage_dir/models/${model_name}.so"
build_repro_model "$model_source" "$built_model" "$fastdyn_root" \
    "$workspace_root" aflnet
config="$stage_dir/${target}_aflnet.toml"
sed -E "s|^([[:space:]]*scroll[[:space:]]*=[[:space:]]*).*$|\\1\"$built_model\"|" \
    "$config_template" > "$config"

export PATH
export LD_LIBRARY_PATH="$fastdyn_root/build-aflnet:$fastdyn_root/device_models/postmartem/verifier:$fastdyn_root/virtuals/fuzzer/fastdyn_fuzz_lib/target/release${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export FASTDYN_AFLNET_PROTOCOL=$protocol
export FASTDYN_AFLNET_TIMEOUT_MS="${FASTDYN_AFLNET_TIMEOUT_MS:-5000}"
export FASTDYN_AFLNET_NET_TIMEOUT_MS="${FASTDYN_AFLNET_NET_TIMEOUT_MS:-100000000}"
export FASTDYN_AFLNET_WAIT_TIMEOUT_MS="${FASTDYN_AFLNET_WAIT_TIMEOUT_MS:-5000}"
# Do not let BoardRunner's first-run snapshot-range prompt consume a fuzzing
# phase.  All writable segments preserve the full firmware lifecycle; callers
# may explicitly override this with an index expression such as "0,2-3".
export FASTDYN_SNAPSHOT_SEGMENTS="${FASTDYN_SNAPSHOT_SEGMENTS:-all}"

# Modbus's RTU T3.5 timer is driven through the snapshot-aware HAL_GetTick
# hook.  A small deterministic step visits both the pending and expiry paths
# without allowing a timer value to leak from one AFLNet testcase to another.
if [[ $target == modbus ]]; then
    export FASTDYN_FUZZ_TIME_STEP_MS="${FASTDYN_FUZZ_TIME_STEP_MS:-1}"
fi

# This is deliberately a BBL-only campaign. The AFLNet backend has the same
# defaults, but exporting them keeps the launcher self-contained on hosts with
# a system crash reporter or dynamic CPU governor.
export AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES="${AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES:-1}"
export AFL_SKIP_CPUFREQ="${AFL_SKIP_CPUFREQ:-1}"

check_seconds() {
    local value=$1
    local name=$2
    if [[ ! $value =~ ^[1-9][0-9]*$ ]]; then
        printf "%s must be a positive number of seconds, got: %s\n" "$name" "$value" >&2
        exit 2
    fi
}

run_lwip_phase() {
    local label=$1
    local input_dir=$2
    local seconds=$3
    local tick_step=$4
    local phase_dir="$work_dir/phases/$label"
    local phase_status

    printf "Starting lwIP phase %s for %ss\n" "$label" "$seconds"
    set +e
    timeout --signal=INT --kill-after=15s "${seconds}s" env \
        FASTDYN_AFLNET_IN_DIR="$input_dir" \
        FASTDYN_AFLNET_OUT_DIR="$phase_dir/aflnet-out" \
        FASTDYN_BBL_FILE="$work_dir/bbl.txt" \
        FASTDYN_WORK_DIR="$phase_dir/fastdyn-work" \
        FASTDYN_FUZZ_TIME_STEP_MS="$tick_step" \
        "$runner" run -c "$config" -o "$phase_dir"
    phase_status=$?
    set -e

    case "$phase_status" in
        0|124|130|137)
            ;;
        *)
            printf "lwIP phase %s failed with status %s\n" "$label" "$phase_status" >&2
            exit "$phase_status"
            ;;
    esac
}

run_lwip_campaign() {
    local baseline_seconds="${FASTDYN_AFLNET_BASELINE_SECONDS:-55}"
    local main_seconds="${FASTDYN_AFLNET_MAIN_SECONDS:-100}"
    local phase_seconds="${FASTDYN_AFLNET_PHASE_SECONDS:-60}"
    local bbl_count

    check_seconds "$baseline_seconds" FASTDYN_AFLNET_BASELINE_SECONDS
    check_seconds "$main_seconds" FASTDYN_AFLNET_MAIN_SECONDS
    check_seconds "$phase_seconds" FASTDYN_AFLNET_PHASE_SECONDS

    local phase

    mkdir -p "$work_dir/corpora" "$work_dir/phases"
    cp -a "$corpus/." "$work_dir/corpora/baseline"
    for phase in all cgi rto ack reassembly; do
        if [[ ! -d "$lwip_seed_root/$phase" ]]; then
            printf 'Missing saved lwIP %s corpus: %s\n' \
                "$phase" "$lwip_seed_root/$phase" >&2
            exit 2
        fi
        cp -a "$lwip_seed_root/$phase/." "$work_dir/corpora/$phase"
    done

    # Complementary clean starts exercise setup, normal traffic, the CGI
    # handler, timer/retransmit code, response acknowledgement, and pbuf
    # reassembly cleanup. Every process reloads and appends this one BBL set.
    run_lwip_phase baseline "$work_dir/corpora/baseline" "$baseline_seconds" 1000
    run_lwip_phase all "$work_dir/corpora/all" "$main_seconds" 1000
    run_lwip_phase cgi "$work_dir/corpora/cgi" "$phase_seconds" 1000
    run_lwip_phase rto "$work_dir/corpora/rto" "$phase_seconds" 30000
    run_lwip_phase ack "$work_dir/corpora/ack" "$phase_seconds" 1000
    run_lwip_phase reassembly "$work_dir/corpora/reassembly" "$phase_seconds" 1000

    if [[ ! -f "$work_dir/bbl.txt" ]]; then
        printf 'lwIP produced no BBL report. A phase did not reach QEMU; inspect %s/phases/*/fastdyn_timing.jsonl.\n' \
            "$work_dir" >&2
        exit 1
    fi
    bbl_count=$(wc -l < "$work_dir/bbl.txt")
    printf "lwIP basic blocks: %s (%s)\n" "$bbl_count" "$work_dir/bbl.txt"
    if (( bbl_count <= 2500 )); then
        printf "lwIP campaign did not reach the required 2501 basic blocks; increase the phase durations and rerun in a new directory.\n" >&2
        exit 1
    fi
}

campaign_active=false

print_single_campaign_bbl() {
    local bbl_count

    if [[ ! -f "$work_dir/bbl.txt" ]]; then
        printf '%s produced no BBL report; QEMU may not have reached the fuzzer lifecycle. Inspect %s/fastdyn_timing.jsonl.\n' \
            "$target" "$work_dir" >&2
        return 1
    fi

    bbl_count=$(wc -l < "$work_dir/bbl.txt")
    printf '%s basic blocks: %s (%s)\n' "$target" "$bbl_count" "$work_dir/bbl.txt"
}

handle_campaign_signal() {
    if [[ $campaign_active == true ]]; then
        printf '\n%s campaign received an external interruption; reporting persisted BBL coverage before exit.\n' \
            "$target" >&2
        print_single_campaign_bbl || true
    fi
    exit 130
}

trap handle_campaign_signal INT TERM
cd "$fastdyn_root"

if [[ $target == lwip ]]; then
    printf "Starting multi-phase lwIP basic-block campaign in %s\n" "$work_dir"
    printf "Basic-block report: %s/bbl.txt\n" "$work_dir"
    run_lwip_campaign
    exit 0
fi

run_seconds="${FASTDYN_AFLNET_RUN_SECONDS:-$default_run_seconds}"
check_seconds "$run_seconds" FASTDYN_AFLNET_RUN_SECONDS

# MQTT's selected snapshot must include its high SRAM stack, not just the ELF
# data/BSS sections generated by binary_wrange.py.
if [[ $target == mqtt ]]; then
    export FASTDYN_SNAPSHOT_RANGES_FILE="$snapshot_ranges"
    # Keep the 57-block oscillator-divider setup as a comparison shim; its
    # captured final RCC state is installed after SystemInit's reset writes.
    export FASTDYN_MQTT_OSC_STARTUP_SHIM="${FASTDYN_MQTT_OSC_STARTUP_SHIM:-1}"
    # The model executes a bounded, pre-snapshot UART IRQ comparison sweep.
    # It restores the real polled-UART state after each handler, so disabling
    # it is only useful for an intentionally parser-only measurement.
    export FASTDYN_MQTT_IRQ_SWEEP="${FASTDYN_MQTT_IRQ_SWEEP:-1}"
fi

printf "Starting %s basic-block campaign in %s\n" "$target" "$work_dir"
printf "Basic-block report: %s/bbl.txt\n" "$work_dir"
printf 'The launcher owns a %ss wall-clock budget; its final SIGINT is an expected campaign cutoff, not an AFLNet testcase timeout.\n' \
    "$run_seconds"

campaign_active=true
set +e
timeout --signal=INT --kill-after=15s "${run_seconds}s" env \
    FASTDYN_AFLNET_IN_DIR="$corpus" \
    FASTDYN_AFLNET_OUT_DIR="$work_dir/aflnet-out" \
    FASTDYN_BBL_FILE="$work_dir/bbl.txt" \
    FASTDYN_WORK_DIR="$work_dir/fastdyn-work" \
    "$runner" run -c "$config" -o "$work_dir"
campaign_status=$?
set -e
campaign_active=false

case "$campaign_status" in
    0)
        printf '%s campaign ended before its launcher wall-clock budget.\n' "$target"
        ;;
    124)
        printf '%s campaign reached the launcher-owned %ss wall-clock budget (expected).\n' \
            "$target" "$run_seconds"
        ;;
    *)
        printf '%s campaign failed with status %s.\n' "$target" "$campaign_status" >&2
        exit "$campaign_status"
        ;;
esac

if ! print_single_campaign_bbl; then
    exit 1
fi
