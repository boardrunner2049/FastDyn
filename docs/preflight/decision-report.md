# Student 1 evaluation decision

Recorded 16 November 2026.

## Decision

Continue a standalone firmware optimizer with patch-independent candidate and
origin artifacts, using BOLT as its binary infrastructure rather than treating
stock BOLT as the complete optimizer. The new LEMIX experiment removes missing
Thumb support as a confounder: on an actual O0 x86-64 rehosted firmware
application, BOLT changed layout and improved its instruction/branch model but
did not improve completed work, while STOKE's direct extractor reached less
than 1% of attempted functions. This is evidence for a firmware-specific
objective and adapter, not evidence for replacing BOLT or the STOKE lineage.

Do **not** claim a firmware speedup from the current evidence. The measured
SmartSpeaker path is device- and timer-paced under a low-fidelity MMIO backing
shim, and the result is neutral.

The next implementation gate should first close the supported-ISA end-to-end
path demonstrated by SmartSpeaker: BOLT-selected body export without padding,
precise stack/effect classification, deterministic O0 canonicalization as an
ablation, existing STOKE search/checking, candidate import, compact emission
and useful-work/size acceptance. In parallel, the first target gate remains one
bounded Thumb round trip on a synthetic ELF:
decode, preserve a vector/reset entry, rewrite a feature-qualified leaf region,
repair a branch and PC-relative literal reference, emit, and execute. If that
gate cannot be completed within its budget, retain identity candidates and move
the optimization evaluation to a BOLT-supported ISA. Do not respond by writing
a new general search engine.

This is a conditional **go** for the firmware optimizer foundation and a
**no-go for performance claims or broad MCU scope**. FastDyn patch rebinding,
hook semantics, observation comparison and TCG profitability are deferred to a
separate phase two that consumes the optimizer's image and origin map.

The detailed customer case is not that stock tools are ineffective. It is that
firmware customers need exact-image optimization, target qualification,
firmware placement, stable provenance and explicit rejection. The optimizer
stays useful outside FastDyn; phase two adds patch and runtime policy. See
[`customer-motivation.md`](customer-motivation.md) for the full reasoning,
customer segments, economic model, differentiation and staged roadmap.

The stronger design conclusion is now supported both by source audit and a
supported-ISA application measurement: ISA support alone cannot close the gap.
In the declared ten-requirement firmware matrix, BOLT supplies 2 requirements
natively, offers adaptable machinery for 3 and lacks/differs on 5. The selected
STOKE implementation supplies 1 natively, 1 through adaptation and lacks or
uses a different model for 8. In particular, its represented state omits device
transactions and interrupt schedules, while its cost model uses Haswell
latencies. The formal argument and source anchors are in
[`design-fit-audit.md`](design-fit-audit.md).

## Evidence matrix

