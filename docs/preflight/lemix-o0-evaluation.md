# BOLT-first optimization with a firmware acceptance and compact-emission layer

**Date:** 17 November 2026
**Project:** BinFast / FastDyn, Student 1
**Scope:** focused supported-host baseline plus patch-independent preflight prototype

## Abstract

We reran the LEMIX SmartSpeaker experiment to separate six previously conflated causes of failure: disabled optimization, extraction, state/equivalence contracts, stochastic search, emitted layout, and external timer pacing. We reused the exact unoptimized SmartSpeaker ELF (SHA-256 `b2ceed21fb5ac2dc2723bc6404822edacfa19675bc0fd93a54e17dc909342e8b`), frozen profile, and zero-backed MMIO shim, and pinned BOLT and STOKE to the required revisions. The result changes the earlier diagnosis in three important ways.

First, BOLT's x86 frame optimizer was disabled in the historical run because `frame-opt` defaults to `none`. Enabling its ordinary load-forwarding mode optimized 1,609 redundant stack loads and deleted 253; the deleted loads represent 151,152 profile-weighted executions. On the exact 29-byte `prvGetThreadFromTask` body, BOLT produced a legal 24-byte body and reduced fixed-work CPU cost by 20.0% (reference/FOP ratio 1.2501, paired 95% interval 1.2410–1.2604). This is a function-level result, not whole-application acceleration. In the timer-paced SmartSpeaker application, R/F1 was 0.9949 (0.9568–1.0166); all arms still completed about 500 timer-supplied dispatch markers in five seconds, so SmartSpeaker capacity remains unestablished. A separate two-million-message queue fixture now supplies a bounded fixed-work whole-program comparison, reported below.

Second, the dominant STOKE failure was representation, not search. The stock path admitted only 1 of 23 frozen structural candidates. Exact decoding showed that 22 candidates began with `endbr64`, not disposable padding; the ELF declares IBT/SHSTK properties. Those properties establish a protected-entry requirement for this study, but they do not establish that runtime enforcement was enabled. Preserving the four-byte entry prefix and exporting only the searchable fall-through body raised extraction to 23/23. The bottleneck then moved to semantic linking and state construction: 10/23 reached states, 11/23 retained unresolved RIP-relative/external references, and 2/23 failed memory resizing.

Third, the proposed three-instruction accessor is not legal under the admitted memory-output contract. If the input pointer aliases the original spill slot at entry stack address `S-0x20`, the original store overwrites the pointed-to object before the load, whereas the short rewrite reads the old value. Ordinary inputs conceal this difference. BOLT's ordinary frame optimization retains both stores and passes the declared STOKE checks; BOLT's experimental store removal produces the same semantic mismatch as the short rewrite. Legal normalization improved the testcase objective in all eight S2 runs, but improved verified search effectiveness remains unestablished: three candidates produced concrete verifier counterexamples and five ended unknown without a retained counterexample, for 0/8 verified results. One cold `_fini` candidate was supplied by initialization with zero proposals, then verified, emitted, installed, and executed. It is a cold integration control, not a discovered hot-path optimization or a performance result.

The design conclusion is not that existing binary optimizers are ineffective. It is that their optimization strength needs a firmware-specific acceptance boundary: exact code/reference identities, entry constraints, observable memory and fault behavior, fixed-layout/code-growth budgets, and completed-work cost. The bounded custom `PreflightPass SDK v1` run is retained in Section 7 as an informative baseline, but it is no longer the proposed optimizer. Section 8 reports the corrected architecture and its first execution: real pinned BOLT optimizes first, a native post-optimization pass exports the candidate state, and the firmware wrapper validates or compactly adopts the result. Patch actions remain empty in phase one.

The matched SmartSpeaker FreeRTOS O0/Firm-BOLT/O3 experiment now closes the requested supported-host comparison. Across 30 randomized exact-work blocks, Firm-BOLT/O3 target CPU cost is 0.9903 [0.9733, 1.0093], wholly within the frozen five-percent parity band. O3 itself is unresolved against O0 for this host-scheduled workload. The result supports BOLT reuse plus firmware acceptance and compact emission; it does not establish Cortex-M or FastDyn acceleration.

## 1. Research question and boundary

The experiment asks whether optimization policies—not missing Cortex-M engineering—can exploit an O0 firmware-derived workload after LEMIX recompiles it as a supported x86-64 application. It does **not** measure an original MCU binary, hardware timing, a realistic device model, or vulnerability preservation. The preload shim only maps fixed addresses to zero-backed memory. It makes the application runnable; it does not reproduce peripheral state.

The frozen reference has 97 compile commands, all with `-O0 -g -fno-omit-frame-pointer`; the link uses pthreads, non-PIE output, and emitted relocations. The build log records 377 pointer-width warnings, which is relevant when interpreting a 64-bit rehost as evidence about 32-bit firmware. The reference, profile, preload, BOLT binary, archive, build log, host, CPU affinity, governor, and recovered historical controls are pinned in [manifest.json](../../build/preflight/lemix-o0-rerun-2026-11-16/manifest.json).

### 1.1 Historical screening experiment and why it was rerun

The first screening experiment used the same SmartSpeaker reference but did not enable BOLT frame optimization. It compared the reference with an unprofiled restricted rewrite and a trained layout rewrite using 11 five-second samples per arm. All arms produced 500 timer-supplied dispatch markers. The trained arm's reference/candidate CPU ratio was 0.9987 with an independently resampled 95% interval of 0.8978–1.0743; the restricted arm's ratio was 0.9817 with interval 0.8695–1.1068. BOLT's pre-FOP profile model predicted 3.1% fewer executed instructions and 6.4% fewer branches, but the timer-paced measurement could not resolve a CPU effect.

The historical STOKE route attempted 1,332 functions, emitted 9, and admitted only `prvGetThreadFromTask` from the frozen 23-candidate cohort. Four searches totaling 10 million proposals found no new rewrite. That experiment correctly exposed an end-to-end availability problem, but it incorrectly treated the leading `f3 0f 1e fa` bytes as disposable alignment and used the short accessor as if its legality were self-evident. It also inferred too much from whole-ELF growth and equal timer-paced work counts.

The focused rerun retains those historical observations as a screening baseline and corrects their interpretation with controlled contrasts:

| Question | Historical evidence | Focused rerun |
| --- | --- | --- |
| O0 dataflow | Layout and rewrite controls; FOP left at `none` | `L0/L1/F0/F1/FH` matrix with ordinary and hot FOP |
| Timing | 11 samples; independently resampled arms | 30 randomized paired blocks; CPU seconds per actual marker |
| Capacity | 500 markers in five seconds | Still timer-capped; capacity explicitly unestablished |
| Local execution | No fixed-work exact-body result | 30 paired blocks over byte-identical accessor bodies |
| Extraction | 1/23 structural candidates reached STOKE | 23/23 after preserving the `endbr64` entry prefix |
| Accessor target | Short form presented as obvious cleanup | Alias and fault probes plus STOKE contract checks |
| Search | One body, original seed, 10 million proposals | Six frozen cases; S1/S2; 70 million proposals; separate proof/install gates |
| Growth | Aggregate text/file growth | Retained original, replacement, alignment, and executable mapping inventory |

This report is therefore the single authoritative narrative for both experiments. Historical raw files and logs remain unchanged so that the corrections are auditable.

## 2. Experimental design

### 2.1 BOLT matrix

One frozen profile was used for all rewritten arms. Every unrelated option was explicit. `L0` and `F0` disable layout; `L1`, `F1`, and `FH` use `ext-tsp`, `cdsort`, function splitting, and all-cold splitting. `F0/F1` use `--frame-opt=all`; `FH` uses `hot`. Experimental store removal (`FRS`) is diagnostic and excluded from the primary matrix. The historical unprofiled restricted control remains separate from profiled `L0`.

The primary timing protocol used 30 complete randomized blocks, with all six variants randomized within each block. Ratios are reference CPU seconds per dispatch divided by candidate CPU seconds per dispatch; values above one favor the candidate. Intervals bootstrap complete paired blocks 100,000 times. A 5% ratio was frozen as the minimum useful effect.

### 2.2 STOKE funnel

The original 23 profiled, loop-free, call-free candidates were retained. Identities use file offset, virtual address, size, and byte hash rather than names. We distinguish stock extraction, canonical export, state construction, search, testcase correctness, bounded verification, emission, installation, and final acceptance. S1 searches canonical unchanged bodies. S2 starts from a checked deterministic BOLT-FOP normalization but uses the same contract, states, four seeds, and 2.5-million-proposal limit per seed. Only six preselected cases were searched.

## 3. BOLT findings

### 3.1 The historical run did not execute frame optimization

This is source-confirmed and command-confirmed. In the pinned source, `frame-opt` defaults to `none`, and the recovered historical commands did not override it. The named dynamic-statistics stage also reports counts before SCTC/FOP; those figures are not a final post-FOP instruction inventory. The historical controls and logs are preserved and hashed in the new manifest rather than overwritten.

With ordinary FOP enabled, both `F0` and `F1` report 1,609 optimized redundant loads, all replaced from registers, and 253 deleted loads. No stores are removed. The full-mode profile frequencies are 279,227 optimized-load executions and 151,152 deleted-load executions. Frame restoration failed for 168 functions representing 5.2% dynamic coverage, and 69 functions were not optimized; these are explicit eligibility limits, not silent failures.

Hot-only FOP transforms only 39 loads and deletes 13, but captures 98.53% of the full-mode dynamic redundant-load frequency and 98.67% of deleted-load frequency. Thus 2.42% of the static transformations cover almost all of the profiled dynamic opportunity. This is strong evidence that a profile can prioritize safe dataflow work; it is not evidence that cold code is semantically dispensable.

### 3.2 Representative example: legal load forwarding versus illegal store removal

The exact original accessor is:

```asm
pushq %rbp
movq  %rsp, %rbp
movq  %rdi, -0x18(%rbp)
movq  -0x18(%rbp), %rax
movq  (%rax), %rax
movq  %rax, -0x8(%rbp)
movq  -0x8(%rbp), %rax
addq  $0x8, %rax
popq  %rbp
retq
```

Ordinary FOP forwards the two reloads but retains the stores. The result is 9 instructions/24 bytes instead of 10/29. The body used by the native harness is byte-identical to the production BOLT body, as recorded in [body-provenance-frs.json](../../build/preflight/lemix-o0-rerun-2026-11-16/accessor-harness/body-provenance-frs.json). Over 734,366,871 calls per sample and 30 paired blocks, its reference/FOP ratio is 1.2501 (1.2410–1.2604), corresponding to 20.0% lower CPU cost per call.

Experimental `--frame-opt-rm-stores` removes 497 stores program-wide (167,603 profile-weighted executions) and shrinks this accessor to 7 instructions/16 bytes. It is not admissible under the broad memory-output contract. In the concrete alias state, the initial object contains `0x1234` and the pointer equals the spill address. The original and FOP bodies store the pointer before loading and return `pointer+8`; the short and FRS bodies leave `0x1234` in memory and return `0x123c`. This is a concrete semantic counterexample, not a timeout or unsupported-input result.

Invalid-pointer probes also fault at address 1, but at different host instruction offsets: original 12, FOP 11, short 0, and FRS 7. Offset changes alone are not lost attribution. The prototype maps the FOP load back to the exact original instruction identity and compares fault kind, address, width, required state, and ordered event prefix; that declared comparison accepts. These remain host provenance observations and do not establish original MCU fault-PC preservation.

### 3.3 CPU cost changed locally, not at the timer ceiling

| Comparison | Median paired ratio | Paired bootstrap 95% interval | Interpretation |
| --- | ---: | ---: | --- |
| R / F1 | 0.9949 | 0.9568–1.0166 | no observed 5% useful gain; not semantic equivalence |
| L1 / F1 | 1.0004 | 0.9693–1.0336 | incremental FOP unresolved at application scale |
| L0 / F0 | 0.9944 | 0.9651–1.0277 | same conclusion without layout |
| F0 / F1 | 0.9973 | 0.9800–1.0314 | layout effect unresolved |
| R / FH | 0.9895 | 0.9353–1.0460 | hot-only FOP unresolved |

All 180 primary application samples passed the output/return/work checks. Median dispatch markers were 500 for every arm because the event source supplies one approximately every 10 ms. Median process CPU cost per marker was 0.000166872 s for R and 0.000170106 s for F1. Equal marker counts therefore say nothing about capacity. The function-level replay establishes that useful CPU savings exist in an exact hot body, but their application share is too small or noisy to resolve under the paced fixture. No existing compute-limited whole-application route was substituted after seeing results, so whole-application capacity remains unestablished.

A declared 100-block precision extension on the unchanged five-second fixture refuted the earlier exact-reference fallback: stock BOLT/reference CPU cost was 0.9864 [0.9790, 1.0000], where a value below one means stock BOLT used less CPU. The next mechanism was frozen before measurement: hot-only FOP applies 39 load transformations while retaining 98.53% of full FOP's profiled redundant-load frequency. Thirty paired 30-second blocks completed 3,000 markers per arm and matched the same 400-interrupt trace prefix. Stock BOLT/hot-FOP CPU cost was 1.0034 [0.9929, 1.0240]; it did not clear the 1.005 deployment gate, so the policy retains stock BOLT. That exact timed stock-BOLT image is atomically published as a `FirmwareCandidate v1`. A separately rebuilt BAT carrier has byte-identical executable sections and supplies origins for 85,731 relocated instructions without replacing the deployed image; entry coverage is complete. Policy/candidate index SHA-256 values are `26555fbc8d304afdc96f7201c3b704985b0d287ba8533789d2ddc67e99ccf2e1` and `2a3e5bf1f678cf2c9fe4cf0601428d40ccab81b7f58ca51f16270f6cd2164592`. This is a measured non-regression fallback, not a strict SmartSpeaker speedup or a capacity result.

A separately frozen BOLT-native layout screen compared eight BAT-enabled FOP layouts while holding dataflow treatment fixed. Callgrind was unsupported because Valgrind did not retain the zero-backed MMIO mapping; this was recorded as an environment failure, not a candidate failure. Static profile-weighted dynostats selected no-split, ordinary split and `cdsplit` before native training. All 40 executions completed 1,500 markers and shared the same 400-event trace. Stock/candidate CPU results were `ext_cdsort_cdsplit` 1.0094 [0.9580, 1.0452]; `ext_cdsort_nosplit` 1.0025 [0.9804, 1.0364]; `ext_cdsort_split` 1.0053 [0.9581, 1.0433]. No lower interval bound exceeded 1.0, so no layout advanced and no held-out result was consumed.

The firmware wrapper then composed four checked, profile-prioritized ordinary-FOP bodies directly into the exact stock-BOLT layout, leaving all cold/unknown bodies unchanged. The overlay covers 26,634 profiled function entries, saves 22 live bytes inside existing capacities, and records 71 exact changed-instruction origins with no file growth. Twenty matched 30-second executions completed 3,000 markers and the same interrupt prefix; stock/overlay CPU was 0.9967 [0.9704, 1.0105], so deployment still retains stock BOLT. The dominant overlaid accessor is byte-identical to the previously measured FOP body and therefore retains its function-level 20.0% CPU-cost reduction; this does not establish application capacity. A separately identified 10x tick calibration completed 5,000 markers per arm. A frozen 30-block confirmatory run in that 10x environment completed 60 valid 20-second executions at approximately 20,000 markers each. Stock/overlay CPU-per-marker was 1.0044 [0.9911, 1.0148]; its lower bound did not clear the predeclared 1.005 gate, so it also does not advance the overlay. The 100x calibration saturated shutdown and killed the stock process, so its apparent ratio is invalid.

