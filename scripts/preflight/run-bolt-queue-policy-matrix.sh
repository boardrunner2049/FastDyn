#!/usr/bin/env bash
set -euo pipefail

# Reproduce the bounded BOLT-policy comparison on the fixed-work queue fixture.
# The workload, profile, pinned BOLT build, and ordinary wrapper must already
# exist; run build-lemix-queue-workload.sh and the normal preflight setup first.

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$repo_root"

samples=${SAMPLES:-30}
affinity_cpu=${AFFINITY_CPU:-0}
queue=build/preflight/lemix-queue

run_arm() {
  local directory=$1
  local policy=$2
  shift 2
  local command=(
    python3 scripts/preflight/run-bolt-queue-firmware-wrapper.py
    --output "$queue/$directory"
    --optimization-policy-id "$policy"
    --replace
  )
  local argument
  for argument in "$@"; do
    command+=("--bolt-extra-arg=$argument")
  done
  PYTHONPATH=src "${command[@]}"
}

run_arm bolt-policy-baseline-1k fop-layout-baseline-1k
run_arm bolt-policy-peepholes fop-layout-peepholes \
  --peepholes=double-jumps,useless-branches
run_arm bolt-policy-regreassign fop-layout-regreassign --reg-reassign
run_arm bolt-policy-regreassign-aggressive fop-layout-regreassign-aggressive \
  --reg-reassign --use-aggr-reg-reassign
run_arm bolt-policy-frame-store-removal fop-layout-experimental-store-removal \
  --frame-opt-rm-stores

python3 scripts/preflight/compose-bolt-queue-policies.py \
  --reference "$queue/O0/lemix_queue_workload" \
  --baseline-result "$queue/bolt-policy-baseline-1k/wrapper-result.json" \
  --baseline-image "$queue/bolt-policy-baseline-1k/lemix_queue_workload.bolt-qualified-compact" \
  --experimental-result "$queue/bolt-policy-frame-store-removal/wrapper-result.json" \
  --experimental-image "$queue/bolt-policy-frame-store-removal/lemix_queue_workload.bolt-qualified-compact" \
  --output "$queue/bolt-policy-composed-2026-11-16/lemix_queue_workload.bolt-composed-compact" \
  --report "$queue/bolt-policy-composed-2026-11-16/report.json"

timing="$queue/bolt-policy-matrix-timing-2026-11-16"
taskset -c "$affinity_cpu" python3 scripts/preflight/benchmark-finite-variants.py \
  --paired-blocks --samples "$samples" --seed 20261116 \
  --variant "reference=$queue/O0/lemix_queue_workload" \
  --variant "baseline_compact=$queue/bolt-policy-baseline-1k/lemix_queue_workload.bolt-qualified-compact" \
  --variant "store_removal_compact=$queue/bolt-policy-frame-store-removal/lemix_queue_workload.bolt-qualified-compact" \
  --output "$timing/report.json"

mkdir -p "$timing/callgrind" "$timing/cachegrind"
for specification in \
  "reference=$queue/O0/lemix_queue_workload" \
  "baseline=$queue/bolt-policy-baseline-1k/lemix_queue_workload.bolt-qualified-compact" \
  "store_removal=$queue/bolt-policy-frame-store-removal/lemix_queue_workload.bolt-qualified-compact"
do
  name=${specification%%=*}
  binary=${specification#*=}
  valgrind --tool=callgrind \
    --callgrind-out-file="$timing/callgrind/$name.out" \
    --log-file="$timing/callgrind/$name.log" "$binary" >/dev/null
  valgrind --tool=cachegrind --branch-sim=yes \
    --cachegrind-out-file="$timing/cachegrind/$name.out" \
    --log-file="$timing/cachegrind/$name.log" "$binary" >/dev/null
done

python3 scripts/preflight/summarize-bolt-queue-policy-matrix.py

wrapper="$queue/bolt-firmware-wrapper"
taskset -c "$affinity_cpu" python3 scripts/preflight/benchmark-finite-variants.py \
  --paired-blocks --samples "$samples" --seed 20261120 \
  --variant "reference=$queue/O0/lemix_queue_workload" \
  --variant "stock_bolt=$queue/O0/lemix_queue_workload.bolt-pgo" \
  --variant "full_fop=$wrapper/lemix_queue_workload.bolt-wrapper-relocatable" \
  --variant "compact_fop=$wrapper/lemix_queue_workload.bolt-qualified-compact" \
  --output "$wrapper/policy-training-2026-11-17.json"
python3 scripts/preflight/select-lemix-queue-policy.py --wrapper "$wrapper"
relocatable_choice=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["choice"]["relocatable"])' \
  "$wrapper/queue-policy-selection.json")
case "$relocatable_choice" in
  reference) policy_image="$queue/O0/lemix_queue_workload" ;;
  stock_bolt) policy_image="$queue/O0/lemix_queue_workload.bolt-pgo" ;;
  full_fop) policy_image="$wrapper/lemix_queue_workload.bolt-wrapper-relocatable" ;;
  compact_fop) policy_image="$wrapper/lemix_queue_workload.bolt-qualified-compact" ;;
  *) echo "unsupported frozen queue choice: $relocatable_choice" >&2; exit 1 ;;
esac
taskset -c "$affinity_cpu" python3 scripts/preflight/benchmark-finite-variants.py \
  --paired-blocks --samples "$samples" --seed 20261121 \
  --variant "reference=$queue/O0/lemix_queue_workload" \
  --variant "stock_bolt=$queue/O0/lemix_queue_workload.bolt-pgo" \
  --variant "relocatable_policy=$policy_image" \
  --output "$wrapper/policy-held-out-2026-11-17.json"
python3 scripts/preflight/summarize-lemix-queue-policy.py --wrapper "$wrapper"
PYTHONPATH=src python3 scripts/preflight/publish-recovered-corpus-policy.py \
  --selection "$wrapper/queue-policy-selection.json" \
  --evidence "$wrapper/queue-policy-held-out-report.json" \
  --output "$wrapper/published-policy"
PYTHONPATH=src python3 scripts/preflight/publish-fixed-layout-bolt-candidates.py \
  --policy-root "$wrapper/published-policy" \
  --output "$wrapper/fixed-layout-firmware-candidates"
PYTHONPATH=src python3 scripts/preflight/publish-relocatable-bolt-candidates.py \
  --policy-root "$wrapper/published-policy" \
  --native-report "$wrapper/bolt-post-optimization-report.json" \
  --output "$wrapper/relocatable-firmware-candidates"
python3 scripts/preflight/generate-lemix-o0-rerun-report.py \
  --main-report docs/preflight/lemix-o0-evaluation.md
