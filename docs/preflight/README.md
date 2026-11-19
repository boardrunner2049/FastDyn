# Firmware binary preflight

This directory records the Student 1 implementation and evidence for a
standalone firmware binary optimizer built on BOLT/STOKE reuse. Phase one does
not parse or reason about FastDyn patches. It qualifies and optimizes an exact
firmware image and emits a consumer-neutral `FirmwareCandidate v1` containing
the image, target, memory/layout evidence, exact origin map and optimization
report.

FastDyn patch rebinding, hook semantics, observation comparison and emulator
profitability form a separate phase two. Some phase-two prototypes already
exist in this directory, but they are not dependencies of phase one.

The current production direction uses pinned BOLT as the optimizer. A native
post-optimization BOLT pass exports function results after frame optimization,
and a thin firmware wrapper checks protected entries, records contracts and
selectively imports reference-complete BOLT bodies into compact in-place
images. The earlier custom `PreflightPass SDK v1` transformation results remain
as validation/emission fixtures, not a competing optimizer. It does not claim
verified stochastic yield or whole-application capacity.

## Direct BOLT Cortex-M backend

Firm-BOLT now contains a direct ARM/Thumb backend in the pinned BOLT tree; this
replaces the earlier Python-only fallback as the target integration path. It
supplies an ARM `MCPlusBuilder`, Cortex-M CPU/ELF-attribute qualification,
Thumb callable-symbol handling, direct branch/call construction, ARM
relocation handling, AArch32 JITLink support and annotation-safe ARM emission.
The reference path includes Thumb-2 `ADR.W` and PC-relative `MOVW/MOVT` from
input relocation selection through final in-place linking. Relocation-mode
function movement reconstructs the common zero-addend split expressions as
per-instruction `S-P` values, retains `R_ARM_THM_MOVW_PREL_NC` and
`R_ARM_THM_MOVT_PREL` at their new instruction addresses, and binds the
subtraction PC only after architectural operand decoding has completed.
For SHT_REL non-zero split fields, Firm-BOLT pairs records by relocation
section, symbol, relocation family and destination register, prefers the
canonical adjacent `MOVW; MOVT` form, and reconstructs the complete modular
addend while accounting for the two distinct PREL instruction addresses.
Non-canonical graphs that admit different addends remain fail-closed.
Thumb-2 clustered relaxation now has a compact firmware path as well: when
ordinary checked compaction leaves enough space inside the caller's own typed
function range, Firm-BOLT can append a 10-byte `MOVW r12; MOVT r12; BX r12`
call veneer and retarget the original `BL`. The absolute target explicitly
sets bit zero because BOLT's synthetic JITLink label is an even code address;
without that repair Cortex-M would raise `INVSTATE`. `r12` is used only on a
call/tail-call edge where AAPCS permits the clobber, never for an arbitrary
intra-function jump. The default modeled-instruction-count contract rejects
this added execution and retains the input. An explicit relaxed timing
contract installs the same candidate without adding a segment. Thumb-1
absolute veneers are qualified for caller-owned capacity and explicit typed
external slots; unconstrained out-of-function placement remains unqualified.
Each emitted veneer instruction now carries a distinct synthesized-from
annotation rather than borrowing the source instruction's output address. The
post-pass report maps all three instructions to the exact original `BL`; the
linker-resolved report records their three contiguous output PCs and classifies
the synthetic block's nonexistent input address as null. The forced-range gate
therefore has zero unmapped instructions and an auditable fault/event source.
It recovers PC- and ADR-register-based TBB/TBH tables, represents the dispatch
base separately from detached table storage, regenerates table entries after
layout, and preserves `$t`/`$d` mapping boundaries. Cortex-M vectors are
imported from either `.isr_vector` or a validated conventional vector symbol.
The firmware policy admits only in-place output: original program headers and
allocated section ranges must remain exact, and any new allocatable RX/RW range
is a hard error. Unknown references, exception-return-shaped control flow and
decode failures retain their complete original function.

Exact primary vector entries have a narrow exception-entry contract instead
of being blanket-opaque. Ordinary FOP may replace a stack reload with a
same-width register move while retaining the stack store, instruction
identities/order/count, entry address and exact exception-return instruction.
Secondary entries and handlers touched by any other transformation retain the
input body. This architectural handler gate remains active even when the
optional global guest-instruction-count policy is disabled.

The qualified CPU families are Cortex-M0/M0+ (Armv6-M), M3 (Armv7-M), M4/M7
(Armv7E-M), M23 (Armv8-M Baseline), M33 (Armv8-M Mainline), and M55/M85
(Armv8.1-M Mainline). CPU selection remains explicit and must match the ELF
architecture attributes. M33 TrustZone instructions are decoded but fence the
complete containing function from optimization; M55 integer and floating-point
MVE instructions are decoded and exactly re-emitted. M85 PACBTI-M instructions
are decoded and exactly retained as an opaque function. Secure/Non-secure and
PACBTI-M state transformations remain outside the claim.

The audited Cortex-M optimization pipeline is now the backend default; the
former hidden opt-in is no longer required. Passing
`--firmbolt-arm-full-pipeline=false` selects an explicit identity/emission
diagnostic only. Production optimization also requires an explicit
`.isr_vector` or supported vector symbol. Missing vector ownership is a clean
rejection, and a vector that names a secondary entry protects the exact
containing function rather than using nearest-address matching.

The acceptance workflow independently covers a real Cortex-M0+ ESCape32 O0
image, Cortex-M0+ address-stable and guarded-movable interleaved-literal
fixtures, a moved relocation-bearing pool, a relocation-bearing shared-pool
clone, installed branch-trampoline,
caller-owned absolute-call-veneer and
externally reserved veneer-slot execution fixtures, an opt-in profiled
ordinary and tail Thumb-1 indirect-call-promotion fixtures, an opt-in
immutable-vtable Thumb-1 method-load-elimination fixture, a relocation-bearing and
executable Cortex-M3 fixture, a retained `R_ARM_THM_JUMP6`/`CBNZ` Cortex-M3
fixture, a typed executable-flash `R_ARM_ABS8`/`R_ARM_ABS16` Cortex-M3
fixture, an opt-in native-inlining Cortex-M3 fixture, an opt-in profiled native
tail-duplication Cortex-M3 fixture, an opt-in profiled native loop-inversion
Cortex-M3 fixture, an opt-in native control-flow-peephole Cortex-M3 fixture,
an opt-in tail-duplication propagation Cortex-M3 fixture with an independent
register-definition-deletion contract,
an opt-in linked read-only scalar-load Cortex-M3 fixture paired with an
unresolved local-pool retention control,
opt-in profiled ordinary (including target-in-`r12`) and
tail-indirect-call-promotion Cortex-M3 fixtures,
an opt-in immutable-vtable method-load-elimination Cortex-M3 fixture,
opt-in profiled TBB, in-function ABS32 and relocation-complete detached ABS32
hot-index-promotion Cortex-M3 fixtures, an opt-in TBH-to-TBB footprint fixture
with odd-entry alignment and out-of-byte-range retention controls, an opt-in
safe identical-code-folding fixture with address-taken controls, a second safe
ICF fixture with compatible and incompatible ARM EHABI models, a third safe
ICF fixture with distinct but byte-identical external compact EHABI records,
and a GNU-linked safe-ICF fixture with retained `.rel.ARM.exidx` metadata,
explicit user-directed and
profile-execution-count and CDSort call-graph relocation-complete whole-image
function-ordering Cortex-M3 fixtures, a matched-profile exec-count/HFSort/
Pettis-Hansen/CDSort function-clustering matrix, an opt-in Profile2 hot/cold
function-splitting and cache-directed hot/warm/cold CDSplit Cortex-M3
fixtures, including linked split-fragment EHABI growth and retained-relocation
rebuilding, an opt-in profile-guided hot-block-alignment Cortex-M3 fixture,
relocation-complete compact and generic ARM EHABI function-reordering
fixtures, a platform-qualified absolute `R_ARM_TARGET1` dispatch-table
function-reordering fixture, a relocation-mode PC-relative `MOVW/MOVT`
zero-addend fixture and a separately executed non-zero-addend fixture, an
opt-in fixed-size native `memcpy` inlining fixture, an opt-in guarded
size-one `memcpy` specialization fixture, an opt-in profile-driven native
three-way-branch fixture, an opt-in native conditional-tail-call
simplification fixture, an opt-in profile-driven Thumb IT conditional-move
fixture, an Armv6-M conditional-move retention fixture, an opt-in target-aware
Thumb-2 register-reassignment fixture with separately checked inline and
uniquely owned external compact EHABI programs, a shared-external-program
retention fixture, and the
real Cortex-M4 FreeRTOS image.
The 19 November v141 run accepts all fifty-five without
adding a segment and uses
compact, policy-checked publication: BOLT may
analyze and assemble all qualified functions, but only functions changed by a
recorded audited pass and satisfying the firmware publication invariants
replace input bytes. The per-profile overwrite counts are retained in the
machine-readable report rather than duplicated here. The headline real-image
counts remain 26/127 ESCape32 functions and 171/607 FreeRTOS functions; the
Thumb-1 method, M3 propagation and M3 linked-read-only-load fixtures each
install one bounded worker.
These are publication counts,
not performance results. The narrow-data gate intentionally installs no body:
it validates typed data-relocation consumption and compact publication.

The twenty-ninth profile exercises BOLT's native `ReorderFunctions` pass in an
explicit ARM relocation mode rather than the compact per-function publisher.
`--relocs --use-old-text --reorder-functions=user` consumes a frozen order,
reuses the linker's exact 32-byte `.text` flash extent and PT_LOAD, and leaves
`.bolt.org.text` empty. It moves `worker_b`, `Reset_Handler`, `fixture_halt`
and `worker_a` to `0x08000100`, `0x08000104`, `0x08000118` and `0x0800011c`;
repairs both calls and the halt branch; preserves the entry/vector Thumb bit;
retargets `.rel.text` and `.rel.isr_vector` to live sections; and exports exact
old-to-new instruction origins. Reference and candidate execute under FastDyn
and write `(17, 42)`. The one-shot elapsed readings are deliberately excluded
from performance claims. This qualifies user-directed relocation-complete
ordering, not profile-derived ordering, function splitting or arbitrary
firmware reference completeness.

The thirtieth profile replaces the order file with a frozen no-LBR profile and
`--reorder-functions=exec-count`. All three functions are profiled; the 1000,
100 and 10 execution counts place `worker_a`, `Reset_Handler` and `worker_b`
at `0x08000100`, `0x08000104` and `0x0800011c`. The same exact PT_LOAD,
`.text`, vector/entry, SHT_REL, origin and FastDyn `(17, 42)` gates pass. This
closes profile-driven execution-count ordering for the qualified image. It
does not by itself qualify call-graph clustering.

The thirty-first profile adds a frozen branch/call profile and native BOLT
`--reorder-functions=cdsort`. With the same profile, the execution-count
control orders `worker_a`, `worker_b`, `Reset_Handler`, while CDSort orders
`worker_a`, `Reset_Handler`, `worker_b`; this isolates call-graph affinity from
count-only sorting. The same placement, relocation, entry/vector, origin and
FastDyn `(17, 42)` gates pass.

Four additional profiles use one frozen call graph to compare native BOLT
exec-count, HFSort, Pettis-Hansen and CDSort ordering without changing the
evidence. Exec-count orders `worker_a`, `worker_b`, `worker_c`; HFSort and
Pettis-Hansen order `worker_b`, `worker_a`, `worker_c`; CDSort orders
`worker_c`, `worker_a`, `worker_b`. HFSort/Pettis-Hansen report four graph
nodes, four callsites, three hot functions, two clusters and a 12-byte final
average call distance, retaining the linked `worker_a`/`worker_c` pair as an
adjacent cluster. Each candidate has 88 live bytes in the unchanged 88-byte
executable PT_LOAD; ordinary function alignment now consumes the two bytes
previously reported as linker slack. Five text relocations and the vector relocation follow the moved
Thumb identities, all 36 instruction origins are exact, and FastDyn executes
every reference/candidate pair to `(17, 42, 29)`. This qualifies the bounded
relocation-complete integration, not a runtime benefit or arbitrary firmware
reference completeness.

The thirty-second profile runs native BOLT Profile2 hot/cold splitting under
an explicit fragment-placement and cross-fragment branch-event contract. A
frozen profile marks the worker's `(17)` return hot and leaves the `(42)`
return cold. The 12-byte input worker becomes a 10-byte main fragment and a
4-byte cold fragment; its original conditional branch widens from 16 to 32
bits. The linked image consumes the exact 44-byte `.text` range declared by
the linker, leaves `.bolt.org.text` empty, preserves the PT_LOAD, repairs all
calls/branches and relocations, and records all six original identities across
both fragments. The vector-reachable reset handler remains unsplit. FastDyn
executes both the trained hot and held-out cold paths and writes `(17, 42)` for
reference and candidate. Omitting the contract flag rejects before emission.
This is structural and execution evidence, not a speedup claim or CDSplit
hot/warm/cold qualification.

