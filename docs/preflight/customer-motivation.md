# Why firmware customers need a binary optimizer

## Executive argument

Firmware customers often cannot solve performance problems by recompiling. They
may have only a shipped image, an incomplete or mismatched source tree, and a
mixture of library and application optimization levels. They need optimization
of the exact binary that was deployed or supplied for analysis.

That creates a product gap between a conventional host binary optimizer and a
usable firmware binary optimizer:

1. BOLT supplies mature binary representation, analysis, rewriting, profiles
   and layout machinery, but the pinned implementation does not accept the
   surveyed ARM32/Thumb firmware.
2. STOKE supplies useful search and verification machinery on supported x86
   regions, but the selected implementation has no usable Cortex-M path.
3. Firmware brings fixed vectors, reserved regions, Thumb encodings, branch and
   literal reach, MMIO, interrupts, missing relocations and tight growth budgets
   that a host-oriented optimizer cannot infer from hotness.

Phase one is deliberately independent of FastDyn patches:

> Given the exact firmware image, target capabilities and firmware placement
> constraints, produce a qualified firmware candidate plus an exact origin map
> and transformation report—or retain the unchanged image with an explicit
> rejection reason.

Phase two is a separate FastDyn consumer. It imports patches only after a
firmware candidate exists, uses the phase-one origin map to rebind or reject
them, checks FastDyn-specific observations and measures emulator profitability.
The standalone optimizer neither parses patch syntax nor reasons about hook
actions.

This boundary keeps the firmware optimizer reusable for customers who do not
use FastDyn. It also prevents patch integration requirements from being
mistaken for missing optimizer fundamentals.

## Customer problem

### Binary-only and provenance-constrained inputs

Common firmware-analysis inputs are not clean compiler projects. Source may be
unavailable, legally or operationally difficult to rebuild, inconsistent with
the deployed image, or dependent on an obsolete toolchain. Even when source is
present, rebuilding can change the vulnerability under investigation.

The evaluation encountered this directly. The retained ESCape32 study described
a 64 KiB flash configuration, while the recovered pinned source declares 32
KiB. Rebuilding that source is useful evidence, but it is not a substitute for
optimizing the originally supplied binary. A product that starts from the exact
image avoids silently changing the baseline.

### Repeated execution makes offline preparation valuable

Firmware analysis, emulation, regression and fuzzing campaigns repeatedly
execute the same image. This is the setting in which offline optimization can
be economically useful: preparation is paid once, while execution savings can
be collected over many runs.

For preparation cost `C`, unchanged execution time `T_ref`, optimized execution
time `T_opt`, and `N` campaign executions, the optimization breaks even when:

```text
N × (T_ref - T_opt) > C
```

or equivalently:

```text
N > C / (T_ref - T_opt)
```

This formula also defines an honest stop condition. If `T_opt >= T_ref`, the
candidate is not profitable regardless of how attractive its native instruction
count looks. If the customer expects only a few executions, even a valid speedup
may not amortize search and verification. The phase-one candidate report should
record preparation cost and target-neutral cost evidence. A phase-two consumer
can add its own execution measurements and make the final decision using the
customer's expected run count.

### Firmware behavior has more observers than ordinary program output

A conventional equivalence relation is necessary but may not be sufficient for
firmware security analysis. The customer may care about:

- the address, width and order of guest memory accesses;
- a fault's original guest PC, access details and architectural recovery state;
- MMIO reads and writes, callbacks, DMA-visible state and interrupt
  opportunities;
- externally visible firmware and detector observations;
- modeled guest time and termination behavior.

These are not academic edge cases. Removing an unused invalid load can erase a
fault. Treating a polling MMIO read as ordinary invariant memory can alter
device behavior. Moving work across an interrupt opportunity can change shared
state even when ordinary function outputs agree.

The phase-one optimizer therefore needs a declared firmware correctness model,
not a vague promise to “preserve behavior.” It can support a narrow region class
first, but it must reject transformations when architectural or firmware effects
cannot be established.

