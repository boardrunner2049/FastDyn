# FastDyn TOML Configuration

FastDyn runs firmware from a single TOML file. The file describes the QEMU
machine, memory backing, firmware image, FastDyn plugin settings, optional FMI
v3 plants, runtime helper processes, and profiling options. The same TOML is
used by `fastdyn run`, `fastdyn loop`, the smoke tests, and `fastdyn swarm`.

The maintained vehicle examples are:

- `configs/copter462.toml`: ArduCopter 4.6.2 with a Rumoca FMI v3 quadrotor.
- `configs/rover462.toml`: ArduRover 4.6.2 with a Rumoca FMI v3 rover.
- `configs/plane462.toml`: ArduPlane 4.6.2 with a Rumoca FMI v3 fixed-wing model.

Run a config with:

```bash
source ./setup.sh --build-qemu --with-rumoca
fastdyn run -c configs/copter462.toml
```

## Path And Environment Expansion

The files a configuration names (the firmware `binary`, `hardware_trace`, a
device model's `scroll` and a slave's `device_scroll`) are looked up next to
the configuration file when given as relative paths, so an example folder
with its configuration, firmware and models works from any directory; a path
that is not there is taken relative to the current directory. Runtime helper
strings support `${NAME:-default}` expansion, so a config can provide defaults
while `fastdyn swarm` injects per-worker ports:

```toml
"--out=udpout:127.0.0.1:${FASTDYN_MAVLINK_GCS_PORT:-14552}"
```

Important injected environment variables are:

- `FASTDYN_WORK_DIR`: current run work directory (the device models write the
  MMIO trace `io.log` there).
- `FASTDYN_CONFIG`: absolute config path.
- `FASTDYN_MONITOR_PORT`: QEMU monitor TCP port.
- `FASTDYN_MAVLINK_FIRMWARE_PORT`: firmware-facing MAVLink UDP port.
- `FASTDYN_MAVLINK_GCS_PORT`: GCS/helper-facing MAVLink UDP port.
- `FASTDYN_MAVCESIUM_PORT`: MAVCesium HTTP port.
- `FASTDYN_RUMOCA_HTTP_PORT` and `FASTDYN_RUMOCA_WS_PORT`: standalone Rumoca viewer ports.
- `FASTDYN_QEMU_MEMORY_DIR`: per-run RAM backing directory.
- `FASTDYN_QMP_SOCKET`: per-run QMP socket path.

## Machine

`[Machine]` controls QEMU and global board timing.

`qemu_path` is optional. When it is not set, FastDyn uses, in order:
the `FASTDYN_QEMU` environment variable; the QEMU this FastDyn build was
compiled against (recorded in `build/fastdyn_build.json` next to
`libfastdyn.so`, normally the `third_party/common/qemu` submodule); then
`qemu-system-<arch>` on `PATH`. Set it only to use a different QEMU.

```toml
[Machine]
platform = "STM32F427"
# qemu_path = "../qemu/build/qemu-system-arm"   # optional, see above
monitor_port = 5555
qmp_socket = "/tmp/qmp.sock"
log_file = "qemu.log"
log_options = "none"
icount = { shift = 5, sleep = false, align = false }
timer_irq_period_ns = 1000000
semihosting = true
semihosting_config = "enable=on,target=native"
coverage = false
fuzzing = false
fuzzing_schema = "path/to/fuzzing-schema.json"
edge_coverage = false
print_command = false
exit_timeout_ms = 5000
```

`qmp_socket` is QEMU's control socket. Left at `/tmp/qmp.sock` (the default),
each run gets its own, `<work_dir>/qmp.sock`; when that path is too long for a
Unix socket (107 bytes), it goes to a short per-user folder instead,
`fastdyn-<uid>/` under `$XDG_RUNTIME_DIR`, the temporary directory or `/tmp`.
Set another path to connect to the socket yourself (e.g. a co-simulation
master).

When a run is stopped (Ctrl-C, a helper process ending, or a caller such as
BoardRunner ending a timed run), QEMU gets SIGINT and exits normally, so
plugins write their results (e.g. `bbl.txt`); it is killed only if it is still
running after `exit_timeout_ms` (default 5000).

To see valid architecture presets and the SVD-backed values accepted by
`[Machine].platform`, use the CLI catalog:

```bash
fastdyn help platforms              # interactive browser in a terminal
fastdyn help platforms STM32
```

The browser first lets you choose a CPU architecture/QEMU target or a CMSIS-SVD
device platform. The generic Cortex-M branch exposes all CPU models supported
by the patched QEMU target through Cortex-M55. The device-platform branch is a
small inline picker through vendor names, catalog directories where available,
product families, and finally the exact platform identifier. It does not
require a search term or fill the terminal.
It redraws in place and disappears after a selection, then prints ready-to-copy
CPU or `platform = "..."` TOML settings. The second command lists every
matching platform value as plain text. Use `--no-browse` for the vendor summary
in a terminal, or `--browse` to force the browser.

