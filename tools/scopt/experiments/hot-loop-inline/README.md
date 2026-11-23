# Inlining calls inside hot firmware loops

These development runs replay the FreeRTOS compute application (Cortex-M3,
`O0-fb`) from an ordinary sealed cache. Both guest and helper policies use
automatic PGO. Each selects calls in cycles observed during training. A cycle
can cross function boundaries. The guest policy embeds one callee layer per
selected call. The helper policy counts executions at each original native call
instruction, including sites on conditional slow paths.

## Native firmware execution time

On the updated QEMU runtime the 12-region guest policy measured **1.106 s**
versus **7.440 s** with the old linear search reenabled on the *same artifact*
(6 pairs, **6.747×**, 95% interval **6.088–7.034×**). With the search fixed,
the application-level comparison against ordinary sealed execution was:

| Independent series | Ordinary median | Inlined median | Median paired speedup (95% bootstrap interval) |
|---|---:|---:|---:|
| CPU 4, 6 pairs | 1.083 s | 1.056 s | 1.034× (0.986–1.051) |
| CPU 6, 8 pairs | 1.144 s | 1.100 s | 1.031× (0.998–1.078) |

Both estimates favor inlining on this input and both intervals include parity.
The following measurements on the previous runtime motivated the lookup fix.

| Policy | Pairs | Ordinary median | Candidate median | Median paired speedup (95% bootstrap interval) |
|---|---:|---:|---:|---:|
| Guest, at least 1,000 observed calls, 12 regions | 8 | 1.091 s | 6.704 s | 0.162× (0.159–0.168) |
| Guest, at least 1,000,000 observed calls, 1 region | 6 | 1.140 s | 1.120 s | 1.024× (0.958–1.049) |
| Native helper, eliminate the call, 1 site | 8 | 1.139 s | 1.151 s | 0.974× (0.953–1.009) |
| Native helper, also admit shared wrappers, 4 sites | 8 | 1.100 s | 1.093 s | 0.977× (0.964–1.042) |

The time boundary is the firmware's `[BENCH][fw]` interval in fresh pinned
processes with offline compilation, training and setup excluded. The ratio uses
paired observations, so it can differ from dividing the two column medians.
All 60 timed processes produced the same 16-register transcript and the sealed
cache files retained their hashes. Each experiment used its own interleaved
ordinary arm. The same input was used for training and measurement.

All 12 guest regions activated on the diagnostic replay. The hottest call was
`prvRxTask` at `0x4a70: bl 0x331c`. An independent breakpoint reached the
copied callee. Disabling region entry on the same 12-region artifact restored
1.065 s in a diagnostic run. External native-PC sampling placed 265 of 327
worker samples in the safe loader's linear `sc7_source_index` search, versus
0 of 53 with regions disabled. In this runtime each region comparison can
scan up to 38,252 public records. Narrowing selection to one hot call removed
the large regression, with an interval spanning parity.

The updated loader indexes each validated source to its first region once and
uses that index for safe admission. A fresh capture bound the updated QEMU
executable to the firmware and both timing arms. The linear and indexed arms
each activated 12 regions and performed 450 dependency hash checks. In a
separate debugger run, flipping one bit in the in-memory expected guest hash at
the first `sc7_try_region` caused one refusal, left 11 regions active, and
preserved all 16 outputs. To reproduce the timing control, set
`QEMU_SC_V7_EXPERIMENT=linear-source-scan` only in the linear arm of the
same safe-artifact comparison. Inspect `source_index` and `linear_source_scan`
in the separate stats runs. [Paired observations and guard diagnostics](runtime-index.json)

Run the [dependency refusal regression](../../tests/qemu/safe_region_index.py)
with a compiled indexed safe-region launch and a fresh output directory:

```sh
python3 tools/scopt/tests/qemu/safe_region_index.py "$INDEXED/launch.json" "$OUT/refusal"
```

Only 741 of 22,025 annotated native helper sites executed during the profiled
replay. The selected shift helper eliminated an actual call on 5,264 executions
and grew the artifact by 120 native bytes. The three additional wrappers retained
calls to shared code. `helper_lookup_tb_ptr` accounted for about 12.4 million
calls in observed cycles, but the selected-root emitter cannot inline its live
runtime checks and return-address-sensitive profiling path. Neither helper
policy established a gain. Keep these policies opt-in.

## Reproduction

Start with an ordinary sealed snapshot and its compiled direct launch of a
bounded, deterministic workload as described in [getting started](../../docs/getting-started.md).
Set `SNAP` to the imported snapshot directory, `BASE` to its compiled-launch
directory and `OUT` to a fresh output directory. The guest build needs its
captured ELF, the snapshot runtime identity file and the bound layout from the
matching QEMU debug build.

```sh
mkdir -p "$OUT"
scopt optimize "$SNAP" --pipeline hot-loop-inline \
  --pgo-launch "$BASE/launch.json" --store "$OUT/helpers" > "$OUT/helper.json"

scopt optimize "$SNAP" --pipeline hot-loop-firmware-inline \
  --pgo-launch "$BASE/launch.json" --elf "$ELF" --v7-exe "$QEMU_SHA256_ID" \
  --runtime-identity "$RUNTIME_JSON" --store "$OUT/guest" > "$OUT/guest.json"

PYTHONPATH=src:tools/scopt python3 tools/scopt/experiments/hot-loop-inline/measure.py \
  "$BASE/launch.json" "$SNAP" "$(jq -r .artifact_dir "$OUT/guest.json")" \
  "$OUT/guest-timing" --cpu 4 --pairs 8 --seed 127903
```

Use the Python environment installed for scopt. The measurement script accepts
each input as a cache file or a directory containing `cache.sc`. It expects
private RAM mappings and the FreeRTOS fixed-work completion marker and
register output. To change guest selection, pass
`--params` with `{"firmware-inline":{"min_call_count":1000000}}`.
The guest experiment reused its reached training phase for the one-region
revision. An explicitly supplied edge profile must identify the same snapshot.
For the additional helper wrappers, pass
`{"host-inline":{"include_hot_shared_wrappers":true}}`.

[Raw block timings, identities, training counts and region diagnostics](results.json)
