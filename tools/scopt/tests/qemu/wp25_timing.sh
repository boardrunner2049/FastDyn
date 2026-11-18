#!/bin/bash
# WP25 integration: the one early B4 timing data point (plan WP25 "one early paired
# timing data point (>= 30 pairs, train) as a reported outcome"; design §7.5: B4-only
# artifacts are v5 and run on the current binary for this early measurement).
#
# Arms: warm (the base snapshot), b4 (D1|D2|D3), b4-d1, b4-d2, b4-d3 (per-class
# ablations, T-B4-11 shape) and sfb (icount-entry-sfb, a baseline TCG fix, reported
# separately and never counted as B4). 30 interleaved reps x 3 train inputs = 90 pairs
# per arm. Holds the host-wide timing lock, uses 4 cores (taskset), samples load
# averages every 30 s. Run from the wp25-int worktree.
set -u
F="${FASTDYN_ROOT:-$(cd "$(dirname "$0")/../../../.." && pwd)}"
OUT=${OUT:-$F/runs/wp25-int/timing}
LOAD=$F/runs/wp25-int/timing-load.log
CORES=${CORES:-8-11}
SNAP=$F/runs/b2/compute/store/snapshots/53b4132fc05526ff2925cd3acc610bf613e8b11e2315e15e520bff7fe080dab2
A=$F/runs/wp25/store/artifacts
B4=$A/055ef40cce9a55ebd20194b85ad74ba2d89177cd06c31a7d697748d4779db529
B4D1=$A/12a445d5c5716d46a7ede19a88e19b926b178e42d688bef014416f8af87c98a2
B4D2=$A/9cddf36e942174e35ac62ee51b2ea3184f616b9cd23c33fa859604f8050a5bf5
B4D3=$A/b65f36095e3ef908d90b70b0ca47996ef90b5bcede36bc680e328a12c094dff0
SFB=$F/runs/wp25/sfb-store/artifacts/69b0719ab746eac5d1c63947607bcd8618b57c33c281f45d8e8f6de6a4c7b719
cd "$(dirname "$0")/../.."
(
  while true; do echo "$(date +%s) $(cat /proc/loadavg) | qemu=$(pgrep -c qemu-system-arm)" >> $LOAD; sleep 30; done
) &
LOADPID=$!
echo "start $(date -Is) load $(cat /proc/loadavg) cores $CORES" >> $LOAD
flock $F/runs/.timing.lock bash -c "echo \"lock acquired \$(date -Is) load \$(cat /proc/loadavg)\" >> $LOAD; \
  taskset -c $CORES $F/tools-env/bin/python -m scopt run \
  -c $F/runs/wp13-r2/compute/configs/compute-sc.toml \
  --arms warm,b4,b4-d1,b4-d2,b4-d3,sfb \
  --warm $SNAP --artifact b4=$B4 --artifact b4-d1=$B4D1 --artifact b4-d2=$B4D2 \
  --artifact b4-d3=$B4D3 --artifact sfb=$SFB \
  --input-set $F/fixtures/inputs/train/freertos-mps2-compute \
  --qemu $F/wt/qemu-int/build/qemu-system-arm --reps 30 --seed 2501 --out $OUT"
RC=$?
echo "end $(date -Is) rc $RC load $(cat /proc/loadavg)" >> $LOAD
kill $LOADPID
exit $RC
