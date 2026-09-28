# VIO Probes — verification for the Boardrunner MMIO physics pipeline

This directory documents `tests/integration/vio_probes/` — the automated
verification harness that proves each sensor's byte-level plumbing works
between the endpoint (Gazebo or synthetic writer), the shm channel, the C
packer, the SPI/I2C/UART slave model, and the firmware's driver.

Companion architecture doc: [`../BoardRunner_VIO_Physics_Architecture.md`](../BoardRunner_VIO_Physics_Architecture.md)

## Index

| Sensor family | Doc | Status |
|---|---|---|
| **IMU** (Invensense) | [imu.md](imu.md) | complete (Phase 3) |
| **Baro** (MS5611) | *(planned)* | Phase 4a |
| **Mag** (HMC58xx / QMC5883) | *(planned)* | Phase 4b |
| **GNSS** (u-blox UBX) | *(planned)* | Phase 4c |
| **Actuator PWM** (IOMCU) | *(planned)* | Phase 4d |

Add a new file per sensor family when the corresponding `probe_<sensor>.py`
lands. Each file follows the same structure (see `imu.md`).

## What a probe does

Every `probe_<sensor>.py` supports three subcommands:

| Subcommand | Purpose | Firmware protocol needed? |
|---|---|---|
| `inject` | Push a synthetic sample into a shm URI. Manual observation via MAVProxy. | No |
| `show` | Dump current shm state for a URI (or all). | No |
| `verify` | Automated per-URI PASS/FAIL: baseline → inject → response snapshot → compare. | Yes — see below |

Verify's protocol dependence is the only hardcoded part of the whole VIO
stack. See [Firmware coupling — what's hardcoded](#firmware-coupling--whats-hardcoded).

## Verify pattern (per sensor family)

Same shape every time:

1. **Baseline snapshot** — read the firmware's current per-instance sensor
   value via a firmware-side telemetry channel.
2. **Save shm state** — for later restore.
3. **Inject** a distinctive stimulus into the URI's shm segment (background
   thread; runs for tens of seconds).
4. **Wait + drain** stale telemetry so the response window sees fresh data.
5. **Response snapshot** — read the sensor value again.
6. **Compare** per instance: whichever sensor instance's reading shifted
   by more than a threshold is the one bound to this URI. PASS if any.
7. **Restore** shm to the saved values.

This shape works for accel (via `SCALED_IMU_N.xacc/yacc/zacc` on ArduPilot),
gyro (via `SCALED_IMU_N.xgyro/ygyro/zgyro`), pressure (via
`SCALED_PRESSURE.press_abs`), mag (via `SCALED_IMU_N.xmag/ymag/zmag`),
etc. Only the message type and field names change per sensor.

## Firmware coupling — what's hardcoded

**Firmware-agnostic** (reusable for PX4, Zephyr, custom firmware):

- `boardrunner_vio/` — channel API, shm transport, packers, chip models.
- `include/boardrunner/` — canonical schema + URI grammar.
- `tests/integration/vio_probes/probe_*.py inject` and `show` — pure shm
  manipulation; no firmware protocol involved.
- Gazebo endpoints under `virtuals/.../gazebo/endpoints/` — publish SI into
  shm; don't know the firmware even exists.

**ArduPilot / MAVLink-specific** (the *only* firmware-coupled surface):

| Coupling | Where | Reason |
|---|---|---|
| MAVLink message names (`SCALED_IMU`, `RAW_IMU`, `ATTITUDE`, …) | `tests/integration/vio_probes/common.py` | MAVLink protocol |
| `SCALED_IMU` ↔ IMU instance mapping (RAW_IMU=0, SCALED_IMU2=1, SCALED_IMU3=2) | `common.py::_TYPE_TO_INSTANCE` | **ArduPilot quirk**: primary IMU uses `RAW_IMU`, not `SCALED_IMU` |
| `MAV_CMD_SET_MESSAGE_INTERVAL = 511` requests | `MavlinkObserver.connect()` | MAVLink command ID |
| `MAV_TYPE_GCS = 6` heartbeat | `MavlinkObserver._heartbeat_loop` | MAVLink convention (needed to trigger telemetry streaming from AP) |
| Assumption that MAVProxy relays via UDP 14550 | Default `--mavlink-url` | MAVProxy setup convention |
| pymavlink connection string parsing | `MavlinkObserver.connect()` | pymavlink library |

If we target a firmware that doesn't speak MAVLink, `common.py`'s
`MavlinkObserver` needs to be replaced with an equivalent observer for
that protocol. Everything else in the VIO stack stays.

## Future — pluggable observation backend

When a second firmware target appears, the right refactor is a
`VerifyBackend` protocol:

```python
class VerifyBackend(Protocol):
    def snapshot_sensor_by_instance(self, sensor_type: str) -> dict[int, dict]: ...

class MavlinkArdupilotBackend: ...         # what we have today
class MavlinkPX4Backend: ...               # PX4 uses different SCALED_IMU mapping
class GDBMemoryBackend: ...                # read AP_InertialSensor::_accel[N] directly
class ConsoleUARTBackend: ...              # for firmware without MAVLink
```

`verify` grows a `--backend` flag. **Not needed until second firmware
appears** (YAGNI). Tracked as task #28.

## Adding a new sensor family probe (recipe)

When we get to baro / mag / gnss / actuator, each follows this same shape:

1. **Sample struct** — flesh out `BoardrunnerXxxSample` in `include/boardrunner/canonical.h`.
2. **Packer** — write `include/boardrunner/packers/<family>.h/.c` (SI → chip bytes).
3. **Slave model refactor** — mirror ICM20602's pattern: `slave_spi_init` attaches channel, `update_sample` calls `BR_XXX_READ` + packer with LSB-defaults fallback.
4. **TOML `data_source`** — add URI to the slave's connection block.
5. **Probe script** — copy `probe_imu.py` → `probe_<sensor>.py`, swap:
   - Sample struct + format string in `common.py`
   - Injection stimulus (accel-like: pressure Pa, mag Tesla, etc.)
   - MAVLink message type observed (`SCALED_PRESSURE`, `SCALED_IMU.xmag`, `GPS_RAW_INT`, …)
   - Threshold magnitudes
6. **Sensor doc** — add `docs/vio_probes/<sensor>.md` with:
   - Coverage matrix (what's tested)
   - Coverage gaps (what's not)
   - MAVLink message mapping (URI → firmware telemetry field)
   - Any sensor-specific quirks

## Running probes

Prereqs:
- FastDyn running (`fastdyn probe-run …`).
- MAVProxy attached with UDP forward:
  ```bash
  mavproxy.py --master=/tmp/usart2_host --baudrate 57600 --console \
              --out=udp:127.0.0.1:14550
  ```
- Wait for `Mode HOLD` in MAVProxy.

Then:
```bash
python3 tests/integration/vio_probes/probe_<sensor>.py verify --all
```

Or for a single URI:
```bash
python3 tests/integration/vio_probes/probe_<sensor>.py verify \
    boardrunner://vehicle/0/sensor/<kind>/<n>
```
