# FastDyn runtime plugin SDK

This directory contains compiled-in FastDyn runtime modules. A module may have
two independent halves:

```text
virtuals/my_feature/
  host/preprocessor.py    optional host-side TOML preparation
  runtime/my_feature.c    QEMU-plugin callback and runtime initialization
  meson.build             compiles the runtime source
```

The host preprocessor is discovered from `host/preprocessor.py`; see
[`docs/WritingVirtuals.md`](../docs/WritingVirtuals.md) for the Python side.
The C runtime side uses [`include/fastdyn_runtime.h`](../include/fastdyn_runtime.h).

## Runtime module contract

Do not add an initializer call, plugin name, or callback entry to
`virtuals/virtuals.c`. Declare a runtime module in its own source file:

```c
#include <fastdyn_runtime.h>
#include <fastdyn/arch/arm_v7m.h>

static const VirtualContext *runtime;

static void example_hook(unsigned int cpu_index, void *userdata)
{
    uint32_t input = virtual_read_register(runtime, VIRTUAL_ARM_V7M_R0);
    uint32_t output;
    virtual_read_memory(runtime, input, sizeof(output), &output);
    virtual_write_register(runtime, VIRTUAL_ARM_V7M_R0, output + 1);
    virtual_log(runtime, VIRTUAL_LOG_DEBUG, "copied 0x%x", input);
    (void)cpu_index;
    /* `userdata` is the prepared argument string from virtuals.txt. */
}

static void write_results(void)
{
    /* Write any final runtime state here. */
}

static int example_runtime_init(const VirtualContext *ctx)
{
    char result_path[4096];

    runtime = ctx; /* Context remains valid for the plugin lifetime. */
    if (virtual_register_callback(ctx, "example_hook",
                                         example_hook) != 0) {
        return -1;
    }
    if (virtual_artifact_path(ctx, "results.tsv", result_path,
                                      sizeof(result_path)) != 0) {
        return -1;
    }
    virtual_register_exit(ctx, write_results);
    return 0;
}

VIRTUAL_PLUGIN("example", example_runtime_init);
```

`VIRTUAL_PLUGIN` places the module in FastDyn's compiled runtime
registry. FastDyn invokes it only after the normal callback registry and the
run-artifact root are ready. The supplied context is stable for the plugin
lifetime and is intentionally small.

## Complete runtime API

Every SDK operation accepts the plugin context. It identifies the owning
plugin and remains valid for the QEMU-plugin lifetime, so a module may retain
it in a `static const VirtualContext *runtime` variable for use by its
callbacks.

### Lifecycle and artifacts

| API | Purpose |
| --- | --- |
| `virtual_register_callback(ctx, name, callback)` | Register a callback name consumed by generated or user virtual rules. |
| `virtual_artifact_path(ctx, relative, out, size)` | Resolve a safe path below `run-artifacts/<plugin-name>/`. Absolute paths and traversal are rejected. |
| `virtual_register_exit(ctx, callback)` | Write results or release runtime state when QEMU exits. |
| `virtual_register_post_device_init(ctx, callback)` | Defer binding that requires initialized device instances; runs before guest translation. |
| `virtual_set_actuator_pwm(ctx, channel, pwm)` | Send a PWM actuator value to the configured physics backend. |
| `virtual_log(ctx, level, format, ...)` | Emit a flushed, plugin-qualified runtime diagnostic. Levels are `VIRTUAL_LOG_DEBUG`, `VIRTUAL_LOG_INFO`, `VIRTUAL_LOG_WARN`, and `VIRTUAL_LOG_ERROR`. |
| `virtual_wait_for_trace_drain(ctx)` | Wait until FastDyn's inline trace logger has consumed its current records. Only needed when a result depends on coverage/trace output. |

### Guest state from a virtual callback

These APIs are intended for a virtual callback, IRQ hook, or translated-block
hook executing in QEMU's runtime context.

| API | Result |
| --- | --- |
| `virtual_pc(ctx)` / `virtual_sp(ctx)` | Current guest program counter or stack pointer. |
| `virtual_icount(ctx)` | FastDyn guest instruction count. |
| `virtual_guest_time_ns(ctx)` | QEMU virtual time in nanoseconds. |
| `virtual_read_register_bytes(ctx, reg, buffer, size, value_size)` | Read the complete target register in target byte order. `*value_size` receives its actual width. |
| `virtual_write_register_bytes(ctx, reg, buffer, size)` | Write the complete target-register representation in target byte order. |
| `virtual_read_register(ctx, reg)` | Read a 32-bit register, or the low 32 bits of a wider register. |
| `virtual_write_register(ctx, reg, value)` | Write a 32-bit register or low 32-bit value. |
| `virtual_read_memory(ctx, address, size, buffer)` | Copy guest RAM into `buffer`; returns `0` on success. |
| `virtual_write_memory(ctx, address, size, buffer)` | Copy `buffer` into guest RAM; returns `0` on success. |
| `virtual_raise_irq(ctx, irq, level)` | Raise or level-assert a guest IRQ using FastDyn's QEMU integration. |

