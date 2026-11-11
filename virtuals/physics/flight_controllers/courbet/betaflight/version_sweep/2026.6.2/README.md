# Betaflight 2026.6.2: F405 hardware rehosting report

Validated **2026-11-11**. Build, pinned profile, physical sensor/motor slicing
and the complete host-piloted mission all passed. See the [sweep report](../README.md)
for shared methodology and timing definitions.

## Artifact and configuration

| Item | Value |
| --- | --- |
| Upstream source | [betaflight/betaflight 2026.6.2](https://github.com/betaflight/betaflight/tree/e0b7bb01b17b21351057e9ead2d1ab39dd44fa16) |
| Full firmware commit | `e0b7bb01b17b21351057e9ead2d1ab39dd44fa16` |
| Target/configuration | `MATEKF405CTR` |
| Compiler version | GCC `13.3.1` |
| Firmware source patches | None |
| ELF | [bin/betaflight_STM32F405_MATEKF405CTR.elf](bin/betaflight_STM32F405_MATEKF405CTR.elf) |
| ELF SHA-256 | `d571ed03e1ae69ee2677d85722e85ad22cbb3d517f1b1bb2a8273ffcebbff590` |
| ELF bytes including debug info | 4,654,616 |
| Linked text / data / BSS bytes (`size`) | 499,108 / 6,792 / 92,932 |
| Receiver / UART identifiers | MAVLink; UART1/2/3 = 51/52/53 |

The linked text/data/BSS values are not separate flash/RAM region allocations.
The STM32F405 has 1 MiB flash, 128 KiB SRAM and 64 KiB CCM; the linker checks
its regions. The ELF retains debug information and is therefore larger than
the firmware's loadable memory footprint.

Board source: [betaflight/config at 3a406c5c3](https://github.com/betaflight/config/blob/3a406c5c30c20d7c3bc7dd5ed547cca961baf0a9/configs/MTKS/MATEKF405CTR/config.h).
[The retained header](board-config.h) is unchanged; SHA-256 `9ab40ba5d9ad448dadcbd7857231685641b6634fd9e5caed45766d5b9d7c075a`.

## Measured work and validation time

| Measurement | Seconds |
| --- | ---: |
| Commands producing the validated artifact | 7.868 |
| All measured build commands, including discarded/failed attempts | 7.868 |
| All measured mission attempts | 120.276 |
| First passing mission check, including startup and assertions | 77.889 |
| First recorded command to first passing mission end | 501.199 |

Window: `20261111T183934.748570Z` to `2026-11-11T18:47:55.947416+00:00`.
It includes interleaved work and the session interruption, not exclusive human
engineering time. Shared setup/downloads and later documentation/regression work
are outside this window. There were 1 measured build commands
(0 nonzero) and 3 measured mission
attempts (2 nonzero). Diagnostic aborts count as
nonzero attempts, not distinct firmware defects. See [raw command timing](../evidence/steps.jsonl).

## Challenges, resolutions and evidence

| Observed issue | Retained resolution | Evidence and limits |
| --- | --- | --- |
| Development-snapshot settings were incompatible: rx_uart/gps_uart fields are absent; mspConfig is only one byte. | Use DWARF-derived serialConfig function masks for UART1 RX/telemetry, UART2 MSP and UART3 GPS. Preserve board pins. | abi.json, parameters.txt and observation-only hardware trace entries. |
| A single parameter rule exceeded the core parser's 300-byte argument limit and 512-byte line buffer. | Load a separately hashed parameter file through bf_hw_parameters. Keep core parsing unchanged. | First 1.288 s failed runtime attempt in sweep steps; parameter checksum is checked before generation. |
| The host rejected valid older GENERIC autopilot heartbeats. | Explicit --heartbeat-autopilot 0 for this release. | The 41.099 s no-heartbeat attempt, then the qualifying 77.889 s mission. |

The [ABI manifest](abi.json) records consumed type sizes/field offsets and
constants. [profile.toml](profile.toml) pins the ELF hash, vector addresses,
initial stack, reset vector and the instruction at `run+2`.
[labeled_conf/virtuals.txt](labeled_conf/virtuals.txt) identifies both replaced
transport operations and observation-only driver functions.
[parameters.txt](parameters.txt) is checksum-checked, and its symbolic write
provenance is in `abi.json`. Configuration is transient and resets each process.

## Flight and sensor evidence

The route was `(30,0) -> (30,30) -> (0,30) -> home`, at a 15 m target height.
The host pilot closed the position/velocity loop over firmware telemetry and
sent ordinary receiver frames. Firmware calibration, stabilization, PID and
motor mixing remained in ARM code.

- Waypoints: **3/3**, in order.
- Peak GPS height: **15.1 m**; final N/E:
  **0.0/0.1 m** (console rounding).
- Final relative altitude: **0.00 m**, landed and disarmed.
- Peak sampled absolute roll/pitch: 11.60/10.40 deg.
- Physical raw reads: MPU6000 SPI **761,604**, BMP280 I2C **3,384**.
- Plant pressure encoded through raw BMP280 ADC:
  **99368.5–99546.7 Pa**;
  peak modeled gyro rate **80.7 deg/s**.
- QEMU wait wall time: 76.677 s. Last periodic FMU report:
  84.060 simulated seconds, 1.12x.
  This periodic report is a lower bound on final virtual time, not its exact end.
- Recording: 419 samples over 70.119 wall seconds
  from the first saved position.

Evidence: [console](evidence/mission.log), [FastDyn timing](evidence/timing.jsonl),
[sensor statistics](evidence/sensors.json), [flight data](evidence/flight.json),
[machine-readable results](result.json), [static audit](evidence/static-audit.txt)
and [preprocessor flags](evidence/preprocessor-defines.txt). MPU read/decode and BMP280 compensation
traces demonstrate original physical-driver execution; their bodies were not
replaced. The pressure range and gyro response establish changing feedback.

## Reproduce or replay

From the FastDyn root:

```bash
BF_SWEEP=virtuals/physics/flight_controllers/courbet/betaflight/version_sweep
fastdyn-env/bin/python "$BF_SWEEP/run.py" 2026.6.2
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/mavlink/replay_betaflight.py \
  play "$BF_SWEEP/2026.6.2/evidence/flight.json" --port 18500
```

Use `build.py 2026.6.2 --toolchain /path/to/gcc-13.3.1` for an isolated review
build. A rebuilt ELF needs new bindings and runtime qualification; the existing
hash/result does not transfer to it. Upstream libraries/configuration and this
firmware retain [their GPL license](../LICENSE-BETAFLIGHT).

## Limits

This is a function-level hardware transport rehost, not complete STM32 peripheral
emulation or a cycle benchmark. SPI/I2C data and GPS fix/accuracy are ideal.
Clock/RTC, flash and absent internal ADC operations use the documented shared
substitutions. DMA/EXTI sensor timing, compass, storage, OSD, noise and long-term
reliability are outside this validation. Normal arming/calibration/failsafe paths
are retained; receiver-loss behavior was not separately stress-tested in this
release sweep. Mission completion does not imply identical path tracking or
onboard waypoint execution. The peak-altitude overshoot remains part of the
result rather than being hidden by the pass label.
