# Physics Co-Simulation in FastDyn

An overview of the `world_model` integration and co-simulation slave mode:
what was found in the existing code, what was decided and why, what was built,
and what is still open. Detail lives in
[WorldIntegration.md](WorldIntegration.md), [WorldPlugin.md](WorldPlugin.md),
and [CoSimulationSlave.md](CoSimulationSlave.md).

## Slide: The question

**Can a device model author describe a peripheral's physical behavior in
Modelica instead of writing C?**

`tools/world` is a standalone FMI 3 Co-Simulation runtime with no dependency
on firmware, emulators, or device abstractions. It is under development and
not released yet: the submodule needs PSecLab access and is fetched only with
`git submodule update --init --checkout tools/world`. Its whole public contract is
absolute virtual time in nanoseconds plus typed accessors:

```c
wm_init();
set_fire_intensity(5.0);
wm_advance_to(1000000);
get_temperature(&temperature);
```

The work below answers what that costs to wire into FastDyn, what it buys,
and where it does not reach.

## Slide: What world supplies, and what it does not

`world` supplies a peripheral's **transfer function**, not the peripheral.

The proportion is visible in an existing hand-written model. Of roughly 141
lines in
BoardRunner's `examples/stm32f429i_disc1/hybrid/ADC_with_DMA/generated_models/adc_model.c`,
the physical content is a single assignment (`s->dr = 0x0AAA;`). The rest is
register offsets, bitfield semantics, conversion timing, the DMA request, and
interrupt gating.

`world` replaces that one line. Bridging the remaining 140 is the integration
problem.

## Slide: Finding — FastDyn already had the timing model

The first proposal was a tick-based, guest-time-driven advance policy. It was
already implemented.

`kick_irq` (`virtuals/virtuals.c:134`) advances physics to
`qemu_plugin_get_virtual_timer()` on each periodic IRQ, before the guest
handles it — "lockstep with the firmware timer tick", in the source comment.
`fmu_advance_simulation` (`virtuals/physics/physics_engines/fmu/fmu.c:672`)
already caps its internal step and stages inputs.

**The timing half of the problem was solved; the proposal was describing
shipping code.** Two related corrections followed: the physics subtree holds
about 5,600 tracked lines of C, not the ~97k visible on disk (the rest is
untracked build output), and the existing FMU backend is already FMI 3 with
its own private ABI, not a legacy FMI 2 path.

## Slide: Finding — six uncoordinated time mechanisms

Nothing owns time. `kick_irq` even special-cases `phy_backend_is("fmu")`
inline.

| Mechanism | Driven by |
| --- | --- |
| `wm_world_advance_to` quantum loop | caller |
| `fmu_advance_simulation` substeps | caller |
| `kick_irq` tick | guest time, periodic IRQ |
| `catch_up` thread (`ardupilot.c:35`) | **host** time, `usleep(1000)` |
| `catch_up` timer (gazebo altimeter) | QEMU timer |
| per-model timers (e.g. ADC conversion) | QEMU timer |

The fourth is a live determinism bug: it advances physics from a host-time
polling thread, which breaks twintrace replay, fuzzing reproducibility, and
swarm results. The FMU backend has already moved off it; ArduPilot and Gazebo
have not.

## Slide: Finding — the FMU backend is not generic

`fmu.c` never opens `modelDescription.xml`. Bindings are hardcoded FMI value
references:

```c
.vr = { .pwm = 61, .gps = 72, .gyro = 76, .mag = 77, .accel = 78, ... }
```

Value references are positional. Adding or reordering a Modelica variable
shifts every index after it, and nothing validates them — `get_values()` reads
VR 78 and calls it acceleration. The result is plausible-looking wrong physics
with no error, which for a published result is a silent-corruption path.

The override mechanism moves the magic numbers rather than removing them: the
table is a fixed 15 entries, so a new sensor has nowhere to go. Types are
assumed Float64, 3-vectors are assumed contiguous, and the packaging path
expects a pre-extracted directory in the FMI 2 `binaries/linux64` layout while
calling an FMI 3 ABI.

**This is the real argument for `world`:** not FMI 3, and not a better physics
loop — FastDyn has both — but discovery and validation. A generated init
wrapper that checks name, direction, and type is the guard that is missing.

## Slide: Finding — two integration surfaces, not one

FastDyn has two independent paths by which firmware obtains data, and the
existing FMU physics uses the second.