FastDyn patch triggers, pre-hook state and multiplicity are additional phase-two
observers. They are intentionally not inputs to phase one.

### Each consumer has its own final cost

Phase one can report code size, instruction properties and any qualified target
cost, but it should not bake a FastDyn-specific objective into the optimizer.
When FastDyn consumes a candidate, it executes guest code through QEMU/TCG. A
shorter guest sequence can
translate into more host work, a different translation-block shape, additional
dispatch, or slower device/instrumentation paths. Hardware cycle counts and
native instruction counts are consequently insufficient acceptance metrics.

The MMIO evaluation demonstrates the importance of the execution environment.
The permissive echo case achieved a 24.22 baseline/optimized ratio, while a
normal observer forced the optimization to be declined and produced a 0.9985
ratio. A late observer caused 1,000,012 fallbacks and reduced the ratio to
1.1146. These are not preflight speedups, but they show that effect policy,
observer placement and fallback work can dominate profitability. A customer
phase-two adapter must score the candidate in the actual FastDyn execution
bundle. Other customers can provide hardware, simulator or code-size policies
without changing the phase-one artifact.

## Who benefits

### Firmware security and fuzzing teams

Their scarce resource is completed, semantically meaningful executions. They
benefit when a fixed image can be prepared once and then executed faster across
large malformed-input campaigns while retaining the firmware behavior they use
for discovery and attribution.

### Device vendors validating released firmware

They often need to analyze the shipped image rather than a nearby source build.
An image-bound bundle, exact origin map and explicit unsupported-region report
provide auditability that a source recompilation cannot.

### Reverse-engineering and incident-response teams

They regularly work with unknown or mixed optimization levels and incomplete
symbols or relocations. Conservative regional eligibility and unchanged-image
fallback let them gain value from understood code without requiring the entire
image to be recovered first.

### Emulator and analysis-platform operators

They need a stable image and origin artifact that can be consumed by their own
patch, device, instrumentation or cache layer. FirmwareCandidate v1 makes the
optimizer output versioned and content-addressed without coupling it to one
runtime.

## Product differentiation

### Exact-image optimization

The input is the customer's supplied image, not a reconstructed source build.
Starting optimization level can be O0 through Oz, mixed or unknown. Source is
optional evidence, not the ordinary compilation input.

### Stable provenance without consumer knowledge

Every emitted region carries exact provenance back to the original image.
Removed, duplicated, split and merged code must be represented explicitly. The
optimizer does not need to know whether a later consumer uses that provenance
for patches, profiling, coverage, crash attribution or debugging.

### Firmware-aware placement

The product understands reset/vector entries, reserved flash ranges, literal
references, branch reach, mode bits and code-growth budgets. The ESCape32
reproduction illustrates why this matters: under the recovered 32 KiB linker
configuration, the Os image has only 1,544 bytes of ROM headroom. Growth must be
checked after branch repair and veneers, not only on the proposed instruction
sequence.

### Firmware correctness validation

Phase-one acceptance checks the declared architectural and firmware effects,
placement constraints and budget. Unknown or unsupported cases retain the
reference and receive stable status codes such as `unsupported`, `unknown`,
`counterexample`, `over_budget` and `not_profitable`.

### Consumer-specific integration in phase two

The FastDyn adapter can evaluate generated host work and full-harness time under
the same patches, inputs, device model, instrumentation and schedule. It can
rebind patches through exact provenance or reject the candidate. This policy is
not part of the standalone optimizer. Profile data can prioritize phase-one
work, but it cannot redefine valid firmware inputs or prove that a cold path is
unreachable.

### Auditable artifacts

Phase one publishes the image, target, memory/layout evidence, origin map and
optimization report as FirmwareCandidate v1. Phase two publishes patch bindings
and FastDyn environment identity separately as ExecutionBundle v1. This protects
downstream consumers without making the optimizer depend on them.

## What the completed evaluation establishes

### A rehosted O0 application isolates design gaps from ISA engineering

