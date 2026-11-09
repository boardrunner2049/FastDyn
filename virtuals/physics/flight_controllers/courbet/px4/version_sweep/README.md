# PX4 FMUv6C Version Sweep

This sweep tests whether the maintained PX4 v1.17.0 firmware-slicing configuration can be
reused unchanged with older PX4 flight releases. It was performed on 2026-11-09 using the
latest stable patch tag found in each PX4 minor line that contains the
`px4_fmu-v6c_default` target.

The result is intentionally conservative: only v1.17.0 passes the unchanged configuration.
Older releases are not claimed to be impossible to slice, but they need version-specific,
reviewed profiles or bridge adapters. A failed validation gate is not bypassed to obtain a
runtime result.

## Fixed slicing configuration

Every tag was tested against the same files used by the maintained target:

- `../fmu_v6c/profile.toml`;
- `../fmu_v6c/labeled_conf/virtuals.txt`;
- `../fmu_v6c/labeled_conf/modifiers.txt`.

The profile keeps the same board-initialization bypasses, uORB sensor/actuator boundary,
TIM8 HRT interrupt injection, NuttX SysTick modifier, console bridge, and ABI expectations.
Only the input ELF and temporary output directory changed:

```sh
PYTHONPATH="$PWD/src" python3 \
  virtuals/physics/flight_controllers/courbet/prepare_execution_configuration.py \
  --profile virtuals/physics/flight_controllers/courbet/px4/fmu_v6c/profile.toml \
  --elf "$PX4_ELF" \
  --output-dir "$TEMP_OUTPUT"
```

The generated output was kept outside the repository. No failing release replaced the
checked-in v1.17.0 firmware or generated bindings.

## Results

| PX4 release | Source commit | ARM build | Unchanged slicing validation | Runtime |
| --- | --- | --- | --- | --- |
| `v1.17.0` | `d6f12ad1c4f70ad3230afd7d86e971421e02fef4` | Pass, GCC 13.2.1 | Pass | Pass: boot, motor output, automatic boot, and closed-loop mission |
| `v1.16.2` | `54f0455ffcd755534539a7cf33a09a20bf71d29d` | Pass, GCC 13.2.1 | Fail: `sensor_gps_s` ABI | Not run; ABI gate failed |
| `v1.15.4` | `99c40407ffd7ac184e2d7b4b293f36f10fe561ef` | Pass, GCC 13.2.1 | Fail: `sensor_gps_s` ABI | Not run; ABI gate failed |
| `v1.14.4` | `1555f2bd2229544c43966ab5f94879c41d8e1e01` | Pass with GCC 10.2.1; GCC 13.2.1 fails | Fail: HRT instruction contract | Not run; instruction gate failed |
| `v1.13.3` | `1c8ab2a0d7db2d14a6f320ebd8766b5ffaea28fa` | Pass with CMake 3.24.3 and GCC 10.2.1 | Fail: HRT instruction contract | Not run; instruction gate failed |

All successful build products were unstripped 32-bit ARM EABI5 ELFs with DWARF information.
Their local evidence digests are:

| PX4 release | ELF bytes | ELF SHA-256 |
| --- | ---: | --- |
| `v1.17.0` | 45,192,908 | `e6ed5d1860c61febecf520a3809741bbcfb4dcc14294338dc570f16ca07fc010` |
| `v1.16.2` | 45,040,204 | `7b3b228423251cb541c89ef5fe78ab54fa1301d7867cc325dc826f5763932e3c` |
| `v1.15.4` | 43,889,328 | `c573588b9e837d60c214c5275441cbe0c1c072b6d92dd193ff3c3331ccc9ab26` |
| `v1.14.4` | 44,206,064 | `3b750a854e49b9c43e3aac6ef6c9418c8ccbcb8f3eec61511214df191ff23651` |
| `v1.13.3` | 40,047,140 | `13a586afee26bc1eddb2f79b358b37fc6a4f51742022a624957c6fd59e63c225` |

The older ELFs are evidence artifacts only and are not checked into FastDyn.

## Issues by flight release

### v1.17.0

This is the baseline and the only release that clears symbol, vector, instruction, and DWARF
ABI validation without changing the slicing configuration. Its runtime checks are documented
in `../../../../../../docs/PX4FirmwareSlicing.md` and `../fmu_v6c/README.md`.

The build environment still needs `setuptools<81`, because PX4's DroneCAN generator imports
the removed `pkg_resources` API.

### v1.16.2

The first hard failure is the bridge ABI check:

```text
('sensor_gps_s', 'size', 144, 152)
```

The injected position and velocity fields retain the v1.17 offsets, but `heading` is at byte
116 rather than byte 120. v1.17 adds `system_error` at byte 116 and later adds authentication
state, growing the structure to 152 bytes. Reusing the v1.17 bridge would therefore write the
heading into the wrong field.

