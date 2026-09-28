# VIO Probes — synthetic stimulus tests for the Boardrunner physics pipeline

## What these are

Each `probe_<sensor>.py` writes a known synthetic sample into a Boardrunner
shm segment (bypassing Gazebo entirely) and lets you observe whether the
firmware — through the full C-model → packer → SPI/I2C/UART slave → AHRS
chain — actually consumes it.

They are **integration tests**, not unit tests: they need a live FastDyn
instance with the corresponding slave model refactored to the channel API,
plus a matching `data_source` URI in the TOML config.

## When to use them

- **After refactoring a slave model** to the VIO channel API — prove the
  new code path is reachable end-to-end before hooking up Gazebo.
- **When Gazebo integration is misbehaving** — isolate the failure by
  ruling out the Gazebo half of the pipeline first.
- **As regression coverage** when the canonical schema or the packer
  headers change — every probe still passing means the wire format
  contract still holds.

## Layout

```
tests/integration/vio_probes/
├── README.md              (this file)
├── common.py              shared: wire constants, seqlock writer,
│                          shm open/read helpers.  Mirror of canonical.h.
├── probe_imu.py           IMU stimulus (any Invensense slave)
├── probe_baro.py          (future — Phase 4a)
├── probe_mag.py           (future — Phase 4b)
├── probe_gnss.py          (future — Phase 4c)
├── probe_actuator_pwm.py  (future — Phase 4d, READ-side probe)
└── run_all.py             (future — orchestrator, exit 0/1 for CI)
```

## Running

Each probe supports two subcommands:

| Subcommand | Purpose | Needs MAVLink? |
|---|---|---|
| `inject` | Write a fixed sample to a shm URI. You watch MAVProxy manually. | No |
| `verify` | Automated PASS/FAIL. Handles baseline / stimulus / restore itself. | Yes |

**Prerequisites (any mode):**
1. FastDyn is running (`fastdyn probe-run -c configs/rover462.toml …`) so
   the shm segment(s) exist.
2. The slave model whose URI you're targeting has been refactored to
   call `api_channel_attach` in its `slave_spi_init` hook.

**Additional for `verify`:**
3. MAVProxy is forwarding MAVLink over UDP so both the human and the probe
   can read it:
   ```bash
   mavproxy.py --master=/tmp/usart2_host --baudrate 57600 \
               --out=udp:127.0.0.1:14550
   ```
4. Boot has reached `Mode HOLD` (probe waits for a heartbeat but not for cal).
5. Python must have `pymavlink` — that's in the FastDyn venv, so run:
   ```bash
   ./fastdyn-env/bin/python3 tests/integration/vio_probes/probe_imu.py ...
   ```

### `verify` — automated PASS/FAIL

```bash
# Single URI
./fastdyn-env/bin/python3 tests/integration/vio_probes/probe_imu.py verify \
    boardrunner://vehicle/0/sensor/imu/2

# All three URIs, print table and identify primary IMU
./fastdyn-env/bin/python3 tests/integration/vio_probes/probe_imu.py verify --all
```

What it does:
1. Snapshots current shm state.
2. Collects baseline ATTITUDE for ~2.5 s.
3. Injects a 30° accel-based tilt in a background thread for ~3 s.
4. Collects response ATTITUDE for ~2.5 s (while injection continues).
5. Restores original shm state.
6. Prints PASS if `|Δroll|` exceeds 50% of the injected tilt (loose threshold
   accounts for chip mount rotation and AHRS damping), else `fail` with
   a diagnostic reason.

Why accel-tilt not gyro burst: gyros are subject to bias calibration.
Sustained gyro gets treated as bias and subtracted, giving false negatives.
Accel is the AHRS's gravity reference — tilting it *must* update roll/pitch.

### `inject` — raw single-shot (manual observation)

Useful for stepping through the pipeline yourself or debugging failures.

```bash
# 1.5 rad/s yaw stimulus on ICM20602 for 5 s
python3 tests/integration/vio_probes/probe_imu.py inject \
    boardrunner://vehicle/0/sensor/imu/0 \
    --gyro-z 1.5 --duration 5
```

Watch MAVProxy for the corresponding field to shift.

## Adding a new probe (recipe)

When we get to Phase 4a (baro), 4b (mag), etc., each new probe follows the
same shape as `probe_imu.py`:

1. Add the sensor's payload struct format to `common.py`
   (mirror the C struct in `include/boardrunner/canonical.h`).
2. Add a payload-specific writer helper if useful
   (e.g. `inject_baro_stream(mm, pressure_pa, temp_c, duration_s)`).
3. Copy `probe_imu.py` → `probe_<sensor>.py`, swap the CLI flags for the
   modality's fields, update the ATTITUDE assertion pattern in the
   docstring.

## Boot-safety guarantee (do not break)

Probes must **never** create a shm segment that doesn't already exist.
If a segment is missing, that's diagnostic information: it means the
C-side slave never attached its channel. The probe should fail loudly
(non-zero exit, clear stderr message) rather than silently create the
segment and mask the real bug.
