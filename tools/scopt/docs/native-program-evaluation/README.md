# FastDyn native optimizer benchmark

Each speedup divides the preceding or reference arm’s vCPU interval by the optimized arm’s interval. Values above 1 favor the optimized arm. The two numbers in brackets give a paired 95% bootstrap interval. Fixed-work guest output and warm-cache checks apply to every timed run.

## Combined pipeline on the modifier and state-forwarding fixture

The hot path executes an RBIT helper and reaches a guarded successor. One held-out input exercises the specialized path. The other can enter the general path. The ablation column compares the entire pipeline against the same pipeline with only the named pass disabled. The raw sample files include both input-specific results.

| Pass | Against previous prefix | Full pipeline against ablation | Cache image identical to preceding prefix |
| --- | ---: | ---: | :---: |
| `host-inline-splice` | 0.998× [0.986, 1.052] | 1.004× [0.968, 1.030] | no |
| `local-scalar-cleanup` | 0.990× [0.966, 1.064] | 0.998× [0.949, 1.011] | yes |
| `specialize-edges` | 0.992× [0.984, 1.066] | 0.927× [0.902, 0.992] | no |
| `constant-propagation` | 0.943× [0.914, 0.973] | 0.960× [0.929, 0.986] | no |
| `branch-folding` | 0.985× [0.951, 0.999] | 1.052× [0.968, 1.072] | no |
| `dead-host-ops` | 1.022× [0.952, 1.062] | 0.982× [0.948, 1.036] | no |
| `native-scalar-cleanup` | 1.015× [0.957, 1.103] | 0.964× [0.939, 1.011] | yes |
| `region-state-forward` | 0.987× [0.968, 1.070] | 0.976× [0.958, 1.006] | no |
| `region-state-dse` | 0.984× [0.954, 1.004] | 0.976× [0.960, 1.014] | yes |
| `hotcold-partition` | 1.015× [0.946, 1.037] | 0.972× [0.951, 1.009] | yes |
| `llvm-cache-layout` | 1.023× [1.001, 1.042] | 0.985× [0.959, 1.125] | no |
| `order-cold` | 0.979× [0.946, 1.061] | 1.014× [0.936, 1.047] | no |

For the whole pipeline against the original warm capture, the measured speed ratio was **0.953× [0.931, 0.998]**. Against the identity-linked cache it was **0.972× [0.920, 1.014]**.

## Experiments on other workloads

| Workload and comparison | Median speedup and 95% interval |
| --- | ---: |
| State fixture, full six-pass pipeline versus warm, 30 pairs | 0.972× [0.954, 0.997] |
| State fixture, removal of same-value store versus preceding pipeline, 30 pairs | 1.005× [0.997, 1.042] |
| RBIT firmware, splice versus warm | 1.070× [1.009, 1.155] |
| RBIT firmware, second capture, splice versus warm | 1.107× [1.015, 1.192] |
| RBIT firmware, second capture, splice versus identity link | 1.045× [0.981, 1.125] |
| RBIT firmware, final fresh capture, splice versus warm | 1.138× [1.089, 1.204] |
| Private state DSE fixture, enabled versus disabled | 0.977× [0.940, 1.001] |
| Nested native frame, compacted versus original | 1.010× [0.974, 1.068] |
| Three-TB fixture, transformed versus warm | 0.962× [0.940, 1.081] |
| Partial-mode guarded artifact versus warm | 0.775× [0.682, 0.883] |
| Complete-mode guarded artifact versus warm | 0.839× [0.813, 0.903] |
| Complete versus partial, identical cache bytes | 1.012× [0.984, 1.102] |
| One RBIT splice per TB versus warm | 0.991× [0.977, 1.029] |
| Two RBIT splices per TB versus warm | 1.139× [1.086, 1.219] |
| Two RBIT splices per TB versus one | 1.156× [1.069, 1.191] |
| FreeRTOS, scalar cleanup versus warm | 0.992× [0.967, 1.009] |
| FreeRTOS, scalar cleanup versus identity | 1.019× [0.975, 1.075] |
| FreeRTOS, identity versus warm | 0.977× [0.932, 1.004] |
| FreeRTOS, scalar cleanup versus warm | 0.992× [0.860, 1.079] |
| FreeRTOS, native-program versus warm | 0.979× [0.927, 1.196] |
| FreeRTOS, native-program versus scalar cleanup | 1.040× [0.980, 1.148] |
| FreeRTOS, LLVM cache-directed placement versus existing Pettis–Hansen placement, 10 pairs | 0.976× [0.835, 1.388] |

## What changed on the hot path

On the separate region-DSE fixture the optimized path overwrites r3 before any admitted observer. Its first store, `mov dword ptr [rbp + 0xc], 5`, becomes a seven-byte NOP. The independent checker accepted the private r0=0 contract and rejected the same deletion on the public entry. All general bodies were identical between enabled and disabled arms.

The same-TB two-splice firmware executes two different RBIT operations. One-site mode leaves the second call in place. Two-site mode replaces both and adjusts the second fragment’s physical offset after the first grows. All three timing arms returned identical expected register values.

On the state fixture specialization first installs a guard. Constant propagation then makes the first private variant profitable to emit, so that prefix comparison includes routing through the variant. It folds one environment load. Later passes fold a conditional transfer, eliminate a dead host operation, and replace the seven-byte store `mov dword ptr [rbp], 0` with NOPs when the guarded entry already establishes the value. Independent entry can still reach the general store. The extra dispatch and guard work can outweigh saving those instructions. The timing alone cannot distinguish guard cost, code placement, and microarchitectural effects.

