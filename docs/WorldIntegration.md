# World Model Integration

**Status: proposal.** `tools/world` exists and works standalone; the
FastDyn-side layers below do not. Sections marked *Today* describe current
behavior and are verifiable in the tree.

The proposal is narrower than it first appears. FastDyn **already** has a
correct guest-time coupling policy for its FMU backend
(`virtuals/virtuals.c:134-152`), and this document adopts it rather than
inventing one. What is genuinely new is the register mapping and its
generator; the timing model is largely existing practice, written down.

This document proposes how `tools/world` — a standalone FMI 3 Co-Simulation
runtime — becomes the physics source for FastDyn device models, so that a
model author can describe a peripheral's physical behavior in Modelica rather
than hand-coding it in C.

## Scope and non-goals

In scope: the boundary between FastDyn and `world`, the runtime execution
model, and the generation flow that turns a Modelica-derived FMU into a
loadable device model.

Explicitly not in scope: running `world` as a separate process, service, or
thread. Everything below executes inside `libfastdyn.so` on the QEMU plugin's
thread. The only `world` component that runs outside QEMU is its Python
generator, which runs before QEMU starts and exits before it launches.

## What world provides, and what it does not

*Today.* `world` converts an FMU into typed, named accessors over absolute
virtual time:

```c
wm_init();
set_fire_intensity(5.0);
wm_advance_to(1000000);      /* uint64_t nanoseconds */
get_temperature(&temperature);
wm_destroy();
```

Accessors resolve at generation time to numeric binding IDs; there is no
runtime string lookup. All public time is `uint64_t` nanoseconds, and
`wm_world_advance_to` rejects backward time
(`tools/world/src/world.c:320`).

What it does not provide is the larger half of a device model. Consider
BoardRunner's `examples/stm32f429i_disc1/hybrid/ADC_with_DMA/generated_models/adc_model.c`:
of roughly 150 lines, the physical content is one assignment
(`s->dr = 0x0AAA;`). The remainder is register offsets, bitfield semantics
(`ADON`, `CONT`, `SWSTART`, `EOC`, `EOCIE`), conversion timing, the DMA
request, and interrupt gating.

`world` therefore supplies a peripheral's **transfer function**, not the
peripheral. Bridging the two is the work this document proposes.

## Today: two integration surfaces, not one

FastDyn has two independent paths by which emulated firmware obtains data, and
they are frequently confused. The existing FMU physics path uses the second,
not the first.

**Path 1, device handler (MMIO).** A `DeviceModel` with `init`, `read`,
`write`, `serve`, and `interrupt` bound to address ranges, dispatched by
`device_models/dev.c` to the classic, elder, passthrough, or twintrace
backend. Firmware-agnostic: the model describes a chip, so it applies to any
firmware running on that chip.

**Path 2, function hook (virtual).** A virtual instruction bound to the PC of
a firmware *function*, whose callback synthesizes data directly into guest
registers and memory. Firmware-specific.

The FMU backend is a `phy_backend_t`, not a `DeviceModel`. Nothing under
`virtuals/` calls `dev_register_device_model`. The ArduCopter configuration is
26 virtuals and 95 modifiers over raw addresses
(`virtuals/physics/flight_controllers/courbet/ardupilot/copter462/unlabeled_conf/`):

```text
0x8139f90 ins_block_read *
0x803d658 compass_read_block *
0x8155a84 adc_voltage *
```

Those callbacks already perform the encoding step this document proposes
generating: `convert_to_invensense()` and `pack_to_hmc5843_wire_format()` in
`virtuals/physics/flight_controllers/ardupilot/ardupilot.c` manufacture the
exact wire bytes of an InvenSense IMU and an HMC5843 magnetometer and inject
them at the driver's function boundary, bypassing the SPI and I2C peripherals
entirely.

| | Path 2, function hook | Path 1, device handler |
| --- | --- | --- |
| Physics precedent | Flies ArduCopter today | None |
| Firmware coupling | Requires symbols and PCs; invalidated by recompilation | Firmware-agnostic |
| Reuse unit | One firmware build | One chip |
| Fidelity | Elides the peripheral, so driver bugs, register races, and DMA are invisible | Exercises the real driver path |
| Cost per sensor | Hand-written wire-format packer | Model the peripheral once, then reuse |

Path 2's coupling cost is visible in the configuration itself: `binary` is
pinned to `arducopter_v462` and `existing_config_path` names a checked-in
rules directory, because those addresses are valid only for that build.

