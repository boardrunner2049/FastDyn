#!/usr/bin/env bash
# WP22 unit smoke of the QEMU .sc v6 loader (qint-v6 binary).
#
#   v6_smoke.sh <qemu-system-arm (qint-v6)> <out_root>
#
# 1. v6_unit.py: v5 control, identity-v6 (empty extension table), guarded
#    synthetic file (SPEC links + variants, GENERAL slots), stale runtime
#    signature, and one malformed file per v6_synth.MUTATIONS entry
#    (fail loud before guest execution, file not truncated);
# 2. v6_gdb_diag.py: gdb -nx diag runs: variant execution count, tlb_flush
#    disarm (Q7), tb_flush reset and re-arm (Q6), HMP one-insn-per-tb (Q9).
# Each writes its verdict JSON under <out_root>; exit status 0 only if both PASS.
# Non-timing: never takes runs/.timing.lock.
set -euo pipefail
if [[ $# -ne 2 ]]; then sed -n '2,13p' "$0" >&2; exit 2; fi
qemu=$(realpath "$1")
out=$(realpath -m "$2")
here=$(cd "$(dirname "$0")" && pwd)
py=${PYTHON:-python3}
rc=0
"$py" "$here/v6_unit.py" --qemu "$qemu" --out "$out/v6-unit" || rc=1
"$py" "$here/v6_gdb_diag.py" --qemu "$qemu" --out "$out/v6-gdb-diag" || rc=1
echo "v6_smoke: $([[ $rc -eq 0 ]] && echo PASS || echo FAIL) ($out)"
exit $rc