For a guided path from the near-empty
[`configs/bare_bones.toml`](../configs/bare_bones.toml) to a complete run,
see [Building a FastDyn Configuration](BuildingAConfig.md).

The same interactive help menu also provides essential run configuration
fragments through `fastdyn help machine`, `fastdyn help memory`, and
`fastdyn help firmware`.

When `edge_coverage = true`, FastDyn also writes cumulative edge coverage to
`edges.txt` alongside `bbl.txt` in the active work directory.

Set `fuzzing = true` only for a fuzzing campaign. It starts the input backend
compiled into the plugin; `coverage = true, fuzzing = false` collects coverage
without publishing or waiting for fuzz inputs.

`fuzzing_schema` is the JSON field schema used by the generic fuzzer.

For high-fidelity ArduPilot/FMU runs, keep `timer_irq_period_ns = 1000000`.
That gives the firmware a 1 ms board tick. With `icount.sleep = false`, QEMU is
advanced by instruction-counted simulation time rather than wall time, which is
the path used for faster-than-realtime campaigns.

## Memory

Memory banks map to QEMU `memory-backend-*` objects. Swarm runs override the RAM
file directory per worker, so workers do not share memory.

Only `memory_size` is required (and `base_address` where the RAM is not at
0). `id` defaults to the bank's name, `memory_type` to `"SRAM"`, `backend` to
`"file"` and `share` to `true`. A file-backed bank without `memory_file` gets
`<work dir>/<id>.ram`. `backend = "ram"` or `"memfd"` keeps the RAM without a
backing file.

```toml
[Memory]

[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size = "512M"
memory_type = "SRAM"
backend = "file"
memory_file = "../qemu/ws/my_m4_ram3"
share = true
prealloc = false

[[Memory.ram1]]
id = "ram1"
index = 1
base_address = "0x30000000"
memory_size = "512K"
memory_type = "SRAM"
backend = "file"
memory_file = "../qemu/ws/my_m4_ram"
share = true
prealloc = false
```

## CPU

`[[CPU.cpu0]]` identifies the firmware, monitor ELF, FastDyn plugin, and virtual
instruction configuration files.

```toml
[CPU]

[[CPU.cpu0]]
arch = "arm"
machine = "cortexm"
cpu = "cortex-m4"
plugin_library = "build/libfastdyn.so"
monitor_elf = "../qemu/ws/monitor.elf"
binary = "virtuals/physics/flight_controllers/courbet/bin/arducopter_v462"
init_nsvtor = "0x08004000"
twintrace = "None"
hardware_trace = "hardware_log/io.log"
existing_config_path = "virtuals/physics/flight_controllers/courbet/ardupilot/copter462/unlabeled_conf"
```

Run modules use FastDyn's generic per-CPU plugin configuration namespace:

```toml
[CPU.cpu0.plugins.introspection]
enabled = true

[CPU.cpu0.plugins.introspection.activity_monitor]
enabled = true
port = 8765
open_browser = true
```

FastDyn preserves each `plugins.<name>` table without interpreting the plugin
name or settings. The selected module receives only its TOML settings, the
uniform FastDyn logger, lifecycle cleanup registration, and a private artifact
folder. It cannot add QEMU command-line options.

### Instruction Modifiers

FastDyn supports inline instruction modifiers under `[[CPU.cpu0.modifiers]]` to patch register states (such as redirecting `PC`/`RIP` execution flow or overriding register values) dynamically when QEMU executes a specific target address.

Virtual instructions use the adjacent `[[CPU.cpu0.virtuals]]` TOML array to
invoke a named FastDyn action when QEMU executes a target address (for example,
raise an IRQ). See [Virtual Instructions and Modifiers](VirtualsAndModifiers.md)
for the complete syntax, built-in virtual registry, argument formats, and the
callback-versus-inline implementation distinction.

```toml
# x86_64 Example
[[CPU.cpu0.modifiers]]
at = "0x18008"
patch = "rip <- 0x18004"

# ARM Example
[[CPU.cpu0.modifiers]]
at = "0x08000210"
patch = "r15 <- 0x08000214"
```

#### Architecture TCG Register Mappings:
In QEMU's TCG translation generator (`update_reg`), target registers map to specific internal TCG slots:
- **ARM 32-bit**: `r15` / `pc` maps to TCG slot **`15`** (`R15` / `PC`). `r13` / `sp` maps to TCG slot **`13`** (`R13` / `SP`).
- **Intel / x86_64**: `rip` / `pc` maps to TCG slot **`16`** (`cpu_eip` / `RIP`). `rsp` / `sp` maps to TCG slot **`4`** (`RSP`).

