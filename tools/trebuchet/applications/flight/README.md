# Flight firmware qualification

The manifest pins the two original ARM ELFs, firmware lineage, input parameters and Courbet's existing KLAF waypoint mission. It reserves this mission for functional qualification. Neither shipped image currently has a validated transformed deployment in this directory. The shipped ArduCopter and PX4 ELFs contain symbols but lack retained code relocations for FirmBOLT's emulator-performance mode. A source build with the release target, retained relocations and validated regenerated hooks is a separate image.

ArduCopter runs the FMUv3 ChibiOS release-tag image with the FastDyn Copter FMI plant. PX4 runs the FMUv6C ARM/NuttX release image with the P2 plant, real commander, EKF, navigation and control modules, and its checked-in version-specific boot script. The maintained PX4 slice replaces selected hardware initialization and SIH sensor data at the uORB boundary. Both configurations use a 1 ms interrupt quantum and icount shift 5. The documented configurations and the manifest specify the initial environment and actuator mapping.

The supplied ArduCopter image reports GCC 13.2.1 and effective `-Og` in DWARF. The official CubeBlack 4.6.2 release ELF uses GCC 10.2.1 and differs in entry address and loaded bytes. Its identity and segment comparison are recorded in [the release comparison](evidence/ardupilot-official-release.json). The supplied PX4 image reports GCC 13.2.1 and size-optimized compilation units. Flight qualification on these two original images establishes the simulation configurations tested here.

## Reproduce a diagnostic baseline

Run from the FastDyn root in an environment with the patched ARM QEMU, FastDyn Python environment, the Arm GNU toolchain, Python-enabled gdb-multiarch, the native build dependencies, and Rust from the pinned Rumoca toolchain. Use distinct output directories for each invocation.

```sh
git submodule update --init --depth 1 third_party/common/qemu third_party/common/rumoca \
    third_party/common/modelica_models third_party/courbet_deps/fire_modelica_models \
    third_party/courbet_deps/mavlink_headers
python3 utils/setup_fire_toolchain.py
meson setup /tmp/trebuchet-flight-plugin -Ddevice_models=true -Denable_phy=true \
    -Denable_fmu=true -Denable_flight_controllers=true
ninja -C /tmp/trebuchet-flight-plugin
F=tools/trebuchet/applications/flight
fastdyn-env/bin/python "$F/baseline.py" inspect ardupilot /tmp/inspect-copter
fastdyn-env/bin/python "$F/baseline.py" inspect px4 /tmp/inspect-px4
fastdyn-env/bin/python utils/build_fmi3_fmu.py --skip-submodules --config configs/copter462.toml
fastdyn-env/bin/python utils/build_fmi3_fmu.py --skip-submodules --config configs/px4_fmu_v6c_fire_p2.toml
fastdyn-env/bin/python "$F/baseline.py" run ardupilot /tmp/mission-copter --plugin /tmp/trebuchet-flight-plugin/libfastdyn.so
fastdyn-env/bin/python "$F/baseline.py" run px4 /tmp/mission-px4 --plugin /tmp/trebuchet-flight-plugin/libfastdyn.so
```

The static check validates the pinned ELF and regenerates its hooks in the output directory using Courbet's validator. The run command starts the original configuration and records the actual plugin, QEMU, FMU, generated hooks and effective configuration. Run one at a time or use separate container network namespaces for helper and monitor ports. A failed or interrupted attempt keeps its receipt and available raw files.

## Frozen functional oracle

Both closed loops use the recorded home, 15 m takeoff, three waypoints and landing at home. The check requires both the firmware MAVLink positions and the FMI plant trajectory to reach the airborne waypoints in order within 5 m horizontally and vertically, then land within 5 m horizontally and 1 m vertically of home. The route legs are about 15–20 m long. The 5 m horizontal allowance stays below half the separation between successive waypoints, and 5 m vertically preserves ascent to at least 10 m. The landing bound matches the ArduCopter helper. The first plant altitude supplies the ground datum. The maintained PX4 plant derives its FMI GPS from the plant's NED position. ArduCopter's shipped model has zero position bias.

