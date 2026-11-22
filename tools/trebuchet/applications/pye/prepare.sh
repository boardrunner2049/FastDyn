#!/usr/bin/env bash
set -euo pipefail
action=${1:?usage: prepare.sh fetch|build WORK}
dst=${2:?usage: prepare.sh fetch|build WORK}
adapter=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$dst"
cd "$dst"

runtime_rev=78ff170de9e32c79db6e64d3e33d2bd60002bdcd
editor_rev=b64e20050416cd8770ca2cd6d1ffe736637da224

check() {
    if [[ $(git -C "$1" rev-parse HEAD) != "$2" || -n $(git -C "$1" status --porcelain --untracked-files=normal) ]]; then
        echo "Wrong revision or dirty checkout in $1" >&2
        exit 1
    fi
}

case $action in
fetch)
    if [[ ! -e micropython ]]; then
        git clone --depth 1 --branch v1.27.0 https://github.com/micropython/micropython.git micropython
    fi
    if [[ ! -e editor ]]; then
        git init -q editor
        git -C editor remote add origin https://github.com/robert-hh/Micropython-Editor.git
        git -C editor fetch --depth 1 origin "$editor_rev"
        git -C editor checkout -q --detach FETCH_HEAD
    fi
    check micropython "$runtime_rev"
    check editor "$editor_rev"
    git -C micropython submodule update --init --depth 1 lib/berkeley-db-1.xx lib/micropython-lib
    ;;
build)
    check micropython "$runtime_rev"
    check editor "$editor_rev"
    check micropython/lib/berkeley-db-1.xx 0f3bb6947c2f57233916dccd7bb425d7bf86e5a6
    check micropython/lib/micropython-lib 6ae440a8a144233e6e703f6759b7e7a0afaa37a4
    export SOURCE_DATE_EPOCH=1765291456
    make -j2 -C micropython/mpy-cross
    port=micropython/ports/qemu
    fw=build-MPS2_AN385/firmware
    make -j2 -C "$port" BOARD=MPS2_AN385 "$fw.elf"
    make -C "$port" -f Makefile -f "$adapter/relocs.mk" BOARD=MPS2_AN385 "$fw-relocs.elf"
    for suffix in '' '-relocs'; do
        arm-none-eabi-objcopy -O binary "$port/$fw$suffix.elf" "$port/$fw$suffix.bin"
    done
    cmp "$port/$fw.bin" "$port/$fw-relocs.bin"
    arm-none-eabi-gcc --version
    sha256sum "$port/$fw.elf" "$port/$fw-relocs.elf" "$port/$fw.bin" editor/pye.mpy
    ;;
*) echo "Unknown action: $action" >&2; exit 2 ;;
esac
