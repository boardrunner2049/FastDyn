#!/usr/bin/env bash
# On Ubuntu 24.04 with build dependencies and FastDyn/scopt Python environments.
# Work and results must be separate directories. Run serially on a fixed CPU set.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
fd=$(cd "$here/../../../.." && pwd)
work=$(realpath -m "${1:?warm-smoke.sh WORK RESULTS}")
results=$(realpath -m "${2:?warm-smoke.sh WORK RESULTS}")
mkdir -p "$work" "$results"
qsrc="$fd/third_party/common/qemu"
qemu="$qsrc/build/qemu-system-arm"
scopt="$fd/scopt-env/bin/scopt"
python="$fd/scopt-env/bin/python"
app="$work/product"
run="$work/ordinary"
export FASTDYN_ROOT="$work/scopt"
export SCOPT_FASTDYN_SRC="$fd"
unset SCOPT_RAM_ROOT # on-disk sparse private banks are valid; avoid the runner's small /dev/shm
export PATH="$fd/fastdyn-env/bin:$PATH"
mkdir -p "$run"
if [[ -e "$run/prep" ]]; then echo "Use a fresh WORK for another run" >&2; exit 2; fi
setarch x86_64 -R true
test "$(git -C "$qsrc" rev-parse HEAD)" = 957530a637cf5e4ca38df4f7aec3f967031bd0e5
patchfile="$here/qemu-mps2-psram-priority.patch"
echo "fbcf80089dbfa1d414af9e68e56bd6328bf30447ac50da57676b31545964757b  $patchfile" | sha256sum -c -
if git -C "$qsrc" apply --check "$patchfile"; then git -C "$qsrc" apply "$patchfile"; fi
echo "64dfbc65548b59a64e49711627837409236ca48b50c97672b2c53784f7043d19  $qsrc/hw/arm/mps2.c" | sha256sum -c -
if [[ ! -f "$qsrc/build/build.ninja" ]]; then
    mkdir -p "$qsrc/build"
    (cd "$qsrc/build" && ../configure --target-list=arm-softmmu --enable-plugins --disable-docs)
fi
ninja -C "$qsrc/build" -j"${BUILD_JOBS:-2}" qemu-system-arm
"$python" "$here/board_check.py" "$qemu" --expect fixed --out "$results/board.json"
plugin_dir="$work/plugin-release"
meson setup "$plugin_dir" "$fd" -Dqemu_path="$qsrc" -Ddevice_models=true -Drelease=true
ninja -C "$plugin_dir" -j"${BUILD_JOBS:-2}"
plugin="$plugin_dir/libfastdyn.so"
if [[ ${WARM_BUILD_ONLY:-0} = 1 ]]; then
    sha256sum "$qemu" "$plugin" "$patchfile" > "$results/binaries.sha256"
    exit 0
fi
"$here/prepare.sh" fetch "$app"
"$here/prepare.sh" build "$app" > "$results/app-build.log" 2>&1
elf="$app/micropython/ports/qemu/build-MPS2_AN385/firmware.elf"
case_id=update-package-version
args=(--work "$app" --run "$run" --elf "$elf" --qemu "$qemu" --plugin "$plugin" --case "$case_id")
runner=("$python" "$here/fastdyn_runner.py")
"${runner[@]}" config "${args[@]}" --out "$results/config.json"
cp "$run/pye.toml" "$results/pye.toml"
sha256sum "$qemu" "$elf" "$plugin" "$patchfile" > "$results/binaries.sha256"
git -C "$fd" rev-parse HEAD > "$results/fastdyn.commit"
git -C "$qsrc" rev-parse HEAD > "$results/qemu.commit"
taskset -pc $$ > "$results/affinity.txt"
uname -a > "$results/host.txt"
# Keep compact raw logs and proof documents on either success or failure. Exclude
# ELF, RAM, caches, captures and generated zero-filled input staging files.
finish() {
    find "$run" -type f \( -name 'stdout*.log' -o -name '*stdout.log' -o -name 'run*.json' \
      -o -name 'prep.json' -o -name 'deploy.json' -o -name 'proof.json' -o -name 'result.json' \
      -o -name 'uart.bin' -o -name 'returned.utf8' -o -name 'fastdyn_timing.jsonl' -o -name 'profile_run.json' \
      -o -name '*-peer.log' -o -name 'stage.json' \) -size -5M -printf '%P\0' \
      | tar --directory="$run" --null -T - -czf "$results/observations.tgz"
}
trap finish EXIT
# Two predeclared captures of the same training job allow its conditional device paths to be retained.
"${runner[@]}" capture "${args[@]}" --case "$case_id" --out "$run/prep"
"$scopt" bundle identity -c "$run/prep/run.toml" --manifest "$run/prep/cache/fw.sc.fastdyn.json" --out "$run/bundle.json"
"$scopt" bundle runtime-identity --run-dir "$run/prep/oracle" --sc "$run/prep/cache/fw.sc" \
    --scm "$run/prep/capture.scm" --out "$run/runtime.json"
"$scopt" import "$run/prep" --bundle "$run/bundle.json" --runtime-identity "$run/runtime.json" \
    --store "$run/store" --no-progress > "$results/import.log"
snapshots=("$run"/store/snapshots/*)
test "${#snapshots[@]}" = 1 && test -d "${snapshots[0]}"
# Each probe uses a fresh deployment of the imported snapshot.
"${runner[@]}" profile "${args[@]}" --snapshot "${snapshots[0]}" --out "$run/profile"
"${runner[@]}" warm "${args[@]}" --snapshot "${snapshots[0]}" --out "$run/warm"
cp "$run/warm/run.json" "$results/ordinary-warm.json"
cp "$run/warm/run-peer/result.json" "$results/ordinary-warm-application.json"
