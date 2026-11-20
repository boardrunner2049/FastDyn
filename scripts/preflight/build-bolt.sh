#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository="$(cd -- "${script_dir}/../.." && pwd)"
source_dir="${repository}/third_party/preflight/llvm-project"
build_dir="${repository}/third_party/preflight/llvm-build"

"${script_dir}/apply-bolt-extension.sh"
cmake -S "${source_dir}/llvm" -B "${build_dir}" -G Ninja \
    -UBOLT_CLANG_EXE -UBOLT_LLD_EXE \
    -DCMAKE_BUILD_TYPE=Release \
    -DLLVM_ENABLE_ASSERTIONS=ON \
    -DLLVM_ENABLE_ZLIB=FORCE_ON \
    -DLLVM_ENABLE_ZSTD=FORCE_ON \
    -DLLVM_ENABLE_PROJECTS='bolt;clang;lld' \
    -DLLVM_TARGETS_TO_BUILD='X86;AArch64;RISCV;ARM'
ninja -C "${build_dir}" llvm-bolt llvm-bat-dump llvm-dwarfdump llvm-objcopy \
    llvm-readelf llvm-dwp clang lld
"${build_dir}/bin/llvm-bolt" --version
test -x "${build_dir}/bin/llvm-bat-dump"
test -x "${build_dir}/bin/clang"
test -x "${build_dir}/bin/clang++"
test -x "${build_dir}/bin/ld.lld"