The LEMIX artifact provides the missing controlled experiment. We compiled its
ported FreeRTOS SmartSpeaker application as an O0 x86-64 ELF and applied BOLT
and STOKE to that exact executable. Because x86-64 is already supported, the
result cannot be dismissed as only a missing Cortex-M backend.

Trained BOLT profiled 194 of 1,337 functions and predicted 3.1% fewer executed
instructions and 6.4% fewer branches. Across 11 interleaved five-second samples,
however, both the reference and optimized application completed exactly 500
interrupt-task dispatches. The median reference/BOLT CPU ratio was 0.9987 with
a bootstrap 95% interval of 0.8978--1.0743, while text grew 18.24%. The honest
outcome is neutral: BOLT improved its internal layout model but did not improve
the measured customer work.

The STOKE integration gap was sharper. BOLT identified 23 hot, loop-free,
call-free structural candidates covering 14.13% of profiled score, but direct
STOKE extraction emitted only 9 of 1,332 attempted application functions. Only
one of the 23 hot candidates reached search, and 10 million proposals found no
new correct rewrite for its redundant O0 accessor sequence. This supports a
BOLT-owned body exporter, precise effect normalization and a deterministic O0
canonicalization baseline before stochastic search. It does not establish a
general limitation of the STOKE lineage.

The experiment uses a zero-backed fixed-address mapping rather than a device
model, so it is not a firmware-fidelity or Cortex-M-speed result. Its value is
more focused: architecture support is not the only missing piece. Objective,
code growth, security-path coverage, extraction and effect representation all
remain product work on a supported ISA. The full method and limitations are in
[`lemix-o0-evaluation.md`](lemix-o0-evaluation.md).

### There is plausible optimization headroom

Across the reconstructed x86-native corpus, higher compiler optimization levels
were faster than O0 for every individual function in the measured configuration.
Individual O0/O3 ratios ranged from roughly 1.38 to 2.55, while the combined
harness exposed still larger compiler effects. This establishes that the source
computations contain optimization opportunities. It does not establish that
BOLT, STOKE or a Cortex-M binary optimizer can realize those gains.

### Existing host optimization is not enough

Trained BOLT improved the combined reconstructed host workload by 2.66% relative
to the restricted BOLT rewrite, showing that correct profile configuration
matters. However, it remained 3.0% slower than the original O0 binary, and
individual functions included both gains and regressions. A separate simple
x86 control measured a 1.44% benefit. The customer product therefore needs
candidate-level profitability checks rather than assuming that enabling BOLT is
beneficial.

The completed Firm-BOLT comparison sharpens that conclusion. The same FreeRTOS
POSIX application was built at O0 and O3, while Firm-BOLT trained only on the
exact O0 binary. Across 30 randomized paired two-million-message blocks,
O3/Firm-BOLT CPU cost was 0.9768 [0.9723, 0.9805]: Firm-BOLT remained 2.38%
behind source O3 but passed the frozen 5% parity rule. More importantly,
stock-BOLT/Firm-BOLT was 1.0273 [1.0252, 1.0329], so the firmware wrapper and
ordinary FOP policy beat the exact trained stock-BOLT baseline by 2.73% while
all output checks matched. O3 remains a performance reference, not evidence
that recompiling source preserves the supplied binary's behavior.

### Reuse remains the correct first choice

The pinned STOKE engine successfully searched, bounded-verified and installed
the upstream x86 popcount rewrite. BOLT's `StokeInfo` exported eligibility and
dataflow information for all reconstructed functions. These results justify an
adapter-first strategy on supported regions.

The recovered corpus also identifies concrete adapter gaps: five memory-heavy
regions could not obtain autogenerated testcases, Pin-based tracing is
incompatible with the current host, and the three regions that reached search
produced no replacement within the fixed budget. These outcomes justify
modernizing testcase capture and state construction. They do not justify a new
search algorithm.

### Cortex-M support is a genuine product milestone