FreeRTOS yielded seven three-byte TEST instructions immediately after AND of the same host register. The observable condition flags were already available from AND. The auxiliary flag had to be dead for each admitted edit. V12 and V10 passed for the edited cache.

Frame compaction removed two explicit NOPs and an inner `lea rsp, [rsp + 8]` immediately killed by the enclosing `leave`. The nested expansion changed from 22 instructions and 80 growth bytes to 19 instructions and 72 growth bytes. Both frame assembly listings are included.

The coverage fixture’s partial and complete cache images have identical SHA-256 hashes, including all 17 general translations, the variant and the link extension. Different timings for these two arms measure run-to-run variation. The even held-out input exercised the guarded variant and took longer than warm. The odd general path was close to parity.

## Measurement and validation

FastDyn started at `817ca5ef7f91c2494dceba6929cd63dbcfae52f7` on `scopt`. The unmodified system runtime used QEMU `934890ed5119ef70da69276991d921573f23ed4f` and the native bridge used LLVM/BOLT 20.1.2. The translation producer and live-revocation fixture additionally applied the QEMU patches under `tools/scopt/patches`. The reproduction command from the checkout is `bash tools/scopt/scripts/reproduce_program.sh /tmp/scopt-program-run` after activating scopt and selecting the native bridge or pinned LLVM source as documented in `tools/scopt/docs/native-program.md`. Bootstrap build and package versions are in `source-build.json`.

Ubuntu 24.04 x86-64 userspace ran through Rosetta on an Apple M4 Pro. The guest was Cortex-M3. Runs used the same QEMU, plugin, guest-input hashes, instrumentation, instruction accounting and output oracle within each campaign. The RBIT, state, and transitive campaigns used two held-out inputs after separate training. Measured arms were interleaved within input and repetition. The recorded CPU affinity set permitted five virtual CPUs. Timing campaigns took a common exclusive lock. Some offline preparation and tests shared the machine. Short intervals, the host translator and scheduler variation limit extrapolation to physical x86 hardware.

The end-to-end run produced 70 baseline-family measurements and 260 per-pass measurements. Editing the shell driver while it ran caused an EOF error after its final benchmark had written the samples. The corrected driver parsed successfully and its independent `--verify-existing` check confirmed all 330 timed runs, 24 paired comparisons, and the published artifact. The stored result is `reproduction-check.json`.

The combined cache reimport bound metadata for all nine general TBs and recovered one link and one private variant. Running the native splice pass again on the splice generation applied zero edits and retained its snapshot ID.

Each prefix/ablation has separate offline timing and memory use in its pass evidence. Allocated sparse-cache bytes, general native code bytes and variant native code bytes are recorded separately in the combined campaign. NOP-padded edits can remove executed work without shrinking the native allocation. Heap use, files and optimizer time are excluded from the guest interval.

Static checks include recovery and native correspondence, local equivalence and private-entry proofs where applicable. V10 compares warm guest behavior and accounts for appended TBs. V18 differential tests used an independent x86-64 QEMU TCG execution engine with 330 forms and 15,840 trials, plus 43 rejected mutants. The full pytest run on Rosetta reported 1,018 passed, 659 skipped, and two failures in arithmetic auxiliary-flag checks. The same two tests passed under the independent x86 interpreter using write-once code pages. The direct Rosetta runner still disagreed when given write-once pages. The interpreted result and the host-native failures are separate evidence.

Short-branch widening has forward-cascade, backward-jump and rejection tests. Staged one-site and two-site caches both ran in QEMU. The executed RBIT fixtures did not require branch widening. Global known-bit masks survive merges. Only independently certified exact values or physical guards authorize a private-entry rewrite. One-partition and three-partition runs emitted byte-identical caches on the transitive fixture.

The admitted global specialization covers guarded static lookups and a bounded transitive predecessor case. Unknown indirect successors and unsupported native frame/branch combinations retain their baseline semantics. The CLI materializes and independently reimports a validated splice generation before B3 composition. The partial and complete producer contracts cover an explicitly declared immutable Thumb/context domain. A cold PendSV root and unresolved indirect transfers retained all 17 TBs in the reachability fixture. A live key violation disarmed the optimized chain before it resumed baseline execution. General-purpose whole-firmware closure, arbitrary helper frames and arbitrary state motion across observers remain outside the admitted transformations.

## Reproduce in the existing Ubuntu container

Run at `/opt/FastDyn` with a new output directory. The first baseline build used the locked setup script. The end-to-end replay checked the prior build hashes, regenerated both fixture captures, and ran the per-pass campaign.

```bash
source /opt/scopt-env.sh
export SCOPT_RUNTIME_BUILD_EVIDENCE=/opt/whopr-baseline/build-evidence.json
export SCOPT_HOST_INLINE_BRIDGE=/opt/scopt-deps/llvm-project-20.1.2/build/bin/scopt-native
export SCOPT_QEMU_USER_ORACLE=/usr/bin/qemu-x86_64
bash tools/scopt/scripts/reproduce_program.sh /tmp/scopt-new-run
```

## Evidence

`per-pass.csv` contains both incremental and leave-one-out comparisons across the applicable workloads. Extract `benchmark-evidence.tar.gz` to read the raw JSON, validation records and source/build evidence. The corresponding `*-raw.json` files contain matched per-input nanosecond samples. The downloadable `native-program-cache.tar.gz` is a GNU sparse tar archive for the recorded QEMU/plugin identity. `native-program.json`, `native-program-v10.json` and `native-program-v18.json` refer to the packaged cache. The full implementation and reproduction instructions are in the source patch.
