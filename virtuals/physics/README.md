# FastDyn Physics

FastDyn physics connects rehosted firmware running in patched QEMU to a plant
model. The current high-fidelity vehicle path uses Rumoca-generated FMI v3 FMUs
inside the FastDyn QEMU plugin. Gazebo support remains in the tree for legacy
Courbet experiments, but it is no longer the default setup for copter, rover, or
plane fuzzing.

## Current Vehicle Path

Use the repository root setup script:

```bash
source ./setup.sh --build-qemu --with-rumoca
```

Then run one of the maintained FMI v3 vehicle configs:

```bash
fastdyn run -c configs/copter462.toml
fastdyn run -c configs/rover462.toml
fastdyn run -c configs/plane462.toml
```

The configs:

- build the selected FMU from `third_party/common/modelica_models` with Rumoca
  when it is missing or stale,
- run the real ArduPilot firmware image in QEMU,
- drive QEMU with instruction-counted simulation time,
- publish a 1 ms board tick to the firmware,
- start MAVProxy with the FastDyn MAVCesium adapter, and
- for ArduCopter, upload and fly the KLAF/Purdue mission by default.

FastDyn prints the web viewer URL during startup:

```text
MAVCesium web viewer: open http://127.0.0.1:5000/mavcesium/
```

## Modelica And FMU Layout

Reusable Modelica dynamics live in the `modelica_models` submodule:

- `LieGroup/`: quaternion and SO(3) helpers.
- `RigidBody/`: shared rigid-body dynamics.
- `RigidBody/Examples/QuadrotorSIL.mo`
- `RigidBody/Examples/RoverPlant.mo`
- `RigidBody/Examples/FixedWingPlant.mo`

Vehicle-specific Modelica wrappers live in `modelica/FastDyn`. Those
wrappers add the ArduPilot-facing sensor and actuator variables, including the
geodetic conversion from local north/east position to GPS latitude/longitude.
Firmware binaries, mission files, MAVLink helper commands, port defaults,
profiling options, and Modelica parameter overrides live in the FastDyn TOML
configs.

### Copter and Rover kinematics

The local origin is the ground plane at `lat0`, `lon0`, `ground_alt_wgs84`.
North, east and down are positive world axes. Forward, right and down are
positive body axes. Attitude uses a ZYX yaw-pitch-roll rotation from FRD into
NED, with the exported components ordered roll, pitch, yaw. Positive roll is
right wing down, positive pitch is nose up, and positive yaw turns north toward east.

| FMU output | OptiFuzz CSV columns (in order) | Units |
|---|---|---|
| `rpy` | `roll_rad`, `pitch_rad`, `yaw_rad` | rad |
| `omega_frd` | `omega_frd_x`, `omega_frd_y`, `omega_frd_z` | rad/s about FRD axes |
| `alpha_frd` | `alpha_frd_x`, `alpha_frd_y`, `alpha_frd_z` | rad/s² about FRD axes |
| `pos_gps` | `lat`, `lon`, `z` | deg, deg, WGS84 ellipsoid height in m |
| `pos_ned` | `pos_n_m`, `pos_e_m`, `pos_d_m` | m |
| `vel_ned` | `gps_vel_n`, `gps_vel_e`, `gps_vel_d` | m/s |
| `accel_ned` | `accel_n_mps2`, `accel_e_mps2`, `accel_d_mps2` | m/s² |
| `vel_frd` | `vel_frd_x`, `vel_frd_y`, `vel_frd_z` | m/s |
| `accel_frd` | `accel_frd_x`, `accel_frd_y`, `accel_frd_z` | m/s² |
| `rotation` | `rotation_11` through `rotation_33`, row-major | dimensionless |
| `mag` | `mag_x`, `mag_y`, `mag_z` | Gauss |
| barometer | `baro_altitude_m`, `baro_pressure_pa`, `baro_temperature_c`, `baro_climb_rate_mps` | m, Pa, °C, m/s |
| `motor_cmd` (Copter) | `motor_cmd_1` through `motor_cmd_4` in PWM channel order | rad/s |
| `motor_cmd` (Rover) | the same four columns hold steering, throttle, forward speed and FRD yaw rate | normalized, normalized, m/s, rad/s |

`rotation * vel_frd = vel_ned` and `rotation * accel_frd = accel_ned`.
The virtual IMU reads body specific force from `plant.accel` and angular
velocity from `omega_frd`. At level rest, `accel_frd_z` is zero and the IMU
column `z_accel` is approximately −9.8 m/s². In level free fall the readings
are +9.8 and zero, respectively. The accelerometer, gyro, and magnetometer
are bias-free. GPS and barometer retain their bias parameters. The first CSV
column, `sim_time`, is in milliseconds.

Rover uses PWM channel 1 for steering and channel 3 for throttle. With neutral
PWM at 1500 µs, higher throttle drives forward. Higher steering PWM turns the
nose left (negative FRD yaw). The plant scales yaw rate with absolute forward
speed, so reversing throttle preserves the yaw direction for the same steering
input and reverses the lateral displacement from the initial heading.