Both surveyed M0+ and M3 ELFs are rejected by the pinned BOLT executable as an
unrecognized machine. The selected STOKE engine reports an unknown architecture
and emits empty extraction directories for both. Neither result is a semantic
counterexample; together they establish the missing target integration.

The target work is also larger than adding a decoder. The Os ESCape32 image has
1,154 PC-relative loads and 1,747 branches within sized function ranges. The Os
M3 image has 3,497 wide Thumb instructions and firmware exception-return
semantics. Neither target ELF retains relocation sections. Reference recovery,
feature gating, branch/literal repair, entries, reservations and exception
boundaries are therefore customer-critical backend work.

The bounded Firm-BOLT fallback now supplies one positive target result without
pretending that this missing backend has disappeared. On the original runnable
Cortex-M3 FreeRTOS image, profile-guided hot-exit layout reorders 34 exact
instruction identities inside an existing 92-byte function and adds no bytes.
It reduces the common translated path from three blocks to two, from 72 to 61
optimized TCG operations and from 768 to 696 host bytes. Thirty randomized
paired runs of 100,000 completed SysTick returns produce a reference/candidate
CPU ratio of 1.0552 [1.0392, 1.0609], with matching 2 MiB RAM prefixes in every
block. This establishes a bounded FastDyn/QEMU speedup for one image, region
contract and schedule. It does not establish a general Thumb backend, hardware
speed, deadline preservation or behavior under arbitrary device schedules.

### The product is not merely a Cortex-M port

The source audit makes the differentiation sharper. In a declared matrix of ten
firmware requirements, BOLT covers two natively, offers reusable but incomplete
machinery for three, and does not represent five. The selected STOKE engine
covers one natively, one through adaptation, and lacks or uses a different model
for eight. These counts describe the pinned implementations against our
firmware contract; they are not general quality rankings.

An instruction-set port solves decoding, encoding and candidate vocabulary. It
does not solve fixed placement, reset/vector behavior, relocation-free literal
recovery, device effects, interrupts, fault attribution or the cost of executing
the result on the customer's target. The selected STOKE state contains x86
registers, flags and ordinary memory but no device transaction history or
interrupt schedule. Two firmware executions can therefore be equal in the
represented state while differing in externally visible behavior. Its latency
objectives also sum Haswell instruction latency, which cannot rank Cortex-M or
emulated firmware candidates without replacement.

BOLT has the more suitable architecture for our product: its binary model,
CFGs, profiles and pass manager can be retained while firmware policies are
added around them. But BOLT supplies rewriting, not a firmware equivalence
proof. Our customer value is the missing control plane that makes those
transformations admissible: target qualification, compact fixed-placement
emission, effect boundaries, explicit unknown/rejection statuses and a
target-relevant profitability gate.

This is a more durable position than selling “BOLT for Thumb.” It applies to
other constrained targets and to already-linked images whose correctness
depends on hardware interactions rather than only function return values. The
complete argument, source anchors and quantitative matrix are retained in
[`design-fit-audit.md`](design-fit-audit.md).

### The phase-two FastDyn preservation prototype is useful but separate

The experimental native BOLT guard accepts exact instruction anchors, protects
their functions and rejects invalid anchors and clone policy. The execution
bundle binds an image to its patch and FastDyn environment. Twenty-four tests
exercise moved emitted addresses, modified hook state, removed faulting
accesses, duplicated triggers, event prefixes, device/callback/detector effects,
coverage, interrupts, guest time, termination and explicit status taxonomy.

These components belong to phase two. They are evidence that a later FastDyn
adapter is feasible, not interfaces that the phase-one optimizer must implement.

What remains is equally important: QEMU does not yet emit the normalized trace
needed for reference/candidate comparison, so the current checker is an offline
service rather than an end-to-end preservation proof.

## Product roadmap framed as customer value

### Stage 0: patch-independent FirmwareCandidate

Customer value: a reproducible exact image, target qualification, memory/layout
evidence, origin map and optimization report that any runtime can consume.

