# Firmware design-fit audit of BOLT and STOKE

Recorded 16 November 2026 against BOLT
`7864c0736a8a09670dfbfc513f1e9649799f2486` and STOKE
`98d8a0f028f2daf2052bfe607dbc32ec8d55ba9e`.

## Finding

The missing ARM32/Thumb implementations are only the first blocker. Porting the
instruction decoder, encoder and search vocabulary would not make the selected
tools a firmware optimizer by itself. The audited designs do not natively
express fixed firmware placement, reset/vector and exception semantics,
effectful MMIO, interrupt schedules, firmware fault attribution or a
firmware-relevant execution cost.

This is not a claim that BOLT is badly engineered or that the STOKE lineage is
incapable of extension. BOLT remains the appropriate rewriting substrate.
The narrower, source-supported claim is:

> In the pinned implementations, firmware correctness and profitability are
> outside the information represented by the native transformation,
> equivalence and cost interfaces. ISA support is necessary but insufficient;
> firmware-specific models and acceptance services are required.

The mismatch is deeper in STOKE than in BOLT. BOLT is an extensible binary
rewriter whose transformations can be constrained by new services. The
selected STOKE engine bakes x86 user-level state and Haswell cost into the
search/checking path, so a Cortex-M port must replace semantic and cost
abstractions as well as instruction syntax.

## Supported-ISA validation of the design result

The LEMIX SmartSpeaker experiment tests the design claim without waiting for a
Thumb backend. The official firmware port was compiled at O0 as x86-64, a
native target for both tools. BOLT successfully decoded, instrumented and
rewrote it, so target architecture cannot explain the following results:

- only 194/1,337 functions received profile data from the scheduled training
  run;
- BOLT's model predicted 3.1% fewer executed instructions and 6.4% fewer
  branches, yet every reference and candidate sample completed the same 500
  dispatches and the CPU-time ratio was neutral at 0.9987 (95% bootstrap
  0.8978--1.0743);
- the trained rewrite increased text by 18.24%;
- `StokeInfo`'s first structural screen admitted 23/194 hot functions and 4.54%
  of hot static instructions;
- direct STOKE extraction emitted 9/1,332 attempted functions, with padding
  syntax dominating failures; and
- the one hot candidate reaching search obtained no rewrite after 10 million
  proposals.

These observations validate a narrower design statement than “the tools are
not designed for firmware.” BOLT's speed-oriented host layout objective does
not natively join completed event-driven work with firmware text/placement.
`StokeInfo`'s conservative store classification does not distinguish an O0
stack spill from external state, and the pinned STOKE whole-ELF extractor does
not compose robustly with this application. None of these gaps is repaired by
adding Thumb opcodes.

The result is not a universal limitation theorem. BOLT is intentionally a host
binary optimizer, and other STOKE configurations or later engines may cover
more code. It is a constructive design proof for our project: a conforming
firmware optimizer must add information absent from these interfaces, and an
end-to-end adapter must be measured before a target port can be credited with
customer value. Full measurements are in
[`lemix-o0-evaluation.md`](lemix-o0-evaluation.md).

## Necessary-capability matrix

The matrix uses ten requirements selected before judging the tools. `native`
means the pinned interface directly represents the requirement. `adaptation`
means useful machinery exists but firmware policy or target semantics must be
added. `absent/different` means the information is not represented by the
audited interface, so an ISA-only port cannot discharge the requirement.

| Firmware requirement | BOLT | Selected STOKE | Source or measured evidence |
| --- | --- | --- | --- |
| Linked-binary representation | Native | Adaptation | BOLT reads 32/64-bit little-endian ELF; STOKE extracts x86 functions rather than retaining a firmware image. |
| Machine-code CFG transformation/search | Native | Native on x86 | BOLT native passes and STOKE's supported popcount search both ran. |
| Exact CPU/profile feature qualification | Adaptation | Absent/different | BOLT target creation has explicit paths for x86-64, AArch64 and RISC-V; the selected STOKE vocabulary is x86-64. |
| Fixed placement, reserved ranges and post-repair growth | Absent/different | Absent/different | Neither native candidate interface accepts the board memory/reservation contract. The recovered ESCape32 image has only 1,544 bytes of headroom. |
| Relocation-free literals and code/data references | Adaptation | Absent/different | BOLT has target-specific relocation/reference machinery, but all 7 target ELFs lack relocation sections and Thumb recovery is absent. STOKE operates on an extracted function. |
| Reset vector and exception entry/return | Absent/different | Absent/different | The two Os images contain 48- and 30-word vector tables. Neither audited interface models Cortex-M exception state or `EXC_RETURN`. |
| MMIO/device/DMA effects | Absent/different | Absent/different | STOKE `CpuState` has ordinary stack/heap/data/segments, with no device transition system; even x86 port-I/O instructions are explicitly unsupported. BOLT performs structural rewriting without an effect verifier. |
| Interrupt opportunities and concurrent observers | Absent/different | Absent/different | The images expose 54 and 73 interrupt/handler/ISR-named symbols, while neither candidate contract contains an interrupt schedule or observer state. |
| Fault kind, address, PC and recovery state | Absent/different | Absent/different | BOLT has no equivalence checker. Selected STOKE correctness gives zero error after matching a non-normal coarse signal code, and bounded validation assumes no signals. |
| Firmware- or runtime-relevant cost | Adaptation | Absent/different | BOLT can consume profiles but needs a firmware metric and placement budget. STOKE latency and “measured” costs both sum `haswell_latency()`. |

