#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
qemu_root=${FASTDYN_QEMU_ROOT:-"$repo/third_party/common/qemu"}
build=${FASTDYN_QEMU_BUILD:-"$qemu_root/build"}
x86_bin=${FASTDYN_QEMU_BIN:-"$build/qemu-system-x86_64"}
arm_bin=${FASTDYN_QEMU_BIN_ARM:-"$build/qemu-system-arm"}
a64_bin=${FASTDYN_QEMU_BIN_AARCH64:-"$build/qemu-system-aarch64"}
rv32_bin=${FASTDYN_QEMU_BIN_RISCV32:-"$build/qemu-system-riscv32"}
rv64_bin=${FASTDYN_QEMU_BIN_RISCV64:-"$build/qemu-system-riscv64"}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

nasm -f bin "$repo/tests/integration/guest_code_fragments/x86_16_boot.asm" -o "$tmp/boot.bin"
arm-none-eabi-as -mcpu=cortex-a15 "$repo/tests/integration/guest_code_fragments/a32.S" -o "$tmp/a32.o"
arm-none-eabi-ld -T "$repo/tests/integration/guest_code_fragments/a32.ld" "$tmp/a32.o" -o "$tmp/a32.elf"
arm-none-eabi-as -mcpu=cortex-a15 "$repo/tests/integration/guest_code_fragments/thumb_a.S" -o "$tmp/thumb_a.o"
arm-none-eabi-ld -T "$repo/tests/integration/guest_code_fragments/a32.ld" "$tmp/thumb_a.o" -o "$tmp/thumb_a.elf"
arm-none-eabi-as -mcpu=cortex-a15 "$repo/tests/integration/guest_code_fragments/thumb_it.S" -o "$tmp/thumb_it.o"
arm-none-eabi-ld -T "$repo/tests/integration/guest_code_fragments/a32.ld" "$tmp/thumb_it.o" -o "$tmp/thumb_it.elf"
arm-none-eabi-as -mcpu=cortex-m4 "$repo/tests/integration/guest_code_fragments/thumb.S" -o "$tmp/thumb.o"
arm-none-eabi-ld -T "$repo/tests/integration/guest_code_fragments/thumb.ld" "$tmp/thumb.o" -o "$tmp/thumb.elf"
llvm-mc -triple=aarch64-none-elf -filetype=obj "$repo/tests/integration/guest_code_fragments/a64.S" -o "$tmp/a64.o"
ld.lld -T "$repo/tests/integration/guest_code_fragments/a64.ld" "$tmp/a64.o" -o "$tmp/a64.elf"
llvm-mc -triple=aarch64-none-elf -filetype=obj \
  "$repo/tests/integration/guest_code_fragments/a64_stress_fragment.S" \
  -o "$tmp/a64_stress_fragment.o"
[[ $(readelf -S --wide "$tmp/a64_stress_fragment.o" | awk '$3 == ".text" { print $7 }') == 000190 ]] || {
  echo "A64 stress fragment is not exactly 100 fixed-width instructions" >&2
  exit 1
}
llvm-mc -triple=riscv32-none-elf -filetype=obj "$repo/tests/integration/guest_code_fragments/riscv.S" -o "$tmp/rv32.o"
ld.lld -m elf32lriscv -T "$repo/tests/integration/guest_code_fragments/riscv.ld" "$tmp/rv32.o" -o "$tmp/rv32.elf"
llvm-mc -triple=riscv32-none-elf -filetype=obj "$repo/tests/integration/guest_code_fragments/riscv_c.S" -o "$tmp/rv32c.o"
ld.lld -m elf32lriscv -T "$repo/tests/integration/guest_code_fragments/riscv.ld" "$tmp/rv32c.o" -o "$tmp/rv32c.elf"
llvm-mc -triple=riscv64-none-elf -filetype=obj "$repo/tests/integration/guest_code_fragments/riscv.S" -o "$tmp/rv64.o"
ld.lld -T "$repo/tests/integration/guest_code_fragments/riscv.ld" "$tmp/rv64.o" -o "$tmp/rv64.elf"
nasm -f elf32 "$repo/tests/integration/guest_code_fragments/x86_32_64.asm" -o "$tmp/x86_32_64.o"
ld -m elf_i386 -T "$repo/tests/integration/guest_code_fragments/x86_multiboot.ld" \
  "$tmp/x86_32_64.o" -o "$tmp/x86_32_64.elf"