The rerun now also has a valid exact-work SmartSpeaker schedule. The interposer disables the wall timer and injects exactly ten acknowledged ticks for each completed interrupt-task marker. Every timed execution below completed 20,000 units and 200,000 ticks and matched the same ordered 400-event interrupt prefix. A newly collected compatible BOLT profile makes scheduler and support-code hotness explicit; it does not turn the zero-backed MMIO shim into a device model.

No exact-schedule arm cleared the frozen gate. Exact-profile full FOP had stock/candidate target CPU 1.0052 [0.9976, 1.0103]. Existing PLT rewriting, inlining, ICP, register reassignment, peepholes, read-only-data simplification and their frozen combinations also failed training. The ordinary checked compact wrapper installed 568 bodies (50 profiled) and saved 1596 live bytes; its target-CPU ratio was 1.0105 [0.9834, 1.0336].

Experimental frame-store removal deleted 497 stores in the relocated BOLT image, but the firmware effect gate rejected 234 structurally eligible functions with changed explicit stores. The remaining 377 installable bodies reached only 1.0019 [0.9644, 1.0159]. This is the intended contract boundary, not a claim that BOLT store removal is generally unsound.

Disabling layout/splitting exposed a missing absolute-reference repair: the first compact image copied `vAudio_Task` with `vRecord_Task` still bound to BOLT address `0x808840` and faulted there. BAT-backed typed rebinding repaired that exact mechanism. The accepted fixed-layout image installs 612 bodies (94 profiled), repairs 2 absolute references and saves 1683 live bytes. Its shared-CPU target ratio was 1.0225 [0.9812, 1.0550]; a separately identified target/driver affinity-isolated run was 0.9946 [0.9782, 1.0364]. Exact-profile hot-only FOP retained 1,608,668 weighted deleted-load executions but was 1.0033 [0.9904, 1.0112]. Every wall-time lower bound also missed its gate, so deployment continues to retain exact-profile stock BOLT. The mechanism findings are nevertheless concrete: exact completed-work scheduling is now valid; explicit-store rejection works; unresolved relocated address-valued immediates are an installation blocker; and broader checked adoption does not by itself establish profitability.


### Native Firm-BOLT extensions and high-precision selection

The final implementation extends the pinned BOLT pass pipeline rather than running a separate raw optimizer. First, a conservative x86-64 arithmetic pass replaces the exact O0 multiply-by-40 sequence `mov; shl $2; add; shl $3` with `lea; shl $3`, retaining the final flag-producing shift and exporting every consumed original identity. It found 61 static fusions, 12 with profile evidence and 824,205 profile-weighted executions. The firmware wrapper accepted 855 compact bodies and saved 2,613 live bytes. Nevertheless, the frozen 100-block precision comparison was neutral: stock/Firm-BOLT target CPU was 1.0003 [0.9931, 1.0102] and wall time was 1.0008 [0.9976, 1.0027]. Exact completed work and the ordered trace matched, so the result is `not_profitable`, not a correctness failure.

Second, Firm-BOLT extends BOLT's own frame optimizer to forward available 64-bit stack values into `CMP64mi8`, `CMP64mi32`, `CMP64mr` and `CMP64rm` while retaining the establishing store and exporting the dependency. This raises ordinary FOP activity from 1,609 to 3,601 static redundant loads (+1,992) and from 2,944,878 to 3,294,026 profile-weighted loads (+349,148). The wrapper records 2,919 accepted dependencies, installs 907 compact bodies and saves 4,174 live bytes. Its frozen training result was also not deployable: stock/extended-FOP target CPU was 1.0016 [0.9920, 1.0106] and wall time was 0.9965 [0.9933, 1.0006].

The precision extension also resolves the ordinary full-FOP question. Across 300 paired blocks, stock/full-FOP target CPU was 1.0052 [0.9976, 1.0103] and wall time was 1.0000 [0.9997, 1.0008]; its confidence bounds do not clear the frozen CPU and wall gates. An isolated peephole arm patched 25 double jumps and passed exact-work smoke, but ordinary-FOP/peephole target CPU was 0.9938 [0.9876, 1.0242]. Therefore the SmartSpeaker policy correctly retains stock BOLT. These are measured profitability rejections under an externally scheduled workload, not evidence that arithmetic fusion, load forwarding or peepholes are generally ineffective, and they do not establish whole-application capacity.


### Matched FreeRTOS SmartSpeaker O0/Firm-BOLT/O3 comparison

The same pinned LEMIX SmartSpeaker source/rehost was rebuilt with GCC `-g -O3` and linked with `-g -O3 -pthread -no-pie -Wl,--emit-relocs`, then compared with the exact O0 input after Firm-BOLT. The manifest records source revision `e44dca2edbc3b32cb1f2a48d3d0be203bd9b5576`, 97 compile commands and 436 warning lines; the host rehost's pointer-width warnings remain an applicability limitation. O3 is only a performance and mechanism oracle: no O3 body is installed into the O0 image and source-level assumptions do not discharge the machine contract. The final matrix used 30 randomized blocks, 20,000 completed markers and 200,000 acknowledged ticks per execution at the existing 100 us settle interval. All 90 executions returned the expected status and shared the same ordered 400-event prefix.

Firm-BOLT/O3 target CPU cost was 0.9903 [0.9733, 1.0093], wholly inside the frozen [0.95, 1.05] parity band. This establishes comparability, not superiority: the interval spans both directions. Firm-BOLT/O0 was 0.9869 [0.9761, 1.0020], and O3/O0 was 1.0039 [0.9892, 1.0229].

| Representative function | O0 bytes | Firm-BOLT live bytes | O3 linked-component bytes | Observed mechanism |
| --- | ---: | ---: | ---: | --- |
| `vListInsert` | 180 | 168 | 82 | FOP installs 168 B; O3 adds register/CFG optimization |
| `uxListRemove` | 133 | 126 | 62 | FOP installs 126 B; O3 adds register/CFG optimization |
| `event_wait` | 95 | 94 | 73 | FOP removes one byte; O3 removes frame/data movement |
| `xTaskIncrementTick` | 925 | 925 | 702 | full FOP is 896 B; compact import rejects a removed branch-target identity |
| `vTaskSwitchContext` | 332 | 332 | 833 | O3 entry is 37 B plus a 796 B `.part.0`; entry size alone is misleading |
| `prvSwitchThread` | 102 | 102 | 0 | standalone O3 symbol disappears through interprocedural optimization |

O3 has 333,043 allocated executable bytes versus 342,107 for O0, a 2.65% reduction. Its debug/relocation-bearing ELF file is larger, so file size is not used as live text. Firm-BOLT retains the original fixed layout and executable capacity but saves 1,683 live bytes across 612 installed bodies. In this historical O3-oracle artifact, the compact importer retained `xTaskIncrementTick` and `vTaskSwitchContext` because FOP removed original branch-target instructions. The later exact-edge/origin importer closes that integration gap for these bodies and installs the 896-byte and 324-byte FOP forms. Its fixed-work candidate remained neutral against stock BOLT at target CPU 0.9997 [0.9874, 1.0086]. Branch provenance is therefore no longer the blocker for these exact functions, and O3 itself is unresolved against O0.

The result therefore does not trigger a new raw optimizer: Firm-BOLT already meets the predeclared O3 parity requirement. The next measurement needs an authentic input/device phase or another compute-limited region rather than more work on these already admitted scheduler bodies. Full hashes, commands, raw samples, disassemblies and mechanism counts are in the [SmartSpeaker O3 oracle evidence](../../build/preflight/lemix-smartspeaker-o3-oracle-2026-11-17/mechanism-report.json) and the later [contiguous-hot selection](../../build/preflight/lemix-smartspeaker-marker-profile-2026-11-17/firm-bolt-contiguous-hot-selection.json).


Hardware counters were unavailable because the installed `perf` did not support the running 6.17 kernel. We therefore make no cache, iTLB, or cycle claim.

### 3.4 Emitted growth is retention and layout, not merely a larger hot body

| Arm | ELF bytes | Executable-section bytes | Retained original text | Replacement hot+cold text | Exec-segment bytes outside executable sections |
| --- | ---: | ---: | ---: | ---: | ---: |
| R | 1,829,560 | 342,107 | 0 | 0 | 6 |
| L0 | 4,727,696 | 692,874 | 340,195 | 350,767 | 2,204,315 |
| L1 | 6,483,376 | 697,038 | 340,195 | 354,931 | 3,950,207 |
| F0 | 4,725,392 | 690,566 | 340,195 | 348,459 | 2,204,319 |
| F1 | 6,483,376 | 694,670 | 340,195 | 352,563 | 3,952,575 |
| FH | 6,483,376 | 696,974 | 340,195 | 354,867 | 3,950,271 |
| FRS | 6,483,392 | 692,494 | 340,195 | 350,387 | 3,954,751 |

The key mechanism is visible in the section inventory. Rewritten binaries retain 340,195 bytes in executable `.bolt.org.text` and add replacement `.text`/`.text.cold`. With layout enabled, 2 MiB alignment and load-segment spacing dominate whole-file and mapped-executable growth. Ordinary FOP itself reduces replacement executable sections by 2,368 bytes relative to L1; without layout it reduces them by 2,308 bytes. File size is unchanged across L1/F1/FH because section/segment alignment masks those local savings.

This refutes the simplistic explanation that FOP grew the hot code. It also exposes a firmware design gap: a fixed-flash optimizer needs in-place or compact emission, reference/alternate-entry proof before dropping old bodies, branch/literal repair accounting, and an explicit final image budget. Host ELF growth is not MCU flash occupancy, and section labels are not dynamic hotness; no cache penalty is inferred from these sizes.

The historical screening report's 600,250-byte reference and 709,714-byte BOLT “text” figures came from the aggregate `size` definition. The table above instead totals allocated executable sections (342,107 bytes for R and 696,222 bytes for L1) and separately inventories mapped segment overhead. These denominators are not interchangeable, so this report does not compare their percentages.

## 4. STOKE findings

### 4.1 Candidate reconciliation and the recovered extraction funnel

The recovered populations are 1,330 defined text symbols, 1,337 BOLT functions, 1,332 stock extractor attempts, 9 emitted bodies, and 1,318 unprofiled `StokeInfo` rows. These counts differ because they are distinct representations; the rerun joins by address/range/hash rather than assuming names or counts correspond one-to-one.

Among 194 profiled rows, the frozen screen contains 23 structural candidates (11.86%). They cover 416/9,158 static instructions (4.54%) and 685,955/4,855,284 profile score (14.13%). The old report's 5.47% score for one function is not treated as an Amdahl fraction. Likewise, the corrected fraction of 125 layouts among 1,337 functions is **9.35%**, not 8.96%; historical raw output remains unchanged.

| Funnel stage | Result | Mechanism |
| --- | ---: | --- |
| Frozen structural cohort | 23/23 | preselected before rerun outcomes |
| Stock extraction within cohort | 1/23 | 22 bodies blocked at entry representation |
| Full-body canonical attempt | 1/23 | confirmed the entry instruction remained the blocker |
| Fixed-prefix canonical extraction | 23/23 | preserve four-byte `endbr64`; search fall-through body |
| State construction accepted | 10/23 | supported bounded state/effect model |
| State construction unresolved references | 11/23 | requires semantic linking, not byte deletion |
| State construction memory failure | 2/23 | two retained `Could not resize memory` failures |

