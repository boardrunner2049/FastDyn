#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
fixture="$repo/tests/integration/constant_pc"
qemu_bin=${FASTDYN_QEMU_BIN:-"$repo/third_party/common/qemu/build/qemu-system-arm"}
plugin_lib=${FASTDYN_PLUGIN_LIB:-"$repo/build/libfastdyn.so"}
fastdyn_bin=${FASTDYN_BIN:-"$repo/fastdyn-env/bin/fastdyn"}
tmp=$(mktemp -d)
if [[ -n ${FASTDYN_CONST_PC_KEEP:-} ]]; then
  echo "constant-PC artifacts: $tmp" >&2
else
  trap 'rm -rf "$tmp"' EXIT
fi

for tool in arm-none-eabi-as arm-none-eabi-ld nm; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 2; }
done
for path in "$qemu_bin" "$plugin_lib" "$fastdyn_bin"; do
  [[ -e "$path" ]] || { echo "missing required build artifact: $path" >&2; exit 2; }
done

build_firmware() {
  local mode=$1 iterations=$2
  arm-none-eabi-as -mcpu=$([[ $mode == a32 ]] && echo cortex-a15 || echo cortex-m4) \
    --defsym "ITERATIONS=$iterations" "$fixture/$mode.S" -o "$tmp/$mode.o"
  arm-none-eabi-ld -T "$fixture/$mode.ld" "$tmp/$mode.o" -o "$tmp/$mode.elf"
}

build_alias_firmware() {
  arm-none-eabi-as -mcpu=cortex-a9 "$fixture/alias.S" -o "$tmp/alias.o"
  arm-none-eabi-ld -T "$fixture/alias.ld" "$tmp/alias.o" -o "$tmp/alias.elf"
}

symbol() {
  printf '0x%s' "$(nm -n "$tmp/$1.elf" | awk -v name="$2" '$3 == name { print $1; exit }')"
}

write_config() {
  local mode=$1 tag=$2 log_options=$3
  local site target machine cpu memory base nsvtor
  site=$(symbol "$mode" redirect_site)
  target=$(symbol "$mode" redirect_target)
  if [[ $mode == a32 ]]; then
    machine=realview-pbx-a9; cpu=cortex-a9; memory=128M; base=0x0; nsvtor=""
  else
    machine=cortexm; cpu=cortex-m4; memory=512K; base=0x20000000
    nsvtor='init_nsvtor = "0x0"'
  fi
  cat >"$tmp/$tag.toml" <<EOF
[Memory]
[Memory.main]
id = "ram0"
base_address = "$base"
memory_size = "$memory"
backend = "ram"
share = false

[Machine]
platform = "generic-$machine"
qemu_path = "$qemu_bin"
enable_gdb = false
stop_on_start = false
monitor_port = 0
qmp_socket = "$tmp/$tag.sock"
semihosting = true
semihosting_config = "enable=on,target=native"
coverage = false
finline = "None"
log_options = "$log_options"
log_file = "$tag.qemu.log"

[CPU]
[[CPU.cpu0]]
arch = "arm"
machine = "$machine"
cpu = "$cpu"
plugin_library = "$plugin_lib"
binary = "$tmp/$mode.elf"
$nsvtor
twintrace = "None"

[[CPU.cpu0.modifiers]]
at = "$site"
patch = "r15 <- $target"
EOF
}

run_case() {
  local mode=$1 chain=$2 tag=$3 log_options=${4:-none}
  write_config "$mode" "$tag" "$log_options"
  if ! FASTDYN_CONST_PC_CHAIN="$chain" FASTDYN_CONST_PC_CHAIN_TRACE=1 \
      PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}" \
      "$fastdyn_bin" -q run -c "$tmp/$tag.toml" -o "$tmp/$tag-work" \
      >"$tmp/$tag.out" 2>"$tmp/$tag.err"; then
    cat "$tmp/$tag.out"
    cat "$tmp/$tag.err" >&2
    cat "$tmp/$tag-work/qemu.log" >&2 2>/dev/null || true
    return 1
  fi
  cat "$tmp/$tag.out"
  cat "$tmp/$tag.err" >&2
  grep -q "PASS constant-pc $mode checksum=iterations\*3" \
    "$tmp/$tag.out" "$tmp/$tag.err"
}

