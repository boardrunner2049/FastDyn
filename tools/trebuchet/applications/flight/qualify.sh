#!/usr/bin/env bash
# Ubuntu 24.04 x86-64 hosted runner. The caller installs the listed native packages.
set -euo pipefail
family=${1:?usage: qualify.sh ardupilot|px4 NEW_WORK_DIR}
work=$(realpath -m "${2:?supply a new work directory outside the checkout}")
case "$family" in
    ardupilot) config=configs/copter462.toml ;;
    px4) config=configs/px4_fmu_v6c_fire_p2.toml ;;
    *) echo "unknown flight family: $family" >&2; exit 2 ;;
esac
cd "$(git rev-parse --show-toplevel)"
mkdir "$work"
setarch x86_64 -R true
export RUSTUP_TOOLCHAIN=nightly-2026-02-27
# Native host-only compiler, with the exact nightly pinned by Rumoca.
rustup toolchain install "$RUSTUP_TOOLCHAIN" --profile minimal
export CARGO_PROFILE_DEV_DEBUG=0 CARGO_INCREMENTAL=0
export CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-2}
git submodule update --init --depth 1 third_party/common/qemu third_party/common/rumoca \
    third_party/common/modelica_models third_party/courbet_deps/mavlink_headers \
    third_party/courbet_deps/fire_modelica_models
python3 utils/setup_fire_toolchain.py
if [[ ! -x fastdyn-env/bin/python ]]; then python3 -m venv fastdyn-env; fi
fastdyn-env/bin/python -m pip install -r requirements.txt -e src
qemu=$PWD/third_party/common/qemu
if [[ ! -x "$qemu/build/qemu-system-arm" ]]; then
    mkdir -p "$qemu/build"
    (cd "$qemu/build" && ../configure --target-list=arm-softmmu --enable-plugins --disable-docs &&
     ninja -j"$(nproc)" qemu-system-arm)
fi
# meson.build includes the source-relative "build" include directory.
mkdir -p build
meson setup "$work/plugin" -Ddevice_models=true -Denable_phy=true -Denable_fmu=true \
    -Denable_flight_controllers=true
ninja -C "$work/plugin"
adapter=tools/trebuchet/applications/flight/baseline.py
fastdyn-env/bin/python "$adapter" inspect "$family" "$work/inspection"
fastdyn-env/bin/python utils/build_fmi3_fmu.py --skip-submodules --config "$config"
fastdyn-env/bin/python "$adapter" run "$family" "$work/mission" \
    --plugin "$work/plugin/libfastdyn.so" --timeout 600 --ordinary-warm \
    --warm-attempts "${FLIGHT_WARM_ATTEMPTS:-2}" --frozen-preparation
