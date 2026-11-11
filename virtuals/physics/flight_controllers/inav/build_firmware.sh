#!/usr/bin/env bash
# Prepare a separate source checkout and build the INAV firmware for review.
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)
source_dir=${1:-"$repo_root/out/inav-source"}
revision=d0c611e040887940226c9536bc84a3795ef9e800
if [[ -e "$source_dir" ]]; then
    echo "Refusing to overwrite existing source directory: $source_dir" >&2
    exit 1
fi
mkdir -p "$source_dir"
git -C "$source_dir" init
git -C "$source_dir" remote add origin https://github.com/iNavFlight/inav.git
git -C "$source_dir" fetch --depth 1 origin "$revision"
git -C "$source_dir" checkout --detach FETCH_HEAD
git -C "$source_dir" apply "$repo_root/virtuals/physics/flight_controllers/inav/inav.patch"
# Avoid generating CMake projects for hundreds of unrelated boards.
python3 - "$source_dir/src/main/target/CMakeLists.txt" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text()
old = 'if (EXISTS ${entry})'
if s.count(old) != 1:
    raise SystemExit('unexpected INAV target discovery; refusing to modify it')
p.write_text(s.replace(old, 'if (EXISTS ${entry} AND subdir MATCHES "/MATEKF405$")'))
PY
cmake -S "$source_dir" -B "$source_dir/build-fastdyn" \
    -DCOMPILER_VERSION_CHECK=OFF -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    '-DCMAKE_C_FLAGS=-DCONFIG_IN_RAM -DEEPROM_SIZE=16384 -DUSE_FAKE_BARO -DUSE_FAKE_MAG -DUSE_GPS_FAKE -DFASTDYN_REHOST -DDEFAULT_RX_TYPE=RX_TYPE_MSP'
cmake --build "$source_dir/build-fastdyn" --target MATEKF405 -j "${FASTDYN_BUILD_JOBS:-2}"
echo "Built $source_dir/build-fastdyn/bin/MATEKF405.elf"
echo 'Review its DWARF and all hook addresses before updating the profile and checksum.'
