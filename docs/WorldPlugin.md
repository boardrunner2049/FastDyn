# World Model Plugin

The `world` plugin co-simulates FMI 3 physics alongside emulated firmware and
wires that physics to guest pins. A Modelica model becomes a peripheral's
physical behavior without a line of C.

This page documents the complete pipeline through a runnable demonstration:
firmware toggles a GPIO pin, the pin drives the supply of an RLC circuit, and
the firmware reads the capacitor voltage back through an analog input.

```text
RLC.mo                Modelica source
  |  (exporter)
  v
RLC.fmu               FMI 3 Co-Simulation archive
  |  [CPU.cpu0.plugins.world.models]
  v
world.manifest        written by the host preprocessor
  |  virtual_artifact_path()
  v
world_model runtime   inside libfastdyn.so
  |  world_digital_out / world_analog_in at firmware PCs
  v
guest registers       what the firmware sees
```

## Run the demonstration

```bash
# 1. Fetch and build world_model and the RLC demonstration FMU.
#    tools/world is a submodule and is not fetched by `make`, because the
#    plugin is optional. It is not released yet (PSecLab access needed) and
#    is skipped by `git submodule update` unless `--checkout` is given.
git submodule update --init --checkout tools/world
python3 -m venv tools/world/venv
tools/world/venv/bin/pip install -e tools/world
cmake -S tools/world -B tools/world/build -DWM_BUILD_TESTS=ON
cmake --build tools/world/build

# 2. Build the demo firmware.
tests/firmwares/world_rlc_gpio/build.sh

# 3. Build FastDyn -- AFTER step 1. The plugin compiles itself in when
#    tools/world/build/libworld_model.so exists and is skipped otherwise, and
#    meson decides that at configure time: a plugin library built before
#    tools/world existed silently omits the runtime. Keep your usual feature
#    flags; the plugin needs none of its own.
make qemu_path=<path/to/qemu>

# 4. Run.
export LD_LIBRARY_PATH=$PWD/tools/world/build:$PWD/build:$LD_LIBRARY_PATH
fastdyn run -c configs/world_rlc_gpio.toml -o fastdyn_work_world
```

Expected output, showing the capacitor charging toward 3.3 V while the pin is
high and discharging toward 0 V while it is low:

```text
world_rlc_gpio: driving an RLC circuit from a GPIO pin
  sample  pin      capacitor
     0  1      1106 mV
     1  1      2019 mV
     2  1      2555 mV
     3  1      2866 mV
   ...
     8  0      2165 mV
     9  0      1263 mV
    10  0       735 mV
   ...
    16  1      1134 mV
```

The firmware toggles indefinitely, so this repeats until you stop the run with
Ctrl-C.

The bundled RLC is overdamped (R = 10 Ω, L = 10 mH, C = 1 mF), giving a
dominant time constant near 8.9 ms, so a 100 µs communication step resolves
the transient comfortably.

## One configuration file

`configs/world_rlc_gpio.toml` describes the machine and the physical world it
is attached to. Everything world-specific lives under one plugin table.

### Models

```toml
[CPU.cpu0.plugins.world]
enabled = true
step_ns = 100000          # co-simulation communication quantum

[CPU.cpu0.plugins.world.models.rlc]
path = "tools/world/examples/rlc/out/RLC.fmu"

[CPU.cpu0.plugins.world.models.rlc.parameters]
resistance = 10.0
inductance = 0.01
capacitance = 0.001
```

Relative paths resolve against the working directory. Parameters are applied
at initialization and on every reset. Declare several
`[CPU.cpu0.plugins.world.models.<name>]` tables to compose multiple FMUs, and
link them with `[CPU.cpu0.plugins.world.connections]`; the runtime rejects
dependency cycles before initialization.

### Endpoints

An endpoint gives a stable local name to one world variable, so firmware
bindings do not spell out exporter-specific paths:

```toml
[CPU.cpu0.plugins.world.endpoints]
supply  = { target = "rlc.voltage",        direction = "in" }
vcap    = { target = "rlc.output_voltage", direction = "out" }
current = { target = "rlc.current",        direction = "out" }
```