The earlier interpretation of `f3 0f 1e fa` as padding was wrong. Exact decoding identifies `endbr64` in 22/23 candidates, and `readelf` reports IBT and SHSTK properties. The pinned STOKE assembler cannot re-encode that instruction. Treating it as a protected fixed entry prefix recovers searchability without deleting a control-flow constraint. We did not verify runtime CET enablement: the [Linux shadow-stack documentation](https://docs.kernel.org/arch/x86/shstk.html) distinguishes ELF capability notes from loader/runtime enablement and exposes actual shadow-stack status through `/proc/$PID/status`. This is a representative firmware lesson: vector entries, mode switches, landing pads, literal pools, and alignment bytes require typed preservation, not blanket NOP stripping.

The next bottleneck is now explicit. Eleven bodies have RIP-relative or external control/data references whose semantics cannot be recovered by rebasing text alone; two state-generation runs fail while resizing memory. These are adapter/state issues, not search failures and not counterexamples.

### 4.2 Accessor legality under named contracts

| Candidate | Native ordinary case | Native stack/object alias | STOKE declared contract | Fault/event contract |
| --- | --- | --- | --- | --- |
| BOLT FOP (stores retained) | same return | same memory and return | hold-out and bounded: equivalent | same fault address; host PC offset differs |
| Three-instruction short form | same return | **different memory and return** | hold-out and bounded: counterexample | not qualified |
| FRS store-removal form | same return | **different memory and return** | hold-out and bounded: counterexample | not qualified |

The short form's non-discovery is therefore not evidence of weak search. It is illegal under the experiment's admitted alias/memory-output contract. If a future firmware ABI proves that the input can never alias private stack, that proof defines a narrower experiment; it may not be assumed from a normal testcase. STOKE's current checks also do not express FastDyn's future original-guest fault attribution and ordered event-prefix obligations. That is a qualification boundary, not a blanket claim that all STOKE verification is inadequate.

### 4.3 Controlled conditioning and end-to-end installation

The timed search executed 70,000,000 proposals across 32 runs, with 3333.3 seconds of summed subprocess elapsed. S1 produced 20 no-candidate runs and four duplicate successful `_fini` controls (one per seed). The `_fini` target after the protected entry prefix was `sub $8,%rsp; add $8,%rsp; ret`; the zero initializer supplied `ret`, so these runs required zero proposals. The candidate passed bounded verification, changed nine original file offsets, installed successfully, and completed 500 paced markers. Because it was supplied at initialization and `_fini` is cold, this is only an integration acceptance control—not a search discovery, hot-path optimization, or speedup.

For the accessor, unchanged S1 began at cost 4,922 and retained best-correct cost 2,005 across all four 2.5-million-proposal runs. The earlier 2,534-versus-2,005 observation is explained by STOKE's initialization: current cost belongs to the configured zero/padded current program, while `best_correct` is initialized from the target. The richer states/contract in this rerun also change the starting cost, so costs across the two experiments are not directly comparable.

S2 began from checked FOP seeds for the accessor and `vListInitialiseItem`. In all eight runs, search returned a lower testcase-objective cost (accessor 1,005; list 1,003), but all eight failed the separate bounded verifier. The measured claim is therefore limited: normalization improved testcase-objective outcomes; improved verified search effectiveness remains unestablished. Three failures have concrete verifier counterexamples and five are unknown without a retained counterexample. There were no verifier timeouts or tool failures in this set.

| Case | Seed | Target hash / cost | Normalized-seed hash / start cost | Returned-candidate hash / testcase cost | Verifier result |
| --- | ---: | --- | --- | --- | --- |
| `prvGetThreadFromTask/1` | 101 | `c64e7a4671a0` / 2005 | `83373aa451b5` / 2004 | `0d096ebe5934` / 1005 | counterexample |
| `prvGetThreadFromTask/1` | 202 | `c64e7a4671a0` / 2005 | `83373aa451b5` / 2004 | `2ca9b9212619` / 1005 | unknown_no_counterexample |
| `prvGetThreadFromTask/1` | 303 | `c64e7a4671a0` / 2005 | `83373aa451b5` / 2004 | `1399d8a5953d` / 1005 | unknown_no_counterexample |
| `prvGetThreadFromTask/1` | 404 | `c64e7a4671a0` / 2005 | `83373aa451b5` / 2004 | `babd1a7f1bc2` / 1005 | unknown_no_counterexample |
| `vListInitialiseItem` | 101 | `dc39736fc6ee` / 2002 | `a76f6c869c29` / 2002 | `71da76f8dee6` / 1003 | unknown_no_counterexample |
| `vListInitialiseItem` | 202 | `dc39736fc6ee` / 2002 | `a76f6c869c29` / 2002 | `1845a24cbb8f` / 1003 | unknown_no_counterexample |
| `vListInitialiseItem` | 303 | `dc39736fc6ee` / 2002 | `a76f6c869c29` / 2002 | `498a1ac93e36` / 1003 | counterexample |
| `vListInitialiseItem` | 404 | `dc39736fc6ee` / 2002 | `a76f6c869c29` / 2002 | `bbe45c748bc3` / 1003 | counterexample |

Full candidates, verifier output, and counterexample states are retained in [s2-verification-classification.json](../../build/preflight/preflight-sdk-v1-2026-11-16/s2-verification-classification.json) and [counterexamples/stoke-s2-rejections.json](../../build/preflight/lemix-o0-rerun-2026-11-16/counterexamples/stoke-s2-rejections.json). A search-level correctness bit is not a completed proof.

The deterministic FOP versions of both functions passed the declared checks, installed through STOKE's replacement tool, and ran the paced fixture. This shows that export/check/emission/installation can work independently of stochastic discovery.

## 5. Answers to the eight required questions

1. **Did FOP run previously?** No. The recovered commands omitted `frame-opt`, whose pinned default is `none`. Enabling ordinary FOP optimized 1,609 loads and deleted 253 without removing stores; hot-only mode captured almost all profiled dynamic load opportunity with 39 transformations.
2. **What changed?** In the representative accessor, two reloads became register uses and one redundant reload disappeared, reducing 29 to 24 bytes while retaining both stores. Across the emitted image FOP reduced replacement executable bytes by 2,368 with layout and 2,308 without it. The much larger ELF growth comes from retaining original text plus replacement regions and alignment, not FOP.
3. **Does matched-work CPU cost improve?** Yes at exact-function scope: 20.0% lower CPU cost per accessor call. No useful whole-application gain was resolved in the paced run: R/F1=0.9949 (0.9568–1.0166). Capacity throughput remains unestablished.
4. **How much coverage was recovered?** Structural-cohort extraction rose from 1/23 to 23/23. State construction reached 10/23; 11 references and two memory-state failures are now the dominant integration blockers. Search was deliberately limited to six accepted cases.
5. **Is the short accessor legal?** It matches ordinary cases but fails both the concrete stack/object alias probe and STOKE checks with stack/heap outputs. It is not legal under the declared contract. FastDyn fault/event preservation was not checked and remains a phase-two obligation.
6. **Does normalization help search?** It lowers the testcase objective in 8/8 S2 runs, but this does not establish improved verified search effectiveness: three candidates have concrete verifier counterexamples, five are unknown without a counterexample, and none verifies or installs. The unrelated cold `_fini` control was supplied by initialization with zero proposals and only demonstrates integration.
7. **What is observed versus unresolved?** The classification appears in Section 6. We do not convert unsupported extraction, state failures, timeouts, or unmeasured hardware counters into semantic or performance claims.
8. **What should be reused?** Reuse BOLT-style pass scheduling, decoding/CFG/profile machinery, ordinary FOP dataflow, and compact deterministic rules; reuse STOKE testcase/checking/search/replacement for bounded supported x86 regions. Build an independent firmware acceptance layer for exact identities, typed references/effects, state construction, in-place/size-bounded emission, fault/MMIO observations, and completed-work cost. Do not build a new stochastic engine from this evidence.

## 6. Claim audit

| Status | Finding |
| --- | --- |
| Observed | Ordinary BOLT FOP transforms O0 stack reloads and materially accelerates the exact accessor body. |
| Observed | The paced application shows no resolved 5% CPU-per-marker gain; capacity is not measured. |
| Observed | The BOLT-first wrapper compactly installs 855 exact BOLT bodies, saving 2368 live bytes without growing the reference ELF. |
| Observed | Of those bodies, 473 preserve the explicit-memory sequence verbatim and 382 are justified by 1219 exported FOP store-to-load dependencies. |
| Observed | The direct-call extension repairs 159 exact original targets across 65 additional functions. |
| Observed | The internal-branch extension validates 3 identity-preserving local branches across 3 additional functions. |
| Observed | RIP-relative handling repairs 52 exact original data targets across 26 additional functions. |
| Observed | The branch-heavy composition admits 646 more functions after validating 4956 branch targets, 1770 calls and 1321 data references. |
| Observed | Typed absolute-symbol handling repairs 6 callback references across 2 functions. |
| Observed | The compact cohort's paired paced interval spans gains and regressions, so it is inconclusive rather than a speedup claim. |
| Observed | Canonical fixed-prefix export recovers 23/23 structural bodies; state construction then admits 10. |
| Observed | The short/FRS accessor violates the admitted alias and memory-output contract. |
| Observed | S2 improves testcase-objective outcomes; verified search effectiveness remains unestablished at 0/8. |
| Observed | A zero-proposal, cold `_fini` initialization control verifies, emits, installs, and executes. |
| Source-confirmed | Pinned FOP defaults to `none`, qualifies its implemented treatment for x86, and runs after the named pre-FOP dynostats boundary. |
| Source-confirmed | `StokeInfo` is a screening/effect exporter, not a search/import/installation pipeline. |
| Refuted | “The frame optimizer ran and found no O0 opportunity.” It was disabled. |
| Refuted | “The 22 extraction failures were harmless padding.” They contain protected `endbr64` entries; runtime CET enforcement was not checked. |
| Refuted | “The three-instruction accessor is an obvious legal target.” It has a concrete alias counterexample. |
| Refuted | “BOLT text growth proves a larger hot footprint.” Retained old text and segment alignment explain most mapped/file growth; dynamic hot bytes were not measured. |
| Unresolved | Whole-application compute-limited capacity, hardware cache effects, realistic device inputs, original Cortex-M acceleration, and vulnerability preservation. |
| Deferred | FastDyn patch bindings, patch trigger semantics, and original-guest fault/event attribution. |

## 7. Recorded custom-pass baseline

Before the BOLT-first pivot, the rerun findings were carried into a bounded custom prototype. Its results remain useful acceptance fixtures and are preserved rather than rewritten. `PreflightPass SDK v1` exposes immutable `FirmwareUnitView` inputs and proposal-only `RewriteBuilder` edits; passes are registered explicitly with target/features, required and invalidated analyses, allowed edits, growth/region/work budgets, and stable skip reasons. Eligibility is expressed in machine structure and features, never in a required source optimization level. The pipeline records the input-contract, empty-patch-contract, state-set, proposal, and output hashes at each gate. This code now supplies contracts, fixtures and validation services around BOLT; its two pattern-specific transformations are not the production optimizer.

The identity round trip preserved one protected `endbr64` entry, one mutable RIP-relative reference, and six potentially faulting origins for `xTaskGetTickCount`; its serialized contract hash round-tripped exactly. Runtime CET enforcement remains `unknown`, rather than being inferred from ELF properties. An external pass-writer example is explicitly registered in `examples/preflight/external_identity_pass.py`; its ordinary NOP case is accepted and its protected-entry case is rejected.

### 7.1 Typed reference and state backlog

The 11 formerly unresolved cases were classified by referenced object rather than by encoded displacement:

| Reference class | Cases | Prototype disposition |
| --- | ---: | --- |
| Direct mutable scalar globals | 5 | `xTaskGetTickCount` linked and admitted with an 8-byte observable `xTickCount` object; four linked but not yet admitted |
| Pointer roots requiring transitive objects | 3 | unsupported; pointee ranges, permissions, and aliases remain undeclared |
| Static-storage address materialization | 2 | unsupported; escape and output binding are not qualified |
| Writable table without immutability proof | 1 | unsupported; `.data` is not frozen as a constant |

Thus the immediate linking result is 1 supported, 4 resolved but conservatively not admitted, and 6 explicitly unsupported/unchanged. Every reference records target identity, addend, encoding/range rule, permissions, mutability, binding, and layout invalidations. The two legacy `Could not resize memory` cases now have explicit bounded list-object/stack states with requested address, size, permission, operation, and outcome. The selected historical StateGen retained only its generic error—not the failing randomized state/address—so the prototype does not invent those missing historical values. Evidence is in [reference-class-audit.json](../../build/preflight/preflight-sdk-v1-2026-11-16/reference-class-audit.json) and [state-construction-audit.json](../../build/preflight/preflight-sdk-v1-2026-11-16/state-construction-audit.json).

### 7.2 Checked FOP reuse and compact emission

Four frozen pass outcomes exercise the acceptance boundary:

| Fixture | Result | Mechanism |
| --- | --- | --- |
| `prvGetThreadFromTask` ordinary FOP | accepted | 29-byte qualified range → 24 live bytes + 5 NOP padding; stores and fault origins retained |
| `vListInitialiseItem` ordinary FOP | accepted | 23-byte qualified body → 22 live bytes + 1 NOP padding; protected prefix remains outside the edit |
| Three-instruction short accessor | counterexample | observable stack-memory summary changed under the admitted alias contract |
| Experimental store-removal accessor | counterexample | same concrete contract mismatch; no bytes published |

Both positive outputs were installed by atomic in-place replacement with zero retained original bytes in the edited range, zero repair bytes, unchanged whole-image length, and exact origin maps. The emitted accessor body hash is `296180d44a6905c1e17389011dc76830c9523b2b60ff8f5b244578227b98e778` and exactly matches the BOLT FOP body used for the 30-block fixed-work result. The reference and installed candidate each produced 100 paced markers in the one-second installation smoke. This confirms installation, not application capacity: the only quantitative execution benefit remains the exact-body 20.0% CPU-cost reduction, and FastDyn benefit is unestablished.

The negative proposals stop at the model gate with `observable_effect_summary_changed`; neither `SHOULD-NOT-EXIST` output is published. The fault fixture accepts despite raw host offsets 12 versus 11 because it compares exact original identity, fault kind/address/width, recovery state, and ordered event prefix. Its result is `accepted: all declared guest observations agree`. Complete stage and size evidence is in [stage-ledger.jsonl](../../build/preflight/preflight-sdk-v1-2026-11-16/stage-ledger.jsonl), [pass-outcomes.json](../../build/preflight/preflight-sdk-v1-2026-11-16/pass-outcomes.json), and [fault-identity-comparison.json](../../build/preflight/preflight-sdk-v1-2026-11-16/fault-identity-comparison.json).

### 7.3 Firmware boundary

The original identity boundary remains recorded for `RTOSDemo.axf` hash `53ca19e3ae4927d4a81d95420c57567484ca2a3375f0757ac0fd8ec7db4f0a04`, but the implementation is no longer identity-only. BOLT itself still rejects ARM32/Thumb, so the firmware layer now contains a deliberately bounded fallback that adapts ordinary FOP's established spill/reload forwarding rule to 16-bit Thumb instructions. This is neither a BOLT Thumb backend nor a new dataflow algorithm. It admits only an explicitly private, nonescaped stack slot with no observer boundary; non-adjacent cases additionally require exact no-clobber/no-overwrite evidence for every intervening instruction. Contracts that admit an invalid/device-mapped stack, asynchronous observation, unknown effects or alternate entries retain the original bytes.

On the real Cortex-M3 `RTOSDemo.axf`, the checked candidate changed only `ldr r1, [sp]` to flag-preserving `mov r1, r0` in `vListInitialiseItem`, retained the store, changed exactly two ELF bytes, added zero bytes, preserved the vector and every other ELF byte, and produced zero mismatches over 1024 bounded architectural states. Reference and candidate both booted under FastDyn and reached the function entry in every run. Because that function executes only during initialization, the 30-block timing is inconclusive: reference/candidate CPU ratio 0.9965, 95% paired bootstrap interval [0.9904, 1.0094]. This is a real-firmware execution/installation result, not a speedup claim.

A generated fixed-work Cortex-M3 fixture executes the exact same O0 body 25,000,000 times per sample, removing exactly that many guest stack loads. Its median CPU ratio is 1.0109 with 95% interval [0.9990, 1.0140]; guest-timed ratio is 1.0106 [0.9967, 1.0160]. The central estimate is positive, but both intervals include 1.0, so this calibrated rerun is inconclusive and does not establish FastDyn or real-firmware acceleration. The earlier five-million-call run is retained separately as raw evidence rather than substituted into this claim.

On the real Cortex-M0+ ESCape32 O0 image, the same rule changed `serialresp` from `str r0, [r7,#4]; ldr r3, [r7,#4]` to `str r0, [r7,#4]; mov r3,r0`. It verified the `r7` frame-base prologue, changed exactly two file bytes with zero growth, and atomically published candidate `4b942fd9688b3595a6e1537fff1a44094be699de37ab309fa1a0e32d1a01af49`. Target execution remains unavailable for that board/device configuration, so this is structural acceptance only. The audit also corrects an earlier survey error: this artifact's vector-bearing executable range begins at `0x08001800`, not `0x08000000`, consistent with its retained 4 KiB boot plus 2 KiB configuration reservation.

### 7.4 Real-firmware hot-cohort result

A training-only exact-PC probe found 738 adjacent spill/reload pairs across the real M3 image. During its separately identified three-second run, 40 pairs executed 63,025 times. The dominant `xTaskIncrementTick` and `SysTick_Handler` pairs each executed 28,664 times. Profile membership only selected work; every installed site still passed the structural/effect/branch-target/emission gates.

The final measurement uses QEMU `icount` (`shift=5`, no sleep/alignment), a newly added no-I/O `benchmark_end_after` action, and exactly 50,000 completed `SysTick_Handler` returns per sample. Reference and candidate completed equal work in all 30 randomized blocks, and their compared 2 MiB RAM prefixes matched in every block. A non-`icount` calibration completed equal tick counts but produced a different 2 MiB RAM prefix in 1 block, demonstrating that externally paced event counts do not freeze the firmware schedule. The `icount` environment is therefore a deterministic measurement configuration, not a claim that it is identical to board-timed execution.

Three target policies were retained rather than reporting only the best-looking one. The initial same-register policy emitted `mov r0,r0`; its two-site CPU ratio was 0.9908 [0.9843, 0.9969], a resolved regression, so it was rejected. Specializing same-register forwarding to the architectural Thumb NOP improved the two-site median to 1.0047 [0.9967, 1.0074], but the interval includes neutrality and its lower bound misses the predeclared 1.005 useful-effect gate. Expanding the checked rule to all 40 measured-hot sites produced CPU ratio 0.9947 [0.9909, 1.0012]. Both artifacts are semantically admitted under the declared private-stack contract but rejected as `not_profitable`; deployment retains the reference.

For the inspected SysTick translation block, optimized TCG emitted 19 operations and 224 host bytes versus 24 operations and 320 bytes for the reference. Thus a smaller translated hot block still failed the full-harness profitability gate. The observed mechanism refutes “remove the most guest loads” as a sufficient target policy: emitted host-block size, runtime dispatch/cache interactions, fixed-work CPU cost, and schedule identity are separate obligations. Hardware performance counters were unavailable on this kernel, so the remaining cause of the full-harness result is unresolved rather than attributed to a cache effect.

The next extension keeps the same established forwarding rule but permits a bounded gap only when exact instruction identities prove that no intervening instruction clobbers the source/base register, overwrites the private slot, introduces an alternate entry, or crosses a control/observer boundary. The fresh training profile found 873 structurally qualified sites, 51 executed sites and 91,293 total hits. The added `xTaskIncrementTick` reload at `0x08000d8e` executed 28,432 times after an earlier same-slot reload and a retained global store. It is not treated as adjacent or pure: the intermediate stack read and global write remain in the effect/event sequence.

The three-site bounded candidate reduces the inspected hot tick block from 44 to 34 optimized TCG operations and from 624 to 448 generated host bytes. Nevertheless, its 30-block 100,000-tick CPU ratio is 0.9981 [0.9934, 1.0049], with zero RAM-prefix mismatches, so it is rejected as `not_profitable`.

A separately frozen five-arm ablation retained reference plus four policies in every randomized block. Adjacent tick-only was 1.0040 [1.0000, 1.0076]; bounded tick-pair was 1.0010 [0.9968, 1.0064]; handler-only was 1.0020 [0.9958, 1.0043]; and combined was 0.9976 [0.9945, 1.0051]. No arm's lower interval met the frozen 1.005 useful-effect threshold, all RAM prefixes matched, and deployment therefore retains the reference. This rules out simple policy mixing as the missing Cortex-M gain for this workload; a different optimization mechanism is required. Raw evidence is retained in the [bounded profile](../../build/preflight/thumb-cortex-m3-bounded-fop-profile-2026-11-16/report.json), [three-site result](../../build/preflight/thumb-cortex-m3-bounded-fop-2026-11-16/report.json), [TCG inventory](../../build/preflight/thumb-cortex-m3-bounded-fop-2026-11-16/tcg-diagnostic/report.json), and [policy ablation](../../build/preflight/thumb-cortex-m3-bounded-fop-ablation-2026-11-16/report.json).

### 7.5 Target-cost prefilter and fallthrough-branch control

The next bounded pass targeted a different O0 mechanism: a 16-bit unconditional Thumb branch whose resolved target is exactly its sequential successor. Replacing `b.n next` with one architectural NOP preserves the next PC, registers, flags, memory, instruction count and in-place size under a contract that does not expose branch events or instruction-cycle timing. The pass requires an exact resolved code edge and rejects unknown targets or observable branch/timing contracts.

Within the 26-function cohort selected before this experiment, the training probe found 242 structural sites; 133 executed 191,857 times. All 133 executed sites crossed the SDK, semantic, reference and compact-emission gates. The candidate changed 266 bytes with zero file growth. Across 30 randomized blocks of 100,000 completed SysTick returns, pinned to CPU 0, every compared 2 MiB RAM prefix matched. CPU ratio was 1.0024 [0.9917, 1.0081], and guest-region ratio was 1.0019 [0.9920, 1.0123]. The interval misses the predeclared 1.005 useful-effect gate, so the candidate is `not_profitable` and the reference remains selected.

For the inspected hot translation block, reference and candidate both emit 15 optimized TCG operations and 100 host bytes. The generated-host prefilter therefore returns `not_profitable:generated_host_cost_not_pareto_improved`. This identifies the mechanism: the source-level-looking branch cleanup did not reduce the inspected optimized TCG or generated host block. The new target-cost service now rejects zero/non-Pareto translated-code deltas before full timing. Passing remains only a prefilter: the stack-forwarding candidate reduced the inspected block by five TCG operations and 96 host bytes but still failed full-harness profitability. Generated-host cost and fixed-work measurement are therefore sequential, non-substitutable gates.

### 7.6 Accepted profile-weighted hot-exit layout

The next target mechanism used the same firmware SDK and reference fallback but changed block layout rather than deleting firmware effects. Training found 236 structurally qualified unused frame-pointer initializations, of which 18 executed. The dominant function, `prvCheckTasksWaitingTermination`, executed 3,448,738 times in three seconds. Its apparently dead `mov r7,sp` was deliberately retained because asynchronous SysTick entry can expose the transient register state through the exception frame.

The accepted `thumb-hot-exit-layout-v1` pass instead permutes the same 34 instruction identities inside the function's existing 92-byte range. It keeps the prologue, memory accesses, calls, flags, fault annotations and epilogue, repairs all internal code references, and makes the common zero-cleanup return the fall-through path. Both the original and candidate hot path contain 10 instruction identities, preserving the declared instruction-count timing model; the image grows by 0 bytes.

The profile-weighted translated path, rather than one entry block, explains the useful effect. The reference executes 3 translation blocks with 72 optimized TCG operations and 768 generated host bytes. The candidate executes 2 blocks with 61 operations and 696 bytes. Across 30 randomized paired executions of 100,000 completed SysTick returns, CPU ratio was 1.0552 [1.0392, 1.0609], guest-region ratio was 1.0500 [1.0459, 1.0826], and wall ratio was 1.0502 [1.0440, 1.1018]. All RAM-prefix comparisons matched and the CPU lower bound clears the frozen 1.005 gate, so this candidate is accepted. This is an original Cortex-M3 image running under FastDyn/QEMU, not a hardware-cycle, device-fidelity or unknown-vulnerability-preservation claim. Raw evidence and exact instruction origins are retained in the [hot-exit report](../../build/preflight/thumb-cortex-m3-hot-exit-layout-2026-11-17/report.md).


## 8. BOLT-first firmware wrapper

The production direction now uses BOLT as the optimizer rather than competing with it. A native `FastDynFirmwareReport : BinaryFunctionPass` runs immediately after BOLT's `FrameOptimizerPass` and `AllocCombinerPass`, before instruction lowering. The thin driver invokes the pinned trained F1 pipeline, enables BOLT Address Translation, consumes the native report, validates the emitted ELF and protected entries, records the earlier custom pass as a baseline, and then performs bounded compact adoption.

```text
Pinned BOLT profile/CFG/dataflow/layout/FOP
                    ↓
native post-optimization candidate report
                    ↓
firmware entry/reference/effect/placement gates
                    ↓
full BOLT candidate or selectively adopted compact image
```

The first end-to-end LEMIX result is `integration_accepted`. Real BOLT again optimized 1,609 redundant loads and deleted 253 loads representing 151,152 profile-weighted executions; it removed no stores. The native post-pass observed 1,395 function records, including 59 ignored records, and 1,125 functions whose estimated post-optimization size differs from their original size. Aggregate function bytes fell from 342,086 to 333,206 (2.60%); the post-optimization representation contains 85,525 non-pseudo instructions. The pre-pass origin tracker plus FOP annotation preservation maps 83,377 of them (97.49%) to exact original instruction addresses. The remaining 2,148 instructions are all BOLT-synthesized unconditional jumps: 2,140 have exact original source-to-target CFG-edge provenance, while eight lack that proof and remain ineligible. No instruction is assigned a nearest-address identity.

| Representative BOLT result | Original | Post-optimization | Result |
| --- | ---: | ---: | --- |
| `prvGetThreadFromTask` | 29 B | 24 B | 9 instructions; exact measured FOP body |
| `vListInitialiseItem` | 27 B | 25 B | 8 instructions |

All 22 `endbr64` entry constraints were found in the emitted binary and preserved. The reference and full BOLT candidate each completed 100 markers in the one-second paced installation smoke. With BAT enabled, the full BOLT ELF is 6,690,288 bytes with 694,670 executable-section bytes; this remains a host layout artifact, not acceptable fixed-flash occupancy by itself.

The wrapper also imported the **actual emitted BOLT bodies**, rather than regenerating them, for the two reference-complete fixtures. `prvGetThreadFromTask` uses 24 live BOLT bytes in its 29-byte original range; `vListInitialiseItem` uses 25 in 27 bytes and preserves its protected prefix. Every emitted instruction in both adopted bodies has an exact original-address mapping. The resulting ELF remains exactly 1,829,560 bytes—equal to the reference instead of the full BOLT candidate—and completed 100 paced markers. This is selective compact adoption, not full-program compact BOLT emission: reference completeness remains a gate before more functions can be adopted.

The same mechanism now selects a conservative cohort automatically: simple, single-entry, unsplit functions with complete origins, no calls/branches/RIP-relative operands, at most 1024 original bytes, a strictly smaller BOLT body, non-overlapping original ranges, and an unchanged protected prefix. It admitted 115 functions covering 8,445 original bytes and imported 7,836 live BOLT bytes, saving 609 live bytes while retaining the reference's 1,829,560-byte ELF size. The automatically compacted image completed 100 paced markers.

The native extension now exports the **pre-FOP instruction/effect inventory** and 1,609 exact load-to-store forwarding edges. The firmware wrapper qualifies 855 compactly installed bodies: 473 retain the exact ordered explicit-memory instruction sequence, while 382 use 1219 audited FOP edges from an original stack load to the exact earlier unchanged source store. No store or other non-forwarded memory instruction may change in this tier.

The next reference class is also implemented rather than discarded: 65 additional functions contain 159 direct x86 `E8 rel32` calls. Each call must retain its exact original instruction identity and order. The importer decodes the semantic target from the original call bytes, range-checks and rewrites the displacement for the original placement, and verifies the repaired target before publication. This adds 127 bytes of savings.

For local control flow, 3 more functions with 3 relative branches are installed. A branch is admitted only when its BOLT target remains inside the adopted body and the target instruction maps to the exact original target identity; moving the whole body then leaves the relative displacement invariant. Five otherwise compactable functions remain unchanged because their optimized branch target did not meet this strict identity rule. The admitted branch class contributes 16 more saved bytes.

RIP-relative data references are now handled without freezing their values. Using capstone-5.0.7, the wrapper decodes each original reference target, rewrites the candidate displacement for the original placement, range-checks it, and re-decodes the emitted address. The leaf-only arm adds 9 functions and 14 references; the combined call-plus-data arm adds 17 functions, 53 calls, and 38 data references. Mutable globals remain references to their exact original addresses. These two arms add 26 functions and 77 saved bytes.

Composing the same rules over branch-heavy bodies admits another 646 functions. This arm validates 4,956 exact local branch targets, repairs 1770 direct calls and 1321 RIP-relative data references, and contributes 1539 saved bytes. Conditional branches retain exact original instruction identities and targets. BOLT-created or removed unconditional jumps are instead proven by equality of the original and post-pass CFG edge sets, with exact source/target block identities; final offsets come from ELF decoding and target identities from BAT. The wrapper explicitly rejects 8 unproven provenance cases, 45 changed edge sets, and 14 changed targets. Across all admitted arms, the wrapper installs 855 functions and saves 2368 live bytes without ELF growth. This remains a bounded x86 reference contract, not a whole-firmware equivalence proof.

Raising the frozen region limit from 256 to 1024 bytes exposed a distinct reference class rather than an optimization failure. The first larger candidate crashed in `main_full`: BOLT had relocated six callback/function-pointer constants into its output address space, while the compact importer had repaired only calls and RIP-relative operands. Reproducible bisection isolated the function, and exact disassembly identified the immediates. The typed absolute-symbol adapter now resolves each symbol to its original identity, patches the final immediate at the original placement, and re-decodes it before installation. It repairs 6 references in 2 functions; `main_full` now completes the same paced work. The failed artifact and [crash bisection](../../build/preflight/bolt-firmware-wrapper-edge-provenance-1k-2026-11-16/crash-bisection.json) are retained rather than overwritten. One unsupported `MP3_GetBuffer_Info` RIP encoding is localized to that function, which retains its reference body.

In 30 randomized paired two-second blocks, its reference/candidate CPU-cost-per-marker ratio was 0.9973 (0.9810–1.0156). The interval spans both gains and regressions, so this paced application comparison is inconclusive and does not establish capacity or an application speedup. Raw block order, CPU/wall/work observations, and paired ratios are in [auto-compact-paired-samples.json](../../build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16/auto-compact-paired-samples.json).

The frozen profile marks 59 of the correctness-qualified functions as executed. A separate hot-only image publishes those bodies, saves 160 live bytes, remains reference-sized, and does not use profile membership as a correctness argument. A separate three-arm run compared reference, hot-only and all-qualified publication. HOT was 0.9930 (0.9788–1.0222); ALL was 1.0034 (0.9668–1.0318). Both are inconclusive.

The existing fixed-work FreeRTOS queue fixture completes two million messages with identical checksum. Stock BOLT-PGO has CPU ratio 0.9969 (0.9877–1.0135) and wall ratio 0.9936 (0.9833–1.0152); neither establishes a gain. Raw paired evidence is in [fixed-work-paired-results.json](../../build/preflight/lemix-queue/fixed-work-paired-results.json).

The same fixed-work fixture now also exercises the firmware wrapper. It installed 28 qualified BOLT bodies in place, repaired 39 direct calls and 26 RIP-relative references, validated 8 local branches, and saved 99 live bytes without changing the reference ELF size. Under the relocatable policy, the wrapper accepted and atomically published the complete BOLT candidate after fixed-work, protected-entry and address-map checks; it is byte-identical to the validated BOLT candidate (271 protected entries, status `accepted`). This policy retains BOLT's optimization rather than claiming a second speedup. The published BOLT+FOP artifact resolved a CPU improvement against the reference: 1.0081 (1.0002–1.0587); wall ratio was 1.0084 (0.9942–1.0703). The fixed-layout compact fallback produced CPU ratio 1.0126 (1.0000–1.0249) and wall ratio 1.0147 (0.9966–1.0313). Thus compact installation is currently an accepted but statistically unresolved pass result, not an improvement claim. The mechanism boundary is visible: the compact image retains selected BOLT instruction bodies but restores original placement, whereas the full candidate retains BOLT reordering and splitting. This run does not by itself isolate layout from FOP because the direct stock-BOLT/FOP contrast remains noisy. Evidence is in [wrapper-result.json](../../build/preflight/lemix-queue/bolt-firmware-wrapper/wrapper-result.json) and [fixed-work-paired-results.json](../../build/preflight/lemix-queue/bolt-firmware-wrapper/fixed-work-paired-results.json).

A frozen follow-up tested BOLT peepholes, conservative and aggressive register reassignment, and experimental frame-store removal behind the same wrapper. Peepholes removed zero branches; both register modes reported no changes, and all three compact images were byte-identical to ordinary FOP. Store removal deleted 24 stores in the full candidate, but the explicit-effect gate rejected 13 affected functions. It accepted 16 functions versus 29 for ordinary FOP. After exact in-place reference repair, none of the accepted experimental bodies differed from ordinary FOP, so per-function composition selected zero experimental bodies. In the new 30-block CPU-pinned timing run, reference/ordinary-FOP CPU was 0.9946 (0.9886–0.9987) and reference/store-removal was 0.9985 (0.9939–1.0035); neither establishes a native gain in this run. Store removal versus ordinary FOP was 1.0055 (0.9999–1.0105) in CPU cost. Deterministic Callgrind diagnostics nevertheless show a real mechanism: ordinary FOP removes 17,429,327 instruction references, or 1.025%, while the contract-safe store-removal image executes 603,684 more than ordinary FOP. The correct decision is to retain ordinary FOP and stop expanding these x86 policies for this workload. Full evidence is in the [policy-matrix report](../../build/preflight/lemix-queue/bolt-policy-matrix-2026-11-16/report.json).

A subsequent training/held-out deployment experiment removes that uncertainty without changing the optimization mechanism. A 30-block CPU-pinned training matrix froze full BOLT+ordinary FOP for relocatable images and the exact reference for fixed layout. On 30 newly randomized held-out blocks, the relocatable policy improved CPU cost over O0 by 1.0120 [1.0063, 1.0146] and was directly faster than frozen stock BOLT by 1.0087 [1.0072, 1.0162]. Every run completed two million messages with the same checksum. The fixed-layout policy retains the reference because compact FOP did not clear the 1.005 training lower-bound gate. Full evidence is in the [queue held-out policy report](../../build/preflight/lemix-queue/bolt-firmware-wrapper/queue-policy-held-out-report.json). The two layout decisions are atomically published through the hash-bound deployment schema; publication index SHA-256 is `4f2318abcbc14afa17eba00a05dba8574428b4e6bec1b1974b06b988043dbc74`. Its fixed-layout choice is also published as a standard `FirmwareCandidate v1` with identity virtual-address origins; candidate index SHA-256 is `7102a8d3a8c02b95554264681f7649499df34542e452c2445a7a281f2f97dc4b`. The selected relocated image is published as a second `FirmwareCandidate v1`; BAT provides instruction-level non-identity origins for 4,027 emitted instructions, retained executable sections contribute 5 identity ranges, and the entry point is covered. Relocatable candidate index SHA-256 is `0383b3328cb25c91a6d6f0e254ef1441736ca897babf7fafe9fb2ce30e193290`.

The final supported-host comparison rebuilds the same FreeRTOS POSIX queue application at O0 and O3, trains Firm-BOLT only on the O0 image, then freezes all artifacts before 30 randomized paired two-million-message runs. Every variant returned the same completed-message count and checksum. O3/Firm-BOLT CPU cost is 0.9768 [0.9723, 0.9805], so Firm-BOLT is 2.38% slower than O3 but clears the frozen 5% parity bound. O0/Firm-BOLT is 1.0121 [1.0077, 1.0187], while stock-BOLT/Firm-BOLT is 1.0273 [1.0252, 1.0329]. Firm-BOLT therefore beats the exact stock-BOLT baseline by 2.73% in this run while remaining within 2.38% of source O3. It optimizes the exact O0 binary; O3 is a performance reference, not a semantic oracle. This is a whole FreeRTOS x86-64 rehost result, not Cortex-M or FastDyn evidence. Full commands, hashes, raw samples and the frozen gate are in the [Firm-BOLT O0/O3 report](../../build/preflight/firm-bolt-freertos-o0-o3-2026-11-17/report.md).


The BOLT-first policy was also evaluated on the complete retained firmware-derived native corpus rather than only SmartSpeaker and the queue fixture. The rebuilt O0 executable contains three ESCape32 computations and five FreeRTOS list routines. BOLT FOP optimized 52 loads, deleted 1 load representing 930,124 training executions, and the wrapper accepted 5 compact bodies saving 31 live bytes.

The deployment policy was frozen on seed 11 before evaluating seed 13. For relocatable images it selects full BOLT+FOP only when the training lower confidence bound clears 1.005; otherwise it executes the exact reference. Fixed-layout images may select only the compact accepted image or the reference. The held-out results use 30 paired CPU-pinned blocks. `Reference/policy` measures improvement over O0; `stock/policy` measures the selected wrapper policy directly against the trained stock-BOLT artifact.

| Workload | Frozen relocatable choice | Reference/policy CPU | Stock BOLT/policy CPU |
| --- | --- | ---: | ---: |
| `combined_all` | `reference` | 1.0091 [0.9934, 1.0284] | 1.0941 [1.0600, 1.1104] |
| `crc8` | `reference` | 0.9976 [0.9864, 1.0051] | 0.9990 [0.9861, 1.0140] |
| `crc8dvbs2` | `reference` | 0.9906 [0.9746, 1.0121] | 1.4077 [1.3924, 1.4274] |
| `dshotcrc` | `full_fop` | 1.5533 [1.5319, 1.5693] | 1.2410 [1.2192, 1.2650] |
| `uxListRemove` | `full_fop` | 1.1037 [1.1004, 1.1153] | 1.0664 [1.0584, 1.0766] |
| `vListInitialise` | `full_fop` | 1.4307 [1.4241, 1.4375] | 1.0681 [1.0568, 1.0758] |
| `vListInitialiseItem` | `full_fop` | 1.5457 [1.5312, 1.5620] | 1.0305 [1.0197, 1.0423] |
| `vListInsert` | `full_fop` | 1.0930 [1.0837, 1.1092] | 1.0636 [1.0578, 1.0734] |
| `vListInsertEnd` | `full_fop` | 1.0585 [1.0495, 1.0819] | 1.0499 [1.0338, 1.0567] |

The relocatable policy generalized six selected gains and used exact-reference fallback on three workloads. It was directly faster than stock BOLT on 8/9 workloads, inconclusive on 1, and slower on 0. The fixed-layout policy generalized 3 gains and retained the reference for 6 cases. This establishes a supported-host corpus result: profitability selection around BOLT prevents the measured `crc8dvbs2` and combined-workload regressions while retaining BOLT's large wins.

The primary 30-block `crc8` comparison was inconclusive. A declared post-primary precision extension retained the frozen policy and unchanged artifacts and collected 100 new paired blocks. It resolved that case in favor of exact-reference fallback: stock BOLT used 1.0074 times its CPU cost (95% CI 1.0004--1.0116). Thus the policy is faster than stock BOLT on 9/9 corpus workloads when this precision extension is included; the original primary result remains recorded as 8 faster and one inconclusive.

The decision is deployable rather than a benchmark-only label. The atomic publisher emitted 18 hash-bound decisions (nine workloads in relocatable and fixed-layout modes). Each selected executable embeds the frozen selection and held-out evidence, and the loader revalidates their hashes, workload choice and output-acceptance status. The publication index SHA-256 is `7241b19237dec6c0a8898850e63d48c7978ee67a5cfa7d062936bd06bcf7e4ae`. All 9 fixed-layout decisions are additionally published as consumer-loadable `FirmwareCandidate v1` directories with identity virtual-address origin ranges; candidate index SHA-256 is `cd5f6c4231ca59492dd0f2fe165ea9029a2613fcc017ef98b05be407a7a23c47`. All 9 relocatable decisions are also `FirmwareCandidate v1` artifacts: 6 selected BOLT images use instruction-level BAT origins and the remaining exact-reference fallbacks retain identity origins. The selected BOLT artifacts each map 534 emitted instructions and retain 4 identity executable ranges; every entry point is covered. Relocatable candidate index SHA-256 is `2fa303c9449b9d1ec0ae0e5cdda8319deac45398fbf8ce41210e7ff77380f886`.

Emission accounting remains policy-specific. The reference and compact fixed-layout images are both 26,960 bytes; the compact image saves 31 live code bytes inside existing function capacities. The complete relocatable BOLT container is 6,300,848 bytes because it retains original code and adds a distant host-ELF text segment. That container size is not a Cortex-M flash-occupancy result and is not used for the fixed-layout policy. These results do not establish original-MCU or FastDyn speedup. Full evidence is in the [held-out corpus policy report](../../build/preflight/recovered-firmware-corpus-2026-11-16/bolt-firmware-wrapper/held-out-policy-report.json), and the complete workflow is reproduced by `scripts/preflight/run-recovered-corpus-wrapper.sh`.


The earlier custom transformation results remain unchanged in [custom-pass-baseline.json](../../build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16/custom-pass-baseline.json). They are now explicitly classified as a bounded validation/emission baseline, not the production optimizer. Native and wrapper evidence is retained in [wrapper-result.json](../../build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16/wrapper-result.json), [bolt-post-optimization-report.json](../../build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16/bolt-post-optimization-report.json), and the exact [BOLT command](../../build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16/command.json). The pre-BAT, instruction-only provenance, and failed pre-absolute-reference runs are preserved separately as historical evidence.

### 8.5 Native BOLT follow-up: opcode audit and copy cleanup

Width-complete stack-CMP forwarding raised FOP from 3,601 to 4,652 static reads but added only 20,055 weighted executions. The new available-but-unsupported audit then found no remaining opcode class above 21 dynamic reads, closing additional opcode plumbing as a useful SmartSpeaker direction.

A registered post-FOP copy pass deleted 22 adjacent redundant copies in ten observed blocks (122,405 exact block-weighted executions). Across the complete 800-block independent aggregate, Firm-BOLT is strictly faster at target CPU 1.0083 [1.0049, 1.0126] and wall 1.0013 [1.0004, 1.0019], with matching exact work and traces. Its target lower bound is nevertheless 0.0066 percentage point short of the frozen greater-than-0.5% useful-effect rule, so stock BOLT remains selected. A failed first precision attempt is retained separately: a missing driver observation triggered a legacy logger exception at sequence 462, and none of its samples enter the aggregate. The repaired harness adds append-only journaling and deterministic resume validation. Per-block equality tracking expands the pass to 391 deletions/144,395 weighted executions; its 30-block screen regressed at target CPU 0.9914 [0.9808, 1.0103]. A hot-only 29-site form was neutral at 1.0006 [0.9862, 1.0174]. The dominant extra identity is an original stack reload across a pointer store; after FOP it is a register copy, but end-to-end acceptance still inherits an unproved private-stack/no-alias obligation. Generalized equality now clears at every store, and the wrapper rejects FOP dependencies that cross an intervening store. The corrected candidate deletes 69 copies/122,846 weighted executions and passes installation, but its training screen is unresolved at target CPU 1.0041 [0.9875, 1.0147] and wall 0.9998 [0.9983, 1.0015]. Inline-all produced byte-identical executable sections. Four frozen alignment policies failed, with target medians from 0.9855 to 0.9993.

Exact block counts then selected two bounded follow-ups. A 32-bit self-move is deleted only after an immediately preceding whitelisted same-GPR 32-bit definition has already zeroed the upper half; 53 static sites cover only 13 weighted executions, and both its generalized and adjacent-base screens fail advancement. A higher-activity rule folds `mov src,tmp; mov (tmp),tmp` by replacing only the following load's exact base register. It accepts only a zero-displacement, unindexed, unsegmented `MOV64rm` that overwrites `tmp`, retains the load identity and memory/fault behavior, and records the removed copy. The 45 sites include six observed sites and 483,207 weighted executions, dominated by the 441,399-execution accessor. The wrapper admits the resulting 21-byte accessor only when both its exact byte string and native cleanup ledger agree; all six fixed-work installations match. The native screen still fails advancement at target CPU 1.0004 [0.9875, 1.0153], process CPU 1.0025 [0.9834, 1.0148], and wall 0.9998 [0.9971, 1.0033]. This is consistent with x86 register-move elimination and does not establish the separately measured TCG cost.

A final exact-rule coalescing `lea; shl; mov` into the destination removed 28 copies representing 562,102 weighted executions, 541,394 in the two scheduler functions. Its first wrapper run exposed a validator bug: a one-second paced observation differed by one marker while exact fixed-work behavior matched. Publication now gates on completed units, ticks, marker multiplicity, return status and ordered interrupt prefix; paced counts are diagnostic only. The unchanged candidate then screened at target CPU 0.9939 [0.9915, 1.0071] and wall 1.0002 [0.9997, 1.0003], so it is semantically accepted but not profitable. These are recorded mechanism and stop results, not general claims about copy propagation, alignment or inlining.


The final bounded rule combines a dead `MOV64rm` result with its following same-register `TEST64rr`, emitting `TEST64mi32 mem,-1`. It retains the original load/fault identity and one memory access of the same address, width and order, while reproducing the exact TEST flags. Under the default full-register-output contract, BOLT liveness admits 0 sites: deleting the loaded value is not legal merely because the following branch uses only flags. A separately labeled supported-host System V ABI arm enables `--assume-abi`; this qualifies 75 sites, 18 observed and 462,847 block-weighted executions. The wrapper recognizes only ledgered fusions, preserves the load identity, requires the original register test to disappear, and installs 6 affected bodies. All six fixed-work observations match.

The frozen 30-block screen initially had target CPU 1.0151 [1.0056, 1.0314] but did not clear its wall non-regression gate. One predeclared 100-block precision extension was then combined with the original blocks without retuning or early stopping. Across all 130 selection blocks, target CPU is 1.0111 [1.0027, 1.0167], process CPU is 1.0055 [0.9986, 1.0144], and wall is 1.0009 [0.9991, 1.0027]. The target lower bound remains below 1.005 and the wall lower bound below 1.0, so the frozen result is `training_rejected`; no held-out sample was consumed and stock BOLT remains selected. This distinguishes a real target-CPU signal from a deployable policy result and keeps the ABI assumption out of the firmware default. The aggregate and raw hashes are retained in [firm-bolt-memory-test-abi-selection-aggregate-130.json](../../build/preflight/lemix-smartspeaker-marker-profile-2026-11-17/firm-bolt-memory-test-abi-selection-aggregate-130.json).



### 8.6 ABI-qualified leaf-frame elimination and the layout-isolation result

The next Firm-BOLT rule removes an exact RBP call frame only under explicit `--assume-abi`, never under the default firmware contract. Its leaf form requires a single-entry, single-block function; no calls, branches, early returns, stack opcodes or RBP/RSP aliases in the retained body; an exact `push rbp; mov rsp,rbp; ...; pop rbp; ret` shape; and a protected `endbr64` prefix when present. It removes the matching CFI state changes together with the frame. This is a supported-host System V ABI experiment, not authorization to erase Cortex-M exception-visible stack/register effects.

The exact empty-frame arm qualified 7 observed functions and 222,270 block-weighted entries. Across its predeclared 130-block aggregate, target CPU was 1.0112 [1.0052, 1.0157] and wall was 1.0009 [0.9991, 1.0020]. The target gate cleared, but the wall non-regression gate did not. Generalizing only to stack-independent leaf bodies qualified 62 functions, 7 observed and 243,389 weighted; its frozen 100-block result was target CPU 1.0132 [1.0041, 1.0191] and wall 1.0007 [0.9998, 1.0023], again not deployable.

A second exact rule handles the two fixed-global-return functions whose post-FOP bodies contain a private, nonescaped `-8(%rbp)` return spill. It accepts only a RIP-relative global load into RAX, that exact spill, the already ledgered FOP-deleted reload, no pointer input or escape, and the retained return. It deliberately does not admit `prvGetThreadFromTask`: the accessor's caller-supplied pointer can alias its frame store, as the retained negative fixture demonstrates. The two accepted private-spill functions add 441,399 weighted entries. In the complete relocated Firm-BOLT image, target CPU was 1.0062 [1.0010, 1.0141] and wall 1.0015 [0.9999, 1.0023]. This was rejected, but changed function sizes also shifted unrelated BOLT output, so it did not isolate the local mechanism.

The address-stable overlay closes that confound. It starts from the exact stock-BOLT marker image, installs only 9 observed checked frame bodies (684,788 weighted entries), repairs 4 RIP-relative references to the same original objects, and fills the remaining symbol capacities with NOPs. It saves 61 live bytes, changes 121 file bytes, changes zero bytes outside the selected stock symbol ranges, preserves every stock address and file size, and records an original identity for every emitted live instruction. A separate 5,000-unit smoke completed exact work and the same 400-event ordered interrupt prefix. Before packaging, the native rule was tightened to reject personality/LSDA functions and to remove only an absent or exact four-operation canonical RBP unwind program. That stricter build qualifies the same 62 leaf and 2 private-spill functions and re-emits the byte-identical timed overlay (SHA-256 `37c6a9d31835bfae5b0b59ed1445390ddb0c07e83f137cc5639f77a2ba2dac0e`), so the timing evidence applies to the packaged rule without post-result retuning.

Across the frozen 100 randomized paired blocks, the exact stock-layout overlay reports target CPU 0.9983 [0.9910, 1.0082], process CPU 0.9984 [0.9893, 1.0066], and wall 0.9991 [0.9973, 1.0020]. Exact completed work and traces match; the candidate is rejected by the frozen training policy; stock BOLT remains selected. This comparison isolates the checked frame bodies from global layout motion. It remains a native supported-host cost result under the named ABI contract, not whole-application capacity, original-MCU performance or arbitrary firmware equivalence. Evidence is in the [frame-overlay selection](../../build/preflight/lemix-smartspeaker-stock-frame-overlay-2026-11-17/selection.json) and [training report](../../build/preflight/lemix-smartspeaker-stock-frame-overlay-2026-11-17/training-report.json).



## 9. Design implications for a firmware binary optimizer

The evidence supports an independent firmware acceptance layer around BOLT, not an independent raw optimizer. Firmware changes the acceptance boundary:

- **Representation is semantic.** Entry instructions, vectors, literals, branch targets, alternate entries, and code/data boundaries need typed identities. The `endbr64` recovery is a host analogue of this requirement.
- **Memory effects cannot be optimized from “unused” alone.** Stack/private memory, aliased objects, MMIO, DMA-visible memory, and faulting accesses require distinct summaries. The accessor counterexample is the minimal concrete case.
- **Emission is part of optimization.** Retained old bodies, alignment, veneers, and fixed ranges must be priced after repair. A profitable local rewrite can still be over budget.
- **Profiles prioritize; they do not authorize.** Hot-only FOP demonstrates excellent concentration of dynamic opportunity, while the 14.5% function coverage shows why cold/error paths remain part of the contract.
- **Search is downstream of integration.** Extraction and states, not the proposal engine, were the first bottlenecks. Once repaired, normalized search found testcase winners that proof rejected. Verification and installation must remain explicit gates.
- **The objective is completed work in the deployed execution environment.** Timer-supplied markers cap observed throughput. Firmware evaluation needs fixed-work or input-driven work oracles plus CPU/host work, image growth, and offline cost.
- **Guest simplification is not target profitability.** The real M3 hot cohort removes checked stack reloads and shrinks the inspected generated host block, yet both the two-site and all-hot policies fail the fixed-work CPU acceptance gate. The wrapper must retain measured reference fallback and cannot rank candidates by removed guest loads alone.
- **Generated-host inventory is an effective rejection prefilter, not an acceptance oracle.** Branch-to-next cleanup produced no optimized-TCG or host-byte delta and is now rejected before expensive timing; stack forwarding passed that prefilter but failed the full-harness gate.
- **Path-level translation shape can dominate local instruction count.** The accepted hot-exit pass preserves the same hot-path instruction identities and modeled guest instruction count, yet reduces the translated path from three blocks to two, from 72 to 61 optimized TCG operations, and from 768 to 696 generated host bytes. This mechanism clears the fixed-work profitability gate where local forwarding did not.

The optimizer is BOLT on both the supported host and the qualified Cortex-M
profiles; the custom SDK remains the firmware acceptance and compact-emission
layer. The pinned BOLT tree now contains a direct ARM/Thumb `MCPlusBuilder`,
relocation handler, symbolizer, AArch32 JITLink support and an audited native
pass schedule. The production-parity-v107 gate accepts all 28 independently
identified production/architectural profiles, processes all 127 functions in
the real M0+ ESCape32 image and 600 of 607 functions in the real M4 FreeRTOS
image; the seven retained M4 functions are ARM-state libgcc bodies that are not
valid Cortex-M Thumb code. It qualifies large interleaved Thumb-1 literal
pools, exact PC-relative references, local conditional-branch trampolines,
TBB/TBH plus in-function and relocation-complete detached ABS32 switch tables,
ordinary and conservative-tail Thumb-1 register-call promotion with a
separately admitted target literal read and exact direct/fallback origins,
exact immutable-vtable Thumb-1 method-load elimination with retained cold
fallback and original-identity fault attribution,
tail-duplication constant/copy propagation under an independent
register-definition-deletion contract,
linked read-only scalar-load simplification under an independent
read/fault/timing-elision contract with unresolved local pools retained,
vectors/secondary entries, M4 DSP/FPU decode,
ordinary FOP and exact origin mapping. Cortex-M23, M33, M55 and M85 add exact
Armv8-M Baseline/Mainline and Armv8.1-M CPU/ELF matching; TrustZone transition
instructions fence their complete functions, while MVE integer/floating-point
instructions are decoded and exactly re-emitted. PACBTI-M instructions likewise
fence their complete functions and are retained exactly. It validates seven M0+ ABS32 tables with
157 targets and eight M4 tables with 44 targets. No emitted instruction lacks
an original identity or a proven synthesized control edge.
The v76 extension also qualifies guarded movable/shared and terminal
non-fallthrough Thumb-1 literal pools. On the real M0+ image it selects 28
functions, attaches 30 pool owners, rebinds 461 PC-relative references and
represents 876 inline data bytes. Twenty-seven bodies are installed
byte-identically and all 2,078 final PC-relative references retain exact access
addresses; this is representation coverage, not a speedup result. Its
Cortex-M0 fixture installs 1/2 functions, binds one `ADR` and two loads from
different blocks to two guarded pools, and reconstructs eleven final mapping
symbols. The default contract preserves all three original access addresses,
so the installed executable bytes are identical. Both paths execute on
Cortex-M0 and reference/candidate write RAM words `(15, 15)`. One pool carries
a typed `R_ARM_ABS32` dependency from input `SHT_REL`; the other contains a raw
constant. Compact ARM metadata processing
recovers implicit addends, distinguishes executable-section data objects from
Thumb functions and preserves the required even flash-data address. A raw
`.rodata` pointer negative retains the exact input. An opt-in relaxed fixture
clones a value-only shared pool into eight explicitly typed capacity bytes;
shared ADR cloning remains rejected. A separate gate moves a
relocation-bearing pool from `0x08000018` to `0x08000014` and verifies that
the retained `R_ARM_ABS32` source offset follows the live bytes, its rebuilt
symbol index still names `external_object`, and the vector relocation still
names `Reset_Handler`. The no-report path produces the same metadata. A second
gate covers one-to-many growth: cloning a relocation-bearing value pool grows
`.rel.text` by one eight-byte record, binds the two live copies at
`0x08000030` and `0x0800003c` to `external_object`, and executes both paths on
Cortex-M0. Reference and candidate write `(0x12345678, 0x12345678)`. Explicit
global `--relocs`, literal islands without a proven non-fallthrough owner and
unmodeled relocation classes remain fail-closed.
The modeled set now includes `R_ARM_THM_JUMP6`: the accepted Cortex-M3 gate
retains the exact `CBNZ` relocation/target record and both reference and
candidate write `42`. Its wrapped REL addend and forward-only range are checked
independently; LLVM MC's unresolved-branch relaxation and pinned LLD's missing
relocation-52 support remain separately identified producer gaps.
The same gate now models `R_ARM_ABS8` and `R_ARM_ABS16` with their ABI-defined
sign-extended REL addends and signed-or-unsigned result ranges. Its first
fixture exposed a BOLT reader assertion on typed data relocations embedded in
executable flash. Firm-BOLT now routes exact range-covered data objects through
the data path and rejects unowned sites. Both records survive compact
publication, and reference/candidate consume them to write `42` on Cortex-M3.
The real M4 track now also admits one `LDR.W` terminal pool in
`vPortEnableVFP`, using the signed ±4095-byte Thumb-2 range and the same
exact-address contract. The pool remains byte-identical in the current
171-function M4 publication; the 678-byte delta comes from FOP and surviving
branch shortenings. The SVC-followed pool in
`prvPortStartFirstTask` remains unchanged because SVC may return; a negative
fixture requires an explicit non-return exception contract before treating
those following bytes as unreachable data.
The stage ledger now reports this as one qualified owner and one genuine
retention. It separately skips `__aeabi_memcpy`, whose conservative island
metadata has no data PC-relative reference, instead of counting that no-op as
a rejected pool transformation.
The Thumb-2 reference path also now admits `ADR.W` plus PC-relative `MOVW/MOVT`
relocations into BOLT's code-reference pipeline and validates their resolved
values after final in-place emission.
It also qualifies a compact Thumb-2 call veneer when the complete 10-byte
`MOVW r12; MOVT r12; BX r12` sequence fits the caller's typed range. The
synthetic absolute target is forced odd to preserve Thumb state, and `r12` is
used only where the call ABI permits the clobber. The positive fixture installs
one veneer with no new segment under an explicitly relaxed modeled-time
contract; the default instruction-count contract rejects that candidate and
retains the original. Thumb-1 now has a separately qualified literal-backed
absolute veneer in a caller-owned compact range; neither profile yet qualifies
placement without an explicit typed slot under arbitrary whole-image layouts.
The three emitted veneer instructions retain a separate synthesized-from
identity pointing to the exact original `BL`; their final output PCs are
linker-resolved independently. This avoids both nearest-address attribution and
the incorrect alternative of assigning three instructions one original output
address. The positive fixture reports three mapped veneer instructions, zero
unmapped instructions, and a null input-block address for the synthetic block.

Exact primary vector entries are now typed separately from ordinary functions.
The admitted handler subset is same-count ordinary FOP with stores, origins,
entry address and the exact exception-return instruction retained. A positive
fixture installs this form; a CFG-normalized negative is rejected with
`exception_contract_unsupported`. Secondary entries and all broader handler
rewrites remain unchanged. None of the six exact vector-entry functions in the
real M4 fixture exposed an FOP opportunity. The final linked ledger selects
171 bodies, including 127 with FOP and 50 with instruction shortening (overlap
is allowed); 127 functions change 678 bytes. Eight further attempted bodies
are retained as `missing_emitted_body`.

The v76 dataflow extension closes a separate Cortex-M representation gap in
ordinary FOP. The earlier backend modeled only word-sized stack spills; a
byte/halfword stack access through SP or R7 could therefore make frame
restoration fail, and directly replacing such a load with a move would be
semantically wrong because the load truncates and may sign-extend. Firm-BOLT
now models immediate byte and halfword stack accesses and emits a same-width,
flag-preserving `UXTB`, `SXTB`, `UXTH` or `SXTH` from the still-live stored
register. It retains the original store and rejects a narrow replacement when
the source register is not encodable. Stack tracking now interprets Thumb-1's
word-scaled SP adjustment and word/halfword memory immediates as architectural
byte offsets, while Thumb-2 wide immediates remain byte-valued. It also tracks
large-frame `ADD.W/SUB.W`; the executed M3 fixture uses a 1024-byte wide
frame. On the real M4 image these corrections change raw FOP opportunities
from 264 to 371, lower frame-restoration failures from 356 to 334, and publish
23 additional FOP functions relative to the original word-only gate. The first
narrow-width step contributed 50 opportunities; correcting scaled
word/halfword access offsets contributes a further 57. An executed M3 fixture
uses inputs with nonzero discarded high bits, checks all four unsigned and
signed results, and checks one word spill reached through two different SP
values; reference and candidate produce the same values.

That SP-rebase fixture also exposed a missing ARM `addToImm` hook when BOLT's
allocation combiner reached adjacent adjustments. The target hook now handles
narrow scaled and wide byte immediates, ADD/SUB sign changes and a 4095-byte
encoding ceiling. AllocCombiner checks failure before erasing an instruction.
The strict modeled-time policy skips this instruction-removing pass, while a
separate relaxed regression proves a legal merge and an overflow regression
proves both original instructions are retained.

The inherited v73 target-hook audit closes additional base-class failure paths that the
unprofiled corpus had not activated: AAPCS integer arguments, zeroing-EOR
liveness, conservative frame/platform-register scavenging, long direct branch
construction, `UDF`/`BKPT`, and narrow/wide shrink-wrap stack adjustments now
have ARM implementations and encoding-level unit tests. The production gate
remains unchanged at 171 selected M4 bodies and the same exact M3/M4 execution
results, so this is capability hardening rather than a new optimization gain.

The v76 gate includes a trained-layout path rather than relying on synthetic
reverse ordering. A frozen function-relative no-LBR profile assigns the taken
arm of a six-instruction Cortex-M3 worker 100 counts and its cold arm one.
BOLT's ordinary `ext-tsp` pass inverts `BEQ` to `BNE` and exchanges the two
return blocks, making the trained hot arm fall through. Compact publication
accepts the result without changing instruction count or range, records
`block_reordering` and exact origins, and changes three bytes. Together with
the three FOP bodies, the M3 gate now installs 4/13 functions and changes 23
bytes. Reference and candidate produce the same thirteen RAM words, including
the trained worker's result. A diamond-with-join precursor was rejected as
`instruction_count_changed` because that layout needed a compensating branch;
the profile is an optimization priority, not authority to weaken modeled-time
policy.

The target effect interface also now implements ARM exclusive-access
classification rather than inheriting base-class failure paths. It covers
`LDREX`/`STREX` word, byte, halfword and doubleword encodings, their
acquire/release variants, and `CLREX`. The real M4 report records 60 exclusive
loads, 60 exclusive stores and no explicit clears after optimization; every
reported instruction carries the corresponding effect bits. The same
reference/candidate 2 MiB RAM hashes and 10,000-work-unit completion gate pass.
This makes atomic boundaries visible to clients; it does not authorize
rewriting an exclusive sequence or prove interrupt/device equivalence.

The v80 gate adds BOLT's native inliner to the audited Cortex-M schedule, but
only behind an explicit `--firmbolt-allow-inlining` call/event contract. The
first executable test exposed two architecture assumptions in the generic
pass: it required a direct call to have exactly one prime operand, whereas
Thumb `BL` also carries predicate operands, and it treated `BX LR` as an
ordinary local branch rather than a return. The ARM adapter now recognizes the
semantic call target, excludes returns from local-target remapping, and
implements direct and register tail-call-to-call conversion. A denied arm
proves that requesting inlining without the firmware contract fails before
emission.

The accepted Cortex-M3 fixture inlines a two-instruction leaf into an
AAPCS-valid caller that saves and restores LR. The caller retains four guest
instructions, shrinks from 10 live bytes to 8, changes seven bytes in its
qualified range and adds no segment. The copied `MOVS` keeps the callee's exact
original identity (`0x08000024`) while its linker-resolved output address is
`0x0800001c`; it is explicitly typed as a cloned original rather than being
misinterpreted as a caller-relative offset. Reference and candidate both write
the word `43` under the Cortex-M3 FastDyn execution gate. This establishes
working native-pass integration, compact emission and attribution for the
named empty-patch/call-boundary contract. It is one semantic execution case,
not a performance result or permission to inline patch-, MMIO-, exception- or
observer-sensitive calls.

The v81 gate adds BOLT's native tail-duplication pass to the same audited
schedule. Firmware keeps it fail-closed by default because copying a block can
change an instruction identity's execution multiplicity. The accepted arm
requires `--firmbolt-allow-tail-duplication`, excludes vector entries,
address-stable regions, veneer slots and jump-table functions, and tags every
copied instruction as a clone of its exact original identity.

The executed Cortex-M3 fixture uses a frozen no-LBR profile to duplicate one
four-byte terminal tail onto a hot predecessor. BOLT removes the hot-path
branch and shortens the original wide branch; compact publication changes four
bytes inside the original 18-byte function, adds no segment and maps the two
clones from original addresses `0x0800002a` and `0x0800002c` to output
addresses `0x08000022` and `0x08000024`. Reference and candidate both write
RAM word `3`. This is native profile-driven pass integration and semantic
execution evidence for the named empty-patch/multiplicity contract, not a
timing result or permission to duplicate observer-, exception-, MMIO- or
patch-sensitive code.

The v106 gate separately enables BOLT's existing post-duplication
constant/copy propagation under
`--firmbolt-allow-tail-duplication-propagation`. The implementation adds the
previously missing ARM conditional-move and register-replacement hooks and
fixes the generic source discovery to ignore optional CPSR definitions and
predicate operands rather than assuming operand 1. It preserves flag and
predicate behavior and rejects unencodable immediates, incompatible register
classes, destination aliases and tied operands. The 22-byte M3 worker becomes
20 bytes: dead `r2` definition identity `0x08000028` is deleted, the hot clone
contains `add.w r0, r0, #5`, and clone identities `0x08000034` and
`0x08000038` remain exact. Reference and candidate execute hot and cold paths
to `(5, 10)`. A live-out `r1` copy is retained, and omitting the independent
effect contract rejects before emission. The one-shot runner timestamps are
diagnostics only; no speedup is inferred.

The v84 gate adds BOLT's target-generic loop-inversion pass under an explicit
`--firmbolt-allow-loop-inversion` Cortex-M contract. Firmware cannot treat the
ordinary layout transform as invisible: changing a top-tested loop into a
bottom-tested loop removes a per-iteration branch and changes modeled event
opportunities. The pass therefore excludes vector entries, address-stable
ranges, veneer slots and jump-table functions and remains a no-op without the
permission even when a profile is present.

The executed M3 fixture has a 16-byte four-block loop with a two-byte body and
a frozen 4:1 loop/exit profile. BOLT moves the body ahead of the test, reverses
the conditional branch and removes the unconditional back edge. Compact
publication emits 14 live bytes in the original range with no new segment.
Six instructions retain exact original identities at their new addresses;
the replacement entry branch is recorded as a synthesized control edge from
the original entry block, leaving zero unattributed instructions. Reference
and candidate both write RAM word `4`. The one-shot execution times in the
artifact are deliberately not interpreted: this is a semantic/integration
gate, not a repeated performance result or a proof for interrupt-, device-,
exception- or patch-observed loops.

The v89 gate then qualifies BOLT's generic double-jump peephole for an
explicit firmware branch-event contract. A Cortex-M request without
`--firmbolt-allow-control-flow-peepholes` fails before emission. Vector
entries, address-stable functions, veneer slots and jump-table functions are
excluded. Tail-call trap insertion and useless-conditional-branch removal
remain separate rejections: their post-return fault and removed-branch event
semantics have not been qualified. A direct useless-branch probe additionally
showed that BOLT merges the duplicate CFG edge during CFG construction before
the named peephole runs. The emitted deletion is therefore attributed to CFG
normalization; it is not counted as successful peephole integration.

In the executed M3 fixture the taken conditional edge originally reaches a
one-instruction branch trampoline. The peephole redirects that condition to
the trampoline's target. It changes one immediate byte, retains the full
14-byte in-place body and all seven exact original identities, and adds no
segment. The original trampoline remains available to any independently
admitted entry, while the selected path executes one fewer branch. Reference
and candidate both write RAM word `5`. The artifact's single execution is a
semantic gate rather than a performance estimate.

The v90 gate additionally qualifies native BOLT indirect-call promotion for
ordinary Thumb-2 register calls. A request without
`--firmbolt-allow-indirect-call-promotion`, without `--assume-abi`, or using
`--icp=all` rejects before emission. The qualified sequence materializes the
hot Thumb target in AAPCS call-scratch register `r12`/IP, compares it with the
original target register, executes a direct-call clone on a match and retains
the original `BLX` as the fallback. Method-load elimination, jump-table
targets and Thumb-1 calls remain outside this ordinary call contract.

The executed M3 fixture provides 40 symbol-bounded bytes, including a guarded
unreachable range that is separately proven non-entry before reuse. Compact
publication installs a 28-byte body. Its final origin ledger covers all ten
emitted instructions: three retained original identities, one direct-call
clone of the exact original indirect call, and six synthesized guard/merge
instructions attributed to that same identity. The fallback `BLX` is mapped
to its actual emitted PC rather than the old input PC; this duplicate-identity
case required replacing one-to-one input-address translation with the
linker-resolved final instruction cursor. Reference and candidate both write
RAM word `17`. This is an execution/integration gate, not a throughput claim;
the artifact contains only one diagnostic execution per variant.

The v99 gate closes the ordinary-call case in which the indirect target itself
is `r12`/IP. Reusing `r12` for the materialized hot address would destroy the
fallback target. Instead the ordinary-call form uses LR as the guard scratch:
both the promoted direct `BL` and retained fallback `BLX` overwrite LR by ISA
definition, and the caller already preserves LR when it needs the incoming
value. This requires no added stack memory effect. The emitted body again
occupies 28 of the 40 qualified bytes and its exact origin ledger contains
three retained instructions, the direct-call clone, and six synthesized
guard/merge instructions. Reference and candidate both write RAM word `17`.
The paired indirect-tail-call test is deliberately negative. A tail `BX r12`
must preserve incoming LR for its callee, so LR is not available as scratch;
Firm-BOLT rejects the promotion and retains the original body even when the
tail-call contract is enabled. These are mechanism and preservation results,
not repeated-work timing evidence. The executable acceptance sources are the
[ordinary-call fixture](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-indirect-call-promotion-r12.s)
and the paired
[tail-call rejection fixture](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-indirect-tail-call-promotion-r12-rejected.s).

The v101 gate qualifies one narrower part of BOLT's existing virtual-call
optimization instead of introducing another optimizer. ARM now recognizes
exact fixed-offset 32-bit Thumb `LDR` method fetches and feeds their vtable,
method-register and fetch identities to ordinary indirect-call promotion. It
may remove the hot-path method load only with the separate
`--firmbolt-allow-method-load-elimination` contract and only when the profiled
slot is completely covered by mapped, allocatable, non-writable image bytes.
The guard materializes the raw vtable data address, not a Thumb code pointer.
The cold fallback retains the exact original method load and indirect call, so
a different vtable still performs the read and dispatch.

The executed Cortex-M3 fixture uses one profiled immutable vtable and one
different immutable vtable. Compact publication changes 26 executable bytes
and installs 32 live bytes in the original 52-byte symbol range. Its final
ledger contains the two entry instructions, six synthesized guard/merge
instructions attributed to the original call, one direct-call clone, the
original cold method load and call, and the original return. The original
method-load identity occurs exactly once at emitted offset 24. Reference and
candidate both write `(17, 19)`, covering the promoted hot path and retained
cold fallback.

The same run then points a third object's vtable at an MPU-protected
`0x60000000` range. Both variants take the cold fallback and raise a precise
four-byte data-access violation. Their raw stacked PCs differ
(`0x080000ae` reference and `0x080000c2` candidate), as expected after compact
publication, but the final origin ledger maps both to original method-load
identity `0x080000ae`. The ordered result prefix is `(17, 19)` in both;
`CFSR=0x00000082`, `HFSR=0x40000000` and `MMFAR=0x60000000` also agree. This is
an architectural execution and fault-attribution gate, not a throughput
result or a claim about every device/observer schedule.

Two negative controls define the permission boundary. Omitting the read-
elision contract still allows ordinary ICP, but leaves the method load before
the guard and reports zero eliminated method loads. A profiled slot in writable
`.data` is rejected even when the option is present. Indexed, writeback,
negative-offset, conditional, narrow, SP/PC-based and otherwise unsupported
fetches also remain unchanged. The executable positive/fault and writable-negative sources are the
[method-load fixture](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-indirect-call-method-load-elimination.s)
and [mutable-slot rejection fixture](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-native-indirect-call-method-load-writable-rejected.s).

The v91 gate adds a separate indirect-tail-call contract instead of silently
generalizing the ordinary-call result. ARM now implements BOLT's conservative
epilogue-recognition hook: an indirect `BX` in a multi-block function is
classified as a tail call only when its terminal block has no successors,
contains a real POP/LDMIA stack restore, and does not save LR in that block.
Promotion additionally requires
`--firmbolt-allow-indirect-tail-call-promotion`; omitting it logs the skip and
retains the call. Tail calls without that epilogue evidence remain unchanged.

The M3 fixture retains `push {r4}` and `pop {r4}`, so their stack memory and
alias effects are not optimized away. It promotes the profiled `BX` into a
guarded direct tail branch plus the original fallback and proves a following
32-byte executable block unreachable before consuming its capacity. Compact
publication installs 24 bytes in the original 38-byte range. The final ledger
contains the two original epilogue instructions, four synthesized guard
instructions, a direct-tail-call clone of the exact original identity and the
original fallback. Reference and candidate both write RAM word `19`.

This case also exposed a late-layout accounting gap: MC relaxation widened the
planned two-byte direct branch to a four-byte `b.w` after optimization. v91
adds private final-instruction labels to the link-only address map and derives
Thumb widths from the linked bytes. The reported clone is therefore at output
offset 18 with size four, while the fallback is at offset 22 with size two;
neither is collapsed onto their shared input identity. This is preservation
and integration evidence, not a performance result.

The v92 gate adds BOLT's native jump-table hot-index promotion for recovered
Thumb-2 inline `TBB`/`TBH` tables. It is independently gated by
`--firmbolt-allow-jump-table-promotion`; the ordinary and tail-call permissions
do not authorize it. A candidate is admitted only when the table, index
register, targets and instruction identities are exact, the compared index is
encodable, and APSR flags are dead. The emitted guard branches directly to the
profiled case and retains the original table dispatch as the fallback. At the
v92 gate, ABS32 tables and Thumb-1 dispatches remain unchanged; v96 qualifies
the bounded ABS32 case below.

The executed M3 case promotes index zero in a three-entry `TBB`, preserves all
three destinations, shrinks the symbol-bounded body from 60 to 36 occupied
bytes and adds no segment. Its origin ledger records the four-byte compare and
two-byte conditional branch as synthesized from the exact original dispatch,
then maps the retained four-byte `TBB` and every case instruction exactly.
Reference and candidate both write RAM word `10`. A flags-live negative is
rejected because the inserted compare would alter state consumed by the case.

The first linked candidate also revealed that rebuilding ARM mapping symbols
from original table offsets is unsafe after insertion: stale `$d`/`$t` markers
made the guard and fallback appear to be data. v92 instead uses the unique
linked jump-table label for `$d` and the linked table end for `$t`. The final
table bytes `02 04 06` at `0x08000016` resolve exactly to the three emitted
case addresses. The artifact's one reference/candidate execution (92.040 and
79.201 microseconds) is a diagnostic only, not a performance estimate.

