#!/usr/bin/env bash
# Usage: hosted-campaign.sh NEW_WORK NEW_RESULTS IDLE_CPU
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../../.." && pwd)
work=$(realpath -m "${1:?supply a fresh work directory}")
results=$(realpath -m "${2:?supply a fresh results directory}")
cpu=${3:?supply a dedicated timing CPU number}
mkdir "$work" "$results"
policy="$here/campaign-policy.json"
cp "$policy" "$results/predeclared-policy.json"
git -C "$repo" rev-parse HEAD > "$results/fastdyn.commit"
historical=16790d6e56c25b820748d275cb80577db205bcbd
if ! git -C "$repo" cat-file -e "$historical^{commit}" 2>/dev/null; then
    git -C "$repo" fetch --no-tags --depth=1 origin "$historical" > "$results/historical-fetch.log" 2>&1 || true
fi
# On an incomplete run, retain all available receipts, extracted observations,
# stdout, stderr and raw UART bytes. Original large deployment files stay in WORK.
preserve() {
    status=$?
    cd "$work"
    find . -type f \( -name '*.json' -o -name '*.jsonl' -o -name '*.log' -o -name '*.toml' \
       -o -name 'uart.bin' -o -name 'returned.utf8' -o -name 'expected.utf8' -o -name 'keys.bin' \) \
       ! -path './product/*' ! -path './plugin-release/*' ! -path './campaign/pre-change/tools/*' \
       -print0 | tar --null -T - -czf "$results/observations.tgz"
    "$here/export-training.sh" "$work" "$results" "$repo/third_party/common/qemu/build/qemu-system-arm"
    echo "$status" > "$results/exit-code.txt"
}
trap preserve EXIT
shift 3
WARM_BUILD_ONLY=1 "$here/warm-smoke.sh" "$work" "$results" > "$results/runtime-build.log" 2>&1
"$repo/scopt-env/bin/python" "$here/run_campaign.py" --product "$work/product" \
    --qemu "$repo/third_party/common/qemu/build/qemu-system-arm" \
    --plugin "$work/plugin-release/libfastdyn.so" --out "$work/campaign" --cpu "$cpu" "$@" \
    > "$results/campaign.log" 2>&1
