#!/usr/bin/env bash
# Reproduce the unchanged SmartSpeaker precision, mechanism and publication path.
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$repo_root"

python3 scripts/preflight/run-smartspeaker-policy-precision.py
scripts/preflight/run-smartspeaker-hot-fop-heldout.sh
python3 scripts/preflight/build-smartspeaker-bolt-mechanisms.py
python3 scripts/preflight/build-smartspeaker-inline-all-extension.py
python3 scripts/preflight/rebuild-smartspeaker-stock-bat.py
PYTHONPATH=src python3 scripts/preflight/publish-smartspeaker-stock-policy.py
scripts/preflight/run-smartspeaker-layout-overlay.sh
scripts/preflight/run-smartspeaker-10x-confirmatory.sh
python3 scripts/preflight/generate-lemix-o0-rerun-report.py \
  --main-report docs/preflight/lemix-o0-evaluation.md