run_prepared() {
  local tag=$1 chain=$2 mode=$3
  local expected=${4:-"PASS constant-pc $mode checksum=iterations*3"}
  if ! FASTDYN_CONST_PC_CHAIN="$chain" FASTDYN_CONST_PC_CHAIN_TRACE=1 \
      PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}" \
      "$fastdyn_bin" -q run -c "$tmp/$tag.toml" -o "$tmp/$tag-work" \
      >"$tmp/$tag.out" 2>"$tmp/$tag.err"; then
    cat "$tmp/$tag.out"
    cat "$tmp/$tag.err" >&2
    return 1
  fi
  grep -Fq "$expected" "$tmp/$tag.out" "$tmp/$tag.err"
}

build_firmware a32 10000
build_firmware thumb 10000

run_case a32 1 a32-optimized
grep -q 'fastdyn-const-pc: promoted' "$tmp/a32-optimized.err"
run_case a32 0 a32-legacy
grep -q 'reason=disabled' "$tmp/a32-legacy.err"
echo "PASS A32 exact fixed redirect and legacy-switch equivalence"

run_case thumb 1 thumb-optimized
grep -q 'fastdyn-const-pc: promoted' "$tmp/thumb-optimized.err"
run_case thumb 0 thumb-legacy
grep -q 'reason=disabled' "$tmp/thumb-legacy.err"
echo "PASS Thumb exact fixed redirect and legacy-switch equivalence"

# A second immediate PC candidate is generated on the unreachable suffix but
# must not claim another slot after the first promotion.
write_config thumb thumb-second none
cat >>"$tmp/thumb-second.toml" <<EOF

[[CPU.cpu0.modifiers]]
at = "0x$(printf '%x' $(( $(symbol thumb redirect_site) + 2 )))"
patch = "r15 <- $(symbol thumb redirect_target)"
EOF
run_prepared thumb-second 1 thumb
grep -q 'reason=redirect-already-promoted' "$tmp/thumb-second.err"
echo "PASS one promoted redirect per TB; later modifier remains legacy and unreachable"

# Enter the ordinary target with the existing register-derived path, then put
# an immediate modifier on its two-way conditional terminator.  Both original
# slots are reserved, so this candidate must use the general exit.
build_firmware thumb 1
write_config thumb thumb-no-slot none
site=$(symbol thumb redirect_site)
target=$(symbol thumb redirect_target)
occupied_site=$(symbol thumb occupied_site)
occupied_target=$(symbol thumb occupied_target)
python3 - "$tmp/thumb-no-slot.toml" "$site" "$target" "$occupied_site" "$occupied_target" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
text = text.replace(
    f'at = "{sys.argv[2]}"\npatch = "r15 <- {sys.argv[3]}"',
    f'at = "{sys.argv[2]}"\npatch = "r15 <- r14"',
)
text += (
    "\n[[CPU.cpu0.modifiers]]\n"
    f'at = "{sys.argv[4]}"\n'
    f'patch = "r15 <- {sys.argv[5]}"\n'
)
path.write_text(text)
PY
run_prepared thumb-no-slot 1 thumb
grep -q 'reason=no-exit-slot' "$tmp/thumb-no-slot.err"
echo "PASS occupied original exit slots retain the legacy modifier path"

# A state-changing C callback before the modifier is intentionally
# unclassified by the injector and therefore keeps the general-exit path.
write_config thumb thumb-callback none
cat >>"$tmp/thumb-callback.toml" <<EOF

[[CPU.cpu0.virtuals]]
at = "$(symbol thumb redirect_site)"
instruction = "printreg"
args = ["0"]
EOF
run_prepared thumb-callback 1 thumb
grep -q 'reason=callback-overlap' "$tmp/thumb-callback.err"
echo "PASS unclassified callback overlap retains observable order and fallback"

# Capture IT state at the actual instruction translation point.  The PC
# modifier remains unconditional, but the chain optimization is rejected.
arm-none-eabi-as -mcpu=cortex-m4 "$fixture/thumb_it.S" -o "$tmp/thumb_it.o"
arm-none-eabi-ld -T "$fixture/thumb.ld" "$tmp/thumb_it.o" -o "$tmp/thumb_it.elf"
cp "$tmp/thumb-optimized.toml" "$tmp/thumb-it.toml"
it_site=$(symbol thumb_it it_redirect_site)
it_target=$(symbol thumb_it it_redirect_target)
python3 - "$tmp/thumb-it.toml" "$tmp/thumb_it.elf" "$tmp/thumb-it.sock" \
  "$it_site" "$it_target" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text()
