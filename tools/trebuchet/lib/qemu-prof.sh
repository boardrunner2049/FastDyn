#!/bin/bash
# Used as [Machine].qemu_path for profiling runs: FastDyn's QEMU command line plus the
# FirmBOLT block profiler (FBPROF_PLUGIN, FBPROF_OUT, FBPROF_STOP are set by trebuchet.sh).
exec "${TREBUCHET_QEMU:?}" "$@" -plugin "${FBPROF_PLUGIN:?},out=${FBPROF_OUT:?},stop=${FBPROF_STOP:?}"
