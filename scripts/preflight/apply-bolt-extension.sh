#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository="$(cd -- "${script_dir}/../.." && pwd)"
bolt_source="${repository}/third_party/preflight/llvm-project"
expected_revision="7864c0736a8a09670dfbfc513f1e9649799f2486"

if [[ "$(git -C "${bolt_source}" rev-parse HEAD)" != "${expected_revision}" ]]; then
    printf 'BOLT checkout is not at %s\n' "${expected_revision}" >&2
    exit 1
fi

if git -C "${bolt_source}" apply --check \
    "${repository}/patches/bolt-fastdyn-preflight.patch" 2>/dev/null; then
    git -C "${bolt_source}" apply "${repository}/patches/bolt-fastdyn-preflight.patch"
elif ! git -C "${bolt_source}" apply --reverse --check \
    "${repository}/patches/bolt-fastdyn-preflight.patch" 2>/dev/null; then
    printf 'BOLT integration patch is neither applicable nor already applied\n' >&2
    exit 1
fi

git -C "${bolt_source}" status --short
