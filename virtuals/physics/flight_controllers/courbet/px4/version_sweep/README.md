# PX4 FMUv6C Version Sweep

FastDyn maintains five PX4 FMUv6C firmware-slicing configurations, one for the latest stable
patch in each PX4 minor release from v1.13 through v1.17. The sweep was performed on
2026-11-09. Every profile runs an unmodified ARM/NuttX ELF built from the named official tag;
none of these targets is host-native SITL.

Support has two deliberately separate levels:

- **Firmware-slicing support** means the ELF passes symbol, vector, instruction, and DWARF
  validation, boots NuttX/PX4, connects all six FMU topics, reports sensors, and drives a
  motor through the plant.
- **Closed-loop flight support** additionally means the common autonomous mission completes
  takeoff, horizontal waypoints, landing, and disarm while MAVCesium receives position and
  attitude data.

All five configurations have firmware-slicing support. PX4 v1.15.4 through v1.17.0 also have
closed-loop flight support. PX4 v1.13.3 and v1.14.4 expose a repeatable in-flight estimator
limitation described below; they must not be represented as mission-qualified.

## Supported configurations

| PX4 release | Source commit | Profile | GPS adapter | Runtime result |
| --- | --- | --- | --- | --- |
| `v1.13.3` | `1c8ab2a0d7db2d14a6f320ebd8766b5ffaea28fa` | `fmu_v6c_v1_13` | scaled integer v1.13 | Boot/sensor/motor pass; mission limitation |
| `v1.14.4` | `1555f2bd2229544c43966ab5f94879c41d8e1e01` | `fmu_v6c_v1_14` | scaled integer v1.14 | Boot/sensor/motor pass; mission limitation |
| `v1.15.4` | `99c40407ffd7ac184e2d7b4b293f36f10fe561ef` | `fmu_v6c_v1_15` | floating point v1.15/v1.16 | Full mission pass |
| `v1.16.2` | `54f0455ffcd755534539a7cf33a09a20bf71d29d` | `fmu_v6c_v1_16` | floating point v1.15/v1.16 | Full mission pass |
| `v1.17.0` | `d6f12ad1c4f70ad3230afd7d86e971421e02fef4` | `fmu_v6c` | floating point v1.17 | Full mission pass |

The checked-in unstripped ELFs retain symbols and DWARF needed by the configuration generator:

| PX4 release | ELF bytes | ELF SHA-256 |
| --- | ---: | --- |
| `v1.13.3` | 40,047,140 | `13a586afee26bc1eddb2f79b358b37fc6a4f51742022a624957c6fd59e63c225` |
| `v1.14.4` | 44,206,064 | `3b750a854e49b9c43e3aac6ef6c9418c8ccbcb8f3eec61511214df191ff23651` |
| `v1.15.4` | 43,889,328 | `c573588b9e837d60c214c5275441cbe0c1c072b6d92dd193ff3c3331ccc9ab26` |
| `v1.16.2` | 45,040,204 | `7b3b228423251cb541c89ef5fe78ab54fa1301d7867cc325dc826f5763932e3c` |
| `v1.17.0` | 45,192,908 | `e6ed5d1860c61febecf520a3809741bbcfb4dcc14294338dc570f16ca07fc010` |

Run a version with its wrapper, for example:

```bash
virtuals/physics/flight_controllers/courbet/px4/fmu_v6c_v1_13/run.sh
virtuals/physics/flight_controllers/courbet/px4/fmu_v6c_v1_16/run.sh
virtuals/physics/flight_controllers/courbet/px4/fmu_v6c/run.sh
```

The wrapper validates that version's bindings, builds the shared plugin, derives a temporary
runtime TOML from `configs/px4_fmu_v6c_fire_p2.toml`, and runs the same FMUv6C machine and P2
plant. It does not duplicate the maintained machine configuration.

## Why the sweep stops at v1.13

The cutoff is a board-target boundary, not an arbitrary software cutoff. Official PX4 tags
`v1.12.3`, `v1.11.3`, `v1.10.2`, and `v1.9.2` do not contain `boards/px4/fmu-v6c`, so they
cannot build `px4_fmu-v6c_default`. FMUv6C first appears in the v1.13 release line.

Going further back is possible only as a new sweep over a different Pixhawk board target. It
would need its own firmware ELF, memory map, vector address, hardware-init slices, UART choice,
interrupt hooks, instruction signatures, and ABI validation. Re-labeling an older FMUv5 or
FMUv4 build as this FMUv6C configuration would not be valid version support.

## Version-specific challenges

### v1.13.3

- The HRT ISR reads TIM8 status into `R2` at `hrt_tim_isr+0x0a`; the modifier belongs at
  `+0x0e`. Applying the v1.15+ `R1`/`+0x10` hook would corrupt a live register.
- `sensor_gps_s` is 112 bytes. Latitude, longitude, and altitudes are scaled `int32_t` values;
  velocity starts at byte 68 and heading is at byte 92.
- PX4 still uses the monolithic `sih` module. The later `simulator_sih` plus separate
  `sensor_*_sim` boot sequence does not exist, so this release has a distinct `boot.nsh`.
- The control allocator defaults off. `SYS_CTRL_ALLOC=1` is required so HIL motor outputs use
  the same normalized contract as the later profiles.
- The old USB worker dereferences the absent emulated endpoint, and its local parameter
  autosave worker attempts unsupported flash export. The profile slices those two workers;
  console/MAVLink remain real, and parameter persistence remains explicitly out of scope.
