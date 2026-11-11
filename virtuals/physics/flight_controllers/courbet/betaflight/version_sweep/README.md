# Betaflight F405 hardware-target version sweep

Validated on **2026-11-11**: all six pinned release images boot, execute their
physical MPU6000/BMP280 drivers, drive the Copter FMU, and complete the declared
three-waypoint flight, return, landing and disarm. The default
`configs/betaflight_f405.toml` still selects the previously validated
`2026.12.0` development snapshot; this sweep has separate profiles and runners.

This uses firmware slicing: ARM hardware-driver code runs, while selected bus,
clock, flash and UART operations use host substitutions. No virtual gyro,
accelerometer, barometer, magnetometer or GPS target, or FASTDYNF405 board, is used.
The sensors are
register models fed by the physics plant, not physical devices attached to the
server. This does not establish full STM32 MMIO, DMA/EXTI or cycle accuracy.

The report follows the separation of build, slicing and mission results in the
[PX4 FMUv6C sweep](https://github.com/boardrunner2049/FastDyn/blob/127722e60138765c765f53a7cfca4cdd869ceda9/virtuals/physics/flight_controllers/courbet/px4/version_sweep/README.md)
and the observed-failure/resolution/evidence format in
[iNav PR #54](https://github.com/boardrunner2049/FastDyn/pull/54).
The iNav report has a different integration and smaller validation scope; its
adapters and runtime results are not evidence for this sweep.

## Release selection and hardware identity

The sample covers six release lines and three board-configuration mechanisms.
It is a compatibility sample, not a test of every release or every F405 board.

| Release / report | Firmware commit | Upstream target/configuration | GCC | Receiver |
| --- | --- | --- | --- | --- |
| [2026.6.2](2026.6.2/README.md) | `e0b7bb01b` | `CONFIG=MATEKF405CTR`, grouped board config | 13.3.1 | MAVLink UART1 |
| [2025.12.5](2025.12.5/README.md) | `7348054f2` | `CONFIG=MATEKF405CTR`, flat board config | 13.3.1 | MAVLink UART1 |
| [4.5.5](4.5.5/README.md) | `4adbd3ef7` | `CONFIG=MATEKF405CTR`, flat board config | 10.3.1 | MSP UART2 |
| [4.4.3](4.4.3/README.md) | `738127e7e` | `TARGET=STM32F405` + MTKS-MATEKF405CTR unified settings | 10.3.1 | MSP UART2 |
| [4.3.2](4.3.2/README.md) | `60c9521da` | built-in `TARGET=MATEKF405` | 9.3.1 | MSP UART2 |
| [4.2.11](4.2.11/README.md) | `948ba6339` | built-in `TARGET=MATEKF405` | 9.2.1 | MSP UART2 |

The fixed active sensor combination is STM32F405 / MPU6000 on SPI1 (CS PC2,
data-ready PC3, CW270 mounting) / BMP280 on I2C1 (PB6/PB7, address 0x76).
The legacy MATEKF405 target also supports other sensors; modeled identities
select its original MPU6000/BMP280 branches. Target names are not interchangeable
board identifiers.

[plan.json](plan.json) and each `abi.json` pin the full source/config commits.
The config repository commits are `3a406c5c30c20d7c3bc7dd5ed547cca961baf0a9`
for 2026.6.2 and `92c695af8a2756d75152b54959de28d48fc25cf7` for
2025.12.5/4.5.5. For 4.4.3,
[the official unified configuration](https://github.com/betaflight/unified-targets/blob/7f1abbf7844d3299ecf2ed4cca4680dc95a941b5/configs/default/MTKS-MATEKF405CTR.config)
is pinned at `7f1abbf7844d3299ecf2ed4cca4680dc95a941b5`.
Its active bus/pin/alignment/motor/timer settings are applied to parameter groups
before peripheral initialization. The CLI text is retained as provenance; the
CLI parser is not the provisioning path. OSD, storage and battery features are
disabled. The 4.4.3 report explains this configuration boundary.

## Measured time to rehost

These are actual session measurements, not estimates of future effort.
[Raw command records](evidence/steps.jsonl), [CSV](evidence/results.csv),
[JSON results and definitions](evidence/results.json),
[command output](evidence/logs/) and per-version console/timing logs are retained.
`measure.py` records monotonic duration, command, UTC boundaries, exit status and
output for successful and failed attempts.

| Release | Validated-image build sequence (s) | All build attempts (s) | Passing mission check (s) | Rehosting window (min) | Failed mission attempts |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026.6.2 | 7.868 | 7.868 | 77.889 | 8.35 | 2 |
| 2025.12.5 | 7.512 | 7.518 | 78.118 | 16.13 | 2 |
| 4.5.5 | 6.779 | 8.696 | 69.499 | 17.03 | 0 |
| 4.4.3 | 6.735 | 39.357 | 73.969 | 22.77 | 3 |
| 4.3.2 | 12.283 | 14.680 | 77.365 | 22.82 | 2 |
| 4.2.11 | 12.274 | 14.501 | 86.086 | 22.97 | 2 |

- **Build sequence** measures commands that produced the validated image.
  For 4.4.3 it sums the flash translation-unit build, remaining build/link and
  HEX completion: 0.636 + 4.932 + 1.167 s. The middle command linked the ELF but
  failed because its HEX output directory was missing; the final command completed
  HEX generation. Discarded variants are included only in the attempts column.
- **Mission check** includes helpers, QEMU startup, FMU execution, flight and
  smoke assertions, with the postflight viewing delay disabled.
- **Rehosting window** spans the first recorded per-version build command to its
  first qualifying mission end. It includes investigation, adapter work, retries,
  work on other versions and the brief user interruption. Windows overlap: they
  are not exclusive engineering time and must not be added together.
- Shared survey/document review began at **18:34:44 UTC**. The first measured
  build began at **18:39:34 UTC**. All six first qualified by **19:06:47 UTC**,
  about **32 min** after the survey began. Documentation, packaging and regression
  work continued afterwards, outside these first-qualification windows.
- Shared compiler download/extraction took 65.103 s (GCC 10.3.1), 56.106 s
  (9.3.1), and 46.821 s (9.2.1), concurrently. These costs are separate; their sum
  is not elapsed wall time. GCC 13.3.1 was already installed.
- Four early background launch logs lack completed timing records and are
  excluded from measured sums. The failed-attempt counts include three stopped
  diagnostic runs using the wrong pre-4.5 host stick mapping; those are not three
  additional firmware defects.

The host already had FastDyn, patched QEMU, a Copter FMU, the development rehost
and its sensor models. This measures incremental release adaptation on that
setup, not a from-scratch project or firmware performance benchmark.

## Flight results and validation levels

All six separately pass **build**, **hash/instruction/DWARF profile validation**,
**physical sensor/motor slicing**, and **closed-loop mission**. The mission is
a host RC pilot using firmware ANGLE stabilization, not an onboard waypoint
mission implementation.

The common plant and route are unchanged: take off to 15 m above home, visit
N/E `(30,0)`, `(30,30)`, `(0,30)` m, return to `(0,0)`, land, settle and disarm.
Arrival uses a 3 m horizontal radius, 1 m altitude tolerance and 0.75 m/s
horizontal-speed limit. Ground checks require altitude within 0.25 m and settled
vertical motion. The smoke test and host pilot define the exact checks.

| Release | Waypoints | Peak GPS height (m) | Final N/E (m, rounded) | Peak sampled roll/pitch (deg) | Samples |
| --- | --- | ---: | --- | --- | ---: |
| 2026.6.2 | 3/3 | 15.1 | 0.0 / 0.1 | 11.6 / 10.4 | 419 |
| 2025.12.5 | 3/3 | 15.0 | 0.0 / 0.1 | 10.0 / 10.1 | 165 |
| 4.5.5 | 3/3 | 16.2 | -0.0 / 0.1 | 10.0 / 9.9 | 166 |
| 4.4.3 | 3/3 | 15.5 | 0.0 / -0.0 | 9.9 / 9.9 | 154 |
| 4.3.2 | 3/3 | 17.0 | -0.0 / 0.1 | 10.0 / 10.0 | 167 |
| 4.2.11 | 3/3 | 16.9 | 0.1 / 0.1 | 10.0 / 9.9 | 164 |

Each run finished at 0.00 m relative altitude and confirmed disarm. The older
telemetry is slower and lacks velocity fields; its host pilot differentiates
received GPS positions using firmware timestamps. Altitude overshoot and sample
differences are retained. Passing does not establish identical tracking,
estimator accuracy, noise robustness, hardware timing or real-aircraft tuning.

Each report includes sensor read counts, changing pressure range and gyro
response. Observation-only trace hooks establish execution of original ARM
MPU6000 read/decode and BMP280 compensation functions. Those functions are not
replaced with virtual sensor callbacks.

## Run a selected release

From the FastDyn root, after normal FastDyn/QEMU/FMU setup:

```bash
BF_SWEEP=virtuals/physics/flight_controllers/courbet/betaflight/version_sweep
fastdyn-env/bin/python "$BF_SWEEP/run.py" 2026.6.2
fastdyn-env/bin/python "$BF_SWEEP/run.py" 4.2.11
```

The runner verifies the pinned profile, generates addresses for this checkout,
derives a runtime config from the default hardware config, supplies the correct
UART IDs, RC protocol, ANGLE mapping and GPS packet format, then runs the mission
and physical-sensor assertions. It reserves HTTP 18500 and ports 18551, 18552,
18556 and 18560. Use `--port 18600` for another instance. It writes a new
timestamped output directory and leaves the default config intact.

For SSH viewing, forward the HTTP port **on the local computer**:

```bash
ssh -N -L 18500:127.0.0.1:18500 your-ssh-host
```

Open `http://127.0.0.1:18500/mavcesium/` during the run. The smoke runner closes
the viewer when validation ends. Use saved playback afterwards; a tunnel does
not start a remote web server.

## Replay reference data without rerunning firmware

Each version retains a small `evidence/flight.json`, not an MP4:

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/mavlink/replay_betaflight.py \
  play "$BF_SWEEP/4.2.11/evidence/flight.json" --port 18500
```

Use the same SSH forward and viewer URL. This plays recorded telemetry;
`run.py` starts a fresh firmware/physics mission. Use `run.py` for sweep reruns
because it supplies release-specific receiver/GPS settings. The development
snapshot's frozen reference bundle remains separate. Video export is described
in [BetaflightReplay.md](../../../../../../docs/BetaflightReplay.md).

## Rebuild from pinned source

`build.py` needs a Betaflight Git repository containing the commits and the
compiler version in the selection table. Initialize its `src/config` submodule
for config-repository builds. It creates isolated worktrees under ignored
`out/`, checks source cleanliness, and keeps validated binaries intact:

```bash
fastdyn-env/bin/python "$BF_SWEEP/build.py" 2026.6.2 \
  --source ../betaflight --toolchain /path/to/gcc-13.3.1
fastdyn-env/bin/python "$BF_SWEEP/build.py" 4.4.3 \
  --source ../betaflight --toolchain /path/to/gcc-10.3.1
```

Compiler installations must contain `bin/arm-none-eabi-gcc`. Upstream download
URLs and setup times are in [toolchain-setup.jsonl](evidence/toolchain-setup.jsonl);
archive hashes are in [environment.json](evidence/environment.json). The recorded
GCC 13.3.1 build used installed GNU Tools for STM32 13.3.rel1. Other compilers were
downloaded from Arm using the release Makefiles' URLs.

All builds use Cortex-M4 hard float and `DEBUG=GDB` without LTO. On older releases,
the debug branch disables LTO; a `LTO=no` variable alone does not control it.
4.4.3 compiles only `flash_m25p16.c` at `-O0` to avoid its GCC 10.3.1 debug
`maybe-uninitialized` failure, retaining `-Werror`; the rest uses `-Og`.
Its debug information uses `-ggdb2 -gdwarf-5`. No firmware source patch is used.

Build paths and timestamps can change ELF hashes. To adopt a reviewed rebuild:

```bash
fastdyn-env/bin/python "$BF_SWEEP/prepare.py" 2026.6.2 --elf /path/to/new.elf
fastdyn-env/bin/python "$BF_SWEEP/run.py" 2026.6.2
```

`prepare.py` changes that version's ELF, ABI manifest, symbolic rules, parameters
and hash/instruction checks. It also checks the embedded source revision and MCU
target. Review the diff before accepting it; generation is not runtime validation.
The runner saves candidate evidence under `out/`. Before promoting a new artifact,
update its report, result identity, evidence and measurements together. The
checked-in results describe each `result.json` hash, not later builds; a failed
candidate must not inherit an old pass claim.

## Files, checks and limits

- `plan.json`: exact release/source/config/compiler selection.
- `<version>/bin/*.elf`, `profile.toml`, `abi.json`, `parameters.txt`,
  `labeled_conf/`: pinned artifact, consumed layouts, explicit settings,
  symbolic slicing rules and instruction checks. `unlabeled_conf/` is regenerated.
- `<version>/README.md`, `result.json`, `evidence/`: challenges, measurements,
  console, FastDyn timing, sensor statistics and replay data.
- `evidence/`: command timings/failures, results, compiler setup and environment
  identity. Large peripheral I/O traces stay out of Git.

Static audits retain reconstructed compiler flags, preprocessing output,
driver symbols and ELF load/ABI information per version. Stock Betaflight also
compiles an optional `USE_VIRTUAL_CURRENT_METER` estimator; the current/voltage
sources are explicitly NONE here. That inactive feature is not a replacement
for the MPU6000 or BMP280. Virtual IMU/barometer/magnetometer/GPS selections and
simulator builds are absent.

```bash
fastdyn-env/bin/python -m pytest -q tests/unit/test_betaflight_hardware.py \
  tests/unit/test_betaflight_mission.py tests/unit/test_betaflight_sweep.py \
  tests/unit/test_betaflight_replay.py tests/unit/test_betaflight_flight_view.py \
  tests/unit/test_betaflight_video.py
```

The bus model is synchronous. Hardware decoding, calibration, filtering, PID,
mixer and normal receiver/failsafe processing remain in ARM firmware. Clock/RTC
substitutions and `icount.shift=3` provide usable virtual time, not a physical
timing benchmark. Guest settings reset each run. GPS fix/accuracy are ideal;
compass and peripheral timing/noise are not modeled. No arming-check override,
virtual sensor selection or onboard mission executor was added. No FastDyn core
API or generic schema change was needed for the sweep.

The shared runtime uses the Cortex-M4 QEMU machine and STM32F427 SVD routing,
as documented in the original hardware guide. The compiled MCU/board target is
STM32F405. The shared routing choice is not a claim of complete F405 peripheral
emulation.

Final checks: **67 focused tests passed**, all six strict profiles regenerated,
another 2026.6.2 mission passed through `run.py`, and the original development
hardware smoke passed (3/3, peak 15.0 m, landing 0.10 m). Clean script rebuilds
also passed for 2026.6.2 and 4.4.3. See
[validation.json](evidence/validation.json) for these checks, separate from the
first-qualification measurements above.

Firmware and board configuration retain upstream GPL-3.0-or-later licensing; see
[LICENSE-BETAFLIGHT](LICENSE-BETAFLIGHT) and NOTICE. The pinned commits and build
script identify corresponding sources.