The thirty-third profile runs the second cache-directed splitting stage with
a frozen branch/call profile. A cover-call benefit selects a 12-byte main,
84-byte warm and 12-byte cold fragment for a 100-byte worker. The linked
candidate consumes the exact 144-byte linker-owned `.text` and unchanged
PT_LOAD, leaves `.bolt.org.text` empty, retains all 49 original instruction
identities, and keeps the vector handler unsplit. FastDyn executes the trained
hot/warm and held-out cold inputs; reference and candidate both write
`(5, 42, 2)`. ARM CDSplit without `--compact-code-model` now rejects during
option adjustment instead of reaching LongJmp's two-fragment assertion. This
qualifies one relocation-complete CDSplit image; it is not a speedup claim or
permission to split address-stable, jump-table, veneer or multi-entry code.

The v76 gate retains the firmware gap fix exposed by the earlier M4 run.
Ordinary BOLT CFG finalization can remove explicit branches to the next block
while FOP is the only ledgered optimization. That is ordinarily legal, but it
changes the modeled guest instruction schedule. Firm-BOLT now retains
same-width redundant moves after forwarding and, under the strict default
contract, preserves original explicit unconditional branches even when their
targets become physical fall-throughs. This removes the incidental count
change instead of rejecting otherwise legal FOP candidates. It additionally
models byte and halfword stack spills and replaces redundant reloads with
same-width `UXTB`/`SXTB`/`UXTH`/`SXTH` instructions, preserving truncation,
sign extension, flags, stores and instruction count. A narrow replacement
whose source register is not encodable is retained. On M4 this raises raw FOP
opportunities from 264 to 371 and reduces frame-restoration failures from 356
to 334. Stack-pointer tracking now distinguishes Thumb-1's word-scaled
`ADD/SUB sp,#imm` encoding from Thumb-2's byte-valued wide immediate, tracks
wide large-frame adjustments, and normalizes Thumb-1 word/halfword load/store
indices to architectural byte offsets. The executed M3 fixture uses a
1024-byte wide frame and a second store/load pair that aliases across two SP
rebases. The final ledger selects 171 linked bodies: 127 carry FOP, 50 carry
branch shortening, and some carry both. Of these, 127 change bytes, for 678
changed executable bytes. Eight additional attempted bodies have no linked emitted image and are
atomically retained as `missing_emitted_body`. The earlier 33 accepted/75
`instruction_count_changed` split remains historical evidence of the fixed
branch-repair gap, not the current result.

The SP-rebase fixture also reached BOLT's allocation combiner and exposed the
previously missing ARM `addToImm` target hook. Firm-BOLT now combines narrow or
wide ADD/SUB adjustments only when the architectural byte sum is encodable,
including ADD-to-SUB sign changes; a sum above 4095 is retained. The strict
modeled-time policy skips this instruction-removing pass while still allowing
FOP in the same function. A separately tested relaxed mode performs the legal
combination, and the pass now checks target failure before erasing either
instruction.

The inherited v73 target-hook audit additionally supplies conservative AAPCS argument,
liveness-cleaning and register-scavenging behavior, long conditional and
unconditional direct-branch constructors, `UDF`/`BKPT` construction, and
narrow/wide stack-adjustment constructors used by shrink wrapping. These hooks
previously inherited base-class `not implemented` paths. Unit tests exercise
their encodings and signed stack effects; enabling them does not change the
v72 publication counts or the exact M3/M4 execution results.

On the real M0+ image the backend parses 48 vector entries, protects 13
resolved handlers, exports exact origins, validates 2,078 PC-relative data
references and validates seven ABS32 switch tables containing 157 targets.
The terminal/non-fallthrough literal-pool extension selects 28 functions,
attaches 30 pool owners, rebinds 461 PC-relative references and represents 876
inline data bytes. Twenty-seven bodies are installed, but all reproduce the
input bytes, so the final executable-byte delta is zero. This is typed
representation and integration coverage, not an execution-speed result. All
synthesized CFG-edge instructions have an
exact original-edge proof, and no emitted instruction is unmapped. Candidate
and final provenance are counted separately: 156 candidate control edges pass
the proof, while only 12 belong to the one published real-M0+ body. The
separate M0+ literal fixture executes both a narrow LDR-literal and ADR on
FastDyn's Cortex-M0 model and matches both expected RAM words.

The guarded-movable literal fixture exercises the new typed pool path rather
than merely retaining the original addresses. `ThumbLiteralPoolPlacement`
accepts two interleaved four-byte pools only after proving that each is guarded
by an existing one-successor unconditional branch and that all supported
PC-relative users belong to the owner block. It preserves the original
alignment-fill encoding, carries exact pool/reference identities, reconstructs
final `$d/$t` markers, and checks word alignment plus Thumb-1 reach after final
placement.
The gate installs 1/2 functions and binds one `ADR` plus two loads from
different blocks to two guarded pools. Under the default contract, all three
accesses retain their exact original addresses and the final executable bytes
are identical. Both paths execute on Cortex-M0; reference and candidate write
RAM words `(15, 15)`. The first pool carries one typed `R_ARM_ABS32`
dependency from input `SHT_REL`; the second is raw constant data. Compact ARM
metadata processing recovers implicit addends, keeps executable-section
`STT_OBJECT` symbols as data, and preserves the required even flash-data
address without enabling BOLT's new-segment relocation layout. A `.rodata`
raw pointer into a pool remains a checked negative and retains the input;
explicit global `--relocs` also remains rejected on ARM. A separate relaxed
contract fixture clones a shared value pool into eight explicitly named typed
capacity bytes. Shared ADR cloning is rejected because it would change an
observable address.

The new moved-relocation gate separates live bytes from retained ELF metadata.
Under an explicitly relaxed address contract, CFG normalization moves a
relocation-bearing pool from `0x08000018` to `0x08000014`; the load is rebound
to that exact recorded translation. Firm-BOLT now remaps the retained
`R_ARM_ABS32` source offset and its symbol index after rebuilding and sorting
`.symtab`, so it still names `external_object` at the new pool address. It also
remaps the vector relocation back to `Reset_Handler`. A no-report run exercises
the same path, proving correctness does not depend on JSON diagnostics. That
gate deliberately covered only a one-to-one remap; the following gate covers
container growth.

The relocation-clone gate now exercises that one-to-many case. A checked
value-only pool is cloned into explicit typed capacity, and compact emission
grows `.rel.text` from one to two `R_ARM_ABS32` records for the pool. The live
copies at `0x08000030` and `0x0800003c` both retain the `external_object`
binding after output symbol-table sorting. Their two original-identity loads
are rebound to those exact addresses. Reference and candidate execute both
paths on Cortex-M0 and write RAM words `(0x12345678, 0x12345678)`. The section
remains `SHT_REL`, with its original link/info relationship and eight-byte
entry size. This qualifies bounded relocation-section growth for admitted
pool clones; it does not generalize the supported relocation-class set.

The relocation-class gate now also closes `R_ARM_THM_JUMP6`, the standardized
Thumb-2 static relocation for `CBZ`/`CBNZ`. Its wrapped REL addend, split
`i:imm5:0` field, forward-only `[PC+4, PC+130]` target range, opcode/register
preservation and Thumb-state target requirement are implemented in the ARM
relocation handler and AArch32 JITLink. The retained executable record remains
at `0x08000012` and names `jump6_target` at `0x0800001a` after compact
publication. Reference and candidate both execute on Cortex-M3 and write RAM
word `42`. LLVM MC normally relaxes an unresolved `CBZ`/`CBNZ` to a hint and
the pinned LLD rejects relocation 52, so the fixture explicitly materializes
the conforming post-link ELF instead of misreporting those separate producer
limitations as a BOLT failure.

The narrow-data gate closes a different firmware representation gap. AAELF32
defines `R_ARM_ABS8` and `R_ARM_ABS16` REL addends by sign-extending the stored
field, while accepting either signed or unsigned final results. Those edge
kinds are now implemented in the BOLT relocation handler and AArch32 JITLink,
including endianness and overflow checks. More importantly, BOLT's executable
reader previously asserted that every relocation site in an executable section
belonged to a function. The first real fixture reproduced that crash with a
typed flash object after the code. Firm-BOLT now classifies an exact
range-covered `BinaryData` site as data, retains the relocation record, and
fails closed if neither function nor typed-data ownership exists. Input and
output retain `R_ARM_ABS8 answer8` at `0x08000030` and `R_ARM_ABS16 answer16`
at `0x08000032`. Reference and candidate consume both fields and write their
sum, `42`, on Cortex-M3.

The real M4 gate now also admits a Thumb-2 `LDR.W` terminal literal pool.
`vPortEnableVFP` has a proven return before its aligned four-byte pool; the pass
checks the signed ±4095-byte PC-relative range, preserves the exact access
address, and publishes the body byte-identically. The pool remains part of the
171 selected M4 functions; the current 678-byte delta comes from FOP and the
few branch shortenings that remain narrow after final relaxation.
The neighboring `prvPortStartFirstTask` pool remains unchanged: it follows
`svc #0`, which may architecturally return to the next instruction. A dedicated
negative fixture prevents treating SVC as non-returning without an explicit
exception-handler contract.

The Thumb-1 branch fixture closes a previously hidden test weakness. Local
relaxation had treated register-indirect `bx lr` as a symbolic PC-relative
branch, which marked an otherwise valid function non-simple; the old test saw
the relaxation diagnostic but installed zero functions. Firm-BOLT now excludes
non-symbolic branches from displacement repair. The corrected gate installs
1/3 functions, changes 11 executable bytes, emits one final trampoline with an
exact source-block identity and linker-resolved PC, and executes both branch
outcomes on the Cortex-M0 model. Reference and candidate write the same RAM
words `(1, 2)`.

The separate Thumb-1 call gate closes the previously explicit absolute-veneer
gap for a qualified caller-owned range. The backend emits an optional real NOP
for word alignment followed by `ldr r3, [pc, #0]`, `bx r3`, and a symbolic
32-bit `target+1` literal. `r3` is call-clobbered under AAPCS, the low target
bit preserves Thumb state, and the literal remains symbolic until final link.
The first linked trial exposed an important representation bug: assembler
alignment inside a private per-function fragment did not survive final
fixed-address rebasing and made the load read instruction bytes. Firm-BOLT now
computes padding from the final retained function address and emits it as an
instruction. The accepted fixture installs 1/5 functions, changes 24
executable bytes, records two synthesized veneer instructions plus four bytes
of typed inline data, and adds no segment. Reference and candidate both write
RAM word `42` on FastDyn's Cortex-M0 model. This fixture deliberately enables
unreachable-code elimination and relaxes the global instruction-count
contract to create a caller-owned compact budget; those are recorded fixture
conditions, not the default firmware policy.

The external-slot gate removes the caller-size dependency without treating an
arbitrary NOP run or symbol gap as free space. A linker must reserve a
read-only executable `.firmbolt.veneer` range and give each word-aligned slot a
`__firmbolt_thumb_veneer_slot*` function identity and exact size. Firm-BOLT
rejects a name/section mismatch or any original direct/relocation reference to
the slot, proves the source function's entire qualified
range can reach the slot, replaces the complete slot, and records
`veneer_slot_allocation` separately from the source's `branch_relaxation`.
Under the strict default instruction-count contract the gate installs 2/6
functions, changes 12 executable bytes, writes the same `ldr r3; bx r3;
target+1` veneer in the 8-byte external slot, and adds no segment. Reference
and candidate both write `42` on Cortex-M0. The slot's placeholder instruction
count is capacity outside admitted original control flow; the source call
remains one modeled guest instruction.

On M3, reference and candidate execute to the same thirteen architectural
results, including unsigned and signed byte/halfword reloads with nontrivial
discarded high bits and a word spill reloaded through a differently rebased
SP. Three FOP bodies change 20 executable bytes. A frozen no-LBR profile also
drives ordinary BOLT `ext-tsp` layout: `profiled_worker` keeps six
instructions, inverts its conditional, makes the hot return arm fall through,
and changes three more executable bytes. That fourth installed body carries
exact origins and executes to the expected thirteenth RAM word. The separate
JUMP6 reference/candidate pair writes the same
architectural result and retains its exact relocation identity. The narrow-data
pair also writes `42` and retains both exact relocation identities. On M4, all
eight recovered tables and 44 entries validate
exactly, 42,359 final instructions retain original identities, and the final
171-body publication selects 127 byte-changing functions with 678 changed
executable bytes.

The final ELF check validates every ARM mapping symbol, not merely the MC
intermediate object. It caught and repaired BOLT's non-relocation-mode symbol
reconstruction: emitted literal islands now receive linker-resolved `$d/$t`
addresses, while markers in untouched functions remain at their original
addresses. The v81 artifacts validate every final mapping symbol in all
fourteen cases; the tail-duplication gate adds two exact markers and no marker
has a zero or out-of-section address.
Each attempted
function carries both a machine-readable pass ledger and any publication
rejection reason.

The v76 literal-pool ledger also separates transformation rejection from
metadata-only island discovery. On the real M4 image it records one qualified
pool owner (`vPortEnableVFP`), one retained function whose SVC fallthrough is
not proved (`prvPortStartFirstTask`), and one unreferenced island-marked
function (`__aeabi_memcpy`) that requires no pool transformation. The runner
asserts these counts rather than inferring them from a combined retained total.

The two M4 reference runs and the policy-filtered candidate produce the same
2 MiB RAM-prefix hash after the same 10,000 completed work units. This is an
accepted semantic smoke, not a timing claim: the runner takes only one candidate
sample and two reference reproducibility samples. M0+ ESCape32 board/device
execution also remains unavailable.

