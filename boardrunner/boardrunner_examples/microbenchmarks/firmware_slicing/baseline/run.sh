#!/bin/bash
# Entrypoint of the pseclab/halucinator-slice image.
#
#   run.sh pcupdate [iters]   function bypass  (Figure 8a), ~40 s per iteration
#   run.sh counter  [iters]   host operation   (Figure 8b), ~10 s per iteration
#
# Runs the slice firmware (N=1000) under unmodified upstream HALucinator with
# the PcUpdate / CounterVirt handler and collects the SENTINEL_ELAPSED line
# the handler prints (elapsed between the bench_start and bench_done
# breakpoints, measured in-process). Prints one line per iteration and, when
# /results is mounted (-v "$PWD/results:/results"), writes
# /results/<bench>_run.csv (iter,elapsed_s,per_op_ms).
set -u
# bash is PID 1 here and would ignore SIGINT/SIGTERM; make Ctrl-C / docker
# stop kill everything in the container instead of leaving it running.
trap 'kill -9 -1 2>/dev/null; exit 130' INT TERM
BENCH="${1:-pcupdate}"
ITERS="${2:-10}"
N=1000
case "$BENCH" in
    pcupdate) CFG=microbench_slice_pcupdate_config.yaml ;;
    counter)  CFG=microbench_slice_counter_config.yaml ;;
    *) echo "Usage: run.sh [pcupdate|counter] [iterations]" >&2; exit 2 ;;
esac
CSV=""
if mountpoint -q /results 2>/dev/null; then
    CSV="/results/${BENCH}_run.csv"; echo "iter,elapsed_s,per_op_ms" > "$CSV"
fi
cd /root/microbench
for i in $(seq 0 $((ITERS - 1))); do
    pkill -9 -x qemu-system-arm 2>/dev/null; sleep 0.2
    # Run in the background and `wait` so a trapped signal is handled
    # immediately instead of after the 10-40 s HALucinator run.
    ( PYTHONPATH=/root/halucinator halucinator -c="$CFG" \
          -c=microbench_slice_addrs.yaml -c=microbench_slice_memory.yaml -n slice_bench 2>&1 \
      | awk -F'SENTINEL_ELAPSED: ' '/SENTINEL_ELAPSED:/ {print $2+0}' | tail -1 ) > /tmp/elapsed &
    wait $!
    E=$(cat /tmp/elapsed)
    if [ -z "$E" ]; then echo "iter $i: no SENTINEL_ELAPSED line (HALucinator failed)" >&2; exit 1; fi
    PMS=$(awk -v t="$E" -v n="$N" 'BEGIN{printf "%.6f",(t/n)*1000}')
    printf "iter %2d  %.6f s  (%s ms/op)\n" "$i" "$E" "$PMS"
    [ -n "$CSV" ] && printf "%d,%.6f,%s\n" "$i" "$E" "$PMS" >> "$CSV"
done
[ -n "$CSV" ] && echo "wrote $CSV"
exit 0
