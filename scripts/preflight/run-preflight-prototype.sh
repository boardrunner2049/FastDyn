#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
output=${1:-"$repo_root/build/preflight/preflight-sdk-v1-2026-11-16"}

PYTHONPATH="$repo_root/src" python3 \
  "$repo_root/scripts/preflight/run-preflight-prototype.py" \
  --output "$output" --replace

PYTHONPATH="$repo_root/src" pytest -q \
  "$repo_root/tests/unit/test_preflight_sdk.py" \
  "$repo_root/tests/unit/test_preflight_validation.py" \
  "$repo_root/tests/unit/test_firmware_candidate.py" \
  "$repo_root/tests/unit/test_preflight_artifacts.py"

python3 "$repo_root/scripts/preflight/generate-lemix-o0-rerun-report.py" \
  --run-dir "$repo_root/build/preflight/lemix-o0-rerun-2026-11-16" \
  --main-report "$repo_root/docs/preflight/lemix-o0-evaluation.md"

echo "preflight prototype and consolidated report complete: $output"
