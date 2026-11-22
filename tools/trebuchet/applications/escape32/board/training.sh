#!/usr/bin/env bash
# Usage: training.sh profile|optimize ESC_WORK BOARD_QEMU NEW_OUTPUT [PROFILE_DIR]
set -euo pipefail
action=${1:?}
work=$(realpath "${2:?}")
qemu=$(realpath "${3:?}")
out=$(realpath -m "${4:?}")
fb=$(realpath "${FIRMBOLT_REPO:?supply a private pinned FirmBOLT checkout}")
fbpy=${FIRMBOLT_PYTHON:-"$fb/.venv/bin/python"}
helpers="$fb/tools/emulator-profile"
adapter=$(cd "$(dirname "$0")" && pwd)
fd=$(cd "$adapter/../../../../.." && pwd)
elf="$work/build-metadata/ESCAPE1-rev17.3.elf"
bank=${ESCAPE1_BASE_BANK:-"$work/board-rebuild-final/flash.bin"}
mkdir "$out"
cp "$adapter/training-policy.json" "$out/"
case "$action" in
profile)
    # The private profiler's stop-PC flag exits QEMU before final wire checks.
    # Its normal-exit callback instead flushes after the full bench completes.
    python3 - "$helpers/firmbolt_tbprof.c" "$out/tbprof.c" <<'PY'
from pathlib import Path
import sys
src = Path(sys.argv[1]).read_text()
old = "QEMU_PLUGIN_EXPORT int qemu_plugin_install"
init = "edges=g_hash_table_new(ek_hash,ek_eq);"
if src.count(old) != 1 or src.count(init) != 1:
    raise ValueError("unexpected upstream TB profiler")
src = src.replace(old, "static void normal_exit(qemu_plugin_id_t id, void *opaque) { dump(); }\n"+old)
Path(sys.argv[2]).write_text(src.replace(init, "qemu_plugin_register_atexit_cb(id, normal_exit, NULL); "+init))
PY
    cc -O2 -shared -fPIC -o "$out/tbprof.so" "$out/tbprof.c" -I"$fd/third_party/common/qemu/include/qemu" \
        -I"$fd/third_party/common/qemu/include" $(pkg-config --cflags glib-2.0)
    python3 "$adapter/motor.py" "$qemu" "$bank" "$out/motor" --training-plugin "$out/tbprof.so"
    "$fbpy" "$helpers/tbprof2bolt.py" "$out/motor/session.tbprof" "$out/motor.preagg"
    "$fbpy" "$helpers/tbprof_report.py" "$elf" "$out/motor/session.tbprof" 20 > "$out/profile.txt"
    ;;
optimize)
    profile=$(realpath "${5:?supply the training profile directory}")
    backend=()
    if [[ -n ${FIRMBOLT_BOLT:-} ]]; then backend=(--bolt "$FIRMBOLT_BOLT"); fi
    for recipe in full narrow; do
        extra=()
        if [[ "$recipe" == narrow ]]; then extra=(-- '--funcs=^delayf/2'); fi
        timeout 300 "$fbpy" -m firmbolt --verbose "${backend[@]}" --cpu cortex-m0plus --emulator-profile "$profile/motor.preagg" \
            --new-code-address 0x08010000 --report "$out/$recipe.json" "$elf" -o "$out/$recipe.elf" \
            "${extra[@]}" > "$out/$recipe.log" 2>&1 || continue
        "$fbpy" "$adapter/guest.py" "$elf" "$out/$recipe.elf" "$bank" "$out/$recipe.bin" "$out/$recipe-layout.json"
        python3 "$adapter/motor.py" "$qemu" "$out/$recipe.bin" "$out/$recipe-cold"
        python3 "$adapter/motor.py" "$qemu" "$out/$recipe.bin" "$out/$recipe-dynamic" --training-plugin "$profile/tbprof.so"
        "$fbpy" "$adapter/guest.py" "$elf" "$out/$recipe.elf" "$bank" "$out/$recipe.bin" "$out/$recipe-executed.json" \
            --profile "$out/$recipe-dynamic/session.tbprof"
    done
    ;;
*) exit 2 ;;
esac
