#!/usr/bin/env bash
# WP4 real-data check: one cold capture run of the minimal fixture, then parse
# the .scm with the WP4 reader.
#
#   scm_capture_parse.sh <qemu-system-arm with scc capture> <out_root> [slice_slots]
#
# The run gets a fresh run directory <out_root>/cold (own cache/, work/, RAM
# files), QEMU_SC_CAPTURE=<out_root>/cold/capture.scm, `setarch x86_64 -R`,
# a timeout, supercache_flat=false (from configs/b0-sc.toml). Then:
#   scopt meta dump --strict   (exit 1 on any rejected chunk or a missing END)
#   scopt meta merge-check     (single file: sanity)
#   slot binding summary vs. the .sc (record bytes + code_fnv64 per slot)
# and, if slice_slots is given (e.g. 0-7), writes <out_root>/slice.scm.
set -euo pipefail

if [[ $# -lt 2 ]]; then
    sed -n '2,14p' "$0" >&2
    exit 2
fi
qemu=$(realpath "$1")
out_root=$(realpath -m "$2")
slots=${3:-}
root=/data/extra_copy/fastdyn
fastdyn_src=$root/src/fastdyn
fastdyn_bin=$fastdyn_src/fastdyn-env/bin/fastdyn
template=$fastdyn_src/configs/b0-sc.toml
py=$root/tools-env/bin/python
here=$(cd "$(dirname "$0")/../.." && pwd)   # scopt checkout holding this script
timeout_s=${SMOKE_TIMEOUT:-180}

[[ -x "$qemu" ]] || { echo "not executable: $qemu" >&2; exit 2; }
if [[ -e "$out_root/cold" ]]; then
    echo "refusing to reuse $out_root; pick a fresh out_root" >&2
    exit 2
fi
rd=$out_root/cold
mkdir -p "$rd/cache"
toml=$out_root/b0-sc.toml
python3 - "$template" "$toml" "$rd" <<'EOF'
import sys
tpl, out, rd = sys.argv[1:4]
s = open(tpl).read()
old = "/data/extra_copy/fastdyn/runs/b0"
assert s.count(old) == 4, s.count(old)
s = s.replace(old + "/", rd + "/")
assert old not in s and "supercache_flat = false" in s
open(out, "w").write(s)
EOF
sha256sum "$qemu" | tee "$out_root/qemu.sha256"
scm=$rd/capture.scm
set +e
(cd "$fastdyn_src" && env -u QEMU_SC_PROFILE -u QEMU_SC_PROFILE_PHASES \
    QEMU_SC_CAPTURE="$scm" FASTDYN_QEMU="$qemu" \
    setarch x86_64 -R timeout --signal=TERM --kill-after=10 "$timeout_s" \
    "$fastdyn_bin" run -c "$toml" -o "$rd/work") > "$rd/stdout.log" 2>&1
rc=$?
set -e
echo "fastdyn exit=$rc"
grep -m2 'supercache-capture' "$rd/stdout.log" || true
[[ -f "$scm" ]] || { echo "no .scm produced" >&2; exit 1; }

cd "$here"
set +e
"$py" -m scopt meta dump --strict "$scm" > "$out_root/scm.summary.txt"
dump_rc=$?
set -e
cat "$out_root/scm.summary.txt"
"$py" -m scopt meta dump --jsonl --no-arrays "$scm" > "$out_root/scm.jsonl"
"$py" - "$scm" "$rd/cache/minimal.sc" <<'EOF'
import sys
from scopt.canon import fnv1a64
from scopt.formats.layouts import SC_CODE_OFFSET
from scopt.formats.sc_v5 import ScHeader, ScRecord
from scopt.formats.scm_v1 import read_scm
scm = read_scm(sys.argv[1])
with open(sys.argv[2], "rb") as f:
    h = ScHeader.from_bytes(f.read(80))
    ri = scm.run_info
    hdr_ok = (ri.code_address, ri.code_size, ri.prologue_end, ri.runtime_signature,
              ri.tb_struct_size) == (h.code_address, h.code_size, h.prologue_end,
                                     h.runtime_signature, h.tb_size)
    bound = 0
    for slot, tb in scm.tbs.items():
        f.seek(h.records_offset + 104 * slot)
        rec = f.read(104)
        r = ScRecord.from_bytes(rec)
        f.seek(SC_CODE_OFFSET + r.tc_offset)
        code = f.read(r.end_offset - r.tc_offset)
        bound += (tb.record == rec and tb.code_fnv64 == fnv1a64(code)
                  and fnv1a64(tb.guest_bytes) == r.guest_hash)
print(f"sc records={h.record_count} scm tbs={len(scm.tbs)} run_info_matches_header={hdr_ok} "
      f"slots_bound={bound}")
EOF
if [[ -n "$slots" ]]; then
    "$py" -m scopt meta slice --slots "$slots" "$scm" "$out_root/slice.scm"
fi
echo "meta dump --strict exit=$dump_rc"
exit $dump_rc