| Track | Result | Decision meaning |
| --- | --- | --- |
| Phase-two native BOLT contract guard | Exact entry accepted and protected; emitted x86 binary ran; invalid PC and unqualified clone policy rejected | The later FastDyn integration point is viable. It is not part of phase-one optimization. |
| Supported STOKE control | Pinned x86 popcount tutorial completed search, bounded verification and replacement | Existing machinery is reusable where its target/model applies. |
| Recovered firmware-derived native corpus | Eight functions, six optimization levels, 11 interleaved samples; all outputs agreed | There is compiler headroom, but it is not evidence of attainable binary or MCU speedup. |
| Trained BOLT on recovered host corpus | Combined O0/BOLT-PGO ratio 0.9697; restricted ratio 0.9446. PGO was 1.0266 times the restricted rewrite. Per-function outcomes were mixed. | Training improved BOLT versus the restricted control, but neither beat the original combined workload. A separate simple x86 control retained a 1.0144 ratio. |
| O0 LEMIX SmartSpeaker application | 194/1,337 functions profiled; BOLT modeled -3.1% instructions and -6.4% branches; all variants completed 500 dispatches/5 s; trained median CPU ratio 0.9987 (95% bootstrap 0.8978--1.0743); text +18.24% | On a supported ISA and firmware-shaped application, BOLT's native model improvement did not yield measured useful-work benefit and carried code growth. This is a neutral result, not a slowdown claim. |
| STOKE on O0 LEMIX SmartSpeaker | `StokeInfo`: 23/194 hot structural candidates, covering 4.54% of hot static instructions and 14.13% of score. Direct extractor emitted 9/1,332 attempted functions; only 1/23 hot candidates reached search; no rewrite after 10M proposals. | The dominant measured gap is extraction/state integration before search. The surviving accessor also motivates a deterministic O0-cleanup baseline, while retaining existing STOKE verification where applicable. |
| STOKE on recovered host corpus | Three `no_candidate` results at one million proposals; five pointer/memory cases could not generate testcases | No missed optimization is established. The selected engine/test generator has concrete applicability gaps. |
| STOKE trace generation | Pin 2.13 aborts on auxiliary-vector type 51 on this host | Modernize/replace the testcase capture adapter before judging memory-heavy search quality. |
| Cortex-M STOKE extraction | M0+ and M3 report objdump architecture `UNKNOWN`, return zero, and emit empty directories | Selected engine has no usable ARM/Thumb path and its adapter must reject empty-success output. |
| Cortex-M BOLT attempt | All M0+/M3 images fail with `BOLT-ERROR: Unrecognized machine in ELF file.` | The missing ARM32/Thumb backend is demonstrated, not hypothetical. |
| Firmware design-fit audit | BOLT: 2 native, 3 adaptable, 5 absent/different requirements; selected STOKE: 1, 1 and 8 respectively | A Thumb port is necessary but insufficient. Firmware state/effects, placement, fault semantics and cost must be first-class services. |
| Firmware qualification | Reset vectors and executable ranges qualify; no target ELF retains relocation sections | Identity publication is viable. Reference recovery, layout repair and placement validation are mandatory for rewriting. |
| Phase-two FastDyn unchanged M3 baseline | 15/15 runs passed; guest marker median 1.307 ms; total internal run median 114 ms; process wall median 287 ms | A future integration baseline exists. It is not a phase-one objective or optimized comparison. |
| FreeRTOS/introspection | Passed with the QEMU build matching the plugin | RTOS/interrupt observer execution is available. The smoke script's default QEMU path is stale and incompatible. |
| MMIO/effect experiment | Fast substitution case: 24.22 ratio; normal observer: 0.9985; late observer: 1.1146 with 1,000,012 fallbacks | This is not preflight performance evidence. It proves observer/effect policy and fallback costs materially change the FastDyn objective. |
| Artifact and preservation tests | 30 tests pass: six patch-free/boundary FirmwareCandidate cases plus 24 phase-two anchor/event/bundle cases | The phase boundary is enforced in the candidate schema and import graph; QEMU trace production remains unintegrated and finite tests do not prove unknown-bug preservation. |

## Firmware applicability

The recovered ESCape32 source does not reproduce the retained E1 memory
configuration. Its linker script declares 32 KiB total flash, leaving 26,624
bytes after the 4 KiB boot and 2 KiB configuration reservations. The current
`-Os` image uses 25,080 ROM-accounted bytes, leaving only 1,544 bytes. O0 and O3
overflow that recovered configuration.

A separately labeled 64 KiB linker replica was built to compare with the E1
budget of 59,392 bytes. It produced 25,080 bytes at Os, 37,548 at O0 and 33,620
at O3. Those numbers do not reproduce E1's 35,624/56,556/49,348 values, so the
old run remains historical evidence rather than a reproduced result.

