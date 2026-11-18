#!/usr/bin/env bash
set -euo pipefail

# CMake compiler launcher used only for the survey's mixed-level firmware
# reproductions.  The project's own -Os remains visible in the command line;
# this final option applies to project C compilations while linked libraries
# retain their independently built settings.
level="${FASTDYN_FORCE_OPT_LEVEL:?set FASTDYN_FORCE_OPT_LEVEL to O0, O3, Os, or Oz}"
case "${level}" in
  O0|O1|O2|O3|Os|Oz) ;;
  *) printf 'unsupported optimization level: %s\n' "${level}" >&2; exit 64 ;;
esac
exec "$@" "-${level}"