The telemetry oracle checks firmware source identity, normal mission-helper event ordering, estimator readiness, command progression, arming, finite attitude samples, and PX4's post-flight disarm. It also requires a motor-input excursion of at least 25 microseconds on each channel, consistent with the backend's detectable PWM change. The FMI trace samples existing sensor reads before either firmware adapter sees them, at most 10 Hz, preserving model step and call ordering. A telemetry-only success or a boot-only run leaves the operational gate open.

For a future timed comparison the primary objective is host CPU seconds used by QEMU for equal simulated mission work, smaller is better. Record process and complete operational costs separately. These traced qualification runs are diagnostics. Fresh warm-cache proof, transformed hook identities, role-disjoint scenarios and comparison against the frozen incumbent are still required for performance admission.

## Hosted ordinary-warm qualification

On Ubuntu 24.04 x86-64, install the system dependencies below, then run the script from the checkout. The runner needs rustup and permission for the process-local `setarch x86_64 -R` operation used by this QEMU build.

```sh
sudo apt-get update
sudo apt-get install -y --no-install-recommends build-essential pkg-config cmake ninja-build meson \
    git python3-venv python3-dev gdb-multiarch binutils-arm-none-eabi lsof libglib2.0-dev libcjson-dev libpixman-1-dev \
    libfdt-dev zlib1g-dev libusb-1.0-0-dev libstlink-dev flex bison libexpat1-dev \
    libssl-dev libudev-dev libclang-dev protobuf-compiler libprotobuf-dev
tools/trebuchet/applications/flight/qualify.sh px4 "$RUNNER_TEMP/flight-px4"
```

Use `ardupilot` for the ArduCopter job. The adapter opens the exact packaged FMU once in each new mission directory and keeps its native library path stable across runs. After the cacheless functional mission passes, it runs one ordinary-cache population mission and up to two replays with the same plant and input. Each requires the same functional oracle. scopt's deployed-state proof checks inode, record prefix, manifest fingerprint and header identity. Only a verified replay with zero appended blocks qualifies as fully warm. Receipt values record how many translations each replay appended. An observed failure ends that attempt. These fixed qualification scenarios and diagnostic plant traces carry no final-performance weight.

## Observed local diagnostics

| Release | FMI samples | Telemetry positions / attitudes | Outcome |
| --- | ---: | ---: | --- |
| ArduCopter 4.6.2 | 1,384 | 534 / 534 | Passed both trajectories and the near-ground finish. The helper stopped before disarm. |
| PX4 v1.17.0 | 562 | 238 / 253 | Passed both trajectories, landing and disarm. |

The first ArduCopter run completed its mission but needed manual QEMU shutdown because the environment lacked `lsof`. That run was excluded. The subsequent clean run finished normally. Its buffered MAVProxy log ended at 1.256 m, beyond the declared 1 m bound, although the helper received a final 1.000 m observation and the independent FMI trace ended 0.766 m above its initial ground height. Its negative receipt and raw files are preserved as `ardupilot-buffered-log`. Configuring MAVProxy to flush each packet retained the terminal observation on the following run. The same frozen gate then passed.

Each evidence archive contains the console, plant, wire telemetry and timing-event streams, launch TOML and receipt. Recheck a positive archive or reproduce the stored negative verdict with the same command:

```sh
F=tools/trebuchet/applications/flight
D=$(mktemp -d)
tar -xzf "$F/evidence/px4-cold.tar.gz" -C "$D"
fastdyn-env/bin/python "$F/oracle.py" "$D"
```

The simulated duration includes the plant sampling window and varies with helper scheduling. It is descriptive provenance. No paired speedup or equal-duration CPU result follows from these runs.

## Frozen inventory and late translations

In the first hosted qualification, the independently rechecked PX4 cold, population and both replay missions passed their operational oracles. The 47,399-record inventory grew by 7,232 records on replay one and by another 4,665 on replay two. Both scopt proofs passed. The job retained record-prefix and manifest digests but omitted the complete original cache archive. ArduCopter passed the hosted cold oracle and failed the subsequent population mission's landing check. Neither result establishes fully warm performance. The artifact identities and per-phase revalidation are in `evidence/hosted-44979548250.json`.

`--frozen-preparation` seals an independent sparse copy of the original capture and its FastDyn manifest **before** observing replay. The runnable copy must match the entire frozen file and manifest digests. The first replay feeds the same documented mission, so its late translations describe run-to-run variability on known inputs. The receipt records a successful application oracle, existing scopt deployment proof, exact before/after record counts, and the resulting file digest. The immutable copy stays in `frozen/`. Later cumulative replays cannot populate that snapshot. A new workload distribution, changed scenario, or unseen-input claim requires a distinct preregistered evaluation.

