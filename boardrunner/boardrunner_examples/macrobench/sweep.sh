#!/usr/bin/env bash
# Run N measured iterations per (firmware, backend), interleaved.
# Each iteration launches a fresh docker exec + fresh QEMU process, so no
# TCG / interpreter / plugin state persists across iterations — hence no
# warmup discard.
#
# Usage:
#   ./sweep.sh               # N=10, default 4 firmwares
#   ./sweep.sh 5             # N=5
#   ./sweep.sh 10 "stm32_uart_it zephyr_fs"
#
# Writes one CSV per firmware under results/.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
N="${1:-10}"
FWS="${2:-}"
RESULTS="$HERE/results"
mkdir -p "$RESULTS"

# On Ctrl-C, kill the emulators the current runner.py left inside the
# containers; otherwise they hold the endpoints and the next run stalls.
cleanup() {
    for c in halucinator halucinator-fd; do
        docker exec "$c" bash -c "pkill -9 -f '[/]usr/local/bin/halucinator'; pkill -9 -x qemu-system-arm; pkill -9 -f '[b]ench_uart_peer'; true" >/dev/null 2>&1 || true
    done
}
trap 'cleanup; exit 130' INT TERM

if [[ -z "$FWS" ]]; then
    FWS="stm32_uart_it zephyr_k64f zephyr_h103 zephyr_fs"
fi

echo "sweep: N=$N fws=[$FWS]"

for fw in $FWS; do
    out="$RESULTS/${fw}.csv"
    echo "iter,firmware,backend,wall_s" > "$out"
    echo "--- $fw ---"
    for ((i=1; i<=N; i++)); do
        for backend in halucinator halucinator-fd; do
            line=$(python3 "$HERE/runner.py" --fw "$fw" --backend "$backend" --csv 2>/dev/null || echo "$fw,$backend,NaN")
            echo "  iter=$i $backend → $line"
            echo "${i},${line}" >> "$out"
        done
    done
done

echo
echo "done. run: python3 $HERE/summarize.py"