The v96 gate extends the same native BOLT hot-index mechanism to recovered
Thumb-2 ABS32 switches, but only when the literal pointer, complete table and
all case destinations belong to one independently relocatable function range.
Detached/shared tables remain address-stable and are rejected. The accepted M3
fixture promotes index zero, retains the original `mov pc, r2` fallback and
three absolute entries, and compacts its 80-byte symbol range to 52 occupied
bytes. It changes 27 executable bytes; the two synthesized guard instructions
carry the exact original dispatch identity.

This extension found a preservation bug that static table-target checking had
missed: the first candidate moved the pointer/table but left its `ldr.w`
targeting the old pointer PC, which had become a `bx lr`. The hot index passed,
but cold index one hard-faulted. The final implementation records the exact
table-pointer identity and rebinds only that PC-relative load as part of the
accepted ABS32 promotion transaction; pinned and externally observed literal
pools retain their prior behavior. The emitted load now reaches `0x08000054`,
whose word names the table at `0x08000058`; its entries resolve exactly to
`0x08000048`, `0x0800004c` and `0x08000050`. Reference and candidate execute
inputs `(0, 1, 2, 3)` and both write `(10, 20, 30, 0)`. These are functional
integration results; the single execution times are not a speedup estimate.

The post-v101 v102 increment closes only the independently provable detached
ABS32 case; it does not generalize detached tables from profile or address
proximity. Firm-BOLT records immutable input-relocation evidence separately
from the mutable relocation graph used for emission. A detached, allocatable,
read-only table is eligible only when the original ELF contains one unique
`R_ARM_ABS32` record for the literal pointer and for every table entry, each
record resolves to the exact decoded object, and the caller supplies
`--firmbolt-allow-detached-jump-table-update`. The function and table are then
published as one checked update. Missing permission, missing records,
ambiguous records, writable storage or an unresolved target leaves the input
unchanged.