The hosted script also checks the independent fully warm gate. The default budget is two checked replays after population. Set `FLIGHT_WARM_ATTEMPTS` before the job to declare another bounded budget of 1–16 replays. A frozen preparation can pass when every fully warm attempt fails. The receipt retains both states.

A later predeclared eight-replay qualification archived both complete frozen inventories. Both shipped-image families passed the cold mission, population and all eight replay oracles, with independent scopt deployment proofs. ArduCopter started from 32,465 records, appended 3,573 on the first replay and still appended 692 on the eighth. PX4 started from 47,128 records, appended 7,409 on the first and still appended 1,395 on the eighth. Both first replays qualify against their sealed populations. Both failed the strict zero-append gate. [Hosted receipts and artifact digests](evidence/hosted-qualification.json)

## Release firmware with retained relocations

Run `release/build.sh px4 /new/build-directory` or `release/build.sh ardupilot /new/build-directory` from the checkout on Linux with the board build dependencies, Python 3.11+, GDB and GNU Arm tools. The build checks out the exact upstream revision and submodules. A compiler wrapper runs the actual release link twice on identical inputs and restores its original output before upstream packaging. The second link adds GNU ld's `--emit-relocs`. For CubeBlack the script downloads and checks the upstream GCC 10-2020-q4-major package. For PX4 the build used Ubuntu's `gcc-arm-none-eabi` 15:13.2.rel1-2. Each output directory records tool versions, the link invocation, submodule revisions, Python packages and SHA-256 values.

`release/compare.py` checks the entry point, ELF flags and each load segment's addresses, sizes, alignment, flags and payload digest. Its symbol comparison requires every original named function and object, including binding, address, size and multiplicity. Extra local symbols may alias an existing extent. The unmodified Courbet generator checks each ELF, and both rebuilt siblings must agree on modifiers, virtuals and every original symbol binding.

The PX4 release target produced a pair with 73,993 code relocations. All original symbols remained at their original addresses. The linker emitted 502 additional local function aliases. The regenerated modifier and virtual files were byte-identical across the pair. A separate cacheless FastDyn mission on the relocation-bearing sibling passed both independent tracks and disarmed after landing. This input has a different identity from the previously qualified shipped PX4 ELF. [ELF and hook comparison](evidence/px4-release-comparison.json) · [Mission receipt](evidence/px4-release-cold.json) · [Raw mission observations, link inputs and hosted comparison](evidence/px4-release-cold.tar.gz)

To reproduce an operational check on the pair, use `baseline.py run px4 /new/mission-directory --release-pair /build-directory/artifacts --plugin /path/to/libfastdyn.so` in the prepared FastDyn environment. The adapter checks both ELF identities and load equivalence before regenerating hooks for the exact image. A separate run can collect a diagnostic TB profile by adding `--training-profiler /path/to/libfirmbolt_tbprof.so`. Use fresh output directories for each run. Instrumented qualification on this mission carries no held-out performance weight.

During the local plant build, the pinned Rumoca Cargo build script regenerated two parser sources. The generated files matched Cargo's staging outputs byte for byte and were restored after compiling the compiler. The operational receipts record the resulting plant and native library SHA-256 values.

The CubeBlack release build reproduced the published 4.6.2 firmware's executable and application data bytes using the official compiler. Both comparisons (official to rebuilt, rebuilt to retained) have the same placement and six changed flash-mapped bytes in ELF header fields `e_shoff`, `e_shnum` and `e_shstrndx`. The retained sibling has 63,063 code relocations and five additional local aliases. The strict full-load comparison and the original SHA-pinned absolute-address hook validator both reject admission of this pair. [ELF and hook comparison](evidence/ardupilot-release-comparison.json) · [All differing loader-visible bytes](evidence/ardupilot-release-load-delta.json) · [Build provenance](evidence/ardupilot-release-provenance.tar.gz)

## PX4 guest optimization diagnostics

The release-pair training mission ran on the original FMI configuration, with a TB profiler that flushes FirmBOLT's existing counters at QEMU exit. Both independent tracks passed, including landing and disarm. The resulting profile converted to 77,268 pre-aggregated records. Profile collection used the same qualification mission and provides no held-out timing result.