- The source requires GNU Arm Embedded 10.2.1. It also requires CMake 3.24.3: CMake 3.28's
  host `LINUX` variable makes this old build define both Linux and NuttX paths and attempt to
  include `linux/spi/spidev.h`.
- Firmware boot, automatic boot, all six bridge topics, scaled-integer GPS, and the motor
  `1100 -> 1180 -> 1100` smoke pass. In the common mission it takes off and moves, then reports
  `Compass needs calibration - Land now!` and does not complete the mission. The retained
  profile does not suppress that estimator result.

### v1.14.4

- It shares the v1.13 HRT instruction/register shape but has a distinct 128-byte GPS ABI:
  position begins at byte 28, velocity at byte 76, and heading at byte 100.
- GNU Arm Embedded 13.2.1 fails an old NuttX inline-assembly warning promoted by `-Werror`;
  GNU Arm Embedded 10.2.1 builds the official source successfully.
- The later split simulation publishers are present, so this release shares the v1.15+
  boot sequence.
- Firmware boot, automatic boot, all six bridge topics, scaled-integer GPS, and the motor
  smoke pass. The common mission reaches its first horizontal waypoint, then reports
  `Navigation failure! Land and recalibrate sensors`, enters failsafe, lands, and disarms.
  The final mission item is not reached.
- Two plausible compatibility adjustments were tested and rejected: normalizing the old
  Quad-X arm-position parameters to the v1.15 values, and forcing the old EKF2 heading-only
  magnetometer mode. Neither completed the mission, so neither workaround is retained.

### v1.15.4

- The HRT hook changes to the v1.15+ `R1`/`+0x10` form.
- `sensor_gps_s` changes from scaled integers to double-precision degrees/metres, is 144
  bytes, and places heading at byte 116.
- An optional CMake probe reports missing Python `symforce`; it is nonfatal for the FMUv6C
  firmware target and must not be confused with a slicing failure.
- The full common mission passes: 227 position samples, 253 attitude samples, 15.16 m maximum
  relative altitude, 13.25 m maximum horizontal distance, landing, and disarm.

### v1.16.2

- It shares the v1.15 GPS and HRT adapters.
- A shallow NuttX checkout omits tags used by `px_update_git_header.py` and causes an
  `IndexError`; the pinned submodule checkout must include its tags.
- The full common mission passes: 231 position samples, 248 attitude samples, 15.13 m maximum
  relative altitude, 13.22 m maximum horizontal distance, landing, and disarm.

### v1.17.0

- `sensor_gps_s` grows to 152 bytes. Heading moves from byte 116 to byte 120 after the added
  status fields, so the v1.15/v1.16 adapter is intentionally rejected for this ELF.
- PX4's DroneCAN generator imports `pkg_resources`; the build environment needs
  `setuptools<81`.
- Static validation, boot/motor smoke, direct automatic boot, and the common mission pass.

## ABI progression

The uORB boundary is an in-process C++ ABI, not a stable wire protocol:

| Release | `sensor_gps_s` size | Position representation | Velocity offset | Heading offset |
| --- | ---: | --- | ---: | ---: |
| `v1.13.3` | 112 | scaled `int32_t` | 68 | 92 |
| `v1.14.4` | 128 | scaled `int32_t` | 76 | 100 |
| `v1.15.4` | 144 | double degrees/metres | 92 | 116 |
| `v1.16.2` | 144 | double degrees/metres | 92 | 116 |
| `v1.17.0` | 152 | double degrees/metres | 92 | 120 |

The console UART ABI, accel/gyro/mag/baro subsets, and actuator-output subset remain stable
across these five ELFs. Each GPS adapter name is nevertheless explicit in `virtuals.txt`, and
the generator validates the selected layout against that ELF's DWARF. A missing or mismatched
adapter fails generation before checked-in bindings are replaced.

## Verification commands

```bash
fastdyn-env/bin/python \
  virtuals/physics/flight_controllers/courbet/tests/test_execution_configuration.py

for version in 1.13.3 1.14.4 1.15.4 1.16.2 1.17.0; do
  fastdyn-env/bin/python \
    virtuals/physics/flight_controllers/courbet/tests/smoke_px4_fmu_v6c.py \
    --version "$version"
  fastdyn-env/bin/python \
    virtuals/physics/flight_controllers/courbet/tests/smoke_px4_fmu_v6c.py \
    --version "$version" --auto-boot
done
```

Run `smoke_px4_mission.py --version VERSION` only when evaluating the stronger flight level.
At this sweep revision it passes for `1.15.4`, `1.16.2`, and `1.17.0`; the v1.13/v1.14
failures above are expected open limitations, not passing regressions.

## Change ownership and justification

No file under `core/`, `include/`, or `src/fastdyn/`, no TOML schema, and no generic runtime
API changes for this sweep. The shared Courbet C flight-controller plugin gains explicit GPS
encoders because the actual PX4 uORB ABI changes representation and offsets across releases.
Keeping that selection in the target adapter preserves the firmware boundary and prevents a
wrong ELF/profile pairing from silently corrupting guest memory.

The common version runner and temporary-TOML generator also remain under `courbet/px4`.
Environment expansion was not added to FastDyn's generic path parser merely for this workflow.
Toolchain rules, HRT hooks, module boot policy, and generated bindings live in separate target
profiles because they are firmware-version facts, not FastDyn core behavior.
