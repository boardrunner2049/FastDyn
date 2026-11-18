#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
run_dir=${1:-"$repo_root/build/preflight/lemix-o0-rerun-$(date +%Y%m%d-%H%M%S)"}

if [[ -e "$run_dir" && -n "$(find "$run_dir" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "refusing to overwrite non-empty run directory: $run_dir" >&2
  echo "choose a new directory; historical and completed evidence are immutable" >&2
  exit 2
fi

mkdir -p "$run_dir"

python3 "$repo_root/scripts/preflight/run-lemix-o0-rerun.py" \
  --run-dir "$run_dir" --stage all --blocks 30 --duration 5

python3 "$repo_root/scripts/preflight/benchmark-lemix-accessor.py" \
  --run-dir "$run_dir" --blocks 30

python3 "$repo_root/scripts/preflight/benchmark-lemix-accessor.py" \
  --run-dir "$run_dir" --check-only

python3 "$repo_root/scripts/preflight/run-lemix-stoke-focused.py" \
  --run-dir "$run_dir" --stage all

python3 "$repo_root/scripts/preflight/run-lemix-stoke-search.py" \
  --run-dir "$run_dir" --workers 4

python3 "$repo_root/scripts/preflight/generate-lemix-o0-rerun-report.py" \
  --run-dir "$run_dir" \
  --main-report "$repo_root/docs/preflight/lemix-o0-evaluation.md"

echo "focused LEMIX O0 rerun complete: $repo_root/docs/preflight/lemix-o0-evaluation.md"