**The register mapping proposed below targets path 1, where no physics
precedent exists.** It is correspondingly more novel and less validated than
the copter path. This matters for BoardRunner specifically: path 2
deliberately elides the peripheral, which is the object of study when the goal
is learning and verifying peripheral models.

A staged adoption follows. Path 2 with world is a small change that swaps the
data source beneath packers that already exist, and validates FMI 3 loading,
guest-time coupling, and typed bindings against a real workload. Path 1 with
world is the register-mapping generator, and is the contribution.

## Today: the device model contract

The elder backend `dlopen`s a model and resolves symbols by name convention
(`device_models/elder/elder.c:109-126`):

```c
void*    <name>_init(ConfigSection *args);
uint64_t <name>_read (void *opaque, hwaddr offset, unsigned size, uint64_t pc);
void     <name>_write(void *opaque, hwaddr offset, uint64_t value,
                      unsigned size, uint64_t pc);
int      <name>_serve(int);
int      <name>_interrupt(int);
```

Note that read and write receive `pc`, not time. A model that needs guest
time calls `core_get_icount()`; there is no `ns` accessor on this path today.

## Proposed design

### Three layers

| Layer | Location | Responsibility |
| --- | --- | --- |
| A | `tools/world`, unmodified | FMI 3 loading, composition, `advance_to`, typed bindings |
| B | new, in `libfastdyn.so` | One active world; the clock policy; shadow registers; IRQ queue |
| C | `boardrunner_sdk` | `api_world_*` accessors used by device model authors |

Layer A must stay free of FastDyn concepts. Its independence is what makes it
testable and reusable; `world`'s public header deliberately contains no FMI
terms, and it should acquire no FastDyn terms either.

### Build and packaging

`libworld_model` links into `libfastdyn.so` behind a new meson option
(`enable_world`), following the existing `enable_fmu` / `enable_libhw`
pattern in `meson.build`. Its dependencies — libxml2, minizip, and `dlopen` —
are all usable from within a QEMU plugin. FMU binaries are host-native
`x86_64-linux` objects, which is orthogonal to the emulated guest
architecture.

### Configuration-time flow

`wm-generate` is codegen, not runtime. It validates the world TOML and its
FMUs, then writes `world_bindings.{c,h}`, `world_manifest.json`, and
`ui_config.json`. This maps directly onto the existing run-preprocessor SDK
described in [VirtualPreprocessing.md](VirtualPreprocessing.md): register a
`RunDefinition` whose `prepare()` invokes generation and returns the produced
files as artifacts.

```text
world.toml + peripheral.toml + *.fmu
            |
            v
   run preprocessor (host, before QEMU)
            |
            +--> world_bindings.{c,h}, world_manifest.json
            +--> generated <name>_{init,read,write}.c
            v
   compiled to <name>.so, loaded by the elder backend
```

Python never loads, steps, or controls an FMU. That boundary is already
enforced in `world`: `wm_world_load_toml()` is a stub returning
`WM_ERR_UNSUPPORTED` (`tools/world/src/config.c`).

### Runtime execution model

*This policy already exists for the FMU backend and should be preserved, not
redesigned.* `kick_irq` (`virtuals/virtuals.c:134-152`) advances physics to
`qemu_plugin_get_virtual_timer()` on each periodic IRQ, before the guest
handles it — "lockstep with the firmware timer tick," in the source comment.
`fmu_advance_simulation` (`virtuals/physics/physics_engines/fmu/fmu.c:672`)
already caps its internal step at 2 ms and already stages inputs, applying
`pwm_dirty` through `set_pwm_inputs()` at step boundaries.

The world advances at exactly one place — a periodic tick driven by guest
time. MMIO accesses never advance it.

```text
tick:   apply pending writes
        wm_advance_to(guest_now)
        latch outputs into shadow registers
        drain queued IRQs

read:   return shadow value          O(1), no solve
write:  stage into pending buffer    O(1)
```

Three consequences motivate this shape over advancing lazily inside an access:

- **Bounded cost on the hot path.** `wm_world_advance_to` iterates
  `ceil(dt / quantum_ns)` times, stepping every model and propagating
  connections on each pass (`tools/world/src/world.c:320`). Performing that
  inside a TCG read callback makes a single register read arbitrarily
  expensive after an idle period.
- **No reentrancy.** `wm_step_callback_t` fires inside the advance loop. If
  advancement can be triggered from an MMIO callback, step callbacks execute
  nested inside guest memory accesses. Confining advancement to the tick
  removes that case.
