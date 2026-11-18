#!/usr/bin/env bash
# B0 cold+warm smoke run of the minimal fixture with a given QEMU binary.
#
#   base_warm_smoke.sh <qemu-system-arm> <out_root>
#
# Two runs, each in its own fresh run directory under <out_root>:
#   cold: empty cache          -> must translate and append records
#   warm: sparse copy of cold's cache + manifest -> must append 0 records and
#         leave the .sc byte-identical (same inode, mtime and sha256), i.e. the
#         FastDyn fingerprint matched and the cache was not deleted/recreated
# Both must print the B0 guest-visible lines (from runs/b0/warm1.log) and exit 0.
#
# Every run directory has its own cache/, work/, RAM files and QMP socket.
# The rendered TOML must be byte-identical across runs (FastDyn hashes its
# content into the cache fingerprint), so it names the stable path
# <out_root>/current, a symlink that is re-pointed at each fresh run dir.
# QEMU runs under `setarch x86_64 -R` with a timeout; supercache_flat=false.
set -euo pipefail

if [[ $# -ne 2 ]]; then
    sed -n '2,13p' "$0" >&2
    exit 2
fi
qemu=$(realpath "$1")
out_root=$(realpath -m "$2")
root="${FASTDYN_ROOT:-$(cd "$(dirname "$0")/../../../.." && pwd)}"
fastdyn_src=$root/src/fastdyn
fastdyn_bin=$fastdyn_src/fastdyn-env/bin/fastdyn
template=$fastdyn_src/configs/b0-sc.toml
expected_log=$root/runs/b0/warm1.log
timeout_s=${SMOKE_TIMEOUT:-120}

[[ -x "$qemu" ]] || { echo "not executable: $qemu" >&2; exit 2; }
mkdir -p "$out_root"
if [[ -e "$out_root/cold" || -e "$out_root/warm" ]]; then
    echo "refusing to reuse $out_root (cold/ or warm/ exists); pick a fresh out_root" >&2
    exit 2
fi
cur=$out_root/current
toml=$out_root/b0-sc.toml

# Render the TOML once: only the run-dir paths change, all via $cur.
python3 - "$template" "$toml" "$cur" <<'EOF'
import sys
tpl, out, cur = sys.argv[1:4]
s = open(tpl).read()
old = "/data/extra_copy/fastdyn/runs/b0"
assert s.count(old) == 4, s.count(old)
s = s.replace(old + "/cache/minimal.sc", cur + "/cache/minimal.sc")
s = s.replace(old + "/", cur + "/")
assert old not in s
assert "supercache_flat = false" in s
open(out, "w").write(s)
EOF

record_count() {  # header record_count of a .sc, or "absent"
    python3 - "$1" <<'EOF'
import struct, sys, os
p = sys.argv[1]
if not os.path.exists(p):
    print("absent"); sys.exit(0)
with open(p, "rb") as f:
    h = f.read(80)
print(struct.unpack_from("<Q", h, 40)[0] if len(h) == 80 else "short")
EOF
}

file_id() {  # inode, mtime and sha256 of a file (proves a warm run kept the cache)
    if [[ -f "$1" ]]; then
        echo "$(stat -c '%i %Y.%y' "$1") $(sha256sum "$1" | cut -d' ' -f1)"
    else
        echo absent
    fi
}

norm() {  # guest-visible lines + frontend verdict lines
    grep -E '^(Current CPU type|Reg [0-9]+:|\[BENCH\]|qemu-system-arm:|fastdyn\.targets\.qemu_target\|INFO\|  QEMU exited|fastdyn\.core\|INFO\|  FastDyn Exit)' "$1" \
        | sed -E 's/^(\[BENCH\]\[[^]]*\]) elapsed_ns=.*/\1 elapsed=<host-time>/' || true
}

expected=$(norm "$expected_log")

run_one() {  # name [seed_cache_dir]
    local name=$1 seed=${2:-}
    local rd=$out_root/$name
    mkdir -p "$rd/cache"
    if [[ -n "$seed" ]]; then
        cp --sparse=always "$seed/minimal.sc" "$rd/cache/minimal.sc"
        cp "$seed/minimal.sc.fastdyn.json" "$rd/cache/minimal.sc.fastdyn.json"
    fi
    ln -sfn "$rd" "$cur"
    rm -f "$rd/ram-ram0.ram" "$rd/ram-ram1.ram" "$rd/qmp.sock"
    local pre; pre=$(record_count "$rd/cache/minimal.sc")
    local pre_id; pre_id=$(file_id "$rd/cache/minimal.sc")
    set +e
    (cd "$fastdyn_src" && env -u QEMU_SC_CAPTURE -u QEMU_SC_PROFILE -u QEMU_SC_PROFILE_PHASES \
        FASTDYN_QEMU="$qemu" setarch x86_64 -R timeout --signal=TERM --kill-after=10 "$timeout_s" \
        "$fastdyn_bin" run -c "$toml" -o "$rd/work") > "$rd/stdout.log" 2>&1
    local rc=$?
    set -e
    local post; post=$(record_count "$rd/cache/minimal.sc")
    local post_id; post_id=$(file_id "$rd/cache/minimal.sc")
    local untouched=no
    if [[ -n "$seed" && "$pre_id" == "$post_id" ]]; then untouched=yes; fi
    local bench; bench=$(grep -o 'elapsed_us=[0-9.]*' "$rd/stdout.log" | head -1 || true)
    norm "$rd/stdout.log" > "$rd/guest.norm"
    echo "$rc" > "$rd/exit_code"
    local same=no
    if [[ "$(cat "$rd/guest.norm")" == "$expected" ]]; then same=yes; fi
    local used; used=$(grep -o 'QEMU executable: [^ ]*' "$rd/stdout.log" | head -1 | cut -d' ' -f3)
    echo "$name: exit=$rc records_pre=$pre records_post=$post cache_untouched=$untouched guest_norm_matches_b0=$same $bench qemu=$used"
    echo "$name $rc $pre $post $same $untouched" >> "$out_root/summary.txt"
}

: > "$out_root/summary.txt"
sha256sum "$qemu" | tee "$out_root/qemu.sha256"
run_one cold
run_one warm "$out_root/cold/cache"
rm -f "$cur"

verdict=PASS
read -r _ c_rc c_pre c_post c_same _ < <(sed -n 1p "$out_root/summary.txt")
read -r _ w_rc w_pre w_post w_same w_untouched < <(sed -n 2p "$out_root/summary.txt")
[[ $c_rc == 0 && $c_pre == absent && $c_post =~ ^[0-9]+$ && $c_post -gt 0 && $c_same == yes ]] || verdict=FAIL
[[ $w_rc == 0 && $w_pre == "$c_post" && $w_post == "$c_post" && $w_same == yes && $w_untouched == yes ]] || verdict=FAIL
echo "verdict=$verdict (cold appended $c_post records; warm appended $((w_post - w_pre)))"
[[ $verdict == PASS ]]
