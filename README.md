# FastDyn

FastDyn runs microcontroller firmware in a patched QEMU and lets you shape the
run from one TOML configuration: peripheral device models (written in C,
replayed from a hardware trace, or passed through to a real board over a debug
probe), virtual instructions and register/memory modifiers at chosen program
counters, run plugins, co-simulated physics (FMI v3 FMUs, Gazebo), coverage and
fuzzing. It is driven by the `fastdyn` command and a Python API.

Tools built on FastDyn live in their own repositories:

- [LibVIO](https://github.com/boardrunner2049/LibVIO): the library device
  models use for buses (SPI, I2C), DMA, signals and host I/O.
- [BoardRunner](https://github.com/boardrunner2049/BoardRunner): learns device
  models from a hardware trace with an LLM and verifies them.
- [PhantomSmith](https://github.com/PSecLab/PhantomSmith): rehosts complete
  firmware automatically, including the ArduRover 4.6.2 reference target.

Contributions follow [docs/CodingConventions.md](docs/CodingConventions.md).

## Quick start with Docker

The Dockerfile builds FastDyn, its patched QEMU, libhw, the libFuzz fuzzer and
the Gazebo/Courbet flight-controller backend from this checkout. Check out the
submodules it builds from, then build from the repository root:

```bash
git submodule update --init --depth 1 \
    third_party/common/qemu third_party/common/libhw \
    third_party/common/cmsis-svd-data \
    third_party/courbet_deps/mavlink_headers \
    third_party/courbet_deps/ardupilot_gazebo third_party/courbet_deps/SITL_Models
docker build -t fastdyn .
docker run -it fastdyn
```

The container starts in `/workspace/FastDyn` with the `fastdyn` command ready.

## Build from source

On Ubuntu 24.04.

### 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake git meson ninja-build pkg-config python3-venv \
  libglib2.0-dev libcjson-dev libpixman-1-dev libfdt-dev zlib1g-dev flex bison \
  libstlink-dev libusb-1.0-0-dev
```

### 2. Set up and build

From the FastDyn checkout:

```bash
source ./setup.sh --build-qemu --build-libhw
```

This creates the virtual environment `fastdyn-env/` with the `fastdyn`
command, checks out and builds the pinned QEMU fork
(`third_party/common/qemu`, with FastDyn's patch) and libhw
(`third_party/common/libhw`), and builds the FastDyn QEMU plugin
`build/libfastdyn.so` with device models and the physics backends. Leave out
`--build-libhw` if you will not connect a board (passthrough, twintrace
recording). Re-running it reuses what is already built; in a new shell,
`source fastdyn-env/bin/activate` is enough.

After changing FastDyn's C code, rebuild with `ninja -C build`.

[docs/Building.md](docs/Building.md) has the rest: every `setup.sh` option,
building by hand, the `make` flags, LibVIO for device models, installing for
model builds elsewhere, SUNDIALS, Gazebo and Rumoca.

## Minimal example

[`configs/minimal.toml`](configs/minimal.toml) runs a bundled FreeRTOS
firmware and changes it at chosen program counters, without touching the
binary. The file lists the memory, machine, CPU and device settings with a
comment on what each one does; these are its hooks (it also starts the timer
at `main()`):

```toml
[[CPU.cpu0.modifiers]]            # before main() calls vStartLEDFlashTasks(1): r0 = 3
at = "0x0800397e"
patch = "r0 <- 3"

[[CPU.cpu0.virtuals]]             # inside vStartLEDFlashTasks(): print the argument it got
at = "0x080068b0"
instruction = "printreg"
args = ["0"]

[[CPU.cpu0.virtuals]]             # when the scheduler starts: print the time and stop
at = "0x08001940"
instruction = "benchmark_end"
args = ["minimal"]
```

```console
$ fastdyn -q run -c configs/minimal.toml
Current CPU type: cortex-m3-arm-cpu
Reg 0:0x00000003
[BENCH][minimal] elapsed_ns=3355151 elapsed_us=3355.151 elapsed_s=0.003355151
```

The function received 3, not the 1 the firmware passes, and the run ends by
itself. A modifier patches registers or memory at a PC and a virtual instruction
runs a FastDyn action there ([docs/VirtualsAndModifiers.md](docs/VirtualsAndModifiers.md)).

A larger example, the same firmware with RTOS introspection and a live
activity monitor at `http://127.0.0.1:8765/` (stop it with Ctrl-C):

```bash
fastdyn run -c configs/introspection/freertos.toml -o fastdyn-introspection-demo
```

## Running FastDyn

```bash
fastdyn run -c <config.toml> -o <work-dir>
```

One TOML configuration selects the board, firmware, memory, device models,
virtual instructions, plugins and physics. `fastdyn help` browses the
features and prints configuration snippets; `fastdyn build-model` compiles C
device models. Start with [docs/RunningFastDyn.md](docs/RunningFastDyn.md) and
[docs/BuildingAConfig.md](docs/BuildingAConfig.md).

- ArduPilot vehicles with FMI v3 physics, MAVCesium, and parallel campaigns
  (`fastdyn swarm`): [docs/FlightControllerSimulation.md](docs/FlightControllerSimulation.md)
- Fuzzing (LibAFL/AFLNet, OptiFuzz): [docs/Fuzzing.md](docs/Fuzzing.md)

## Documentation Map

- `docs/Building.md`: `setup.sh` options, building by hand, `make` flags,
  LibVIO, installing for model builds elsewhere.
- `docs/RunningFastDyn.md`: running a configuration, its outputs, logging, and
  building device models with `fastdyn build-model`.
- `docs/Configuration.md`: FastDyn TOML reference for QEMU, FMU, Rumoca,
  helper processes, profiling, and swarm options.
- `docs/FastDynDeviceModel.md`: the device models (classic, passthrough,
  elder, twintrace) and how a model library is written
  (`docs/ElderScrollDeviceModel.md`).
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
  firmware configs, MAVLink helpers, missions, and local smoke tests.
- `virtuals/physics/physics_engines/README.md`: C physics backend contract and
  FMU/Gazebo backend notes.
- `virtuals/fuzzer/libafl_phi/README.md`: OptiFuzz/CP-Explore usage with
  FMUv3, vehicle selection, dry-runs, swarm execution, and legacy Docker/Gazebo.
- `docs/FlightControllerSimulation.md`: ArduCopter/Rover/Plane with FMI v3
  physics, MAVCesium, helper processes, profiling, `fastdyn swarm`, CI.
- `docs/Fuzzing.md`: the fuzzing workflows (LibAFL/AFLNet, OptiFuzz).
- `docs/FastDyn_STR_Integration.md`: schema-driven fuzzing integration guide.
- `tests/integration/README.md`: local integration smoke and mission tests.
- `docs/CodingConventions.md`: how code, tests, docs and commits are written.

## Tests

Unit tests live in `tests/unit/` (conventions in `tests/unit/README.md`):

```bash
pytest tests/unit/ -v
```

Integration and mission smoke tests are described in
`tests/integration/README.md`.

## License

FastDyn is licensed under the [Apache License 2.0](LICENSE). Copyright 2026
PSecLab. The bundled third-party firmware and files keep their own licenses;
see [NOTICE](NOTICE).
