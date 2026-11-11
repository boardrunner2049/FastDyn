#!/usr/bin/env bash
set -Eeuo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
export FASTDYN_BF_SMOKE_CONFIG="${FASTDYN_BF_SMOKE_CONFIG:-configs/betaflight_f405.toml}"
export FASTDYN_BF_SMOKE_LOG="${FASTDYN_BF_SMOKE_LOG:-out/ci/betaflight_matekf405ctr.log}"
export FASTDYN_BF_SMOKE_WORK_DIR="${FASTDYN_BF_SMOKE_WORK_DIR:-out/ci/betaflight_matekf405ctr_work}"
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/prepare_execution_configuration.py \
    --profile virtuals/physics/flight_controllers/courbet/betaflight/matekf405ctr/profile.toml
tests/integration/betaflight_f405_smoke.sh
fastdyn-env/bin/python - "$FASTDYN_BF_SMOKE_LOG" <<'PY'
from pathlib import Path
import re
import sys
log = Path(sys.argv[1]).read_text()
for marker in ('bf-hw: MPU6000 raw SPI samples active',
               'bf-hw: BMP280 raw I2C samples active',
               'hw-mpu-gyro-decode', 'hw-mpu-acc-decode', 'hw-bmp-compensate',
               'starting real accelerometer calibration'):
    assert marker in log, f'missing physical driver evidence: {marker}'
assert 'virtual gyro @' not in log
summary = re.search(r'bf-hw: summary MPU6000_SPI_reads=(\d+) BMP280_I2C_reads=(\d+) pressure_range=([\d.]+)\.\.([\d.]+)Pa max_gyro=([\d.]+)dps', log)
assert summary, 'missing physical sensor statistics'
assert int(summary[1]) > 1000 and int(summary[2]) > 100
assert float(summary[4]) - float(summary[3]) > 100, 'pressure did not follow flight altitude'
assert float(summary[5]) > 5, 'gyro did not follow physical rotation'
print('[ci] MATEKF405CTR physical sensor driver flight passed')
PY
