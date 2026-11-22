#!/usr/bin/env bash
# Build the last BTT driver revision preceding the October 2025 aux-input change.
# Run prepare.sh once to fetch the pinned PlatformIO environment and published job.
set -euo pipefail
work=$(cd "${1:?supply the prepared grblHAL work directory}" && pwd)
mode=${2:-historical}
case "$mode" in
    historical) rev=127db7f9d811bd19270cbb48e81bb3fbfeedf53f ;;
    bound-fix) rev=e1f83889b6cfb9860d6e5462dbf86d67ef2324da ;;
    *) echo "choose historical or bound-fix" >&2; exit 2 ;;
esac
out="$work/release-$mode"
mkdir "$out"
git init -q "$out/driver"
git -C "$out/driver" remote add origin https://github.com/grblHAL/STM32F1xx.git
git -C "$out/driver" fetch --depth 1 origin "$rev"
git -C "$out/driver" checkout -q --detach FETCH_HEAD
git -C "$out/driver" submodule update --init --depth 1 --recursive --jobs 4
git -C "$out/driver" submodule status --recursive > "$out/submodules.txt"
here=$(cd "$(dirname "$0")" && pwd)
if [[ "$mode" == bound-fix ]]; then
    git -C "$out/driver/grbl" apply "$here/board/aux-bound.patch"
fi
python3 - "$out" "$mode" <<'PY'
from pathlib import Path
import sys
out, mode = Path(sys.argv[1]), sys.argv[2]
config = (out / "driver/platformio.ini").read_text()
pins = """platform = ststm32@20.0.0
platform_packages =
  platformio/toolchain-gccarmnoneeabi@1.70201.0
  platformio/framework-stm32cubef1@1.8.7
  platformio/tool-ldscripts-ststm32@0.3.0
  platformio/tool-scons@4.40801.0"""
config = config.replace("platform = ststm32\n", pins + "\n")
if mode == "historical":
    # Upstream 4d2c917 supplies the board_init implementation in boards/.
    original = "lib_deps =\n  grbl"
    assert config.count(original) == 1
    config = config.replace(original, "lib_deps =\n  boards\n  grbl")
(out / "platformio.ini").write_text(config)
(out / "relocs.ini").write_text(config.replace("-Wl,--defsym=LD_VECT_TAB_OFFSET=0x7000",
                                               "-Wl,--defsym=LD_VECT_TAB_OFFSET=0x7000\n  -Wl,--emit-relocs"))
PY
export PLATFORMIO_CORE_DIR="$work/platformio"
SOURCE_DATE_EPOCH=$(git -C "$out/driver" show -s --format=%ct HEAD)
export SOURCE_DATE_EPOCH
for variant in platformio relocs; do
    PLATFORMIO_BUILD_DIR="$out/$variant" "$work/toolenv/bin/pio" run -d "$out/driver" \
        -c "$out/$variant.ini" -e BTT_SKR_MINI_E3_V20 -j "${JOBS:-1}"
done
cmp "$out"/{platformio,relocs}/BTT_SKR_MINI_E3_V20/firmware.bin
sha256sum "$out"/{platformio,relocs}/BTT_SKR_MINI_E3_V20/firmware.{bin,elf}