The target effect interface now classifies `LDREX`/`STREX`, their byte,
halfword, doubleword and acquire/release variants, and `CLREX` instead of
inheriting an unimplemented base hook. The real M4 artifact contains exactly
60 exclusive loads and 60 exclusive stores after optimization; those counts
are frozen in the report and every instruction carries an explicit exclusive
effect bit. This is atomic-effect coverage, not evidence that rewriting an
exclusive sequence is legal.

The v80 extension adds BOLT's native inliner as an explicitly admitted
firmware pass. Without `--firmbolt-allow-inlining`, any requested Cortex-M
inlining is rejected because it changes a call boundary. The accepted M3
fixture fixes two generic-pass ARM gaps (Thumb predicate operands on `BL` and
`BX LR` return classification), preserves an exact cloned-callee identity,
shrinks the caller from 10 to 8 live bytes, changes seven executable bytes and
adds no segment. Reference and candidate both write `43` under FastDyn. This
is an executed integration/attribution result, not a speedup measurement or a
general observer-sensitive inlining contract.

The v81 extension adds BOLT's native tail duplication behind an explicit
execution-multiplicity contract. A frozen profile duplicates one four-byte
terminal tail onto the hot predecessor, removes that path's branch and retains
the original cold-reachable copy. Compact publication changes four bytes in
one 18-byte function, emits no new segment and records two exact cloned
original identities at their final output addresses. Reference and candidate
both write RAM word `3` on Cortex-M3. Missing multiplicity permission and ARM
constant/copy propagation after duplication are clean rejections. This is an
executed semantic gate, not a performance measurement or authorization for
patch-, MMIO-, exception- or observer-sensitive duplication.

The v84 extension places BOLT's native loop-inversion pass in the audited ARM
schedule behind `--firmbolt-allow-loop-inversion`. The frozen M3 loop begins
with a top test and an unconditional back edge. BOLT moves the two-byte body
before the test, reverses the condition and removes the back-edge branch. The
live body shrinks from 16 to 14 bytes inside its original range, adds no
segment, retains six exact original identities and attributes the replacement
entry branch to its original CFG edge. Without the permission the same profile
leaves the function unchanged. Reference and candidate both write RAM word
`4` on Cortex-M3. This admits branch-count and modeled-time changes only for
the named fixture; it is an executed semantic gate, not a timing result or a
general loop/interrupt equivalence claim.

The v89 extension adds BOLT's native double-jump peephole behind
`--firmbolt-allow-control-flow-peepholes`. In the executed M3 fixture a taken
conditional branch originally enters a one-instruction trampoline, which then
branches to the result block. BOLT retargets the condition directly to the
result. Compact publication changes one immediate byte inside the same
14-byte function, preserves all seven original instruction identities and
adds no segment. Reference and candidate both write RAM word `5`. Missing
branch-event permission rejects before emission. Tail-call trap insertion and
useless-conditional-branch removal are separately rejected: their fault/event
and removed-branch contracts have not been qualified. The latter audit also
found that BOLT merges the duplicate CFG edge during construction before the
named peephole can run, so the resulting deletion would currently be
`cfg_normalization`, not evidence that the peephole executed. This is a
semantic integration gate, not a performance claim.

The v90 extension added BOLT's native indirect-call promotion for ordinary
Thumb-2 register calls. It requires an explicit call/branch-event contract and
`--assume-abi`; the first qualified form uses the AAPCS `r12`/IP call-scratch
rule and leaves method-load elimination, jump-table promotion and Thumb-1
calls unchanged. In
the executed M3 fixture a profile identifies one hot target. BOLT emits a
MOVW/MOVT address guard, a direct-call clone and the original indirect fallback
inside a proven symbol-bounded range. The live body is 28 bytes versus the
40-byte input range and adds no segment. The final map contains all ten emitted
instructions: three retained identities, one direct-call clone of the exact
original callsite and six synthesized guard/merge instructions tied to that
identity. Reference and candidate both write RAM word `17`. The smoke timings
are not interpreted as performance evidence.

The v99 extension closes the ordinary-call target-in-`r12` case without
adding a stack spill. It materializes the hot target in LR because both the
direct `BL` and fallback `BLX r12` overwrite LR, while preserving `r12` until
the guarded fallback executes. The executed M3 body again compacts 40→28
bytes, carries exact original/clone/synthesized identities, and reference and
candidate both write RAM word `17`. The corresponding tail-call case remains
fail-closed: `BX r12` must pass the incoming LR to the callee, so no equivalent
non-memory scratch register is available under the current contract. A lit
negative verifies that requesting tail promotion retains the original
`push`/`pop`/`bx r12` sequence. Neither single-run execution is reported as a
speedup.

The v101 extension qualifies BOLT's existing virtual-method-load elimination
for exact fixed-offset 32-bit Thumb loads from profiled slots in mapped,
allocatable, non-writable image ranges. It requires a separate explicit
read-elision contract. The emitted guard compares the runtime vtable against
the raw immutable data-object address, calls the hot target directly on a
match, and retains the exact original method load and `BLX` in the cold
fallback. The executed fixture compacts 52→32 live bytes, changes 26 bytes,
and maps all twelve emitted instructions: two retained entry instructions,
six synthesized guard/merge instructions, one direct-call clone, the original
cold method load and call, and the return. Reference and candidate both
produce `(17, 19)` for matching and different vtables. A third object points
to an MPU-protected `0x60000000` method table. Both variants take the retained
cold load and raise the same precise four-byte data-access violation. The raw
stacked PCs differ (`0x080000ae` and `0x080000c2`), but both map to original
method-load identity `0x080000ae`; their ordered prefix, CFSR `0x82`, HFSR
`0x40000000` and MMFAR `0x60000000` agree.

Without the read-elision option, ordinary ICP still runs but the original load
remains before the guard and the elimination count is zero. A writable `.data`
slot is rejected even when requested. Indexed, writeback, negative-offset,
conditional, narrow, SP/PC-based and other unsupported fetches remain
unchanged. This is an architectural hot/cold/fault integration result, not a
general device-schedule preservation or performance claim.

The v91 extension separately qualifies profiled indirect tail calls whose
Thumb `BX` lies in a conservatively recognized terminal epilogue. ARM now
implements BOLT's target epilogue hook: the block must have no successors,
must contain a real POP/LDMIA stack restore, and must not save LR in the same
block. Tail promotion additionally requires
`--firmbolt-allow-indirect-tail-call-promotion`; without it, BOLT reports the
skip and retains the tail call. The executed fixture preserves its original
`push {r4}`/`pop {r4}` memory effects, then emits the guarded direct tail branch
and original `BX` fallback. Compact publication installs 24 bytes in the
38-byte qualified range. Its final origin ledger covers two retained epilogue
instructions, four synthesized guard instructions, one exact direct-tail-call
clone and the original fallback. Reference and candidate both write RAM word
`19`. During this gate, a planned two-byte branch relaxed to four bytes after
the optimization passes; v91 therefore labels every final instruction and
derives final Thumb widths from the linked bytes, fixing the size ledger
without confusing duplicate input identities. This remains an execution
integration gate, not a speedup claim.

The v92 extension qualifies native BOLT hot-index promotion for recovered
Thumb-2 inline `TBB`/`TBH` tables under a separate
`--firmbolt-allow-jump-table-promotion` contract. It emits an exact-index
compare and direct conditional branch for the profiled hot case, while
retaining the original table dispatch as the cold fallback. The first admitted
fixture requires dead APSR flags at the dispatch; a second regression in which
the selected case consumes incoming flags is rejected. At the v92 gate,
ABS32 tables and Thumb-1 remain unchanged; v96 qualifies the bounded ABS32
case below. The executed M3 fixture promotes index zero, keeps
all three original table destinations, and shrinks the qualified symbol range
from 60 to 36 occupied bytes. The final ledger records two synthesized
jump-table-promotion instructions, the original fallback `TBB`, and every case
instruction with exact identities. Reference and candidate both write RAM
word `10`.

This gate exposed an emitted-layout bug rather than an instruction-selection
bug: after inserting the guard, the old `$d`/`$t` mapping-symbol positions
misclassified live code as table data. v92 rebuilds the data marker from the
linked jump-table label and the Thumb marker from the linked table end. The
installed table is exactly `02 04 06` at `0x08000016`, resolves to the three
emitted case addresses, and can be decoded safely by a subsequent rewrite.
The one-shot 92.040 versus 79.201 microsecond diagnostic pair is not used as a
performance claim.

The v96 extension independently qualifies recovered in-function Thumb-2 ABS32
switches. It requires the literal pointer, full table and all destinations to
be exact and atomically relocatable; detached/shared tables without complete
reference evidence remain unchanged.
Its M3 fixture promotes hot index zero while retaining the original
`mov pc, r2` fallback, compacts the body from 80 to 52 bytes and changes 27
executable bytes. A four-input execution gate covers hot, both cold fallback
indices and default: reference and candidate both write `(10, 20, 30, 0)`.

The first candidate demonstrated why table entries alone are insufficient: it
moved the pointer/table but left the literal load targeting its old PC, so the
hot guard passed while cold index one hard-faulted. v96 carries the exact
table-pointer identity and rebinds only that accepted load to the emitted
island symbol. The final load, pointer word, three table entries, mapping state
and original/synthesized instruction identities are all checked after linking.
Pinned and externally observed literal pools remain address-stable.

The post-v101 v102 increment admits a narrower detached-table case without
assuming that a nearby read-only object is private. It preserves immutable
input `SHT_REL` evidence separately from mutable/synthesized output
relocations and requires a unique exact `R_ARM_ABS32` record for the pointer
word and every table entry. It also requires the explicit
`--firmbolt-allow-detached-jump-table-update` contract. Missing metadata or
missing permission retains the original body and table.

The linker/install regression keeps the detached table at `0x0801004c`, updates its
words to the three emitted case addresses `0x08000020`, `0x08000024` and
`0x08000028`, remaps the pointer relocation to the live literal at
`0x0800002c`, and retains all four exact `R_ARM_ABS32` records. One of two
functions is overwritten and no segment is added. The promoted index uses
BOLT's existing compare/branch mechanism; the original `mov pc, r2` remains
the fallback. The paired no-permission and no-relocation controls overwrite
zero functions.

The production runner now exercises the same mechanism as a twenty-third
profile with a separately linked read-only table at `0x08008000`. Compact
publication shrinks the worker from 68 to 40 live bytes, remaps the exact
pointer-word relocation from `0x08000050` to `0x08000054`, retains all three
entry relocations, and preserves the three emitted targets `0x08000048`,
`0x0800004c` and `0x08000050`. Reference and candidate execute hot, both cold
and default indices under FastDyn and both write `(10, 20, 30, 0)`. The
no-contract arm leaves the worker unchanged. These are functional,
relocation and publication results; the single execution observations are
not a performance estimate.

The v103 increment closed the ordinary Thumb-1 register-call subset of the
native ICP gap without treating Thumb-1 as Thumb-2 with different spelling.
Because Armv6-M cannot materialize an arbitrary 32-bit target in `r12`, the
guard loads a typed `target+1` word from executable read-only flash. That added
read has its own `--firmbolt-allow-thumb1-icp-literal-load` contract. If no low
argument register is dead, the guard saves one in LR, compares the original
call register with the loaded target, restores the argument with a
flag-preserving move, and then takes either the direct `BL` or original `BLX`.
It never introduces a stack spill. Both outcomes branch around the target word.

The executed M0+ fixture forces that preservation obligation by making both
targets consume r0-r2. Compact publication changes only `indirect_caller`,
shrinks its occupied body from 40 to 32 bytes, emits nine typed promotion
instructions plus one aligned four-byte target word, and maps the direct clone
and cold fallback to the exact original call identity. Reference and candidate
execute the profiled direct path and an unprofiled fallback path on the
Cortex-M0 model; both write `(13, 35)`. A matched control with the ordinary
call-event contract but without the added-read contract leaves all 40 worker
bytes unchanged. The two one-shot execution times in the raw report are smoke
diagnostics only and support no speedup claim.

The v104 increment extends the same typed-data path to conservative Thumb-1
indirect tail calls without borrowing LR. For a terminal `BX` preceded by
recognized callee-saved restoration, the guard saves one borrowed low argument
register in AAPCS scratch `r12`, loads and compares the exact typed `target+1`
word, restores the argument with a flag-preserving move, then takes either the
direct branch or the original `BX`. Incoming LR is unchanged and no stack
access is introduced. The executed fixture makes r0-r2 live, calls the worker
with both the profiled and a cold target, and both reference and candidate
write `(13, 35)`. Compact publication shrinks `tail_dispatch` from 38 to 24
live bytes, records six synthesized guard instructions and four typed data
bytes, and retains the exact original tail-call identity for both direct and
fallback branches. Omitting the added-read contract leaves all 38 bytes
unchanged. At v104, Thumb-1 method-load elimination and tail calls whose
target is `r12` without a genuinely dead low guard register remained
fail-closed; v105 closes only the exact method form documented below.
The focused regression is
[`thumb1-native-indirect-tail-call-promotion.s`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb1-native-indirect-tail-call-promotion.s),
and the executed production source is
[`firm_bolt_cortex_m0plus_icp_tail.s`](../../tests/preflight/fixtures/firm_bolt_cortex_m0plus_icp_tail.s).

