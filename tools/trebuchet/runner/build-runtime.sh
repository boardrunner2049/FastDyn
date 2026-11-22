#!/usr/bin/env bash
# Build the native pieces needed by Trebuchet's Cortex-M application runner on Ubuntu 24.04.
set -euo pipefail
root=$(git rev-parse --show-toplevel)
cd "$root"
git submodule update --init --depth 1 third_party/common/qemu
qemu=$root/third_party/common/qemu
mkdir -p "$qemu/build"
(cd "$qemu/build" && ../configure --target-list=arm-softmmu --enable-plugins --disable-docs && ninja -j"${BUILD_JOBS:-3}" qemu-system-arm)
mkdir -p build
meson setup build -Ddevice_models=true -Dqemu_path="$qemu"
ninja -C build -j"${BUILD_JOBS:-3}"
"$qemu/build/qemu-system-arm" --version
sha256sum "$qemu/build/qemu-system-arm" build/libfastdyn.so
