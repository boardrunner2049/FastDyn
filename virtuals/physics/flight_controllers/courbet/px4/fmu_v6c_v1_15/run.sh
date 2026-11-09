#!/usr/bin/env bash
set -euo pipefail
profile_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$profile_dir/../run_profile.sh" "$profile_dir" \
    "$profile_dir/../../bin/px4_fmu-v6c_v1.15.4.elf" "$@"