The v105 increment closes the exact immutable-vtable Thumb-1 method-load case
without broadening the contract. The recognized form is a 32-bit fixed-offset
method fetch from a profiled slot fully covered by mapped, allocatable,
non-writable image bytes. It requires both the independent read-elision
contract and the Thumb-1 immutable-flash guard-read contract. The typed literal
contains the raw vtable data address, not a Thumb code pointer; the cold
fallback retains the original method load and indirect call.

Compact publication changes only `virtual_caller`, reducing its live body from
52 to 34 bytes. The ledger records eight synthesized promotion instructions,
one aligned four-byte typed data word, one retained cold method load and exact
source identities for both direct and fallback calls. Reference and candidate
execute the hot and cold vtables to the same ordered result prefix `(17, 19)`.
An invalid vtable at `0x3ffffffe` makes the four-byte cold method load cross the
admitted SRAM boundary. The reference faults at `0x0800006c` and the candidate
at `0x08000082`; the origin ledger maps both to original method-load identity
`0x0800006c`, with matching access address, width, prefix and required
Cortex-M0 state. Omitting the added-read contract leaves all 52 bytes
unchanged. Mutable, unmapped, indexed, writeback, negative-offset,
conditional, narrow and SP/PC-based fetches remain unsupported and unchanged.
This is functional and fault-attribution evidence only, not a performance or
whole-firmware-equivalence result. The focused regression is
[`thumb1-native-indirect-call-method-load-elimination.s`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb1-native-indirect-call-method-load-elimination.s),
and the executed fixture is
[`firm_bolt_cortex_m0plus_icp_method_load.s`](../../tests/preflight/fixtures/firm_bolt_cortex_m0plus_icp_method_load.s).

The v106 increment closes ARM/Thumb target integration for BOLT's existing
tail-duplication constant/copy propagation; it does not claim a new propagation
algorithm. Upstream's generic pass needed three target-specific corrections:
ARM conditional-move classification, encoding-checked register/immediate and
register/register replacements, and descriptor-based move-source discovery
instead of the x86-shaped operand-1 assumption. The hook preserves predicates
and flag behavior, rejects destination aliases, tied operands, wrong register
classes and unencodable immediates, and deliberately refuses to turn Thumb-1
`tMOVr` into flag-setting `MOVS`.

The feature has an effect contract independent of tail duplication itself.
`--firmbolt-allow-tail-duplication` admits the clone and changed execution
multiplicity; `--firmbolt-allow-tail-duplication-propagation` separately admits
deletion of a register-only definition after liveness and encoding checks. A
missing propagation contract rejects before emission. In the executed M3
fixture, the hot clone becomes `add.w r0, r0, #5`, one dead `r2` definition at
original identity `0x08000028` disappears, and clone identities
`0x08000034`/`0x08000038` remain exact. Compact publication shrinks the worker
from 22 to 20 bytes; reference and candidate execute both paths and write the
same ordered words `(5, 10)`. An otherwise equivalent `r1` definition is
retained because `r1` is live-out. These are semantic, liveness and origin
checks—not a timing result. See the run-local
[`report.md`](../../build/preflight/firm-bolt-cortex-m-production-parity-v106-taildup-propagation-2026-11-18/report.md),
focused
[`constant-propagation`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-tail-duplication-propagation.s)
and
[`copy-propagation`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-tail-duplication-copy-propagation.s)
regressions, and the executed
[`firm_bolt_cortex_m3_taildup_propagation.s`](../../tests/preflight/fixtures/firm_bolt_cortex_m3_taildup_propagation.s)
fixture.

The v107 increment integrates BOLT's existing `SimplifyRODataLoads` pass; it
does not claim a new constant-propagation algorithm. ARM now supplies the
PC-relative displacement and constant-materialization hooks required by the
generic pass. Qualified Thumb-2 word/byte/halfword and signed narrow loads use
flag-preserving `MOVW`/`MOVT` sequences for `r0`–`r12`. Cortex-M0/M0+ remains
unchanged because Thumb-1 has no equivalent general flag-preserving immediate
materialization sequence.

Firmware use requires `--firmbolt-allow-read-only-load-elision`, which admits
removal of the exact flash-read, fault and modeled-time event. The object must
be allocatable, non-writable and free of static/dynamic relocations across a
conservative four-byte range. In the executed M3 fixture, a complete `.rodata`
reference changes `ldr.w` to `movw` in a same-size six-byte function and is
attributed as `synthesized_from_original_instruction`. A second candidate
using an unresolved local literal pool is rejected as `missing_emitted_body`;
the original `ldr.w; bx` and both immutable original identities are published.
Reference and candidate write `(0x1234, 0x5678)`. This is semantic and origin
evidence, not a speedup measurement. See the run-local
[`report.md`](../../build/preflight/firm-bolt-cortex-m-production-parity-v107-rodata-load-2026-11-18/report.md),
the focused Thumb-2
[`positive/retention test`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-simplify-rodata-loads.s),
the Thumb-1
[`retention test`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb1-native-simplify-rodata-loads.s),
and the executed
[`fixture`](../../tests/preflight/fixtures/firm_bolt_cortex_m3_rodata_load.s).

The detached-table regression sources are the
[retained-relocation positive/no-contract fixture](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-abs32-jump-table-promotion-detached-relocs.s)
and the
[missing-relocation retention fixture](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-abs32-jump-table-promotion-detached-rejected.s).
The executed production profile uses the
[detached-table fixture](../../tests/preflight/fixtures/firm_bolt_cortex_m3_detached_abs32_jump_table_icp.s)
and its
[split code/read-only-data linker contract](../../tests/preflight/fixtures/firm_bolt_cortex_m3_detached_abs32_jump_table_icp.ld).

Reproduce the current complete gate with:

```bash
python3 scripts/preflight/run-firm-bolt-cortex-m-port.py \
  --skip-build --full-pipeline --frame-opt=all \
  --output build/preflight/firm-bolt-cortex-m-v141-external-ehabi-register-reassignment
```

The complete accepted/rejected stage evidence is in the current
[`report.json`](../../build/preflight/firm-bolt-cortex-m-v141-external-ehabi-register-reassignment/report.json),
SHA-256 `e5dbc0262ff83eea8481907fc42ef2f643508f413968217ce82318941ffaf63f`.
The matching
[`validation-manifest.json`](../../build/preflight/firm-bolt-cortex-m-v141-external-ehabi-register-reassignment/validation-manifest.json),
SHA-256 `19eb9cb77a32661174621f4d4866c74daa12b23e06d3e5512654dd0e8147911a`,
binds the exact commands and XML evidence to the report, patch and tested
binary hashes.
The preceding v131 machine-readable
[`validation-manifest.json`](../../build/preflight/firm-bolt-cortex-m-production-parity-v131-pcrel-nonzero-addends-final2-2026-11-19/validation-manifest.json)
has SHA-256
`f9c201958a108f05ce8d73f034710e99426071a9a2d8cf9efda5a2d226c8efa3`
and records the exact regression commands, outcomes and hashes of their
XML/JSON evidence.
The reproducible pinned-source patch is
[`bolt-fastdyn-preflight.patch`](../../patches/bolt-fastdyn-preflight.patch),
with SHA-256
`911334e1bd7882defc78cb112c82497456ea5722c815b8ed509b7e0d107223c6`,
which clean-applies to pinned revision
`7864c0736a8a09670dfbfc513f1e9649799f2486`,
and the tested `llvm-bolt` binary SHA-256 is
`d4085de0aaf8561a48a4bbf5751c856a57827e7ed50da3be5d92e40cfd532a48`.
The current combined ARM BOLT/AArch32 JITLink lit run passes 89/89
(77 ARM and 12 JITLink). The BOLT unit executables pass 128 tests and
explicitly skip 144 target-inapplicable parameterizations: Core is 84/144,
Passes is 5/0 and Profile is 39/0. The retained
[`core-tests.xml`](../../build/preflight/firm-bolt-cortex-m-v141-external-ehabi-register-reassignment/core-tests.xml)
has SHA-256
`caa880f60451a21564cdd6a113403c76cc138a93927fe15d692499cdab86f943`;
the ARM and JITLink results are
[`arm-bolt-lit.xml`](../../build/preflight/firm-bolt-cortex-m-v141-external-ehabi-register-reassignment/arm-bolt-lit.xml)
and
[`aarch32-jitlink-lit.xml`](../../build/preflight/firm-bolt-cortex-m-v141-external-ehabi-register-reassignment/aarch32-jitlink-lit.xml),
with SHA-256 values
`e51d81000b336a0b26fc7b1b7325373fece7222d7d1205b07f85f7e6fe26f08b`
and `91bd6ffa0d25dcebf77baf9bce67fc9f3b16dc3204484ac9e57262a1efc8a56d`.

An earlier targeted checkpoint completed 55/55 BOLT ARM tests, 11/11 AArch32 JITLink lit
tests, all 208 BOLT Core parameterizations, all 45 ARM-filtered Core
parameterizations and 19/19 AArch32 JITLink units, with zero failures. In the
Core runner, 79 target-applicable bodies pass and 129 target-inapplicable
parameterizations explicitly call `GTEST_SKIP`; the ARM-filtered split is 12
pass and 33 explicit skips. The new ARM replacement-hook unit passes for Thumb
and skips its three non-Thumb parameterizations. The retained XML/JSON runner
outputs—not inferred relevance percentages—are the source of these counts.
The two new ARM propagation regressions and the existing ARM tail-duplication
regression pass in v106. The earlier ten-case cross-target tail-duplication and
two-case loop-inversion results remain retained evidence; v106 did not rerun
that broader cross-target subset.
The ARM peephole test and all four existing x86/AArch64 peephole regression
tests pass.
The three existing x86 ICP regressions and the AArch64 `icp.c` regression pass.
The existing AArch64 `icp-inline.c` test is not green in this environment: its
generated profile is stale under the configured Clang 18 build, so it observes
zero promotable calls before reaching the inlining check.
The retained XML/JSON files in the v107 `commands-and-logs` directory are the
source of truth for these counts. A broad `check-bolt` run on this
host reported 823 passed, one expected failure, 120 skipped, 35 unsupported
and 38 failed. One failure was a Firm-BOLT narrow-Thumb branch boundary unit
whose expectation had omitted the architectural `PC+4`; the corrected
positive/negative forward and backward boundaries pass separately. The other
37 were outside this Cortex-M/ARM path; they include unavailable Clang-18
instruction/features, a missing configured `clang++` wrapper and
AArch64/RISC-V/X86 expectation failures. The
complete aggregate was not rerun after the unit correction, so it is not
reported as a green full-suite result.

The current v129 validation passes 66/66 BOLT ARM lit tests, 12/12 AArch32
JITLink lit tests, and 123 passed plus 129 explicitly target-inapplicable BOLT
Unit parameterizations. The function-order tests exercise explicit,
profile-derived execution-count, CDSort, HFSort and Pettis-Hansen call-graph
ordering under matched profiles; the split tests retain
unadmitted/incompatible-model rejections plus exact main/warm/cold fragments,
branch repair, relocation and origin checks. Whole-image
emission exposed four
mechanisms that are now covered:
byte-layout addresses remain canonical while Thumb callable state stays in
symbol metadata; AArch32 data fixups apply ELF `S + A` before restoring the
Thumb pointer bit; intentionally even ABS32 switch-table entries compensate
for local function-symbol typing; and origin maps parse locations as canonical
byte addresses. The final ARM suite includes the switch-table regressions that
initially caught the low-bit error.

v119 closed the fixed-footprint ARM EHABI ordering gap. BOLT decodes
the PREL31 code address in every allocatable `SHT_ARM_EXIDX` record, carries
the `CANTUNWIND` or inline compact model with the original function identity,
sorts by final code address and re-encodes the table after layout. The executed
Cortex-M3 candidate moves all three functions, keeps `.ARM.exidx` at exactly
32 bytes and its original address/file offset, preserves the `PT_ARM_EXIDX`
mapping and live `.text` link, maps all 12 instructions and matches reference
RAM `(17, 42)`. The real M4 compact control retains all 595 `CANTUNWIND`
records and its 4,760-byte index exactly. External `.ARM.extab` personality or
LSDA records and, at v119, all split fragments rejected explicitly; they were
not silently treated as unwind support. v126 additionally
qualifies compatible folded aliases and bounded index shrinkage as described
below, and v128 qualifies split-fragment range replication inside exact linked
capacity.

v120 extends that path to bounded external ARM-defined compact records. Each
PREL31 target must resolve to a complete file-backed allocatable `.ARM.extab`
range with valid reserved bits, personality index 0–2 and enough encoded
opcode words. The executed reset entry uses personality index 1: after its
index slot moves, Firm-BOLT re-encodes the external pointer from `0x7ffffff4`
to `0x7fffffec`, while both resolve to table identity `0x08000124`. The exact
eight-byte extab and 32-byte exidx footprints remain fixed.

v121 adds relocation-complete generic personalities without interpreting
language-specific data. The generic header must have one exact retained
`R_ARM_PREL31` relocation to an exact discovered function; missing or
ambiguous identity rejects before publication. The executed personality moves
from Thumb identity `0x08000125` to `0x08000101`; Firm-BOLT rewrites the extab
header from `0x7ffffffd` to `0x7fffffd9`, preserves all eight opaque bytes
after it, keeps the 12-byte extab and 32-byte exidx at their original
addresses, preserves all 13 instruction origins, and matches reference RAM
`(17, 42)`. Arbitrary language-specific descriptor interpretation remains
unsupported. At v121 split-fragment unwind and variable-footprint index growth
were also unsupported; v127 later qualified shrinkage, and v128 qualifies
capacity-bounded fragment growth with retained GNU `SHT_REL` metadata.

