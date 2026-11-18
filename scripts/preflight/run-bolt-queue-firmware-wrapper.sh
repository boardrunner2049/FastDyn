#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$repo_root"

python3 scripts/preflight/run-bolt-queue-firmware-wrapper.py --replace

python3 scripts/preflight/benchmark-finite-variants.py \
  --paired-blocks \
  --samples 30 \
  --seed 20261117 \
  --variant R=build/preflight/lemix-queue/O0/lemix_queue_workload \
  --variant STOCK=build/preflight/lemix-queue/O0/lemix_queue_workload.bolt-pgo \
  --variant FULL_FOP=build/preflight/lemix-queue/bolt-firmware-wrapper/lemix_queue_workload.bolt-wrapper-relocatable \
  --variant WRAP=build/preflight/lemix-queue/bolt-firmware-wrapper/lemix_queue_workload.bolt-qualified-compact \
  --output build/preflight/lemix-queue/bolt-firmware-wrapper/fixed-work-paired-results.json

python3 scripts/preflight/generate-lemix-o0-rerun-report.py \
  --run-dir build/preflight/lemix-o0-rerun-2026-11-16 \
  --main-report docs/preflight/lemix-o0-evaluation.md
