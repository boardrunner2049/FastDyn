# Flight-controller simulation (ArduPilot + FMI v3)

FastDyn runs ArduPilot firmware (ArduCopter, ArduRover, ArduPlane) in patched
QEMU against a Rumoca-generated FMI v3 vehicle plant, with mission automation,
the MAVCesium web view, and parallel campaigns. Building FastDyn itself is
covered in the [README](../README.md) and [Building.md](Building.md).

## Quick start: ArduCopter FMUv3 + MAVCesium

On Ubuntu 24.04, this is the shortest path to the Rumoca FMI v3 quadrotor
plant, ArduCopter running in patched QEMU, mission automation, and the
MAVCesium web view:

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake device-tree-compiler git libexpat1-dev libfdt-dev \
  libglib2.0-dev libpixman-1-dev meson ninja-build pkg-config python3-venv \
  zlib1g-dev

# Rumoca and OptiFuzz use Rust. Skip this if cargo is already on PATH.
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"

source ./setup.sh --build-qemu --with-rumoca
fastdyn run -c configs/copter462.toml
```

The first setup run builds the pinned Rumoca toolchain, builds the pinned
patched QEMU fork, applies FastDyn's QEMU icount/plugin compatibility patch,
builds `build/libfastdyn.so`, and creates QEMU RAM backing files. The first
`fastdyn run` builds the FMI v3 FMU if it is missing or stale.

FastDyn prints the MAVCesium URL during startup:

```text
http://127.0.0.1:5000/mavcesium/
```

The default mission uses `configs/copter462.toml`, starts on the KLAF tarmac,
runs with a 1 ms board tick, drives QEMU from instruction-counted simulation
time, uploads the ArduCopter mission, flies it, and exits after final landing.

## Running the vehicle configs

We recommend running every `fastdyn` command from the FastDyn repository root. The `cmsis-svd-data` submodule is auto-fetched into `third_party/common/cmsis-svd-data/` by `make` and resolved automatically when you pass `-b <board>`.

Configs may include an `[FMU]` table. When `[FMU] auto_build = true`,
`fastdyn run -c <config>` builds the selected FMU when it is missing or stale,
using the pinned Rumoca and `modelica_models` submodules. The provided
Courbet configs build FastDyn-owned ArduPilot wrapper models:
`FastDyn.Copter`, `FastDyn.Rover`, and `FastDyn.Plane`.
Those wrappers live directly in `modelica/FastDyn/` and inherit
the reusable base plants from `third_party/common/modelica_models/RigidBody`.
Keep vehicle-generic rigid-body dynamics in `modelica_models`; keep
FastDyn-specific sensors, firmware interfaces, missions, ports, helper
processes, and parameter overrides in this repository. Switch configured FMUs
with `active = "<name>"` or `fastdyn run -c <config> --fmu <name>`.
If the pinned FMU toolchain is not available, FastDyn reports the missing path
and recommends `source ./setup.sh --with-rumoca`. For artifact generation
without running QEMU, use `./utils/build_fmi3_fmu.py`.

The same TOML can also start helper processes for a run. Use `[Rumoca]` for an
optional standalone `rumoca lockstep run` webviewer and `[Run.processes.<name>]`
for other commands such as MAVProxy or mission automation scripts. The
ArduCopter FMUv3 config uses the FMU plant in the QEMU plugin and starts
MAVProxy with MAVCesium plus the mission helper by default. It also runs QEMU
with instruction-counted virtual time so the mission is driven by the simulation
clock and can run faster than realtime:

```toml
[Machine]
icount = { shift = 5, sleep = false, align = false }
timer_irq_period_ns = 1000000 # 1 ms board tick; keep this for fidelity runs.

[FMU.models.quadrotor.parameters]
# Purdue University Airport (KLAF) tarmac start point.
lat0 = 40.414929
lon0 = -86.932387
ground_alt_wgs84 = 149.0

# Match the active Gazebo gs_drone ArduPilot PWM endpoints. The FMU maps this
# linearly onto aerodynamic motor speed and then applies explicit first-order
# motor lag.
pwm_min = 1100.0
pwm_max = 1900.0
omega_max = 1300.0

[Rumoca]
enabled = false
config = "third_party/common/rumoca/examples/quadrotor_sil/quadrotor_standby.toml"

