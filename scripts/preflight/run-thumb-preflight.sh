#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

python3 -m pytest -q \
  tests/unit/test_preflight_sdk.py \
  tests/unit/test_firmware_candidate.py \
  tests/unit/test_preflight_artifacts.py \
  tests/unit/test_preflight_validation.py \
  tests/unit/test_bolt_firmware_wrapper.py

python3 scripts/preflight/run-thumb-preflight-pilot.py --replace --samples 30
scripts/preflight/build-thumb-fop-benchmark.sh
python3 scripts/preflight/benchmark-thumb-fop.py --replace --samples 30
python3 scripts/preflight/run-thumb-m0plus-pilot.py --replace
python3 scripts/preflight/profile-thumb-fop-candidates.py --replace --seconds 3
python3 scripts/preflight/run-thumb-hot-fop-pilot.py \
  --output build/preflight/thumb-cortex-m3-hot-fop-top2-nop-rejected-2026-11-16 \
  --replace --samples 30 --selection top2
python3 scripts/preflight/run-thumb-hot-fop-pilot.py \
  --output build/preflight/thumb-cortex-m3-hot-fop-2026-11-16 \
  --replace --samples 30 --selection all-hit
python3 scripts/preflight/run-thumb-tcg-diagnostic.py --replace
python3 scripts/preflight/profile-thumb-fop-candidates.py \
  --output build/preflight/thumb-cortex-m3-bounded-fop-profile-2026-11-16 \
  --max-gap-instructions 3 --replace --seconds 3
python3 scripts/preflight/run-thumb-hot-fop-pilot.py \
  --profile build/preflight/thumb-cortex-m3-bounded-fop-profile-2026-11-16/report.json \
  --output build/preflight/thumb-cortex-m3-bounded-fop-2026-11-16 \
  --selected-reload 0x08000d8e --selected-reload 0x08000d8a \
  --selected-reload 0x080066fc --samples 30 --work-units 100000 \
  --affinity-cpu 0 --replace
python3 scripts/preflight/run-thumb-tcg-diagnostic.py \
  --candidate build/preflight/thumb-cortex-m3-bounded-fop-2026-11-16/firmware-candidate/image.axf \
  --output build/preflight/thumb-cortex-m3-bounded-fop-2026-11-16/tcg-diagnostic \
  --address 0x08000d7c --work-units 100 --replace
python3 scripts/preflight/benchmark-thumb-fop-policies.py --replace
python3 scripts/preflight/profile-thumb-fallthrough-branches.py --replace --seconds 3
python3 scripts/preflight/run-thumb-fallthrough-pilot.py --replace --samples 30
python3 scripts/preflight/run-thumb-tcg-diagnostic.py \
  --candidate build/preflight/tfb-pin-final-2026-11-16/RTOSDemo.fallthrough-branch.axf \
  --output build/preflight/tfb-pin-final-2026-11-16/tcg-diagnostic \
  --address 0x080066e2 --replace

python3 scripts/preflight/generate-lemix-o0-rerun-report.py \
  --run-dir build/preflight/lemix-o0-rerun-2026-11-16 \
  --main-report docs/preflight/lemix-o0-evaluation.md
