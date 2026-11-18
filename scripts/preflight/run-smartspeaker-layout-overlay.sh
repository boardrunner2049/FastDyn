#!/usr/bin/env bash
# Reproduce the frozen SmartSpeaker layout screen and checked FOP overlay.
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$repo_root"

matrix=build/preflight/lemix-smartspeaker-layout-matrix-2026-11-17
overlay=build/preflight/lemix-smartspeaker-stock-fop-overlay-2026-11-17
accelerated=build/preflight/lemix-smartspeaker-accelerated-overlay-2026-11-17
baseline=build/preflight/lemix-o0-rerun-2026-11-16/binaries
preload=build/preflight/lemix-smartspeaker/lemix_mmio_map.so
cpus=${AFFINITY_CPUS:-24,25}

python3 scripts/preflight/build-smartspeaker-bolt-layout-matrix.py
timeout -s INT -k 2 5 valgrind --tool=callgrind \
  --callgrind-out-file="$repo_root/$matrix/callgrind.stock.out" \
  env LD_PRELOAD="$repo_root/$preload" \
  "$repo_root/$baseline/SmartSpeaker.L1" \
  >"$matrix/callgrind.stock.stdout" 2>"$matrix/callgrind.stock.log" || true
python3 scripts/preflight/screen-smartspeaker-bolt-layout-matrix.py
taskset -c "$cpus" python3 scripts/preflight/benchmark-lemix-daemon.py \
  --paired-blocks --samples 10 --duration 15 --seed 20261127 \
  --preload "$preload" \
  --variant "stock_bolt=$baseline/SmartSpeaker.L1" \
  --variant "ext_cdsort_nosplit=$matrix/SmartSpeaker.ext_cdsort_nosplit" \
  --variant "ext_cdsort_split=$matrix/SmartSpeaker.ext_cdsort_split" \
  --variant "ext_cdsort_cdsplit=$matrix/SmartSpeaker.ext_cdsort_cdsplit" \
  --output "$matrix/layout-training.json" \
  >"$matrix/layout-training.log" 2>&1
python3 scripts/preflight/summarize-smartspeaker-layout-training.py

python3 scripts/preflight/build-smartspeaker-stock-fop-overlay.py
taskset -c "$cpus" python3 scripts/preflight/benchmark-lemix-daemon.py \
  --paired-blocks --samples 1 --duration 5 --seed 20261128 \
  --preload "$preload" \
  --variant "stock_bolt=$baseline/SmartSpeaker.L1" \
  --variant "checked_hot_fop_overlay=$overlay/SmartSpeaker.L1.checked-hot-fop-overlay" \
  --output "$overlay/smoke.json" >"$overlay/smoke.log" 2>&1
taskset -c "$cpus" python3 scripts/preflight/benchmark-lemix-daemon.py \
  --paired-blocks --samples 10 --duration 30 --seed 20261129 \
  --preload "$preload" \
  --variant "stock_bolt=$baseline/SmartSpeaker.L1" \
  --variant "checked_hot_fop_overlay=$overlay/SmartSpeaker.L1.checked-hot-fop-overlay" \
  --output "$overlay/training.json" >"$overlay/training.log" 2>&1

mkdir -p "$accelerated"
gcc -shared -fPIC -O2 -Wall -Wextra \
  tests/preflight/lemix_mmio_map.c tests/preflight/lemix_fast_tick_10x.c \
  -ldl -o "$accelerated/lemix_mmio_10x_tick.so"
taskset -c "$cpus" python3 scripts/preflight/benchmark-lemix-daemon.py \
  --paired-blocks --samples 1 --duration 5 --seed 20261202 \
  --preload "$accelerated/lemix_mmio_10x_tick.so" \
  --variant "stock_bolt=$baseline/SmartSpeaker.L1" \
  --variant "checked_hot_fop_overlay=$overlay/SmartSpeaker.L1.checked-hot-fop-overlay" \
  --output "$accelerated/calibration-10x.json" \
  >"$accelerated/calibration-10x.log" 2>&1
gcc -shared -fPIC -O2 -Wall -Wextra \
  tests/preflight/lemix_mmio_map.c tests/preflight/lemix_fast_tick.c \
  -ldl -o "$accelerated/lemix_mmio_100x_tick.so"
set +e
taskset -c "$cpus" python3 scripts/preflight/benchmark-lemix-daemon.py \
  --paired-blocks --samples 1 --duration 5 --seed 20261201 \
  --preload "$accelerated/lemix_mmio_100x_tick.so" \
  --variant "stock_bolt=$baseline/SmartSpeaker.L1" \
  --variant "checked_hot_fop_overlay=$overlay/SmartSpeaker.L1.checked-hot-fop-overlay" \
  --output "$accelerated/calibration-100x.json" \
  >"$accelerated/calibration-100x.log" 2>&1
capacity_status=$?
set -e
if [[ $capacity_status -eq 0 ]]; then
  echo "100x calibration unexpectedly remained fully valid" >&2
  exit 1
fi
python3 scripts/preflight/summarize-smartspeaker-stock-fop-overlay.py
