#!/bin/bash
# Entrypoint of the pseclab/fastdyn-slice image.
#
#   run.sh modifier [iters]   function bypass  (Figure 8a)
#   run.sh virtual  [iters]   host operation   (Figure 8b)
#   run.sh both     [iters]   both of the above
#   run.sh plot               render Figure 8 -> /results/fig8.pdf
#
# Each iteration is a fresh QEMU process; one `[BENCH] ... elapsed_ns=<N>`
# line per run. When /results is mounted (-v "$PWD/results:/results") the
# lines are also written to /results/<bench>_run.log and `plot` uses those
# files (falling back to the shipped measurements for anything missing).
set -u
# bash is PID 1 here and would ignore SIGINT/SIGTERM; make Ctrl-C / docker
# stop kill everything in the container instead of leaving it running.
trap 'kill -9 -1 2>/dev/null; exit 130' INT TERM
CMD="${1:-both}"
ITERS="${2:-100}"
ROOT=/workspace/FastDyn/boardrunner/boardrunner_examples/microbenchmarks/firmware_slicing
OUT=/results
mountpoint -q "$OUT" 2>/dev/null || OUT=""   # only when mounted from the host

summarize() {   # $1 = bench name, stdin = [BENCH] lines
    grep -oE 'elapsed_ns=[0-9]+' | cut -d= -f2 | sort -n | \
    awk -v b="$1" -v N=1000 '{a[i++]=$1;s+=$1} END{
        if (i==0) { printf "%-8s no samples\n", b; exit }
        mean=s/i; ss=0; for (k=0;k<i;k++){d=a[k]-mean; ss+=d*d}
        sd=(i>1)?sqrt(ss/(i-1)):0
        printf "%-8s n=%d  mean=%.2f us/run  sd=%.2f us  (%.1f ns/op, N=%d ops/run)  min=%.2f  median=%.2f  max=%.2f us\n",
               b, i, mean/1000, sd/1000, mean/N, N, a[0]/1000, a[int(i/2)]/1000, a[i-1]/1000 }'
}

bench() {       # $1 = modifier|virtual
    local dir; case "$1" in modifier) dir=modifier_benchmarking ;; virtual) dir=virtuals_benchmarking ;; esac
    echo "=== $1 ($ITERS runs) ==="
    if [ -n "$OUT" ]; then
        "$ROOT/$dir/run_bench.sh" "$ITERS" | tee "$OUT/$1_run.log" &
        wait $!
        summarize "$1" < "$OUT/$1_run.log"
    else
        "$ROOT/$dir/run_bench.sh" "$ITERS" | tee /tmp/$1.log &
        wait $!
        summarize "$1" < /tmp/$1.log
    fi
}

case "$CMD" in
    modifier|mod|m) bench modifier ;;
    virtual|virt|v) bench virtual ;;
    both|all)       bench modifier; bench virtual ;;
    plot)
        [ -n "$OUT" ] || { echo "mount a results directory: -v \"\$PWD/results:/results\"" >&2; exit 2; }
        args=()
        pick() {   # $1 = label, $2 = plot option, $3 = file in /results, $4 = shipped file
            if [ -f "$OUT/$3" ]; then
                local n; n=$(grep -cvE '^(tool|iter),|^\s*$' "$OUT/$3")
                echo "$1: /results/$3 ($n runs)"; args+=("$2" "$OUT/$3")
            else
                echo "$1: shipped $4 (paper's measurement)"
            fi
        }
        pick "FastDyn function bypass    " --fastdyn-modifier    modifier_run.log results/modifier_benchmark.txt
        pick "FastDyn host operation     " --fastdyn-virtual     virtual_run.log  results/virtual_benchmark.txt
        pick "HALucinator function bypass" --halucinator-pc      pcupdate_run.csv baseline/results/pc_update_benchmark.txt
        pick "HALucinator host operation " --halucinator-counter counter_run.csv  baseline/results/counter_update_benchmark.txt
        pick "Avatar2 HITL reads         " --avatar2-reads       hitl_avatar2_reads.csv   hitl_comparison/results/_avatar2_per_iter_reads.csv
        pick "Avatar2 HITL writes        " --avatar2-writes      hitl_avatar2_writes.csv  hitl_comparison/results/_avatar2_per_iter_writes.csv
        pick "FastDyn HITL reads         " --fastdyn-reads       hitl_fastdyn_reads.csv   hitl_comparison/results/_fastdyn_per_iter_reads.csv
        pick "FastDyn HITL writes        " --fastdyn-writes      hitl_fastdyn_writes.csv  hitl_comparison/results/_fastdyn_per_iter_writes.csv
        exec python3 "$ROOT/scripts/plot_results.py" "${args[@]}" -o "$OUT/fig8.pdf" ;;
    *) echo "Usage: run.sh [modifier|virtual|both|plot] [iterations]" >&2; exit 2 ;;
esac
