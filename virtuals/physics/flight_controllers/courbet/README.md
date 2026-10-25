# Courbet Autopilot Firmware In FastDyn

This directory contains ArduPilot and PX4 firmware images, virtual instruction
configuration, MAVLink helpers, missions, and legacy Gazebo assets used by the
FastDyn/Courbet vehicle tests.

The maintained default path is no longer a manual Gazebo launch. Use the
repository root `setup.sh` and the FastDyn TOML configs:

```bash
source ./setup.sh --build-qemu --with-rumoca
fastdyn run -c configs/copter462.toml
```

## Execution Configuration Generation

Use one command-line tool for the supplied ArduCopter 4.6.2, ArduPlane 4.6.2,
and PX4 FMUv6C profiles. Run these commands from the FastDyn repository root:

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/prepare_execution_configuration.py \
  --profile virtuals/physics/flight_controllers/courbet/ardupilot/copter462/profile.toml

fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/prepare_execution_configuration.py \
  --profile virtuals/physics/flight_controllers/courbet/ardupilot/plane462/profile.toml

fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/prepare_execution_configuration.py \
  --profile virtuals/physics/flight_controllers/courbet/px4/fmu_v6c/profile.toml
```

`--profile` also accepts the directory containing `profile.toml`. Paths inside
that file are relative to the file; `--elf` and `--output-dir` overrides are
relative to the caller's working directory. Use `--output-dir /tmp/px4-conf`
to review generated bindings without replacing the target's current output.
Dependencies are FastDyn's Python environment (pyelftools and TOML support),
plus a Python-enabled `gdb-multiarch` or `arm-none-eabi-gdb` for PX4 DWARF checks.

```text
courbet/
├── prepare_execution_configuration.py
├── execution_configuration/       # ELF resolution, rule compilation, validation, output
├── profiles/
│   ├── ardupilot/                 # Legacy callback argument conventions
│   └── px4/                       # NuttX/uORB ABI and console callback arguments
├── tests/
│   ├── test_execution_configuration.py
│   └── smoke_px4_fmu_v6c.py
├── ardupilot/
│   ├── plane462/
│   │   ├── profile.toml
│   │   ├── labeled_conf/
│   │   └── unlabeled_conf/
│   └── copter462/
│       ├── profile.toml
│       ├── labeled_conf/
│       └── unlabeled_conf/
└── px4/fmu_v6c/
    ├── profile.toml
    ├── labeled_conf/
    ├── unlabeled_conf/
    ├── boot.nsh
    ├── run.sh
    └── README.md
