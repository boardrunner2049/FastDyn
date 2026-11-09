# PX4 Firmware Slicing

FastDyn runs the unmodified PX4 FMUv6C ARM/NuttX ELF in patched QEMU and connects it to
an FMI 3 vehicle plant. This is firmware slicing: selected hardware-only functions are
bypassed and selected firmware data boundaries are bridged, while the NuttX scheduler,
uORB, sensor processing, EKF2, commander, navigation, and controllers continue to run.

The target is real Pixhawk-target firmware on an emulated board. It is neither host-native
PX4 SITL nor a register-complete STM32H743/Pixhawk 6C hardware model.

## Pinned firmware

The maintained target is the newest stable PX4 release available when this profile was
updated:

| Item | Value |
| --- | --- |
| PX4 release | `v1.17.0` stable |
| Source commit | `d6f12ad1c4f70ad3230afd7d86e971421e02fef4` |
| Board target | `px4_fmu-v6c_default` |
| Guest | ARM Cortex-M7 / NuttX |
| Compiler | GNU Arm Embedded `13.2.1 20231009` |
| Checked-in ELF SHA-256 | `e6ed5d1860c61febecf520a3809741bbcfb4dcc14294338dc570f16ca07fc010` |

PX4 publishes a flashable `.px4` release asset, not the unstripped ELF required by FastDyn.
FastDyn therefore builds the ELF from the official tag and its pinned submodules.
The ELF must retain its symbol table and DWARF information so the configuration generator
can validate hook symbols, instruction bytes, and guest structure layouts.

The locally built package is not expected to be byte-identical to PX4's published `.px4`
asset. Build metadata and the host build environment affect the linked image. The source
tag, full commit, toolchain, ELF digest, and generated hook digest are the reproducibility
boundary for this profile.

PX4's DroneCAN generator still imports `pkg_resources`. With contemporary Python packaging,
the isolated PX4 build environment must pin `setuptools<81` in addition to installing
`Tools/setup/requirements.txt`.

## Why PR #52 is a reference, not an integration