The FMUv6C linker allocates application flash at `0x08020000..0x08200000`. The first 1 KiB-aligned address after the original physical flash payload is `0x081f6800`. Every one of the 88 `R_ARM_TARGET1` relocations writes a Thumb function's absolute symbol value to an entry in the constructor table. The image-wide recipe with this relocation contract stopped at a failed CBZ/CBNZ legalization in `Commander::run`. A second recipe selected `memset` from the real training profile. The debug-preserving output passed the original hook generator. All sixteen core exception vectors, 28 hook-site instruction prefixes, modifiers and virtuals matched. Its 154-byte new code section fit, but the emitted executable load segment also contained six bytes of padding and an extra 1,921,856-byte copy of the original text. Loading that artifact would exceed the board's flash limit. [Initial guest qualification](evidence/px4-release-guest.json) · [Original profile, oracle, validated relocations and optimizer logs](evidence/px4-release-guest-profile.tar.gz)

The private optimizer stays in its separately installed checkout. For a reproduction, build the public mission-exit wrapper against that checkout's profiler and the headers of the QEMU binary used by FastDyn. From the FastDyn root, with absolute paths in `FIRMBOLT_REPO`, `PAIR`, `PLUGIN` and the new `OUT` directory:

```bash
mkdir "$OUT"
flight=tools/trebuchet/applications/flight
profiler="$FIRMBOLT_REPO/tools/emulator-profile"
cc -shared -fPIC -O2 "-DFIRMBOLT_TBPROF_SOURCE=\"$profiler/firmbolt_tbprof.c\"" \
  -Ithird_party/common/qemu/include/qemu -Ithird_party/common/qemu/include \
  $(pkg-config --cflags glib-2.0) "$flight/release/training-profiler.c" -o "$OUT/profile.so"
fastdyn-env/bin/python "$flight/baseline.py" run px4 "$OUT/train" --release-pair "$PAIR" \
  --plugin "$PLUGIN" --training-profiler "$OUT/profile.so"
"$FIRMBOLT_REPO/.venv/bin/python" "$profiler/tbprof2bolt.py" "$OUT/train/session.tbprof" "$OUT/train.preagg"
fastdyn-env/bin/python "$flight/release/px4_target1.py" "$PAIR/relocs.elf" > "$OUT/target1.json" &&
  "$FIRMBOLT_REPO/.venv/bin/python" -m firmbolt --cpu cortex-m7 --emulator-profile "$OUT/train.preagg" \
    --new-code-address 0x081f6800 --report "$OUT/result.json" "$PAIR/relocs.elf" -o "$OUT/firmware.elf" \
    -- --firmbolt-arm-target1=abs --funcs-no-regex=memset --update-debug-sections
fastdyn-env/bin/python "$flight/baseline.py" run px4 "$OUT/candidate" --release-pair "$PAIR" \
  --candidate-elf "$OUT/firmware.elf" --candidate-layout "$OUT/result.json.layout.json" --plugin "$PLUGIN"
fastdyn-env/bin/python "$flight/baseline.py" run px4 "$OUT/candidate-trace" --release-pair "$PAIR" \
  --candidate-elf "$OUT/firmware.elf" --candidate-layout "$OUT/result.json.layout.json" \
  --plugin "$PLUGIN" --training-profiler "$OUT/profile.so"
```

FirmBOLT commit `3b3f1165d1433f981a5be01ff920248273528ce0` corrected the extra segment extent. The new executable load uses 156 bytes. Every original loaded byte outside the verified Thumb entry redirect stayed identical. The adapter checked the final layout's linked instruction bytes and the branch target. The uninstrumented mission completed both tracks with 577 independent plant observations, 245 telemetry positions, 268 attitudes and disarm. In a separate successful mission, the training plugin counted 24,538,539 executions across 51 distinct TB starts in the moved code. Both operational runs used the exact same candidate SHA-256, `1bcb49a3337553e58f37d1d84fe4d3596a6ddcec822edb112832492c971a1255`. [Cold transformed mission](evidence/px4-transformed-cold.json) · [Executed transformed PCs](evidence/px4-transformed-execution.json) · [Raw observations](evidence/px4-transformed-raw.tar.gz)
