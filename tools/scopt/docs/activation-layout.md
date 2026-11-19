# Placement from an ordinary restore trace

Use the activation-layout planner with an ordinary warm v5 snapshot and
a frozen, untimed QEMU x86-64 training trace. The planner requires the
same QEMU ELF with DWARF information for TranslationBlock and gdb to
read those layout offsets.

    scopt plan-activation-layout /path/to/warm-snapshot \
      --qemu /path/to/qemu-system-arm --config training.toml \
      --receipt training-trace/trace.json --protocol training-trace/protocol.json \
      --out placements.json --report projection.json

Pass --trace-manifest trace-input.json when training records each
benchmark marker as an instruction in an observed TB. The planner
checks both marker instruction positions, the final dispatch, and the
training launch identities against the snapshot, trace and receipt.

For older traces where the end callback sits inside the last dispatched
TB, pass --terminal-evidence with the trace SHA, exact plugin
SHA, last dispatch line and both PCs, and evidence that benchmark_end
prints and exits synchronously. The planner checks the end marker
against the actual captured guest instruction boundaries.

Apply the generated placement parameters through the existing validated
native linker and package pipeline.

    scopt optimize /path/to/warm-snapshot --qemu /path/to/qemu-system-arm \
      --pipeline activation-layout --params placements.json --store output-store

## Trace evidence

Collect a regular warm launch with QEMU -d exec and -D naming the raw
unfiltered trace. Preserve the staged cache and the launched marker
file. Verify exact guest output and zero appended cache records.
The recorded argv must identify this QEMU, that exact staging cache,
and the plugin with its virtual marker file. The configuration should
provide benchmark_start and benchmark_end for one vCPU.

The planner consumes the ordinary trace runner's trace.json with
ordinary_warm, argv, trace, trace_sha256, qemu_sha256, plugin_sha256,
exact_guest_oracle, input and proof fields. The proof records
verdict=PASS, file_version=5, appended=0, n, and record_count_after.
The protocol binds ordinary_warm, warm_cache_sha256, qemu_sha256,
plugin_sha256, expected_guest and the training input files. For a
single input use input_bank. For named banks use input_banks. For fixed
work without input files use a null input_bank or empty input_banks.
The frozen map gives
source paths and their SHA-256 digests and the purpose is
"untimed training only, outer dispatch/link-write trace".
The planner rehashes the trace, both cache copies, QEMU, the
plugin, configuration, oracle and input banks. The projection
receipt carries those identities and the chosen marker interval.

## Projection and limits

The model marks pages touched by first-activation TB state writes,
original reset branch fields, subsequent native link patches, and
the corresponding source and destination linkage metadata. It counts
repeat observed TB entries to pages that have changed since their
previous observed entry. It greedily places up to eight selected TB
entries on separate 16 KiB boundaries after the original arena's
occupied range, leaving room before each entry for its own writable
TB structure. Each step preserves or improves both the 4 KiB and
16 KiB model scores and strictly improves their sum.

QEMU's exec trace omits most directly chained executions. First
activation, code-page write coverage and observed entry reuse are
therefore proxies. Page placement also changes native locality and
arena footprint. The report is a training projection. Compare a
validated artifact with the exact original warm cache on independent
inputs before selecting it for deployment.