[PR #52](https://github.com/boardrunner2049/FastDyn/pull/52) implements a host-native PX4
SITL endpoint. It sends `HIL_SENSOR` and `HIL_GPS` over the PX4 Simulator MAVLink API and
receives `HIL_ACTUATOR_CONTROLS`. It does not run ARM firmware or NuttX and is deliberately
not merged into this path.

The PR is useful as an executable statement of the simulator boundary:

| SITL boundary in PR #52 | Firmware-slicing equivalent | Firmware path |
| --- | --- | --- |
| `HIL_SENSOR` | Replace simulation sensor samples at uORB publication | `sensor_accel`, `sensor_gyro`, `sensor_mag`, `sensor_baro` |
| `HIL_GPS` | Replace the `sensor_gps` uORB sample | `sensor_gps` |
| `HIL_ACTUATOR_CONTROLS` | Observe PX4's simulated PWM output topic | `actuator_outputs_sim` |
| Host fixed-step loop | QEMU virtual time and the guest HRT interrupt | TIM8 IRQ 62 at 1 ms |
| TCP backpressure policy | Not applicable to the in-process FMU bridge | Direct guest-memory and `phy_*` calls |

This mapping preserves the SITL contract while moving execution into the actual board
firmware. It does not copy the PR's Python runtime into FastDyn core.

## Current slicing boundary

The profile starts PX4's built-in SIH sensor publishers. A virtual instruction at
`uORB::DeviceNode::publish` identifies selected topics by `orb_metadata::o_name` and replaces
the sample fields with FMI values before the real broker copies them. PX4 then executes its
normal downstream processing.

The boundary intentionally sits above SPI/I2C and the physical sensor drivers. Consequently,
it validates estimator, commander, control, scheduling, uORB, and MAVLink behavior, but it
does not validate sensor-register protocols, DMA behavior, physical driver initialization,
or driver-level mounting rotations.

The profile also slices hardware-only initialization that cannot complete on the generic
Cortex-M machine: STM32 clocks, low-level board setup, DMA setup, board identity, I2C, SDIO,
USB, persistent parameter autosave, and the ROMFS startup script. The replacement `boot.nsh`
starts the required real PX4 modules explicitly.

## SITL-to-firmware mapping challenges

### 1. Frames and mounting orientation

PR #52 defines accelerometer, gyro, and magnetometer values as body FRD. The FMI interface
uses the same contract. PX4's `sensor_*` topics are already past the raw chip-axis and driver
rotation stage, so the uORB bridge must inject body-FRD values directly and must not repeat a
board or sensor rotation.

A lower slicing point at an SPI/I2C driver would require the opposite decision: manufacture
raw chip-axis samples and let the real driver apply the board-specific rotation. Mixing these
two levels produces plausible magnitudes with incorrect signs or swapped axes.

### 2. Calibration ownership

SITL generally provides calibrated physical units. The current uORB boundary likewise
bypasses raw sensor scale, offset, and temperature calibration in the physical driver. PX4's
downstream estimator bias handling and any calibration applied after `sensor_*` publication
still run, but this profile is not evidence that board-level accelerometer, gyro, or compass
calibration works.

Device IDs and untouched fields come from PX4's SIH publishers. Starting a different publisher
or changing its device ID can activate different calibration parameters even when the injected
numeric samples are unchanged.

### 3. Units and ABI layouts

The bridge uses PX4 uORB units, not MAVLink wire units:

- acceleration: m/s²;
- angular velocity: rad/s;
- magnetic field: gauss;
- barometric pressure: Pa;
- temperature: degrees Celsius;
- GPS latitude/longitude: degrees;
- GPS altitude: metres;
- GPS velocity: NED m/s;
- actuator output: normalized, then mapped to the plant's PWM range.

PR #52 converts pressure to hPa and GPS values to scaled integers because MAVLink requires
those encodings. Applying those conversions at the uORB boundary would be incorrect. Every
guest field offset used by the bridge is checked against ELF DWARF before hooks are generated.

### 4. Time, rates, and sample age

PR #52 is paced by host wall time. The firmware path instead advances the FMU from QEMU virtual
time, services PX4's HRT callout queue through TIM8 IRQ 62, and runs NuttX SysTick separately.
Host throughput may change without changing guest-visible time.

Sensor rates are owned by the PX4 simulation publishers and work queues. The hook changes
sample contents, not their publication schedule or timestamps. A future publisher change can
therefore alter estimator behavior even if the bridge ABI remains compatible.

### 5. Perfect sensors and stale detection

A static, noise-free FMU can repeatedly produce bit-identical sensor values. PX4's validators
may classify that as a stale sensor. The firmware adapter adds small deterministic, zero-mean
dither to acceleration, gyro, magnetic field, and pressure. This keeps the real validation
path active; it is not a calibrated sensor-noise model. A simple alternating sequence is not
sufficient because VehicleIMU integration can cancel it exactly.

### 6. GPS cadence and altitude datum

GPS publication cadence comes from `sensor_gps_sim`, while the bridge replaces position and
velocity fields. The FMI currently has no geoid model, so the profile supplies the same value
for mean-sea-level and ellipsoid altitude. PR #52 requires an explicit geoid separation and
therefore exposes a fidelity feature that the firmware profile still lacks.

### 7. Motor order, scaling, disarm, and stale commands

The current quad mapping is PX4 Quad-X channel order: front right, rear left, front left, rear
right. Non-reversible motor outputs are normalized by PX4 to `[0, 1]` and mapped to
`1100..1900` microseconds for the plant.

PR #52 also defines explicit disarm and a 100 ms stale-actuator fallback. At the uORB boundary,
PX4's `actuator_outputs_sim` message does not carry the MAVLink armed-mode bit, and a stopped
publisher produces no callback. The firmware bridge therefore cannot reproduce those two
policies from the current hook alone. A future implementation should use an additional
`vehicle_status`/`actuator_armed` hook or a guest-time watchdog, rather than infer arming from
a zero motor value.

### 8. SIH is a publisher, not the physics authority

PX4's `simulator_sih` still integrates its internal vehicle dynamics because it supplies the
sensor publication machinery used by this profile. The hook replaces the selected sensor
samples with FMI values, so SIH ground-truth topics do not describe the FastDyn plant and must
not be used for validation.

### 9. Recompilation changes addresses

Moving from v1.17.0-rc2 to v1.17.0 preserved the checked ABI and instruction patterns but moved
several hooks by `0xb0` bytes. Literal addresses are generated artifacts, never portable
bindings. Each new ELF must pass symbol resolution, vector checks, instruction checks, and
DWARF layout checks before replacing the active configuration.

### 10. Emulated-board scope

This profile is not a complete STM32H743 peripheral model. It deliberately bypasses hardware
initialization and injects data above the physical drivers. Results may support claims about
the executed PX4 software path, but not about unmodeled buses, DMA, interrupt races, electrical
faults, bootloader behavior, or the timing of a physical Pixhawk 6C.

### 11. Validity and failure semantics

`HIL_SENSOR` carries an explicit set of updated fields, and transport loss can be represented
by a stopped or delayed message stream. The firmware hook currently replaces numeric fields in
an already-published uORB sample; SIH still owns metadata such as timestamps, device IDs, and
error counts. It therefore models nominal samples but does not automatically model a failed
sensor, a driver reset, a changed device identity, or stale publication. Those cases require
explicit metadata and health hooks rather than special numeric values.

## Validation gates

For a new PX4 ELF:

1. Build the unstripped `px4_fmu-v6c_default.elf` from a pinned official tag.
2. Run `prepare_execution_configuration.py` into a temporary directory.
3. Review every generated address change and keep all ABI and instruction checks enabled.
4. Regenerate the checked-in `unlabeled_conf` only after validation passes.
5. Run the configuration-generator tests and the PX4 boot smoke test.
6. Run the closed-loop mission smoke test before making flight-behavior claims.

The maintained commands are documented in
`virtuals/physics/flight_controllers/courbet/px4/fmu_v6c/README.md`.

## Core FastDyn change record

This v1.17.0 upgrade makes no change to `core/`, `include/`, `src/fastdyn/`, the TOML schema,
or the generic runtime SDK. The existing virtual/modifier mechanism, physics API, runner, and
ELF analysis API already provide the required capability. Changes are intentionally limited
to the bundled PX4 firmware, target profile/generated bindings, target tests, notices, and
documentation.

Avoiding a core change is deliberate: PX4-specific boot policy, uORB ABI, sensor semantics,
and board bypasses belong to the Courbet firmware adapter. Moving them into core would violate
FastDyn's ownership boundary and couple the execution engine to one flight stack.
