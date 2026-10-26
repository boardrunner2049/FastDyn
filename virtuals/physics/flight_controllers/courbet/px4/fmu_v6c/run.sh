#!/usr/bin/env bash
set -euo pipefail
profile_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fastdyn_root="$(cd -- "$profile_dir/../../../../../.." && pwd)"
cd -- "$fastdyn_root"
python_bin="${FASTDYN_PYTHON:-$fastdyn_root/fastdyn-env/bin/python}"
fastdyn_bin="${FASTDYN_CLI:-$fastdyn_root/fastdyn-env/bin/fastdyn}"
"$python_bin" "$profile_dir/../../prepare_execution_configuration.py" \
    --profile "$profile_dir/profile.toml"
ninja -C build
if [[ "${1:-}" == --shell ]]; then
    shift
    # An empty override disables the default boot script in the TOML.
    export FASTDYN_BRIDGE_BOOT=""
    set -- --no-run-processes "$@"
fi
exec "$fastdyn_bin" run -c configs/px4_fmu_v6c_fire_p2.toml "$@"