[Rumoca.webviewer]
http_port = 8080
ws_port = 8081
scene = "third_party/common/rumoca/examples/quadrotor_sil/quadrotor_scene.js"

[Run.profiling]
timing = true
timing_echo = true
python = false
perf = "off" # off | stat | record
perf_frequency_hz = 99
fmu = true

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
start_delay_sec = 0
terminate_run_on_exit = true
command = [
    "python3",
    "virtuals/physics/flight_controllers/courbet/mavlink/mav_command_and_control.py",
    "--connect",
    "udpin:127.0.0.1:${FASTDYN_MAVLINK_GCS_PORT:-14552}",
    "--monitor-sec",
    "180",
    "virtuals/physics/flight_controllers/courbet/mavlink/copter_init.param",
    "virtuals/physics/flight_controllers/courbet/mavlink/copter_mission.waypoints",
]
```

Then run the normal command:

```bash
fastdyn run -c configs/copter462.toml
```

The same setup also includes ArduRover and ArduPlane FMUv3 configs using the
shared templates:

```bash
fastdyn run -c configs/rover462.toml
fastdyn run -c configs/plane462.toml
```

FastDyn prints the clickable local MAVCesium URL:

```text
http://127.0.0.1:5000/mavcesium/
```

The run also writes startup and mission timing events to
`fastdyn_work/fastdyn_timing.jsonl`. Summarize the slow phases with:

```bash
fastdyn timing-summary fastdyn_work/fastdyn_timing.jsonl
```

When `[Run.profiling] python = true`, Python helper profiles are written under
`fastdyn_work/profiles/*.cprofile`. When `perf = "stat"` or `"record"`, QEMU is
wrapped with Linux `perf` if the host permits it; otherwise FastDyn prints a
warning and runs without the perf wrapper. Keep `perf = "off"` and
`python = false` for lowest-overhead fuzzing runs once the bottlenecks are known.

Use `--no-run-processes` to ignore `[Rumoca]` and `[Run.processes]` for a single
run. Set `[Rumoca] enabled = true` if you also want the separate Rumoca web
viewer for standalone lockstep experiments.

## Parallel campaigns

Use `fastdyn swarm` to run many complete FastDyn instances on one machine with
isolated work directories, QEMU monitor/QMP sockets, MAVLink ports, MAVCesium
ports, optional Rumoca webviewer ports, GDB ports, logs, and QEMU RAM backing
files:

```bash
fastdyn swarm -c configs/copter462.toml -n 20 -o out/swarm/copter --base-port 15000
```

Each worker runs the same TOML, but the supervisor injects environment
overrides. Worker 0 uses MAVCesium at
`http://127.0.0.1:15003/mavcesium/`; worker 1 uses
`http://127.0.0.1:15023/mavcesium/`, and so on with the default
`--port-stride 20`. Logs are written to `out/swarm/copter/logs/worker-000.log`,
`logs/worker-001.log`, etc. Use `--dry-run` to print the full port plan
without launching QEMU:

```bash
fastdyn swarm -c configs/copter462.toml -n 20 -o out/swarm/copter --dry-run
```

CI launches real two-worker swarms for ArduCopter, ArduRover, and ArduPlane
long enough to verify both workers load the FMU backend, use the 1 ms board
tick, and print independent MAVCesium URLs:

```bash
FASTDYN_SWARM_INSTANCES=2 tests/integration/courbet_swarm_smoke.sh
```

The workers are isolated by default, which is the right baseline for fuzzing
and reproducibility. A communication experiment can intentionally connect them
through a shared MAVLink/router/radio model later; avoid accidental shared ports
or shared RAM files because those create nondeterministic coupling.

## CI

GitHub Actions runs the same high-fidelity path in
`.github/workflows/courbet-mission.yml`: setup, patched QEMU build, Rumoca FMI
v3 FMU generation, FastDyn plugin build, OptiFuzz FMUv3 dry-runs for copter,
rover, and plane, OptiFuzz two-worker swarm dry-runs for all three vehicles,
MAVCesium helper startup, real two-worker swarm launch smokes for all three
vehicles, ArduRover and ArduPlane FMUv3 launch smoke, and the ArduCopter mission
upload, takeoff, waypoint progression, and final landing.
The full mission integration script is `tests/integration/courbet_mission_smoke.sh`;
locally, run it after setup with:

```bash
tests/integration/courbet_mission_smoke.sh
```
