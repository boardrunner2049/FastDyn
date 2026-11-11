# Betaflight 4.3.2: F405 hardware rehosting report

Validated **2026-11-11**. Build, pinned profile, physical sensor/motor slicing
and the complete host-piloted mission all passed. See the [sweep report](../README.md)
for shared methodology and timing definitions.

## Artifact and configuration

| Item | Value |
| --- | --- |
| Upstream source | [betaflight/betaflight 4.3.2](https://github.com/betaflight/betaflight/tree/60c9521da6072e6c891bd30fd772e91a790f0b53) |
| Full firmware commit | `60c9521da6072e6c891bd30fd772e91a790f0b53` |
| Target/configuration | `MATEKF405 legacy board target` |
| Compiler version | GCC `9.3.1` |
| Firmware source patches | None |
| ELF | [bin/betaflight_MATEKF405.elf](bin/betaflight_MATEKF405.elf) |
| ELF SHA-256 | `0a9e9b368a0ac625a6b48fd01721bdbcc6b3942a8da8202946b50913201de870` |
| ELF bytes including debug info | 8,285,284 |
| Linked text / data / BSS bytes (`size`) | 435,805 / 6,580 / 97,776 |
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
| Commands producing the validated artifact | 12.283 |
| All measured build commands, including discarded/failed attempts | 14.680 |
| All measured mission attempts | 180.418 |
| First passing mission check, including startup and assertions | 77.365 |
| First recorded command to first passing mission end | 1369.302 |

Window: `20261111T184349.919901Z` to `2026-11-11T19:06:39.221490+00:00`.
It includes interleaved work and the session interruption, not exclusive human
engineering time. Shared setup/downloads and later documentation/regression work
are outside this window. There were 3 measured build commands
(2 nonzero) and 3 measured mission
attempts (2 nonzero). Diagnostic aborts count as
nonzero attempts, not distinct firmware defects. See [raw command timing](../evidence/steps.jsonl).

## Challenges, resolutions and evidence

| Observed issue | Retained resolution | Evidence and limits |
| --- | --- | --- |
| The board is the legacy built-in MATEKF405 target, with MPU6000 and optional other sensor branches. | Compile that unchanged physical target. Model WHO_AM_I/product ID so its original MPU6000 branch detects and initializes. | Target source at the pinned release and real MPU6000 decode traces. |
| motorDevice.count moved to offset 44; UART identifiers are 0/1/2 rather than 51/52/53. Buffer/end-write UART hooks do not exist. | Validate each consumed layout, pass the count offset and all three UART IDs explicitly, and use the original per-byte UART write interface. | abi.json, symbolic rules, source transport inspection and completed mission. |
| MSP RX, zero MAVLink velocity fields and linear ANGLE differ from the development baseline. | Use UART2 MSP receiver frames, timestamp-based GPS velocity and --angle-response linear --angle-limit 55. | The wrong-map run timed out in NAV; the corrected run completed 3/3. Peak GPS altitude was 17.0 m. |
| Old recursive make/python discovery failed in this environment. | Set MAKE=/usr/bin/make and PYTHON=/usr/bin/python3; use the release's hex goal and required GCC 9.3.1. | Failed command logs and the successful source-unchanged build. |

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
- Peak GPS height: **17.0 m**; final N/E:
  **-0.0/0.1 m** (console rounding).
- Final relative altitude: **0.00 m**, landed and disarmed.
- Peak sampled absolute roll/pitch: 10.00/10.00 deg.
- Physical raw reads: MPU6000 SPI **806,135**, BMP280 I2C **3,582**.
- Plant pressure encoded through raw BMP280 ADC:
  **99345.0–99546.6 Pa**;
  peak modeled gyro rate **66.4 deg/s**.
- QEMU wait wall time: 76.225 s. Last periodic FMU report:
  89.628 simulated seconds, 1.19x.
  This periodic report is a lower bound on final virtual time, not its exact end.
- Recording: 167 samples over 71.266 wall seconds
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
fastdyn-env/bin/python "$BF_SWEEP/run.py" 4.3.2
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/mavlink/replay_betaflight.py \
  play "$BF_SWEEP/4.3.2/evidence/flight.json" --port 18500
```

Use `build.py 4.3.2 --toolchain /path/to/gcc-9.3.1` for an isolated review
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
