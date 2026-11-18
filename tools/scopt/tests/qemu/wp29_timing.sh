#!/bin/bash
# WP29: B4 timing on the frozen gate binary (T-B4-11 "per-class ablations validated and
# measured"; gate item 5, a reported outcome, never pass/fail). Plan timing rule: WP29 may
# time "reported performance rows"; every arm on the gate binary, baselines re-collected
# on it (D-EV-2).
#
# Arms (compute, train inputs 1-3, 30 interleaved reps -> 90 pairs per arm):
#   warm    the gate-binary base snapshot (reference)
#   b4      eliminate-state-sync D1|D2|D3          b4-d1/b4-d2/b4-d3  per-class ablations
#   sfb     icount-entry-sfb (a baseline TCG entry fix: NOT B4, never counted as caching)
#   sfb-b4  B4 layered on sfb (B4's marginal effect on top of the entry fix)
# Holds the host-wide timing lock, pins 4 cores (taskset), samples load averages every 30 s.
# Run from the wp29 worktree after `wp29_gate.py package-compute package-sfb-b4`.
set -u
F="${FASTDYN_ROOT:-$(cd "$(dirname "$0")/../../../.." && pwd)}"
W=$F/runs/wp29
OUT=${OUT:-$W/timing}
LOAD=$W/timing-load.log
CORES=${CORES:-24-27}
GATE=$F/wt/qemu-v6/build/qemu-system-arm
PK=$W/compute/package.json
art() { $F/tools-env/bin/python -c "import json,sys; d=json.load(open('$PK')); print(next(r['artifact_dir'] for r in d['rows'] if r['label']==sys.argv[1]))" "$1"; }
SNAP=$($F/tools-env/bin/python -c "import json; print(json.load(open('$W/compute/base.json'))['snapshot'])")
TOML=$W/compute/configs/compute-sc-gate.toml
cd "$(dirname "$0")/../.."
(
  while true; do echo "$(date +%s) $(cat /proc/loadavg) | qemu=$(pgrep -c qemu-system-arm)" >> $LOAD; sleep 30; done
) &
LOADPID=$!
echo "start $(date -Is) load $(cat /proc/loadavg) cores $CORES gate_sha $(sha256sum $GATE | cut -c1-64)" >> $LOAD
flock $F/runs/.timing.lock bash -c "echo \"lock acquired \$(date -Is) load \$(cat /proc/loadavg)\" >> $LOAD; \
  taskset -c $CORES $F/tools-env/bin/python -m scopt run -c $TOML \
  --arms warm,b4,b4-d1,b4-d2,b4-d3,sfb,sfb-b4 \
  --warm $SNAP --artifact b4=$(art b4) --artifact b4-d1=$(art b4-d1) \
  --artifact b4-d2=$(art b4-d2) --artifact b4-d3=$(art b4-d3) --artifact sfb=$(art sfb) \
  --artifact sfb-b4=$(art sfb+b4) \
  --input-set $F/fixtures/inputs/train/freertos-mps2-compute \
  --qemu $GATE --reps 30 --seed 2901 --out $OUT"
RC=$?
echo "end $(date -Is) rc $RC load $(cat /proc/loadavg)" >> $LOAD
kill $LOADPID
exit $RC