Exit condition: an identity candidate for the unchanged image publishes and
reloads with every artifact hash validated and no patch/runtime inputs.

### Stage 1: supported-ISA standalone optimization

Customer value: an end-to-end BOLT/STOKE adapter can be evaluated without the
uncertainty of a new target backend.

Scope: x86-64 fixtures or another BOLT-supported firmware configuration. Import
BOLT eligibility, generate valid states, invoke existing search/checking,
import a candidate, validate the phase-one firmware contract and publish a
FirmwareCandidate atomically.

Exit condition: at least one installed candidate is correct under the declared
phase-one contract and beneficial under a frozen standalone metric; rejected
candidates retain the reference.

### Stage 2: bounded Thumb backend gate

Customer value: determines whether Cortex-M can become a supported product
without promising arbitrary firmware.

Scope: one synthetic ARMv6-M ELF, a qualified leaf region, exact vector/reset
preservation, one branch-reach boundary, one PC-relative literal boundary and
post-repair code-size checking. No MMIO or exception paths are transformed.

Exit condition: decode, rewrite, emit and execute successfully with exact origin
output. Failure within the allocated budget stops broad MCU claims.

### Stage 3: one real firmware subset

Customer value: first evidence that the standalone optimizer improves an actual
firmware workload without source recompilation.

Scope: one pinned target/configuration and preselected region class. Unknown
references, exception paths, MMIO and unsupported effects remain unchanged and
are counted in the coverage report.

Exit condition: an accepted candidate satisfies firmware placement and
correctness checks and improves a frozen size, simulator, emulator or hardware
metric after code growth and preparation cost are included.

Status: achieved for the bounded Cortex-M3 hot-exit region under FastDyn/QEMU.
The stage remains open for broader firmware classes because alternate entries,
general branch/literal repair, exception semantics and device/MMIO effects are
still explicit unsupported boundaries.

### Stage 4: FastDyn patch integration

Customer value: FastDyn can consume the same standalone FirmwareCandidate while
adding exact patch rebinding, hook semantics, instrumentation/device identity,
event comparison and TCG/full-harness profitability.

Scope: phase two reads the origin map and transformation report. It never
retroactively assumes that phase one knew about a patch. If a binding was
removed, duplicated or changed incompatibly, the adapter rejects the candidate
and uses the reference image. A later constrained rerun can express generic
protected ranges without teaching the optimizer FastDyn patch syntax.

Exit condition: an optimized candidate and the unchanged reference execute
under identical FastDyn inputs; bindings validate, declared observations agree,
and the optimized execution is profitable.

### Stage 5: expand only from measured blockers

Customer value: each new target feature or semantic rule corresponds to a real
workload need rather than an open-ended compiler project.

The gap table should drive prioritization: add one extension for each reproduced
unsupported instruction, missing reference form, proof limitation or measured
bottleneck. Missed optimization, unsupported input, inconclusive proof and
nonprofitability remain different product outcomes.

## Honest positioning

We can currently say:

- the exact-image firmware optimization problem is real and not covered by the
  selected stock BOLT/STOKE target integration;
- reusable host machinery works on a supported control;
- target/backend and semantic gaps have concrete reproducers;
- a patch-independent FirmwareCandidate boundary exists;
- a separate FastDyn integration prototype shows how consumer-specific patch
  and observer policy can be added later.

We cannot currently say:

- the pinned upstream BOLT or STOKE implements a general Cortex-M optimizer;
- the one accepted FastDyn/QEMU result predicts hardware cycles, deadlines or
  other firmware/configuration gains;
- the finite preservation tests prove retention of unknown vulnerabilities;
- native host-function ratios predict MCU or TCG speedup;
- timeouts or unsupported inputs prove that STOKE's broader lineage cannot
  optimize the routines.

That positioning is commercially stronger than an unsupported speedup claim.
It gives customers a reusable firmware optimizer first, then a measurable and
optional path to FastDyn patch integration.