`target` is `<model>.<variable>`, where the variable name is the one declared
in the FMU's `modelDescription.xml`. `direction` is checked against the
variable's FMI causality at runtime: binding an output as an input fails with
a causality error rather than silently doing nothing.

### Pins

A pin binds an endpoint to a firmware program counter.

```toml
[[CPU.cpu0.plugins.world.pins]]
at = "world_gpio_write"     # firmware symbol, or "0x8000082"
kind = "digital_out"
endpoint = "supply"
register = "r0"
low = 0.0
high = 3.3

[[CPU.cpu0.plugins.world.pins]]
at = "world_adc_read"
kind = "analog_in"
endpoint = "vcap"
register = "r0"
scale = 1000.0              # volts -> millivolts
```

| Key | Meaning |
| --- | --- |
| `at` | Trigger PC: a firmware symbol name or a numeric address. |
| `kind` | `digital_out` drives a world input; `analog_in` samples a world output. |
| `endpoint` | The endpoint declared above. Direction is validated against `kind`. |
| `register` | Core register carrying the value (`r0`-`r12`, `sp`, `lr`, `pc`). |
| `low` / `high` | `digital_out` only: the physical values for a zero and non-zero register. |
| `scale` | `analog_in` only: multiplied into the physical value before the integer register write. |

### Trace

```toml
[CPU.cpu0.plugins.world.trace]
output = "rlc_trace.csv"
variables = ["rlc.output_voltage", "rlc.current"]
```

The CSV lands under `<work-dir>/run-artifacts/world/` and is sampled at
initialization and at every internal communication point — far finer than the
firmware's own sampling, which is the point: it records what the physics did
between observations.

## Watching the physics: world_model's observer

world_model ships a read-only web observer that plots a trace alongside the
model metadata. Enable it from the plugin table:

```toml
[CPU.cpu0.plugins.world.trace]
output = "rlc_trace.csv"
variables = ["rlc.voltage", "rlc.output_voltage", "rlc.current"]

[CPU.cpu0.plugins.world.observer]
enabled = true
host = "127.0.0.1"
port = 8770
open_browser = false
horizon_ns = 200_000_000   # initial x-axis extent; cosmetic, the view auto-fits
```

FastDyn derives a world TOML from the `models`, `endpoints`, `connections` and
`trace` tables, runs world_model's own generator on it to produce
`world_manifest.json` and `ui_config.json`, and serves them for the lifetime of
the run:

```text
World observer available at http://127.0.0.1:8770
```

The page plots the traced series, lists every physical variable with its
causality and unit, shows the runtime log, and lists physical events. It
**only reads artifacts** — it cannot advance or steer the world, which is
correct here, because the guest decides when physics advances.

### If the plots are flat but events keep arriving

The observer plots a rolling window of the last 5000 trace samples. If the
firmware stops driving its pin, the capacitor settles and that window becomes a
straight line, even though the run looks healthy and the event list keeps
filling.

The demo firmware derives its pin edge from the sample counter
(`sample_index % SAMPLES_PER_LEVEL`) precisely so this cannot happen. An
earlier version used a nested loop bounded by a comparison against the running
counter, which GCC compiles to `cmp/bne`:

```text
8000106:  cmp r7, r8        ; r8 = sample_index + 1, r7 = target
8000108:  bne.n 80000a4
```

A single miscount lets `r8` step past `r7`, and that loop then never exits: the
pin is stuck, the physics freezes, and the plots flatten. If you write your own
world-driving firmware, bound its loops so they cannot be wedged by one bad
iteration.

### If the page never leaves "waiting for trace"

The host half of this plugin runs whether or not the native half was compiled
in: the observer starts, the metadata is generated, the paths are correct --
and nothing writes the trace or the log. That presents as a broken observer
rather than a missing plugin.

The tell is in the run's own output. A healthy run logs both of these:

```text
World observer available at http://127.0.0.1:8770
world ready: 1 model(s), 3 endpoint(s), 100000 ns step
```

