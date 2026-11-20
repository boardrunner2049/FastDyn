#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository="$(cd -- "${script_dir}/../.." && pwd)"
bolt_source="${repository}/third_party/preflight/llvm-project"
patch_path="${repository}/patches/bolt-fastdyn-preflight.patch"
expected_revision="7864c0736a8a09670dfbfc513f1e9649799f2486"

if [[ "$(git -C "${bolt_source}" rev-parse HEAD)" != "${expected_revision}" ]]; then
    printf 'BOLT checkout is not at %s\n' "${expected_revision}" >&2
    exit 1
fi

header_source="${repository}/tools/preflight/bolt/FastDynPatchContracts.h"
implementation_source="${repository}/tools/preflight/bolt/FastDynPatchContracts.cpp"
header_target="${bolt_source}/bolt/include/bolt/Passes/FastDynPatchContracts.h"
implementation_target="${bolt_source}/bolt/lib/Passes/FastDynPatchContracts.cpp"

if ! cmp -s "${header_source}" "${header_target}"; then
    printf 'BOLT header mirror differs from the canonical overlay\n' >&2
    exit 1
fi
if ! cmp -s "${implementation_source}" "${implementation_target}"; then
    printf 'BOLT implementation mirror differs from the canonical overlay\n' >&2
    exit 1
fi

temporary_patch="$(mktemp)"
cleanup() {
    git -C "${bolt_source}" reset -q -- \
        bolt/include/bolt/Core/ARMUnwindInfo.h \
        bolt/include/bolt/Passes/FastDynPatchContracts.h \
        bolt/lib/Core/ARMUnwindInfo.cpp \
        bolt/lib/Passes/FastDynPatchContracts.cpp \
        bolt/lib/Target/ARM \
        bolt/test/ARM \
        lld/test/ELF/arm-thumbv8_1m-branch-future.s \
        llvm/test/ExecutionEngine/JITLink/AArch32/ELF_thumb_data_pointers.s \
        llvm/test/ExecutionEngine/JITLink/AArch32/ELF_relocations_thumbv8_1m.s \
        2>/dev/null || true
    rm -f -- "${temporary_patch}"
}
trap cleanup EXIT

# Intent-to-add makes the complete overlay appear in the mechanical diff while
# retaining it as an untracked working-tree file after generation.
git -C "${bolt_source}" add -N -- \
    bolt/include/bolt/Core/ARMUnwindInfo.h \
    bolt/include/bolt/Passes/FastDynPatchContracts.h \
    bolt/lib/Core/ARMUnwindInfo.cpp \
    bolt/lib/Passes/FastDynPatchContracts.cpp \
    bolt/lib/Target/ARM \
    bolt/test/ARM \
    lld/test/ELF/arm-thumbv8_1m-branch-future.s \
    llvm/test/ExecutionEngine/JITLink/AArch32/ELF_thumb_data_pointers.s \
    llvm/test/ExecutionEngine/JITLink/AArch32/ELF_relocations_thumbv8_1m.s

# The Cortex-M backend crosses BOLT Core/Rewrite/Target plus LLVM's AArch32
# JITLink relocation layer.  Diff the complete pinned checkout so the
# deliverable cannot silently omit a required common-infrastructure change.
LC_ALL=C git -C "${bolt_source}" diff --full-index --binary \
    >"${temporary_patch}"

if [[ ! -s "${temporary_patch}" ]]; then
    printf 'generated BOLT extension patch is empty\n' >&2
    exit 1
fi

install -m 0644 "${temporary_patch}" "${patch_path}"
sha256sum "${patch_path}"