for fixture in order retranslation fault exit; do
  nasm -f elf32 "$repo/tests/integration/guest_code_fragments/x86_${fixture}.asm" \
    -o "$tmp/x86_${fixture}.o"
  ld -m elf_i386 -T "$repo/tests/integration/guest_code_fragments/x86_multiboot.ld" \
    "$tmp/x86_${fixture}.o" -o "$tmp/x86_${fixture}.elf"
done
nasm -f elf32 "$repo/tests/integration/guest_code_fragments/x86_boundary.asm" \
  -o "$tmp/x86_boundary.o"
ld -m elf_i386 -T "$repo/tests/integration/guest_code_fragments/x86_boundary.ld" \
  "$tmp/x86_boundary.o" -o "$tmp/x86_boundary.elf"
cc -shared -fPIC -I"$qemu_root/include" $(pkg-config --cflags glib-2.0) \
  "$repo/tests/integration/guest_code_fragments/qemu_fragment_probe.c" \
  -o "$tmp/probe.so"

set +e
pc32=0x$(nm -n "$tmp/x86_32_64.elf" | awk '/fragment_site32/{print $1}')
pc64=0x$(nm -n "$tmp/x86_32_64.elf" | awk '/fragment_site64/{print $1}')
"$x86_bin" -machine pc -display none -nodefaults -no-reboot \
  -drive "file=$tmp/boot.bin,format=raw,if=floppy" \
  -device isa-debug-exit,iobase=0xf4,iosize=0x04 \
  -plugin "$tmp/probe.so",mode=x86_16,pc=0x7c03,stress=on >"$tmp/stdout" 2>"$tmp/stderr"
status=$?
set -e
if [[ $status -ne 1 ]]; then
  cat "$tmp/stderr" >&2
  echo "x86-16 fragment smoke failed: QEMU exit $status (expected debug-exit 1)" >&2
  exit 1
fi
echo "PASS x86-16: original MOV consumed the result of 100 inserted instructions"

set +e
"$x86_bin" -machine pc -display none -nodefaults -no-reboot \
  -kernel "$tmp/x86_32_64.elf" \
  -device isa-debug-exit,iobase=0xf4,iosize=0x04 \
  -plugin "$tmp/probe.so",mode=x86_all,pc="$pc32",pc64="$pc64",stress=on
status=$?
set -e
[[ $status -eq 1 ]] || { echo "x86-32/64 fragment smoke failed: $status" >&2; exit 1; }
echo "PASS x86-32 and x86-64: protected- and long-mode sites consumed fragment results"

run_x86_fixture() {
  local fixture=$1 mode=$2 pc=$3 expected=${4:-1} extra=${5:-}
  set +e
  "$x86_bin" -machine pc -display none -nodefaults -no-reboot \
    -kernel "$tmp/x86_${fixture}.elf" \
    -device isa-debug-exit,iobase=0xf4,iosize=0x04 \
    -plugin "$tmp/probe.so,mode=$mode,pc=$pc${extra:+,$extra}" \
    >"$tmp/x86_${fixture}.out" 2>"$tmp/x86_${fixture}.err"
  status=$?
  set -e
  [[ $status -eq $expected ]] || {
    cat "$tmp/x86_${fixture}.err" >&2
    echo "x86 $fixture failed: exit $status, expected $expected" >&2
    exit 1
  }
}

order_pc=0x$(nm -n "$tmp/x86_order.elf" | awk '/fragment_site_order/{print $1}')
run_x86_fixture order x86_order "$order_pc" 1
echo "PASS x86 registration order"

retranslation_pc=0x$(nm -n "$tmp/x86_retranslation.elf" | awk '/fragment_site_retranslation/{print $1}')
run_x86_fixture retranslation x86_32 "$retranslation_pc" 1 account=on
grep -q 'FASTDYN_ACCOUNT original_handles=2 original_size=2' "$tmp/x86_retranslation.err"
echo "PASS x86 repeated execution, SMC-driven retranslation, and original plugin accounting"

boundary_pc=0x$(nm -n "$tmp/x86_boundary.elf" | awk '/fragment_site_boundary/{print $1}')
run_x86_fixture boundary x86_32 "$boundary_pc" 1
echo "PASS x86 page-boundary original-decode rollback"

fault_pc=0x$(nm -n "$tmp/x86_fault.elf" | awk '/fragment_site_fault/{print $1}')
run_x86_fixture fault x86_fault "$fault_pc" 1
echo "PASS x86 fault restoration after fragment lazy-flag change"