- **Write ordering is centralized.** `world` requires that inputs set at T
  take effect only after the caller has advanced to T. Expressed once in the
  tick, this is a property of the framework; expressed per model, it is a
  rule every author can forget.

The tick reuses existing machinery rather than introducing a clock:
`timer_irq_period_ns` (1 ms by default) and the QEMU virtual timer already
used by `timer_start`.

#### Staleness is fidelity, not approximation

A shadow register updated on a tick is a more faithful model than one solved
at the instant of the read. Real peripherals latch a sampled value; firmware
reads the latch, never the physical world. An ADC data register holds a
conversion that completed in the past, and a PWM duty write takes effect at
the next period boundary.

Two escape valves, both declared per register:

- `immediate = true` forces an advance on a write whose round-trip latency
  changes firmware behavior.
- A per-world tick period, so one fast signal does not raise the cost of
  every other model.

### Time contract

| Invariant | Rationale |
| --- | --- |
| The world never advances past `guest_now` | Leading the guest returns values from its future; the resulting acausality is very hard to diagnose. Assert on it. |
| The tick derives from guest time, never host time | Under icount, guest time is a deterministic function of instructions retired, so the tick replays identically. |
| Advancement is single-threaded | See *Determinism* below. |
| Multiple accesses at one guest instant coalesce | One advance per instant, not one per access. |

### Interrupts originating in physics

A threshold crossing inside the world must be able to raise a guest
interrupt; this is the main reason the tick cannot be omitted, since firmware
may be blocked in WFI with no MMIO pending. Register a step callback that
evaluates conditions and enqueues interrupts; the tick drains the queue after
`advance_to` returns. Interrupts are queued rather than raised from inside
the callback so that they are never delivered mid-access.

### Register mapping

This is the new declarative artifact, and the piece that makes
"Modelica file to device model" literal. It binds register offsets to world
aliases and encodings:

```toml
[Peripheral.adc3]
base  = "0x40012200"
world = "examples/thermal/world.toml"

[Peripheral.adc3.registers.DR]
offset = 0x4C
access = "r"
source = "temperature"                        # a [World.Outputs] alias
encode = { type = "u12", min = 0.0, max = 3.3 }

[Peripheral.adc3.registers.CR2]
offset = 0x08
access = "rw"
bits   = { ADON = 0, CONT = 1, SWSTART = 30 }
```

A generator over this plus `world_manifest.json` emits compilable
`adc3_{init,read,write}`. The generator lives in FastDyn, not in `world`, so
that `world` never learns what MMIO is.

The generated result is structurally identical to a hand-written model:
registers plus a periodic callback that updates them and raises interrupts —
the same shape as `adc_periodic_conversion_cb` in the existing ADC example.
Generated and hand-written models therefore share one SDK, one idiom, and one
debugging story.

### Layer C: the author-facing API

```c
#include <boardrunner/world.h>

double v;
api_world_get(WORLD_temperature, &v);
api_world_set(WORLD_pwm_duty, duty);
```

This matches the established SDK idiom (`api_dma_request`, `api_signal_set`,
`api_i2c_send`) rather than introducing a second style.

## Determinism, replay, and fuzzing

The world runs on the plugin thread and is driven by guest time. It is not
threaded, and must not become threaded for any configuration where
determinism is required: twintrace replay, LibAFL/AFLNet fuzzing, and swarm
reproducibility all depend on it. The README's argument for isolating swarm
workers — that shared state creates nondeterministic coupling — applies here
with equal force.

A free-running world on its own thread is defensible only under `libhw`
passthrough, where a physical board has already removed determinism. That
configuration's rules must not leak into the others.

**Open problem.** `wm_reset()` exists, but `world`'s design notes record that
FMI state serialization was investigated and not implemented
(`tools/world/docs/design.md`). Fuzzing loops that snapshot and restore guest
state will therefore desync a stateful world, or force a full re-initialization
per iteration. This must be resolved before world-backed models are usable in
the fuzzing path; it does not block sensor-style models in ordinary runs.

## Relationship to phy_backend_t

*Today.* `virtuals/physics/phy.h` defines a physics interface — reached
through path 2 above, never registered as a device handler — with a fixed
schema frozen to one domain: `get_imu_batch`, `get_navsat_reading`,
`get_mag_reading`, `get_altimeter_reading`, `get_lidar_samples`,
`set_servo_pwm`, and `get_joint_state(double *motor_0_pos, double
*motor_2_pos)` — the last specific down to two named motors. Adding a
peripheral means editing a struct in the plugin and recompiling.

