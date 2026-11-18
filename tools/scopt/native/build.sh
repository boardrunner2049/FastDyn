#!/usr/bin/env bash
# Build against the LLVM llvmorg-20.1.2 release archive and LLVM 20 dev libraries.
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 LLVM_PROJECT_SOURCE_ARCHIVE BUILD_ROOT" >&2
    exit 2
fi

readonly archive="$(realpath "$1")"
readonly root="$(realpath -m "$2")"
readonly native="$(dirname "$(realpath "$0")")"
readonly expected=f0a4a240aabc9b056142d14d5478bb6d962aeac549cbd75b809f5499240a8b38
readonly llvm_config="${LLVM_CONFIG:-llvm-config-20}"

actual=$(sha256sum "$archive" | cut -d' ' -f1)
if [[ "$actual" != "$expected" || "$("$llvm_config" --version)" != 20.1.2 ]]; then
    echo "This bridge requires the locked LLVM 20.1.2 archive and development libraries" >&2
    exit 2
fi

mkdir -p "$root"
source_tree="$root/source"
if [[ ! -d "$source_tree" ]]; then
    # Extract only components required to build BOLT against packaged LLVM.
    stage=$(mktemp -d "$root/source.XXXXXX")
    trap 'rm -rf -- "$stage"' EXIT
    tar -xJf "$archive" -C "$stage" --strip-components=1 --wildcards \
        '*/bolt/*' '*/cmake/*' '*/llvm/lib/Target/X86/*'
    printf '%s\n' "$expected" > "$stage/.source-sha256"
    mv "$stage" "$source_tree"
    trap - EXIT
fi
if [[ "$(cat "$source_tree/.source-sha256" 2>/dev/null || true)" != "$expected" ]]; then
    echo "Build root does not contain a verified source extraction" >&2
    exit 2
fi
for adaptation in bolt-cmake.patch bolt-inline-api.patch; do
    if ! patch -s --dry-run -R -d "$source_tree" -p1 < "$native/$adaptation" >/dev/null 2>&1; then
        patch -d "$source_tree" -p1 < "$native/$adaptation"
    fi
done

cmake -S "$source_tree/bolt" -B "$root/build" -G Ninja \
    -DLLVM_DIR="$("$llvm_config" --cmakedir)" \
    -DBOLT_TARGETS_TO_BUILD=X86 \
    -DBOLT_ENABLE_RUNTIME=OFF \
    -DLLVM_INCLUDE_TESTS=OFF \
    -DCMAKE_BUILD_TYPE=Release \
    -DSCOPT_NATIVE_SOURCE="$native"
cmake --build "$root/build" --target scopt-native --parallel "${BUILD_JOBS:-2}"
"$root/build/bin/scopt-native" --version
sha256sum "$root/build/bin/scopt-native" "$native"/bolt-*.patch
