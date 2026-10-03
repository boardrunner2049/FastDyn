# Running FastDyn

## Slide: Run a firmware from one configuration

**FastDyn turns a firmware ELF and one TOML configuration into a QEMU run.**

```bash
fastdyn run -c configs/target.toml -o fastdyn_work
```

- `-c` / `--config` is required: it selects the board, CPU, memory, firmware,
  device routing, virtuals, and enabled run-wide plugins.
- `-o` / `--work-dir` selects where FastDyn writes run artifacts. It defaults to
  `./fastdyn_work`; give each experiment its own directory.
- FastDyn prepares enabled plugins, resolves symbols and trigger addresses,
  writes generated virtual/modifier rules, prepares RAM backing, builds a
  configured FMU when applicable, then launches QEMU.

## Slide: A practical first run

```bash
# Optional: derive a reviewed starter from an ELF.
./fastdyn-env/bin/python tools/elf2config/elf_to_config.py firmware.elf \
  --output configs/firmware.toml

# Browse or refine the configuration.
fastdyn help

# Run it and keep all artifacts together.
fastdyn run -c configs/firmware.toml -o fastdyn_work
```

Before launching, make sure the TOML has the correct QEMU path, CPU target,
RAM map, initial vector address for Cortex-M firmware, and peripheral routing.
The generated configuration uses `classic` for the conventional Cortex-M MMIO
window as a starting point; it must be reviewed for the actual board.

## Slide: Useful run options

```bash
# Retain a prior work directory rather than resetting it.
fastdyn run -c configs/target.toml -o fastdyn_work --persist-work-dir

# Use an explicit SVD file or SVD catalog when resolving [Machine].platform.
fastdyn run -c configs/target.toml -s third_party/common/cmsis-svd-data

# Override the active FMU, or skip its automatic build.
fastdyn run -c configs/target.toml --fmu quadrotor
fastdyn run -c configs/target.toml --no-build-fmu
```

Use `fastdyn run --help` for every option. `fastdyn help run` provides the
same primary workflow in the interactive configuration-help menu.

## Slide: What to inspect after a run

The work directory is the run record. Depending on the configuration, it
contains:

- `virtuals/virtuals.txt` and `virtuals/modifiers.txt`: resolved runtime rules;
- `run-artifacts/`: generated plugin data such as introspection schemas;
- logs, QEMU output, RAM backing, and timing data;
- plugin-specific results such as FunctionCounter counts or VariableWatch logs.

If the run does not boot, begin with the CPU/machine selection, memory map,
ELF/vector address, QEMU path, and peripheral routing. Use `fastdyn help` to
find valid configuration values without memorizing them.

## Slide: Logging

FastDyn writes log messages to stderr and command results to stdout. Control
them with options placed before the command:

```bash
fastdyn -q run -c configs/minimal.toml          # warnings and errors only
fastdyn -v run -c configs/minimal.toml          # include debug messages
fastdyn --log-level fastdyn.targets=DEBUG run -c configs/minimal.toml   # one area only
fastdyn --log-file fastdyn_work/fastdyn.log run -c configs/minimal.toml # also save everything
```

`--log-level` takes a level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) or
`LOGGER=LEVEL`, and can be repeated. `FASTDYN_LOG_LEVEL` sets the default
level when no option is given. The log file always records debug messages,
without colour. Programs that use FastDyn's Python API configure logging
themselves, for example with `fastdyn.logs.configure()`.

Messages from the QEMU plugin and runtime plugins (for example
`fastdyn.function_tracer|WARNING|  ...`) use the same format and the same
default level. `--log-level fastdyn.native=LEVEL` sets their level on its
own. They are printed by QEMU directly, so they are not saved in the log
file, and native debug messages only exist in builds with `DEBUG_PRINT`
enabled.

## Slide: Building device models

Device models for the elder handler are shared libraries. `fastdyn
build-model` compiles them, one `.so` per `.c` file, named after the file:

```bash
fastdyn build-model models/usart2.c -o build/models          # build/models/usart2.so
fastdyn build-model models/ -o build/models -r -j 8          # every .c under models/, in parallel
fastdyn build-model --name spi4 models/spi4.c models/spi4_dma.c -o build/models
```

FastDyn adds its own headers (`<fastdyn/device_model.h>`, QEMU's
`<qemu/qemu-plugin.h>`), LibVIO when `pkg-config vio` finds it (`--no-vio`
to leave it out), and the model's folder and its `include/` directories.
`--pkg NAME` adds another pkg-config package; `--pkg fastdyn` links the
fuzzer hooks (`<fastdyn/fuzz_hooks.h>`). Models are compiled with
`-shared -fPIC -g -Werror=incompatible-pointer-types`, plus `$CFLAGS` and
`$LDFLAGS`.

Only models whose sources, headers or compile command changed are rebuilt.
Without `--keep-going`, no new build starts after the first failure. Each
model's result is printed, followed by a summary such as
`built 52, up to date 3, failed 3`; `--json` prints a report with the full
compiler output instead. A model that fails has no `.so` afterwards, so an
older build is never loaded by mistake. The exit status is 1 if any model
failed.

From Python: `fastdyn.build_models(paths_or_models, out_dir, jobs=...,
keep_going=..., vio=..., pkgs=..., exclude=..., recursive=...)` returns a
`BuildReport`; `fastdyn.Model(name, sources)` describes a multi-file model.