text = re.sub(r'binary = ".*"', f'binary = "{sys.argv[2]}"', text)
text = re.sub(r'qmp_socket = ".*"', f'qmp_socket = "{sys.argv[3]}"', text)
text = re.sub(r'at = "0x[0-9a-fA-F]+"\npatch = "r15 <- 0x[0-9a-fA-F]+"',
              f'at = "{sys.argv[4]}"\npatch = "r15 <- {sys.argv[5]}"', text)
path.write_text(text)
PY
run_prepared thumb-it 1 thumb_it "PASS constant-pc IT fallback"
grep -q 'reason=target-state-or-address' "$tmp/thumb-it.err"
echo "PASS active ARM IT state retains the legacy modifier path"

# ARM CF_PCREL may reuse the original physical translation at another virtual
# page.  Warm and link the identity mapping, enter the same code through a
# section alias, and require the absolute destination to appear as the next
# dispatcher trace after the aliased source TB.
build_alias_firmware
cp "$tmp/a32-optimized.toml" "$tmp/a32-alias.toml"
alias_site=$(symbol alias alias_redirect_site)
alias_target=$(symbol alias alias_redirect_target)
alias_loop=$(symbol alias alias_loop)
python3 - "$tmp/a32-alias.toml" "$tmp/alias.elf" "$tmp/a32-alias.sock" \
  "$alias_site" "$alias_target" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text()
text = re.sub(r'binary = ".*"', f'binary = "{sys.argv[2]}"', text)
text = re.sub(r'qmp_socket = ".*"', f'qmp_socket = "{sys.argv[3]}"', text)
text = re.sub(r'log_options = ".*"', 'log_options = "exec"', text)
text = re.sub(r'log_file = ".*"', 'log_file = "a32-alias.qemu.log"', text)
text = re.sub(r'at = "0x[0-9a-fA-F]+"\npatch = "r15 <- 0x[0-9a-fA-F]+"',
              f'at = "{sys.argv[4]}"\npatch = "r15 <- {sys.argv[5]}"', text)
path.write_text(text)
PY
run_prepared a32-alias 1 alias "PASS constant-pc alias absolute-target fallback"
grep -q 'PASS constant-pc alias absolute-target fallback' \
  "$tmp/a32-alias.out" "$tmp/a32-alias.err"
alias_page=$(printf '%016x' $((alias_loop + 0x100000)))
target_pc=$(printf '%016x' "$alias_target")
python3 - "$tmp/a32-alias-work/a32-alias.qemu.log" "$alias_page" "$target_pc" <<'PY'
from pathlib import Path
import re
import sys

pcs = []
for line in Path(sys.argv[1]).read_text().splitlines():
    match = re.search(r'/([0-9a-fA-F]{16})/', line)
    if match:
        pcs.append(match.group(1).lower())
alias_pc, target_pc = sys.argv[2].lower(), sys.argv[3].lower()
if not any(pcs[i] == alias_pc and pcs[i + 1] == target_pc
           for i in range(len(pcs) - 1)):
    raise SystemExit(f"alias fallback dispatcher trace missing: {alias_pc} -> {target_pc}")
PY
echo "PASS PC-relative alias guard retains the absolute destination via fallback"

# Keep structural logs bounded: only translation is relevant for `op`, and a
# short loop is sufficient to cause QEMU to patch the outgoing jump.
build_firmware thumb 64
run_case thumb 1 thumb-structure op
slot=$(sed -n 's/.*fastdyn-const-pc: promoted .* slot=\([01]\).*/\1/p' \
  "$tmp/thumb-structure.err" | head -1)
[[ -n $slot ]]
op_log="$tmp/thumb-structure-work/thumb-structure.qemu.log"
grep -Eq "goto_tb.*\$$slot|goto_tb.*slot=$slot|goto_tb.*$slot" "$op_log"
grep -Eq 'exit_tb' "$op_log"
grep -Eq 'and_i32 .*pc,\$0x[f]+[c0]00' "$op_log"
grep -Fq 'st_i64 $0x0,env' "$op_log"
grep -Fq 'st8_i32 $0x1,env' "$op_log"
if grep -q 'plugin_cb' "$op_log"; then
  echo "unlowered plugin cleanup marker survived final TCG operations" >&2
  exit 1
fi

run_case thumb 1 thumb-link exec
link_log="$tmp/thumb-link-work/thumb-link.qemu.log"
grep -q 'Linking TBs .* index ' "$link_log"
echo "PASS matching generated chain operations, no nested cleanup marker, and live TB linking"

echo "PASS FastDyn constant-PC integration"