The linker/install regression keeps the detached table at `0x0801004c`,
rewrites its three words to `0x08000020`, `0x08000024` and `0x08000028`, and
remaps the pointer-word relocation from input `0x08000028` to emitted
`0x0800002c`. The final literal word is `0x0801004c`; `.rel.text` names the
same `.rodata` object and all three `.rel.rodata` entries remain present. The
hot index receives the existing BOLT compare/branch promotion, while the
original `mov pc, r2` remains the cold fallback. The final image overwrites
one of two functions and adds no segment. A paired no-contract arm with the
same retained relocations and a no-relocation arm both overwrite zero
functions. These are exact relocation, layout and installation results.

The complete production runner adds an independently linked executable
variant as the twenty-third profile. Its `.rodata` table remains at
`0x08008000`; compact publication shrinks the worker from 68 to 40 live bytes
and moves the exact pointer-word relocation from `0x08000050` to
`0x08000054`. The final pointer still names `0x08008000`, all three table-entry
relocations remain at `0x08008000`, `0x08008004` and `0x08008008`, and their
emitted targets are exactly `0x08000048`, `0x0800004c` and `0x08000050`.
Reference and candidate execute inputs `(0, 1, 2, 3)` under FastDyn and both
write `(10, 20, 30, 0)`. The no-contract arm retains the exact original worker.
The single execution observations establish behavior, not a speedup.
The regression sources are the
[retained-relocation positive/no-contract fixture](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-abs32-jump-table-promotion-detached-relocs.s)
and the
[missing-relocation retention fixture](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb-abs32-jump-table-promotion-detached-rejected.s).
The 23rd executed profile uses the
[detached-table fixture](../../tests/preflight/fixtures/firm_bolt_cortex_m3_detached_abs32_jump_table_icp.s)
and its
[split code/read-only-data linker contract](../../tests/preflight/fixtures/firm_bolt_cortex_m3_detached_abs32_jump_table_icp.ld).

