#!/usr/bin/env bash
# Rebuild and evaluate the BOLT-first policy on all retained firmware routines.
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$repo_root"

output=${1:-build/preflight/recovered-firmware-corpus-2026-11-16}
bolt=${BOLT_BIN:-third_party/preflight/llvm-build/bin/llvm-bolt}
samples=${SAMPLES:-30}
affinity_cpu=${AFFINITY_CPU:-0}

python3 scripts/preflight/evaluate-recovered-corpus.py \
  --bolt "$bolt" --work-dir "$output" --samples 7

PYTHONPATH=src python3 scripts/preflight/run-bolt-queue-firmware-wrapper.py \
  --reference "$output/corpus-O0" \
  --profile "$output/training.fdata" \
  --stock-bolt "$output/corpus-O0.bolt-pgo" \
  --output "$output/bolt-firmware-wrapper" \
  --artifact-stem corpus-O0 \
  --optimization-policy-id fop-layout-baseline \
  --program-arg=8 --program-arg=90112 --program-arg=11 --replace

python3 scripts/preflight/benchmark-recovered-corpus-policies.py \
  --root "$output" --phase training --samples "$samples" \
  --affinity-cpu "$affinity_cpu"
python3 scripts/preflight/select-recovered-corpus-policy.py --root "$output"
python3 scripts/preflight/benchmark-recovered-corpus-policies.py \
  --root "$output" --phase held-out --samples "$samples" \
  --affinity-cpu "$affinity_cpu"
taskset -c "$affinity_cpu" python3 scripts/preflight/benchmark-finite-variants.py \
  --paired-blocks --samples 100 --seed 20261118 \
  --program-arg=0 --program-arg=4194304 --program-arg=13 \
  --variant "reference=$output/corpus-O0" \
  --variant "stock_bolt=$output/corpus-O0.bolt-pgo" \
  --output "$output/bolt-firmware-wrapper/held-out-timing/crc8-precision-extension.json"
python3 scripts/preflight/summarize-recovered-corpus-wrapper.py --root "$output"
PYTHONPATH=src python3 scripts/preflight/publish-recovered-corpus-policy.py \
  --root "$output"
PYTHONPATH=src python3 scripts/preflight/publish-fixed-layout-bolt-candidates.py \
  --policy-root "$output/bolt-firmware-wrapper/published-policy" \
  --output "$output/bolt-firmware-wrapper/fixed-layout-firmware-candidates"
PYTHONPATH=src python3 scripts/preflight/publish-relocatable-bolt-candidates.py \
  --policy-root "$output/bolt-firmware-wrapper/published-policy" \
  --native-report "$output/bolt-firmware-wrapper/bolt-post-optimization-report.json" \
  --output "$output/bolt-firmware-wrapper/relocatable-firmware-candidates"
python3 scripts/preflight/generate-lemix-o0-rerun-report.py \
  --main-report docs/preflight/lemix-o0-evaluation.md
