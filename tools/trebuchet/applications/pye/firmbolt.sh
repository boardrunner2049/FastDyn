#!/usr/bin/env bash
# Profile the declared training session or build and check its guest image.
set -euo pipefail
action=${1:?usage: firmbolt.sh profile|optimize WORK STAGE_JSON NEW_OUTPUT [TRAIN_OUTPUT]}
work=$(realpath "${2:?supply the prepared work directory}")
stage=$(realpath "${3:?supply the staged training case}")
out=$(realpath -m "${4:?supply a new output directory}")
adapter=$(cd "$(dirname "$0")" && pwd)
elf=$work/micropython/ports/qemu/build-MPS2_AN385/firmware-relocs.elf
fd=$(cd "$adapter/../../../.." && pwd)
fb=${FIRMBOLT_REPO:?supply the pinned FirmBOLT checkout}
qemu=${TREBUCHET_QEMU:-$fd/third_party/common/qemu/build/qemu-system-arm}
fbpy=${FIRMBOLT_PYTHON:-$fb/.venv/bin/python}
tools=$fb/tools/emulator-profile
mkdir "$out"

case $action in
profile)
    cc -O2 -shared -fPIC -o "$out/libtbprof.so" "$tools/firmbolt_tbprof.c" \
        -I"$fd/third_party/common/qemu/include/qemu" -I"$fd/third_party/common/qemu/include" \
        $(pkg-config --cflags glib-2.0)
    python3 "$adapter/cold.py" --stage "$stage" --qemu "$qemu" --elf "$elf" \
        --out "$out/run" --training-profile-plugin "$out/libtbprof.so"
    "$fbpy" "$tools/tbprof2bolt.py" "$out/run/session.tbprof" "$out/train.preagg"
    "$fbpy" "$tools/tbprof_report.py" "$elf" "$out/run/session.tbprof" 12 > "$out/profile.txt"
    ;;
optimize)
    placement=${TREBUCHET_PYE_CODE_ADDRESS:-0x80000}
    train=$(realpath "${5:?supply the profile output}")
    "$fbpy" "$adapter/guest_layout.py" "$elf" --new-code-address "$placement" > "$out/input-layout.json"
    # The full-image policy cannot publish several functions with observable
    # literal addresses. Training identifies strncmp as a hot cross-page callee.
    TIMEFORMAT='{"wall_seconds":%3R,"user_seconds":%3U,"system_seconds":%3S}'
    { time "$fbpy" -m firmbolt --verbose --cpu cortex-m3 --emulator-profile "$train/train.preagg" \
        --new-code-address "$placement" --report "$out/firmbolt.json" "$elf" \
        -o "$out/firmware.elf" -- --funcs-no-regex=strncmp > "$out/firmbolt.log" 2>&1; } \
        2> "$out/optimizer-cost.json"
    "$fbpy" "$adapter/guest_layout.py" "$elf" --new-code-address "$placement" --candidate "$out/firmware.elf" > "$out/layout.json"
    python3 "$adapter/cold.py" --stage "$stage" --qemu "$qemu" --elf "$out/firmware.elf" \
        --out "$out/cold"
    python3 "$adapter/cold.py" --stage "$stage" --qemu "$qemu" --elf "$out/firmware.elf" \
        --out "$out/diagnostic" --training-profile-plugin "$train/libtbprof.so"
    "$fbpy" "$adapter/guest_layout.py" "$elf" --new-code-address "$placement" --candidate "$out/firmware.elf" \
        --profile "$out/diagnostic/session.tbprof" > "$out/executed-layout.json"
    ;;
*) echo "Unknown action: $action" >&2; exit 2 ;;
esac
