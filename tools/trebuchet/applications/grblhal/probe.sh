#!/usr/bin/env bash
# Cold reset diagnostic. The BTT bootloader normally hands off at 0x08007000.
set -euo pipefail
work=$(cd "${1:?supply the grblHAL work directory}" && pwd)
qemu=${2:?supply the patched qemu-system-arm}
out=${3:?supply a new output directory}
mkdir "$out"
image="$work/driver/.pio/build/BTT_SKR_MINI_E3_V20/firmware.elf"
sha256sum "$qemu" "$image" > "$out/images.sha256"
# Sample stopped CPUs. These are diagnostic snapshots, not a timing campaign.
{
    for sample in 1 2 3; do
        sleep 1
        printf 'stop\ninfo registers\ncont\n'
    done
    printf 'quit\n'
} | timeout -k 2 15 "$qemu" -M cortexm,memory-backend=ram0 -cpu cortex-m3 \
    -object memory-backend-ram,id=ram0,size=48K -m 48K \
    -global cortexm-soc.ram_baseaddr0=0x20000000 \
    -global armv7m.init-nsvtor=0x08007000 -kernel "$image" \
    -display none -serial none -monitor stdio -icount shift=0,align=off,sleep=off \
    -d in_asm -D "$out/boot.trace" > "$out/monitor.log" 2>&1
