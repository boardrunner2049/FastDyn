# FastDyn

**[Read the documentation and Rumoca tutorial →](https://jgoppert.github.io/FastDyn/)**

## Automatic Rehosting of Ardupilot Ardurover v4.6.2

Please access the detailed rehosting steps in the `docs/Ardurover_Rehosting.md`

## Development environment and tutorial

Choose [Nix, Docker, or native installation](docs/book/general/environment.md).
The [one-hour Rumoca walkthrough](docs/book/rumoca/session.md) includes runnable
commands, Modelica sources, gain tuning, and recorded payload experiments.

The Docker environment is generated from the Nix development shell:

```bash
nix build .#devContainer --out-link out/dev-container
docker load --input out/dev-container
docker run --rm -it --user "$(id -u):$(id -g)" \
  --volume "$PWD:/workspace" --publish 5000:5000 fastdyn-dev:local
```

Participants can also use the published GHCR image without installing Nix.
See [building and sharing the image](docs/book/general/container.md) for preview
tags, publication status, and commands. The older root Dockerfile serves the
legacy Gazebo environment.

## Quick Start: ArduCopter FMUv3 + MAVCesium

The pinned Rumoca compiler supports the current array-based Copter and Rover
models. Plane uses the library's three-wheel template; its contact-event FMI
export is pending compiler support. For a working Copter mission, follow the
[first-mission walkthrough](docs/book/rumoca/getting-started.md).

On Ubuntu 24.04, the native development setup for the Rumoca FMI v3 quadrotor
plant, ArduCopter running in patched QEMU, mission automation, and the
MAVCesium web view:

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake device-tree-compiler git libexpat1-dev libfdt-dev \
  libglib2.0-dev libpixman-1-dev libudev-dev lsof meson ninja-build pkg-config \
  python3-dev python3-venv universal-ctags zlib1g-dev

# Rumoca and OptiFuzz use Rust. Skip this if cargo is already on PATH.
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"

source ./setup.sh --build-qemu --with-rumoca
fastdyn run -c configs/copter462.toml
```

The first setup run builds the pinned Rumoca toolchain, clones/builds the
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

## Documentation Map

The tutorial book uses **mdBook** and deploys to
[GitHub Pages](https://jgoppert.github.io/FastDyn/) from `main`.
Choose [Nix, Docker, or a manual installation](docs/book/general/environment.md),
then preview it with `mdbook serve docs --open` at **http://localhost:3000**.
A lightweight documentation-only setup is also described in
[the publishing guide](docs/book/documentation.md). Chapters live in `docs/book/`.

The book includes general FastDyn documentation, a one-hour Rumoca walkthrough,
TOML overlays, recorded Copter/Plane/Rover missions, and a payload study with
all trajectories. Copter, Rover, QAV-R, and the payload experiments use the
same pinned compiler. The historical Plane recording is marked separately
from its current model, which awaits contact-event export support.

For a directly runnable RTOS introspection demonstration using the bundled
debug-symbol FreeRTOS ELF, see
[`configs/introspection/freertos.toml`](configs/introspection/freertos.toml).

- `docs/Configuration.md`: FastDyn TOML reference for QEMU, FMU, Rumoca,
  helper processes, profiling, and swarm options.
- `docs/VirtualsAndModifiers.md`: reference for PC-triggered virtual
  instructions and direct register/memory modifiers, including TOML syntax
  and the built-in virtual callback registry.
- `docs/VirtualPreprocessing.md`: public SDK for FastDyn-owned virtual and
  firmware-wide preprocessing, including RTOS introspection.
- `docs/IntrospectionExamples.md`: directly runnable RTOS introspection
  configurations and their bundled debug-symbol test binaries.
- `docs/WritingVirtuals.md`: contributor guide for implementing, registering,
  testing, and configuring a new native virtual or preprocessing module.
- `docs/FunctionCounterPlugin.md`: a runnable, self-contained example of a
  run preprocessor that instruments ELF function entries and writes counts.
- `virtuals/physics/README.md`: maintained Rumoca FMI v3 physics path,
  mission assets, timing semantics, and legacy Gazebo notes.
- `virtuals/physics/flight_controllers/courbet/README.md`: Courbet/ArduPilot
  firmware configs, MAVLink helpers, missions, and local integration tests.
- `virtuals/physics/physics_engines/README.md`: C physics backend contract and
  FMU/Gazebo backend notes.
- `virtuals/fuzzer/libafl_phi/README.md`: OptiFuzz/CP-Explore usage with
  FMUv3, vehicle selection, dry-runs, swarm execution, and legacy Docker/Gazebo.
- `tests/integration/README.md`: local integration and mission tests.

## Local Build

FastDyn depends on two sibling repositories that you must build first:

- `qemu/` — the patched QEMU fork that hosts the FastDyn plugin
- `libhw/` — the hardware-probe abstraction (ST-Link / OpenOCD / J-Link)

Clone both as siblings of this repository (or anywhere — you'll point `make` at them via `qemu_path` / `libhw_path`).

### 1. System dependencies

```bash
sudo apt-get update
sudo apt-get install -y meson ninja-build pkg-config libglib2.0-dev libcjson-dev python3-venv
# Optional: only required if you enable SUNDIALS=true
sudo apt-get install -y libsundials-dev
```

### 2. Build the QEMU fork and libhw

Follow the build instructions in each sibling repo. At a minimum:

```bash
# libhw → produces <libhw_path>/out/libhw.so
cd <path/to>/libhw && make

# qemu (fork) → produces qemu-system-arm + plugin headers
cd <path/to>/qemu
git apply <path/to>/FastDyn/patches/qemu-fastdyn-plugin-icount.patch
./configure --target-list=arm-softmmu --enable-plugins
make qemu-system-arm
```

### 3. Build the FastDyn plugin

From the FastDyn repository root:

```bash
make qemu_path=<path/to>/qemu libhw_path=<path/to>/libhw
```

Defaults if you omit the flags: `qemu_path=../qemu`, `libhw_path=../libhw`.

The build produces `build/libfastdyn.so` and copies `libhw.so` next to it.

### 4. Make `libfastdyn.so` discoverable at runtime

```bash
export LD_LIBRARY_PATH=$PWD/build:$PWD/device_models/postmartem/verifier${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
```

### 5. Optional Makefile flags

Most features are off by default. Override at `make` time:

| Flag                 | Default | Enables                                                                 |
| -------------------- | ------- | ----------------------------------------------------------------------- |
| `LIBHW`              | `true`  | Hardware passthrough via libhw                                          |
| `LIBFUZZ`            | `true`  | LibAFL fuzzing harness                                                  |
| `DEV`                | `true`  | Built-in device models                                                  |
| `DEBUG_PRINT`        | `true`  | Verbose plugin logging                                                  |
| `LIBGZ`              | `false` | Gazebo / SITL physics integration                                       |
| `LIBPY`              | `false` | Halucinator-mode Python callbacks                                       |
| `SUNDIALS`           | `false` | SUNDIALS solver for FMU physics                                         |
| `PHY`                | `false` | Physics engine; auto-enabled by `LIBGZ`, `FMU`, or `FLIGHT_CONTROLLERS` |
| `FMU`                | `false` | FMU (Functional Mockup Unit) support                                    |
| `FLIGHT_CONTROLLERS` | `false` | ArduPilot / PX4 SITL adapters                                           |
| `BOARD_RUNNER`       | `true`  | BoardRunner SDK build                                                   |

Example:

```bash
make qemu_path=<path/to>/qemu libhw_path=<path/to>/libhw LIBPY=true SUNDIALS=true
```

## Install the Python CLI

Use the setup helper to create a virtualenv, install `fastdyn`, initialize the
default FastDyn/Courbet/Rumoca submodules, and create the local QEMU RAM backing
files:

```bash
source ./setup.sh --with-rumoca
```

If the patched QEMU fork and FastDyn QEMU plugin are not built yet, setup prints
the exact commands. To have setup clone/build them too, run:

```bash
source ./setup.sh --build-qemu --with-rumoca
```

`setup.sh --build-qemu` fetches `https://github.com/Arslan8/qemu.git` at
`fastdyn` by default and applies
`patches/qemu-fastdyn-plugin-icount.patch`. Override the QEMU source when
needed:

```bash
source ./setup.sh --build-qemu --with-rumoca \
  --qemu-repo https://github.com/Arslan8/qemu.git \
  --qemu-ref fastdyn
```

Verify with:

```bash
fastdyn --help
```

To leave the venv later: `deactivate`.

## Run

We recommend running every `fastdyn` / `boardrunner` command from the FastDyn repository root. The `cmsis-svd-data` submodule is auto-fetched into `third_party/common/cmsis-svd-data/` by `make` and resolved automatically when you pass `-b <board>`.

Configs may include an `[FMU]` table. When `[FMU] auto_build = true`,
`fastdyn run -c <config>` builds the selected FMU when it is missing or stale,
using the pinned Rumoca and `modelica_models` submodules. The provided
Courbet configs build FastDyn-owned ArduPilot wrapper models:
`FastDyn.Copter`, `FastDyn.Rover`, and `FastDyn.Plane`.
Those wrappers live directly in `modelica/FastDyn/` and inherit
the reusable plants from `third_party/common/modelica_models/Vehicles/Templates`.
Plane source is included, but its contact-event FMI export is not supported yet.
Keep vehicle-generic rigid-body dynamics in `modelica_models`; keep
FastDyn-specific sensors, firmware interfaces, missions, ports, helper
processes, and parameter overrides in this repository. Switch configured FMUs
with `active = "<name>"` or `fastdyn run -c <config> --fmu <name>`.
If the pinned FMU toolchain is not available, FastDyn reports the missing path
and recommends `source ./setup.sh --with-rumoca`. For artifact generation without running
QEMU, use `./utils/build_fmi3_fmu.py`.

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

ArduRover drives a rectangle and stops when the final waypoint is reached:

```bash
fastdyn run -c configs/rover462.toml
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

## Parallel Campaigns

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

CI launches real two-worker swarms for ArduCopter and ArduRover
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
`.github/workflows/ci.yml`: setup, patched QEMU build, Rumoca FMI
v3 FMU generation, FastDyn plugin build, OptiFuzz FMUv3 dry-runs for copter,
rover, and plane, OptiFuzz two-worker swarm dry-runs for all three vehicles,
MAVCesium helper startup, real two-worker Copter/Rover swarm launches, the
ArduRover waypoint mission, and ArduCopter upload, takeoff, waypoints, and
firmware-confirmed landing. A separate check verifies that the three-wheel
Plane model lowers and its unsupported contact-event export is rejected.
Plane flight is not validated by the current CI.

CI caches the patched QEMU checkout, Cargo build artifacts, and pip downloads.
Cache keys include the compiler and dependency revisions; completed builds are
saved before simulation checks. Rumoca and OptiFuzz builds appear as separate
steps so their timings are visible.

The `mission-report` artifact contains console and binary MAVLink logs for
Copter and Rover, plus PNG/SVG plots and CSV/JSON telemetry. The plot shows the flight track
and numbered mission waypoints, plus altitude tracking through landing. The
control altitude setpoint is reconstructed from measured altitude plus
ArduCopter's `NAV_CONTROLLER_OUTPUT.alt_error`; the reported navigation target
is shown separately. Time uses the firmware simulation clock and altitude is
relative to home. A download link appears in the CI job summary.

The full mission integration script is `tests/integration/courbet_mission_test.sh`;
locally, run it after setup with:

```bash
tests/integration/courbet_mission_test.sh
```

Generate the same report locally after the mission:

```bash
python -m fastdyn.mission_report \
  --log out/ci/courbet_mission.tlog \
  --mission virtuals/physics/flight_controllers/courbet/mavlink/copter_mission.waypoints \
  --output out/ci/mission-summary.png --require-setpoint
```

## Fuzzer

FastDyn has two complementary fuzzing workflows:

- The in-process LibAFL/AFLNet fuzzer targets firmware loops and input points
  directly with `coverage = true` plus `anchor` / `assert` virtual
  instructions. Docker is used there as a reproducible build environment for
  LibAFL/AFLNet dependencies; see
  `virtuals/fuzzer/fastdyn_fuzz_lib/README.md`.
- OptiFuzz / CP-Explore lives in `virtuals/fuzzer/libafl_phi`. Its default
  backend is now the high-fidelity FMUv3 ArduCopter path:

  ```bash
  source ./setup.sh --with-rumoca
  cd virtuals/fuzzer/libafl_phi
  cargo run --no-default-features --features std --bin baby_fuzzer
  ```

  `setup.sh` creates the expected sibling Banquo parser checkout at `../banquo`
  unless `--skip-optifuzz` is used. OptiFuzz then launches
  `fastdyn run -c configs/copter462.toml`, writes each execution under
  `out/optifuzz/execution-<n>`, passes parameter mutations to the mission helper
  through `FASTDYN_OPTIFUZZ_MUTATION_BIN`, and can optionally use `fastdyn swarm`
  by setting `FASTDYN_OPTIFUZZ_SWARM_INSTANCES=2` or higher. Select another
  vehicle/config with `FASTDYN_OPTIFUZZ_VEHICLE=rover` and
  `FASTDYN_OPTIFUZZ_CONFIG=configs/rover462.toml`, or `plane` /
  `configs/plane462.toml`. Set
  `FASTDYN_OPTIFUZZ_BACKEND=gazebo` and build with `--features gazebo` to use
  the older Gazebo/Courbet runner (requires Gazebo development libraries).
  To check the wiring without launching QEMU, run:

  ```bash
  FASTDYN_OPTIFUZZ_SMOKE=1 FASTDYN_OPTIFUZZ_DRY_RUN=1 cargo run --no-default-features --features std --bin baby_fuzzer
  ```

  Set `FASTDYN_OPTIFUZZ_COVERAGE=1` when using a FastDyn plugin built with the
  LibAFL coverage writer; the default high-fidelity plugin leaves this off.

- `fastdyn swarm` is also available directly as a high-fidelity campaign runner
  for many full ArduCopter/FMUv3 simulations. It keeps the actual QEMU firmware,
  board tick, FMU plant, MAVProxy, MAVCesium, and mission script path intact
  while scaling out to many isolated workers on the same host.

## Required OS Dependencies

For Sundial build of Fastdyn:

```bash
sudo apt-get update
sudo apt-get install -y libsundials-dev pkg-config
```

---

## LLM Integration

FastDyn can send generated prompts directly to an LLM provider, process the
response, and write or patch the device model automatically. OpenAI remains the
default provider. Ollama can be used as an optional local HTTP backend.

### Setup

#### 1. Install Dependencies

The `openai` and `python-dotenv` packages are required for the default OpenAI
provider. They are included in
`requirements.txt`, so running `./setup.sh` or `pip install -r requirements.txt`
will install them.

#### 2. Configure Your API Key for OpenAI

Create a file at `~/.fastdyn.env` with your OpenAI API key:

```bash
echo 'OPENAI_API_KEY=sk-your-key-here' > ~/.fastdyn.env
chmod 600 ~/.fastdyn.env
```

Alternatively, export the environment variable directly:

```bash
export OPENAI_API_KEY=sk-your-key-here
```

The tool checks the environment variable first, then falls back to `~/.fastdyn.env`.
You can also specify a custom env file with `--env-file /path/to/.env`.

Ollama does not require an API key.

#### 3. Configure Build Paths (for `--compile`)

If you want to use the `--compile` flag to automatically compile models, fill in
the `boardrunner/boardrunner_sdk/build_config.env` file:

```bash
FASTDYN_INCLUDE_DIR=/path/to/FastDyn/include
QEMU_INCLUDE_DIR=/path/to/qemu/include
```

### Usage

```bash
# Basic: send an initial prompt and extract the model
boardrunner llm -d fastdyn_work_adc -o boardrunner/boardrunner_sdk/model/model.c

# Use a specific model
boardrunner llm -d fastdyn_work_adc -o model.c --model gpt-4.1

# With compilation after extraction
boardrunner llm -d fastdyn_work_adc -o boardrunner/boardrunner_sdk/model/model.c --compile

# Revised prompt (patch mode) with retry
boardrunner llm -d fastdyn_work -o boardrunner/boardrunner_sdk/model/model.c --max-retries 2

# Disable the conversation-reset line for multi-turn context
boardrunner llm -d fastdyn_work_adc -o model.c --no-stateless

# Use Ollama through a local or SSH-forwarded HTTP endpoint
boardrunner llm -d fastdyn_work_adc \
  -o boardrunner/boardrunner_sdk/model/model.c \
  --compile \
  --model qwen3-coder-next \
  --model-provider ollama \
  --ollama-url http://127.0.0.1:11434 \
  --evaluate
```

If Ollama runs on a remote GPU server, expose it locally before running
FastDyn:

```bash
ssh -N -L 11434:127.0.0.1:11434 h100
curl http://127.0.0.1:11434/api/tags
```

### Command Reference

| Option                           | Default                       | Description                               |
| -------------------------------- | ----------------------------- | ----------------------------------------- |
| `-d` / `--work-dir`              | (required)                    | Work directory with prompt files          |
| `-o` / `--output`                | (required)                    | Model .c file path                        |
| `--model`                        | `gpt-4o`                      | Model name                                |
| `--model-provider`               | `openai`                      | Provider backend: `openai` or `ollama`    |
| `--env-file`                     | `~/.fastdyn.env`              | Path to .env file with API key            |
| `--temperature`                  | `0.2`                         | Sampling temperature                      |
| `--stateless` / `--no-stateless` | `--stateless`                 | Keep or strip the conversation reset line |
| `--compile` / `--no-compile`     | `--no-compile`                | Compile model after writing               |
| `--sdk-dir`                      | `boardrunner/boardrunner_sdk` | Path to boardrunner SDK                   |
| `--max-retries`                  | `1`                           | Max retry attempts on failure             |
| `--ollama-url`                   | `http://127.0.0.1:11434`      | Ollama server base URL                    |
| `--ollama-num-ctx`               | `262144`                      | Ollama `num_ctx`; use `0` for default     |
| `--ollama-timeout`               | `1800`                        | Ollama request timeout in seconds         |

### How It Works

1. **Initial prompt** (`initial_prompt.txt`): The tool sends the prompt to the
   selected LLM provider, extracts the C code from the fenced code block in the
   response, and writes it to the output file.

2. **Revised prompt** (`revised_prompt.txt`): The tool sends the prompt to the
   selected LLM provider, parses SEARCH/REPLACE blocks from the response, and
   applies them as patches to the existing model file.

3. **On failure**: If a patch fails or compilation fails, the tool prompts you to
   send a follow-up request to the LLM with the error context for automatic correction.

4. The raw LLM response is always saved to `<work_dir>/llm_response.txt` for auditing.

---

## Unit Tests

Unit tests live in `tests/unit/`. See `tests/unit/README.md` for conventions and
detailed instructions.

```bash
# Run all unit tests
pytest tests/unit/ -v

# Run a specific test file
pytest tests/unit/test_patch.py -v
```
