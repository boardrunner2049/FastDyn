#!/usr/bin/env bash
# Standalone FastDyn HITL-passthrough microbench (paper Fig 7 methodology).
#
# Runs microbench_hitl_N<N>.axf inside FastDyn's patched QEMU with
# libfastdyn.so loaded. The passthrough device model owns 0x40000000..
# 0x5FFFFFFF and forwards every MMIO in that window to the real
# STM32F429I-DISC1 via LibHW's stlink backend. benchmark_start /
# benchmark_end are native-C callbacks built into libfastdyn.so's
# virtual registry; virts.txt binds them to the firmware's sentinel PCs.
# On benchmark_end the plugin prints "[BENCH][hitl] elapsed_ns=... elapsed_s=X"
# and exits QEMU.
#
# Emits a CSV row matching bench_avatar2.py so plot.py can compare them.
#
# Required env vars (or CLI-set):
#   FIRMWARE  path to a microbench_hitl_N<N>.axf built from firmware_src/
#   N         iterations baked into the firmware (matches -DN_INNER)
#
# Optional overrides (all default to inferred paths -- see below):
#   OUT         CSV output path (default: ../results/results.csv)
#   FASTDYN_QEMU  FastDyn-fork QEMU binary supporting plugins
#   PLUGIN      libfastdyn.so path (default: $FDYN_ROOT/build/libfastdyn.so)
#   LIBHW_DIR   dir containing libhw.so (default: $FDYN_ROOT/../libhw/out)
#   DEV_JSON    passthrough device config (default: sibling dev_config.json)

set -euo pipefail

: "${FIRMWARE:?FIRMWARE=path/to/microbench_hitl_{read,write}_N<N>.axf required}"
: "${N:?N=<iterations baked into firmware> required}"

# Direction inferred from firmware filename (microbench_hitl_{read,write}_N*).
# Override with DIRECTION=reads|writes if needed.
if [[ -z "${DIRECTION:-}" ]]; then
    case "$(basename "${FIRMWARE}")" in
        *_read_*)  DIRECTION="reads"  ;;
        *_write_*) DIRECTION="writes" ;;
        *) echo "bench_fastdyn: cannot infer direction from '${FIRMWARE}';" \
                "set DIRECTION=reads|writes" >&2; exit 1 ;;
    esac
fi

# ----- layout inference ------------------------------------------------
# This script lives at:
#   <FastDyn>/boardrunner/boardrunner_examples/microbenchmarks/
#            hitl_comparison/fastdyn_passthrough/scripts/bench_fastdyn.sh
# → HITL_ROOT = 3 dirs up
# → FDYN_ROOT = 7 dirs up
HERE=$(dirname "$(readlink -f "$0")")
HITL_ROOT=$(readlink -f "${HERE}/../..")
FDYN_ROOT=$(readlink -f "${HITL_ROOT}/../../../..")

OUT="${OUT:-${HITL_ROOT}/fastdyn_passthrough/results/results.csv}"
DEV_JSON="${DEV_JSON:-${HERE}/dev_config.json}"
PLUGIN="${PLUGIN:-${FDYN_ROOT}/build/libfastdyn.so}"
LIBHW_DIR="${LIBHW_DIR:-${FDYN_ROOT}/../libhw/out}"
# Fallback for the FastDyn-fork QEMU: expect a sibling `qemu/build/` clone.
FASTDYN_QEMU="${FASTDYN_QEMU:-${FDYN_ROOT}/../qemu/build/qemu-system-arm}"

# libhw.so is loaded dynamically by libfastdyn.so at plugin-load; make sure
# the runtime linker can find it.
# Same runtime search path the fastdyn CLI sets up: libhw, the plugin's own
# build dir, and the optional fuzzer library a LIBFUZZ=true build links.
export LD_LIBRARY_PATH="${LIBHW_DIR}:$(dirname "${PLUGIN}"):${FDYN_ROOT}/virtuals/fuzzer/fastdyn_fuzz_lib/target/release:${LD_LIBRARY_PATH:-}"

for f in "${FIRMWARE}" "${FASTDYN_QEMU}" "${PLUGIN}" "${DEV_JSON}"; do
    if [[ ! -f "${f}" ]]; then
        echo "bench_fastdyn: missing prerequisite: ${f}" >&2
        exit 1
    fi