If `world ready` never appears, the plugin library that run loaded was built
without the world runtime. Build `tools/world` first, then rebuild FastDyn.

A second, unmistakable symptom is the guest printing `3735928559 mV`, which is
`0xDEADBEEF`: with no virtual to write the register, the naked stub returns
whatever the previous semihosting call left in `r0`.

FastDyn also warns outright after a run that wrote neither trace nor log,
though a continuously-running demo only reaches that on exit.

### The runtime log and physical events

world_model routes FMU diagnostics through a log callback, and its observer
reads the resulting file for both its log panel and its event list. world's own
generated harness installs that callback; FastDyn owns the runtime here, so the
plugin installs the equivalent and writes `run-artifacts/world/runtime.log` in
world_model's format:

```text
189505 ns | supply | event | pin | supply driven to 3.300
38643201 ns | supply | event | pin | supply driven to 0.000
```

The observer picks physical events out of that stream by matching the `event`
severity, so each pin edge appears in its event list. An edge is reported only
when the level actually changes — firmware rewrites an unchanged level far more
often than it changes it.

Without this the log and event panels sit empty and read as though something is
broken, which is what they did before the callback was installed.

Generating the metadata rather than hand-writing it keeps world_model's
artifact format inside world_model. `observer` accepts `true` as shorthand for
a default table, and requires a `trace` table, since a plot needs a trace.

The observer serves for the lifetime of the run and shows a **rolling window of
the last 5000 trace samples**. world_model traces every communication step and
rejects any other setting (`[Trace].every_step` must be `true`), so there is no
decimation and the visible span is simply:

```text
visible span = 5000 x step_ns
```

