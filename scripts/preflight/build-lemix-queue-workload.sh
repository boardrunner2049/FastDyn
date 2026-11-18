#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
freertos_root="$repo_root/third_party/preflight/freertos-clean/FreeRTOS"
kernel="$freertos_root/Source"
demo="$freertos_root/Demo/Posix_GCC"
source_file="$repo_root/tests/preflight/lemix_queue_workload.c"
output_root=${OUTPUT_ROOT:-"$repo_root/build/preflight/lemix-queue"}
compiler=${CC:-gcc}

sources=(
    "$source_file"
    "$kernel/event_groups.c"
    "$kernel/list.c"
    "$kernel/queue.c"
    "$kernel/stream_buffer.c"
    "$kernel/tasks.c"
    "$kernel/timers.c"
    "$kernel/portable/MemMang/heap_3.c"
    "$kernel/portable/ThirdParty/GCC/Posix/port.c"
    "$kernel/portable/ThirdParty/GCC/Posix/utils/wait_for_event.c"
)

includes=(
    -I"$demo"
    -I"$kernel/include"
    -I"$kernel/portable/ThirdParty/GCC/Posix"
    -I"$kernel/portable/ThirdParty/GCC/Posix/utils"
)

for level in O0 O3; do
    output_dir="$output_root/$level"
    mkdir -p "$output_dir"
    flags=(-g "-$level" -fno-omit-frame-pointer -pthread -no-pie
           -Wl,--emit-relocs -DprojCOVERAGE_TEST=0 -DprojENABLE_TRACING=0)
    "$compiler" "${includes[@]}" "${flags[@]}" "${sources[@]}" \
        -o "$output_dir/lemix_queue_workload"
done