The clean FreeRTOS MPS2/Cortex-M3 build produced 108,636, 109,680 and 108,752
ROM-accounted bytes at Os, O0 and O3. Its linker reserves 64 KiB of privileged
flash and 32 KiB of privileged initialized RAM in the load image, which explains
why simple GNU `size` totals differ from the retained table. The reset vector at
address zero was verified and prompted a fix: address zero must not be treated
as an absent vector base.

Both targets have no emitted relocation sections. ESCape32 Os contains 7,964
decoded instructions, including 1,154 PC-relative loads and 1,747 branches.
The M3 Os image contains 11,840 instructions within sized function symbols,
including 3,497 wide Thumb instructions. A decoder alone is therefore
insufficient; reference recovery,
branch reach, literals, padding/reservations and exception semantics are part
of the first backend gate.

## Concrete BOLT and STOKE gaps

### Already reusable

- BOLT's native pass manager, binary model and `StokeInfo` analysis/export.
- STOKE x86 extraction, search, bounded checking and replacement on its
  supported tutorial fixture.
- BOLT instrumentation profiles and trained layout passes on supported ISAs.
- Consumer-neutral image/origin artifacts; the FastDyn execution bundle remains
  a phase-two wrapper.

### Implemented under a different model and needing adaptation

- `StokeInfo` exports alias-expanded register sets. The evaluation adapter had
  to normalize these to ABI arguments, return/frame/stack state and heap effects.
- STOKE's supported equivalence/checking path still needs a declared firmware
  effect model for the supported phase-one region class.
- BOLT address translation is useful, but phase one must export exact provenance
  for arbitrary later consumers.
- Native host timing and BOLT layout cost do not predict every consumer's cost.

### Demonstrably absent or broken for the selected runtime

- ARM32/Thumb dispatch and emission in the pinned BOLT build.
- ARM/Thumb extraction/semantics in the selected STOKE engine.
- Reliable STOKE success signaling for unsupported ARM input.
- STOKE autogenerated testcases for five memory-heavy recovered functions under
  the attempted configuration.
- Pin-based traced testcase collection on this modern host.
- Firmware relocation/reference evidence in the target ELFs.
- Candidate import and atomic FirmwareCandidate publication after optimization.
- A robust whole-application STOKE extraction path: the pinned extractor
  emitted 9 of 1,332 attempted functions from the x86-64 SmartSpeaker ELF,
  largely failing on padding syntax.
- A firmware profitability objective joining completed work and emitted text:
  trained BOLT predicted 3.1% fewer instructions but delivered neutral work and
  increased text by 18.24% in the SmartSpeaker experiment.
- In separate phase two: QEMU normalized traces, patch rebinding and a FastDyn
  cost comparison.

## Recommended bounded sequence

1. Publish patch-independent identity FirmwareCandidates as the phase-one
   fallback.
2. Implement the supported-x86 BOLT/STOKE adapter using BOLT-decoded bodies,
   excluding padding and distinguishing stack from external memory. Add a
   deterministic O0 cleanup baseline and repeat the frozen SmartSpeaker case
   before changing stochastic search.
3. Add a preselected input-driven LEMIX subject so completed inputs, rather
   than timer dispatches, provide a higher-signal held-out metric.
4. Implement the synthetic Thumb backend gate. Start with ARMv6-M instructions
   and no MMIO/exception paths; reject unknown references.
5. Only after the gate passes, choose one real target and one leaf-region class.
   Measure emitted size after repair and a frozen standalone target metric.
6. Stop phase one if no accepted target candidate beats the unchanged image
   after preparation and code-growth accounting.
7. In phase two, consume the origin map, import/rebind FastDyn patches, fix the
   QEMU/plugin identity check, add normalized observation emission, and measure
   TCG/full-harness cost. An incompatible candidate falls back to the reference.

Raw-report hashes and exact summary values are in
[`evidence/evaluation-summary-2026-11-16.json`](evidence/evaluation-summary-2026-11-16.json).
The complete paper-style LEMIX evaluation is in
[`lemix-o0-evaluation.md`](lemix-o0-evaluation.md).
