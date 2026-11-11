# INAV MATEKF405 quadrotor in FastDyn

This profile runs the included Cortex-M4 INAV firmware in patched QEMU and
connects it to the `FastDyn.Copter` FMI 3 co-simulation model. It implements a
single MATEKF405 quadrotor; the absolute hook addresses and guest structure
offsets apply **only** to the included ELF. The configuration checks its
SHA-256 before installing the hooks.

From the FastDyn root, prepare QEMU, the native plugin and Rumoca as described
in the main README. Rumoca requires the nightly Rust toolchain pinned by its
`rust-toolchain.toml`, `rustfmt`, and the `libudev` development files. Then run:

```sh
source ./setup.sh --build-qemu --with-rumoca --skip-optifuzz
python utils/setup_fire_toolchain.py  # pinned FMI headers for packaging
fastdyn run -c configs/inav_matekf405.toml -o inav-fastdyn_work
```

The profile uses the quadrotor parameters and home position from the existing
ArduCopter profile. Its FMU timer advances in 1 ms increments on QEMU's virtual
clock. INAV configures its own SysTick; the FMU timer does not raise another
interrupt. Firmware settings use RAM-backed EEPROM and reset on each run.

To provide receiver frames, run this from another shell on the same host:

```sh
# Send a disarmed signal, arm after 2 seconds, and raise throttle after 4.
python virtuals/physics/flight_controllers/inav/send_rc.py --arm --throttle 1500 --duration 30
```

The receiver accepts 20-byte UDP datagrams on `127.0.0.1:14590`: ASCII `FDRC`
followed by eight little-endian unsigned 16-bit PWM values, in AETR and AUX1–4
order. Each value must be 900–2100 microseconds; invalid packets are ignored.
AUX1 at 1700–2100 is the arm switch. INAV's receiver timeout and failsafe are
active when frames stop. One profile instance can bind the receiver port at a
time. Telemetry and mission upload interfaces are not implemented in this
profile.

## Frames, units, and calibration

The FMU reports acceleration, angular velocity and magnetic field in body FRD
axes, with m/s², rad/s and gauss respectively. The MPU6500 bridge negates
specific force to match INAV's +1 g level convention and compensates for the
MATEKF405's `CW180_DEG` sensor mounting. It uses the firmware driver's
2048 counts/g accelerometer and 16.4 counts/(degree/s) gyro scales, with
saturating signed 16-bit conversion. The firmware then performs its normal
sensor alignment, gyro calibration, filtering, PID and estimator processing.

The fake magnetometer uses the same 1024 counts/gauss convention as INAV's
X-Plane SITL adapter; the barometer reports pascals and hundredths of a degree
Celsius. GPS position is passed in degrees × 10⁷ and altitude and NED velocity
in centimetres and centimetres/second. The FMU's WGS84 altitude is used as the
simulated GPS altitude; it is not corrected for a geoid model. GPS fix and
accuracy are synthetic. INAV's SITL convention treats the ideal simulated
accelerometer and compass as factory-calibrated, with zero offsets and unity
gains; gyro zero calibration still runs at startup. This does not represent
real sensor calibration or noise.

The mixer is initialized in the FMU's motor order using its body-FLU moment map
converted to INAV's FRD axes. It is installed before INAV counts motors; the
firmware's normal mixer computes the PWM sent to the FMU. The virtual hooks also
replace the selected board's unavailable bus and oscillator operations. They
are designed for this profile, not as general STM32 peripheral emulation.

## Firmware source and reproduction

The included `inav-matekf405.elf` is INAV 10.0.0 at upstream commit
`d0c611e040887940226c9536bc84a3795ef9e800`, GPL-3.0-or-later. The upstream
source is <https://github.com/iNavFlight/inav>; [inav.patch](inav.patch) records
the changes used for this image, and [LICENSE-INAV](LICENSE-INAV) contains its
license. The patch selects fake GPS, MSP receiver and AUX1 arm, enables GPS,
uses RAM-backed settings, and marks the ideal simulated accelerometer and
magnetometer as calibrated. It does not replace INAV's control algorithms.

The image was built with GNU Arm Embedded GCC 14.2.1, CMake `RelWithDebInfo`,
and these C flags:

```text
-DCONFIG_IN_RAM -DEEPROM_SIZE=16384 -DUSE_FAKE_BARO -DUSE_FAKE_MAG
-DUSE_GPS_FAKE -DFASTDYN_REHOST -DDEFAULT_RX_TYPE=RX_TYPE_MSP
```

To build a review copy with these options, run
`virtuals/physics/flight_controllers/inav/build_firmware.sh`. It creates a
new `out/inav-source` checkout and refuses to overwrite an existing one. `COMPILER_VERSION_CHECK=OFF` is needed
when using GCC 14.2.1 because upstream currently expects 13.2.1. Firmware
rebuilds can change addresses and structure layouts, even when the source is
unchanged. Review the ELF symbols and DWARF, update the hook addresses and
ABI offsets, then update `firmware_sha256`; do not reuse this profile blindly.

## Checks

```sh
python -m pytest -q tests/unit/test_inav_sensors.py tests/unit/test_config_paths.py
python -m pytest -q tests/integration/test_inav_fmu_smoke.py
# After building out/fmi3/Copter/FastDyn_Copter.fmu:
FASTDYN_RUN_INAV_COPTER=1 python -m pytest -q tests/integration/test_inav_fmu_smoke.py
```

The integration check needs a built FastDyn plugin and patched QEMU. It uses
FastDyn's usual QEMU resolution, including `FASTDYN_QEMU` when set. It builds a deterministic FMI test plant, boots the
included firmware, checks the sensor bridges, calibration, GPS fix and motor
mixer in guest memory, arms through receiver frames, and verifies that
throttle reaches the FMI motor input. The optional real-Copter check also
verifies a climb of at least one metre and receiver-loss failsafe. It does not
establish long-duration flight stability or mission completion.