v122 reuses BOLT's ordinary `AlignerPass` rather than adding a parallel
alignment algorithm. Cortex-M basic-block alignment is opt-in, requires an
explicit executed-padding/event contract, and remains incompatible with the
default guest-instruction-count preservation contract. Address-stable,
vector-entry, veneer-slot and jump-table functions remain excluded. In the
executed M3 gate the hot loop moves from `0x0800010a` to the requested
16-byte boundary at `0x08000110`; six Thumb NOP bytes are accounted as
executed padding, all original instruction origins remain exact, and the
candidate uses 44 live bytes inside the linker's 74-byte text capacity with
unchanged program headers. Reference and candidate both write RAM word `10`.
Missing admission, the strict instruction-count contract and an over-budget
placement each reject atomically. This is pass integration and preservation
evidence, not a runtime-performance claim.

v123 reuses BOLT's existing jump-table footprint pass and adds the missing
Cortex-M target hook plus firmware publication contract. A TBH table is
converted to TBB only when every final halfword-scaled target delta fits an
unsigned byte. The executed odd-entry fixture changes the table width from
two bytes to one, adds one explicit trailing byte to keep following Thumb code
halfword-aligned, and shrinks the worker from 30 to 28 bytes while preserving
all 11 original instruction identities. Reference and candidate both produce
RAM words `(10, 20, 30, 42)`. A second target at displacement 256 remains TBH,
and omitting the read-width/layout contract rejects before emission. The
production gate also found and fixed two shared BOLT integration bugs: adjacent
inline tables were double-counted as aggregate islands, and final `$t` mapping
used the input rather than emitted entry width. This is code-size and
preservation evidence; the one-shot execution times are not a performance
claim.

v124 qualifies BOLT's existing safe identical-code-folding algorithm for
relocation-complete Cortex-M images rather than implementing another folding
pass. The target restriction was primarily an unaudited guard: BOLT's generic
CFG/instruction comparison and relocation-backed address-taken analysis are
reusable. Firmware admission is narrower. `--icf=safe` additionally requires
`--firmbolt-allow-identical-code-folding`; `--icf=all` is rejected. Vector
entries, typed veneer slots and address-stable functions are excluded, and a
fold is installed only when every child instruction has an exact canonical
instruction identity. The executed M3 fixture folds the two four-byte
direct-call twins to one body. The removed body accounts for four bytes and
two now-unneeded alignment bytes, so live `.text` falls from 72 to 66 bytes. Two
otherwise-identical functions whose addresses occur in `.rodata` remain at
distinct addresses, and their relocated Thumb pointers still name the exact
retained functions. The final ledger maps both child identities to the two
canonical output PCs. Reference and candidate both produce RAM words
`(42, 42, 17, 17, 1)`; the last word confirms the address-taken functions
remain distinct. Program headers and the vector are unchanged, the retired
original text section is empty, and no segment is added. This is safe-mode
integration, identity and compact-emission evidence—not a general claim that
function address equality is unobservable, nor a speedup result.

v125 closes the first EHABI-covered folding gap without weakening unwind
semantics. After safe ICF selects a child/canonical pair, Firm-BOLT resolves
both original functions to their linker-established EHABI ranges and compares
the decoded models. `CANTUNWIND` and inline compact models must be exactly
equal; external records must name the same table and personality identity.
The executed Cortex-M3 gate folds one four-byte twin, rebuilds `.ARM.exidx`
from four records/32 bytes to three records/24 bytes, updates the section
header and `PT_ARM_EXIDX`, and zeroes the retired eight-byte file range. The
other program headers, vector and exact instruction origins remain unchanged;
reference and candidate both produce `(42, 42)`. A byte-identical twin with a
`CANTUNWIND` model on one identity and an inline compact model on the other is
rejected after linking, and the wrapper removes the disposable partial output
instead of publishing it. Retained relocation records on an index that would
need compaction also reject until their offsets can be rebuilt. This qualifies
At v125 this qualified compatible unwind aliases and bounded shrinkage, not
arbitrary EHABI merging, split-fragment unwind, language-specific descriptors
or index growth; v128 adds the separately checked fragment-growth case.

v126 extends that comparison to separately emitted ARM-defined external
compact records. Distinct `.ARM.extab` targets may alias only when neither is
a generic personality/LSDA record and their complete, length-validated bytes
are equal. The executed personality-index-1 fixture folds one four-byte twin,
leaves `.ARM.extab` byte-exact, rebuilds `.ARM.exidx` from four records/32
bytes to three records/24 bytes, updates `PT_ARM_EXIDX`, zeroes the retired
eight-byte index range and preserves the exact vector and all non-index
segments. Reference and candidate both produce `(17, 17)` under FastDyn. A
byte-identical code pair with personality indexes 1 and 2 is rejected after
linking; the wrapper deletes BOLT's disposable partial candidate and publishes
nothing. Generic/LSDA aliases still require exact table identity because their
opaque language-specific payload and retained dependencies are not yet modeled.
The unchanged external-table bytes can include an orphaned duplicate after the
index alias is removed; compacting that allocated data is a separate
reference-completeness problem outside the current claim. These are layout,
identity and preservation results, not runtime-performance results.

v127 closes the retained-index-relocation defect exposed by GNU ld. GNU ld
13.3/2.42 preserves `.rel.ARM.exidx` in the final ET_EXEC image, whereas LLD's
synthetic table path does not. Before this milestone, Firm-BOLT correctly
compacted the allocated index but copied all eight relocation records: three
still named the eliminated child entry, and GNU ld's section-relative sentinel
record still named input offset `0x18`. The rewriter now records an exact map
for both words of every input index entry, removes relocations for an eliminated
entry and remaps retained fields after sorting/compaction. The executed gate
rebuilds `.rel.ARM.exidx` from eight records/64 bytes to five records/40 bytes,
removes exactly the child's `R_ARM_NONE` and two `R_ARM_PREL31` dependencies,
and changes the sentinel offset to `0x10`. The allocated `.ARM.exidx` remains
three records/24 bytes, `.ARM.extab` remains byte-exact, and reference and
candidate again produce `(17, 17)`. A YAML regression makes the GNU relocation
graph reproducible without requiring GNU ld in the lit environment; the
production profile separately builds the same graph with the pinned GNU linker.

v128 closes the split-fragment unwind gap without inventing a second unwind
model. EHABI ranges are derived from final main/warm/cold placement and retain
the exact source function's decoded unwind identity. When another function's
unwind range separates two fragments, Firm-BOLT emits another index record for
the later run; adjacent fragments covered by the same range do not create
redundant records. Growth is permitted only inside bytes already backed by the
same `PT_LOAD` in both file and memory and not owned by another allocated
section. The GNU-linked CDSplit fixture grows `.ARM.exidx` from three
records/24 bytes to four records/32 bytes and `PT_ARM_EXIDX` with it. Its
retained `.rel.ARM.exidx` grows from five records/40 bytes to six records/48
bytes: the worker's source `R_ARM_PREL31` dependency is cloned at the new warm
range, the external compact Reset model keeps its exact table dependency, and
the section-relative sentinel moves from `0x10` to `0x18`. All other program
headers remain exact. FastDyn executes the hot, warm and held-out cold paths in
both images and observes `(5, 42, 2)`. A matched image without the eight-byte
linker-owned index gap rejects atomically: 32 bytes are required and only 25
are qualified. This is unwind/reference/placement and execution evidence, not
a throughput result or permission to extend arbitrary firmware segments.

v129 closes a platform-relocation fail-open path. `R_ARM_TARGET1` has no one
ABI-wide meaning: GNU and LLD can resolve it as ABS32 or REL32. FirmBOLT now
rejects it by default and admits the bare-metal absolute form only with
`--firmbolt-arm-target1=abs`. Before disassembly it scans every static
relocation whose target section is allocatable, including non-code tables that
BOLT's normal reader does not visit, and rejects unsupported types before an
output file exists. An explicitly relative input falsely declared absolute is
also rejected from its linked contents. In the accepted M3 fixture,
`target_worker` moves from `0x0800011c` to `0x08000100`; the dispatch word and
retained relocation move from `0x0800011d` to `0x08000101`, preserving the
Thumb bit. Reference and candidate both execute through FastDyn and write
`42`. The relocation audit also found and fixed an incorrect continuous ARM
TLS-number interval that had silently classified TARGET1, PREL31, MOVW/MOVT
and branch relocations as TLS. Typed EHABI relocations remain owned by their
dedicated complete index rebuilder. Split PC-relative MOVW/MOVT expressions
were exact-retained in compact mode at that checkpoint. This is relocation,
placement and execution evidence, not a throughput result.

v130 adds relocation-mode movement for the common zero-addend Thumb-2
PC-relative `MOVW`/`MOVT` pair. The symbolizer reconstructs each field as
`:lower16:(S-P)` or `:upper16:(S-P)` and defers the temporary PC-label
annotation until ARM's decoder has validated the architectural operand count;
attaching BOLT metadata during the decoder callback would otherwise turn four
architectural operands into six. In the executed M3 gate, `pcrel_worker` moves
from `0x0800010a` to `0x08000100`, while `external_data` remains at
`0x08001000`. The retained `R_ARM_THM_MOVW_PREL_NC` and
`R_ARM_THM_MOVT_PREL` records move to `0x08000100` and `0x08000104`, all ten
worker instruction identities map exactly, no instruction is synthesized and
no segment is added. Reference and candidate both execute in FastDyn and
write RAM word `42`. ARM BOLT lit passes 67/67, AArch32 JITLink passes 12/12,
and BOLT unit tests pass 123 with 129 explicit skips. Non-zero split-field
implicit addends still require paired reconstruction and remain unsupported
and fail-closed. This is reference/link/layout/execution evidence, not a
throughput result or a claim about arbitrary producer encodings.
The source-level regression is
[`thumb2-pcrel-mov-pair-reorder.s`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb2-pcrel-mov-pair-reorder.s),
and the production execution gate uses the
[`firm_bolt_cortex_m3_pcrel_mov_pair.s`](../../tests/preflight/fixtures/firm_bolt_cortex_m3_pcrel_mov_pair.s)
fixture with its explicit
[`linker layout`](../../tests/preflight/fixtures/firm_bolt_cortex_m3_pcrel_mov_pair.ld)
and
[`function order`](../../tests/preflight/fixtures/firm_bolt_cortex_m3_pcrel_mov_pair.order).

v131 reconstructs non-zero SHT_REL addends for both absolute and PC-relative
Thumb `MOVW`/`MOVT` pairs. Records are matched within one relocation section
by symbol, relocation family and encoded destination register. An adjacent
`MOVW; MOVT` is authoritative; without that canonical form, all compatible
partners must reconstruct the same addend or the input remains unchanged. For
PREL pairs the two relocation places differ, so the implementation first
recovers the complete modular value at the `MOVT` place and translates it
back to the `MOVW` place before solving for `A`. The production fixture uses
`external_data + 4`: its worker again moves `0x0800010a→0x08000100`, the
decoded linked fields change from `(0x0efa, 0)` to `(0x0f04, 0)`, and both
relocations move to `0x08000100`/`0x08000104`. All ten identities remain
exact, no segment is added, and reference/candidate both load the second word
and write `42` under FastDyn. A separate absolute-pair regression reconstructs
the same `+4` addend. The complete gate is 48/48, ARM BOLT lit is 69/69,
AArch32 JITLink is 12/12 and BOLT unit tests are 123 passed/129 explicitly
skipped. Ambiguous non-canonical pair graphs remain fail-closed. These are
relocation, placement and execution results, not throughput measurements.
The focused regressions are the
[`PC-relative pair`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb2-pcrel-mov-pair-addend-reorder.s)
and
[`absolute pair`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb2-abs-mov-pair-addend-reorder.s);
the production source is
[`firm_bolt_cortex_m3_pcrel_mov_pair_addend.s`](../../tests/preflight/fixtures/firm_bolt_cortex_m3_pcrel_mov_pair_addend.s).

v132 places BOLT's existing `InlineMemcpy` pass in the audited ARM schedule
instead of introducing a second optimizer. It recognizes an exact,
unpredicated constant byte count reaching `r2` and expands only sizes 1--32.
The generated Thumb sequence uses byte loads/stores through `r3`, so it is
alignment-safe and does not require a post-M0 instruction extension. The pass
is off unless `--firmbolt-allow-inline-memcpy` admits disjoint ordinary RAM,
valid source/destination ranges, AAPCS caller-clobbers and the changed
call/fault/event shape. The executed M3 fixture replaces one three-byte call
with three loads and three stores, shrinks `copy_three` from 28 to 18 live
bytes, attributes all six generated memory operations to the exact original
call identity and produces `0x00332211` in both reference and candidate under
FastDyn. Unknown size, size 33 and an intervening call remain unchanged; a
missing contract rejects before publication. The complete gate is 49/49 and
adds no segment. This is pass-integration, effect-contract, compact-emission
and execution evidence, not a throughput result or arbitrary `memcpy`
qualification. The focused source regression is
[`thumb-native-inline-memcpy.s`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-inline-memcpy.s),
and the executed fixture is
[`firm_bolt_cortex_m3_inline_memcpy.s`](../../tests/preflight/fixtures/firm_bolt_cortex_m3_inline_memcpy.s).

