# Betaflight MATEKF405CTR Hardware Sensor Target

This target runs the unchanged STM32F405 ARM ELF built from Betaflight
`5ce286c96`, version `2026.12.0`, with the existing MATEKF405CTR board configuration.
The ELF SHA-256 is
`235afa1124b43784bebed4aa14afb883c7cb16e4fece853d771015888df5ecb7`.
Physical MPU6000 and BMP280 drivers execute in the guest; virtual sensor drivers
are absent from this build.

From the FastDyn root:

```bash
fastdyn-env/bin/fastdyn run -c configs/betaflight_f405.toml
tests/integration/betaflight_matekf405ctr_smoke.sh
```

The run starts the FMU, QEMU, MAVProxy, the host RC pilot and the local browser
viewer. It flies three 30 m corners at 15 m, returns, lands and disarms. The
viewer remains available for 120 seconds after landing while QEMU pauses.
Set `FASTDYN_BF_VIEW_SECONDS=0` to exit immediately. Route, altitude, timeout and
port overrides are the same as in the prototype helper.

For SSH, run on your local computer:

```bash
ssh -N -L 15500:127.0.0.1:15500 your-ssh-host
```

Start FastDyn on the server and open `http://127.0.0.1:15500/mavcesium/` locally.
The page serves a local field, runway, quadcopter, route, telemetry trail and
replay. HTTP and WebSocket use the same forwarded port.

## Sensor and configuration boundary

- MPU6000: original detection, configuration writes, SPI register decoding,
  board alignment, calibration and sensor processing. `spiSequence` supplies
  raw bytes from the plant. The driver selects its supported polling mode;
  DMA and sensor EXTI timing are not emulated.
- BMP280: original detection, calibration coefficient reads, raw ADC decoding
  and integer pressure/temperature compensation. I2C bus-device operations
  supply register responses from the plant.
- GPS: UBX NAV-PVT and MON-VER serial packets on UART3; the original UBX parser
  handles their framing, checksums, units and GPS state.
- Motors: original mixer and PWM output values feed the plant through the
  shared motor adapter. The FMU advances every 1 ms.

Guest runtime parameters select MAVLink RX on UART1, MSP on USB and UART2, UBX
GPS on UART3, PWM motors, AUX1 ARM, AUX2 ANGLE, and an 8 kHz gyro / 1 kHz PID
loop. SD/blackbox/OSD and ADC battery/current sources are disabled by parameters.
These settings are applied before `featureInit`; firmware and board sources
remain unchanged. The guest runs its real accelerometer calibration at startup.
There is no accelerometer-calibrated or arming-check override in this profile.

The QEMU `icount.shift=3` setting permits the normal late-task arming check to
pass for this debug build; it does not claim physical cycle accuracy. Clock/RTC
and absent internal ADC setup use the documented runtime substitutions. The
board has no modeled external compass. Settings do not persist across QEMU
processes. The mission is a host RC pilot; onboard mission execution remains
outside this validation.

## Bindings and validation

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/prepare_execution_configuration.py \
  --profile virtuals/physics/flight_controllers/courbet/betaflight/matekf405ctr/profile.toml
```

Generation checks the exact ELF hash, configuration address, `run+2` call
instruction, and DWARF layouts consumed by the sensor and configuration adapters.
The physical sensor read/compensation entry points have observation-only trace
rules for execution evidence; they are not replaced.

Validated 2026-11-10: full mission, 3/3 waypoints, 15.0 m maximum relative
altitude, landing 0.10 m from home, ground altitude 0.00 m, confirmed disarm and
exit zero. Sensor statistics record 769,287 MPU6000 SPI reads, 3,418 BMP280 I2C
reads, pressure 99,368.6–99,546.7 Pa and peak angular rate 84.5 deg/s. The smoke
test checks this sensor evidence as well as mission and viewer completion.

See the [hardware guide](../../../../../../docs/BetaflightHardwareRehosting.md)
for build provenance, ABI coverage, runtime restrictions and verification records.
The earlier virtual-sensor prototype is not distributed in this change.

## Portable reference replay

The subsequent [release version sweep](../version_sweep/README.md) documents six
physical-sensor release images, measured adaptation time and per-version results.
This directory remains the original development-snapshot baseline.

`reference_flight/` contains the recorded telemetry, frozen runtime TOML and
manifest for a hardware-target flight. These text files can be stored in GitHub;
MP4 output can be regenerated locally.

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/mavlink/replay_betaflight.py play
```

This starts only the saved-flight viewer. Use the same SSH forward and browser
port as above. The script also provides `archive`, `verify`, `rerun` and `video`
commands. See [BetaflightReplay.md](../../../../../../docs/BetaflightReplay.md) for
the full workflow and the distinction between saved playback and a fresh mission.