At the physics demo's 100 µs quantum that is only 500 ms. Both observer
configurations therefore use `step_ns = 1000000`, giving a **5 s** window.
That costs nothing in accuracy: everything up to a 1 ms step is numerically
identical for this circuit (see [`step_ns` is a fidelity
parameter](#step_ns-is-an-integration-step-not-a-scheduling-knob) below —
measured 4.69 s of visible span with the guest readings unchanged).

To widen it further, raise `step_ns` again — but past 1 ms this circuit starts
to lose accuracy, and past ~3 ms the integrator diverges.

### The x-axis does not follow the data

Worth knowing before you conclude something is broken. world_model's page
caches the plot's x range on the first draw, taken from the world's
`start`/`stop` (which `horizon_ns` sets), and afterwards only ever widens the
y range:

```js
function autoView(panel,data){
  if(!panel._autoView){ /* x fixed here, once */ }
  else { /* only ymin/ymax are updated */ }
}
function fit(panel){panel._view=null;drawPanel(panel)}   // clears zoom, not _autoView
```

So the data window slides forward while the axis stays put, and "Reset view"
clears a manual zoom but not the cached range. Set `horizon_ns` to match the
visible span so the two line up at startup, then pan or zoom with the mouse
(drag to pan time, wheel to zoom) to follow the run.

Making the view track the newest data is a one-line change upstream —
`fit()` would also need to clear `panel._autoView` — in `tools/world`.

Two configurations use the observer, one per co-simulation mode.

### Watch it live: `configs/world_rlc_observer_master.toml`

FastDyn owns its own clock here: physics advances on access, whenever the
firmware touches a world-backed pin. The firmware toggles continuously and
never exits, so the plots keep scrolling.

```bash
fastdyn run -c configs/world_rlc_observer_master.toml -o fastdyn_work_observer
# open http://127.0.0.1:8770; Ctrl-C to stop
```

The observer starts before QEMU does, so for the first moment the page reads
`waiting for trace`. It clears itself once the guest begins.

This configuration sets `icount = { shift = 5, sleep = true, align = true }`,
which throttles the guest to the wall clock. That matters for a continuous
firmware: unpaced, the guest outruns realtime by more than an order of
magnitude and emits thousands of samples a second. Paced, virtual time tracks
real time almost exactly — 13.38 s of guest time in 13 s of wall time — so the
scrolling plot reads like a scope.

### Step through it: `configs/world_rlc_observer_slave.toml`

The same world in [slave mode](CoSimulationSlave.md), where a master decides
how far time advances, so you extend the curves one slice at a time.

```bash
# Terminal 1: the guest starts paused, with the observer already serving.
fastdyn run -c configs/world_rlc_observer_slave.toml -o fastdyn_work_observer

# Terminal 2: open http://127.0.0.1:8770, then grant time in small steps.
utils/fastdyn_cosim.py /tmp/fastdyn-observer.qmp --slice-ms 5 --slices 40
```

Before the first slice the trace holds a single row — the sample at time zero —
because nothing has run. After 20 slices of 5 ms it holds 984 rows across
`time_ns`, `rlc.voltage`, `rlc.output_voltage` and `rlc.current`. The physics
advances only when the master says so, and the plot shows exactly that. This
configuration leaves `align` false: the master sets the pace, so QEMU should
not also throttle to the wall clock.

## How firmware exposes a pin

A pin needs a stable instruction to trigger on, and for `analog_in` the
written register must survive to the caller. Both are satisfied by a naked
stub:

```c
__attribute__((naked, noinline)) void world_gpio_write(int level)
{
    (void)level;
    __asm__ volatile("bx lr");
}

__attribute__((naked, noinline)) int world_adc_read(void)
{
    __asm__ volatile("bx lr");
}
```

The virtual bound to `world_gpio_write` reads the level the firmware passed in
`r0`. The virtual bound to `world_adc_read` writes millivolts into `r0`, and
the stub's single `bx lr` returns it. Because the body is empty, nothing
between the hook and the return can disturb the register.

The firmware toggles its pin continuously and never exits, so there is always
physics to observe. Stop a run with Ctrl-C, or bound it from a co-simulation
master by granting a fixed number of slices. A firmware that never ends is also
the right shape under a master: the run lasts exactly as long as the master
grants, instead of the guest shutting down underneath it mid-slice.

This is the function-hook integration path. It is firmware-specific by
construction: the addresses are valid only for one build. Modeling the
peripheral itself so that ordinary MMIO reaches the world is a separate and
larger piece of work; see [WorldIntegration.md](WorldIntegration.md).

## What the host preprocessor does

`virtuals/world/host/preprocessor.py` owns every world-specific concern:

1. validates the plugin table — models, endpoints, pins, trace;
2. resolves FMU paths and fails with the build command when one is missing;
3. resolves symbolic pin triggers from the firmware ELF, clearing the ARM
   Thumb mode bit, and only when a symbolic name is actually used;
4. checks each pin's `kind` against its endpoint's declared direction;
5. rejects two pins that resolve to the same address, because the native
   dispatcher supports one callback per PC;
6. writes `run-artifacts/world/world.manifest`; and
7. returns the pin rules as generated `VirtualInstruction`s.

The generated rules then flow through FastDyn's ordinary pipeline — trigger
resolution, conflict validation, serialization — exactly like user-authored
ones. For this configuration `<work-dir>/virtuals/virtuals.txt` contains:

```text
0x8000082 world_digital_out supply 0 0.0 3.3
0x8000084 world_analog_in vcap 0 1000.0
```

and the manifest is:

```text
step_ns	100000
model	rlc	/data/fastdyn/tools/world/examples/rlc/out/RLC.fmu
param	rlc	resistance	10.0
endpoint	supply	rlc	voltage	in
endpoint	vcap	rlc	output_voltage	out
trace	/data/fastdyn/fastdyn_work_world/run-artifacts/world/rlc_trace.csv
trace_var	rlc.output_voltage
```

## What the native runtime does

`virtuals/world/runtime/world_plugin.c` registers `world_digital_out` and
`world_analog_in`, then builds the world from the manifest through
world_model's public C API: `wm_model_load`, `wm_world_create`,
`wm_world_add_model`, `wm_world_set_parameter`, `wm_world_configure_trace`,
`wm_world_initialize`.

Every endpoint resolves once, after initialization, to a
`wm_variable_handle_t`. The callbacks use handles, never a name lookup.

## Time

The world advances only when the guest observes or drives it:

```c
static void world_sync(void)
{
    uint64_t now = virtual_guest_time_ns(runtime);
    if (now <= world_time_ns) return;
    wm_world_advance_to(world, now);
    world_time_ns = now;
}
```

Each callback calls this before acting. Three properties follow:

- **Physics is fresh at the instant of the access.** There is no tick rate to
  tune against the firmware's sampling rate, and no aliasing between them.
- **The world never runs ahead of the guest,** so a read cannot return a value
  from the guest's future.
- **Repeated accesses in one guest instant are free.** `wm_world_advance_to`
  iterates `while (time_ns < target)`, so an unchanged timestamp does no work.

A `digital_out` pin advances *before* applying the new level, so the interval
that just elapsed is integrated with the value that was actually applied
during it, and the new level takes effect from that instant forward.

The demonstration configuration enables `icount`, which makes guest
nanoseconds a deterministic function of instructions retired. The world
therefore advances identically on every run — necessary for twintrace replay
and for reproducible fuzzing.

**This policy is sufficient here because the demo's interrupts do not come
from physics.** Firmware that sleeps waiting for an interrupt the world must
generate would never wake, since no access would advance the world. Such a
configuration needs a periodic advance as a floor; FastDyn's existing
`raise_periodic_irq` is the natural carrier.

## Abstraction boundaries

The plugin follows [WritingVirtuals.md](WritingVirtuals.md):

- Native and host halves live together under `virtuals/world/`, discovered
  generically — the frontend contains no reference to `world`.
- The plugin is self-enabling. It adds no build option and no top-level make
  flag; `virtuals/meson.build` lists it exactly as it lists the other
  compiled-in plugins, and the plugin's own `meson.build` decides whether it
  can build. `Makefile`, `meson_options.txt` and `src/fastdyn/` are untouched.
- The runtime registers itself with `VIRTUAL_PLUGIN("world", ...)`. No entry
  was added to `cb_registry`, and `virtuals/virtuals.c` is untouched.
- No QEMU `--plugin` argument is added. Configuration reaches the runtime as
  an artifact resolved through `virtual_artifact_path()`.
- Register indices come from `include/fastdyn/arch/arm32.h` by name; no raw
  register numbers appear in the configuration or the runtime.
- Callback arguments are parsed and validated defensively in C even though the
  preprocessor already validated them.

## Limitations

- **ARM only.** `register = "..."` names are validated against the ARM core
  register set; other architectures are rejected with a clear error rather
  than silently mismapped.
- **Endpoint names must not collide with firmware symbol names.** FastDyn's
  generic argument resolution rewrites a token that matches a symbol into its
  address. Prefer descriptive endpoint names such as `supply` over names that
  might exist in the ELF.
- **Scalar Float64 endpoints.** The pin types cover the digital-out and
  analog-in cases; other FMI types are not yet exposed through pins.
- **No reset protocol.** `wm_world_reset()` exists but FMI state
  serialization does not, so a fuzzing loop that snapshots and restores guest
  state will desynchronize the world. World-backed models are not yet suitable
  for the in-process fuzzing path.
- **Event mode is rejected.** world_model refuses FMUs that use early return
  or event mode. Check an exporter's output before committing to it.

## Files

| Path | Role |
| --- | --- |
| `virtuals/world/host/preprocessor.py` | Validation, manifest generation, pin rules |
| `virtuals/world/runtime/world_plugin.c` | World construction and the two callbacks |
| `virtuals/world/meson.build` | Detects a built `tools/world` and links `libworld_model` |
| `configs/world_rlc_gpio.toml` | The single demonstration configuration |
| `configs/world_rlc_observer_master.toml` | The same world with the web observer, running live |
| `configs/world_rlc_observer_slave.toml` | The same world with the observer, paced by a master |
| `virtuals/world/host/observer.py` | Derives the world TOML, generates metadata, serves the observer |
| `tests/firmwares/world_rlc_gpio/` | Demo firmware source, linker script, build script |
| `tests/unit/test_world_plugin.py` | Host-side preprocessor tests |
| `tools/world/` | The standalone world_model runtime |