done
if [[ ! -e "${LIBHW_DIR}/libhw.so" ]]; then
    echo "bench_fastdyn: libhw.so not found in ${LIBHW_DIR}" >&2
    exit 1
fi
command -v arm-none-eabi-nm >/dev/null \
    || { echo "bench_fastdyn: need arm-none-eabi-nm on PATH" >&2; exit 1; }

# Resolve sentinel PCs from the ELF (they shift with N).
BENCH_START=$(arm-none-eabi-nm --defined-only "${FIRMWARE}" \
              | awk '$3=="bench_start"{print "0x"$1}')
BENCH_DONE=$(arm-none-eabi-nm --defined-only "${FIRMWARE}" \
             | awk '$3=="bench_done"{print "0x"$1}')
if [[ -z "${BENCH_START}" || -z "${BENCH_DONE}" ]]; then
    echo "bench_fastdyn: could not resolve bench_start/bench_done in ${FIRMWARE}" >&2
    exit 1
fi

WORK=$(mktemp -d -t fastdyn.XXXXXX)
trap 'rm -rf "${WORK}"' EXIT

# Bind FastDyn's native-C sentinel callbacks to the firmware PCs.
# benchmark_start / benchmark_end are libfastdyn.so's built-in timing
# virtuals (virtuals/virtuals.c); benchmark_end prints
# "[BENCH][hitl] elapsed_ns=... elapsed_s=..." and exits QEMU.
cat >"${WORK}/virts.txt" <<EOF
${BENCH_START} benchmark_start
${BENCH_DONE} benchmark_end hitl
EOF

# QEMU needs two file-backed RAM banks for cortexm; sizes match the paper
# invocation (512 M + 512 K). Content doesn't matter -- the firmware zeros
# .bss itself in Reset_Handler.
LOG="${WORK}/qemu.log"
set +e
"${FASTDYN_QEMU}" \
    -machine "cortexm,memory-backend=ram0" -cpu cortex-m4 -kernel "${FIRMWARE}" \
    --semihosting --semihosting-config enable=on,target=native \
    -global armv7m.init-nsvtor=0x08000000 \
    -object "memory-backend-file,id=ram0,mem-path=${WORK}/ram0,size=512M,share=on" \
    -global "cortexm-soc.ram_baseaddr0=0x20000000" \
    -object "memory-backend-file,id=ram1,mem-path=${WORK}/ram1,size=512K,share=on" \
    -global "cortexm-soc.ram_baseaddr1=0x30000000" \
    -global "cortexm-soc.ram_backend1=ram1" \
    --plugin "${PLUGIN},virtual=${WORK}/virts.txt,dev=${DEV_JSON}" \
    -display none 2>&1 | tee "${LOG}"
rc=${PIPESTATUS[0]}
set -e

# benchmark_end calls exit(0), so QEMU exits 0 on a clean run.
if [[ "${rc}" -ne 0 && "${rc}" -ne 143 ]]; then
    echo "bench_fastdyn: qemu exited ${rc}" >&2
    exit "${rc}"
fi

ELAPSED=$(grep -oE 'elapsed_s=[0-9.]+' "${LOG}" | tail -1 | cut -d= -f2)
if [[ -z "${ELAPSED}" ]]; then
    echo "bench_fastdyn: could not find '[BENCH][hitl] ... elapsed_s=' in qemu output" >&2
    exit 1
fi

PER_OP_MS=$(awk -v t="${ELAPSED}" -v n="${N}" 'BEGIN{printf "%.6f", (t/n)*1000.0}')
printf "fastdyn-passthrough  %-6s  iters=%d  total=%.4fs  per_op=%s ms\n" \
    "${DIRECTION}" "${N}" "${ELAPSED}" "${PER_OP_MS}"

mkdir -p "$(dirname "${OUT}")"
if [[ ! -s "${OUT}" ]]; then
    echo "tool,direction,iters,total_s,per_op_ms" >"${OUT}"
fi
printf "fastdyn-passthrough,%s,%d,%.6f,%s\n" \
    "${DIRECTION}" "${N}" "${ELAPSED}" "${PER_OP_MS}" >>"${OUT}"
