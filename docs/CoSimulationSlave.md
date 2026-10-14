# FastDyn as a Co-Simulation Slave

FastDyn normally owns its own clock: QEMU runs, and the firmware's virtual time
advances as fast as the host allows. In **slave mode** an external process owns
the clock instead. A master grants the guest a slice of virtual time, the guest
executes exactly that far and pauses itself, and the master decides what
happens next.

This is the standard co-simulation shape. It lets FastDyn participate in an FMI
master algorithm, stay in lockstep with a network simulator, or keep several
FastDyn instances synchronized with each other instead of racing on wall-clock
time.

This page is self-contained: prerequisites, runnable examples, the complete QMP
and Python interfaces, and the semantics you have to respect.

---

## Contents

- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Examples](#examples) — five runnable ones
- [Configuration reference](#configuration-reference)
- [QMP reference](#qmp-reference)
- [Python client reference](#python-client-reference)
- [Semantics you must respect](#semantics-you-must-respect)
- [Debugging](#debugging)
- [What was fixed](#what-was-fixed)
- [Limitations](#limitations)
- [Files](#files)

---

## Prerequisites

The budget interface lives in the patched QEMU, so it needs a QEMU rebuild.
The bundled slave configuration also drives the RLC world model, so it needs
`tools/world` and the demo firmware; skip steps 2 and 3 if you point the
configuration at your own firmware with no `[CPU.cpu0.plugins.world]` table.

```bash
# 1. Patched QEMU. The patch carries the budget interface and its fixes.
cd <path/to>/qemu
git apply <path/to>/FastDyn/patches/qemu-fastdyn-plugin-icount.patch
./configure --target-list=arm-softmmu --enable-plugins     # first time only
ninja -C build qemu-system-arm

# 2. world_model and the RLC demonstration FMU.
cd <path/to>/FastDyn
git submodule update --init --checkout tools/world
python3 -m venv tools/world/venv
tools/world/venv/bin/pip install -e tools/world
cmake -S tools/world -B tools/world/build -DWM_BUILD_TESTS=ON
cmake --build tools/world/build

# 3. The demo firmware.
tests/firmwares/world_rlc_gpio/build.sh

# 4. The FastDyn plugin, AFTER steps 1 and 2. exact_budget_stop is passed
#    through it, so it must be rebuilt against the patched QEMU headers, and
#    meson decides at configure time whether to compile the world runtime in.
make qemu_path=<path/to>/qemu

# 5. Make the runtime libraries discoverable.
export LD_LIBRARY_PATH=$PWD/build:$PWD/tools/world/build${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
```

Verify the whole path with the smoke test:

```bash
tests/integration/run_cosim_slave_smoke.sh
```

```text
exact-stop: 12/12 slices landed on the deadline
default: budget tracked the grant sum exactly
lockstep: two guests agreed on virtual time at every boundary
co-simulation slave smoke passed
```

---

## Quick start

Two terminals. The guest starts paused and executes nothing until granted time.

```bash
# Terminal 1: the slave.
fastdyn run -c configs/cosim_slave.toml -o fastdyn_work_cosim

# Terminal 2: the master.
utils/fastdyn_cosim.py /tmp/fastdyn-cosim.qmp --slice-ms 20 --slices 6
```

```text
connected; guest is prelaunch at 0 ns (exact-stop mode)
 slice   granted(ns)   advanced(ns)   reached(ns)  overshoot   status
     0      20000000       20000000      20000000          0   paused
     1      20000000       20000000      40000000          0   paused
     2      20000000       20000000      60000000          0   paused
     3      20000000       20000000      80000000          0   paused
     4      20000000       20000000     100000000          0   paused
     5      20000000       20000000     120000000          0   paused
```

The guest prints its RLC readings only while a slice is running, and stops
between them. Nothing advances unless the master says so.

---

## Examples

### 1. Sub-millisecond slices

The fixed per-stop cost that exact mode removes is what used to make small
quanta impractical. With `exact_budget_stop = true` in the configuration, a
500 µs quantum is clean:

```bash
utils/fastdyn_cosim.py /tmp/fastdyn-cosim.qmp --slice-ms 0.5 --slices 40
```

Every `overshoot` column entry is `0`.

### 2. Comparing the two stop modes

`--exact` requests the mode at runtime, which is convenient for driving both
from one configuration. The client reports the mode actually in effect, not the
one requested:

```bash
# Default: halts at the first block boundary after the deadline.
utils/fastdyn_cosim.py /tmp/fastdyn-cosim.qmp --slice-ms 0.5 --slices 40

# Exact: halts precisely on it.
utils/fastdyn_cosim.py /tmp/fastdyn-cosim.qmp --slice-ms 0.5 --slices 40 --exact
```

Measured over 40 × 500 µs slices on one machine:

| | default | exact |
| --- | --- | --- |
| median overshoot | 109 µs | **0** |
| max overshoot | 156 µs | **0** |
| slices landing exactly on the deadline | 4/40 | **40/40** |

### 3. A master that owns time and steps its own model

The library form. `run_slice()` blocks until the guest halts, so the master's
own model and the guest advance in step:

```python
import sys
sys.path.insert(0, "utils")
from fastdyn_cosim import BudgetMaster

SLICE_NS = 1_000_000          # 1 ms

with BudgetMaster("/tmp/fastdyn-cosim.qmp", exact_stop=True) as master:
    while master.time_ns < 100_000_000:
        my_model.advance_to(master.time_ns + SLICE_NS)   # your physics first
        master.run_slice(SLICE_NS)                       # then the guest
        exchange(my_model, master)                       # couple at the boundary
    print("finished at", master.time_ns, "ns")
```

Advance your own model *before* granting the slice if its outputs are the
guest's inputs for that interval, and read the guest's outputs after
`run_slice()` returns.

### 4. Several guests in lockstep

This is the case slave mode exists for. `utils/cosim_lockstep.py` launches N
FastDyn instances, grants each the same slice, and waits for all of them before
proceeding. Each gets its own QMP socket, RAM file and work directory, derived
from the base configuration, so nothing is shared by accident.

```bash
utils/cosim_lockstep.py --instances 2 --slice-ms 20 --slices 6
```

```text
2 guest(s) connected in exact-stop mode
 round         guest 0         guest 1  spread(ns)
     0        20000000        20000000           0
     1        40000000        40000000           0
     2        60000000        60000000           0
     3        80000000        80000000           0
     4       100000000       100000000           0
     5       120000000       120000000           0

final virtual times agree exactly: [120000000, 120000000]
```

`spread` is the disagreement between guests at the exchange boundary. It scales
to more instances (`--instances 3` gives three zero-spread columns), and the
loop body in that script is where a shared model, message router or radio
channel would exchange data.

**This is where exact-stop mode earns its keep.** With
`exact_budget_stop = false` the same run gives:

```text
 round         guest 0         guest 1  spread(ns)
     2        60075968        60094208       18240
     3        80071040        80097280       26240
     5       120048224       120098464       50240

final virtual times differ: [120048224, 120098464]
```

Each guest overshoots independently, so at every boundary they disagree about
what time it is — by up to 50 µs here. A conservative master can absorb that
with a guard band, but the guests cannot exchange data at a common instant.

### 5. Watching the physics while you pace it

`configs/world_rlc_observer_slave.toml` adds world_model's read-only web observer to
the slave configuration. The observer serves for the lifetime of the run, so
the guest being paused is what makes it useful: open the page, then grant time
in small steps and watch the curves extend.

```bash
# Terminal 1: prints "World observer available at http://127.0.0.1:8770"
fastdyn run -c configs/world_rlc_observer_slave.toml -o fastdyn_work_observer

# Terminal 2
utils/fastdyn_cosim.py /tmp/fastdyn-observer.qmp --slice-ms 5 --slices 40
```

Before the first slice the trace holds one row, the sample at time zero. After
20 slices of 5 ms it holds 984. See [WorldPlugin.md](WorldPlugin.md) for the
observer's configuration.

### 6. Handing over after boot instead of at reset

Not recommended — see [Starting mid-execution](#starting-mid-execution) — but
the mechanism exists. Drop `stop_on_start` and place the `start_budgeting`
virtual at the PC where the firmware should hand over:

```toml
[[CPU.cpu0.virtuals]]
at = "0x8000088"          # numeric: the generic symbol map is not populated
instruction = "start_budgeting"
args = []
```

```text
connected; guest is paused at 480 ns
```

The 480 ns is the reset-to-`main` prologue the guest ran before the master took
over — time that is outside the co-simulation.

---

## Configuration reference

There is no single "slave mode" switch. Slave mode is a combination of settings
plus a master that connects.

| `[Machine]` key | Effect |
| --- | --- |
| `qmp_socket` | The master's control channel. Required. |
| `stop_on_start` | Start paused, so the guest executes nothing until granted budget. |
| `icount` | Makes virtual time a deterministic function of instructions retired. Strongly recommended. |
| `exact_budget_stop` | Halt precisely on each deadline instead of after it. Off by default. |

`configs/cosim_slave.toml` sets all four:

```toml
[Machine]
qmp_socket = "/tmp/fastdyn-cosim.qmp"
stop_on_start = true
icount = { shift = 5, sleep = false, align = false }
exact_budget_stop = true
```

`icount` is not strictly required — budget deadlines are virtual-time deadlines
either way — but without it virtual time tracks host time and the guest's
execution stops being a reproducible function of the budget you granted.

`exact_budget_stop` reaches QEMU the same way `coverage` and `twintrace` do:
`toml_parser.py` reads it, `qemu_target.py` forwards it to the FastDyn plugin as
`--plugin <lib>,…,exact_budget_stop=1`, and `core/core.c` calls
`qemu_plugin_set_budget_exact_stop()`. FastDyn owns the launch command; the
setting travels with the run's configuration.

### Starting mid-execution

The `start_budgeting` virtual enters budget mode at a chosen PC rather than at
reset. It works and is retained, but is **not recommended**:

- **The prefix is outside the co-simulation.** Guest virtual time advances
  while the master is not watching and no coupled model is stepped. A master
  that starts its own clock at zero is then silently offset.
- **The obvious use case is already covered.** Skipping an expensive boot does
  not need a second mechanism: grant one large slice, then switch to fine ones.
- **It is the only path that drives the VM from a vCPU thread.**
  `budget_exhausted()` calls `vm_resume()` and `qemu_system_vmstop_request()`
  while holding the budget lock. With `stop_on_start` that runs only on the
  main loop, from the QMP handler and the deadline timer.

A symbolic `at = "main"` does not currently resolve: FastDyn's generic symbol
map is not populated on this path (`add_map_file` is commented out in
`toml_parser.py`), so user-authored virtuals need a numeric address.

---

## QMP reference

Three commands and one event. Everything the budget interface exposes.

### `run-for`

Add virtual time to the guest's cumulative deadline and resume it.

```json
-> { "execute": "run-for", "arguments": { "budget": 20000000 } }
<- { "event": "RESUME" }
<- { "return": { "totalbudget": 20000000, "currenttime": 0, "exact": true } }
```

**Asynchronous.** It returns while the guest is still running, so the
`currenttime` it reports *precedes* the slice it just granted. `budget` is in
nanoseconds and is added to the cumulative deadline, not set as an absolute
time.

### The `STOP` event

Emitted when the guest reaches its deadline and pauses. A master waits for this
before reading state.

```json
<- { "event": "STOP" }
```

If the firmware finishes inside a slice you get `SHUTDOWN` instead and no
`STOP` will ever arrive.

### `query-budget`

Read the guest's position without granting anything.

```json
-> { "execute": "query-budget" }
<- { "return": { "totalbudget": 20000000, "currenttime": 20000000, "exact": true } }
```

| Field | Meaning |
| --- | --- |
| `totalbudget` | Cumulative deadline in ns: the exact sum of everything granted. |
| `currenttime` | Virtual time the guest has actually reached. |
| `exact` | Whether exact-stop mode is in effect. |

### `set-budget-mode`

Select the stop mode at runtime, overriding the configuration for this run.

```json
-> { "execute": "set-budget-mode", "arguments": { "exact": true } }
<- { "return": { "totalbudget": 0, "currenttime": 0, "exact": true } }
```

### The loop, in full

```text
set-budget-mode   (optional; the config usually decides)
loop:
    run-for(dt)   ->  RESUME, immediate reply, guest runs
                  ->  STOP
    query-budget  ->  where the guest actually got to
```

---

## Python client reference

`utils/fastdyn_cosim.py` is dependency-free and usable as a library or a CLI.

### `BudgetMaster(qmp_socket, connect_timeout=15.0, slice_timeout=60.0, exact_stop=False)`

| Argument | Meaning |
| --- | --- |
| `qmp_socket` | Path to the guest's QMP unix socket. |
| `connect_timeout` | Seconds to wait for the socket to appear. |
| `slice_timeout` | Seconds to wait for each slice's `STOP`. |
| `exact_stop` | Request exact-stop mode on connect. |

Use it as a context manager, or call `connect()` and `close()` yourself.

| Member | Purpose |
| --- | --- |
| `run_slice(ns) -> int` | Grant a slice, block until the guest halts, return the virtual time reached. |
| `grant(ns) -> int` | Grant without waiting; returns the new deadline. For driving several guests concurrently. |
| `wait_for_slice() -> int` | Block until the guest halts; returns the virtual time reached. |
| `time_ns` | Virtual time reached, refreshed after every wait. |
| `budget_ns` | Cumulative deadline. |
| `exact_stop` | The mode actually in effect, as reported by the guest. |
| `status` | The guest's run state (`prelaunch`, `running`, `paused`). |

`run_slice()` is `grant()` followed by `wait_for_slice()`. Granting to every
guest before waiting on any of them is what makes lockstep possible.

`CosimError` is raised with an actionable message when the socket never
appears, the guest shuts down mid-slice, or no `STOP` arrives in time.

### CLI

```bash
utils/fastdyn_cosim.py <qmp-socket> [--slice-ms N] [--slices N] [--exact]
                                    [--slice-timeout SECONDS]

utils/cosim_lockstep.py [--config TOML] [--instances N] [--slice-ms N]
                        [--slices N] [--exact] [--work-dir DIR]
```

---

## Semantics you must respect

### Exact-stop mode removes the overshoot

By default the guest halts at the first translation-block boundary at or after
its deadline. Exact mode halts it precisely, so `currenttime == totalbudget`
after every slice.

**Why the default overshoots.** `budget_exhausted()` calls
`qemu_system_vmstop_request()`, which only raises a flag; the main loop pauses
the VM when it next checks. Under icount the vCPU keeps retiring instructions
until then, and every instruction advances virtual time. The deadline
*detection* is already exact — `totalbudget` tracks the sum of grants with no
drift — so the imprecision is entirely in delivering the stop.

**What exact mode does.** It calls `vm_stop()` from the deadline timer instead.
That timer runs on the main loop with icount having already stopped the vCPU
precisely at the deadline, so pausing there leaves virtual time exactly on
`total_budget`. It is taken only on the main loop, and only with the budget
lock released — `vm_stop()` pauses every vCPU, and a vCPU blocked on that lock
would deadlock. From a vCPU thread the mode falls back to the async request.

The overshoot is a **fixed cost per stop**, not a percentage: roughly 150 µs,
about 4700 instructions at `shift = 5`. So the relative error is set entirely
by slice size — 0.7% of a 20 ms slice, 28% of a 500 µs one — which is why the
default put a floor under the usable quantum.

Zero overshoot holds at every slice size tested (100 µs, 500 µs, 2 ms, 20 ms).
The wall-clock cost is not measurable: about 420 µs per slice in both modes on
the demo, dominated by the QMP round trip rather than the pause. Nothing in the
TCG hot path changes.

### Slices overshoot by default, and that is absorbed

Overshoot does **not** accumulate. When the deadline is passed,
`budget_exhausted()` snaps the cumulative budget forward to the time actually
reached, so the next slice is shortened to compensate. Cumulative virtual time
tracks what you granted; individual boundaries do not.

Always reconcile against the reported `currenttime`, never against the sum of
what you asked for.

### Guest execution is deterministic; observation instants are not (by default)

Running the same slice sequence twice produces **byte-identical guest output**,
but with exact-stop mode off the individual stop points differ by roughly
±150 µs between runs, because the pause is requested from a virtual-clock timer
and serviced by the main loop.

This is safe: a paused guest computes nothing, so where it pauses cannot change
what it computes. What varies is *when the master is allowed to look*. For
loosely coupled exchange at millisecond slices this is unimportant. For tight
coupling that needs exact, repeatable exchange instants, enable exact-stop
mode, which makes stop points both exact and reproducible.

### One budget, monotonically increasing

`total_budget` only ever moves forward. There is no rewind and no reset, which
matches the co-simulation contract but means a master cannot replay a slice.

---

## Debugging

**Trace every budget event.** Off by default because it fires on every slice:

```bash
FASTDYN_BUDGET_DEBUG=1 fastdyn run -c configs/cosim_slave.toml -o fastdyn_work_cosim
```

```text
[budget] resuming until 20000000 ns
[budget] stopping vm at 20000000 ns (exact)
```

**Common failures.**

| Symptom | Cause |
| --- | --- |
| `no QMP socket at … after 15s` | The guest is not running, `qmp_socket` is unset, or the path differs between config and master. |
| `no STOP event within Ns` | The slice is larger than the guest can execute, or the guest is blocked. Check the guest log. |
| `guest shut down before its budget was exhausted` | The firmware finished inside this slice. The bundled demo firmware runs continuously and will not do this; a firmware with a bounded main will. |
| `Could not load plugin …: undefined symbol: qemu_plugin_set_budget_exact_stop` | QEMU was built without the patch, or the plugin was built against newer headers than QEMU. Rebuild both. |
| Guest never pauses; `run-for` seems ignored | `stop_on_start` is missing, so the guest free-ran past the deadline before the first grant. |

**Confirm which mode is live** rather than assuming — `query-budget` reports
`exact`, and both CLIs print it on connect.

---

## What was fixed

The budget mechanism existed in the QEMU fork but was not usable as an
interface. `patches/qemu-fastdyn-plugin-icount.patch` now also carries:

| Problem | Fix |
| --- | --- |
| `printf` on every budget event, swamping a master that steps in small slices | Routed through `budget_debug()`, silent unless `FASTDYN_BUDGET_DEBUG=1` |
| `if (!budget_init) init()` ran from both the QMP main-loop thread and a vCPU thread, so the initializer could run twice and leave them with different mutexes | `budget_system_ensure_init()` using `g_once_init_enter`/`g_once_init_leave` |
| `qmp_run_for()` read `total_budget` unlocked after invoking the callback that modifies it | Re-takes the lock for the read |
| A master had no way to learn where the guest actually stopped | `currenttime` added to `Budget`; new `query-budget` command |
| The guest always halted past its deadline, putting a floor on the usable quantum and ruling out algebraic coupling | Opt-in exact stop, via `[Machine] exact_budget_stop` or `set-budget-mode` |

---

## Limitations

- **No FastDyn-side master.** `utils/fastdyn_cosim.py` and
  `utils/cosim_lockstep.py` are clients; nothing in `src/fastdyn` drives slave
  mode, and `fastdyn swarm` does not use it.
- **Stop points jitter** unless exact-stop mode is enabled.
- **Slices cannot be replayed or rewound.**
- **A rare unexplained stall.** Twice during instrumented default-mode runs a
  slice produced no `STOP` and the master timed out. It did not reproduce in
  ten controlled attempts, and exact mode does not use that code path. If you
  hit it with a reproducer, it is worth recording.
- **`start_budgeting` drives the VM from a vCPU thread**, as described above.
  That path is unchanged from the original implementation and has not been
  audited.

---

## Files

| Path | Role |
| --- | --- |
| `patches/qemu-fastdyn-plugin-icount.patch` | The budget interface and its fixes |
| `utils/fastdyn_cosim.py` | Master client library and CLI |
| `utils/cosim_lockstep.py` | Multi-instance lockstep example |
| `configs/cosim_slave.toml` | Slave-mode demonstration configuration |
| `configs/world_rlc_observer_slave.toml` | Slave mode plus world_model's web observer |
| `tests/integration/run_cosim_slave_smoke.sh` | End-to-end verification of all three behaviours |
| `tests/unit/test_fastdyn_cosim.py` | Protocol tests against a scripted fake QMP server |
| `core/core.c` | Consumes `exact_budget_stop` and calls the plugin API |
| `src/fastdyn/toml_parser.py`, `targets/qemu_target.py` | Read and forward the configuration key |

See [CoSimulationOverview.md](CoSimulationOverview.md) for how this fits with
the world_model physics plugin, and [WorldPlugin.md](WorldPlugin.md) for the
RLC model the demonstration configuration drives.
