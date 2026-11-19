#!/usr/bin/env bash
set -euo pipefail
W=$(realpath "${1:?expected baseline directory}")
arm=${2:?}
D="${3:-$W/whopr-$arm}"
if [[ -d $D/v10 ]]; then
    echo "Validation already ran in $D. Supply a fresh output directory as argument 3." >&2
    exit 2
fi
export SCOPT_ENVLAYOUT_PATH="$W/envlayouts"
repo=$(git rev-parse --show-toplevel)
export FASTDYN_ROOT="$repo" SCOPT_FASTDYN_SRC="$repo"
export SCOPT_RUNTIME_QEMU="${SCOPT_RUNTIME_QEMU:-$repo/third_party/common/qemu/build/qemu-system-arm}"
export SCOPT_V6_EXES=$(sha256sum "$SCOPT_RUNTIME_QEMU" | cut -d' ' -f1)
: "${SCOPT_HOST_INLINE_BRIDGE:?set to the built scopt-native executable}"
read_json() { python -c 'import sys,json;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }
snap=$(read_json "$W/baseline.json" snapshot)
ram_root=$(python - "$W" <<'PYRAM'
import json
import os
from pathlib import Path
import sys

from scopt.run.benchmark_memory import benchmark_ram_override

root = Path(sys.argv[1])
metadata = json.loads((root / "baseline.json").read_text())
print(benchmark_ram_override(root / "host-inline.toml", metadata.get("ram_root"), os.environ) or "")
PYRAM
)
if [[ -n $ram_root ]]; then export SCOPT_RAM_ROOT="$ram_root"; fi
mkdir -p "$D"
extra=()
pipeline=$arm
case $arm in
  compact-frame|frame)
    pipeline=host-inline-extreme
    clean=false
    if [[ $arm == compact-frame ]]; then clean=true; fi
    printf '{"host-inline-extreme":{"allow_partial":true,"max_inline_depth":2,"compact_frame":%s}}\n' "$clean" >"$D/params.json"
    extra+=(--params "$D/params.json");;
  b3|b4) extra+=(--b3 --runtime-identity "$W/runtime-identity.v2.json" --elf "$W/guest.elf" --prep "$W/prep");;
esac
python "$(dirname "$0")/measure_command.py" "$D/offline.json" scopt optimize "$snap" --pipeline "$pipeline" --profile "$W/profile/merged.profile.json" --store "$D" "${extra[@]}" --json >"$D/result.json"
art=$(read_json "$D/result.json" artifact_dir)
scopt validate "$art" --dynamic -c "$W/host-inline.toml" --qemu "$SCOPT_RUNTIME_QEMU" --baseline "$snap" --inputs "$W/heldout-0.bin" --inputs "$W/heldout-1.bin" --out "$D/v10" >"$D/v10.log"
