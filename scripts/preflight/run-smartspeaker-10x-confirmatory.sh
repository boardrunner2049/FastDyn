#!/usr/bin/env bash
# Confirm the checked SmartSpeaker overlay in the separately identified 10x schedule.
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$repo_root"

output=build/preflight/lemix-smartspeaker-accelerated-overlay-2026-11-17
baseline=build/preflight/lemix-o0-rerun-2026-11-16/binaries/SmartSpeaker.L1
overlay=build/preflight/lemix-smartspeaker-stock-fop-overlay-2026-11-17/SmartSpeaker.L1.checked-hot-fop-overlay
preload="$output/lemix_mmio_10x_tick.so"
result="$output/confirmatory-10x.json"
cpus=${AFFINITY_CPUS:-24,25}

if [[ -e "$result" ]]; then
  echo "refusing to overwrite $result" >&2
  exit 1
fi

taskset -c "$cpus" python3 scripts/preflight/benchmark-lemix-daemon.py \
  --paired-blocks --samples 30 --duration 20 --seed 20261203 \
  --preload "$preload" \
  --variant "stock_bolt=$baseline" \
  --variant "checked_hot_fop_overlay=$overlay" \
  --output "$result" >"$output/confirmatory-10x.log" 2>&1

python3 scripts/preflight/summarize-smartspeaker-stock-fop-overlay.py \
  --replace-report