Register numbers and calling conventions are architecture-specific. Include
the matching public header rather than using bare numbers:

| Guest architecture | Header |
| --- | --- |
| ARM A/R 32-bit | `<fastdyn/arch/arm32.h>` |
| ARM Cortex-M | `<fastdyn/arch/arm_v7m.h>` |
| AArch64 | `<fastdyn/arch/aarch64.h>` |
| RISC-V RV64 | `<fastdyn/arch/riscv64.h>` |
| x86-64 | `<fastdyn/arch/x86_64.h>` |

The constants are the stable QEMU GDB-register indices used by FastDyn, not
host register numbers. A callback hooked at a C function prologue sees that
target's ABI argument registers; for example, ARM AAPCS places the first
argument in `VIRTUAL_ARM_V7M_R0`, while the RV64 ABI uses
`VIRTUAL_RISCV64_A0`.

Each architecture header also supplies ABI convenience macros, so a virtual
that only needs ordinary C ABI registers can use `VIRTUAL_FIRST_ARG`,
`VIRTUAL_SECOND_ARG`, `VIRTUAL_RETURN_VALUE`, `VIRTUAL_STACK_POINTER`, and
`VIRTUAL_PROGRAM_COUNTER` without remembering the target register names.
Headers define as many argument macros as their ABI supplies and set
`VIRTUAL_ARGUMENT_COUNT`. ARM32 provides four, AArch64 and RV64 eight, and
x86-64 six. The x86-64 aliases use the System V AMD64 ABI (`RDI`, `RSI`,
`RDX`, `RCX`, `R8`, `R9`); they do not model the Windows x64 ABI. Architectures
with a link register also expose `VIRTUAL_LINK_REGISTER`. Include only one
architecture ABI header in a runtime source file.

### Dynamic instrumentation

| API | Purpose |
| --- | --- |
| `virtual_register_irq_hook(ctx, entry, exit)` | Observe guest IRQ entry and exit. Either callback may be `NULL`. |
| `virtual_register_tb_translation_hook(ctx, callback)` | Inspect or instrument translated QEMU basic blocks. The callback receives QEMU's `qemu_plugin_tb` object. |
| `virtual_register_rule(ctx, address, callback, args)` | Install a runtime-created PC callback. `args` is copied by FastDyn. Use this only for rules the runtime itself derives; host-generated rules should be returned from the Python preprocessor. |
| `virtual_register_update(ctx, address, reg, value)` | Add a runtime-created register update at a guest PC. |
| `virtual_register_gated_modifier(ctx, address, patch, enabled)` | Add a PC modifier enabled while `*enabled` is nonzero. The gate is checked at guest runtime. |
| `virtual_register_mmio_replacement(ctx, replacement, hook_id)` | Install a typed guarded load replacement during post-device initialization. |

The runtime module must not call `virtual_register()`,
`core_get_run_artifact_path()`, `qemu_get_register()`, or `qemu_set_register()`
directly. It must not parse FastDyn/QEMU plugin arguments or require a
feature-specific command-line option. The SDK is the stable FastDyn layer;
the raw QEMU plugin API remains appropriate only for QEMU objects deliberately
passed to a translation hook.

The host preprocessor owns creation of any input artifacts. The runtime module
finds them by their local logical name. For example, the function-counter
plugin creates `run-artifacts/function_counter/functions.tsv` on the host,
then its C module resolves `functions.tsv` through the context.

For output, resolve a local artifact name with `virtual_artifact_path()` and
open the returned path with normal C file I/O. This is the only supported way
for a runtime module to address persistent run storage; it keeps each module
inside its own artifact directory. For diagnostics, use `virtual_log()` rather
than `printf()` or a private log file.

## Complete examples

- [`function_counter/`](function_counter/) is a compact module that registers
  a callback, reads a manifest, and writes final counts on exit.
- [`function_tracer/`](function_tracer/) extends that pattern with a
  DWARF-derived entry-argument manifest and structured trace output.
- [`introspection/`](introspection/) is a larger run-wide module that loads a
  host-generated schema and emits structured activity events.
- [`mmio_opt/`](mmio_opt/) combines shared host analysis, namespaced artifacts,
  post-device binding, and guarded generated-code replacement without private
  frontend or plugin arguments.
