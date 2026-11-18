#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
output="$repo_root/build/preflight/thumb-fop-benchmark"
toolchain=${ARM_NONE_EABI_PREFIX:-arm-none-eabi-}

mkdir -p "$output"

"${toolchain}gcc" \
  -mcpu=cortex-m3 -mthumb -nostdlib -g \
  -Wl,--emit-relocs \
  -Wl,-Map,"$output/thumb-fop-benchmark.map" \
  -T "$repo_root/tests/preflight/thumb_fop_benchmark.ld" \
  "$repo_root/tests/preflight/thumb_fop_benchmark.S" \
  -o "$output/thumb-fop-benchmark.elf"

"${toolchain}objdump" -d "$output/thumb-fop-benchmark.elf" \
  > "$output/thumb-fop-benchmark.disassembly"
"${toolchain}nm" -n "$output/thumb-fop-benchmark.elf" \
  > "$output/thumb-fop-benchmark.symbols"
