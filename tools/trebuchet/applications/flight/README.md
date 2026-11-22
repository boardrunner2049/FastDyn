# Flight firmware qualification

The manifest pins the two original ARM ELFs, firmware lineage, input parameters and Courbet's existing KLAF waypoint mission. It reserves this mission for functional qualification. Neither release currently has a validated transformed deployment in this directory. The shipped ArduCopter and PX4 ELFs contain symbols but lack retained code relocations for FirmBOLT's emulator-performance mode. A source build with the release target, retained relocations and validated regenerated hooks is a separate image.

ArduCopter runs the FMUv3 ChibiOS image with the FastDyn Copter FMI plant. PX4 runs the FMUv6C ARM/NuttX release image with the P2 plant, real commander, EKF, navigation and control modules, and its checked-in version-specific boot script. The maintained PX4 slice replaces selected hardware initialization and SIH sensor data at the uORB boundary. Both configurations use a 1 ms interrupt quantum and icount shift 5. The documented configurations and the manifest specify the initial environment and actuator mapping.

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
