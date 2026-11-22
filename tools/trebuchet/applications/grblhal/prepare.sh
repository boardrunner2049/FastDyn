#!/usr/bin/env bash
# Build the upstream ARM release and native simulator with their shared core.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
work=$(mkdir -p "${1:?supply a working directory}" && cd "$1" && pwd)
cd "$work"

checkout() {
    local dir=$1 repo=$2 ref=$3
    if [[ ! -e "$dir" ]]; then
        git init -q "$dir"
        git -C "$dir" remote add origin "$repo"
        git -C "$dir" fetch --depth 1 origin "$ref"
        git -C "$dir" checkout -q --detach FETCH_HEAD
    fi
    [[ $(git -C "$dir" rev-parse HEAD) == "$ref" ]] || { echo "Unexpected revision in $dir" >&2; exit 1; }
    if [[ -n $(git -C "$dir" status --porcelain --untracked-files=no --ignore-submodules=none) ]]; then
        echo "Local changes in $dir" >&2
        exit 1
    fi
    git -C "$dir" submodule update --init --recursive --depth 1
}

checkout driver https://github.com/grblHAL/STM32F1xx.git e1f83889b6cfb9860d6e5462dbf86d67ef2324da
checkout simulator https://github.com/grblHAL/Simulator.git bfbb08fea98f0131db5a5e33287330638d3d6c92
curl --fail --location --silent --show-error \
    https://raw.githubusercontent.com/bdring/DrawBot_Badge/3a02d0541c2c60a1747d8ce8d9c7996ca49383f0/SourceFiles/GCode/OSHW_Logo.nc \
    -o OSHW_Logo.nc
echo 'b3aa0a1ac047b669fd6788ecd8283dc71fd3f885fb477eaa1a1272706e849ab3  OSHW_Logo.nc' | sha256sum -c -

if [[ ! -x toolenv/bin/python ]]; then python3 -m venv toolenv; fi
if ! toolenv/bin/python -c 'from importlib.metadata import version; assert version("platformio") == "6.1.18"'; then
    toolenv/bin/python -m pip install platformio==6.1.18
fi
export PLATFORMIO_CORE_DIR="$work/platformio"
# Pin the resolved upstream packages while preserving the board and all its flags.
sed '/^platform = ststm32$/c\platform = ststm32@20.0.0\nplatform_packages =\n  platformio/toolchain-gccarmnoneeabi@1.70201.0\n  platformio/framework-stm32cubef1@1.8.7\n  platformio/tool-ldscripts-ststm32@0.3.0\n  platformio/tool-scons@4.40801.0' \
    driver/platformio.ini > "$work/platformio-pinned.ini"
SOURCE_DATE_EPOCH=$(git -C driver show -s --format=%ct HEAD)
export SOURCE_DATE_EPOCH
toolenv/bin/pio run -d driver -c "$work/platformio-pinned.ini" -e BTT_SKR_MINI_E3_V20 -j "${JOBS:-1}"
# FirmBOLT requires linker relocation records. Preserve both release and metadata ELFs.
sed '/-Wl,--defsym=LD_VECT_TAB_OFFSET=0x7000/a\  -Wl,--emit-relocs' "$work/platformio-pinned.ini" > "$work/platformio-relocs.ini"
PLATFORMIO_BUILD_DIR="$work/metadata-build" toolenv/bin/pio run -d driver -c "$work/platformio-relocs.ini" -e BTT_SKR_MINI_E3_V20 -j "${JOBS:-1}"
cmp driver/.pio/build/BTT_SKR_MINI_E3_V20/firmware.bin metadata-build/BTT_SKR_MINI_E3_V20/firmware.bin
SOURCE_DATE_EPOCH=$(git -C simulator show -s --format=%ct HEAD)
cmake -S simulator -B simulator/trace-release -DCMAKE_PROJECT_INCLUDE="$here/simulator-trace.cmake"
cmake --build simulator/trace-release --parallel "${JOBS:-1}" --target grblHAL_sim
sha256sum driver/.pio/build/BTT_SKR_MINI_E3_V20/firmware.{elf,bin} metadata-build/BTT_SKR_MINI_E3_V20/firmware.{elf,bin} simulator/trace-release/grblHAL_sim
