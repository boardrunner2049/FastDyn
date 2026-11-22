#!/usr/bin/env bash
# Build the board's release configuration plus a retained-relocations sibling.
set -euo pipefail
family=${1:?usage: build.sh ardupilot|px4 NEW_WORK_DIR}
work=$(realpath -m "${2:?missing new work directory}")
root=$(git rev-parse --show-toplevel)
tools=$root/tools/trebuchet/applications/flight/release
case "$family" in
    ardupilot)
        url=https://github.com/ArduPilot/ardupilot.git
        tag=Copter-4.6.2
        commit=1ebd4d996e8b6bbeb1462a9655c00aa861d5beef
        export FLIGHT_LINK_NAME=arducopter
        final=build/CubeBlack/bin/arducopter
        ;;
    px4)
        url=https://github.com/PX4/PX4-Autopilot.git
        tag=v1.17.0
        commit=d6f12ad1c4f70ad3230afd7d86e971421e02fef4
        export FLIGHT_LINK_NAME=px4_fmu-v6c_default.elf
        final=build/px4_fmu-v6c_default/px4_fmu-v6c_default.elf
        ;;
    *) echo "unknown family: $family" >&2; exit 2 ;;
esac
mkdir "$work"
export FLIGHT_RELEASE_OUTPUT=$work/artifacts
mkdir "$FLIGHT_RELEASE_OUTPUT" "$work/bin"
if [[ $family == ardupilot ]]; then
    archive=gcc-arm-none-eabi-10-2020-q4-major-x86_64-linux.tar.bz2
    curl --fail --retry 2 -LsS "https://firmware.ardupilot.org/Tools/STM32-tools/$archive" -o "$work/$archive"
    echo "21134caa478bbf5352e239fbc6e2da3038f8d2207e089efc96c3b55f1edcd618  $work/$archive" | sha256sum -c
    tar -xjf "$work/$archive" -C "$work"
    export PATH="$work/gcc-arm-none-eabi-10-2020-q4-major/bin:$PATH"
fi
export FLIGHT_REAL_TOOLCHAIN
FLIGHT_REAL_TOOLCHAIN=$(dirname "$(command -v arm-none-eabi-gcc)")
for driver in gcc g++ ld; do ln -s "$tools/compiler" "$work/bin/arm-none-eabi-$driver"; done
arm-none-eabi-gcc --version > "$FLIGHT_RELEASE_OUTPUT/compiler.txt"
arm-none-eabi-ld --version > "$FLIGHT_RELEASE_OUTPUT/linker.txt"
export PATH="$work/bin:$PATH"
git clone --depth 1 --branch "$tag" "$url" "$work/source"
cd "$work/source"
test "$(git rev-parse HEAD)" = "$commit"
git rev-parse HEAD > "$FLIGHT_RELEASE_OUTPUT/source-revision.txt"
# Waf checks include directories even for modules a target may leave unused.
if [[ $family == ardupilot ]]; then
    git submodule update --init --recursive --depth 1 --jobs 4
else
    git submodule update --init --recursive --depth 1 --jobs 4 src platforms/nuttx
fi
python3 -m venv "$work/venv"
source "$work/venv/bin/activate"
python -m pip install 'setuptools<81' wheel pyelftools==0.32 capstone==5.0.7
python -m pip install --no-deps "$root/src"
if [[ $family == ardupilot ]]; then
    python -m pip install 'empy==3.3.4' pexpect future lxml pyserial pymavlink dronecan intelhex \
        jinja2 pyyaml pyparsing psutil packaging
    # --debug-symbols adds DWARF while leaving the upstream release optimization active.
    ./waf configure --board CubeBlack --debug-symbols
    ./waf -j"$(nproc)" copter
else
    python -m pip install 'setuptools<81' -r Tools/setup/requirements.txt
    make -j"$(nproc)" px4_fmu-v6c_default
fi
cp "$final" "$FLIGHT_RELEASE_OUTPUT/normal.elf"
git submodule status --recursive > "$FLIGHT_RELEASE_OUTPUT/submodules.txt"
python -m pip freeze > "$FLIGHT_RELEASE_OUTPUT/python-packages.txt"
python "$tools/compare.py" "$root" "$family" "$FLIGHT_RELEASE_OUTPUT"
