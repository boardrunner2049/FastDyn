# Betaflight 4.2.11: F405 hardware rehosting report

Validated **2026-11-11**. Build, pinned profile, physical sensor/motor slicing
and the complete host-piloted mission all passed. See the [sweep report](../README.md)
for shared methodology and timing definitions.

## Artifact and configuration

| Item | Value |
| --- | --- |
| Upstream source | [betaflight/betaflight 4.2.11](https://github.com/betaflight/betaflight/tree/948ba6339766851806d7637370829ea0ff74c690) |
| Full firmware commit | `948ba6339766851806d7637370829ea0ff74c690` |
| Target/configuration | `MATEKF405 legacy board target` |
| Compiler version | GCC `9.2.1` |
| Firmware source patches | None |
| ELF | [bin/betaflight_MATEKF405.elf](bin/betaflight_MATEKF405.elf) |
| ELF SHA-256 | `0506924e3d51dd1d3ad3c4933319f82022b2239dbd23543ae904cf23f52b87e1` |
| ELF bytes including debug info | 7,729,900 |
| Linked text / data / BSS bytes (`size`) | 491,149 / 6,512 / 83,748 |
| Receiver / UART identifiers | MSP; UART1/2/3 = 0/1/2 |

The linked text/data/BSS values are not separate flash/RAM region allocations.
The STM32F405 has 1 MiB flash, 128 KiB SRAM and 64 KiB CCM; the linker checks
its regions. The ELF retains debug information and is therefore larger than
the firmware's loadable memory footprint.

The [retained board header](board-target.h) comes from this release's built-in
`src/main/target/MATEKF405/target.h`. Its normal MPU6000 branch uses SPI1/PC2
and CW270 alignment; its normal BMP280 branch uses I2C1. Extra sensor variants
in that target were not separately validated.

## Measured work and validation time

| Measurement | Seconds |
| --- | ---: |
| Commands producing the validated artifact | 12.274 |
| All measured build commands, including discarded/failed attempts | 14.501 |
| All measured mission attempts | 160.899 |
| First passing mission check, including startup and assertions | 86.086 |
| First recorded command to first passing mission end | 1378.026 |

Window: `20261111T184349.921753Z` to `2026-11-11T19:06:47.947932+00:00`.
It includes interleaved work and the session interruption, not exclusive human
engineering time. Shared setup/downloads and later documentation/regression work
are outside this window. There were 3 measured build commands
(2 nonzero) and 3 measured mission
attempts (2 nonzero). Diagnostic aborts count as
nonzero attempts, not distinct firmware defects. See [raw command timing](../evidence/steps.jsonl).

## Challenges, resolutions and evidence

| Observed issue | Retained resolution | Evidence and limits |
| --- | --- | --- |
| The old 12-byte busDevice holds SPI/I2C directly; extDevice and spiSequence are absent. | Add a separate legacy SPI transport for register/byte/transfer operations and legacy I2C addressing. Retain mpuGyroReadSPI, mpuAccRead and BMP280 compensation in ARM code. | Legacy raw-register tests, bus ABI assertion and decode/compensation traces. |
| This GPS parser does not support NAV-PVT, so it produced no usable GPS position during PREFLIGHT. | Emit UBX NAV-SOL, NAV-POSLLH and NAV-VELNED through UART3, with correct payload layouts and checksums. | First PREFLIGHT timeout; legacy UBX framing/velocity test; final 3D fix and completed mission. |
| Only usTicks exists; the newer reciprocal/clock-frequency statics are absent. | Seed only the existing time-conversion variable. Do not write guessed addresses for absent statics. | Symbol inspection, generated cycleCounterInit rule and advancing firmware/FMU time. |
| Receiver, telemetry and ANGLE conventions differ; motor count is at offset 44. | Use the same explicit MSP/legacy-position/linear-ANGLE handling as 4.3.2, with its own ABI/hash and GCC 9.2.1. | Passing native/profile tests and 86.086 s mission check. Peak GPS altitude was 16.9 m. |

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
- Peak GPS height: **16.9 m**; final N/E:
  **0.1/0.1 m** (console rounding).
- Final relative altitude: **0.00 m**, landed and disarmed.
- Peak sampled absolute roll/pitch: 10.00/9.90 deg.
- Physical raw reads: MPU6000 SPI **784,867**, BMP280 I2C **3,488**.
- Plant pressure encoded through raw BMP280 ADC:
  **99346.1–99546.7 Pa**;
  peak modeled gyro rate **62.1 deg/s**.
- QEMU wait wall time: 85.036 s. Last periodic FMU report:
  83.292 simulated seconds, 1.04x.
  This periodic report is a lower bound on final virtual time, not its exact end.
- Recording: 164 samples over 81.641 wall seconds
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
fastdyn-env/bin/python "$BF_SWEEP/run.py" 4.2.11
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/mavlink/replay_betaflight.py \
  play "$BF_SWEEP/4.2.11/evidence/flight.json" --port 18500
```

Use `build.py 4.2.11 --toolchain /path/to/gcc-9.2.1` for an isolated review
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
