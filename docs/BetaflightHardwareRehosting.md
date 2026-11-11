# Betaflight F405 Hardware Sensor Rehosting

Status on 2026-11-10: **MATEKF405CTR hardware sensor rehost and closed loop
mission verified.** The target is an STM32F405 board with an
MPU6000 gyro/accelerometer on SPI and a BMP280 barometer on I2C. Its existing
Betaflight board configuration and firmware sources are unchanged.

The implementation preserves those physical sensor drivers and supplies their
bus responses from the FastDyn physics model. The earlier FASTDYNF405
virtual-sensor flight demonstration is a separate prototype; its runtime results
do not qualify this hardware target.

Related documents: [release version sweep](BetaflightVersionSweep.md),
[portable reference replay](BetaflightReplay.md),
[hardware target README](../virtuals/physics/flight_controllers/courbet/betaflight/matekf405ctr/README.md).

## Validation levels

The [PX4 FMUv6C version sweep at commit 127722e6](https://github.com/boardrunner2049/FastDyn/blob/127722e60138765c765f53a7cfca4cdd869ceda9/virtuals/physics/flight_controllers/courbet/px4/version_sweep/README.md)
provides the model for recording firmware identity, separate runtime milestones,
per-build ABI checks, and unresolved failures. Its PX4 uORB adapters are specific
to that stack. The Betaflight hardware target needs its own SPI/I2C boundary.

| Level | Required evidence | MATEKF405CTR status |
| --- | --- | --- |
| Build verified | ARM ELF links, fits memory, retains symbols/DWARF, contains the physical drivers, and excludes virtual sensor drivers | Passed |
| Profile validated | New ELF hash, vectors, hook symbols, instruction locations, and consumed ABI layouts pass generation checks | Passed |
| Firmware slicing verified | Firmware boots; MPU6000 and BMP280 detect and process changing bus data; motor commands drive the plant | Passed |
| Closed loop flight verified | Firmware receives sensor feedback and completes the declared control/mission sequence, landing and disarm, with recorded telemetry | Passed |

A passing compilation establishes the first level only. A board change or ELF
change requires the later levels to be established for that exact artifact.

## Firmware configuration and identity

This section records the initial development-snapshot checkpoint. The subsequent
[release version sweep](BetaflightVersionSweep.md) validates six additional
release images with their own pinned profiles and reports.

| Item | Recorded value |
| --- | --- |
| Firmware version | `2026.12.0` development snapshot |
| Firmware commit | `5ce286c9676348494224837959b1bf742041279f` |
| Board | `MATEKF405CTR`, manufacturer `MTKS` |
| Board configuration commit | `cacb3a06d7d90abd9c71255318ee95a59fa333f7` in `betaflight/src/config` |
| Board configuration | `src/config/configs/MTKS/MATEKF405CTR/config.h` in the Betaflight checkout |
| Board configuration SHA-256 | `9ab40ba5d9ad448dadcbd7857231685641b6634fd9e5caed45766d5b9d7c075a` |
| MCU and ABI | STM32F405, Cortex-M4, Thumb-2, ARM EABI v5, hard float |
| Toolchain | `arm-none-eabi-gcc` 13.2.1, dated 20231009 |
| Build options | `DEBUG=GDB`, `-Og`, `-ggdb2`, DWARF-5, `LTO=no` |
| Source and configuration changes | None; both Git worktrees are clean |
| ELF filename | `betaflight_STM32F405_MATEKF405CTR.elf` |
| ELF file size | 4,825,240 bytes, including debug information |
| ELF SHA-256 | `235afa1124b43784bebed4aa14afb883c7cb16e4fece853d771015888df5ecb7` |
| Entry point | `0x0800d249` |

The firmware embeds `2026.12.0`. The checkout's Git description,
`4.5.0-1561-g5ce286c96`, refers to ancestry from an older tag and does not identify
this snapshot as a Betaflight 4.5 release.

Build artifacts are local, ignored files under
`out/betaflight/hardware_f405/MATEKF405CTR/` from the FastDyn root:

- `betaflight_STM32F405_MATEKF405CTR.elf`, `.bin`, and `.map`.
- `betaflight_2026.12.0_STM32F405_MATEKF405CTR.hex`.
- `build-report.json`, `build.log`, `board-config.h`, and `size.txt`.
- `preprocessor-defines.txt`, `symbols.txt`, `elf-header.txt`, and
  `compile-command.txt` for the static audit.

The runnable profile and unchanged ELF are installed under
`virtuals/physics/flight_controllers/courbet/betaflight/matekf405ctr/`.
The ELF file size includes debug information; the linked memory use is smaller:

| Linker region | Used bytes | Region capacity |
| --- | ---: | ---: |
| FLASH vectors | 392 | 16 KiB |
| FLASH configuration | 0 | 16 KiB |
| FLASH1 firmware | 533,628 | 992 KiB |
| RAM | 88,348 | 128 KiB |
| CCM | 13,396 | 64 KiB |

## Physical sensor boundary

The unmodified board configuration selects:

| Component | Board connection | Driver contract to preserve during rehosting |
| --- | --- | --- |
| MPU6000 gyro and accelerometer | SPI1, chip select PC2, data ready PC3; `CW270_DEG` alignment | Device identity, configuration writes, sensor register reads, byte order and scaling, and the selected interrupt/transfer mode |
| BMP280 barometer | I2C1 | Chip identity, calibration coefficients, control writes, raw pressure/temperature samples, and firmware compensation |

The implemented feedback path is:

```text
Rumoca FastDyn.Copter sensor quantities
  -> target adapter encodes MPU6000 and BMP280 bus responses
  -> physical Betaflight sensor drivers decode and compensate samples
  -> Betaflight sensor processing, estimation, controllers and mixer
  -> motor-output adapter -> FastDyn.Copter plant
```

The adapter in `flight_controllers/betaflight/hardware.c` operates below sensor
decoding and compensation. `spiSequence` fills transfer buffers; I2C bus-device
hooks supply BMP280 registers. `mpuGyroReadSPI`, `mpuAccReadSPI` and
`bmp280Calculate` execute in ARM firmware and have observation-only trace rules.
There are no direct adapter writes to decoded IMU or compensated barometer
fields. The register encodings and coefficients follow the pinned driver source
and its BMP280 reference tests.

The closest existing reference is ArduPilot's Invensense `_block_read()` hook,
which supplies sensor-formatted data to a physical driver. Betaflight's transfer
shape differs: this MPU implementation has SPI segments and optional DMA, so
ArduPilot's FIFO layout and batching cannot be copied unchanged. Both approaches
use function-level hardware substitution; neither by itself establishes a full
STM32 peripheral model or cycle-accurate SPI/I2C transactions.

## Verified build properties

The saved preprocessing output contains `USE_GYRO_SPI_MPU6000`,
`USE_ACC_SPI_MPU6000`, `USE_BARO_BMP280`, `USE_SPI`, and `USE_I2C`.
`ENABLE_SIMULATOR` is zero. The virtual gyro, accelerometer, barometer,
magnetometer, and GPS selection macros are absent.

The linked ELF contains `mpu6000SpiDetect`, `mpu6000SpiGyroDetect`,
`mpu6000SpiAccDetect`, `mpuGyroReadSPI`, `mpuAccReadSPI`,
`mpuGyroStartDmaRead`, `bmp280Detect`, `bmp280Calculate`,
`bmp280CompensatePressure`, `spiSequence`, and `i2cRead`. The corresponding
virtual sensor entry points are absent. Compilation and linking returned zero
without warnings. Runtime traces separately establish original driver execution,
and the completed mission establishes feedback through the plant.

## Implementation and remaining limits

| Area | Implemented behavior and limits |
| --- | --- |
| MPU6000 detection | Identity/product registers and configuration writes supplied; original detection/init/read functions retained |
| SPI transfers | Synchronous, bounded segment transport with command prefixes and chip-select continuation; original wrappers and decoding retained |
| Interrupts and DMA | Driver naturally selects `GYRO_EXTI_NO_INT`; `spiUseDMA` reports false. DMA completion and sensor EXTI timing are outside this profile |
| BMP280 | Calibration and raw pressure/temperature samples supplied; original ARM integer compensation retained. I2C operations complete synchronously |
| Sensor timing | Driver advertises 8 kHz gyro and 1 kHz accelerometer; `pid_process_denom=8` gives 1 kHz PID. FMU steps at 1 ms and samples are held between steps |
| Coordinate frames | FRD plant axes are transformed into the MPU mounting frame; the original `CW270_DEG` alignment maps them into the firmware body frame |
| Initialization | Clock/RTC time uses shared callbacks; absent internal ADC init/busy is skipped. SD, blackbox, OSD and ADC battery/current sources are disabled by parameters |
| Serial, receiver and GPS | MAVLink RC/telemetry on UART1, MSP on required USB plus UART2, UBX serial GPS on UART3; original protocol parsers execute |
| Motor protocol | Runtime selects PWM and the shared four-motor plant mapping; original controllers and mixer remain active |

The runtime configuration observer runs before `featureInit`. It selects GPS,
telemetry and serial RX features, the UART roles above, PWM, AUX1 ARM and AUX2
ANGLE at 1700–2100 microseconds, and the PID divider. It changes guest parameter
groups; it does not change firmware source or the ELF. Every consumed offset is
checked against DWARF. The serial validator requires USB MSP to remain assigned;
removing it reset all UART claims during bring-up, so the final settings keep it.

At the scheduler's first pass, the bridge calls the firmware's
`accStartCalibration`; real samples complete calibration and save the result in
guest flash. No `accHasBeenCalibrated`, compass-detection or arming-check override
is present. `icount.shift=5` caused the real late-task check to reject arming;
`shift=3` with the 1 kHz PID configuration passed it without changing the check
or its threshold. Instruction timing is not physical cycle timing.

The board has no modeled external compass. GPS course may affect reported yaw;
the host pilot still assumes north-facing initial physical yaw and neutral yaw
input. Settings live only within the current QEMU process. The waypoint mission
is host RC control, not onboard waypoint execution.

## ABI and instruction validation

Bindings are generated for this exact hardware ELF. The shared
`courbet/profiles/betaflight` validator now checks the additional bus and runtime
configuration layouts when the physical MPU6000 driver is present. The profile
pins its own SHA-256, flash/RAM vector addresses, stack address, initial vector
words and `run+2` instruction bytes.

| Consumed boundary | Validation performed |
| --- | --- |
| `gyroDev_t`, `accDev_t`, `baroDev_t` | DWARF sizes and every member offset touched by an adapter |
| `extDevice_t`, `busDevice_t`, `busSegment_t` | Bus/device identity, register/address representation, buffers, lengths, callbacks and completion state |
| UART, motor and GPS structures | Actual linked definitions and the selected configuration; no offsets inherited without checking |
| Time and interrupt hooks | Vector entries, instruction signatures, register liveness and calling convention at each selected hook |
| Generated rule files | Exact ELF hash and symbol resolution; reject mismatches before replacing existing generated output |

Keep boot failures and estimator/arming failures as explicit failed milestones.
A workaround should be retained only with a reason and evidence showing its
effect. A working visualization alone is not proof of sensor-driver coverage.

## Reproduce the build and inspect the artifact

From the FastDyn root, with the existing Betaflight checkout beside it and ARM
GCC 13.2.1 installed:

```bash
BF_SOURCE="$PWD/../betaflight"
BF_OUTPUT="$PWD/out/betaflight/hardware_f405/MATEKF405CTR"
mkdir -p "$BF_OUTPUT/obj/main"

make -C "$BF_SOURCE" CONFIG=MATEKF405CTR \
  CONFIG_DIR="$BF_SOURCE/src/config" \
  DEBUG=GDB LTO=no GCC_REQUIRED_VERSION=13.2.1 \
  OBJECT_DIR="$BF_OUTPUT/obj/main" BIN_DIR="$BF_OUTPUT/obj" \
  -j16 fwo

BF_ELF="$BF_OUTPUT/obj/main/betaflight_STM32F405_MATEKF405CTR.elf"
arm-none-eabi-readelf -h -A "$BF_ELF"
arm-none-eabi-size "$BF_ELF"
arm-none-eabi-nm -n "$BF_ELF" \
  | rg 'mpu6000|mpuGyroReadSPI|mpuAccReadSPI|bmp280|spiSequence|i2cRead'
sha256sum "$BF_ELF"
```

The recorded artifact was built from the commits above with initialized config
and MAVLink submodules. Rebuilding can change timestamps and the ELF hash; retain
the resulting artifact's own provenance and regenerate its bindings. This build
command produces ELF/map/HEX output under `obj`; the current checkpoint also
retains top-level copies, a BIN image, and the static audit files listed above.

## Run the hardware target

```bash
fastdyn-env/bin/fastdyn run -c configs/betaflight_f405.toml
tests/integration/betaflight_matekf405ctr_smoke.sh
```

`configs/betaflight_f405.toml` selects MATEKF405CTR. This PR distributes the
physical-sensor profiles; the earlier custom-board experiment remains outside
this change. For SSH, forward TCP 15500 and open the page while FastDyn runs:

```bash
ssh -N -L 15500:127.0.0.1:15500 your-ssh-host
```

The local browser URL is `http://127.0.0.1:15500/mavcesium/`. Set
`FASTDYN_BF_VIEW_SECONDS=0` for immediate exit after landing; the default is 120
seconds, with QEMU paused after the mission. Existing altitude, waypoint and port
environment overrides remain available.

## Runtime verification sequence

Validation proceeds through these stages in order:

1. Validate the new profile against the hardware ELF, including the physical
   driver ABI and any interior instruction hooks.
2. Boot to the scheduler; demonstrate MPU6000 and BMP280 detection with logged
   bus transactions and sensor outputs.
3. Check stationary gravity/pressure and signed responses to known plant
   changes through the physical drivers, including timing and axis alignment.
4. Exercise motor stop and bounded output changes through the plant; confirm
   sensor feedback responds to those motor outputs.
5. Confirm protocol handling, calibration, arming, stabilization and recovery.
   Record the behavior of every changed or bypassed check.
6. Run the declared mission with altitude, route, landing, disarm and viewer
   telemetry criteria. Label host RC control and onboard navigation separately.

The full hardware-target smoke passed with 3/3 waypoints, maximum relative
altitude 15.0 m, landing 0.10 m from home, final relative altitude 0.00 m,
confirmed disarm, a completed viewer recording and exit zero. Its 73.9-second
run recorded 769,287 MPU6000 SPI reads and 3,418 BMP280 I2C reads; pressure ranged
from 99,368.6 to 99,546.7 Pa and peak angular rate was 84.5 deg/s. These are
simulation results through physical drivers, not tests on a physical aircraft.

Evidence under `out/betaflight/hardware_f405/MATEKF405CTR/` includes
`full-flight.log`, `full-flight-work/betaflight_flight.json`,
`flight-driver-state.txt` and `prototype-regression.log`. The GDB snapshot shows
the original `mpuGyroReadSPI` function pointer, polling mode, 8 kHz sample rate,
completed accelerometer calibration and zero late-task percentage. The custom-board regression experiment is not part of this PR.

`tests/unit/test_betaflight_hardware.py` exercises raw SPI identity/reset/range
and axis encoding, segmented transfers, BMP280 compensation against plant values,
absent I2C devices, UBX checksums and units, profile generation and rejection of a
wrong ELF without replacing bindings. The integration smoke also requires
original physical-driver trace entries and changing sensor statistics.

## Future version coverage and change ownership

Future version rows must pin the Betaflight source, matching board configuration,
toolchain, build options, ELF hash, ABI and instruction checks, adapter choice,
and separate boot/sensor/motor and mission outcomes. A release lacking the
selected board/sensor combination needs a separately identified target. A common
MCU name does not establish interchangeable board wiring or sensor-driver ABI.

Keep firmware-specific bus adapters in
`virtuals/physics/flight_controllers/betaflight/`, target profiles and symbolic
rules under Courbet's Betaflight directories, and ABI checks in the corresponding
profile validator. Reuse the shared physics and configuration-generation APIs.
Changes to FastDyn core or generic APIs need a demonstrated shared requirement
and their own verification.

This hardware stage changes the Betaflight target adapters, bindings, runtime
configuration, tests and documentation. It adds no generic FastDyn runtime API
or schema changes. The earlier session's runner cancellation fixes remain
separate. Firmware source and the board configuration remain unchanged, and the
runtime ELF retains the compiled artifact's original hash.