v133 qualifies BOLT's existing `SpecializeMemcpy1` pass after fixed-size
inlining. For a selected call whose size remains dynamic, it adds a guarded
one-byte fast path and retains the original call for every other size. The
pass is off unless `--firmbolt-allow-memcpy1-specialization` explicitly admits
the added guard and changed call, access, fault and event shape under AAPCS and
valid, disjoint, byte-addressable ordinary RAM. Compact publication shrinks
the executed `copy_runtime` body from 30 to 22 live bytes. The fixture executes
size one through the fast path, size two through the retained fallback and an
unselected size-one call through the unchanged control; reference and
candidate both produce `0x44332211`. All seven generated compare, branch,
load, store and merge instructions map to the exact original call identity.
The first integration attempt exposed three late-materialized branches without
origins; v133 now reconstructs them only from a unique call/predecessor graph
and rejects the complete function as `missing_instruction_origin` when that
proof is unavailable. Missing contract also rejects before publication. The
complete FOP-enabled gate is 50/50 and adds no segment. This is integration,
compact-emission and preservation evidence, not a throughput result or a claim
about overlapping, MMIO or partially valid memory ranges. The focused source
regression is
[`thumb-native-memcpy1-specialization.s`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-memcpy1-specialization.s),
and the executed fixture is
[`firm_bolt_cortex_m3_memcpy1_specialization.s`](../../tests/preflight/fixtures/firm_bolt_cortex_m3_memcpy1_specialization.s).

v134 places BOLT's existing `ThreeWayBranch` pass in the audited ARM schedule
and implements ARM condition-code composition for the exact EQ/NE,
signed-order and unsigned-order partitions that have direct Thumb condition
encodings. Unsupported or mixed condition families remain unchanged. The pass
is off unless `--firmbolt-allow-three-way-branch` admits the frozen profile,
unchanged shared NZCV state, branch-identity, path-order and modeled-timing
effects. In the executed M3 fixture, the second `beq` becomes `ble` after the
preceding `blt`; this is equivalent on the only path reaching it. Compact
publication changes one executable byte in the same 18-byte function, retains
all nine original instruction identities at their exact addresses, creates no
clone or synthesized identity, and adds no segment. Reference and candidate
both execute inputs below, equal to and above the threshold and write
`(10, 20, 30)` under FastDyn. Missing admission rejects before publication.
The complete FOP-enabled gate is 51/51, ARM BOLT lit is 72/72, AArch32
JITLink lit is 12/12, and BOLT units pass 126 parameterizations with 138
target-inapplicable skips. These are pass-integration and preservation
results, not a throughput claim. The focused source regression is
[`thumb-native-three-way-branch.s`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-three-way-branch.s),
and the executed fixture is
[`firm_bolt_cortex_m3_three_way_branch.s`](../../tests/preflight/fixtures/firm_bolt_cortex_m3_three_way_branch.s).

v135 reuses BOLT's `SimplifyConditionalTailCalls` CFG pass, which was
previously hard-gated to x86 even though the ARM backend now supplies its
branch analysis, target replacement, condition reversal, tail-call annotation
and long-branch repair dependencies. Cortex-M schedules it only when the
ordinary option is explicitly present and
`--firmbolt-allow-conditional-tail-calls` admits the LR,
branch/call-identity, event-order and modeled-timing changes. Multi-entry,
split, EH-range, vector-entry, veneer-slot, jump-table and address-stable
functions remain excluded. In the executed M3 fixture, one conditional branch
and one direct tail branch become a wide conditional tail branch. Incoming LR
is unchanged, the ordinary return path remains intact, the worker shrinks
12→10 live bytes, four retained instructions preserve exact original
identities, and the removed tail-branch identity is recorded explicitly.
Reference and candidate execute both paths to `(42, 7)` under FastDyn. Missing
admission rejects before publication. The complete FOP-enabled gate is 52/52,
ARM BOLT lit is 73/73, AArch32 JITLink lit is 12/12, and BOLT units pass 126
parameterizations with 138 target-inapplicable skips. This is pass-integration
and preservation evidence, not a throughput claim. The focused source
regression is
[`thumb-native-conditional-tail-call.s`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-conditional-tail-call.s),
and the executed fixture is
[`firm_bolt_cortex_m3_conditional_tail_call.s`](../../tests/preflight/fixtures/firm_bolt_cortex_m3_conditional_tail_call.s).

v137 reuses BOLT's existing `CMOVConversion` CFG pass.  The target mismatch is
explicit: x86 encodes each conditional move independently, whereas Thumb
requires a preceding IT instruction that describes the following predicated
instructions.  Firm-BOLT adds target hooks for sequence qualification and
prefix creation, recognizes only flag-neutral `tMOVr`, `t2MOVr`, `t2MOVi` and
`t2MOVi16` forms, and emits an all-Then IT mask.  Loads, stores, flag-defining
moves, PC/SP moves, already predicated forms and blocks exceeding the target's
IT limit remain unchanged.  The v136 implementation incorrectly treated
Armv6-M and Armv8-M Baseline as IT-capable.  End-to-end Cortex-M0+ emission
exposed the error because LLVM rejected the transformed body.  v137 now gates
materialization on Thumb-2: Armv6-M Cortex-M0/M0+ and Armv8-M Baseline
Cortex-M23 retain their branches, Armv7-M admits at most four moves, and the
qualified Armv8-M Mainline CPUs admit one.  The pass remains off unless
`--firmbolt-allow-conditional-moves` separately admits removal of the branch
event and changes to interrupt opportunities and modeled timing.  It excludes
multi-entry, split, EH-range, vector, veneer, jump-table and address-stable
functions.

The executed Cortex-M3 fixture replaces one `bne` with `it eq` and predicates
the existing `mov.w`; the worker remains exactly 12 bytes.  The IT instruction
maps to the removed branch's original identity, the other four instructions
retain exact origins, and no instruction is unmapped.  Reference and candidate
execute both selected-value paths to RAM words `(42, 7)` under FastDyn.  The
missing-contract control rejects before publication.  Focused regression also
covers one- and four-slot IT blocks, an explicit trailing branch, five-slot
retention and rejection of a flag-setting move.  The Cortex-M0+ retention
fixture keeps the same profiled hammock byte-identical with all five original
instruction identities and executes reference/candidate to `(42, 7)`.
The complete FOP-enabled gate is 54/54, ARM BOLT lit is 75/75, AArch32 JITLink
lit is 12/12, and the current
BOLT unit runs have 127 passes and 141 explicit target-inapplicable skips.
These are pass-integration and architectural-execution results; the single
execution timings are not a performance experiment.  The focused source
regression is
[`thumb-native-conditional-move.s`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-conditional-move.s),
the Armv6-M retention regression is
[`thumb1-native-conditional-move-retention.s`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb1-native-conditional-move-retention.s),
and the executed fixtures are
[`firm_bolt_cortex_m3_conditional_move.s`](../../tests/preflight/fixtures/firm_bolt_cortex_m3_conditional_move.s)
and
[`firm_bolt_cortex_m0plus_conditional_move.s`](../../tests/preflight/fixtures/firm_bolt_cortex_m0plus_conditional_move.s).

v138 adapts BOLT's existing `RegReAssign` pass instead of introducing a
parallel optimizer.  The upstream profitability model is x86-specific: it
ranks classic and extended registers by REX-prefix cost.  Firm-BOLT adds a
target interface for candidate register pairs, exact operand-class legality,
target re-encoding and per-instruction byte savings.  The first qualified
Thumb-2 policy exchanges only ordinary AAPCS callee-saved R4--R6 with R8 or
R10.  It excludes R7/R11 frame-pointer roles, the possible R9 platform
register, implicit uses/defs, CFI, compact/generic ARM EHABI programs, vector
handlers, multi-entry or split functions, jump tables, veneers and
address-stable layouts.  Narrow
instructions that cannot encode the exchanged high register reject the whole
function rather than being partially renamed.  The option remains off unless
`--firmbolt-allow-register-reassignment` admits transient register-state,
interrupt-opportunity and modeled-timing changes.

The executed Cortex-M3 worker exchanges R8 with R4 and narrows four wide
load/store encodings.  Its live body shrinks 26→18 bytes, all eight original
instruction identities remain mapped, and reference/candidate execution both
produce RAM words `(42, 4, 8)`, including exact final R4 and R8 values.  The
missing-contract control rejects before publication.  The complete gate is
55/55; ARM BOLT lit is 76/76, AArch32 JITLink lit is 12/12, and BOLT units
report 128 passes plus 144 target-inapplicable skips.  This demonstrates a
real target-specific compact-encoding mechanism and preservation boundary; it
does not establish a whole-firmware speedup.  The focused source regression is
[`thumb-native-register-reassignment.s`](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-register-reassignment.s),
and the executed fixture is
[`firm_bolt_cortex_m3_register_reassignment.s`](../../tests/preflight/fixtures/firm_bolt_cortex_m3_register_reassignment.s).

v139 removes the name-based ARM EHABI restriction without weakening unwind
semantics.  The real FreeRTOS linker emits its typed `SHT_ARM_EXIDX` table as
`.ARM`, not `.ARM.exidx`; the old guard therefore failed to recognize the
table.  `RegReAssign` now decodes every allocatable typed index, validates
PREL31 ordering, associates records with exact discovered functions and
admits CANTUNWIND-covered functions individually.  Compact and generic unwind
programs remain byte-exact and their functions remain ineligible until the
register operands in those programs can be rewritten atomically with code.
The renamed-section regression optimizes the CANTUNWIND worker while retaining
an otherwise profitable compact-record control.  On the real M4 FreeRTOS
image, all 607 discovered functions classify as CANTUNWIND-covered; no
profitable register exchange was present, so this is a safety/coverage result,
not a claimed size win.  The executed Cortex-M3 fixture now also carries linked
CANTUNWIND records through the 26→18-byte transformation.  Its GNU
`--emit-relocs` output retains one PREL31 relocation for an input index record
that the linker merged away.  Compact in-place emission now publishes identity
mappings for every live index field and removes only the aligned, out-of-range
PREL31 record; the final `.rel.ARM.exidx` names the one live record instead of
crashing or preserving stale metadata.

v140 admits a bounded but real subset of those compact records instead of
classifying every compact program as opaque.  A register exchange is eligible
only when one exact function owns one inline EHABI compact model-0 word and
the complete fixed-width bytecode can be renamed without changing its length.
The target decoder rewrites arbitrary core-register pop masks (`0x80`),
`vsp = rN` (`0x90`) and contiguous `r4`--`rN` pops (`0xA0`) when the renamed
range remains representable by the same opcode.  Reserved instructions,
width-changing encodings, shared ranges, external compact tables and generic
personality records remain unchanged.  Mutation is atomic: the function body
records the accepted register exchange, and both compact in-place emission and
full relocation emission must apply the same exchange to the unwind word or
reject the candidate.

The focused fixture now transforms two identical 26-byte workers, one
`CANTUNWIND` and one carrying an inline `pop {r8}` program.  Both bodies become
18 bytes and the latter program becomes `pop {r4}; finish`; the in-place path
keeps its original EHABI entry address, while relocation mode moves the body,
rebuilds its PREL31 entry and preserves the same renamed program.  Negative
unit coverage rejects a contiguous-pop exchange that would require a
noncontiguous one-byte mask.  The complete FOP-enabled production gate remains
55/55, ARM BOLT lit is 76/76, AArch32 JITLink lit is 12/12, and BOLT units
report 128 passes plus 144 target-inapplicable skips.  This closes inline
model-0 unwind coupling for register reassignment; it does not authorize
rewriting shared, external or generic EHABI programs.

v141 extends that coupling to bounded external ARM-defined compact programs
without treating language-specific data as opcodes.  A shared typed EHABI
service decodes personality indexes 0--2 into architectural opcode order and
records the exact file-backed storage range.  Register reassignment admits the
program only when one exact function owns that complete range and every
register-bearing opcode can be re-encoded at the same size.  Compact in-place
publication updates the exact `.ARM.extab` bytes; relocation publication
composes multiple private updates in one section before rebuilding their
PREL31 index entries.  This composition rule closes a bug found during the
gate where a later record update could otherwise have restored an earlier
record from immutable input bytes.

The expanded fixture transforms four 26-byte workers: one `CANTUNWIND`, one
inline compact and two separately owned external personality-index-1 records.
All become 18-byte workers and every admitted `pop {r8}` becomes `pop {r4}` in
both publication modes.  A separate fixture gives two ranges the same external
record; both functions remain unchanged and the shared `pop {r8}` is retained.
Generic personalities and unsupported or width-changing bytecodes remain
opaque.  The complete production gate remains 55/55, ARM BOLT lit is now
77/77, AArch32 JITLink lit is 12/12, and the unit split remains 128 passes plus
144 explicit target-inapplicable skips.  This closes uniquely owned external
compact coupling; it does not authorize shared-record mutation, generic/LSDA
payload interpretation or general liveness-backed register allocation.