```

`profile.toml` selects `firmware = "ardupilot"` or `"px4"`, the input ELF,
the labeled directory, the output directory, and target-specific checks.
It is a generator descriptor; QEMU CPU/memory and FMU settings remain in the
FastDyn `configs/*.toml` files. `boot.nsh` remains a runtime input.

The shared pipeline reads the real ELF, rejects missing or ambiguous symbols,
resolves `symbol+offset`, and clears the Thumb bit only on function symbols.
It compiles both rule files and completes validation before replacing output
files. Validation failures preserve previous output. The resulting
`unlabeled_conf` contains `virtuals.txt`, `modifiers.txt`, `firmware.sha256`, and
`symbols.txt`. The symbol listing is diagnostic, ignored by Git, and not read
at runtime. Hook selection and C callback implementations remain hand-authored.

PX4 firmware rules validate the structure layouts consumed by the current
C bridge. FMUv6C's descriptor supplies the vector address and HRT/SysTick
instruction checks. A board/version with matching bridge ABI can reuse the
Python adapter with its own descriptor and labeled rules. ABI changes require
reviewing the C adapter as well; changing Python expectations alone is not enough.

The ArduCopter and ArduPlane descriptors pin their supplied ELF SHA-256 hashes
because their legacy rules contain absolute instruction addresses. Review those rules before using
a rebuilt ELF and updating the digest. ArduPilot does not yet have the PX4
adapter's structure-layout validation. Regeneration also corrects the old
ArduPlane `read_mag_when_published` binding from `0x802bd5c` to the supplied
ELF's `publish_filtered_field` address `0x8040dd0`.

ArduCopter's labeled rules also preserve the active configuration's banner
suppression, TX-space override, throttle-check override, and literal `copter`
GPS callback tag. Regeneration reproduces the existing 26 virtuals and 95
modifiers without changing runtime behavior.

Other existing vehicle directories retain their current layouts. To migrate
one, add its own `profile.toml` and review its labeled rules against its ELF;
reuse `profiles/ardupilot` instead of copying Python code. The older
`create_map_file.sh` and `preprocess.py` remain available for legacy workflows.
`create_new_conf.sh` still only copies a Rover template and does not create a
validated target profile.

Run generator regression checks (temporary outputs only):

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/tests/test_execution_configuration.py
```

PX4 run instructions and integration checks are in
[px4/fmu_v6c/README.md](px4/fmu_v6c/README.md).

## Maintained Vehicle Configs

From the FastDyn repository root:

```bash
fastdyn run -c configs/copter462.toml
fastdyn run -c configs/rover462.toml
fastdyn run -c configs/plane462.toml
```

These configs run the real ArduPilot firmware in patched QEMU and connect it to
a Rumoca-generated FMI v3 plant:

- `configs/copter462.toml` uses `FastDyn.Copter`.
- `configs/rover462.toml` uses `FastDyn.Rover`.
- `configs/plane462.toml` uses `FastDyn.Plane`.

The FastDyn wrapper models inherit reusable base plants from
`third_party/common/modelica_models/RigidBody/Examples` and define the
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
- `mav_command_control_ardupilot.py`: ArduPilot mission upload, arming, mode switch,
  monitoring, and OptiFuzz mutation parameter injection.
- `mav_command_control_px4.py`: PX4 mission upload, typed parameters, estimator
  readiness, AUTO.MISSION, arming, and landing/disarming confirmation.
- `mav_command_and_control.py`: compatibility entry point for the ArduPilot helper.
- `px4_init.param`: optional PX4 overrides, separate from ArduPilot parameters.
- `mav_health_check.py`: short smoke-test helper used for rover and plane.
- `copter_init.param`: ArduCopter parameters loaded before the mission.
- `copter_mission.waypoints`: full ArduCopter takeoff/waypoint/land mission.
- `rover_rectangle.txt`: short KLAF rover mission.
- `plane_circle_point.txt`: KLAF fixed-wing mission.

The ArduCopter config starts MAVProxy/MAVCesium and the mission helper by
default. Rover and plane configs start MAVProxy/MAVCesium plus a health check
helper by default.

FastDyn prints the viewer URL during startup:

```text
MAVCesium web viewer: open http://127.0.0.1:5000/mavcesium/
```

## Flight-Stack-Specific Mission Helpers

The two helpers operate on an existing MAVLink endpoint. ArduPilot retains its
existing command-line arguments, home-row indexing, readiness checks and
OptiFuzz behavior. All maintained copter configs use its explicit filename.
The old `mav_command_and_control.py` command still forwards to ArduPilot.

The split follows the launcher/commander separation in
[FIRE_flightstack_sim](https://github.com/mhcho1994/FIRE_flightstack_sim/tree/humble/tools/launcher).
FastDyn continues to use its QGC WPL missions and parameter files; no Gazebo,
SITL launcher or YAML scenario dependency is added.

The PX4 FMUv6C config now starts its TELEM1 UART/UDP bridge, MAVProxy/MAVCesium
and the PX4 mission helper automatically. To invoke the helper separately on an
already forwarded endpoint, run from the FastDyn root:

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/mavlink/mav_command_control_px4.py \
  --connect udpin:127.0.0.1:14552 \
  virtuals/physics/flight_controllers/courbet/mavlink/px4_init.param \
  virtuals/physics/flight_controllers/courbet/mavlink/copter_mission.waypoints
```

PX4-specific behavior:

- Accepts only a PX4 autopilot heartbeat, filters replies by vehicle/component,
  and keeps sending a GCS heartbeat during protocol waits and monitoring.
- Reads each parameter's firmware type before sending INT32 values bytewise or
  REAL32 values. Parameter files use `NAME VALUE` lines, with optional `#`
  comments. QGC's five-column `.params` export is not this input format.
  `px4_init.param` has no overrides by default. Do not use `copter_init.param`.
- Drops the conventional leading QGC home row (`current=1`, GLOBAL WAYPOINT),
  renumbers the mission, and handles both MISSION_REQUEST and MISSION_REQUEST_INT.
  Use `--keep-home` if that row is an actual waypoint. ArduPilot keeps the row.
- Requests telemetry and waits for fresh GPS/global-position/estimator validity;
  PX4's own arming checks remain active. Sets AUTO.MISSION with PX4's main/submode
  fields, arms normally, and checks the MISSION_START acknowledgement.
- Requires flight followed by the final mission item, landed state and disarming
  to report completion. Readiness, command, transfer or completion timeouts fail
  the helper. `--no-exit-on-complete` instead observes for `--monitor-sec`.
- `--no-arm` uploads only and exits without mode/arming/start commands.
  OptiFuzz `--mutation-bin` / `--mutation-params` options and their environment
  defaults are supported; integer parameters require integral mutation values.

The PX4 FMUv6C config initializes guest modules through `[Run].bridge_boot`.
TELEM1 (`/dev/ttyS5`, UART7) exchanges raw MAVLink bytes with host UDP 14551;
MAVProxy forwards to the mission helper on UDP 14552 and MAVCesium on TCP 5000.
`--no-run-processes` leaves guest modules and the UART bridge active but disables
all host helpers, including automatic arming. `run.sh --shell` disables both
initialization and host helpers. See the [PX4 profile guide](px4/fmu_v6c/README.md)
for the full command/arming sequence, transport details and real-ELF mission smoke test.

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
# Rover or plane launch smoke; set FASTDYN_COURBET_CONFIG.
FASTDYN_COURBET_CONFIG=configs/rover462.toml \
FASTDYN_COURBET_LABEL=ardurover \
tests/integration/courbet_fmu_vehicle_smoke.sh

# Full ArduCopter mission.
tests/integration/courbet_mission_smoke.sh

# Two-worker swarm launch smoke.
tests/integration/courbet_swarm_smoke.sh
```

GitHub Actions runs these paths in `.github/workflows/courbet-mission.yml`.

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
