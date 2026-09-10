# Courbet ArduPilot Firmware In FastDyn

This directory contains the ArduPilot firmware images, virtual instruction
configuration, MAVLink helpers, missions, and legacy Gazebo assets used by the
FastDyn/Courbet vehicle tests.

The maintained default path is no longer a manual Gazebo launch. Use the
repository root `setup.sh` and the FastDyn TOML configs:

```bash
source ./setup.sh --build-qemu
fastdyn run -c configs/copter462.toml
```

## Maintained Vehicle Configs

From the FastDyn repository root:

```bash
fastdyn run -c configs/copter462.toml
fastdyn run -c configs/rover462.toml
```

These configs run the real ArduPilot firmware in patched QEMU and connect it to
a Rumoca-generated FMI v3 plant:

- `configs/copter462.toml` uses `FastDyn.Copter`.
- `configs/rover462.toml` uses `FastDyn.Rover`.
- `configs/plane462.toml` describes `FastDyn.Plane`; its three-wheel contact
  events currently prevent FMI export, so it is not yet a runnable example.

The FastDyn wrapper models inherit reusable base plants from
`third_party/common/modelica_models/Vehicles/Templates` and define the
ArduPilot-facing sensor and actuator variables in this repository.

The FMU start point and default missions are aligned to the KLAF/Purdue tarmac:

```text
40.414929, -86.932387
ground_alt_wgs84 = 149.0
pwm_min = 1100.0
pwm_max = 1900.0
omega_max = 1300.0
```

The copter FMU keeps its first-order motor response, but its PWM input mapping
matches the active Gazebo `gs_drone` ArduPilot PWM endpoints: `1100..1900` PWM
maps linearly to aerodynamic motor speed.

## MAVLink Helpers

MAVLink helper scripts live in `mavlink/`.

Important files:

- `fastdyn_cesium.py`: MAVProxy module that starts MAVCesium and applies the
  configured terrain/display altitude offset.
- `mav_command_and_control.py`: mission upload, arming, mode switch, monitoring,
  and OptiFuzz mutation parameter injection.
- `mav_health_check.py`: short smoke-test helper used for rover and plane.
- `copter_init.param`: ArduCopter parameters loaded before the mission.
- `copter_mission.waypoints`: full ArduCopter takeoff/waypoint/land mission.
- `rover_rectangle.txt`: short KLAF rover mission.
- `plane_circle_point.txt`: KLAF fixed-wing mission.

The ArduCopter config starts MAVProxy/MAVCesium and the mission helper by
default. Rover also starts a complete waypoint mission helper.

FastDyn prints the viewer URL during startup:

```text
MAVCesium web viewer: open http://127.0.0.1:5000/mavcesium/
```

## Timing And Fidelity

QEMU is run with instruction-counted virtual time and a 1 ms board tick:

```toml
[Machine]
icount = { shift = 5, sleep = false, align = false }
timer_irq_period_ns = 1000000
```

The firmware observes the simulated board clock. The FMU backend publishes
state and advances synchronously from QEMU timer ticks, so host wall-clock speed
can vary without changing firmware-visible time.

## Parallel Runs

Use `fastdyn swarm` for many isolated instances:

```bash
fastdyn swarm -c configs/copter462.toml -n 20 -o out/swarm/copter --base-port 15000
```

Worker 0 uses MAVCesium at `http://127.0.0.1:15003/mavcesium/`; worker 1 uses
`http://127.0.0.1:15023/mavcesium/` with the default `--port-stride 20`.
Per-worker logs are written under the swarm root, for example
`out/swarm/copter/logs/worker-000.log`.

Run a two-worker smoke test:

```bash
FASTDYN_SWARM_CONFIG=configs/copter462.toml \
FASTDYN_SWARM_INSTANCES=2 \
tests/integration/courbet_swarm_smoke.sh
```

## Integration Tests

After setup, these scripts exercise the maintained path:

```bash
# Rover waypoint integration test.
FASTDYN_COURBET_CONFIG=configs/rover462.toml \
FASTDYN_COURBET_LABEL=ardurover \
FASTDYN_COURBET_MIN_ALT_M=0 FASTDYN_COURBET_MIN_ITEM=4 \
tests/integration/courbet_mission_test.sh

# Full ArduCopter mission.
tests/integration/courbet_mission_test.sh

# Two-worker swarm launch smoke.
tests/integration/courbet_swarm_smoke.sh
```

GitHub Actions runs these paths in `.github/workflows/ci.yml`.

## OptiFuzz

OptiFuzz can launch this same FMUv3 path directly:

```bash
cd virtuals/fuzzer/libafl_phi
FASTDYN_OPTIFUZZ_SMOKE=1 FASTDYN_OPTIFUZZ_DRY_RUN=1 cargo run --bin baby_fuzzer
```

For real campaigns, remove `FASTDYN_OPTIFUZZ_DRY_RUN`. Use:

```bash
FASTDYN_OPTIFUZZ_VEHICLE=plane
FASTDYN_OPTIFUZZ_CONFIG=configs/plane462.toml
FASTDYN_OPTIFUZZ_MISSION_FILE=virtuals/physics/flight_controllers/courbet/mavlink/plane_circle_point.txt
```

Set `FASTDYN_OPTIFUZZ_SWARM_INSTANCES=2` or higher to use the FastDyn swarm
runner for each fuzzer execution.

## Legacy Gazebo Path

The old Courbet Gazebo assets and scripts remain under `gazebo/` and
`third_party/courbet_deps/SITL_Models`. They are useful for reproducing older
experiments, but the default FastDyn docs, setup script, CI, and fuzzer wiring
now target the Rumoca FMI v3 backend. To force OptiFuzz onto the legacy path,
set:

```bash
FASTDYN_OPTIFUZZ_BACKEND=gazebo
```

Manual Gazebo setup requires Gazebo Harmonic, ArduPilot Gazebo plugins, the
legacy Courbet services, and the old MAVProxy scripts. Prefer the FMUv3 configs
unless you are intentionally comparing against that legacy stack.

## Flight Logs

ArduPilot logs can be collected by creating the expected log directories before
a run:

```bash
mkdir -p virtuals/physics/flight_controllers/courbet/flight_logs/@ROMFS
mkdir -p virtuals/physics/flight_controllers/courbet/flight_logs/@SYS
mkdir -p virtuals/physics/flight_controllers/courbet/flight_logs/APM/LOGS
```

After a simulation, open the generated `.bin` log in UAV Log Viewer if the
firmware wrote one.