This is a production backend slice over real Cortex-M firmware, but it is not
yet feature-parity with BOLT's mature x86 backend. Remaining target work is
literal islands without a proven non-fallthrough owner, ARM relocation classes
beyond the now-qualified JUMP6/ABS8/ABS16 and paired split MOVW/MOVT set,
producer forms whose relocation graph does not identify a unique split pair,
veneer placement without explicit typed slots or under arbitrary
whole-image layout, clustering and splitting of
address-stable, jump-table, veneer or multi-entry functions beyond the
executed complete-reference layout,
non-ADR/ambiguous table-base data flow, DSP/FPU/MVE-specific optimization rules,
Secure/Non-secure and PACBTI-M state transformations. Cortex-M4 DSP/FPU,
Cortex-M55 MVE and Cortex-M85 PACBTI-M decode/exact retention are qualified
independently from those optimization rules. Handler rewriting beyond the
same-count, store-preserving FOP contract remains disabled until richer
exception-return and modeled interrupt semantics are represented. These cases are retained or rejected explicitly;
they are not counted as successful rewrites.

The BOLT-first composition gate then feeds that exact M4 output to the
firmware-aware `thumb-hot-exit-layout-v1` wrapper.  The wrapper installs a
same-size, zero-growth hot-exit layout candidate.  Across its selected hot
path, generated TCG work falls from 72 to 61 optimized operations and from
768 to 696 host-output bytes.  This composition result is retained in
[`integration-report.json`](../../build/preflight/cmwrap-final/integration-report.json)
and [`hot-path-tcg-inventory.json`](../../build/preflight/cmwrap-final/hot-path-tcg-inventory.json).
It is generated-host evidence; the separately measured fixed-work result
remains the performance evidence and no per-block universal-win claim is made.

## Firm-BOLT FreeRTOS O0/O3 comparison

Firm-BOLT is the pinned extended BOLT pipeline followed by the firmware
acceptance wrapper. A fresh whole-application experiment builds the same
FreeRTOS POSIX queue application at O0 and O3 and trains Firm-BOLT only on the
O0 binary. Across 30 paired two-million-message blocks, O3/Firm-BOLT CPU cost
is `0.9768 [0.9723, 0.9805]`: Firm-BOLT is 2.38% slower than O3 and passes the
frozen 5% comparability gate. It is faster than O0 by `1.0121 [1.0077,
1.0187]` and faster than ordinary trained BOLT by `1.0273 [1.0252, 1.0329]`.
Every variant returns the same checksum. The application links the FreeRTOS
scheduler, queues, lists, timers, event groups, stream buffers, heap and POSIX
port; this remains an x86-64 rehost result, not Cortex-M or FastDyn evidence.
See the [run report](../../build/preflight/firm-bolt-freertos-o0-o3-2026-11-17/report.md)
and reproduce it with
`python3 scripts/preflight/run-firm-bolt-freertos-o0-o3.py`.

The customer-facing motivation, differentiation, economic break-even model and
evidence-backed roadmap are developed in
[`customer-motivation.md`](customer-motivation.md).

The source-level design-fit result is in
[`design-fit-audit.md`](design-fit-audit.md). It shows why an ISA-only port is
insufficient: the pinned tools do not natively represent several necessary
firmware observations, placement constraints or target-relevant costs.

The single supported-ISA research report is
[`lemix-o0-evaluation.md`](lemix-o0-evaluation.md). It compiles the official
LEMIX SmartSpeaker port at O0 and applies BOLT/STOKE to that exact x86-64
executable. Ordinary FOP reduces CPU cost for the exact accessor by about 20%,
while paced whole-application capacity remains unestablished. A later
100-block precision extension refuted the initial exact-reference fallback:
stock BOLT used less paced CPU (`stock/reference = 0.9864 [0.9790, 1.0000]`).
A frozen hot-FOP follow-up completed 30 paired 30-second blocks at
`stock/hot-FOP = 1.0034 [0.9929, 1.0240]`, below the 1.005 acceptance gate, so
the deployable policy retains stock BOLT. The exact timed image is published as
a BAT-backed `FirmwareCandidate v1` with origins for 85,731 relocated
instructions and complete entry coverage. Protected-entry
canonicalization raises the frozen STOKE cohort from 1/23 to 23/23 extraction;
states reach 10/23. Normalization improves testcase objectives in 8/8 S2 runs,
but three verifier counterexamples and five unknown results leave verified
yield at 0/8. The report incorporates the bounded prototype results rather than
placing them in a second narrative document.

The latest native follow-up completes FOP compare support across ordinary x86
widths and adds checked post-FOP copy rules. The remaining unsupported FOP
opcode opportunity is at most 21 dynamic reads. The adjacent rule's 800-block
held-out aggregate is a strict speedup (`target CPU 1.0083 [1.0049, 1.0126]`,
wall `1.0013 [1.0004, 1.0019]`) but misses the frozen useful-effect lower bound
by 0.0066 percentage point, so SmartSpeaker still publishes stock BOLT. The
generalized 391-copy and hot-only 29-copy forms fail their screens, and their
dominant extra site inherits an unproved FOP memory-alias obligation. The
corrected store-barrier rule retains 69 copies/122,846 weighted executions and
passes installation, but its target training interval is `1.0041 [0.9875,
1.0147]`, so it does not advance. Four
alignment policies fail, and inline-all changes no executable byte. A separate
28-site arithmetic/copy coalescing rule covers 562,102 weighted executions but
regresses at target CPU `0.9939 [0.9915, 1.0071]`. That run also corrected the
wrapper: exact fixed work and ordered events now gate publication, while paced
one-second marker counts are diagnostic only. Full reasoning and links are in
Section 8.5 of the main report.

The native report now carries exact basic-block counts and drove two further
bounded checks. Proven 32-bit self-moves covered only 13 weighted executions
and failed both frozen screens. A target API then folded 45 adjacent 64-bit
copies into following zero-displacement loads while retaining the load origin,
address, memory/fault effect and flags; six observed sites cover 483,207
weighted executions, dominated by the 441,399-execution accessor. All exact
installation checks pass, but the 30-block screen is target CPU `1.0004
[0.9875, 1.0153]` and wall `0.9998 [0.9971, 1.0033]`, so it does not advance.
The native x86 result is consistent with hardware move elimination and is not
used as a TCG-cost conclusion.

A final checked x86 rule folds a dead load followed by a same-register test
into a memory test. The default full-register-output firmware contract admits
zero sites. A separately labeled System V ABI arm admits 75 sites, including
18 observed sites and 462,847 block-weighted executions, and all exact-work
installation checks pass. Its combined 130-block selection result is target
CPU `1.0111 [1.0027, 1.0167]` and wall `1.0009 [0.9991, 1.0027]` versus stock
BOLT. Both frozen lower-bound gates remain uncleared, so it is training-rejected,
no held-out sample is consumed, and stock BOLT remains selected.

An ABI-qualified native BOLT pass now also removes exact RBP frames from
single-entry stack-independent leaf bodies and, in two fixed-global-return
functions, removes a proven private nonescaped return spill. The packaged rule
rejects personality/LSDA functions and accepts only absent unwind state or the
exact four-operation canonical RBP unwind program. It qualifies 62 leaf and 2
private-spill functions; 9 observed bodies cover 684,788 block-weighted
entries. An address-stable overlay installs only those nine bodies into the
exact stock-BOLT image, repairs four RIP-relative references to their original
objects, saves 61 live bytes, and changes no byte outside the selected symbol
ranges. Across 100 randomized fixed-work blocks, stock/overlay target CPU is
`0.9983 [0.9910, 1.0082]` and wall is `0.9991 [0.9973, 1.0020]`. All work and
ordered traces match, but neither profitability gate clears, so stock BOLT
remains selected. The stricter packaged implementation re-emits the exact timed
overlay SHA-256. This closes global layout motion as the explanation for the
negative result; it does not authorize frame removal under a firmware
interrupt/observer contract. See Section 8.6 of the main report.

The follow-up now also closes two SmartSpeaker policy hypotheses. An eight-arm
BOLT layout screen froze three candidates before native training; all 40 runs
matched 1,500 markers and the 400-event trace, but every lower confidence bound
was below 1.0, so no layout advanced. The firmware wrapper then overlaid four
effect-qualified hot FOP bodies onto the exact stock-BOLT layout, leaving all
cold/unknown bodies byte-for-byte unchanged. This saves 22 live bytes in place
and records 71 changed-instruction origins with no file growth. The dominant
accessor retains its independently measured 20.0% function-level CPU saving,
but the paced application remains inconclusive at `stock/overlay = 0.9967
[0.9704, 1.0105]`; deployment therefore still retains stock BOLT. A separately
identified 10x schedule then completed 60 valid confirmatory executions, but
`stock/overlay = 1.0044 [0.9911, 1.0148]` also missed its frozen 1.005
lower-bound gate. A 100x calibration saturated shutdown and is invalid.
Reproduce the screen/overlay with
`scripts/preflight/run-smartspeaker-layout-overlay.sh` and the confirmatory run
with `scripts/preflight/run-smartspeaker-10x-confirmatory.sh`.

The latest rerun replaces timer pacing with an exact closed-loop schedule:
20,000 completed markers receive exactly 200,000 acknowledged ticks, and every
arm matches the same 400-event interrupt prefix. A compatible 20,000-unit BOLT
profile was recollected. Full FOP, hot FOP, ICP/inlining, PLT rewriting,
peepholes/register reassignment, and checked compact policies all missed their
frozen training gates. The ordinary compact wrapper installed 568 bodies and
saved 1,596 live bytes; `stock/candidate` target CPU was `1.0105 [0.9834,
1.0336]`. Experimental store removal deleted 497 stores globally, but the
effect gate rejected 234 structurally eligible functions with changed stores;
its safe 377-body subset was `1.0019 [0.9644, 1.0159]`. A fixed-layout policy
exposed and then repaired a relocated absolute callback (`vRecord_Task` at
`0x808840`) using BAT-backed typed rebinding. The repaired 612-body image still
did not clear the gate (`1.0225 [0.9812, 1.0550]`; affinity-isolated `0.9946
[0.9782, 1.0364]`). Exact-profile hot FOP was `1.0091 [0.9696, 1.0387]`.
Stock BOLT therefore remains the SmartSpeaker deployment choice; these are
mechanism findings and valid fixed-work negatives, not proof of equivalence or
optimizer ineffectiveness.

## BOLT-first firmware wrapper

`FastDynFirmwareReport : BinaryFunctionPass` is registered immediately after
BOLT's `FrameOptimizerPass` and `AllocCombinerPass`. The wrapper runs the real
trained BOLT pipeline, enables BOLT Address Translation, checks emitted ELF and
protected-entry obligations, records the prior custom-pass baseline and adopts
qualified emitted BOLT bodies compactly.

The first LEMIX run recorded 1,609 forwarded loads, 253 deleted loads, 22/22
protected entries preserved and 100/100 paced markers. The full BAT-enabled
BOLT ELF is 6,690,240 bytes; selective in-place adoption of the actual BOLT
accessor and list bodies retains the 1,829,560-byte reference size. The native
origin tracker maps 83,377/85,525 post-optimization instructions (97.49%) and
the two named adopted bodies have complete per-instruction mappings. The
conservative automatic cohort imports 115 real BOLT bodies in place, saving 609
live bytes without increasing the ELF. This is a supported-host integration
result, not yet a complete firmware-effect proof for all 115 functions.

The expanded effect-qualified cohort uses exact instruction identities for
conditional control flow and typed source/target CFG-edge identities for
BOLT-synthesized unconditional jumps. Of 2,148 synthesized jumps, 2,140 map to
original edges; the other eight remain rejected. Final-ELF decoding supplies
emitted offsets/sizes and BAT supplies final target translation. The wrapper
installs 855 bodies, validates 4,956 local branches, repairs 1,770 calls, 1,321
RIP-relative references and six absolute callback references, and saves 2,368
live bytes with zero ELF growth. The six absolute references were added after
the initial 1 KiB arm crashed in `main_full`; bisection and disassembly showed
that its callback pointers still named BOLT-output addresses. Exact rebinding
to their original symbol identities repairs the failure. It rejects
eight unproven instructions, 45 changed edge sets and 14 changed targets.
The exact all-qualified image remains inconclusive in 30 paired paced blocks at
CPU-cost ratio 0.9973 [0.9810, 1.0156]; HOT is 0.9930 [0.9788, 1.0222] and ALL
is 1.0034 [0.9668, 1.0318] in the separate three-arm control. These are
CPU-per-marker results, not capacity throughput.

Evidence is in
`build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16/`,
including the native report, exact BOLT command, wrapper decision and separately
labelled custom-pass baseline. The pre-BAT and instruction-only provenance
directories remain historical comparisons.

The fixed-work queue policy matrix is reproduced by
`scripts/preflight/run-bolt-queue-policy-matrix.sh`. Peepholes and conservative
or aggressive register reassignment produced no distinct accepted compact
bytes. Experimental frame-store removal deleted 24 stores in BOLT's full
candidate, but the explicit-effect gate rejected the 13 functions whose stores
changed. Its remaining accepted bodies added nothing beyond ordinary FOP after
in-place repair. The new CPU-pinned timing did not reproduce the earlier small
native gain, while deterministic Callgrind evidence still shows ordinary FOP
removing 17,429,327 instruction references (1.025%). The retained decision is
therefore ordinary FOP plus the firmware wrapper, with native profitability
still workload- and run-dependent. Raw findings and hashes are in
`build/preflight/lemix-queue/bolt-policy-matrix-2026-11-16/report.json`.

