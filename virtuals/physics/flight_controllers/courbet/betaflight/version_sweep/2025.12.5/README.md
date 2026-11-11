# Betaflight 2025.12.5: F405 hardware rehosting report

Validated **2026-11-11**. Build, pinned profile, physical sensor/motor slicing
and the complete host-piloted mission all passed. See the [sweep report](../README.md)
for shared methodology and timing definitions.

## Artifact and configuration

| Item | Value |
| --- | --- |
| Upstream source | [betaflight/betaflight 2025.12.5](https://github.com/betaflight/betaflight/tree/7348054f268f0058574719c134e9f149565bb8ea) |
| Full firmware commit | `7348054f268f0058574719c134e9f149565bb8ea` |
| Target/configuration | `MATEKF405CTR` |
| Compiler version | GCC `13.3.1` |
| Firmware source patches | None |
| ELF | [bin/betaflight_STM32F405_MATEKF405CTR.elf](bin/betaflight_STM32F405_MATEKF405CTR.elf) |
| ELF SHA-256 | `57246a0e2cc2a746bf8fc49dc9339759ffb20e0cc3dbab905c35230490d9695c` |
| ELF bytes including debug info | 4,429,636 |
| Linked text / data / BSS bytes (`size`) | 461,912 / 6,928 / 88,600 |
| Receiver / UART identifiers | MAVLink; UART1/2/3 = 51/52/53 |

The linked text/data/BSS values are not separate flash/RAM region allocations.
The STM32F405 has 1 MiB flash, 128 KiB SRAM and 64 KiB CCM; the linker checks
its regions. The ELF retains debug information and is therefore larger than
the firmware's loadable memory footprint.

Board source: [betaflight/config at 92c695af8](https://github.com/betaflight/config/blob/92c695af8a2756d75152b54959de28d48fc25cf7/configs/MATEKF405CTR/config.h).
[The retained header](board-config.h) is unchanged; SHA-256 `9ab40ba5d9ad448dadcbd7857231685641b6634fd9e5caed45766d5b9d7c075a`.

## Measured work and validation time

| Measurement | Seconds |
| --- | ---: |
| Commands producing the validated artifact | 7.512 |
| All measured build commands, including discarded/failed attempts | 7.518 |
| All measured mission attempts | 122.059 |
| First passing mission check, including startup and assertions | 78.118 |
| First recorded command to first passing mission end | 967.975 |

Window: `20261111T184014.739880Z` to `2026-11-11T18:56:22.714395+00:00`.
It includes interleaved work and the session interruption, not exclusive human
engineering time. Shared setup/downloads and later documentation/regression work
are outside this window. There were 2 measured build commands
(1 nonzero) and 3 measured mission
attempts (2 nonzero). Diagnostic aborts count as
nonzero attempts, not distinct firmware defects. See [raw command timing](../evidence/steps.jsonl).

## Challenges, resolutions and evidence

| Observed issue | Retained resolution | Evidence and limits |
| --- | --- | --- |
| Providing ARM_SDK_PREFIX still triggered the old Makefile's PATH compiler check against GCC 13.2.1. | Supply ARM_SDK_DIR pointing at installed GCC 13.3.1, retaining the upstream version requirement. | 0.006 s failed command and 7.512 s successful build in the command ledger. |
| ANGLE heartbeat custom_mode is 0, while the development snapshot uses 1. | Configure the expected heartbeat and ANGLE identifiers explicitly in the host pilot, including its in-flight guard. | Two failed mission attempts; the final run uses GENERIC autopilot / ANGLE custom_mode 0. |
| systemFaultAction does not exist in this ELF. | Observe HardFault_Handler for diagnostics; retain the original handler rather than invent an address. | Symbolic profile resolves every hook. No hard-fault trace occurs in the passing console. |

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
- Peak GPS height: **15.0 m**; final N/E:
  **0.0/0.1 m** (console rounding).
- Final relative altitude: **0.00 m**, landed and disarmed.
- Peak sampled absolute roll/pitch: 10.00/10.10 deg.
- Physical raw reads: MPU6000 SPI **762,335**, BMP280 I2C **3,387**.
- Plant pressure encoded through raw BMP280 ADC:
  **99368.7–99546.6 Pa**;
  peak modeled gyro rate **79.2 deg/s**.
- QEMU wait wall time: 76.030 s. Last periodic FMU report:
  84.997 simulated seconds, 1.13x.
  This periodic report is a lower bound on final virtual time, not its exact end.
- Recording: 165 samples over 70.459 wall seconds
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
fastdyn-env/bin/python "$BF_SWEEP/run.py" 2025.12.5
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/mavlink/replay_betaflight.py \
  play "$BF_SWEEP/2025.12.5/evidence/flight.json" --port 18500
```

Use `build.py 2025.12.5 --toolchain /path/to/gcc-13.3.1` for an isolated review
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
