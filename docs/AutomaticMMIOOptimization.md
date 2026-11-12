# Automatic MMIO optimization

FastDyn can derive pure device reads from ordinary elder model source, find
their call sites in stripped Cortex-M Thumb firmware, bind them to initialized
model instances, and replace the matching QEMU load. No PC list, modifier row,
or second register description is required.

The feature is an ordinary run-wide plugin. Its discovered `RunDefinition`
uses `RunContext.settings`, configured `device_ranges`, the shared
`ctx.analysis` session, and `plugin_artifact_path()`. The native
`VIRTUAL_PLUGIN("mmio_opt", ...)` consumes those artifacts through
`virtual_artifact_path()`, binds through the public post-device-init lifecycle,
and installs replacements with the typed runtime SDK. It adds no private QEMU
plugin arguments or feature branch to the target frontend.

## Build and run

From a clean checkout, build the ordered QEMU patch series and FastDyn:

```bash
sudo apt-get install clang-18 llvm-18 binutils-arm-none-eabi
git submodule update --init --depth 1 third_party/common/qemu
source ./setup.sh --build-qemu

fastdyn build-model --mmio-opt path/to/uart.c -o build/models
```

The model command uses Clang/LLVM for the host model, embeds the generated
operation table in the shared library, and writes
`build/models/uart.mmio.json`. Configure the resulting library as a normal
elder scroll and enable firmware discovery:

```toml
[CPU.cpu0.plugins.mmio_opt]
enabled = true
```

Analysis and optimized-model library builds both use `-fPIC -O1` before user
`CFLAGS`, so preprocessing/optimization options agree. In particular,
`__OPTIMIZE__`/`__PIC__` conditionals cannot select different callbacks in the
analyzed source and the actual library. The library links the exact analyzed
LLVM IR; the C preprocessor is run once. Changes to this IR force an incremental
rebuild even when generated operation metadata is unchanged, including changes
only to the initializer or slow callbacks. Ordinary builds without `--mmio-opt`
retain their usual flags.

`fastdyn run` writes these artifacts below the run directory:

- `run-artifacts/mmio_opt/discovery.json`: decoded accesses and preparation
  time, with encoded instruction size and memory transfer width kept separate.
- `run-artifacts/mmio_opt/pending-plan.txt`: the model-independent plan loaded
  during plugin initialization.
- `run-artifacts/mmio_opt/installation.json`: installed and declined sites,
  resolved device instances, hook IDs, and fast/fallback execution counts.

The complete checked-in demonstration is:

```bash
tests/integration/run_fastdyn_mmio_opt.sh
```

It builds the unmodified `tests/models/elder_echo.c`, assembles stripped Thumb
firmware, runs optimized and baseline configurations, validates the generated
reports, and prints the measured preparation and execution times. Override
`FASTDYN_QEMU_BIN`, `FASTDYN_PLUGIN_LIB`, or `FASTDYN_BIN` to select build
outputs. Set `FASTDYN_KEEP_TEST_TMP=1` to retain every generated artifact.
Alternatively set `FASTDYN_MMIO_ARTIFACTS=/absolute/new/directory` for a stable
artifact destination. CI uploads this directory as `mmio-opt-artifacts`.

The script also compiles ordinary `mmio_safety.c` and `mmio_predicates.c`
models through `build-model --mmio-opt`. It runs six baseline/optimized pairs:
Echo, state/effect/width safety, complete predicates, unaligned halfword fault,
the shipped `mmio_count` observer, and observer registration after a TB is
generated. Each pair retains the firmware ELF, model-build output, config,
firmware outcome, discovery and installation reports, timing records, and
compressed ordinary `io.log` traces. `summary.json` records observation policies
and separate model compilation, firmware discovery/plan, and execution costs.

## What is optimized

