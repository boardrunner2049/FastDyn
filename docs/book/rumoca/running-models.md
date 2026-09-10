# Run models from the command line

Choose [an environment](../general/environment.md), then generate a config for the firmware and plant you want:

| Vehicle | Base config | Model | Mission completion |
| --- | --- | --- | --- |
| Copter | `configs/copter462.toml` | `FastDyn.Copter` | Final landing |
| Rover | `configs/rover462.toml` | `FastDyn.Rover` | Final waypoint reached |

For Rover:

```bash
fastdyn-config --base configs/rover462.toml --output out/rover.toml
fastdyn run -c out/rover.toml -o out/rover/work
```

Run examples one at a time: their default MAVLink, viewer, and monitor ports
are shared. Plane's wrapper uses the library's three-wheel fixed-wing template,
but its contact events cannot yet be exported by the pinned compiler. Its
[configuration and source](configs.md#plane-462) are included for inspection;
use Copter or Rover for a runnable firmware simulation.

## Build an FMU without starting firmware

Use the same TOML for compilation and execution:

```bash
python utils/build_fmi3_fmu.py --config out/copter.toml --skip-submodules
```

The first mission's Copter writes `out/fmi3/Copter/FastDyn_Copter.fmu`.
`--no-build` generates source and metadata without compiling a shared library.
The current Rumoca also emits a portable source FMU; FastDyn normally compiles
its native library on first use and caches it beside the archive.

To compile the current array-based model directly:

```bash
rumoca compile modelica/FastDyn/Copter.mo \
  --model FastDyn.Copter \
  --source-root modelica \
  --source-root third_party/common/modelica_models \
  --output out/manual/Copter --target fmi3
```

The direct command compiles the Modelica defaults. FastDyn applies the numeric
parameter overrides in TOML when it initializes the FMU.

## Choose another model

Add another `[FMU.models.<name>]` entry with its `model`, `model_file`,
`source_roots`, and a distinct `output` directory. Set `[FMU].active` to that
name, or override the selection for one run:

```bash
fastdyn run -c out/copter.toml --fmu small_quad -o out/small_quad/work
```

That command requires an entry named `small_quad` in your TOML. A compatible
plant must expose FastDyn's PWM and sensor interface. Copy the wrapper in
`modelica/FastDyn/Copter.mo` for a new quadrotor plant, preserving its output
names, dimensions, units, and body FRD/NED conventions. An arbitrary Modelica
model does not automatically implement that interface.

## Run options

| Option | Purpose |
| --- | --- |
| `-c, --config PATH` | Required TOML file |
| `-o, --work-dir PATH` | Generated firmware/plugin configuration and run output |
| `--fmu NAME` | Select a named FMU entry for this run |
| `--no-build-fmu` | Use an existing artifact without automatic compilation |
| `--no-run-processes` | Launch QEMU without configured MAVProxy or mission helpers |
| `-p, --persist-work-dir` | Retain the existing generated work directory |
| `-m, --map-file PATH` | Supply a symbol map |
| `-s, --svd PATH` | Supply peripheral descriptions |

Use `fastdyn run --help` and `python utils/build_fmi3_fmu.py --help` for the
complete command-line help. Keep persistent model settings in TOML; CLI
overrides are useful for individual runs.