Summary of the ten rows:

- BOLT: **2 native, 3 adaptation, 5 absent/different**.
- Selected STOKE: **1 native, 1 adaptation, 8 absent/different**.
- Cortex-M execution coverage in the evaluation: BOLT **0/7 images**;
  STOKE extraction **0/2 target images**.

The counts are not a general quality score. They quantify coverage of this
declared firmware contract in these pinned implementations.

## Expressiveness result

Let the required firmware observation for an execution be

`F = (architectural state, ordered memory accesses, device transactions,
interrupt events, fault attribution, termination)`.

The selected STOKE concrete state contains a coarse error code, x86 general and
SSE registers, flags, stack, heap, data and ordinary memory segments. Call this
projection `S(F)`. It contains no device-transition trace or interrupt schedule.

There are therefore firmware executions `F1` and `F2` for which:

- `S(F1) = S(F2)`; but
- `F1 != F2`, because, for example, one execution performs two reads from a
  clear-on-read register and the other performs one, or an interrupt is enabled
  at a different instruction boundary.

Any checker that receives only `S(F)` must classify those executions the same.
Consequently, equivalence in the unchanged selected STOKE state model does not
imply firmware observational equivalence. Adding Thumb opcodes cannot repair
this information loss; the state and transition model must also be extended.

The selected implementation has an additional concrete mismatch. Its testcase
correctness cost returns zero once target and rewrite have the same non-normal
signal code, irrespective of registers, memory, fault address or fault PC.
Its bounded validator instead adds a `NoSignalsInvariant` to its assumptions.
Neither behavior establishes the firmware fault contract. This observation is
specific to the selected implementation and is not a claim about all later
STOKE research.

BOLT does not make an unsound equivalence claim here; it simply supplies no
semantic equivalence relation. That makes BOLT reusable infrastructure rather
than a complete firmware acceptance argument.

## Quantitative pressure from the surveyed firmware

The requirements are exercised by the corpus rather than being hypothetical:

| Property | ESCape32 M0+ Os | FreeRTOS M3 Os |
| --- | ---: | ---: |
| Function-range instructions | 7,964 | 11,840 |
| Branches | 1,747 (21.94%) | 3,238 (27.35%) |
| PC-relative loads | 1,154 (14.49%) | 420 (3.55%) |
| Wide Thumb instructions | 694 (8.71%) | 3,497 (29.54%) |
| Retained relocation records | 0 | 0 |
| Vector-table words | 48 | 30 |
| Interrupt/handler/ISR-named symbols | 54 | 73 |
| ROM headroom | 1,544 bytes | 4,085,668 bytes |

The BOLT x86 control also illustrates why its ordinary emission policy cannot
be copied into constrained firmware. The original reconstructed ELF was 27,000
bytes, while restricted and trained BOLT outputs were 4,203,600 and 6,299,072
bytes: **155.7x and 233.3x file-size ratios**. These are sparse host ELF layout
artifacts, not equivalent ROM-growth measurements. They nevertheless show that
the default host placement policy is incompatible with a firmware image that
has 1,544 bytes of flash headroom; a firmware emitter needs an explicit compact
placement contract.

## Product implication

The defensible customer thesis is stronger than “we will add Cortex-M to
BOLT/STOKE”:

> We are building the missing firmware optimization control plane around proven
> machine-code machinery: exact target qualification, relocation-free reference
> recovery, fixed-placement emission, conservative effect boundaries, firmware
> observational checking and target-relevant profitability.

The implementation decision remains reuse-first:

1. Keep BOLT's binary model, CFGs, pass manager and applicable layout machinery.
2. Use STOKE search/checking only inside regions whose state and effect contract
   is expressible by the selected engine.
3. Treat a Thumb backend as one workstream, not the product definition.
4. Make firmware semantics and placement first-class in the patch-independent
   `FirmwareCandidate` acceptance layer.
5. Skip MMIO, exception and interrupt-sensitive regions until their transitions
   are explicitly modeled; never infer purity from ordinary load/store syntax.

This creates a bounded engineering program and a differentiated optimizer. A
decoder-only port would reproduce the tools' existing abstraction boundary and
would not address the customer problem.

Machine-readable evidence and exact source anchors are retained in
[`evidence/design-fit-audit-2026-11-16.json`](evidence/design-fit-audit-2026-11-16.json).
