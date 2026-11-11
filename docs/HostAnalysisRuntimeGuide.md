# Host Analysis Runtime Guide

This guide explains how FastDyn users and plugin authors use host analysis
during preparation. Despite the name, this API does **not** execute in the
emulated guest or the native QEMU plugin. It reads an immutable firmware image
before QEMU starts and turns reusable facts into plugin-owned rules and
artifacts.

For the complete operation and result reference, see
[HostAnalysisAPI.md](HostAnalysisAPI.md). For extraction decisions and test
results, see [HostAnalysisMigration.md](HostAnalysisMigration.md).

## Normal FastDyn runs

No user configuration is required to enable host analysis. When an enabled
plugin such as Function Counter, Function Tracer, VariableWatch, ObjectSan,
Introspection, World, or Automatic MMIO needs firmware facts, FastDyn creates
a lazy analysis session for that CPU.

```toml
[CPU.cpu0.plugins.function_counter]
enabled = true
include = ["main", "sensor_*"]
```

The plugin obtains function entries from the shared session, applies its own
include/exclude policy, and writes its existing manifest. If another enabled
plugin examines the same firmware and target, it reuses the session's symbol,
DWARF, and decoded-region indexes. Plugin settings and mutable instrumentation
plans are never stored in the session.

Generated artifacts remain under the normal work directory:

```text
<workdir>/run-artifacts/<plugin>/...
```

The native runtime inputs and callback behavior are unchanged. In particular,
the dispatcher still permits one callback per PC and reports conflicts after
all user and generated rules have been prepared.

## Using analysis in a preprocessor

Run and virtual preprocessors receive the shared session through
`ctx.analysis`:

```python
class ExampleRunPreprocessor:
    def prepare(self, ctx):
        entry = ctx.analysis.symbols.lookup("main", kind="function")
        owner = ctx.analysis.symbols.containing_function(entry.code_address)

        manifest = ctx.plugin_artifact_path("entry.tsv")
        manifest.write_text(
            f"0x{entry.code_address:x}\t{owner.name if owner else entry.name}\n",
            encoding="utf-8",
        )
        return RunPrepareResult(artifacts=[manifest])
```

Use shared services only for facts. Keep these decisions in the plugin:

- which functions, objects, or fields to select;
- which hooks to install and what arguments they receive;
- ABI placement, rendering limits, and diagnostics;
- manifest and runtime-schema formats;
- plugin-specific propagation/selection, allocation, RTOS-hook, and
  firmware-profile policy (shared immutable dataflow facts still belong in
  `ctx.analysis`).

Creating a context or reading `ctx.analysis` does not parse DWARF. Parsing is
triggered only when a service such as `symbols` or `dwarf` is requested.

## Standalone host tools

Tools outside preprocessing can open a session directly. Always specify the
instruction mode; an architecture family such as `arm` does not imply
Cortex-M or Thumb.

```python
from fastdyn.host import AnalysisSession

with AnalysisSession.open(
    "build/firmware.elf",
    architecture="arm",
    instruction_mode="thumb-mclass",
) as analysis:
    for function in analysis.symbols.function_ranges():
        print(function.name, hex(function.start), function.size,
              function.range_provenance)
```

Reusable whole-firmware dataflow facts are also session services. For example,
a preprocessor that needs statically known effective addresses should consume
`analysis.constant_propagation.memory_accesses()` and apply its own address
classification, rather than embedding a second register propagator. The same
cached records can support MMIO discovery, RAM/protocol-buffer analysis, or
other plugins.

The currently supported decoder target is ARM Thumb/M-profile. Image, symbol,
and DWARF queries can still be used for other ELF targets without invoking the
decoder, but callers must provide target properties that match the image.

## Numeric-only workflows

Numeric configuration does not require a readable ELF merely because a lazy
session exists. For example, a World pin with a numeric `at` value and a raw
VariableWatch range retain their existing numeric paths. The image is opened
only if a plugin requests symbols, DWARF, image bytes, or decoding.

```toml
[[CPU.cpu0.plugins.world.pins]]
at = "0x08001234"
kind = "digital_out"
endpoint = "motor_enable"
register = "r0"
```

## Symbol lookup rules

Use `code_address` for function hooks and `address` for data objects:

```python
function = analysis.symbols.lookup("worker", kind="function")
hook_pc = function.code_address

state = analysis.symbols.lookup("device_state", kind="object")
data_address = state.address
```

This matters on Cortex-M: function pointers may carry the Thumb bit, while an
odd data address must not be modified. Undefined symbols are unresolved,
address zero is retained, and a singular lookup fails if the name has multiple
distinct definitions. Use `find()` for an intentionally plural query or add
`kind`/`address` qualification.

Zero-sized function ranges are inferred only within their executable section.
Check `range_provenance` before treating a range as authoritative.

## Image reads

Use `read()` when the distinction between initialized bytes and zero-filled
memory matters:

```python
result = analysis.image.read(address, size)
if result.source != "file":
    raise ValueError("instruction validation requires file-backed bytes")
```

`source` is `file`, `zero-fill`, or `mixed`. Unmapped reads raise
`ImageAddressError`; the API never continues into unrelated file data merely
because an address is inside `p_memsz`.

## DWARF queries

Fixed globals and nested constant member paths return typed results:

```python
item = analysis.dwarf.global_variable("motor_state.temperature")
if item is None or item.type.size is None:
    raise ValueError("variable has no supported fixed layout")
print(hex(item.address), item.type.size)
```

Unknown sizes and unsupported locations are explicit. The API does not guess a
four-byte size, evaluate dynamic locations, or replace Courbet's GDB-based C++
layout verification.

## Cleanup and image replacement

FastDyn closes shared sessions through the existing run cleanup list. A
standalone tool should use a `with` block or call `close()`.

The firmware image is immutable for the life of a session. If a build replaces
the ELF, close the old session and open a new one. Do not reuse the old session
or mutate its cached results.

## Troubleshooting

- `AmbiguousSymbolError`: qualify the lookup or use `find()` and apply an
  explicit consumer policy.
- `SymbolNotFoundError`: the symbol is undefined, stripped, or absent from the
  selected image.
- `DwarfQueryError`: use an ELF with DWARF or choose a numeric/symbol-table
  workflow that does not require layout metadata.
- `UnsupportedTargetError`: select the exact supported instruction mode; do
  not map an arbitrary ARM target to `thumb-mclass`.
- `ImageAddressError`: verify the requested range is covered by a `PT_LOAD`
  segment and whether the consumer permits zero-filled memory.

Host analysis imports do not load Gazebo, GDB, plotting, FMI, FIRE, or World
packages. Failures in those optional domains remain isolated to their owning
tools and plugins.
