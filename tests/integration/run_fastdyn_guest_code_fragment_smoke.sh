#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
qemu_root=${FASTDYN_QEMU_ROOT:-"$repo/third_party/common/qemu"}
qemu_bin=${FASTDYN_QEMU_BIN:-"$qemu_root/build/qemu-system-arm"}
plugin_lib=${FASTDYN_PLUGIN_LIB:-"$repo/build/libfastdyn.so"}
fastdyn_bin=${FASTDYN_BIN:-"$repo/fastdyn-env/bin/fastdyn"}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/plugin/host" "$tmp/plugin/runtime"
cp "$repo/tests/plugins/guest_code_fragments/host/preprocessor.py" "$tmp/plugin/host/"
cc -Wall -Wextra -Werror -shared -fPIC \
  -I"$repo/include" -I"$qemu_root/include" \
  $(pkg-config --cflags glib-2.0) \
  "$repo/tests/plugins/guest_code_fragments/runtime/guest_code_fragments.c" \
  -L"$(dirname "$plugin_lib")" -lfastdyn \
  -Wl,-rpath,"$(dirname "$plugin_lib")" \
  -o "$tmp/plugin/runtime/guest_code_fragments.so"

arm-none-eabi-as -mcpu=cortex-m4 \
  "$repo/tests/integration/guest_code_fragments/thumb_external.S" \
  -o "$tmp/thumb.o"
arm-none-eabi-ld -T "$repo/tests/integration/guest_code_fragments/thumb.ld" \
  "$tmp/thumb.o" -o "$tmp/thumb.elf"

cat >"$tmp/config.toml" <<EOF
[Memory]
[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size = "512K"
backend = "ram"
share = false

[Machine]
platform = "generic-cortexm"
qemu_path = "$qemu_bin"
enable_gdb = false
stop_on_start = false
monitor_port = 0
qmp_socket = "$tmp/qmp.sock"
semihosting = true
semihosting_config = "enable=on,target=native"
coverage = false
finline = "None"

[CPU]
[[CPU.cpu0]]
arch = "arm"
machine = "cortexm"
cpu = "cortex-m4"
plugin_library = "$plugin_lib"
binary = "$tmp/thumb.elf"
init_nsvtor = "0x0"
twintrace = "None"

[CPU.cpu0.plugins.guest_code_fragments]
enabled = true
source = "$tmp/plugin"
profile = "thumb"
pc = 10

[[CPU.cpu0.virtuals]]
at = "0xa"
instruction = "printreg"
args = ["0"]
EOF

PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$fastdyn_bin" -q run -c "$tmp/config.toml" -o "$tmp/work" \
  >"$tmp/stdout" 2>"$tmp/stderr"
cat "$tmp/stdout"
cat "$tmp/stderr" >&2
grep -q '^PASS fastdyn external fragment$' "$tmp/stdout" "$tmp/stderr"
grep -q '^Reg 0:0x00000001$' "$tmp/stdout" "$tmp/stderr"
echo "PASS FastDyn external-plugin initialization and public fragment API"
echo "PASS same-site observer ordering: observer, fragment, original instruction"

cp "$tmp/config.toml" "$tmp/conflict.toml"
cat >>"$tmp/conflict.toml" <<'EOF'

[[CPU.cpu0.modifiers]]
at = "0xa"
patch = "r15 <- 0"
EOF
set +e
PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$fastdyn_bin" -q run -c "$tmp/conflict.toml" -o "$tmp/conflict-work" \
  >"$tmp/conflict.out" 2>"$tmp/conflict.err"
status=$?
set -e
cat "$tmp/conflict.err" >&2
cat "$tmp/conflict-work/qemu.log" >&2 || true
[[ $status -ne 0 ]] || { echo "same-site PC modifier was not rejected" >&2; exit 1; }
grep -q 'conflicts with a program-counter modifier' "$tmp/conflict.err"
echo "PASS same-site control-changing modifier rejection"

sed '/^\[\[CPU.cpu0.virtuals\]\]/,$d' "$tmp/config.toml" \
  >"$tmp/callback-conflict.toml"
cat >>"$tmp/callback-conflict.toml" <<'EOF'

[[CPU.cpu0.virtuals]]
at = "0xa"
instruction = "benchmark_end"
args = ["conflict"]
EOF
set +e
PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$fastdyn_bin" -q run -c "$tmp/callback-conflict.toml" \
  -o "$tmp/callback-conflict-work" \
  >"$tmp/callback-conflict.out" 2>"$tmp/callback-conflict.err"
status=$?
set -e
cat "$tmp/callback-conflict.err" >&2
[[ $status -ne 0 ]] || { echo "same-site state-changing callback was not rejected" >&2; exit 1; }
grep -q 'conflicts with an unclassified or state-changing virtual callback' \
  "$tmp/callback-conflict.err"
echo "PASS same-site unclassified/state-changing callback rejection"

sed 's/profile = "thumb"/profile = "a64"/' "$tmp/config.toml" \
  >"$tmp/unavailable.toml"
set +e
PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$fastdyn_bin" -q run -c "$tmp/unavailable.toml" \
  -o "$tmp/unavailable-work" \
  >"$tmp/unavailable.out" 2>"$tmp/unavailable.err"
status=$?
set -e
cat "$tmp/unavailable.err" >&2
[[ $status -ne 0 ]] || { echo "unavailable A64 profile was accepted by ARM FastDyn" >&2; exit 1; }
grep -q "encoding profile is unavailable on QEMU target 'arm'" \
  "$tmp/unavailable.err"
echo "PASS unavailable-profile rejection and setup failure propagation"
