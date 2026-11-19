#!/usr/bin/env bash
set -euo pipefail

main() {
    here=$(cd "$(dirname "$0")" && pwd)
    if [[ ${1:-} == --verify-existing ]]; then
        python "$here/verify_program_run.py" "${2:?expected completed run directory}"
        return
    fi
    repo=$(git -C "$here" rev-parse --show-toplevel)
    cd "$repo"
    export FASTDYN_ROOT="$repo" SCOPT_FASTDYN_SRC="$repo"
    dest=$(realpath -m "${1:?expected fresh work directory}")
    if [[ -e $dest ]]; then
        echo "Choose a fresh work directory: $dest exists" >&2
        exit 2
    fi
    if [[ -n ${LLVM_PROJECT_SOURCE_ARCHIVE:-} ]]; then
        build_root="${SCOPT_NATIVE_BUILD_ROOT:-$dest-native}"
        tools/scopt/native/build.sh "$LLVM_PROJECT_SOURCE_ARCHIVE" "$build_root"
        export SCOPT_HOST_INLINE_BRIDGE="$build_root/build/bin/scopt-native"
    fi
    : "${SCOPT_HOST_INLINE_BRIDGE:?provide LLVM_PROJECT_SOURCE_ARCHIVE or a locked, built scopt-native}"
    reuse=()
    if [[ -n ${SCOPT_RUNTIME_BUILD_EVIDENCE:-} ]]; then
        reuse=(--reuse-build "$SCOPT_RUNTIME_BUILD_EVIDENCE")
    fi
    python "$here/bootstrap_host_inline.py" "${reuse[@]}" --source-lock tools/scopt/fixtures/host-inline/source-lock.json --work-dir "$dest"
    export SCOPT_RUNTIME_QEMU="$repo/third_party/common/qemu/build/qemu-system-arm"
    SCOPT_RUNTIME_QEMU_SHA256=$(sha256sum "$SCOPT_RUNTIME_QEMU" | cut -d" " -f1)
    export SCOPT_RUNTIME_QEMU_SHA256
    arms=(b3 b4 host-inline-splice frame compact-frame)
    params=()
    for arm in "${arms[@]}"; do
        "$here/run_program_arm.sh" "$dest" "$arm"
        params+=(--arm "$arm=$dest/whopr-$arm")
    done
    python "$here/measure_program.py" "$dest" "$dest/comparison" "${params[@]}"

    # Exercise a real specialization, including an independently reachable general entry.
    firmware="$dest/state-forward"
    qemu="$repo/third_party/common/qemu/build/qemu-system-arm"
    export SCOPT_LLVM_LAYOUT_BRIDGE="$SCOPT_HOST_INLINE_BRIDGE"
    python "$here/bootstrap_global_specialization.py" --fixture state-forward \
        --work-dir "$firmware" --qemu "$qemu" --plugin "$repo/build/libfastdyn.so"
    oracle=()
    if [[ -n ${SCOPT_QEMU_USER_ORACLE:-} ]]; then
        python "$here/hostsym_qemu_oracle.py" --qemu-x86 "$SCOPT_QEMU_USER_ORACLE" \
            --out "$firmware/hostsym-tcg.json"
        oracle=(--selftest "$firmware/hostsym-tcg.json")
    fi
    python "$here/compose_program.py" "$firmware" "$firmware/composed" "${oracle[@]}"
    python "$here/benchmark_passes.py" "$firmware" tools/scopt/pipelines/global-region-llvm.toml \
        "$firmware/pass-timings" --post-splice "$firmware/composed" --params "$firmware/composed/params.json" "${oracle[@]}"
    python "$here/verify_program_run.py" "$dest"
}

main "$@"; exit $?
