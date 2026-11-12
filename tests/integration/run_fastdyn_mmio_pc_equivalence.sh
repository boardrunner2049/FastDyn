#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
qemu_root=${FASTDYN_QEMU_ROOT:-"$repo/third_party/common/qemu"}
build=${FASTDYN_QEMU_BUILD:-"$qemu_root/build"}
fixture="$repo/tests/integration/mmio_pc"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

cc -shared -fPIC -I"$qemu_root/include" $(pkg-config --cflags glib-2.0) \
  "$fixture/qemu_mmio_pc_probe.c" -o "$tmp/probe.so"

arm-none-eabi-as -mcpu=cortex-a15 "$fixture/arm.S" -o "$tmp/arm.o"
arm-none-eabi-ld -T "$repo/tests/integration/guest_code_fragments/a32.ld" \
  "$tmp/arm.o" -o "$tmp/arm.elf"
arm-none-eabi-as -mcpu=cortex-m4 "$fixture/cortex_m.S" -o "$tmp/cortex_m.o"
arm-none-eabi-ld -T "$repo/tests/integration/guest_code_fragments/thumb.ld" \
  "$tmp/cortex_m.o" -o "$tmp/cortex_m.elf"
llvm-mc -triple=aarch64-none-elf -filetype=obj "$fixture/aarch64.S" -o "$tmp/aarch64.o"
ld.lld -T "$repo/tests/integration/guest_code_fragments/a64.ld" \
  "$tmp/aarch64.o" -o "$tmp/aarch64.elf"
llvm-mc -triple=riscv32-none-elf -filetype=obj "$fixture/riscv.S" -o "$tmp/riscv32.o"
ld.lld -m elf32lriscv -T "$repo/tests/integration/guest_code_fragments/riscv.ld" \
  "$tmp/riscv32.o" -o "$tmp/riscv32.elf"
llvm-mc -triple=riscv64-none-elf -filetype=obj "$fixture/riscv.S" -o "$tmp/riscv64.o"
ld.lld -T "$repo/tests/integration/guest_code_fragments/riscv.ld" \
  "$tmp/riscv64.o" -o "$tmp/riscv64.elf"
nasm -f bin "$fixture/x86_16.asm" -o "$tmp/x86_16.bin"
nasm -f elf32 "$fixture/x86_32_64.asm" -o "$tmp/x86_32_64.o"
ld -m elf_i386 -T "$repo/tests/integration/guest_code_fragments/x86_multiboot.ld" \
  "$tmp/x86_32_64.o" -o "$tmp/x86_32_64.elf"

run_probe() {
  local name=$1
  shift
  set +e
  "$@" >"$tmp/$name.out" 2>"$tmp/$name.err"
  local status=$?
  set -e
  if ! grep -q 'PASS saved-pc equivalence' "$tmp/$name.out" "$tmp/$name.err" ||
     grep -q 'FAIL saved-pc equivalence' "$tmp/$name.out" "$tmp/$name.err"; then
    cat "$tmp/$name.out" "$tmp/$name.err" >&2
    echo "$name saved-PC equivalence failed (QEMU status $status)" >&2
    exit 1
  fi
  local report
  report=$(grep 'PASS saved-pc equivalence callbacks=' "$tmp/$name.out" "$tmp/$name.err" | tail -1)
  if [[ "$name" == cortex-m ]] && ! grep -Eq 'zero=[1-9][0-9]*' <<<"$report"; then
    cat "$tmp/$name.out" "$tmp/$name.err" >&2
    echo "$name did not exercise a valid zero saved PC" >&2
    exit 1
  fi
  echo "PASS $name saved-PC equivalence ($report)"
}

run_probe cortex-m "$build/qemu-system-arm" -M mps2-an386 -display none \
  -nodefaults -semihosting -kernel "$tmp/cortex_m.elf" -plugin "$tmp/probe.so"

# qtest performs the access outside a vCPU thread: current_cpu is NULL while
# first_cpu is the stopped Cortex-M CPU.  Both the production scalar path and
# the comparison register API must deliberately select that same first CPU.
set +e
printf 'readl 0x40004018\n' | timeout 2 \
  "$build/qemu-system-arm" -M mps2-an386 -S -display none -nodefaults \
    -qtest stdio -plugin "$tmp/probe.so" \
    >"$tmp/first-cpu.out" 2>"$tmp/first-cpu.err"
set -e
if ! grep -Eq 'PASS saved-pc equivalence callbacks=[1-9][0-9]* matches=[1-9][0-9]*' \
     "$tmp/first-cpu.out" "$tmp/first-cpu.err"; then
  cat "$tmp/first-cpu.out" "$tmp/first-cpu.err" >&2
  echo "first_cpu/non-vCPU saved-PC selection failed" >&2
  exit 1
fi
echo "PASS first_cpu selection from a non-vCPU caller"

run_probe aarch32 "$build/qemu-system-arm" -M virt -cpu cortex-a15 -display none \
  -nodefaults -semihosting -kernel "$tmp/arm.elf" -plugin "$tmp/probe.so"
run_probe aarch64 "$build/qemu-system-aarch64" -M virt -cpu cortex-a53 -display none \
  -nodefaults -semihosting -kernel "$tmp/aarch64.elf" -plugin "$tmp/probe.so"
run_probe riscv32 "$build/qemu-system-riscv32" -M virt -display none -nodefaults \
  -bios none -kernel "$tmp/riscv32.elf" -plugin "$tmp/probe.so"
run_probe riscv64 "$build/qemu-system-riscv64" -M virt -display none -nodefaults \
  -bios none -kernel "$tmp/riscv64.elf" -plugin "$tmp/probe.so"
run_probe x86-16 "$build/qemu-system-x86_64" -M pc -display none -nodefaults \
  -no-reboot -drive "file=$tmp/x86_16.bin,format=raw,if=floppy" \
  -debugcon file:/dev/null -device isa-debug-exit,iobase=0xf4,iosize=4 \
  -plugin "$tmp/probe.so"
run_probe x86-32-64 "$build/qemu-system-x86_64" -M pc -display none -nodefaults \
  -no-reboot -kernel "$tmp/x86_32_64.elf" -debugcon file:/dev/null \
  -device isa-debug-exit,iobase=0xf4,iosize=4 -plugin "$tmp/probe.so"
