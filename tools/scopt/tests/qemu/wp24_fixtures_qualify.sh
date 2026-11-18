#!/usr/bin/env bash
# WP24 B3/B4 fixture acceptance (dynamic): every config x input of
# fixtures/b3/b3conf.py x {icount, noicount}: nocache x2, cold, warm x2 in
# fresh run dirs under its per-mode oracle (fixtures/b3/expected.py), plus the
# F-B3-15 alias image directly on QEMU (mps2-an385).  Not a timing job.
#
#   wp24_fixtures_qualify.sh QEMU OUT_ROOT [-j N]
#
# Exit 0 iff everything passes (~2 minutes with -j 3).  Verdicts:
# OUT_ROOT/b3/verdict.json, OUT_ROOT/alias/verdict.json, OUT_ROOT/summary.txt.
set -uo pipefail
QEMU=$(realpath "$1"); OUT=$(realpath -m "$2"); shift 2
export FIXTURE_QEMU=$QEMU
exec "${FASTDYN_ROOT:-$(cd "$(dirname "$0")/../../../.." && pwd)}/fixtures/b3/qualify.sh" "$OUT" "$@"
