#!/bin/bash
# Entrypoint of the pseclab/halucinator-macro and pseclab/halucinator-fd-macro
# images (Figure 9). $BACKEND is set by the image.
#
#   run.sh [all | <firmware> ...] [iters]   e.g. `all 10`, `stm32_uart_it 3`
#   run.sh summary                         table + average speedup from /results/*.csv
#   run.sh plot                            /results/macrobench.pdf from /results/*.csv
#
# TIMEOUT=<s> overrides the per-iteration limit from firmwares.toml.
# Mount a host directory at /results; both images append to the same
# /results/<firmware>.csv so `summary` and `plot` see both backends.
set -u
trap 'kill -9 -1 2>/dev/null; exit 130' INT TERM
M=/opt/macrobench
CMD="${1:-all}"
mountpoint -q /results 2>/dev/null || { echo "mount a results directory: -v \"\$PWD:/results\"" >&2; exit 2; }
case "$CMD" in
    summary) exec python3 "$M/summarize.py" /results ;;
    plot)    exec python3 "$M/make_macro_figure.py" /results /results/macrobench.pdf ;;
    all)     python3 "$M/macro_run.py" --backend "$BACKEND" --iters "${2:-10}" ${TIMEOUT:+--timeout $TIMEOUT} & wait $! ;;
    *)       FWS=(); ITERS=10
             for a in "$@"; do case "$a" in ''|*[!0-9]*) FWS+=("$a") ;; *) ITERS=$a ;; esac; done
             python3 "$M/macro_run.py" --backend "$BACKEND" --iters "$ITERS" ${TIMEOUT:+--timeout $TIMEOUT} "${FWS[@]}" & wait $! ;;
esac
