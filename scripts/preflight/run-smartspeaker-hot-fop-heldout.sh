#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$repo_root"

output=${1:-build/preflight/lemix-smartspeaker-hot-fop-2026-11-17}
baseline=build/preflight/lemix-o0-rerun-2026-11-16
preload=build/preflight/lemix-smartspeaker/lemix_mmio_map.so

python3 scripts/preflight/smartspeaker-hot-fop-heldout.py \
  --output "$output" --stage freeze
taskset -c 24,25 python3 scripts/preflight/benchmark-lemix-daemon.py \
  --paired-blocks --samples 30 --duration 30 --seed 20261125 \
  --preload "$preload" \
  --variant "stock_bolt=$baseline/binaries/SmartSpeaker.L1" \
  --variant "hot_fop=$baseline/binaries/SmartSpeaker.FH" \
  --output "$output/samples.json" \
  >"$output/measurement.log" 2>&1
python3 scripts/preflight/smartspeaker-hot-fop-heldout.py \
  --output "$output" --stage summarize