run_x86_fixture exit registry_validation 0 1
grep -q 'combined site limits exceeded' "$tmp/x86_exit.err"
echo "PASS registry malformed-input, address, count, byte, and combined-limit validation"

run_x86_fixture exit late_validation 0 1
grep -q 'FASTDYN_LATE_REGISTRATION rejected=1' "$tmp/x86_exit.err"
echo "PASS initialization-only registry sealing"

"$arm_bin" -M virt -cpu cortex-a15 -display none -nodefaults -semihosting \
  -kernel "$tmp/a32.elf" -plugin "$tmp/probe.so",mode=a32,pc=0x40000004,stress=on \
  >"$tmp/a32.out" 2>"$tmp/a32.err"
grep -q 'PASS a32' "$tmp/a32.err" "$tmp/a32.out"
echo "PASS A32 Cortex-A"

"$arm_bin" -M virt -cpu cortex-a15 -display none -nodefaults -semihosting \
  -kernel "$tmp/thumb_a.elf" -plugin "$tmp/probe.so",mode=thumb,pc=0x4000000a,stress=on \
  >"$tmp/thumb_a.out" 2>"$tmp/thumb_a.err"
grep -q 'PASS thumb-a' "$tmp/thumb_a.err" "$tmp/thumb_a.out"
echo "PASS Thumb Cortex-A"

set +e
bash -c 'ulimit -c 0; "$@"; status=$?; exit "$status"' bash \
  "$arm_bin" -M virt -cpu cortex-a15 -display none -nodefaults \
  -kernel "$tmp/thumb_it.elf" \
  -plugin "$tmp/probe.so",mode=thumb,pc=0x4000000a,stress=on \
  >"$tmp/thumb_it.out" 2>"$tmp/thumb_it.err"
status=$?
set -e
[[ $status -ne 0 ]] || { echo "Thumb IT-site fragment was not rejected" >&2; exit 1; }
grep -q 'IT instruction or active IT/ECI/ICI state' "$tmp/thumb_it.err"
echo "PASS Thumb IT-site placement rejection"

"$arm_bin" -M mps2-an386 -display none -nodefaults -semihosting \
  -kernel "$tmp/thumb.elf" -plugin "$tmp/probe.so",mode=thumb,pc=0xa,stress=on \
  >"$tmp/thumb.out" 2>"$tmp/thumb.err"
grep -q 'PASS thumb' "$tmp/thumb.err" "$tmp/thumb.out"
echo "PASS Thumb Cortex-M"

set +e
"$a64_bin" -M virt -cpu cortex-a53 -display none -nodefaults -semihosting \
  -kernel "$tmp/a64.elf" -plugin "$tmp/probe.so",mode=a64,pc=0x40000004,stress=on \
  >"$tmp/a64.out" 2>"$tmp/a64.err"
status=$?
set -e
grep -q 'PASS a64' "$tmp/a64.err" "$tmp/a64.out"
[[ $status -eq 1 ]] || { echo "A64 semihost exit changed: $status" >&2; exit 1; }
echo "PASS A64"

"$rv32_bin" -M virt -display none -nodefaults -bios none -kernel "$tmp/rv32.elf" \
  -plugin "$tmp/probe.so",mode=rv32,pc=0x80000004,stress=on
echo "PASS RV32"
"$rv32_bin" -M virt -cpu rv32 -display none -nodefaults -bios none \
  -kernel "$tmp/rv32c.elf" -plugin "$tmp/probe.so",mode=rv32_c,pc=0x80000004
echo "PASS RV32 compressed form with C enabled"
set +e
bash -c 'ulimit -c 0; "$@"; status=$?; exit "$status"' bash \
  "$rv32_bin" -M virt -cpu rv32,c=false -display none -nodefaults -bios none \
  -kernel "$tmp/rv32c.elf" -plugin "$tmp/probe.so",mode=rv32_c,pc=0x80000004 \
  >"$tmp/rv32c_off.out" 2>"$tmp/rv32c_off.err"
status=$?
set -e
[[ $status -ne 0 ]] || { echo "RV32 compressed fragment was accepted with C disabled" >&2; exit 1; }
grep -q 'compressed extension is disabled' "$tmp/rv32c_off.err"
echo "PASS RV32 compressed form rejected with C disabled"
"$rv64_bin" -M virt -display none -nodefaults -bios none -kernel "$tmp/rv64.elf" \
  -plugin "$tmp/probe.so",mode=rv64,pc=0x80000004,stress=on
echo "PASS RV64"
