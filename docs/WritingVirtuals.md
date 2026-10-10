# Writing a FastDyn Virtual or Preprocessing Module

This guide is for contributors adding FastDyn behavior, rather than users
configuring one of the [existing virtuals](VirtualsAndModifiers.md).

There are two parts to a PC-triggered virtual:

```text
TOML virtual rule -> optional Python preparation -> native C callback
```

Use a **virtual** when behavior occurs at one guest PC. Use a **run-wide
preprocessor** when a feature applies to a whole firmware run, such as RTOS
introspection. Run-wide preprocessors can emit internal virtual rules, which
then use the same normal pipeline.

The public Python contract is described in
[VirtualPreprocessing.md](VirtualPreprocessing.md). This page is the practical
recipe for using it safely.

For a compact, runnable run-preprocessor implementation, see the
[function-counter plugin](FunctionCounterPlugin.md). It discovers ELF
functions, emits one entry virtual per function, and writes aggregate counts
without any feature-specific frontend or QEMU command-line handling.
The [function-tracer plugin](FunctionTracerPlugin.md) builds on that example:
its preprocessor turns DWARF formal parameters into a runtime argument schema.

## Choose the smallest extension

| Need | Implement |
|---|---|
| A direct deterministic register or memory assignment at a PC | A [modifier](VirtualsAndModifiers.md#modifiers), not a virtual. |
| A new action at one PC, with no host preparation | A native C callback and a `VirtualDefinition`. |
| A new action at one PC that needs symbols, SVD IRQ names, or generated files | A native C callback, `VirtualDefinition`, and `VirtualPreprocessor`. |
| Firmware-wide analysis or setup that may generate hooks | A `RunDefinition` and `RunPreprocessor`; add C callbacks only for emitted hooks. |

Do not add a virtual-specific condition to BoardRunner, `Fastdyn.run`, or the
QEMU command builder. Those layers only run the generic preparation pipeline.

## 1. Implement the native callback

The QEMU plugin executes the callback when the trigger PC is reached. Its
signature is defined by `cb_func_t` in `include/common.h`:

```c
static void example_virtual(unsigned int cpu_index, void *userdata)
{
    const char *args = userdata;  /* the prepared, space-joined arguments */
    /* Perform the runtime action through virtual_*(). */
}
```

For a compiled-in feature, use the public C runtime SDK in
`include/fastdyn_runtime.h`. A module declares one initializer; FastDyn finds
the declaration generically after its callback registry and artifact root are
ready:

```c
#include <fastdyn_runtime.h>

static int example_runtime_init(const VirtualContext *ctx)
{
    char output[4096];
    if (virtual_register_callback(ctx, "example_virtual",
                                         example_virtual) != 0) {
        return -1;
    }
    if (virtual_artifact_path(ctx, "results.tsv", output,
                                      sizeof(output)) != 0) {
        return -1;
    }
    virtual_register_exit(ctx, example_write_results);
    return 0;
}

VIRTUAL_PLUGIN("example", example_runtime_init);
```

The module name namespaces `results.tsv` below
`run-artifacts/example/`. The SDK is the only supported route for registering
callbacks, resolving module artifacts, and registering shutdown work. Do not
add a callback to `cb_registry`, call `virtual_register()` directly, inspect
QEMU/plugin arguments, or add an initializer call in `virtuals/virtuals.c`.

The public callback name must exactly match the Python `VirtualDefinition` and
TOML instruction name. Build `libfastdyn.so` after changing native sources.
[`virtuals/README.md`](../virtuals/README.md) is the complete native API
reference, including guest memory/register access, PC/SP/time access, IRQ and
translation hooks, and dynamically-created virtual rules and modifiers.
For register operations, include the header for the guest architecture (for
example `<fastdyn/arch/arm_v7m.h>` or `<fastdyn/arch/riscv64.h>`); never embed
an undocumented register number.

The callback receives a single string, not a Python object. Validate and parse
that string defensively in C even if a Python preprocessor also validates it.

## 2. Register Python metadata

Every FastDyn-owned native callback needs a Python definition, including
callbacks without Python preparation. This lets FastDyn validate capabilities,
normalize arguments, and detect name drift before QEMU starts.

For a user-configurable callback, register its behavior and its public
configuration metadata together:

```python
from fastdyn.virtual_preprocessing import (
    ConfigurationHelp, VirtualDefinition, register_virtual,
)

register_virtual(VirtualDefinition(
    name="example_virtual",
    help=ConfigurationHelp(
        description="Describe the user-visible action.",
        toml='''[[CPU.cpu0.virtuals]]
at = "main+4"
instruction = "example_virtual"
args = ["value"]''',
        documentation="docs/ExampleVirtual.md",
    ),
))
```

For a virtual or feature module, keep the Python registration beside its
native implementation as `virtuals/<feature>/host/preprocessor.py`. FastDyn scans
those files generically; importing the file must call `register_virtual()`
and/or `register_run_preprocessor()`. The frontend does not import a feature
by name.

`ConfigurationHelp` is required for a feature that users should discover and
configure. It owns the concise description, TOML fragment, and documentation
path shown by `fastdyn help`; the generic frontend does not keep a second list
of feature names or arguments. Omit it only for an internal callback generated
by another plugin, such as a private allocation-return hook.

`requires` names runtime capabilities needed by the callback. For example,
`frozenset({"fmu"})` prevents the rule from being serialized unless the FMU
backend is enabled. Standard capabilities are `core`, `introspection`,
`fuzzing`, and `fmu`; a host may add native-build capabilities through the
machine's public `virtual_capabilities` set.

## 3. Add Python preparation only when it is needed

Preparation is host-side work performed before `virtuals.txt` is written.
It is the right place to resolve a virtual's own argument syntax, inspect a
symbol, or create an input artifact. It is not the place to write the rules
file, mutate a machine, or add QEMU command-line options.

```python
from fastdyn.virtual_preprocessing import (
    VirtualContext,
    VirtualDefinition,
    VirtualPrepareResult,
    register_virtual,
)


class ExamplePreprocessor:
    def prepare(self, ctx: VirtualContext, args: list[str]) -> VirtualPrepareResult:
        if len(args) != 1:
            raise ValueError("example_virtual requires one symbol name")

        address = ctx.resolve_symbol(args[0])
        data = ctx.artifact_path("example/payload.txt")
        data.write_text(f"0x{address:x}\\n", encoding="utf-8")
        return VirtualPrepareResult(
            args=[f"0x{address:x}", str(data)],
            artifacts=[data],
        )


register_virtual(
    VirtualDefinition(name="example_virtual", prepare=ExamplePreprocessor())
)
```

`ctx.artifact_path()` creates a location below the run work directory and
rejects absolute paths and path traversal. The result's `args` are the final
tokens received by the C callback. Raise `VirtualPreparationError` for a
clear FastDyn configuration error. Other exceptions also stop preparation, but
may expose implementation detail rather than a clear configuration diagnostic.

Use only the documented context fields and methods: `binary`, `architecture`,
`machine`, `cpu`, `trigger_pc`, `workdir`, `symbols`, `irq_map`,
`capabilities`, `resolve_symbol()`, and `artifact_path()`. Do not depend on a
CPU object, TOML parser, global work-directory layout, or QEMU command line.

## 4. Add a run-wide feature

A run preprocessor is enabled for a CPU by its own predicate. It returns a
declarative plan: generated `VirtualInstruction` objects and artifacts.

```python
from fastdyn.machine import VirtualInstruction
from fastdyn.virtual_preprocessing import (
    ConfigurationHelp, RunContext, RunDefinition, RunPrepareResult,
    register_run_preprocessor,
)


class ExampleFeature:
    def prepare(self, ctx: RunContext) -> RunPrepareResult:
        schema = ctx.artifact_path("example/schema.txt")
        schema.write_text("schema", encoding="utf-8")
        return RunPrepareResult(
            virtuals=[VirtualInstruction("feature_hook", "example_virtual", [str(schema)])],
            artifacts=[schema],
        )


register_run_preprocessor(
    RunDefinition(
        name="example_feature",
        prepare=ExampleFeature(),
        enabled=lambda ctx: bool(ctx.settings.get("enabled", False)),
        help=ConfigurationHelp(
            description="Describe the firmware-wide feature.",
            toml='''[CPU.cpu0.plugins.example_feature]
enabled = true''',
            documentation="docs/ExamplePlugin.md",
        ),
    )
)
```

Generated rules are not special: FastDyn resolves their trigger addresses,
runs their virtual preprocessors, validates capabilities, and serializes them
with user rules. Two different virtuals cannot target the same PC because the
native dispatcher supports one callback there.

There is deliberately no `plugin_args` field. A virtual or run feature must
never add a QEMU `--plugin` argument: FastDyn owns the launch command and is
plugin agnostic. Put user settings in TOML, turn derived data into an artifact
with `ctx.artifact_path()`, and have the native component resolve that logical
artifact through FastDyn's generic native run-artifact API. The native
component must not require a generated command-line option.

For a run-wide module, FastDyn supplies only the settings from its TOML table,
a namespaced artifact allocator (`ctx.plugin_artifact_path()`), a standard
logger (`ctx.logger`), and generic cleanup callbacks returned in
`RunPrepareResult.cleanup`. Configure it with:

```toml
[CPU.cpu0.plugins.example_feature]
enabled = true
```

### Observing MMIO accesses

A native component can watch every MMIO access that FastDyn's device dispatch
sees, for example to detect a firmware polling a status register:

```c
static void on_mmio(const VirtualMmioAccess *access) {
    if (!access->handled) {
        /* no device model claimed access->address */
    }
}

static int example_initialize(const VirtualContext *ctx) {
    return virtual_register_mmio_observer(ctx, on_mmio);
}
```

The observer runs on the vCPU thread after the device model handled the
access, for reads and writes, handled or not: `value` is the value written or
returned (0 for an unhandled read), `handled` says whether a device model
claimed the address, and `in_mmio_window` whether the address lies in a
region FastDyn routes to device models (`0x40000000`-`0x5FFFFFFF`,
`0xE0000000`-`0xEFFFFFFF`). Observers run in registration order; keep them
cheap, since they run for every access. While no observer is registered the
check costs one branch per access.

## Out-of-tree plugins

A plugin can live in another project and still be a FastDyn plugin. Its folder
has the layout of `virtuals/<name>/`, and the plugin's table points at it:

```
my_project/fastdyn_plugins/mmio_count/
    host/preprocessor.py      # registers a plugin called "mmio_count"
    runtime/mmio_count.c      # optional native half
    runtime/mmio_count.so     # built from it (see below)
```

```toml
[CPU.cpu0.plugins.mmio_count]
enabled = true
source = "../my_project/fastdyn_plugins/mmio_count"   # relative to this config file
```

* FastDyn loads `host/preprocessor.py` like a built-in preprocessor. It must
  register a plugin with the table's name; `source` itself is not passed on
  as a setting.
* The native half uses the same SDK as built-in plugins (`fastdyn_runtime.h`)
  and exports one descriptor instead of `VIRTUAL_PLUGIN`:

  ```c
  static int mmio_count_initialize(const VirtualContext *ctx) { ... }
  FASTDYN_EXTERNAL_PLUGIN("mmio_count", mmio_count_initialize);
  ```

  Build it into `runtime/` with
  `fastdyn build-model --pkg fastdyn runtime/mmio_count.c -o runtime/`
  (outside a checkout, after `meson install`, `pkg-config fastdyn` provides
  the headers and library).
* FastDyn passes `runtime/*.so` to `libfastdyn.so` when the plugin is
  enabled, and loads it after its built-in plugins. A library that cannot be
  loaded, has no descriptor, was built for another plugin ABI, reuses a
  loaded plugin's name, or fails to initialize stops the run with a message
  naming it.
* A missing folder, a folder without `host/preprocessor.py`, a preprocessor
  that registers another name, or a built-in plugin's name is reported
  against `[CPU.cpu0.plugins.<name>].source`.

`tests/plugins/mmio_count/` is a complete example: its host half passes a
setting to its native half through an artifact, and the native half counts
MMIO accesses and translated blocks.

## 5. Configure and test it

Once the native and Python registrations are in the build, a user configures a
PC-triggered virtual normally:

```toml
[[CPU.cpu0.virtuals]]
at = "main+4"
instruction = "example_virtual"
args = ["some_symbol"]
```

Add focused unit tests without launching QEMU. Construct a small CPU-shaped
test double and call `prepare_virtual_rules()` for virtual behavior, or
`prepare_run_preprocessors()` for a firmware-wide feature. See
`tests/unit/test_virtual_preprocessing.py` for working examples of symbolic
IRQ conversion, capability validation, conflict handling, artifacts, and
shared QEMU serialization.

Then run:

```bash
./fastdyn-env/bin/python -m pytest -q tests/unit/test_virtual_preprocessing.py
./fastdyn-env/bin/python -m pytest -q tests/unit
```

Finally verify that the generated `<work-dir>/virtuals/virtuals.txt` contains
the expected trigger address, callback name, and final arguments, and test the
native callback with the relevant firmware.

## Preprocessor location

Compiled native code and its host-side preprocessor live together. A feature
directory has this shape:

```text
virtuals/example_feature/
  runtime/
    example_feature.c
  host/
    preprocessor.py
    # optional helpers: schema generation, analysis, UI, etc.
```

FastDyn scans `virtuals/*/host/preprocessor.py` and imports each file generically.
The file self-registers its stable TOML name; FastDyn never contains an
`if plugin_name == ...` dispatch. Keep all feature-specific host logic here as
well—schema planning, firmware analysis, internal hook selection, and a UI are
plugin code. It may use the documented generic FastDyn APIs, such as symbol
resolution and artifact allocation, but it does not belong under
`src/fastdyn`.

### Project-wide ownership convention

This boundary applies to every virtual and plugin:

```text
one feature only        -> virtuals/<feature>/
shared plugin helpers   -> virtuals/utils/
generic FastDyn support -> src/fastdyn/ (core-maintainer-owned)
```

Put a helper beside its feature by default. Move it to `virtuals/utils/` only
when two or more independently useful plugins need it. `virtuals/utils/` is
the shared host-side plugin utility package; it must not become a place for
feature-specific dispatch or frontend policy.

Do not put plugin- or RTOS-specific behavior, TOML interpretation, runtime
arguments, or feature registries in `src/fastdyn/`. If a plugin genuinely
needs a new generic frontend capability, SDK API, lifecycle hook, or loader
behavior, stop and request that addition from the FastDyn core maintainers.
Core maintainers own the generic contract; plugin authors own implementations
against that contract.

## Testing RTOS introspection without RTOS submodules

Do not add whole RTOS source trees as submodules merely to test an
introspector. Split coverage by cost:

1. Use small Python unit fixtures—symbol dictionaries plus a mocked
   `SchemaGenerator`—to test RTOS detection, hook selection, required symbols,
   generated schemas, and the declarative `RunPrepareResult`.
2. Keep one compact, redistributable ELF fixture per supported RTOS only when
   it is needed to verify real DWARF extraction. Store its source and build
   command beside the fixture so it can be regenerated; do not vendor the RTOS
   tree.
3. Test native callbacks in a host-side C harness with a fake
   `qemu_plugin_read_memory`/`qemu_plugin_write_memory` implementation. This
   validates task-list traversal without booting QEMU.
4. Reserve an optional QEMU smoke test for a separately supplied firmware
   artifact. It should validate end-to-end hooks, not every data-layout case.

An RTOS is only supported when all three layers exist: a detection signature,
a Python introspector that emits a schema and hooks, and native callbacks that
consume that schema. Detection alone must remain explicitly unsupported.
