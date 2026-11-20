# Host Analysis API

For a task-oriented preparation and standalone-tool walkthrough, see
[HostAnalysisRuntimeGuide.md](HostAnalysisRuntimeGuide.md).

`fastdyn.host` is the public Python entry point for firmware facts reused by
FastDyn preparation plugins and standalone host tools. It does not run inside
QEMU and it contains no plugin configuration, hook-selection policy, mutable
instrumentation plan, simulator samples, or physics state.

## Opening and sharing a session

```python
from fastdyn.host import AnalysisSession

with AnalysisSession.open("firmware.elf", architecture="arm",
                          instruction_mode="thumb-mclass") as analysis:
    reset = analysis.symbols.lookup("Reset_Handler", kind="function")
    first_instruction = analysis.image.read_bytes(reset.code_address, 4)
```

Opening a session is lazy: the path is resolved, but ELF and DWARF are not read
until a service is requested. A session treats its image as immutable. Replace
the firmware by opening a new session; a session does not poll or hash the file.

`RunContext.analysis` and `VirtualContext.analysis` expose the same session to
preprocessors for one CPU during one preparation run. Sessions are keyed by the
resolved firmware path, architecture, and instruction mode. The existing run
cleanup lifecycle closes the run-scoped pool. Standalone tools can use
`AnalysisSession.open()` directly or explicitly use `AnalysisSessionPool`.

## Image operations

`analysis.image.read(address, size)` returns `ImageRead`, containing the bytes
and `ImageChunk` records whose `source` is `file` or `zero-fill`. A read may be
`mixed`. Negative, overflowed, truncated, or unmapped reads raise
`ImageAddressError`. Bytes beyond `p_filesz` are zero-filled only while they are
inside the same load segment's `p_memsz`; unrelated file bytes are never read.

`read_bytes()` is the convenience form when the caller does not need provenance.

## Symbols and functions

`analysis.symbols` provides:

- `all(kind=...)` and `find(name, kind=...)` for plural queries;
- `lookup(name, kind=..., address=...)` for an intentionally singular query;
- `function_ranges()` and `containing_function(address)`.

`Symbol` records contain the raw address, target-normalized `code_address`,
kind, optional size, aliases, provider, and section. Thumb normalization is
applied only to function symbols for an ARM `thumb-mclass` session. Odd data
addresses and address zero are retained. Undefined symbols are excluded.
Singular lookups raise `SymbolNotFoundError` or `AmbiguousSymbolError` rather
than selecting an arbitrary definition.

`FunctionRange` reports `explicit` or `inferred` provenance. A zero-sized
function is bounded by the next function in its executable section, or by that
section's end. The API does not claim that an inferred range is compiler truth.

## DWARF operations

`analysis.dwarf` lazily indexes DWARF and supports:

- qualifier/typedef traversal and conservative `TypeResult` sizes;
- fixed `DW_OP_addr` globals and nested member paths;
- constant member offsets encoded as integers, `DW_OP_plus_uconst`,
  `DW_OP_constu`, or `DW_OP_consts`;
- fixed-address globals of a named type;
- source-location lookup.

Results use `None`, `supported=False`, and a reason for unknown information;
missing sizes are not represented as zero or guessed as four bytes. Address
width and byte order come from the ELF/DWARF target. Dynamic locations,
location lists, bit-field layout, and arbitrary DWARF expression evaluation
are intentionally unsupported. GDB-based C++ layout verification remains in
Courbet because replacing it with these narrower queries would lose behavior.

## Decoding and shared analyses

`analysis.decoder.instructions(address=None, size=None)` returns immutable
`DecodedInstruction` facts. Executable regions and fact queries are cached by
their parameters. `return_sites(start, size)` recognizes the existing Cortex-M
`bx ... lr`, `pop ... pc`, and `ldm ... pc` forms. `native_instructions()` is an
internal escape hatch used by specialized ObjectSan propagation logic that
still needs Capstone operands.

`analysis.constant_propagation.memory_accesses()` performs bounded,
control-flow-aware propagation of partial register constants and returns
immutable `KnownMemoryAccess` records when an instruction's effective address
is known. It handles Cortex-M vector/entry seeds, direct calls, literal pools,
`movw`/`movt`, register copies, immediate arithmetic, uncertain CFG merges,
and ABI clobbers. The result is deliberately address-class agnostic: a plugin
may classify the same facts as MMIO, RAM, protocol buffers, or another domain.
The unfiltered result is cached by the shared session. Raw-image tools can use
`find_known_memory_accesses()` or construct `ConstantPropagation` directly.

Decoding and session constant propagation currently support only
`architecture="arm"` with `instruction_mode="thumb-mclass"`; other combinations raise
`UnsupportedTargetError`. `mmiofinder` is a compatibility adapter that filters
the shared constant-propagation records by caller-supplied ranges. `LoopFinder`
and `next_pcs` retain their distinct APIs. Candidate memory accesses remain
conservative, including register-list transfers and accesses whose effective
address cannot be recovered.

## Resolution and RTOS metadata

`resolve_address(token, symbols)` resolves integers, decimal/hex literals, and
`symbol+/-offset`. Virtual/modifier/Courbet adapters retain their pre-existing
bare-hex, dereference, register, and callback-argument syntax.

`RTOS_SIGNATURES` and `RTOS_HINT_MARKERS` are shared data with deliberately
different policies: `identify_rtos()` requires an instrumentation-strength
signature, while `elf2config` continues to score individual hint markers.

## Errors and dependencies

All public failures derive from `HostAnalysisError`: `ImageAddressError`,
`SymbolNotFoundError`, `AmbiguousSymbolError`, `DwarfQueryError`, and
`UnsupportedTargetError`. Importing `fastdyn.host` requires only FastDyn's core
ELF/Capstone dependencies. It does not import GDB, Gazebo, plotting, FMI, FIRE,
or `world` packages.

`fastdyn.host.llvm_layout.gep_offsets()` is a separate, lazy host-model service.
It uses libLLVM to parse constant GEP indices and obtain byte offsets from the
module data layout. It returns unknown offsets when a proof is unavailable.
Only callers of this service need libLLVM; firmware sessions do not load it.

Plugin examples in the repository include Function Counter using
`ctx.analysis.symbols`, VariableWatch using fixed DWARF member paths and shared
function ranges, ObjectSan using decoded instructions while retaining its own
propagation and allocator policies, and Automatic MMIO classifying the cached
known-effective-address results for configured device ranges.
