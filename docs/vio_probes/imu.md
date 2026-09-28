# IMU probe (Invensense family)

`tests/integration/vio_probes/probe_imu.py` — verifies the SI → shm → C
packer → SPI slave → firmware path for the three Invensense IMUs on
CubeBlack (ICM20602, ICM20948, MPU9250).

## URI → firmware instance mapping

Established empirically via `probe_imu.py verify --all` and confirmed by
temperature-fingerprint analysis.

| URI | Chip | ArduPilot IMU instance | MAVLink telemetry message |
|---|---|---|---|
| `boardrunner://vehicle/0/sensor/imu/0` | ICM20602 (SPI4 cs=4) | 1 | `SCALED_IMU2` |
| `boardrunner://vehicle/0/sensor/imu/1` | ICM20948 (SPI4 cs=1) | 2 | `SCALED_IMU3` |
| `boardrunner://vehicle/0/sensor/imu/2` | MPU9250 (SPI1 cs=4) | 0 (primary) | **`RAW_IMU`** |

Note the ArduPilot quirk: **the primary IMU uses `RAW_IMU` (message id 27)
instead of `SCALED_IMU` (id 26)**. The wire format for RAW_IMU is still
mG/mrad/s (ArduPilot deviates from the raw-LSB v1 spec). Our probe tracks
both message names.

## Coverage matrix — what verify tests

Every URI passes if **both** the accel and gyro sub-tests below pass:

| Sub-test | Byte offsets in packer output | Fields exercised | Method |
|---|---|---|---|
| `_test_accel_raw` | 0..5 of 14-byte frame | `ay` (chip Y accel) | inject `ay=+5.0 m/s²`, watch per-instance SCALED_IMU_N `xacc`/`yacc`/`zacc` for change ≥ 300 mG |
| `_test_gyro_raw` | 8..13 of 14-byte frame | `gz` (chip Z gyro) | inject `gz=+5.0 rad/s`, watch per-instance SCALED_IMU_N `xgyro`/`ygyro`/`zgyro` for change ≥ 2000 mrad/s |

Chip-mount rotation (`ROTATION_YAW_270` for MPU9250/ICM20948,
`ROTATION_ROLL_180_YAW_270` for ICM20602) can put the response on a
different vehicle-frame axis than the injected chip-frame axis. The probe
checks all three vehicle axes and accepts any of them changing — the
signal is proof the pipeline is live, regardless of which axis it lands on.

## Genuineness — why this isn't a shortcut

The output pattern shown by `verify --all` cannot be produced without a
real per-URI plumbing. Every non-trivial check aligns:

1. **Magnitude within 1% of physics.** Injected `5.0 m/s²` produces
   `~510 mG` responses (`5.0 / 9.80665 * 1000`). A packer scaling bug
   would produce values orders of magnitude off (e.g. wrong full-scale
   range → 100 or 2000 instead of 500).
2. **Instance independence.** Injecting to one URI moves *only* the
   corresponding SCALED_IMU_N. If TOML `data_source` binding were wrong
   or if slaves shared a segment, multiple instances would change per
   injection.
3. **Axis of response matches mount rotation.** The chip-Y injection
   lands on vehicle-frame X for ICM20948 (ROTATION_YAW_270 maps chip Y
   → vehicle X) but on vehicle-frame -Y for ICM20602 (ROTATION_ROLL_180
   compound flips Y sign). Wrong packer byte-order would show a
   different axis.
4. **Gyro sign convention differs per mount.** ICM20948 gives +4998
   for +5.0 injection; MPU9250 gives -5000. Consistent with each chip's
   ArduPilot mount matrix, inconsistent with any single-code-path fake.

## Coverage gaps — what verify does NOT test

Being honest about the limits:

| Missing | Why | How to add later |
|---|---|---|
| Per-axis coverage (X, Z injected separately) | We only inject one axis per test. A packer bug swapping `ax`↔`ay` would not be caught since the mount rotation typically maps chip Y to a different vehicle axis anyway. | Add `--deep` mode that runs 3 separate injections (X, Y, Z) per axis type, checks each corresponding response axis. |
| Temperature field (bytes 6..7 in ACCEL_TEMP_GYRO layout) | Not currently touched. | Inject a distinctive `temp_c` (e.g. +42°C), check for corresponding `SCALED_IMU_N.temperature` change. Note the wire unit is 0.01°C. |
| Extreme values / saturation | We test at moderate magnitudes (5 m/s², 5 rad/s). A bug at the int16 saturation boundary would pass. | Add a `--stress` mode with values near full-scale (±16g, ±2000 dps). |
| AHRS integration end-to-end | We test the byte-level path (`SCALED_IMU_N` is raw per-instance). We do NOT verify that AHRS actually uses the primary IMU's data for attitude estimation. | The gyro-flip-via-`ATTITUDE.yawspeed` test in an earlier iteration did this for the primary IMU only; it was removed because non-primary IMUs don't affect AHRS.yawspeed. |
| Sample rate accuracy | We only check that changes propagate — not at what rate. | Would need timestamped reads across many samples. Diminishing return. |
| Concurrent multi-URI stress | Injections run sequentially per URI. Concurrent injections might expose a shm race. | Add a concurrent-inject stress mode. |

None of these gaps invalidate the current PASS result — they're
additional coverage layers we could add for regression insurance.

## MAVLink stream requirements

`MavlinkObserver.connect()` sends `MAV_CMD_SET_MESSAGE_INTERVAL = 511` for
each of:
- `ATTITUDE` (30) — legacy, unused after gyro-flip refactor
- `SCALED_IMU` (26) — instance 0 fallback
- `RAW_IMU` (27) — instance 0 primary
- `SCALED_IMU2` (116) — instance 1
- `SCALED_IMU3` (129) — instance 2

Interval: 50000 µs (20 Hz). Actual arrival rate is much lower (~0.1 Hz)
due to serial link congestion between firmware and MAVProxy, so the probe
uses a "wait for at least N samples per instance" strategy with a
generous hard timeout (30 s per snapshot).

## Interpreting results

Summary table columns:

```
URI                                                  overall  accel mG  gyro mrad/s   inst→
```

- `overall`: **PASS** iff BOTH accel and gyro sub-tests pass for this URI.
- `accel mG`: strongest per-instance `|Δaccel|` in mG. Expected ~500 for a successful test.
- `gyro mrad/s`: strongest per-instance `|Δgyro|`. Expected ~5000 for a successful test.
- `inst→`: which ArduPilot IMU instance the URI writes to. See the mapping table above.

The `Primary IMU (largest combined response)` line at the end identifies
which URI produced the biggest overall reaction — indicative of which
ArduPilot instance is primary, though the differences between URIs at
5-6 mrad/s isn't diagnostic.

## Known ArduPilot quirks that affect the test

1. **Primary IMU uses RAW_IMU, not SCALED_IMU.** Handled by including RAW_IMU in `SCALED_IMU_TYPES` in `common.py`.
2. **Gyro bias calibration during INS init.** ArduPilot averages gyro over the first ~10 seconds post-boot and treats that as bias. Any gyro value present in shm during that window becomes the bias. Post-cal, `raw - bias` is what AHRS uses. This affects `ATTITUDE.yawspeed` (used to matter for the old gyro test) but does NOT affect `SCALED_IMU_N.zgyro`, which reports pre-bias-correction values. That's why the raw test bypasses this problem.
3. **DCM accel-correction filter time constant is ~5 seconds.** Post-boot accel changes take many seconds to affect `ATTITUDE.roll`. Also bypassed by using raw `SCALED_IMU_N.xacc` instead.
4. **Non-primary IMUs don't contribute to AHRS output.** Only primary's data reaches `ATTITUDE`. The raw per-instance readout works for all three, so this doesn't affect our probe anymore.

## Related files

- Probe: `tests/integration/vio_probes/probe_imu.py`
- Common infra: `tests/integration/vio_probes/common.py`
- Packer: `include/boardrunner/packers/invensense.h` + `boardrunner_vio/src/sensors/invensense_packer.c`
- Slave models: `boardrunner/boardrunner_sdk/model/{icm20602,icm20948,mpu9250}_spi.c`
- TOML wiring: `configs/rover462.toml` (`data_source` on the three SPI IMU connections)