| | Path 2: function hook | Path 1: device handler |
| --- | --- | --- |
| Mechanism | virtual at a firmware function's PC | `DeviceModel` read/write over an address range |
| Physics precedent | flies ArduCopter today | none |
| Firmware coupling | needs symbols and PCs | firmware-agnostic |
| Reuse unit | one firmware build | one chip |
| Fidelity | elides the peripheral | exercises the real driver path |

Nothing under `virtuals/` calls `dev_register_device_model`. The ArduCopter
setup is 26 virtuals and 95 modifiers over raw addresses, and
`ardupilot.c` manufactures InvenSense and HMC5843 wire bytes by hand
(`convert_to_invensense()`, `pack_to_hmc5843_wire_format()`), injecting them at
the driver boundary and bypassing SPI and I2C entirely.

This matters for BoardRunner specifically: path 2 deliberately elides the
peripheral, which is the object of study when the goal is learning and
verifying peripheral models.

## Slide: Decision — advance on access, no scheduler

A deadline-queue scheduler was designed and then dropped. The escalation
ladder is shorter than it looks:

1. **Today** — one hardcoded participant in `kick_irq`. Works.
2. **A registration list, ~30 lines** — replaces the `phy_backend_is("fmu")`
   branch; lets participants be added without editing core files.
3. **A deadline queue** — only buys *different rates per participant*.

Level 3 is speculative until a participant's rate genuinely differs. What was
adopted instead is **advance-on-access**: each callback brings the world up to
the current guest time before acting.

```c
uint64_t now = virtual_guest_time_ns(runtime);
if (now > world_time_ns) { wm_world_advance_to(world, now); world_time_ns = now; }
```

Physics is fresh at the instant of the access, never runs ahead of the guest,
and repeated accesses in one guest instant are free (`wm_world_advance_to`
iterates `while (time_ns < target)`).

**This is sufficient only because interrupts do not come from physics.**
Firmware sleeping on a physics-generated interrupt would never wake, since no
access would advance the world; that configuration needs a periodic advance as
a floor.

## Slide: Built — the world plugin

```text
RLC.mo → RLC.fmu → [CPU.cpu0.plugins.world] → world.manifest → world_model
                                                    ↓
                          world_digital_out / world_analog_in at firmware PCs
                                                    ↓
                                            guest registers
```

One TOML describes the machine and the physical world attached to it. The host
preprocessor owns every world-specific concern — validating models, endpoints
and pins, resolving FMU paths, resolving symbolic triggers from the ELF,
checking each pin against its endpoint's FMI causality — and emits ordinary
virtual rules that flow through FastDyn's normal pipeline.

The runtime uses world's **generic** C API rather than its generated bindings,
so there is no C to compile at configuration time. Endpoints resolve once to
handles after initialization; callbacks never do a name lookup.

## Slide: Demonstration — a GPIO pin drives an RLC circuit

```bash
fastdyn run -c configs/world_rlc_gpio.toml -o fastdyn_work_world
```

```text
  sample  pin      capacitor
     0  1      1106 mV      ← charging toward 3.3 V
     3  1      2866 mV
     7  1      3250 mV
     8  0      2165 mV      ← pin low, discharging
    15  0        48 mV
```

Firmware toggles a pin, the pin drives the circuit's supply, and the firmware
reads the capacitor back as millivolts. The trace CSV holds 1574 rows at 100 µs
— far finer than the firmware's own sampling, which is the point: it records
what the physics did between observations.

The pins are naked stubs (`bx lr`), so the virtual's register write survives to
the caller and nothing between the hook and the return can disturb it.

## Slide: The plugin adds one line to FastDyn

```diff
  subdir('variable_watch')
+ subdir('world')
```

The plugin is self-enabling: `virtuals/world/meson.build` checks for a built
`tools/world` and compiles itself in, or prints `skipping` and is omitted.
There is no build option and no top-level flag; `Makefile`, `meson_options.txt`
and `src/fastdyn/` are untouched. `fastdyn help virtuals` lists it from its
`ConfigurationHelp` with no frontend change.

One consequence worth knowing: compiling it in creates a hard `DT_NEEDED` on
`libworld_model.so` with an absolute `RUNPATH`. If that library later moves,
*every* FastDyn run fails to load the plugin, not only world-backed ones.

## Slide: `step_ns` is a fidelity parameter, not a scheduling one

The bundled RLC FMU's `fmi3DoStep` is a single RK4 step of size `dt`, with no
internal substepping. So the configured communication step *is* the
integration step:

