#!/usr/bin/env bash
# Train exclusively on the published CNC drawing and execute the chosen image.
set -euo pipefail
work=$(cd "${1:?prepared grblHAL workspace}" && pwd)
qemu=$(realpath "${2:?BTT QEMU}")
plugin=$(realpath "${3:?FastDyn plugin}")
out=$(realpath -m "${4:?fresh output directory}")
fb=${FIRMBOLT_REPO:?supply the installed FirmBOLT source}
fbpy=${FIRMBOLT_PYTHON:-$fb/.venv/bin/python}
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../../.." && pwd)
mkdir "$out"
src="$fb/tools/emulator-profile/firmbolt_tbprof.c"
cc -O2 -shared -fPIC -DFIRMBOLT_TBPROF_SOURCE="\"$src\"" \
    "$here/../flight/release/training-profiler.c" -I"$root/third_party/common/qemu/include/qemu" \
    -I"$root/third_party/common/qemu/include" $(pkg-config --cflags glib-2.0) -o "$out/tbprof.so"
firmware="$work/release-historical/platformio/BTT_SKR_MINI_E3_V20/firmware.elf"
relocs="$work/release-historical/relocs/BTT_SKR_MINI_E3_V20/firmware.elf"
python3 "$here/run_board.py" "$qemu" "$firmware" "$work/OSHW_Logo.nc" "$out/training" \
    --profile-plugin "$out/tbprof.so" --timeout 800
"$fbpy" "$fb/tools/emulator-profile/tbprof2bolt.py" "$out/training/drawing.tbprof" "$out/train.preagg"
"$fbpy" "$fb/tools/emulator-profile/tbprof_report.py" "$firmware" "$out/training/drawing.tbprof" 20 > "$out/profile.txt"
"$fbpy" -m firmbolt --cpu cortex-m3 --emulator-profile "$out/train.preagg" --new-code-address 0x08038000 \
    --report "$out/firmbolt.json" "$relocs" -o "$out/firmware.elf" -- --funcs-no-regex=__mulsf3
python3 "$here/run_board.py" "$qemu" "$out/firmware.elf" "$work/OSHW_Logo.nc" "$out/qualified" \
    --fastdyn-plugin "$plugin" --profile-plugin "$out/tbprof.so" --timeout 800
"$fbpy" "$here/board/check_image.py" "$relocs" "$out/firmware.elf" "$out/firmbolt.json.layout.json" \
    "$out/qualified/drawing.tbprof" > "$out/checked-layout.json"