The v103 increment added ordinary profiled Thumb-1 register-call promotion as
the twenty-fourth production profile. This is not a Thumb-2 address sequence
retargeted to Armv6-M: Thumb-1 cannot materialize an arbitrary address in
`r12`. Firm-BOLT instead loads a typed `target+1` word from executable
read-only flash under the independent
`--firmbolt-allow-thumb1-icp-literal-load` contract. When no low argument
register is dead, it preserves one in LR, restores it with a flag-preserving
move before either call, and introduces no stack access. The mismatch path
retains the original `BLX`; both paths jump around the word-aligned data.

The linked fixture makes r0-r2 semantically live in both the profiled and cold
targets. Compact publication changes only `indirect_caller`, reduces occupied
bytes from 40 to 32, records nine synthesized promotion instructions and one
four-byte target word, and preserves the exact original call identity for the
direct clone and fallback. Reference and candidate execute both paths on the
Cortex-M0 model and write `(13, 35)`. With the ordinary call-event contract but
without the added-read contract, all 40 worker bytes remain unchanged. The raw
one-shot execution times are functional smoke diagnostics, not performance
samples.

The v104 increment adds conservative Thumb-1 indirect-tail-call promotion as
the twenty-fifth profile. A recognized terminal `BX` with callee-saved restore
may use the same typed target word, but it cannot borrow LR: incoming LR is the
tail target's return identity. The guard instead saves one borrowed low
argument register in AAPCS scratch `r12`, performs the load/compare, restores
the argument with a flag-preserving move, and takes either a direct branch or
the original `BX`. It adds no stack access. The fixture makes r0-r2 live and
executes profiled and cold targets; reference and candidate both write
`(13, 35)`. Compact publication shrinks `tail_dispatch` 38→24 bytes and records
six synthesized guard instructions plus a four-byte target word. Both terminal
branches retain the exact original tail-call identity. Without the added-read
contract all 38 bytes remain unchanged. This is behavioral integration
evidence, not timing evidence. At v104, Thumb-1 method-load elimination and
`r12`-target tail calls lacking a genuinely dead low guard register remained
fail-closed; v105 closes only the exact method form below.
The focused [BOLT regression](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb1-native-indirect-tail-call-promotion.s)
and [executed production fixture](../../tests/preflight/fixtures/firm_bolt_cortex_m0plus_icp_tail.s)
are retained with the raw v104 evidence.

