#!/bin/bash
# Entrypoint of the pseclab/hitl-slice image (Figure 8c: HITL execution,
# Avatar2 vs FastDyn passthrough). Needs an STM32F429I-DISC1 on USB:
#   docker run --rm -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' \
#       -v "$PWD:/results" pseclab/hitl-slice:<tag> <command>
#
#   run.sh check            openocd connects to the board and lists the target
#   run.sh flash            flash the idle while(1) firmware (once per board)
#   run.sh avatar2 [iters]  Avatar2/openocd:      1000 reads + 1000 writes, ~10 s each
#   run.sh fastdyn [iters]  FastDyn passthrough:  1000 reads + 1000 writes, ~0.5 s each
#   run.sh all [iters]      avatar2 then fastdyn
#   anything else           handed to the fastdyn-slice entrypoint (both, plot, ...)
#
# Each iteration is one emulator session (fresh QEMU, fresh openocd). Rows go
# to /results/hitl_<tool>_<reads|writes>.csv (tool,direction,iters,total_s,per_op_ms).
set -u
trap 'kill -9 -1 2>/dev/null; exit 130' INT TERM
CMD="${1:-check}"
ITERS="${2:-10}"
N=1000
FD=/workspace/FastDyn
H=$FD/boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison
FW=$H/firmware/firmware_src/pre_built
OUT=/results; mountpoint -q "$OUT" 2>/dev/null || OUT=/tmp/results; mkdir -p "$OUT"
export OPENOCD_SCRIPTS=/usr/share/openocd/scripts

probe_free() { pkill -9 -f 'openocd' 2>/dev/null; pkill -9 -x qemu-system-arm 2>/dev/null; sleep 1; }

run_avatar2() {
    for dir in read write; do
        for i in $(seq 1 "$ITERS"); do
            probe_free
            ( cd /tmp && /opt/venv/bin/python -P "$H/avatar2/scripts/bench_avatar2.py" \
                --firmware "$FW/microbench_hitl_${dir}_N$N.axf" --n $N --iters 1 \
                --avatar-qemu /workspace/avatar2/targets/src/avatar-qemu/build/qemu-system-arm \
                --output-dir /tmp/avatar_out --out "$OUT/hitl_avatar2_${dir}s.csv" \
                2>/tmp/avatar2.log | sed -nE "s/^iter 0: +([0-9.]+)s +\((.*)\)/avatar2  ${dir}s  iter $i: \1 s  (\2)/p" | sed "s/  reads  /  reads   /" ) &
            wait $! || { echo "avatar2 ${dir}s iteration $i failed" >&2; exit 1; }
        done
    done
}

run_fastdyn() {
    for dir in read write; do
        for i in $(seq 1 "$ITERS"); do
            probe_free
            ( FIRMWARE="$FW/microbench_hitl_${dir}_N$N.axf" N=$N OUT="$OUT/hitl_fastdyn_${dir}s.csv" \
              PLUGIN=$FD/build-hitl/libfastdyn.so LIBHW_DIR=/workspace/libhw/out \
              FASTDYN_QEMU=/workspace/qemu/build/qemu-system-arm \
              "$H/fastdyn_passthrough/scripts/bench_fastdyn.sh" 2>&1 \
              | sed -nE "s/^fastdyn-passthrough +([a-z]+) +iters=[0-9]+ +total=([0-9.]+)s +per_op=(.*)/fastdyn  \1  iter $i: \2 s  (\3\/op)/p; /FAILED|missing/p" | sed "s/  reads  /  reads   /" ) &
            wait $! || { echo "fastdyn ${dir}s iteration $i failed" >&2; exit 1; }
        done
    done
}

case "$CMD" in
    check) exec openocd -f board/stm32f429discovery.cfg -c "init; targets; shutdown" ;;
    flash) exec "$H/firmware/scripts/flash.sh" ;;
    avatar2) run_avatar2 ;;
    fastdyn) run_fastdyn ;;
    all) run_avatar2; run_fastdyn ;;
    *) exec /opt/bin/run.sh "$@" ;;
esac
[ "$OUT" = /results ] && ls "$OUT"/hitl_*.csv
exit 0
