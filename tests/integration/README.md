# Integration Tests

The integration tests exercise the maintained FastDyn/Courbet/Rumoca FMI v3
vehicle path. Run them from the FastDyn repository root after setup:

```bash
source ./setup.sh --build-qemu --with-rumoca
```

## Vehicle Launch Smoke

`courbet_fmu_vehicle_smoke.sh` launches one vehicle config and waits for:

- FMU auto-build or ready detection,
- FMU backend load,
- QEMU/FMU lockstep clock,
- 1 ms board tick,
- MAVCesium URL print,
- ArduPilot heartbeat, and
- telemetry.

Examples:

```bash
FASTDYN_COURBET_CONFIG=configs/rover462.toml \
FASTDYN_COURBET_LABEL=ardurover \
tests/integration/courbet_fmu_vehicle_smoke.sh

FASTDYN_COURBET_CONFIG=configs/plane462.toml \
FASTDYN_COURBET_LABEL=arduplane \
tests/integration/courbet_fmu_vehicle_smoke.sh
```

## Full Copter Mission

`courbet_mission_smoke.sh` runs the ArduCopter KLAF/Purdue mission and requires:

- mission upload,
- vehicle arm,
- climb above the configured minimum altitude,
- mission item progression, and
- final landing confirmation.

```bash
tests/integration/courbet_mission_smoke.sh
```

## Swarm Smoke

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

The same script accepts `configs/rover462.toml` and `configs/plane462.toml`.
Each worker receives separate QEMU monitor, MAVLink, MAVCesium, Rumoca, GDB,
QMP, work, and RAM-backing paths.

## CI

`.github/workflows/ci.yml` (FastDyn CI) runs on every push to `main` and every
pull request: the README build, `pytest tests/unit`, `configs/minimal.toml`
and `run_bundled_freertos_introspection_smoke.sh`.

`.github/workflows/courbet-mission.yml` is started by hand (Actions > Courbet
Mission > Run workflow). It runs the setup script, builds the
renamed Modelica vehicle models through Rumoca FMI v3, checks OptiFuzz dry-run
wiring for copter/rover/plane, checks two-worker swarm dry-runs, launches real
two-worker swarms for all three vehicles, and runs the full ArduCopter mission.

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