The v105 increment adds exact immutable-vtable method-load elimination on
Thumb-1 while preserving the same independent contracts. It recognizes only a
32-bit fixed-offset fetch from a profiled slot fully covered by mapped,
allocatable, non-writable image bytes. Both the read-elision contract and the
Thumb-1 immutable-flash guard-read contract are required. The typed guard word
is the raw vtable data address rather than a Thumb target; the cold fallback
retains the original method load and `BLX`.

Compact publication installs 1/5 functions and shrinks `virtual_caller` from
52 to 34 live bytes. The final ledger contains eight synthesized promotion
instructions, four typed data bytes, one retained cold method load and exact
direct/fallback call origins. Reference and candidate produce the same hot/cold
prefix `(17, 19)`. A third vtable pointer at `0x3ffffffe` makes the four-byte
method load cross the admitted SRAM boundary. The raw fault PCs differ after
publication (`0x0800006c` versus `0x08000082`), but both map to original load
identity `0x0800006c`; address, width, ordered prefix and required Cortex-M0
state agree. The no-added-read control leaves all 52 bytes unchanged. Mutable,
unmapped, indexed, writeback, negative-offset, conditional, narrow and SP/PC
forms remain unsupported and unchanged. The accepted evidence is functional
and fault-attribution evidence, not a timing, whole-firmware-equivalence or
device-schedule claim. The retained sources are the
[focused BOLT regression](../../third_party/preflight/llvm-project/bolt/test/ARM/thumb1-native-indirect-call-method-load-elimination.s)
and the
[executed production fixture](../../tests/preflight/fixtures/firm_bolt_cortex_m0plus_icp_method_load.s).

