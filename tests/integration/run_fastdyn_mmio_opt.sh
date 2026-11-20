#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
export PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}"
exec "${FASTDYN_PYTHON:-$repo/fastdyn-env/bin/python}" "$repo/tests/integration/mmio_opt/run.py"
