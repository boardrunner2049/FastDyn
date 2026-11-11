#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
qemu_bin=${FASTDYN_QEMU_BIN:-"$repo/third_party/common/qemu/build/qemu-system-arm"}
plugin_lib=${FASTDYN_PLUGIN_LIB:-"$repo/build/libfastdyn.so"}
fastdyn_bin=${FASTDYN_BIN:-"$repo/fastdyn-env/bin/fastdyn"}
tmp=$(mktemp -d)
if [[ ${FASTDYN_KEEP_TEST_TMP:-0} == 1 ]]; then
  echo "MMIO test artifacts: $tmp" >&2
else
  trap 'rm -rf "$tmp"' EXIT
fi

arm-none-eabi-as -mcpu=cortex-m4 \
  "$repo/tests/integration/mmio_opt/firmware.S" -o "$tmp/firmware.o"
arm-none-eabi-ld -T "$repo/tests/integration/mmio_opt/firmware.ld" \
  "$tmp/firmware.o" -o "$tmp/firmware.elf"

PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$fastdyn_bin" build-model --mmio-opt --no-vio \
  "$repo/tests/models/elder_echo.c" -o "$tmp/models" --json \
  >"$tmp/build-report.json"

sed -e "s|@QEMU@|$qemu_bin|g" \
    -e "s|@PLUGIN@|$plugin_lib|g" \
    -e "s|@FIRMWARE@|$tmp/firmware.elf|g" \
    -e "s|@MODEL@|$tmp/models/elder_echo.so|g" \
    -e "s|@QMP@|$tmp/qmp.sock|g" \
    -e "s|@MMIO_ENABLED@|true|g" \
    "$repo/tests/integration/mmio_opt/config.toml.in" >"$tmp/config.toml"

PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$fastdyn_bin" -q run -c "$tmp/config.toml" -o "$tmp/work" \
  >"$tmp/stdout" 2>"$tmp/stderr"

cat "$tmp/stdout"
cat "$tmp/stderr" >&2
grep -q "PASS automatic MMIO substitution" "$tmp/stdout" "$tmp/stderr"

sed -e '0,/enabled = true/s//enabled = false/' \
    -e "s|$tmp/qmp.sock|$tmp/qmp-baseline.sock|" \
    "$tmp/config.toml" >"$tmp/config-baseline.toml"
PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$fastdyn_bin" -q run -c "$tmp/config-baseline.toml" -o "$tmp/work-baseline" \
  >"$tmp/baseline-stdout" 2>"$tmp/baseline-stderr"
grep -q "PASS automatic MMIO substitution" \
  "$tmp/baseline-stdout" "$tmp/baseline-stderr"

python3 - "$tmp/work/run-artifacts/mmio_opt/installation.json" \
  "$tmp/work/run-artifacts/mmio_opt/discovery.json" \
  "$tmp/work/fastdyn_timing.jsonl" "$tmp/work-baseline/fastdyn_timing.jsonl" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
assert report["installed"] >= 5, report
installed = [site for site in report["sites"] if site["installed"]]
assert sum(site["fast_count"] for site in installed) >= 6, report
assert sum(site["fallback_count"] for site in installed) >= 2, report
assert any(site["fast_count"] and site["fallback_count"] for site in installed), report
assert any(site["fast_count"] == 2 and site["fallback_count"] == 0
           for site in installed), report
assert any(not site["installed"] for site in report["sites"]), report
print(json.dumps(report, indent=2, sort_keys=True))

def execution_time(path):
    records = [json.loads(line) for line in open(path, encoding="utf-8")]
    return next(record["duration_s"] for record in records
                if record.get("event") == "phase_end" and
                record.get("phase") == "machine0.qemu_execution")

discovery = json.load(open(sys.argv[2], encoding="utf-8"))
performance = {
    "workload_reads": 1_000_000,
    "baseline_execution_seconds": execution_time(sys.argv[4]),
    "optimized_execution_seconds": execution_time(sys.argv[3]),
    "preparation_seconds": discovery["preparation_seconds"],
    "fast_reads": sum(site["fast_count"] for site in installed),
    "guard_or_mpu_fallbacks": sum(site["fallback_count"] for site in installed),
}
assert performance["fast_reads"] >= performance["workload_reads"], performance
print(json.dumps({"performance": performance}, indent=2, sort_keys=True))
PY

echo "PASS generated operations, guarded substitution, live state, IRQ polling, widths, MPU/guard fallback, retranslation, and slow path"