Compact publication is now inside BOLT rather than a Python same-size rewrite.
BOLT can analyze every qualified function, but a body is installed only when a
native pass records a modification reason and the firmware publication policy
admits the complete final body. The v76 gate extends the correction for a
concrete gap first identified in v25: while FOP was the ledgered optimization,
BOLT CFG finalization
also removed explicit branches to the next block. That ordinary canonicalization
changed the modeled guest instruction schedule. The wrapper now retains
same-width moves after forwarding and preserves original explicit branches
under the strict default contract. Final-link publication failures are also
recorded before origin/symbol output, so retained bytes, ARM mapping symbols
and the stage ledger cannot describe a different rejected candidate.

On the M4 image, 171 linked bodies are selected and 127 actually change 678
executable bytes. The earlier `uxListRemove` 36-to-34 mismatch is repaired by
retaining its two explicit fall-through branches. Eight attempted functions
whose linker emitted no body remain unchanged with the explicit
`missing_emitted_body` status.
On M3, three attempted FOP functions are admitted and change 20 bytes; a
fourth profile-driven layout body changes three bytes. The new body set
executes unsigned/signed byte and halfword forwarding, scaled SP-rebase
forwarding and the trained layout before publication.
On M0+, one of two attempted branch-relaxation functions is admitted but its
linked executable bytes reproduce the input, so the delta is zero. These are
mechanism and publication results, not speedup claims.

A separate Cortex-M0 branch gate now proves installation rather than merely a
pass diagnostic. The audit found that local relaxation incorrectly sent
register-indirect `bx lr` through symbolic branch-range handling, marked the
function non-simple and caused the former fixture to publish zero bodies. The
corrected classifier skips non-symbolic transfers. With reverse block layout,
Firm-BOLT installs 1/3 bodies, changes 11 executable bytes and carries one
synthesized trampoline edge into the final origin map. Reference and candidate
execute both outcomes on the Cortex-M0 model and write identical `(1, 2)` RAM
results. This qualifies local Thumb-1 branch repair independently from the
absolute inter-function call veneer described next.

The Thumb-1 absolute-call gate installs one caller body and executes both the
reference and candidate on FastDyn's Cortex-M0 model; both write RAM word
`42`. Its veneer is an optional explicit NOP followed by `ldr r3, [pc, #0]`,
`bx r3`, and a symbolic 32-bit `target+1` literal. The final artifact records
two synthesized instructions and four bytes of typed inline data with exact
ownership, source-call and target identities. A first linked image caught a
real per-function-fragment alignment error: an assembler alignment directive
was evaluated before fixed-address rebasing, so the final LDR read the BX
encoding rather than the literal. The accepted implementation computes and
emits padding from the final retained function address. It uses the AAPCS
call-clobbered `r3`, sets the Thumb-state bit explicitly, adds no segment and
validates the final literal address. The fixture uses explicit unreachable
padding as a qualified caller-owned budget and opts out of the default global
instruction-count policy; this does not authorize external slack or arbitrary
whole-image placement.

A second Thumb-1 gate uses an external range without inferring slack. The input
linker reserves a read-only executable `.firmbolt.veneer` section, and each
word-aligned slot has an exact `__firmbolt_thumb_veneer_slot*` function identity
and size. Firm-BOLT rejects a name/section mismatch or any original direct or
relocation reference to the slot, then proves the complete source range can
reach it. With the default guest-instruction-count
contract enabled, it installs the unchanged-count caller plus one eight-byte
slot, changes 12 bytes, emits no new segment and records slot allocation
separately from branch relaxation. The two synthesized instructions and the
four-byte literal retain the exact original call identity. Reference and
candidate both write `42` on Cortex-M0. This qualifies explicitly typed
external capacity, not an arbitrary NOP run, symbol gap or movable image.

The M4 fixed-work reference is reproducible, all variants finish 10,000 work
units, and the admitted candidate matches the reference's exact 2 MiB RAM-prefix
hash under the same modeled schedule. This is one semantic smoke, not a speedup
estimate or proof across arbitrary schedules. The remaining target backlog is
literal islands without a proven non-fallthrough owner and ARM relocation
classes beyond the now-qualified JUMP6/ABS8/ABS16 set,
veneer placement without explicit typed slots or under arbitrary whole-image
layout, non-ADR ambiguous table bases,
DSP/FPU/MVE-specific optimization rules,
Secure/Non-secure and PACBTI-M state transformations and handler rewrites
beyond the explicit same-count exception-entry contract. Native identical-code
folding, function splitting/reordering, indirect-call promotion and ARM
tail-duplication copy propagation remain outside the qualified firmware
pipeline; target-generic code in BOLT is not sufficient without entry,
reference, placement and observation contracts.
Search remains optional until the five unknown S2 verifier results and three
counterexamples are understood. FastDyn patch behavior remains phase two.

## 10. Limitations

This is one LEMIX application under a low-fidelity zero-backed MMIO environment
plus qualified M0+/M3/M4 binary-processing cases and one runnable Cortex-M3/M4
FastDyn configuration. The LEMIX x86-64 ABI, pointer width, security landing
pads, host RTOS port and timer behavior differ from the source MCU. The
candidate cohort is structurally screened and the timed search covers six
cases. The bounded verifier is appropriate only to the loop-free regions and
declared model. The native target backend does not by itself qualify arbitrary
MPU/device/observer contracts; the eight explicitly retained linker-unmapped
M4 bodies and independent negative fixtures demonstrate that this acceptance
boundary remains active.
Finite tests and matched RAM prefixes
do not prove preservation of unknown vulnerabilities. No hardware counters
were available and no second input-driven whole application was introduced.
One earlier bounded layout candidate is an accepted original-image speedup
under the declared FastDyn/QEMU configuration; it does not establish hardware
speed, deadline preservation, broad Cortex-M optimization benefit or
equivalence under arbitrary interrupt/device schedules. The real M0+ image is
structurally validated but still lacks its board/device execution model.

## 11. Reproduction and evidence

Run the documented top-level command:

```bash
scripts/preflight/run-lemix-o0-focused-rerun.sh \
  build/preflight/lemix-o0-rerun-YYYYMMDD

scripts/preflight/run-preflight-prototype.sh \
  build/preflight/preflight-sdk-v1-2026-11-16

scripts/preflight/run-bolt-firmware-wrapper.sh \
  build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16

scripts/preflight/run-bolt-queue-policy-matrix.sh

python3 scripts/preflight/run-firm-bolt-cortex-m-port.py \
  --skip-build --full-pipeline --frame-opt=all \
  --output build/preflight/firm-bolt-cortex-m-production-parity-v123-jt-footprint-r5-2026-11-18

scripts/preflight/run-recovered-corpus-wrapper.sh

scripts/preflight/run-smartspeaker-layout-overlay.sh

python3 scripts/preflight/build-smartspeaker-stock-frame-overlay.py

python3 scripts/preflight/run-smartspeaker-stock-frame-overlay-training.py ||   test $? -eq 2

scripts/preflight/run-smartspeaker-10x-confirmatory.sh

python3 scripts/preflight/run-smartspeaker-wrapper-policy.py

python3 scripts/preflight/run-smartspeaker-inplace-policy.py

python3 scripts/preflight/run-smartspeaker-isolated-inplace-policy.py

python3 scripts/preflight/build-smartspeaker-fop-hot.py

python3 scripts/preflight/run-smartspeaker-fop-hot-policy.py

python3 scripts/preflight/build-smartspeaker-absolute-reference-negative.py

python3 scripts/preflight/build-smartspeaker-o3-oracle.py

python3 scripts/preflight/run-smartspeaker-o3-comparison.py

python3 scripts/preflight/analyze-smartspeaker-o3-oracle.py

scripts/preflight/run-thumb-preflight.sh

PYTHONPATH=src taskset -c 0 \
  python3 scripts/preflight/profile-thumb-unused-frame-pointers.py --replace

PYTHONPATH=src taskset -c 0 \
  python3 scripts/preflight/run-thumb-hot-exit-layout-pilot.py --replace
```

The raw commands, logs, samples, disassemblies, inventories, candidate bodies, machine outputs, hashes, counterexamples, and costs are retained separately as supporting data. Primary baseline entry points are [manifest.json](../../build/preflight/lemix-o0-rerun-2026-11-16/manifest.json), [bolt-arm-options.json](../../build/preflight/lemix-o0-rerun-2026-11-16/bolt-arm-options.json), [samples.jsonl](../../build/preflight/lemix-o0-rerun-2026-11-16/samples.jsonl), [paired-results.json](../../build/preflight/lemix-o0-rerun-2026-11-16/paired-results.json), [binary-layout-inventory.json](../../build/preflight/lemix-o0-rerun-2026-11-16/binary-layout-inventory.json), [candidate-universe.csv](../../build/preflight/lemix-o0-rerun-2026-11-16/candidate-universe.csv), [stage-funnel.csv](../../build/preflight/lemix-o0-rerun-2026-11-16/stage-funnel.csv), [search-seeds-and-costs.json](../../build/preflight/lemix-o0-rerun-2026-11-16/search-seeds-and-costs.json), and [budget-and-costs.json](../../build/preflight/lemix-o0-rerun-2026-11-16/budget-and-costs.json). Prototype entry points are [prototype manifest](../../build/preflight/preflight-sdk-v1-2026-11-16/manifest.json), [stage ledger](../../build/preflight/preflight-sdk-v1-2026-11-16/stage-ledger.jsonl), and [pass outcomes](../../build/preflight/preflight-sdk-v1-2026-11-16/pass-outcomes.json). This document consolidates the historical screening experiment, focused rerun, and bounded prototype; historical raw evidence remains unchanged.

The current full Cortex-M production evidence is the
[v144 machine report](../../build/preflight/firm-bolt-cortex-m-v144-exception-return-contract-final/report.json),
SHA-256 `7905674c159b718917eddca0cc8d16179c40d569da3fcaad030fe1334fa51fd1`;
its [validation manifest](../../build/preflight/firm-bolt-cortex-m-v144-exception-return-contract-final/validation-manifest.json),
SHA-256 `5e3e08086667f5ac84d2921c367312d4034d5661ef4b7bd937ff9cb2f3f1f6b4`,
binds the exact regression commands and XML evidence. It accepts 55/55
production and architectural profiles with ordinary FOP enabled. v138 adapted
BOLT's x86-oriented `RegReAssign` pass through target-owned legality, encoding
and byte-cost hooks; v139 added typed `SHT_ARM_EXIDX` range analysis; v140 and
v141 atomically couple inline and uniquely owned external ARM-defined compact
EHABI programs to an accepted exchange. The conservative fixture changes
R8/R4, shrinks 26→18 bytes and renames every admitted `pop {r8}` to
`pop {r4}`; shared, generic and non-representable programs remain unchanged.

v142 reuses BOLT's call graph, `RegAnalysis` and `LivenessAnalysis` for the
separate aggressive path. It considers only caller-saved R2/R3-to-R12 pairs,
requires `--assume-abi` and an explicit contract, excludes tail calls, ABI
outputs and callee-saved names, and requires both exact core-register names
dead at every entry. v143 admits an ordinary direct call only when its exact
transitive callee-use summary observes neither candidate and both values are
dead after the call; unknown, indirect and unresolved calls remain unchanged.
The executed leaf shrinks 14→10 bytes and the direct-call worker 24→20 bytes.
Live-in, callee-observed, indirect-call and live-after-call controls stay
byte-identical; reference and candidate produce
`(42, 4, 8, 42, 54, 43, 12, 43)` under FastDyn with exact origins. Generic
unwind data, debug locations, asynchronous observers and measured
profitability remain outside this result. The full pipeline records an
instruction-shortening attempt on the tail-call control, but compact
publication reproduces its original 12 bytes. v144 distinguishes reset-vector
and exception-vector roles and preserves every ARM-classified possible
EXC_RETURN consumer by exact original identity and instruction text. The real
M0+, M3 and M4 images expose 18 exception-entry functions and 15 possible
exception-return instructions; the gate accepts all of them. Full MSP/PSP,
privilege/mask and nested-interrupt scheduling remain outside this result. The retained v137
conditional-move, v135
conditional-tail-call, v134 three-way-branch, v133 size-one specialization,
v132 fixed-size copy, v123 jump-table-footprint, v121 generic-personality and
v122 alignment results remain part of the aggregate gate. These are
integration, architectural-execution and preservation results, not a runtime
speedup, exception-path, callback/patch or arbitrary-firmware proof.

The confirmed mechanism is narrower than saying “ARM constant propagation was absent.” The generic BOLT pass existed, but ARM lacked its PC-relative displacement and constant-materialization hooks. v107 adds qualified word/byte/halfword and signed-narrow decoding plus flag-preserving `MOVW`/`MOVT` materialization for `r0`–`r12`. Loaded data must be allocatable, non-writable and relocation-free across a conservative four-byte range. Section immutability alone does not authorize removal of the flash-read/fault event. Rejected functions are now reconstructed in the final origin manifest from the immutable pre-transform instruction ledger rather than the mutated candidate CFG.

The mechanical pinned-source patch is [bolt-fastdyn-preflight.patch](../../patches/bolt-fastdyn-preflight.patch), SHA-256 `72f09a3b95f30b29b83571789d819bf97f409751fae130bc8ea347eeee80a6e1`; it clean-applies to the pinned revision and matches the tested checkout. The tested `llvm-bolt` SHA-256 is `59ed2d86219d210caabf039d501c02ac97d92624c4a92b229ad67a22149345af`. Regression runs complete 78/78 ARM BOLT lit tests, 12/12 AArch32 JITLink lit tests, and 129 passed plus 147 explicitly skipped BOLT unit parameterizations (Core 85/147, Passes 5/0 and Profile 39/0).

The [v123 validation manifest](../../build/preflight/firm-bolt-cortex-m-production-parity-v123-jt-footprint-r5-2026-11-18/validation-manifest.json), SHA-256 `3e574846c0dd3252c071d90435074a73f2b24341f2bd534b73d663c3cc7e85f0`, records the exact regression commands, outcomes and evidence hashes. Earlier reports remain unchanged as historical evidence.

### Pinned references

- LLVM/BOLT commit `7864c0736a8a09670dfbfc513f1e9649799f2486`, including `FrameOptimizer.cpp` and `BinaryPassManager.cpp`.
- STOKE commit `98d8a0f028f2daf2052bfe607dbc32ec8d55ba9e`, with submodules and solver configuration recorded in [stoke-environment.json](../../build/preflight/lemix-o0-rerun-2026-11-16/stoke-environment.json).
- Official LEMIX artifact archive SHA-256 `b9133d97b4041d7aec92a6f36a533e9b226107ad2a81ceee1dd70651800a3727` (historical MD5 retained as required).
- [Linux userspace shadow-stack enablement documentation](https://docs.kernel.org/arch/x86/shstk.html), used only to distinguish ELF properties from runtime enablement.
