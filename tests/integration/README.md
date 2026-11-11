# Integration Tests

The integration tests exercise the maintained FastDyn/Courbet/Rumoca FMI v3
vehicle path. Run them from the FastDyn repository root after setup:

```bash
source ./setup.sh --build-qemu --with-rumoca
```

## Guest instruction fragment matrix

Build the five required QEMU targets, then run the architecture matrix:

```bash
source ./setup.sh --build-qemu \
  --qemu-targets arm-softmmu,aarch64-softmmu,x86_64-softmmu,riscv32-softmmu,riscv64-softmmu
FASTDYN_QEMU_BUILD="$PWD/third_party/common/qemu/build" \
  tests/integration/run_guest_code_fragments_smoke.sh
```

The runner assembles self-contained Cortex-A A32 and Thumb, Cortex-M Thumb,
A64, x86-16/32/64, RV32, and RV64 fixtures. Every mode executes 100 accepted
arithmetic fragment instructions and verifies that the unchanged original
instruction consumes the resulting value and fragment-produced flags or integer
predicates. It also checks Thumb IT-state rejection, RISC-V compressed-extension
gating, x86 lazy-flag exception restoration and page-boundary decode rollback,
registration order, TB invalidation/retranslation, original plugin accounting,
copied ownership, malformed input, both independent limits, the combined-site
limit, and initialization-only registry sealing.

Exercise the public SDK and native external-plugin initialization path with:

```bash
FASTDYN_QEMU_ROOT="$PWD/third_party/common/qemu" \
FASTDYN_QEMU_BIN="$PWD/third_party/common/qemu/build/qemu-system-arm" \
  tests/integration/run_fastdyn_guest_code_fragment_smoke.sh
```

That smoke also checks observer-before-fragment ordering, same-site rejection of
a program-counter modifier and a state-changing callback, unavailable-profile
rejection, and propagation of required registration failure through setup.

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