The bounded model pass starts at `fastdyn_model`, uses the host data layout,
and specializes read paths selected by an immutable address/base field or a
constant address. It currently emits constants, unsigned 8/16/32-bit state
loads, and bounded postfix arithmetic/bitwise computations. LLVM's C API parses
GEP types and applies the module's actual host data layout; TBAA is not used for
addresses. Every accessed byte must lie inside the proven returned allocation.
Initialization must return a single constant-sized malloc/calloc allocation
(or NULL), and pointer publication or unproved lifetime prevents live-state
operations.

Read paths use an explicit instruction admission list. Stores, volatile/atomic
operations, calls, fences, and unknown instructions decline the path, including
when their result is unused. Paths depending on PC/size or live non-address
conditions also decline. In particular, an unused call such as
`poll_backend(s); return s->status;` remains on the original device path.

Selector immutability is checked over byte ranges. Calls from write/read/IRQ
callbacks, unknown aliases, and escaping pointers defeat that proof; helper
writes are conservatively declined even if a more powerful interprocedural
analysis could prove them safe. Every admitted address predicate is retained
in generated model metadata and checked at binding. The bounded conjunction
has at most eight predicates; unsupported combinations decline. Rebuild model
libraries for generated metadata ABI 2.

Expressions retain 8/16/32-bit unsigned wraparound and masks for zero-extension
and truncation. Sign extension, wider arithmetic, dynamic shifts, shifts at or
above the operand width, and other unrepresentable forms decline. Narrow
overflow is masked before it can become a guest word result. Constants and
32-bit computations are evaluated by the existing 32-bit expression runtime.

The firmware pass follows vector and ELF entry points plus direct control flow.
It propagates partial constants through literal pools, `movw`/`movt`, register
copies, and immediate arithmetic, and drops facts at clobbers and uncertain
merges. That propagation is the shared, cached
`ctx.analysis.constant_propagation` service; this plugin owns only device-range
filtering and installation policy, so other preprocessors can reuse the same
known-effective-address facts. The first execution mode is Cortex-M Thumb,
little-endian scalar `LDR`, `LDRB`, and `LDRH`.

After `dev_init`, FastDyn resolves each operation against the actual elder
instance and its existing `opaque` allocation. QEMU emits a live effective
address guard at the original load. A match evaluates the operation directly
from that live allocation; a different address, declined operation, or enabled
MPU executes the original protected load. Decoder control still owns
predication, writeback, destination placement, PC advance, and instruction
accounting. The immutable registration survives TB invalidation and
retranslation without recapturing state.

Unaligned predicted addresses decline at installation. The decoder also
declines finalized MemOps requiring alignment checks, and generated guards
route any unaligned effective address through the original load with its
original flags. `CCR.UNALIGN_TRP` therefore retains its UsageFault/HardFault
behavior, including with the MPU disabled. Installation reports describe
registrations; decoder restrictions can keep a registered site entirely slow.
Fast and fallback counters count completed loads, excluding architectural
faults and QEMU's internal retry attempts.

MMIO observers active during binding decline every replacement. A live flag
also protects already generated code against later observer registration:
those reads immediately use ordinary dispatch. Observer registration and model
execution follow the serialized vCPU lifecycle; concurrent host-thread
registration is outside this milestone.

Automatic substitution is disabled for multi-CPU runs and for tracing,
replay, coverage, fuzzing, exact-budget, and related modes whose semantics
require the original path.

## Reproduced result

On the pinned QEMU checkout, the Echo workload installs 10 of 12 discovered
sites and completes 1,000,010 fast reads with two guard/MPU fallbacks. The
corrected counter is incremented after the original load, so retried MMIO
instructions no longer inflate fallback counts.

The no-observer timing comparison removes both dispatch and ordinary per-read
`io.log` writes. It is a combined-work measurement, not an isolated dispatch
speedup. Ordinary logging is enabled in both configurations; substituted
reads omit log entries. The two observer comparisons use equivalent observation
policies and keep all accesses on the ordinary path. Each measurement reports
its logging and observer settings. CI gates functional outcomes and counters,
with no machine-dependent timing threshold. Fresh measurements and outcomes
are retained in `summary.json` and the CI artifact; the checked-in reference is
`tests/integration/mmio_opt/reference-results.json`.
