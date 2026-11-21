# Response to the scopt repair brief (2026-11-21)

Tested revision: FastDyn `scopt` at `7e8666949f9c` (the reviewed commit; no later commit on
the branch), QEMU submodule `957530a637cf`, built on x86-64 Linux; QEMU executable sha256
`b11ba8e4b2be…`. FirmBOLT `aa7db77` for the Trebuchet images. The changes are on branch
`scopt-repair`.

Limits of this response: the native host-inline pipelines were not run, because their
LLVM 20.1.2 bridge is not built here. Items that need them say so. Timing used one host,
CPUs 8-11, one firmware (the Trebuchet compute firmware, FirmBOLT image).

## Verdicts

| # | concern | verdict |
|---|---|---|
| 1 | replay and ablation drivers do not request opt-in passes | confirmed and fixed |
| 2 | failed runs can enter performance reports | confirmed and fixed |
| 3 | stock QEMU output oracle | confirmed and fixed |
| 4 | original and physical call offsets | confirmed at the analysis level and fixed; not shown through the full native pipeline |
| 5 | composition | confirmed for SFB + B3, implemented and measured (1.16× instead of 1.07×); helper-level cleanup deferred, no hot target on this workload |
| 6 | layout sees the code that executes | deferred: specialization creates no variant on this workload, so there is nothing for layout to miss |
| 7 | global facts influence rewrites | source observation confirmed; deferred for the same reason |
| 8 | state continuity across TB boundaries | not investigated; recommended next census |
| 9 | variable-length rewriting, zero with live flags | zero-with-live-flags omission confirmed in source; deferred, constant propagation has no input on this workload |
| 10 | broader native coverage | deferred: helper calls other than the TB lookup are under 1 % of TB entries here |

## 1. Pass selection in the drivers (confirmed, fixed)

`manager.py` runs seven passes only on request. Three drivers named pipelines without
requesting them:

- `run_program_arm.sh`: arm `b3` ran `--pipeline b3`, whose `specialize-returns`,
  `chain-returns` and `chain-static` are all opt-in; arm `b4` ran `--pipeline b4`, whose
  only pass is opt-in. Both arms measured a cache with no edit from those passes.
- `benchmark_passes.py` passed `--disable` for stages outside an arm and nothing for
  stages inside it, so `local-scalar-cleanup` and `specialize-edges` never ran in any
  prefix or leave-one-out arm.
- `compose_program.py` ran `native-program` the same way and then expects a private
  variant and a link.

No wrapper supplies the missing options, and the scripts predate commit `4163f48`
("Disable seven lower-ranked passes by default"), so they were written to run those
stages. This says nothing about measurements archived before that commit.

Evidence on a real snapshot (FirmBOLT image, pipeline `b3`):

| invocation | specialize-returns | chain-returns | chain-static |
|---|---|---|---|
| pipeline named only | policy_disabled | policy_disabled | policy_disabled |
| stages requested | no_change (ran) | edited (52) | edited (233) |

Repair: `scopt/run/pass_policy.py` builds the `--enable`/`--disable` options for an arm
(`policy_arguments`) and checks the receipts afterwards (`check_requested`). It keeps four
outcomes apart: `policy_disabled`, `unsupported`, `no_change`, `edited`. A requested stage
that was skipped by policy or could not run fails the arm; one that ran and changed nothing
does not. The three drivers use it and write the effective configuration next to the result.
Production defaults are unchanged. Tests: `tests/unit/test_pass_policy.py`.

Not done: the drivers themselves were not re-run end to end (they need the native bridge).

## 2. Failed runs in reports (confirmed, fixed)

`tools/trebuchet/lib/bench.py` recorded `ok=false` and exited 0, and `report.py` computed
medians over every sample. Nothing outside enforced a stronger rule. All 168 samples of the
published campaign were valid.

Rule, chosen before looking at any outcome: one failed launch, or a block missing an arm,
invalidates the whole campaign, because every reported step is a ratio inside a block.
`bench.py` keeps the sample and its reasons, writes `"valid": false` and exits 1.
`report.py` names the failed launches and exits 2 without a speedup table; `--diagnostic`
prints the numbers under a "diagnostic only" banner and still exits 2. Nothing is dropped,
replaced or repeated selectively.

Evidence: a wrong expected value injected through the real runner stops the campaign at
that launch with the field and both values; a copy of the valid campaign with one sample
marked failed, or one sample removed, is refused; the unchanged campaign reproduces its
medians (1173.7, 1437.4, 1409.6, 1358.2, 1270.1 ms) and 1.131×. Tests:
`tools/trebuchet/tests/test_results.py`.

## 3. Stock QEMU oracle (confirmed, fixed)

