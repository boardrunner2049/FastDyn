#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
output=${1:-"$repo_root/build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16"}
bolt="$repo_root/third_party/preflight/llvm-build/bin/llvm-bolt"
bat_dump="$repo_root/third_party/preflight/llvm-build/bin/llvm-bat-dump"

if [[ ! -f "$repo_root/build/preflight/preflight-sdk-v1-2026-11-16/pass-outcomes.json" ]]; then
  "$repo_root/scripts/preflight/run-preflight-prototype.sh" \
    "$repo_root/build/preflight/preflight-sdk-v1-2026-11-16"
fi

"$repo_root/scripts/preflight/apply-bolt-extension.sh"
ninja -C "$repo_root/third_party/preflight/llvm-build" llvm-bolt llvm-bat-dump

python3 "$repo_root/scripts/preflight/smoke-bolt-guard.py" \
  --bolt "$bolt" \
  --work-dir "$repo_root/build/preflight/bolt-firmware-wrapper-smoke-2026-11-16"

PYTHONPATH="$repo_root/src" python3 \
  "$repo_root/scripts/preflight/run-bolt-firmware-wrapper.py" \
  --bolt "$bolt" --bat-dump "$bat_dump" --output "$output" --replace \
  --max-region-bytes 1024

reference="$repo_root/third_party/preflight/lemix/artifact/Lemix/Ported_Applications/dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/SmartSpeaker_Ported/build/SmartSpeaker"
python3 "$repo_root/scripts/preflight/benchmark-lemix-daemon.py" \
  --variant "R=$reference" \
  --variant "AUTO=$output/SmartSpeaker.bolt-qualified-compact" \
  --preload "$repo_root/build/preflight/lemix-smartspeaker/lemix_mmio_map.so" \
  --duration 2 --samples 30 --seed 20261118 --paired-blocks \
  --output "$output/auto-compact-paired-samples.json"

python3 "$repo_root/scripts/preflight/benchmark-lemix-daemon.py" \
  --variant "R=$reference" \
  --variant "HOT=$output/SmartSpeaker.bolt-hot-qualified-compact" \
  --variant "ALL=$output/SmartSpeaker.bolt-qualified-compact" \
  --preload "$repo_root/build/preflight/lemix-smartspeaker/lemix_mmio_map.so" \
  --duration 2 --samples 30 --seed 20261119 --paired-blocks \
  --output "$output/hot-vs-all-paired-samples.json"

PYTHONPATH="$repo_root/src" pytest -q \
  "$repo_root/tests/unit/test_bolt_firmware_wrapper.py" \
  "$repo_root/tests/unit/test_preflight_sdk.py" \
  "$repo_root/tests/unit/test_preflight_validation.py" \
  "$repo_root/tests/unit/test_firmware_candidate.py" \
  "$repo_root/tests/unit/test_preflight_artifacts.py"

python3 "$repo_root/scripts/preflight/generate-lemix-o0-rerun-report.py" \
  --run-dir "$repo_root/build/preflight/lemix-o0-rerun-2026-11-16" \
  --main-report "$repo_root/docs/preflight/lemix-o0-evaluation.md"

echo "BOLT-first firmware wrapper and consolidated report complete: $output"