An independent 30-block training matrix subsequently selected full
BOLT+ordinary FOP for the relocatable queue policy. On 30 new held-out blocks,
reference/policy CPU was 1.0120 [1.0063, 1.0146] and stock-BOLT/policy CPU was
1.0087 [1.0072, 1.0162], with identical two-million-message checksums. The
fixed-layout policy retains the reference because compact FOP did not clear the
training gate. Evidence is in
`build/preflight/lemix-queue/bolt-firmware-wrapper/queue-policy-held-out-report.json`.
Both choices are atomically published under
`build/preflight/lemix-queue/bolt-firmware-wrapper/published-policy/` using the
same hash-bound deployment schema as the recovered corpus.
The fixed-layout choice is also available as a standard `FirmwareCandidate v1`
under `fixed-layout-firmware-candidates/`. The selected relocated image is a
second `FirmwareCandidate v1` under `relocatable-firmware-candidates/`; its
entry point and 4,027 emitted instructions are attributed through BOLT Address
Translation (BAT), while retained executable sections keep identity origins.

The same policy now covers the rebuilt firmware-derived corpus through
`scripts/preflight/run-recovered-corpus-wrapper.sh`. Training on seed 11 froze
the choice between the full relocated BOLT+FOP image, compact accepted bodies,
and the exact reference; seed 13 was held out. Across three ESCape32
computations, five FreeRTOS list routines and their combined workload, the
relocatable policy generalized six gains and used exact-reference fallback for
three cases. The primary 30-block result was directly faster than the frozen
stock-BOLT policy on eight of nine workloads, inconclusive on `crc8`, and
slower on none. A no-retuning 100-block precision extension then resolved
`crc8` in favor of exact-reference fallback: stock/reference CPU was 1.0074
[1.0004, 1.0116]. Including that declared extension, the policy is faster on
all nine workloads. Representative
held-out reference/policy ratios are 1.5533 for `dshotcrc`, 1.5457 for
`vListInitialiseItem`, and 1.1037 for `uxListRemove`. The fallback avoids
stock BOLT's 0.6992 `crc8dvbs2` and 0.9352 combined-workload ratios. The
fixed-layout policy has three generalized gains and six reference fallbacks.
The policy is now published as 18 immutable, hash-bound deployment decisions
(nine workloads in relocatable and fixed-layout modes), rather than remaining a
benchmark-only label. Each loader rechecks the embedded frozen selection,
held-out evidence, selected artifact and output-acceptance status.
These are supported-host fixed-work results, not MCU or FastDyn speedups. The
machine-readable result is
`build/preflight/recovered-firmware-corpus-2026-11-16/bolt-firmware-wrapper/held-out-policy-report.json`.
The deployable index is under
`build/preflight/recovered-firmware-corpus-2026-11-16/bolt-firmware-wrapper/published-policy/index.json`.
All nine fixed-layout decisions are converted to consumer-loadable
`FirmwareCandidate v1` artifacts under the sibling
`fixed-layout-firmware-candidates/` directory. All nine relocatable decisions
are also consumer-loadable candidates under `relocatable-firmware-candidates/`:
six selected BOLT images use instruction-level BAT origins and three exact-
reference fallbacks use identity origins. This closes supported-host artifact
publication; it does not establish whole-firmware equivalence, Cortex-M
support or FastDyn speedup.

## Recorded patch-independent SDK baseline

`fastdyn.preflight.region`, `sdk`, `references`, `passes`, and `emission`
implement the phase-one path. The pass interface receives an immutable
`FirmwareUnitView`, shared context and proposal-only `RewriteBuilder`. Separate
gates check testcases, the supported semantic rule/effect contract,
profitability, references/placement, atomic installation and final acceptance.
The schema carries an explicit empty patch contract in phase one and retains
identity/anchor/fault-origin fields needed by phase two.

The preserved baseline output is
`build/preflight/preflight-sdk-v1-2026-11-16/`. It includes the complete stage
ledger, positive and negative outcomes, typed reference/state audits, compact
emission sizes, exact origin maps, S2 classifications, a fault-identity
comparison and the Cortex-M3 identity boundary. An external registered pass
example lives at `examples/preflight/external_identity_pass.py`.

The subsequent bounded Thumb fallback is intentionally narrower than a backend.
It implements three registered same-size rules: ordinary FOP's bounded
private-stack spill/reload forwarding, replacement of an exact resolved
unconditional branch-to-next with one architectural NOP, and a checked hot-exit
layout permutation. A real Cortex-M3 candidate is executed under FastDyn, and a
real Cortex-M0+ FOP candidate is structurally emitted. The first two Cortex-M3
policies fail the CPU profitability gate and retain the reference. The third
reorders the same 34 identities in the 92-byte
`prvCheckTasksWaitingTermination` body, repairs its internal references and
makes its common return the fall-through path. Across 30 paired runs of 100,000
completed SysTick returns, reference/candidate CPU cost is 1.0552
[1.0392, 1.0609], guest-region time is 1.0500 [1.0459, 1.0826], and all 2 MiB
RAM-prefix comparisons match. The accepted image grows by zero bytes and
publishes exact instruction origins. This is a real FastDyn/QEMU result for the
pinned image and declared schedule, not a general Thumb optimizer, hardware
cycle result or proof for arbitrary device/interrupt schedules.

The target-cost service now compares optimized TCG operation count and emitted
host-block bytes before full measurement. It rejects the fallthrough-branch
candidate because the inspected translation remains exactly 15 operations and
100 host bytes. It admits the stack-forwarding candidate to measurement because
that block falls from 24/320 to 19/224, but the full-harness gate still rejects
it. The bounded three-site policy further reduces the inspected tick block from
44/624 to 34/448, but its 100,000-tick CPU ratio is 0.9981
[0.9934, 1.0049]. A frozen four-policy ablation also admits no policy at the
1.005 useful-effect threshold. Generated-host inventory is therefore a
rejection prefilter, not a runtime speedup claim. The accepted hot-exit pass
extends that inventory across the complete dynamic hot path: three translation
blocks become two, 72 optimized TCG operations become 61, and 768 generated
host bytes become 696. Full fixed-work measurement remains the acceptance gate.

## Phase boundary

| Concern | Phase one: firmware optimizer | Phase two: FastDyn integration |
| --- | --- | --- |
| Inputs | Exact image, CPU/features, memory map, entries, reserved ranges, optional generic profile | FirmwareCandidate, FastDyn patch plan, device model, instrumentation, inputs and schedules |
| Knows patch syntax/actions | No | Yes |
| Output | Image, origin map, memory/layout evidence, transform/cost report | Rebound patches and an image-bound ExecutionBundle |
| Correctness | Declared architectural and firmware effects for supported regions | Additional hook state/multiplicity and FastDyn event contract |
| Profitability | Consumer-neutral size/target evidence | TCG host work and full FastDyn harness |
| Failure | Identity candidate or explicit optimizer rejection | Reject candidate and execute the reference image |

`fastdyn.preflight.publish_firmware_candidate()` implements the phase-one
artifact boundary. Its schema contains no patch plan, binding, device model,
instrumentation or FastDyn contract.

## Phase-two FastDyn identity path

`fastdyn.preflight.create_identity_bundle()` publishes an unchanged firmware
image and its patch semantics as an immutable `ExecutionBundle v1`. The real
`configs/minimal.toml` configuration was used as an end-to-end Cortex-M3 case.
It emitted four image-bound bindings and reloaded with all hashes verified.
The exact acceptance manifest is retained in
[`evidence/minimal-identity-2026-11-16.json`](evidence/minimal-identity-2026-11-16.json).

Publication uses a sibling staging directory and an atomic rename. The loader
rehashes the image, patch plan, bindings, origin map, memory map,
instrumentation and device-model documents. Symbolic patch anchors must be
resolved against the immutable reference; nearest-address rebinding is
forbidden. The original image remains the fallback.

| File | Purpose |
| --- | --- |
| `execution-bundle.json` | Version, content identity, target, reset entry and environment hashes |
| `image.<suffix>` | Exact image selected for execution |
| `patch-plan.json` | Intended actions and target-qualification evidence |
| `patch-bindings.json` | Stable original identity, exact current PC, phase, action and multiplicity |
| `origin-map.json` | Original-to-emitted half-open executable ranges |
| `memory-map.json` | Declared RAM plus file-backed executable firmware ranges |
| `instrumentation.json` | Coverage and plugin identity material |
| `device-model.json` | Device routing/model identity material |

`bundle_id` is SHA-256 over the semantic identity fields, excluding filenames
and the ID itself. An image, binding, contract or execution-environment change
therefore changes cache identity.

## Phase-two native BOLT guard prototype

The overlay in `tools/preflight/bolt/` implements an experimental phase-two
`FastDynPatchContracts : BinaryFunctionPass` using BOLT's own pass interface.
The integration patch registers it before mutating transformations. With
`--fastdyn-patch-contracts=<patch-bindings.json>`, it:

- validates schema v1 and exact-anchor placement;
- resolves each current PC to a decoder-derived BOLT instruction boundary;
- marks every containing patch-bearing function ignored by later transforms;
- rejects absent/interior/inexact anchors instead of nearest-address matching.

`scripts/preflight/smoke-bolt-guard.py` builds a relocatable x86-64 fixture,
accepts and protects a valid entry hook, emits and executes the rewritten ELF,
rejects a bad anchor, and runs BOLT's existing `StokeInfo` exporter. The pinned
build exported eight functions including the patch target. This is a supported
host phase-two acceptance case, not part of the standalone optimizer and not
Cortex-M qualification.

## Reuse and baseline evidence

The pinned STOKE revision was built in its historical compatible container.
Its upstream popcount tutorial completed extraction, testcase generation,
search, bounded verification and binary replacement. Search found a two-line
`popcntq; retq` rewrite; the verifier returned `Equivalent: yes`; reference and
rewritten binaries had equal exit status on five inputs. The retained report is
[`evidence/stoke-supported-fixture-2026-11-16.json`](evidence/stoke-supported-fixture-2026-11-16.json).
This establishes reusable phase-one x86 machinery under that fixture's model,
not ARM semantics or the separate FastDyn integration contract.

The separately trained stock-BOLT control used instrumentation-derived `.fdata`
and frozen `ext-tsp`, `cdsort`, function splitting and cold splitting options.
It retained the [raw profile](evidence/bolt-pgo-control-2026-11-16.fdata),
commands, hashes and 15 interleaved wall-time samples in
[`evidence/bolt-pgo-control-2026-11-16.json`](evidence/bolt-pgo-control-2026-11-16.json).
The observed median original/BOLT ratio was 1.0144 on this x86-64 fixture. The
report expressly sets Cortex-M, FastDyn and hardware-cycle claims to false.

## Phase-two observation validation

`fastdyn.preflight.validation` reads versioned JSONL traces and returns one of
`accepted`, `counterexample`, `unknown` or `unsupported`. The strict comparator
checks the ordered event prefix for guest accesses, fault kind/attribution and
recovery state, detector/device/callback effects, patch trigger phase and
observed state, coverage, interrupt opportunities, modeled guest time and
termination. Emitted/current PCs and host timestamps are non-semantic; original
guest identity is compared. Different inputs, schedules or environment
identities are `unknown`, not semantic failures.

The checker is not yet wired into QEMU event production. Its tests cover a
legal moved anchor, changed pre-hook state, removal of an unused faulting load,
prefix truncation, unknown events and schedule mismatch.

## Reproduction

```bash
scripts/preflight/checkout-tools.sh
scripts/preflight/build-bolt.sh
scripts/preflight/smoke-bolt-guard.py \
  --bolt third_party/preflight/llvm-build/bin/llvm-bolt \
  --work-dir third_party/preflight/bolt-guard-smoke
scripts/preflight/reproduce-stoke-tutorial.sh
scripts/preflight/benchmark-bolt-pgo.py \
  --bolt third_party/preflight/llvm-build/bin/llvm-bolt \
  --work-dir third_party/preflight/bolt-pgo-control
scripts/preflight/run-preflight-prototype.sh \
  build/preflight/preflight-sdk-v1-2026-11-16
scripts/preflight/run-bolt-firmware-wrapper.sh \
  build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16
python3 scripts/preflight/build-smartspeaker-stock-frame-overlay.py
python3 scripts/preflight/run-smartspeaker-stock-frame-overlay-training.py || \
  test $? -eq 2
scripts/preflight/run-smartspeaker-policy-workflow.sh
PYTHONPATH=src taskset -c 0 \
  python3 scripts/preflight/profile-thumb-unused-frame-pointers.py --replace
PYTHONPATH=src taskset -c 0 \
  python3 scripts/preflight/run-thumb-hot-exit-layout-pilot.py --replace
```

For Cortex-M0/M0+/M3/M4/M7, the direct BOLT backend is now the primary target path and
unchanged functions remain the per-region fallback. The older same-size Python
fixtures—private-stack spill/reload forwarding, exact branch-to-next replacement
and the hot-exit layout permutation—remain preservation and profitability
fixtures rather than the backend. General literal relocation, veneer repair,
complete exception-return recovery, trace production and device/interrupt effect
modeling remain explicit later capabilities. A deterministic real-firmware
fixed-work path exists through `benchmark_end_after` and QEMU `icount`; its
checked reload and fallthrough-branch candidates are not profitable. The
hot-exit layout clears its frozen CPU gate, establishing one bounded
FastDyn/QEMU acceleration result while leaving broad target and preservation
claims open.

See [gap-table.md](gap-table.md) for the current capability classification and
bounded next extensions. The combined evidence summary, raw-report locations
and hashes are retained in
[`evidence/evaluation-summary-2026-11-16.json`](evidence/evaluation-summary-2026-11-16.json).
