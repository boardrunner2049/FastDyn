#!/bin/bash
# WP28: B3 performance rows on the frozen gate binary (plan WP28 item 4 / design §11 "B3
# performance": a REPORTED outcome, never pass/fail). Plan timing rule: WP28 may time
# "reported performance rows"; every arm on the gate binary, baselines re-collected on it
# (D-EV-2): the warm reference is the gate-binary compute base of wp28_gate.py's
# `compute` step, profiled on the train inputs only (training profiles on the base, §10.2).
#
# Arms (compute, HELD-OUT inputs 1-3, 30 interleaved reps -> 90 pairs per arm):
#   warm       the gate-binary base snapshot (reference)
#   b3-chain   chain-returns only (GENERAL slots on return exits)
#   b3-static  chain-static only (GENERAL slots on static exits; beyond the spec's return scope)
#   b3-spec    specialize-returns + CP + BF + DHO (SPEC links and variants)
#   b3         all six B3 passes
# Holds the host-wide timing lock, pins 4 cores (taskset), samples load averages every 30 s.
# Run from the wp28 worktree after `wp28_gate.py --steps compute`.
set -u
F="${FASTDYN_ROOT:-$(cd "$(dirname "$0")/../../../.." && pwd)}"
W=$F/runs/wp28
OUT=${OUT:-$W/timing}
LOAD=$W/timing-load.log
CORES=${CORES:-24-27}
REPS=${REPS:-30}
GATE=$F/wt/qemu-v6/build/qemu-system-arm
PY=$F/tools-env/bin/python
art() { $PY -c "import json,sys; d=json.load(open('$W/compute.json')); print(d['arms'][sys.argv[1]]['publish']['artifact_dir'])" "$1"; }
SNAP=$($PY -c "import json; print(json.load(open('$W/compute.json'))['base']['snapshot'])")
TOML=$W/compute/configs/compute-sc-gate.toml
cd "$(dirname "$0")/../.."
(
  while true; do echo "$(date +%s) $(cat /proc/loadavg) | qemu=$(pgrep -c qemu-system-arm)" >> $LOAD; sleep 30; done
) &
LOADPID=$!
echo "start $(date -Is) load $(cat /proc/loadavg) cores $CORES reps $REPS gate_sha $(sha256sum $GATE | cut -c1-64)" >> $LOAD
flock $F/runs/.timing.lock bash -c "echo \"lock acquired \$(date -Is) load \$(cat /proc/loadavg)\" >> $LOAD; \
  taskset -c $CORES $PY -m scopt run -c $TOML \
  --arms warm,b3-chain,b3-static,b3-spec,b3 \
  --warm $SNAP --artifact b3-chain=$(art b3-chain) --artifact b3-static=$(art b3-static) \
  --artifact b3-spec=$(art b3-spec) --artifact b3=$(art b3) \
  --input-set $F/fixtures/inputs/heldout/freertos-mps2-compute \
  --qemu $GATE --reps $REPS --seed 2801 --out $OUT"
RC=$?
echo "end $(date -Is) rc $RC load $(cat /proc/loadavg)" >> $LOAD
kill $LOADPID
exit $RC