A shallow NuttX submodule clone also omitted the release tags required by
`px_update_git_header.py` and failed with an `IndexError`. Fetching the NuttX tags restored
the build. A reproducible source checkout must retain those tags, not only the pinned commit.

### v1.15.4

This release has the same 144-byte GPS layout and byte-116 heading field as v1.16.2, so it
fails the same ABI gate.

CMake also reports a missing Python `symforce` module while probing optional functionality.
That probe is nonfatal for `px4_fmu-v6c_default`; the firmware build completes and reaches the
independent ABI failure. It should not be confused with the slicing rejection.

### v1.14.4

The current profile expects a TIM8 status-register read into `R1` at
`hrt_tim_isr+0x0c`, immediately before the modifier at `hrt_tim_isr+0x10`. v1.14.4 has a
different instruction and register sequence: its status read begins at `+0x0a`, uses `R2`,
and its `+0x10` instruction writes the complemented status value. The generator stops with:

```text
hrt_tim_isr+0xc changed: review the instruction hook before regenerating
```

Applying the current modifier anyway would corrupt a live register rather than inject the
TIM8 `CC3IF` bit. This requires a separately reviewed modifier and instruction signature.

The v1.14.4 NuttX/UAVCAN code also fails under GCC 13.2.1 because an inline IRQ assembly path
triggers `primask is used uninitialized`, promoted to an error by `-Werror`. The release-era
GNU Arm Embedded 10.2.1 toolchain builds it successfully.

Manual DWARF inspection reveals a second incompatibility behind the instruction gate:
`sensor_gps_s` is 128 bytes, uses scaled integer `lat`, `lon`, `alt`, and `alt_ellipsoid`
fields, and places `heading` at byte 100. The current bridge expects double-precision degrees
and metres plus a byte-120 heading field.

### v1.13.3

This release shares the incompatible v1.14 HRT instruction shape, so it fails the same
instruction-signature gate. Its GPS ABI is older again: the structure is 112 bytes, position
and altitude are scaled integers, velocity starts at byte 68, and heading is at byte 92.

There is also a host-CMake compatibility problem. With CMake 3.28.3, PX4's old
`if(LINUX)` board test sees CMake's host `LINUX` variable and defines both `__PX4_LINUX` and
`__PX4_NUTTX` for the ARM target. The GPS driver then tries to include the host-only
`linux/spi/spidev.h`, which is unavailable to the cross compiler. Configuring the unchanged
PX4 source with CMake 3.24.3 and GCC 10.2.1 produces the expected NuttX-only build.

## ABI progression

The GPS changes explain why firmware slicing cannot treat a uORB boundary as a stable wire
protocol:

| Release | `sensor_gps_s` size | Position representation | Velocity offset | Heading offset |
| --- | ---: | --- | ---: | ---: |
| `v1.13.3` | 112 | scaled `int32_t` | 68 | 92 |
| `v1.14.4` | 128 | scaled `int32_t` | 76 | 100 |
| `v1.15.4` | 144 | double degrees/metres | 92 | 116 |
| `v1.16.2` | 144 | double degrees/metres | 92 | 116 |
| `v1.17.0` | 152 | double degrees/metres | 92 | 120 |

The UART ABI used by the console bridge remains 156 bytes with `open_count` at byte 0 and
`isconsole` at byte 4 across v1.13.3-v1.16.2. The accelerometer ABI remains 48 bytes with
`x` at byte 20, and `actuator_outputs_s` remains 80 bytes. Those stable subsets do not make
the complete adapter safe when either the GPS ABI or interrupt contract fails.

## Implications for future support

Supporting these releases while preserving the SITL-equivalent boundary requires explicit
version adapters:

1. Split GPS encoding between the v1.13-v1.14 scaled-integer layout, the v1.15-v1.16
   double-precision layout, and the v1.17 layout.
2. Define and independently validate an HRT modifier for the v1.13-v1.14 instruction shape.
3. Pin a compatible build toolchain per release, including CMake for v1.13 and GNU Arm GCC
   for v1.13-v1.14.
4. Run the full boot, motor, automatic-boot, and mission suite for each adapter after all
   static gates pass.

Creating a permissive ABI check or weakening the instruction signature would hide exactly the
version drift that this sweep is intended to expose.

## Core FastDyn change record

This sweep makes no change to FastDyn core, the configuration schema, runtime SDK, or the
maintained slicing configuration. It adds this evidence record only. That is deliberate:
toolchain compatibility, PX4 uORB layouts, and version-specific HRT hooks belong to explicit
PX4 target profiles. Adding silent compatibility behavior to core would make an unsafe older
ELF appear supported without a separately validated adapter.