When reading the packaged FMU in another FMI client at time zero, set the
initial PWM, evaluate `accel_ned` once in initialization mode, then exit
initialization mode. The FastDyn backend does this so the pinned Rumoca
runtime propagates the initial actuator input to the first trace sample.

These commands enable tracing on an existing FMU plugin build and capture both
scenarios from the repository root.

```bash
source fastdyn-env/bin/activate
python utils/build_fmi3_fmu.py --config configs/rover462.toml --skip-submodules
meson configure build -Denable_trace=true && meson compile -C build
mkdir -p out/traces
FASTDYN_TRACE_FILE="$PWD/out/traces/copter.csv" fastdyn run -c configs/copter462.toml
FASTDYN_TRACE_FILE="$PWD/out/traces/rover.csv" fastdyn run -c configs/rover462.toml
python -m pytest -q tests/integration/test_fmu_kinematics.py
```

## Time Semantics

For fidelity, the firmware is driven by the simulated board clock, not host wall
time. The FMU backend advances when QEMU timer ticks ask for a later simulation
time. QEMU catches up to that state using instruction-counted execution. Keep:

```toml
[Machine]
icount = { shift = 5, sleep = false, align = false }
timer_irq_period_ns = 1000000
```

`timer_irq_period_ns = 1000000` gives ArduPilot a 1 ms tick. Tuning `icount`
can change performance, but changing the board tick changes the firmware timing
contract and should be treated as a fidelity-affecting change.

## Missions And MAVCesium

The current mission assets are under:

```text
virtuals/physics/flight_controllers/courbet/mavlink/
```

Important files:

- `copter_init.param`
- `copter_mission.waypoints`
- `rover_rectangle.txt`
- `plane_circle_point.txt`
- `mav_command_and_control.py`
- `mav_health_check.py`
- `fastdyn_cesium.py`

The copter, rover, and plane mission files are aligned to the KLAF/Purdue
tarmac start point used by the FMU configs:

```text
lat0 = 40.414929
lon0 = -86.932387
ground_alt_wgs84 = 149.0
pwm_min = 1100.0
pwm_max = 1900.0
omega_max = 1300.0
```

FastDyn uses WGS84 ellipsoid altitude throughout the FMU, MAVLink, mission, and
MAVCesium path so the vehicle appears on the Cesium terrain without display-only
datum shifts.

The copter actuator input mapping follows the active Gazebo `gs_drone`
ArduPilot PWM endpoints: `1100..1900` maps linearly to aerodynamic motor speed.
The FMU then applies the explicit first-order motor response.

## Parallel Campaigns

Use `fastdyn swarm` to run many isolated physics-backed firmware instances:

```bash
fastdyn swarm -c configs/copter462.toml -n 20 -o out/swarm/copter --base-port 15000
```

Each worker receives separate QEMU RAM backing files, QMP socket, monitor port,
MAVLink ports, MAVCesium port, Rumoca viewer ports, and logs. Use `--dry-run`
to inspect the plan:

```bash
fastdyn swarm -c configs/copter462.toml -n 20 -o out/swarm/copter --dry-run
```

This is the preferred local scaling path for fuzzing campaigns. Workers are
isolated by default; intentionally shared radio/MAVLink behavior should be added
explicitly when a swarm communication experiment needs it.

## OptiFuzz

OptiFuzz/CP-Explore now defaults to the FMUv3 FastDyn backend for `copter`,
`rover`, and `plane`.

```bash
source ./setup.sh
cd virtuals/fuzzer/libafl_phi
cargo run --bin baby_fuzzer
```

Select a different vehicle with environment variables:

```bash
FASTDYN_OPTIFUZZ_VEHICLE=rover \
FASTDYN_OPTIFUZZ_CONFIG=configs/rover462.toml \
FASTDYN_OPTIFUZZ_MISSION_FILE=virtuals/physics/flight_controllers/courbet/mavlink/rover_rectangle.txt \
cargo run --bin baby_fuzzer
```

Set `FASTDYN_OPTIFUZZ_SWARM_INSTANCES=2` or higher to use `fastdyn swarm`.
Set `FASTDYN_OPTIFUZZ_BACKEND=gazebo` only when running the legacy Gazebo path.

## CI Coverage

`.github/workflows/courbet-mission.yml` exercises the maintained physics path:

- setup and patched QEMU build,
- Rumoca FMI v3 FMU generation,
- OptiFuzz FMUv3 dry-runs for copter, rover, and plane,
- OptiFuzz two-worker swarm dry-runs for all three vehicles,
- real two-worker swarm launch smoke tests for all three vehicles,
- ArduRover and ArduPlane FMUv3 launch smoke tests, and
- a full ArduCopter mission through takeoff, waypoint progression, and landing.

Run the same scripts locally after setup:

```bash
tests/integration/courbet_fmu_vehicle_smoke.sh
tests/integration/courbet_swarm_smoke.sh
tests/integration/courbet_mission_smoke.sh
```

## Backend Interface

Physics backends implement the `phy_backend_t` interface documented in
`virtuals/physics/physics_engines/README.md`. The active high-fidelity backend
is the FMU backend under `virtuals/physics/physics_engines/fmu/`; the Gazebo
backend is retained under `virtuals/physics/physics_engines/gazebo/` for older
experiments.
