# OptiFuzz / CP-Explore

`virtuals/fuzzer/libafl_phi` contains the OptiFuzz/CP-Explore fuzzer used to
mutate cyber-physical parameters and evaluate mission-level robustness. In this
FastDyn tree, the default backend is the high-fidelity Rumoca FMI v3 path:
OptiFuzz launches `fastdyn run`, which runs ArduPilot firmware in patched QEMU
against an FMU plant.

The older Gazebo backend and Docker image remain available for reproducing
legacy experiments, but new local and CI coverage targets the FMUv3 backend.

## Prerequisites

From the FastDyn repository root:

```bash
source ./setup.sh --build-qemu --with-rumoca
```

`setup.sh` creates the Python virtualenv, installs the FastDyn CLI, initializes
the pinned Rumoca and `modelica_models` submodules, creates the sibling Banquo
parser checkout used by CP-Explore, prepares QEMU RAM backing files, and, with
`--build-qemu`, builds the patched QEMU fork and FastDyn plugin.

The commands below disable the default `gazebo` Cargo feature so FMUv3 builds
do not need Gazebo development libraries. Enable `--features gazebo` when
using the legacy backend or `trace_recorder`.

## Run The Default Campaign

```bash
cd virtuals/fuzzer/libafl_phi
cargo run --no-default-features --features std --bin baby_fuzzer
```

By default this selects:

- vehicle: `copter`
- config: `configs/copter462.toml`
- mission: `virtuals/physics/flight_controllers/courbet/mavlink/copter_mission.waypoints`
- backend: `fmuv3`
- work root: `out/optifuzz/execution-<n>`

Each execution writes the parameter mutation bytes to
`FASTDYN_OPTIFUZZ_MUTATION_BIN`. The FastDyn mission helper reads those bytes
and applies them to the ArduPilot parameters listed in
`FASTDYN_OPTIFUZZ_PARAM_NAMES`.

## Smoke And Dry-Run Checks

To check the FastDyn command wiring without launching QEMU:

```bash
FASTDYN_OPTIFUZZ_SMOKE=1 \
FASTDYN_OPTIFUZZ_DRY_RUN=1 \
cargo run --no-default-features --features std --bin baby_fuzzer
```

CI runs this dry-run path for copter, rover, and plane, including two-worker
swarm dry-runs.

## Vehicle Selection

Use environment variables to select another maintained vehicle:

```bash
FASTDYN_OPTIFUZZ_VEHICLE=rover \
FASTDYN_OPTIFUZZ_CONFIG=configs/rover462.toml \
FASTDYN_OPTIFUZZ_MISSION_FILE=virtuals/physics/flight_controllers/courbet/mavlink/rover_rectangle.txt \
cargo run --no-default-features --features std --bin baby_fuzzer
```

```bash
FASTDYN_OPTIFUZZ_VEHICLE=plane \
FASTDYN_OPTIFUZZ_CONFIG=configs/plane462.toml \
FASTDYN_OPTIFUZZ_MISSION_FILE=virtuals/physics/flight_controllers/courbet/mavlink/plane_circle_point.txt \
cargo run --no-default-features --features std --bin baby_fuzzer
```

For `copter`, `rover`, and `plane`, the backend defaults to `fmuv3`. Override
with `FASTDYN_OPTIFUZZ_BACKEND=gazebo` only when intentionally using the legacy
Gazebo/Courbet backend.

## Swarm Execution

OptiFuzz can use `fastdyn swarm` for a fuzzer execution:

```bash
FASTDYN_OPTIFUZZ_SWARM_INSTANCES=2 \
FASTDYN_OPTIFUZZ_BASE_PORT=19000 \
cargo run --no-default-features --features std --bin baby_fuzzer
```

The generated command assigns separate QEMU monitor, MAVLink, MAVCesium, Rumoca,
GDB, QMP, work, and RAM-backing paths to each worker. This is the intended path
for scaling campaigns on one machine while preserving firmware and plant
fidelity.

## Coverage

The default high-fidelity plugin leaves LibAFL coverage export off and evaluates
physical mission behavior. If you build a FastDyn plugin with the coverage
writer enabled, set:

```bash
FASTDYN_OPTIFUZZ_COVERAGE=1
```

OptiFuzz then passes `FASTDYN_COVERAGE_FILE` and `FASTDYN_BBL_FILE` to the
FastDyn run and deserializes coverage after the execution.

## Parameters And STL Formulas

General fuzzer settings live in `src/main.rs`. Physical robustness observation
and STL formula handling live in `src/phi_observer.rs`.

Parameter inputs are currently defined in Rust. The repository still contains
the OpenAI-assisted parameter shim (`param_shim_descriptions.txt` and
`src/param_shim.rs`), but it is intentionally disabled for normal offline
campaigns and CI because it requires an API key and network access.

STL formulas live in:

```text
stl_formulas.txt
```

The Banquo parser checkout expected by the crate is prepared by `setup.sh`.

## Observing Results

Campaign artifacts are written under the fuzzer crate and the FastDyn work root,
depending on the selected path:

- `crashes/`
- `robustness_logs/`
- `trace_logs/`
- `out/optifuzz/execution-<n>/`
- `out/optifuzz/execution-<n>/swarm/` when swarm execution is enabled

Per-execution FastDyn logs and timing files are under the selected work
directory. Summarize timing with:

```bash
fastdyn timing-summary out/optifuzz/execution-0/fastdyn_timing.jsonl
```

## Legacy Docker/Gazebo Workflow

The Dockerfile in this directory installs the old Gazebo/Courbet stack. Use it
only for reproducing legacy Gazebo experiments:

```bash
touch .dockerignore
docker build -f FastDyn/virtuals/fuzzer/libafl_phi/Dockerfile -t cp_exp .
docker run -it --name cp_exp cp_exp
```

Inside the container:

```bash
cargo run --bin baby_fuzzer
```

For current FMUv3 work, native `setup.sh` plus `cargo run --bin baby_fuzzer` is
the supported and CI-tested path.
