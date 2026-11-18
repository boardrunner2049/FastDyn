#!/usr/bin/env bash
# WP12 fixture smoke: dynamic checks of the fault and compute fixtures.
#
#   wp12_fixtures_smoke.sh QEMU OUT_ROOT
#
# 1. fault_fixture.elf (all four variants), icount: nocache x2, cold, warm x2,
#    every run in a fresh run dir; guest.norm must equal
#    fixtures/fault/expected/fixture.norm (stacked PC == ELF address etc.).
# 2. compute fixture, input train-1: nocache x2, cold, warm x2 + model check.
# 3. compute fixture, input heldout-1: same (provides the B cache stats).
# 4. cross-input proof: warm cache from train-1 run with heldout-1 appends
#    exactly K(heldout-1)\K(train-1), no invalidation, manifest unchanged,
#    and a second run appends 0.
# Exit 0 iff everything passes.  Needs ~1 minute.
set -uo pipefail
QEMU=$(realpath "$1"); OUT=$(realpath -m "$2")
FIX=/data/extra_copy/fastdyn/fixtures
if [[ -e "$OUT" ]]; then echo "$OUT exists" >&2; exit 2; fi
mkdir -p "$OUT"
export FIXTURE_QEMU=$QEMU
st=0
python3 "$FIX/lib/qualify.py" --template "$FIX/fault/fault-fixture.toml.in" --out "$OUT/fault" \
    --cache-name fault-fixture.sc --timeout 60 --expect-norm "$FIX/fault/expected/fixture.norm" | tail -1 || st=1
grep -q '"pass": true' "$OUT/fault/summary.json" || st=1
mkdir -p "$OUT/compute"
for n in train-1 heldout-1; do
    split=${n%%-*}
    python3 "$FIX/lib/qualify.py" --template "$FIX/freertos-mps2-compute/configs/freertos-compute.toml.in" \
        --input "$FIX/inputs/$split/freertos-mps2-compute/$n.bin" --out "$OUT/compute/$n" \
        --cache-name freertos-compute.sc --timeout 300 | tail -1 || st=1
    grep -q '"pass": true' "$OUT/compute/$n/summary.json" || st=1
    python3 "$FIX/freertos-mps2-compute/check_model.py" "$OUT/compute/$n/cold" || st=1
    cmp -s "$OUT/compute/$n/cold/guest.norm" "$FIX/freertos-mps2-compute/expected/$n.icount.norm" \
        || { echo "FAIL: $n guest.norm differs from expected"; st=1; }
done
python3 "$FIX/freertos-mps2-compute/prove_input_invariance.py" --qual "$OUT/compute" \
    --out "$OUT/invariance" --pairs train-1:heldout-1 | tail -1 || st=1
grep -q '"pass": true' "$OUT/invariance/proof.json" || st=1
echo "wp12_fixtures_smoke: $([[ $st -eq 0 ]] && echo PASS || echo FAIL)"
exit $st