The comparator accepted the three expected values anywhere in the statistics array and
checked three of the sixteen reported values. `Stats_t` at the tested revision has
`frames_ok`, `processed` and `digest` at words 0, 10 and 15, as the review found.

Repair: `tools/trebuchet/lib/result_schema.py` maps each reported value to its structure
field, and both arms are checked against the same sixteen expected values from the
reference transcript. The stock arm compares 15 of them at their own fields; the sixteenth
(`kv ^ kv_evict`) mixes in a local that is not in the structure. `heartbeats`,
`monitor_scans` and `monitor_kv_sum` are not compared: the first two are marked timing
dependent in the source and none is reported.

The mapping has an independent check: a test parses `Stats_t` and the four report calls out
of `main_compute.c` and compares with the table. Tests cover values parked in other words
(the review's case), swapped fields, corruption of every reported field, and changes to the
timing-dependent counters. On a real stock run all 15 fields agree with the FastDyn
reference, a one-bit change in `err_seq` is reported, and another input's reference gives
14 mismatches.

## 4. Call offsets after materialization (confirmed, fixed)

Reproduced with the repository's two-splice fixture plus one untouched call at offset 36:
calls at 11 and 52 are inlined, the first grows by 16 bytes, the untouched call lands on
physical offset 52, and `build_host_calls` returned no row for it although the unit's site
table lists it. Cause: `call_off` of a certificate is an offset in the code it was proved
against; after materialization the sites are physical.

No construction rule prevents this: the splice group only requires that spans do not
overlap. The analysis feeds `native-callees` (candidates for further inlining, and one use
in `validate.py`) and the `host-calls` inventory. I found no proof that depends on a row
being absent, so the effect is a missing candidate and a missing inventory row, as the
review said.

Repair: the subtraction now applies only while the splices are not materialized
(`unit.splice_applied` false), when the retired call still stands at its certificate
offset. Afterwards the retired calls are gone from code and sites, and nothing is
subtracted. I chose this over rewriting certificate offsets, which would break their tie to
the proved source, and over stable call identities, which is a larger change than this
defect needs. Retired calls stay on record in `native_fragments`.

Tests (`tests/unit/test_host_calls_offsets.py`): the collision; the same unit moved, with
its rel32 relinked; and the unmaterialized state, where the certificates must still hide
their calls. The first two fail without the repair.

Not done: reproduction through import, optimize, publish and re-import with the real
host-inline pass. Guarded fallback calls and native islands were read, not exercised.

## 5. Composition (SFB + B3 confirmed and measured)

The brief's concrete candidate holds. Trebuchet said no packager combines the
store-forwarding fix with chaining. None is needed: re-importing the `sfb` artifact as a
snapshot and running `b3` on it, with the ordinary snapshot as warm baseline and the `sfb`
artifact as the intermediate stage, publishes one v6 cache. Its chaining receipts are the
same as on the plain snapshot (52 and 233 slots). It passes `publish` and the dynamic check
on three held-out inputs with no block appended.

Protocol (`protocol-sfb-b3.md`) was written before the combination was timed.

Selection, `scopt tune-warm`, 12 pairs per case (`selection-sfb-b3.json`):

| candidate | heldout-1 | wins |
|---|---:|---:|
| sfb | 1.088× | 12/12 |
| b3 | 1.057× | 12/12 |
| sfb+b3 | 1.185× | 12/12 |

Confirmation of sfb+b3: 1.187× on heldout-2, 1.177× on heldout-3, 12/12 each.

Final evaluation on final-1..3, 24 blocks, four arms per block (`final-sfb-b3.json`; all 96
launches valid):

| arm | guest work | speedup over ordinary warm [95 %] |
|---|---:|---:|
| ordinary warm | 1368.9 ms | 1.000× |
| sfb | 1269.9 ms | 1.073× [1.066, 1.079] |
| b3 | 1301.0 ms | 1.052× [1.046, 1.060] |
| sfb+b3 | 1177.0 ms | 1.161× [1.156, 1.171] |

The combination is worth more than the product of its parts (1.073 × 1.052 = 1.129). I
have not established why.

Change: Trebuchet builds `sfb+b3` as a sixth candidate (`trebuchet.sh compose`) and lets
`tune-warm` decide. Re-run through the driver with all six candidates, `tune-warm` selected
`sfb+b3` again (1.179× on heldout-1; 1.179× and 1.170× in confirmation), and the five-arm
campaign gave 1174, 1442, 1411, 1361 and 1177 ms: scopt step 1.154× [1.149, 1.162],
pipeline 1.224× [1.218, 1.229], and 0.994× [0.990, 1.004] of stock QEMU on guest work
(`tools/trebuchet/results/2026-11-21-composed/`). `sfb` remains what scopt calls a fix of baseline TCG code, so part of
this gain is not an optimizer result in that sense.

Helper-level cleanup across a former call boundary was not attempted. The census in §10
shows no hot helper to apply it to on this workload.

## 6-8. Layout, global facts, state continuity (deferred)

Exact steady-phase profile of the FirmBOLT image over the three training inputs
(`census.json`), 377.8 M block transitions:

| transition | count | share |
|---|---:|---:|
| direct chained (`chain0`, `chain1`) | 333.5 M | 88.3 % |
| TB lookup hit | 40.2 M | 10.6 % |
| exit to the main loop | 4.1 M | 1.1 % |

`specialize-returns` ran and created no variant. Its refusals: target not profiled 116,
fact not constant 84, provenance unexplained 30, fact unused 29, exit not through
`goto_ptr` 21, fact not local to the return TB 4. With no variant, constant propagation,
branch folding and dead-op removal have no input.

- **6.** The omissions the review describes concern private variants and guarded sources.
  This workload has no variant, and the two layout candidates that do apply are null here
  (`b2-arena-slide` 1.003× on the first selection run and 0.992× on the second,
  `exit-pc-fold` 0.999× and 0.996×). I did not build a layout over transformed code.
- **7.** The source observation is right: `program.py` sets `fallback=True` on captured
  edges and the solver only passes a checked direct-edge transfer when it is false. I did
  not extend it. 84 refusals are for facts that are not constant at the return site, which
  propagation would not change by itself, and 116 are for unprofiled targets.
- **8.** Not investigated. It is where the largest share of transitions is (88 % are
  chained block boundaries), so it is the census I would run next: count, weighted by the
  exact edge profile, the guest-state stores at the end of a block that the chained
  successor reloads. The existing analyses do not produce that number.

## 9. Variable-length rewriting (zero fallback confirmed, deferred)

`constant_propagation.py` refuses a known zero with live flags as `flags_live` without
trying `mov r32, 0`, which fits a `[rbp+disp32]` load. That is a real omission. I did not
change it: the pass never runs on this workload (no variant), so I could neither measure a
benefit nor exercise the new template through the symbolic validator on a real variant.
NOP regions and encoding-size refusals were not counted for the same reason.

## 10. Native coverage (deferred)

Call-site census of the same snapshot (`scopt host-calls`, 40,269 sites). The weight is
the number of entries of the enclosing TB, a proxy and not a call count:

| callee | TB entries at its sites |
|---|---:|
| load/store slow path | 1,274.3 M (taken only on a TLB miss; the proxy overstates it most) |
| `lookup_tb_ptr` | 43.0 M |
| `v7m_msr` | 3.4 M |
| `rebuild_hflags_m32_newel` | 3.4 M |
| `exception_internal` | 0.5 M |
| `udiv` | 0.3 M |
| `v7m_mrs` | 0.2 M |

The TB lookup is what chaining already removes. Every other helper sits in blocks that
account for under 1 % of the 377.8 M transitions. No admission extension would pay on this
firmware. A workload with hot device or virtual callbacks would be the place to revisit it.

## What changed, by kind

- **Correctness fixes:** host-call offsets (§4); campaign validity and the stock oracle in
  Trebuchet (§2, §3).
- **Reproduction fixes:** explicit pass requests and receipt checks in three drivers (§1).
- **Experimental, selected by measurement:** the `sfb+b3` candidate in Trebuchet (§5).
- **Defaults:** none changed. The seven opt-in passes stay opt-in.

## Reproduce

```sh
cd tools/scopt && python -m pytest tests -m 'not qemu' -q          # 1,321 passed here
python3 -m pytest tools/trebuchet/tests -q                          # 10 passed
tools/trebuchet/trebuchet.sh all                                    # builds sfb+b3, selects, measures
# four-arm comparison for one image (after `prepare`):
python tools/trebuchet/lib/compare.py out.json 8 O0-fb warm=SNAPSHOT sfb=ART b3=ART sfb+b3=ART
```

With the maintainer fixtures available (`FASTDYN_ROOT` set to that workspace) the scopt
suite gives 1,948 passed and 1 failed. The failure,
`tests/unit/test_wp20_return_edges.py::test_analysis_store_wiring_minimal`, is the same on
the unmodified branch; I did not investigate it.

## Remaining limits and next experiment

- One firmware and one host. The SFB + B3 result should be repeated on the six-workload set.
- The warm caches cover every timed input. A discovery-mode experiment, where final inputs
  do not contribute to the cache, was not run.
- Nothing here measures a fuzzing iteration (reset, input install, run, result collection).
- Next: the cross-boundary state census of §8, then SFB + B3 with native inlining once the
  bridge is built.
