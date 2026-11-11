# Betaflight 4.5.5: F405 hardware rehosting report

Validated **2026-11-11**. Build, pinned profile, physical sensor/motor slicing
and the complete host-piloted mission all passed. See the [sweep report](../README.md)
for shared methodology and timing definitions.

## Artifact and configuration

| Item | Value |
| --- | --- |
| Upstream source | [betaflight/betaflight 4.5.5](https://github.com/betaflight/betaflight/tree/4adbd3ef7cb546947600e5f747bd5453c9573063) |
| Full firmware commit | `4adbd3ef7cb546947600e5f747bd5453c9573063` |
| Target/configuration | `MATEKF405CTR` |
| Compiler version | GCC `10.3.1` |
| Firmware source patches | None |
| ELF | [bin/betaflight_STM32F405_MATEKF405CTR.elf](bin/betaflight_STM32F405_MATEKF405CTR.elf) |
| ELF SHA-256 | `46cb118a04a64d789a7fa21bbb46cd15869f0bb214cfc54acb6192cffbb44c09` |
| ELF bytes including debug info | 4,310,724 |
| Linked text / data / BSS bytes (`size`) | 438,717 / 6,516 / 87,860 |
| Receiver / UART identifiers | MSP; UART1/2/3 = 0/1/2 |

The linked text/data/BSS values are not separate flash/RAM region allocations.
The STM32F405 has 1 MiB flash, 128 KiB SRAM and 64 KiB CCM; the linker checks
its regions. The ELF retains debug information and is therefore larger than
the firmware's loadable memory footprint.

Board source: [betaflight/config at 92c695af8](https://github.com/betaflight/config/blob/92c695af8a2756d75152b54959de28d48fc25cf7/configs/MATEKF405CTR/config.h).
[The retained header](board-config.h) is unchanged; SHA-256 `9ab40ba5d9ad448dadcbd7857231685641b6634fd9e5caed45766d5b9d7c075a`.

## Measured work and validation time

| Measurement | Seconds |
| --- | ---: |
| Commands producing the validated artifact | 6.779 |
| All measured build commands, including discarded/failed attempts | 8.696 |
| All measured mission attempts | 69.499 |
| First passing mission check, including startup and assertions | 69.499 |
| First recorded command to first passing mission end | 1022.093 |

Window: `20261111T184349.920148Z` to `2026-11-11T19:00:52.013394+00:00`.
It includes interleaved work and the session interruption, not exclusive human
engineering time. Shared setup/downloads and later documentation/regression work
are outside this window. There were 2 measured build commands
(1 nonzero) and 1 measured mission
attempts (0 nonzero). Diagnostic aborts count as
nonzero attempts, not distinct firmware defects. See [raw command timing](../evidence/steps.jsonl).

## Challenges, resolutions and evidence

| Observed issue | Retained resolution | Evidence and limits |
| --- | --- | --- |
| No MAVLink serial receiver is compiled into this release. | Send MSP_SET_RAW_RC packets over the actual UART2 MSP parser and RX_MSP path; MAVLink telemetry stays on UART1. | MSP wire checksum/channel-order unit test; real arming, flight and disarm through MSP. |
| GLOBAL_POSITION_INT reports zero velocities and puts microseconds in time_boot_ms. | Differentiate received GPS positions with the firmware timestamp, including 32-bit wrap. Do not read host plant truth in the pilot. | legacy_position tests and recorded telemetry. Peak GPS altitude was 16.2 m rather than exactly 15 m. |
| motorDevice.count moved to offset 52 and motor protocol field names changed. | Pass the validated count offset explicitly; generate parameter offsets from this ELF's DWARF. | abi.json and the unmodified motorWriteAll observation boundary. |
| The old build system has no fwo target or configWriteWord symbol. | Use its hex target and STM32 SPL FLASH_ProgramWord value ABI; return FLASH_COMPLETE=9. Substitute sector erase for this transient store. | Failed fwo command, successful build, direct-value flash adapter test and completed runtime. |

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
- Peak GPS height: **16.2 m**; final N/E:
  **-0.0/0.1 m** (console rounding).
- Final relative altitude: **0.00 m**, landed and disarmed.
- Peak sampled absolute roll/pitch: 10.00/9.90 deg.
- Physical raw reads: MPU6000 SPI **795,263**, BMP280 I2C **3,534**.
- Plant pressure encoded through raw BMP280 ADC:
  **99354.9–99546.7 Pa**;
  peak modeled gyro rate **73.5 deg/s**.
- QEMU wait wall time: 68.430 s. Last periodic FMU report:
  85.710 simulated seconds, 1.32x.
  This periodic report is a lower bound on final virtual time, not its exact end.
- Recording: 166 samples over 64.734 wall seconds
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
fastdyn-env/bin/python "$BF_SWEEP/run.py" 4.5.5
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/mavlink/replay_betaflight.py \
  play "$BF_SWEEP/4.5.5/evidence/flight.json" --port 18500
```

Use `build.py 4.5.5 --toolchain /path/to/gcc-10.3.1` for an isolated review
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
