# Integration Tests

The integration tests exercise the maintained FastDyn/Courbet/Rumoca FMI v3
vehicle path. Run them from the FastDyn repository root after setup:

```bash
source ./setup.sh --build-qemu
```

## Vehicle Missions

`courbet_mission_test.sh` checks mission upload, arming, altitude and waypoint
progress. Copter requires the final mission item, low altitude, and a firmware
`ON_GROUND` report. Rover drives a rectangle and requires a
`MISSION_ITEM_REACHED` message for its final waypoint.

```bash
tests/integration/courbet_mission_test.sh

FASTDYN_COURBET_CONFIG=configs/rover462.toml \
FASTDYN_COURBET_LOG=out/ci/ardurover_mission.log \
FASTDYN_COURBET_MIN_ALT_M=0 FASTDYN_COURBET_MIN_ITEM=4 \
tests/integration/courbet_mission_test.sh

```

Build the two supported FMUs, then check stationary startup, disabled PWM, and
actuator response without firmware:

```bash
python utils/build_fmi3_fmu.py --config configs/copter462.toml
python utils/build_fmi3_fmu.py --config configs/rover462.toml
python tests/integration/fmu_backend_test.py --vehicle copter --vehicle rover
python tests/integration/plane_export_limit_test.py
```

The Plane check lowers the actual three-wheel template and confirms the pinned
compiler rejects its unsupported contact events. It does not validate flight.

## Swarm launch checks

`courbet_swarm_smoke.sh` launches multiple isolated FastDyn workers and verifies
each worker prints a MAVCesium URL, loads the FMU backend, and uses the 1 ms
board tick.

```bash
FASTDYN_SWARM_CONFIG=configs/copter462.toml \
FASTDYN_SWARM_LABEL=arducopter \
FASTDYN_SWARM_INSTANCES=2 \
FASTDYN_SWARM_BASE_PORT=18000 \
tests/integration/courbet_swarm_smoke.sh
```

The same script accepts `configs/rover462.toml`. Use the source configs with
port placeholders for swarms; tutorial-generated single-vehicle configs contain
fixed helper ports and must not be shared between workers.
Each worker receives separate QEMU monitor, MAVLink, MAVCesium, Rumoca, GDB,
QMP, work, and RAM-backing paths.

## CI

`.github/workflows/ci.yml` runs the setup script, builds the
renamed Modelica vehicle models through Rumoca FMI v3, checks OptiFuzz dry-run
wiring for copter/rover/plane, checks two-worker swarm dry-runs, launches real
two-worker Copter/Rover swarms and complete missions. Plane export support is
checked separately and explicitly reported as pending.
The `mission-report` artifact includes console and MAVLink logs, a top-down
track with numbered waypoints, and altitude versus setpoint over time. See
[the repository README](../../README.md#ci) for the local report command.

## Upstream RTOS introspection smoke test

For a fast local configuration test with no clone, build, or RTOS submodule,
run the committed DWARF-enabled FreeRTOS fixture:

```bash
tests/integration/run_bundled_freertos_introspection_smoke.sh
```

It writes a temporary TOML configuration with an enabled
`[CPU.cpu0.plugins.introspection]` table, launches
the bundled `RTOSDemo.axf`, and verifies detection, generated hook virtuals,
schema generation, and live activity records. It needs only patched QEMU,
`build/libfastdyn.so`, and the FastDyn virtual environment.

The scripts below shallow-clone upstream RTOS sources into a temporary
directory, build an official demo with DWARF symbols, and run the complete
FastDyn/QEMU introspection path. They do not add RTOS submodules. They need
the patched QEMU build, `build/libfastdyn.so`, the FastDyn virtual environment,
and `arm-none-eabi-gcc`:

```bash
tests/integration/run_freertos_introspection_smoke.sh
tests/integration/run_threadx_introspection_smoke.sh
tests/integration/run_rtthread_introspection_smoke.sh
tests/integration/run_chibios_introspection_smoke.sh
tests/integration/run_nuttx_introspection_smoke.sh
```

The NuttX script also bootstraps its upstream build-only utilities
(`kconfig-frontends` and `genromfs`) in the temporary directory, and creates
a temporary Python environment for NuttX's ELF post-processing dependencies.

## STM32F429I-DISC1 FreeRTOS LED smoke test

`run_freertos_stm32f429i_led_smoke.sh` builds the dedicated Cortex-M4F fixture
against the real FreeRTOS Kernel, launches it through FastDyn, and reads two
task-owned counters through QMP. Both must advance: they represent the green
and red user LEDs on PG13--PG14 at 250/500 ms.

```bash
tests/integration/run_freertos_stm32f429i_led_smoke.sh
```

The fixture and its matching config are documented in
[`tests/firmwares/freertos_stm32f429i_discovery/`](../firmwares/freertos_stm32f429i_discovery/).