| `step_ns` | samples 0, 3, 7 (mV) |
| --- | --- |
| 10 µs – 1 ms | 1106 2866 3250 (converged) |
| 3 ms | **1206** 2867 3250 |
| 5 ms | 4288 957487 **1915535670** (diverged) |

The 5 ms explosion is obvious. The 3 ms row is the dangerous one: a 9% error
that reads as a plausible measurement.

A properly exported CS FMU substeps internally with its own error control, so
the communication step only resolves *coupling* between models. The correct
mental model is that `step_ns` is an **upper bound** on the integration step,
and whether that bound binds is a property of the FMU, not of FastDyn.

## Slide: Built — co-simulation slave mode

The inverse arrangement: an external process owns the clock, and FastDyn
executes only what it is granted.

The patched QEMU already carried a virtual-time budget over QMP; nothing in
FastDyn used it. The protocol is asynchronous:

```text
run-for(dt)   →  RESUME, immediate reply, guest runs
              →  STOP when the deadline is reached
query-budget  →  the virtual time actually reached
```

```bash
# Terminal 1
fastdyn run -c configs/cosim_slave.toml -o fastdyn_work_cosim
# Terminal 2
utils/fastdyn_cosim.py /tmp/fastdyn-cosim.qmp --slice-ms 20 --slices 6
```

```text
 slice   granted(ns)   advanced(ns)   reached(ns)   status
     0      20000000       20000000      20000000   paused
     2      20000000       20033568      60033568   paused
     4      20000000       19942720     100000000   paused
```

Entry is via `stop_on_start = true`. The `start_budgeting` virtual can enter
budget mode at a chosen PC instead, but is not recommended: the free-running
prefix advances guest time outside the co-simulation, the use case is covered
by granting one large slice, and it is the only path that drives the VM from a
vCPU thread.

## Slide: Budget fixes

All in `patches/qemu-fastdyn-plugin-icount.patch`, which already carried
budget changes — the QEMU repository itself is untouched.

| Problem | Fix |
| --- | --- |
| `printf` on every budget event, swamping a master that steps in small slices | `budget_debug()`, silent unless `FASTDYN_BUDGET_DEBUG=1` |
| `if (!budget_init) init()` racing between the QMP main-loop thread and a vCPU thread | `budget_system_ensure_init()` via `g_once_init_enter/leave` |
| `qmp_run_for()` read `total_budget` unlocked after the callback mutates it | re-takes the lock |
| No way to learn where the guest actually stopped | `currenttime` field, new `query-budget` command |

## Slide: Exact-stop mode

By default the guest halts at the first translation-block boundary at or after
its deadline, overshooting by a fixed cost of roughly one execution round
(~150 µs, about 4700 instructions at `shift = 5`). Because the cost is fixed
rather than proportional, it sets a floor on the usable quantum: 0.7% of a
20 ms slice, but 28% of a 500 µs one.

The deadline *detection* was already exact — `totalbudget` tracks the sum of
grants with no drift. The imprecision was entirely in delivering the stop:
`qemu_system_vmstop_request()` raises a flag, and under icount the vCPU keeps
retiring instructions until the main loop acts on it.

Exact mode calls `vm_stop()` from the deadline timer instead. That timer runs
on the main loop with icount having already stopped the vCPU precisely at the
deadline, so pausing there leaves virtual time exactly on the budget.

| 40 × 500 µs slices | default | exact |
| --- | --- | --- |
| median overshoot | 109 µs | **0** |
| max overshoot | 156 µs | **0** |
| landing exactly on the deadline | 4/40 | **40/40** |

Opt in with `[Machine] exact_budget_stop = true`, needing no change to the
master, or at runtime with the `set-budget-mode` QMP command. Off by default.
No measurable wall-clock cost, and guest output is byte-identical either way.

## Slide: Lockstep is where exact stops pay off

`utils/cosim_lockstep.py` grants every guest the same slice, waits for all of
them, and only then proceeds — one logical clock across N FastDyn instances.
The loop boundary is where a shared model, message router or radio channel
would exchange data.

```text
 round         guest 0         guest 1  spread(ns)
     0        20000000        20000000           0
     3        80000000        80000000           0
     5       120000000       120000000           0
final virtual times agree exactly
```

With `exact_budget_stop = false` the same run drifts, because each guest
overshoots independently:

