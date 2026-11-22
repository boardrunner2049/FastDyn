#!/bin/bash
# Used as [Machine].qemu_path for profiling runs: FastDyn's QEMU command line plus the
# FirmBOLT block profiler. FBPROF_STOP is optional: without it, the profiler
# flushes at ordinary QEMU exit, which is needed for host-observed applications.
plugin="${FBPROF_PLUGIN:?},out=${FBPROF_OUT:?}"
if [[ -n "${FBPROF_STOP:-}" ]]; then
    plugin+=",stop=${FBPROF_STOP}"
fi
exec "${TREBUCHET_QEMU:?}" "$@" -plugin "$plugin"
