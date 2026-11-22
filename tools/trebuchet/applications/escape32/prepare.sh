#!/usr/bin/env bash
# Acquire or build ESCape32's stock ESCAPE1 application and its matching loader.
set -euo pipefail
action=${1:?usage: prepare.sh fetch|build WORK}
work=${2:?usage: prepare.sh fetch|build WORK}
mkdir -p "$work"
cd "$work"

esc_rev=1d718c143380c3eb7e6581478990e1496632e5b8
lib_rev=a3977e14519e4cff8d1feb96445655465797908b
release_url=https://github.com/neoxic/ESCape32/releases/download/rel17
boot=BOOT2_PA2-rev4
app=ESCAPE1-rev17.3

check_source() {
    local tree=$1 expected=$2
    if [[ $(git -C "$tree" rev-parse HEAD) != "$expected" ]]; then
        echo "Unexpected revision in $tree (expected $expected)" >&2
        return 1
    fi
    if [[ -n $(git -C "$tree" status --porcelain) ]]; then
        echo "Source differs from pinned revision: $tree" >&2
        return 1
    fi
}

fetch_source() {
    local tree=$1 url=$2 revision=$3
    if [[ ! -e $tree ]]; then
        local stage
        stage=$(mktemp -d "./$tree.XXXXXXXX")
        git -C "$stage" init -q
        git -C "$stage" remote add origin "$url"
        git -C "$stage" fetch --depth=1 origin "$revision"
        git -C "$stage" checkout -q --detach FETCH_HEAD
        mv -T "$stage" "$tree"
    fi
    check_source "$tree" "$revision"
}

fetch_binary() {
    local name=$1 expected=$2
    if [[ ! -f release/$name.bin ]]; then
        local tmp
        tmp=$(mktemp release/download.XXXXXXXX)
        curl --fail --location --retry 2 --output "$tmp" "$release_url/$name.bin"
        echo "$expected  $tmp" | sha256sum --check
        mv "$tmp" "release/$name.bin"
    fi
    echo "$expected  release/$name.bin" | sha256sum --check
}

case "$action" in
fetch)
    fetch_source upstream https://github.com/neoxic/ESCape32.git "$esc_rev"
    fetch_source libopencm3 https://github.com/libopencm3/libopencm3.git "$lib_rev"
    mkdir -p release
    fetch_binary "$boot" bc0e49a782a622f966b1387ef117f338ef820489b80fe1c91a7d57bd15fed42a
    fetch_binary "$app" ca88d9c173f784717f6c75cebee4ed43ed4abb83756de8e065aa09e410a10604
    ;;
build)
    check_source upstream "$esc_rev"
    check_source libopencm3 "$lib_rev"
    make -C libopencm3 -j2 TARGETS=stm32/g0
    cmake -S upstream -B build -DLIBOPENCM3_DIR="$PWD/libopencm3"
    cmake --build build --target ESCAPE1 BOOT2_PA2 -j2
    # Keep relocation records in a separate ELF, then check the bytes that
    # would be flashed against the upstream-default build.
    cmake -S upstream -B build-metadata -DLIBOPENCM3_DIR="$PWD/libopencm3" \
        -DCMAKE_EXE_LINKER_FLAGS=-Wl,--emit-relocs
    cmake --build build-metadata --target ESCAPE1 -j2
    cmp "build/$app.bin" "build-metadata/$app.bin"
    arm-none-eabi-gcc --version
    cmake --version
    sha256sum "build/$app.bin" "build/$app.elf" "build/boot/$boot.bin" "build/boot/$boot.elf" "build-metadata/$app.elf"
    arm-none-eabi-readelf -A -r "build/$app.elf"
    ;;
*) echo "Unknown action: $action" >&2; exit 2 ;;
esac
