#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 2 ]]; then
    echo "usage: $0 PROFILE_DIR PX4_ELF [--shell] [FASTDYN OPTIONS...]" >&2
    exit 2
fi
profile_dir="$(cd -- "$1" && pwd)"
px4_elf="$(cd -- "$(dirname -- "$2")" && pwd)/$(basename -- "$2")"
shift 2
fastdyn_root="$(git -C "$profile_dir" rev-parse --show-toplevel)"
courbet_dir="$fastdyn_root/virtuals/physics/flight_controllers/courbet"
cd -- "$fastdyn_root"
python_bin="${FASTDYN_PYTHON:-$fastdyn_root/fastdyn-env/bin/python}"
fastdyn_bin="${FASTDYN_CLI:-$fastdyn_root/fastdyn-env/bin/fastdyn}"
"$python_bin" "$courbet_dir/prepare_execution_configuration.py" \
    --profile "$profile_dir/profile.toml"
ninja -C build
mkdir -p out
runtime_config="$(mktemp "$fastdyn_root/out/px4-runtime.XXXXXX.toml")"
cleanup() { rm -f -- "$runtime_config"; }
trap cleanup EXIT
"$python_bin" "$courbet_dir/px4/prepare_runtime_configuration.py" \
    --base configs/px4_fmu_v6c_fire_p2.toml \
    --output "$runtime_config" \
    --elf "$px4_elf" \
    --execution-config "$profile_dir/unlabeled_conf" \
    --boot "$profile_dir/boot.nsh"
if [[ "${1:-}" == --shell ]]; then
    shift
    # An empty override disables the default boot script in the TOML.
    export FASTDYN_BRIDGE_BOOT=""
    set -- --no-run-processes "$@"
fi
"$fastdyn_bin" run -c "$runtime_config" "$@"