What world generalizes is the **schema**, not the timing. Its generated typed
bindings replace a hand-maintained function table with one derived from FMU
metadata, so a new sensor is a TOML entry rather than a header change. The
timing policy in `kick_irq` is already correct and carries over.

Note also that the existing backend is not a legacy FMI 2 path:
`fmu.c` declares its own private FMI 3 ABI (`fmi3Status`,
`fmi3InstantiateCoSimulation_ft`, `fmi3DoStep`) — the same technique world
uses, isolated behind `wm_backend_*`. The `fmi2_user_functions.h` files under
`physics_engines/fmu/*/harness/` belong to separate per-sensor OpenModelica
harnesses, not to this path.

Tracked C and headers under `virtuals/physics/` total roughly 5,600 lines.
The much larger figure visible on disk is untracked build output under
`physics_engines/fmu/*/output_folder/*.fmutmp/`; consolidating onto world
would not retire tracked code at anything like that scale. Those directories
are untracked but absent from the root `.gitignore`, which is worth fixing
independently of this proposal.

## Migration risks

Three behavioral differences will surface when a working `phy_backend`
configuration moves onto world:

| Concern | Today (`fmu.c`) | World | Consequence |
| --- | --- | --- | --- |
| Event mode / early return | Handled: breaks the step loop on `event_needed \|\| early_return` | Rejected outright per `tools/world/docs/design.md` | An FMU that runs today can be refused. Audit the Rumoca output before committing to the migration. |
| Backward or repeated time | Soft no-op returning *success* (`fmu.c:678`) | Hard `WM_ERR_TIME` (`tools/world/src/world.c:320`) | Any caller that re-sends a stale timestamp starts failing. The async path below does exactly that. |
| Time type | `double` seconds | `uint64_t` nanoseconds | Round-tripping ns through a double is exact to ~104 days, so precision is not the issue; the gain is dropping epsilon comparisons such as `fmu_state.time_s + 1.0e-9 < run_until_time`. |

## The async path is real, and is the thing to avoid

The determinism argument above is not hypothetical. Two coupling modes exist
alongside the FMU tick:

- `virtuals/physics/flight_controllers/ardupilot/ardupilot.c:35` runs
  `catch_up()` as a `while (1) { ...; usleep(1000); }` thread, reading
  `last_sim_time_ns` from POSIX shared memory (`shm_open("/last_sim_time_ns")`,
  `virtuals/virtuals.c:120`). It advances physics on host-time polling,
  independent of the guest.
- `virtuals/physics/physics_engines/gazebo/altimeter_virtuals.c:25` uses a
  timer-driven variant of the same `catch_up` shape.

The source calls the first "the legacy async catch-up path," and the FMU
backend has already moved off it. World-backed models should adopt the FMU
tick, not this. The shared-memory publication is still useful for external
observers and can remain as a read-only side channel.

## Prerequisite: use Rumoca, not OpenModelica

`world`'s design notes report that the available OpenModelica 1.27 exporter
supports only FMI 1.0/2.0 and concludes that FMI 3 export is unavailable.
That conclusion does not hold inside FastDyn: Rumoca is already a pinned
submodule and already emits FMI 3, invoked with the literal `fmi3` argument at
`src/fastdyn/fmu_build.py:354`, with `utils/build_fmi3_fmu.py` as a standalone
entry point.

Modelica-to-FMI-3 export is consequently a solved problem in this repository,
and `tools/world/docs/design.md` should be corrected.

## Known sharp edge

`world`'s public header declares `wm_status_t wm_init(const char *config_path)`,
while generated `world_bindings.h` emits `#define wm_init() world_bindings_init()`,
which shadows it. Because the function form calls the always-failing
`wm_world_load_toml()`, the config-path variant cannot succeed. Layer B should
either use `wm_activate_world()` directly or have the declaration removed
upstream, so that reading the header alone does not mislead.

## Open questions

1. Default tick period: reuse `timer_irq_period_ns`, or give each world its
   own, or both with the world's value taking precedence?
2. Should `encode` support a general expression, or stay a closed set of
   affine and fixed-point forms? A closed set is validatable at generation
   time; expressions are not.
3. Does the register mapping belong in the world TOML, a separate peripheral
   TOML, or the FastDyn machine config? Keeping it out of the world TOML
   preserves layer A's independence.
4. What is the reset protocol for fuzzing, given the FMI state-serialization
   gap above?
