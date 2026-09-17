#!/usr/bin/env bash
# PCUpdate handler on halucinator, N=1000, ITERS iterations.

set -euo pipefail
ITERS="${ITERS:-10}"
N=1000
CFG=microbench_slice_pcupdate_config.yaml
RESULTS="$(dirname "$(readlink -f "$0")")/../results"
mkdir -p "$RESULTS"
CSV="$RESULTS/slice_pcupdate_N${N}.csv"
echo "iter,elapsed_s,per_op_ms" > "$CSV"

for i in $(seq 0 $((ITERS - 1))); do
    docker exec halucinator bash -c \
    "pkill -9 -f '[/]usr/local/bin/halucinator' 2>/dev/null || true; \
     pkill -9 -x qemu-system-arm 2>/dev/null || true"
    sleep 0.5
    E=$(docker exec halucinator bash -c \
        "cd /root/microbench && PYTHONPATH=/root/halucinator exec halucinator \
            -c=${CFG} -c=microbench_slice_addrs.yaml -c=microbench_slice_memory.yaml \
            -n slice_bench 2>&1" \
      | awk -F'SENTINEL_ELAPSED: ' '/SENTINEL_ELAPSED:/ {print $2+0}' | tail -1)
    PMS=$(awk -v t="$E" -v n="$N" 'BEGIN{printf "%.6f",(t/n)*1000}')
    printf "iter %2d  %.6fs  %s ms/op\n" "$i" "$E" "$PMS"
    printf "%d,%.6f,%s\n" "$i" "$E" "$PMS" >> "$CSV"
done
