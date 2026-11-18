#!/usr/bin/env bash
set -euo pipefail

# Recover the exact source inputs used by the Student 1 preflight work without
# vendoring either research tool into FastDyn.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository="$(cd -- "${script_dir}/../.." && pwd)"
tools_dir="${repository}/third_party/preflight"
bolt_revision="7864c0736a8a09670dfbfc513f1e9649799f2486"
stoke_revision="98d8a0f028f2daf2052bfe607dbc32ec8d55ba9e"
qemu_revision="99b54836ded8dc3271eba52deee7453efc0a13b9"

mkdir -p "${tools_dir}"

if [[ ! -d "${tools_dir}/llvm-project/.git" ]]; then
    git clone --filter=blob:none --no-checkout \
        https://github.com/llvm/llvm-project.git "${tools_dir}/llvm-project"
fi
git -C "${tools_dir}/llvm-project" fetch --depth 1 origin "${bolt_revision}"
git -C "${tools_dir}/llvm-project" checkout --detach "${bolt_revision}"

if [[ ! -d "${tools_dir}/stoke/.git" ]]; then
    git clone https://github.com/StanfordPL/stoke.git "${tools_dir}/stoke"
fi
git -C "${tools_dir}/stoke" fetch --depth 1 origin "${stoke_revision}"
git -C "${tools_dir}/stoke" checkout --detach "${stoke_revision}"
git -C "${tools_dir}/stoke" submodule update --init --recursive

git -C "${repository}" submodule update --init --depth 1 third_party/common/qemu
if [[ "$(git -C "${repository}/third_party/common/qemu" rev-parse HEAD)" != \
    "${qemu_revision}" ]]; then
    printf 'QEMU checkout is not at %s\n' "${qemu_revision}" >&2
    exit 1
fi

printf 'BOLT %s\n' "$(git -C "${tools_dir}/llvm-project" rev-parse HEAD)"
printf 'STOKE %s\n' "$(git -C "${tools_dir}/stoke" rev-parse HEAD)"
printf 'QEMU %s\n' "$(git -C "${repository}/third_party/common/qemu" rev-parse HEAD)"
