#!/usr/bin/env bash
# End-to-end HITL comparison: Avatar2 vs FastDyn passthrough.
#
# For each direction (reads, writes) runs both stacks against the
# corresponding firmware variant. Only one process can own the ST-Link
# probe at a time, so runs are serialized. Both benches append rows to
# results/combined.csv, which plot.py consumes to produce one PNG per
# direction.
#
# Optional env vars (see individual bench scripts for the full set):
#   N          firmware N variant (default: 1000, the paper's setting; 100/1000/100000)
#   ITERS      sessions per tool and direction (default: 1)
#   AVATAR_QEMU, PLUGIN, LIBHW_DIR, FASTDYN_QEMU, OPENOCD_SCRIPT, ...
#              → forwarded to the per-tool bench scripts if set

set -euo pipefail

HERE=$(dirname "$(readlink -f "$0")")
cd "${HERE}"

N="${N:-1000}"
ITERS="${ITERS:-1}"
RESULTS_DIR="${HERE}/results"
mkdir -p "${RESULTS_DIR}"
CSV="${RESULTS_DIR}/combined.csv"

# Reset combined CSV for this run.
rm -f "${CSV}"

# Defensive: kill any stale probe user before starting.
pkill -f 'openocd -f ' 2>/dev/null || true
sleep 1

echo "== run_all: N=${N} ITERS=${ITERS} CSV=${CSV} =="

for DIR in read write; do
    # Prefer the committed pre_built/ artifacts. If the evaluator ran `make`
    # to rebuild, they land in build/ (gitignored) -- fall back to that.
    FW="${HERE}/firmware/firmware_src/pre_built/microbench_hitl_${DIR}_N${N}.axf"
    if [[ ! -f "${FW}" ]]; then
        FW="${HERE}/firmware/firmware_src/build/microbench_hitl_${DIR}_N${N}.axf"
    fi
    if [[ ! -f "${FW}" ]]; then
        echo "run_all: firmware for direction=${DIR} N=${N} not found in"
        echo "         firmware/firmware_src/{pre_built,build}/."
        echo "         Build with:  make -C firmware/firmware_src all"
        exit 1
    fi

    echo
    echo "== ${DIR}s =========================================================="

    # --- Avatar2 (firmware in avatar-qemu, MMIO forwarded via openocd) ---
    echo "-- [1/2] Avatar2  (${DIR}s) --"
    "${HERE}/avatar2/scripts/bench_avatar2.py" \
        --firmware "${FW}" \
        --n "${N}" \
        --iters "${ITERS}" \
        --out "${CSV}"

    # Avatar2's OpenOCDTarget spawns its own openocd; it should exit on
    # avatar.shutdown(), but be defensive so it doesn't fight FastDyn.
    pkill -f 'openocd -f ' 2>/dev/null || true
    sleep 1

    # --- FastDyn passthrough (firmware in FastDyn-fork QEMU, LibHW direct) ---
    echo "-- [2/2] FastDyn passthrough  (${DIR}s) --"
    for _ in $(seq 1 "${ITERS}"); do
        FIRMWARE="${FW}" N="${N}" OUT="${CSV}" \
            "${HERE}/fastdyn_passthrough/scripts/bench_fastdyn.sh"
    done

    # Cool-down between directions so the probe fully releases.
    sleep 1
done

echo
echo "== summary =="
column -s, -t <"${CSV}"
echo
echo "CSV: ${CSV}"
echo "Render bar charts:"
echo "  python3 plot.py --csv ${CSV} --out-dir ${RESULTS_DIR}"