## FMU

`[FMU]` lets FastDyn build and load Rumoca FMI v3 models directly from Modelica.
The generated FMU is used by the C physics backend in the QEMU plugin.

At startup, FastDyn extracts the selected `.fmu` into an isolated directory
under the run's work directory. It reads the Co-Simulation `modelIdentifier`,
`instantiationToken`, and variable references from that package's
`modelDescription.xml`, then loads `binaries/<platform>/<modelIdentifier>.so`.
UUID-based names require no configuration changes after re-exporting a model.
Unpacked build trees are also supported if they contain a compiled binary;
source-only exports must be compiled before running.

The native plugin receives the resolved `.so` path in `fmu=`, the XML token in
`fmu_token=`, and an optional resource directory in `fmu_resources=`. These
arguments are generated automatically by `fastdyn run`. If FMU initialization
fails, QEMU exits before starting the periodic simulation ticks.

Keep the Modelica model name vehicle-oriented, such as
`FastDyn.Copter`; the FMU is the generated artifact format, not
part of the model identity. FastDyn vehicle models should define the
ArduPilot-facing sensor and actuator variables explicitly while inheriting
generic dynamics from `third_party/common/modelica_models`.

```toml
[FMU]
active = "quadrotor"
auto_build = true

[FMU.models.quadrotor]
model = "FastDyn.Copter"
model_file = "modelica/FastDyn/Copter.mo"
source_roots = ["modelica", "third_party/common/modelica_models"]
output = "out/fmi3/Copter"
build = true
release = false

[FMU.models.quadrotor.parameters]
lat0 = 40.414929
lon0 = -86.932387
ground_alt_wgs84 = 149.0
pwm_min = 1100.0
pwm_max = 1900.0
omega_max = 1300.0
```

Useful controls:

- `active`: selects a model under `[FMU.models]`.
- `auto_build`: rebuilds the FMU when it is missing or stale.
- `source_roots`: Modelica package roots passed to Rumoca.
- `build`: packages an `.fmu` when true; otherwise emits the generated source tree.
- `release`: builds Rumoca with Cargo release mode.
- `[FMU.models.<name>.parameters]`: numeric Modelica parameter overrides.

The copter defaults match the active Gazebo `gs_drone` ArduPilot PWM endpoints:
PWM `1100..1900` maps linearly to aerodynamic motor speed, then the FMU plant
applies its explicit first-order motor lag.

Override selection on the command line with:

```bash
fastdyn run -c configs/copter462.toml --fmu quadrotor
```

## Rumoca Standalone Viewer

`[Rumoca]` starts an optional separate `rumoca lockstep run` process. This is
for standalone lockstep experiments and web viewing of a Rumoca scene. The
normal ArduPilot configs use the FMU through the QEMU plugin and leave this
disabled by default.

```toml
[Rumoca]
enabled = false
config = "third_party/common/rumoca/examples/quadrotor_sil/quadrotor_standby.toml"

[Rumoca.webviewer]
http_port = "${FASTDYN_RUMOCA_HTTP_PORT:-8080}"
ws_port = "${FASTDYN_RUMOCA_WS_PORT:-8081}"
scene = "third_party/common/rumoca/examples/quadrotor_sil/quadrotor_scene.js"
```

When enabled, FastDyn prints a local viewer URL such as
`http://127.0.0.1:8080`.

## Console Bridge Initialization

The PX4 FMUv6C profile initializes its modules automatically through the console
bridge when running `fastdyn run`. Select the NSH command file in `[Run]`:

```toml
[Run]
bridge_boot = "virtuals/physics/flight_controllers/courbet/px4/fmu_v6c/boot.nsh"
```

