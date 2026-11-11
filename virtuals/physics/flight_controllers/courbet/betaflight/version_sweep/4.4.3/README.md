# Betaflight 4.4.3: F405 hardware rehosting report

Validated **2026-11-11**. Build, pinned profile, physical sensor/motor slicing
and the complete host-piloted mission all passed. See the [sweep report](../README.md)
for shared methodology and timing definitions.

## Artifact and configuration

| Item | Value |
| --- | --- |
| Upstream source | [betaflight/betaflight 4.4.3](https://github.com/betaflight/betaflight/tree/738127e7e11da587f63b9531ec732a37bdb436b0) |
| Full firmware commit | `738127e7e11da587f63b9531ec732a37bdb436b0` |
| Target/configuration | `STM32F405 + MTKS-MATEKF405CTR unified configuration` |
| Compiler version | GCC `10.3.1` |
| Firmware source patches | None |
| ELF | [bin/betaflight_STM32F405.elf](bin/betaflight_STM32F405.elf) |
| ELF SHA-256 | `d9763c6fc99cdabc710590fbc8240978aa35322c0b92c3fa2e153c6e450c1d00` |
| ELF bytes including debug info | 4,979,504 |
| Linked text / data / BSS bytes (`size`) | 498,849 / 7,728 / 113,632 |
| Receiver / UART identifiers | MSP; UART1/2/3 = 0/1/2 |

The linked text/data/BSS values are not separate flash/RAM region allocations.
The STM32F405 has 1 MiB flash, 128 KiB SRAM and 64 KiB CCM; the linker checks
its regions. The ELF retains debug information and is therefore larger than
the firmware's loadable memory footprint.

The [official unified configuration](MTKS-MATEKF405CTR.config) is pinned at
`7f1abbf7844d3299ecf2ed4cca4680dc95a941b5`. Active hardware settings are
provisioned through explicit parameter writes, not by replacing physical drivers
or patching compiled instructions. The image's compiled MCU target identifier
is STM32F405; it is not falsely relabeled as a compiled MATEKF405CTR target.

## Measured work and validation time

| Measurement | Seconds |
| --- | ---: |
| Commands producing the validated artifact | 6.735 |
| All measured build commands, including discarded/failed attempts | 39.357 |
| All measured mission attempts | 263.560 |
| First passing mission check, including startup and assertions | 73.969 |
| First recorded command to first passing mission end | 1365.912 |

Window: `20261111T184349.919082Z` to `2026-11-11T19:06:35.831040+00:00`.
It includes interleaved work and the session interruption, not exclusive human
engineering time. Shared setup/downloads and later documentation/regression work
are outside this window. There were 9 measured build commands
(5 nonzero) and 4 measured mission
attempts (3 nonzero). Diagnostic aborts count as
nonzero attempts, not distinct firmware defects. See [raw command timing](../evidence/steps.jsonl).

## Challenges, resolutions and evidence

| Observed issue | Retained resolution | Evidence and limits |
| --- | --- | --- |
| This release provides a generic STM32F405 image and a separate unified board config, not CONFIG=MATEKF405CTR. | Pin the official MTKS-MATEKF405CTR configuration and apply its active SPI, I2C, UART, motor, gyro, barometer and timer parameters before peripheral initialization. | MTKS-MATEKF405CTR.config, its commit/hash in abi.json, and parameter_writes. The CLI parser is not executed to provision the board. |
| GCC 10.3.1 rejects flash_m25p16.c at debug optimization with maybe-uninitialized under -Werror. | Compile that unchanged translation unit at -O0, then build all other files with the normal -Og debug settings. Keep warnings fatal. | flash-O0 and final build records; build.py reproduces the sequence. This is a compiler-option exception, not a source patch. |
| DEBUG=INFO enabled LTO; changing debug flags reused existing objects. One diagnostic GDB inspection exited 137; the cause was not established. | Discard that image. Build the final DEBUG=GDB image in a fresh object directory, with -ggdb2 -gdwarf-5. Inspect its non-LTO symbols and layouts. | The validated SHA belongs to debugobj, not the discarded obj/main optimized build. Later clean reproduction with build.py also passed. |
| The HEX output directory was missing after ELF linking. | Create BIN_DIR explicitly and finish HEX generation. | The validated-artifact build sequence records the failed HEX command and its successful completion separately. |
| The newer host ACTUAL-rates inverse over-commanded the old linear ANGLE controller, producing roughly 20-degree attitude excursions. | Use the release's linear stick mapping and its upstream 55-degree level limit. Do not retune the firmware controller. | Diagnostic flight reached all corners but failed LAND timeout; final peak sampled roll/pitch were below 10 degrees and full landing passed. |

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
- Peak GPS height: **15.5 m**; final N/E:
  **0.0/-0.0 m** (console rounding).
- Final relative altitude: **0.00 m**, landed and disarmed.
- Peak sampled absolute roll/pitch: 9.90/9.90 deg.
- Physical raw reads: MPU6000 SPI **746,088**, BMP280 I2C **3,316**.
- Plant pressure encoded through raw BMP280 ADC:
  **99362.6–99546.6 Pa**;
  peak modeled gyro rate **59.0 deg/s**.
- QEMU wait wall time: 72.831 s. Last periodic FMU report:
  81.044 simulated seconds, 1.16x.
  This periodic report is a lower bound on final virtual time, not its exact end.
- Recording: 154 samples over 68.052 wall seconds
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
fastdyn-env/bin/python "$BF_SWEEP/run.py" 4.4.3
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/mavlink/replay_betaflight.py \
  play "$BF_SWEEP/4.4.3/evidence/flight.json" --port 18500
```

Use `build.py 4.4.3 --toolchain /path/to/gcc-10.3.1` for an isolated review
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