```text
     2        60075968        60094208       18240
     5       120048224       120098464       50240
final virtual times differ
```

A conservative master can absorb that with a guard band, but the guests cannot
exchange data at a common instant. This is the concrete argument for exact
stops, and the reason slave mode is the more promising direction for coupled
swarm work than a richer single node.

## Slide: What determinism does and does not hold

Two measured properties, and the distinction matters.

**Guest execution is deterministic.** The same slice sequence run twice
produces byte-identical guest output.

**Observation instants are not, by default.** Individual stop points differ by
roughly ±150 µs between runs, because the pause is requested from a
virtual-clock timer and serviced by the main loop. Exact-stop mode removes
this: the same grant sequence produces identical stop points.

This is safe: a paused guest computes nothing, so where it pauses cannot
change what it computes. What varies is when the master is allowed to look.
Adequate for millisecond-scale loose coupling; for tight coupling needing
exact repeatable exchange instants, enable exact-stop mode.

Separately, slices overshoot by under 250 µs on 20 ms and the overshoot is
absorbed rather than accumulated — the budget snaps forward, so the next slice
is shortened. Always reconcile against the reported `currenttime`.

## Slide: Open problems

- **No reset protocol.** `wm_world_reset()` exists but FMI state serialization
  does not, so a fuzzing loop that snapshots and restores guest state
  desynchronizes the world. This blocks world-backed models in the in-process
  fuzzing path and is unaffected by anything else here.
- **Event mode.** `world` rejects FMUs using early return or event mode, which
  `fmu.c` handles. Confirm the exporter's output before migrating.
- **Path 1 is unbuilt.** The register-mapping generator that would make
  "Modelica file to device model" literal does not exist. It is the piece that
  matters for BoardRunner's mission.
- **The async catch-up thread** in `ardupilot.c` remains a determinism bug
  worth fixing independently of any of this.
- **Symbols do not resolve.** `add_map_file` is commented out
  (`toml_parser.py:322`), so `at = "main"` fails for user-authored virtuals and
  the `at = "main+4"` examples in the existing docs do not work as written.
  The world preprocessor reads the ELF itself to work around this.

## Slide: Where this goes next

The staged path, cheapest first:

1. **Delete the `ardupilot.c` polling thread.** One file, a real determinism
   bug, independent of everything else.
2. **World behind `phy_backend_t` as an adapter.** The copter keeps flying,
   consumers do not change, and magic value references are replaced by
   discovery and validation.
3. **Retire the adapter.** Migrate ArduPilot virtuals to `api_world_*` one at
   a time; when the last one moves, adding a sensor becomes a TOML line.
4. **The register-mapping generator** — path 1, and the actual contribution.

For scaling, slave mode is the more promising thread. Swarm workers are
isolated today; a master that grants every worker the same slice, waits for
all their `STOP` events, and exchanges at the boundary makes a coupled
experiment deterministic instead of a wall-clock race. The mechanism is
already in the fork.

## Slide: Inventory

| Path | Role |
| --- | --- |
| `docs/WorldIntegration.md` | Design proposal, integration surfaces, migration risks |
| `docs/WorldPlugin.md` | The plugin, its TOML, the RLC/GPIO pipeline |
| `docs/CoSimulationSlave.md` | Budget protocol, master client, semantics |
| `virtuals/world/host/preprocessor.py` | Validation, manifest, pin rules |
| `virtuals/world/runtime/world_plugin.c` | World construction, two callbacks |
| `virtuals/world/meson.build` | Self-detecting build |
| `configs/world_rlc_gpio.toml` | Physics demonstration |
| `configs/cosim_slave.toml` | Slave-mode demonstration |
| `tests/firmwares/world_rlc_gpio/` | Demo firmware and build script |
| `utils/fastdyn_cosim.py` | Co-simulation master client and CLI |
| `tests/unit/test_world_plugin.py` | 12 preprocessor tests |
| `tests/unit/test_fastdyn_cosim.py` | 11 protocol tests against a fake QMP server |
| `utils/cosim_lockstep.py` | Multi-instance lockstep example |
| `tests/integration/run_cosim_slave_smoke.sh` | End-to-end slave-mode verification |
| `patches/qemu-fastdyn-plugin-icount.patch` | Budget interface fixes |

Changes to pre-existing tracked files: one line in `virtuals/meson.build`, and
the QEMU patch. Nothing in `src/fastdyn`, `Makefile`, or `meson_options.txt`.