Relative paths are resolved from the FastDyn repository root. The runner checks
that the selected file exists before replacing the work directory and passes its
absolute path to QEMU as `FASTDYN_BRIDGE_BOOT` for the execution session only.
An existing environment value overrides the TOML (relative environment paths
use the caller's working directory). An explicitly empty value disables boot:

```bash
FASTDYN_BRIDGE_BOOT= fastdyn run -c configs/px4_fmu_v6c_fire_p2.toml --no-run-processes
```

This initializes guest modules through the existing bridge; it does not arm the
vehicle or start a mission. It remains active with `--no-run-processes` because
it is independent of host helpers. `[Run.env]` continues to configure helper
environments, not QEMU. Profiles without `bridge_boot` keep their current behavior.
The PX4 config also enables a separate mission helper that normally arms and flies
the uploaded mission, plus MAVProxy/MAVCesium. Use `--no-run-processes` to run only
guest initialization and the TELEM1/UDP bridge. See the
[PX4 profile guide](../virtuals/physics/flight_controllers/courbet/px4/fmu_v6c/README.md)
for UART mapping, ports, arming behavior and integration checks.

## Runtime Helpers

`[Run]` and `[Run.processes.<name>]` start helper processes next to QEMU. The
current ArduPilot configs use helpers for MAVProxy/MAVCesium and mission or
health monitoring.

```toml
[Run]
cwd = "."
env = { PYTHONPATH = "." }

[Run.processes.mavproxy]
enabled = true
quiet = true
cwd = "virtuals/physics/flight_controllers/courbet/mavlink"
env = { PYTHONPATH = "." }
command = [
    "mavproxy.py",
    "--daemon",
    "--master=udpout:127.0.0.1:${FASTDYN_MAVLINK_FIRMWARE_PORT:-14551}",
    "--out=udpout:127.0.0.1:${FASTDYN_MAVLINK_GCS_PORT:-14552}",
    "--load-module=fastdyn_cesium:{\"port\":${FASTDYN_MAVCESIUM_PORT:-5000}}",
]
ready_message = "MAVCesium web viewer: open http://127.0.0.1:${FASTDYN_MAVCESIUM_PORT:-5000}/mavcesium/"

[Run.processes.mission]
enabled = true
background = true
terminate_run_on_exit = true
command = [
    "python3",
    "virtuals/physics/flight_controllers/courbet/mavlink/mav_command_control_ardupilot.py",
    "--connect",
    "udpin:127.0.0.1:${FASTDYN_MAVLINK_GCS_PORT:-14552}",
    "--monitor-sec",
    "180",
    "virtuals/physics/flight_controllers/courbet/mavlink/copter_init.param",
    "virtuals/physics/flight_controllers/courbet/mavlink/copter_mission.waypoints",
]
```

Process fields:

- `enabled`: include or skip the helper.
- `command`: string or list of strings.
- `cwd`: helper working directory.
- `env`: helper-specific environment.
- `ready_message`: printed immediately after the helper starts.
- `background`: run concurrently with QEMU when true.
- `quiet`: redirect helper stdout/stderr to `/dev/null`.
- `start_delay_sec`: delay helper startup.
- `stop_on_exit`: terminate the helper when FastDyn exits.
- `terminate_run_on_exit`: shut QEMU down when this helper exits.
- `shell`: force shell execution for list commands.

Skip all helpers for one run with:

```bash
fastdyn run -c configs/copter462.toml --no-run-processes
```

## Profiling And Timing

```toml
[Run.profiling]
timing = true
timing_echo = true
python = false
perf = "off" # off | stat | record
perf_frequency_hz = 99
fmu = true
```

Timing events are written to `fastdyn_work/fastdyn_timing.jsonl` and summarized
with:

```bash
fastdyn timing-summary fastdyn_work/fastdyn_timing.jsonl
```

`python = true` profiles Python helper scripts with `cProfile`.
`perf = "stat"` or `"record"` wraps QEMU with Linux `perf` when host permissions
allow it. Keep profilers disabled for lowest-overhead fuzzing campaigns after
you have identified bottlenecks.

## Device Models

`[Device.Models]` registers handler types, and `[Device.<name>]` assigns
address ranges to handlers. This is the traditional FastDyn peripheral model
configuration and is still used alongside the FMU physics backend. A run
without peripheral models leaves out `[Device]` entirely.

```toml
[Device.Models.classic]

[Device.Models.passthrough]
backend = "stlink"

[Device.remaining_space]
ranges = [["0x40000000", "0x400107FF"], ["0x40010C00", "0x40010FFF"]]
irq = [["1", "100"]]
description = "Peripheral ranges not modeled by a specific handler."

[[Device.remaining_space.handlers]]
model = "classic"
enabled = false
```

`irq` lists the interrupts a device's models are told about, as the CPU
reports them: on Cortex-M, exception numbers (device IRQ n is n + 16). Each
entry is a number (`54`, `"0x36"`), a CMSIS-SVD interrupt name (`"USART2"`,
converted for you), or an inclusive `[first, last]` range; for example
`irq = [[0, 20], [21, 25], 41, "USART2"]`. The old string form
`irq = "0-20:21-25"` is no longer accepted.

## Parallel Runs

`fastdyn swarm` runs many isolated copies of one config. Each worker receives
its own work directory, RAM backing directory, MAVLink ports, MAVCesium port,
Rumoca viewer ports, GDB port, and QMP socket.

```bash
fastdyn swarm -c configs/copter462.toml -n 20 -o out/swarm/copter --base-port 15000
```

Use `--dry-run` to inspect the assigned ports without launching QEMU:

```bash
fastdyn swarm -c configs/copter462.toml -n 20 -o out/swarm/copter --dry-run
```

The Courbet Mission workflow's smoke tests run two-worker swarms for copter, rover, and plane to confirm
FMU loading, board timing, MAVCesium URL generation, and port isolation.
