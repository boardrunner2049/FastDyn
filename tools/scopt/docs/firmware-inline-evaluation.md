# Firmware-inline evaluation

`firmware-inline@1` is active and behavior-correct on the bounded FreeRTOS
`RTOSDemo.axf` boot workload.  Its first trusted implementation regressed to
0.757x ordinary warm speed because region activation performed a nested linear
lookup.  Indexing source TBs fixes that runtime defect: the corrected trusted
implementation measures **1.156x** ordinary warm speed with a paired bootstrap
95% interval of **[1.132, 1.185]**.  Trusted mode remains explicit and opt-in.

The exact run used FastDyn `dedef1a`, QEMU `3927bf2`, QEMU executable SHA-256
`e34cdf7c...a284ba1`, and firmware SHA-256 `53ca19e3...4f0a04`.  The v7 loader
accepted artifact id `98d429cdc7827bce`; replay appended zero translations and
the v7 export/reimport/load proof passed.

## Activation and size

The pass discovered 30 verified Thumb call sites, emitted 27 regions at maximum
acyclic expansion depth 5, and reported four pending sites (one missing callee
capture and three missing continuation captures).  The artifact has 164 private
members and 131 internal edges.  During the measured run, 18 region entries
instantiated 118 member descriptors, registered only the 18 composite entries,
and admitted 95 internal edges.  Exact member recovery used the copied search
streams 119 times.

Private native code is 71,064 bytes versus 63,524 bytes for the copied source
TB bodies.  The 7,540-byte difference is return-context guards and ordinary
lookup fallbacks.  Total private-region arena footprint is 102,100 bytes.

## Native before and after

At guest call site `0x08003982 -> 0x0800398e`, the public caller exits through
the ordinary TB path:

```text
7fff6c0149a3: e9 00000000  jmp 7fff6c0149a8
7fff6c0149a8: lea rax,[rip-0xef]
7fff6c0149af: jmp 7fff6c000018
```

The private copy directly enters the copied callee:

```text
7fff6c027b73: e9 18000000  jmp 7fff6c027b90
```

The copied callee keeps guest return effects, checks full return context and PC,
then enters the copied continuation.  A mismatch uses the original lookup path
without replaying the return effects:

```text
7fff6c027c21: cmp dword ptr [rbp+0x3c],0x8003986
7fff6c027c2b: je  7fff6c027c36
7fff6c027c37: jmp 7fff6c027c4c        # embedded continuation
7fff6c027c41: mov rdi,rbp
7fff6c027c44: call qword ptr [rip-0x7a]
7fff6c027c4a: jmp rax                 # fallback
```

Run `scripts/inspect_firmware_inline.py` to reproduce complete public/private
disassembly and size accounting from an artifact.

## Timing result and diagnosis

Twelve randomized paired process blocks were pinned to CPU 0.  The measured
window is the firmware's bounded boot-to-scheduler interval:

| Candidate | Median speed versus ordinary warm | Paired bootstrap 95% interval |
| --- | ---: | ---: |
| current scopt | 0.991x | 0.979-1.017x |
| firmware-inline | 0.747x | 0.733-0.761x |

Against current scopt directly, firmware-inline measured 0.746x
`[0.739, 0.756]`.  This is a regression, not an inconclusive result.

The mechanism did run.  Composite registration reduced QEMU's host-PC tree
insertions from 118 member TBs to 18 entry TBs.  A live, fail-closed FastDyn
predicate then proved 116 of the 118 translation-callback replays inert; only
two instrumented members replayed and produced two callbacks.  Relative to the
correct composite implementation before that predicate, median candidate time
fell from about 327 us to 265 us and relative performance improved from 0.586x
to 0.747x.  This removes the specific redundant post-JIT callback work, but not
the regression.

That result established a regression but its initial diagnosis was incomplete.
The safe implementation did construct member descriptors, but trusted mode
removed that work and still regressed.  The later controlled runtime ablations
below identify the remaining dominant cost as source-to-region lookup, not
descriptor construction or the copied native code.

## Trusted no-bookkeeping result

The trusted run used FastDyn `8735fcb`, QEMU `c0720a2`, QEMU executable
SHA-256 `250efecc...770126`, snapshot `98a7ebe2...df8e41`, safe artifact
`3d548015...5743ac`, and trusted artifact `7983b82b...ee879`. Its explicit
control was:

```json
{"firmware-inline":{"mode":"exhaustive","trust":"user",
 "interrupts":"disabled","icount":"ignored"}}
```

It skips the complete 15-byte translated-entry prefix, uses one descriptor per
region, preserves residual exit indexes 0/1/3, and performs no member-descriptor
construction, callback replay, guest/artifact hashing, member recovery lookup,
or mapping invalidation. Normal MMIO remains executable because the skipped
prefix does not clear `can_do_io`. A real fault or recovery request inside a
trusted region terminates with exit 125 and is resolved after the run with
`scopt firmware-region recover`. Trusted mode also assumes the admitted captured
return contexts; safe mode retains the guard and fallback.

The diagnostic run loaded artifact id `188300033a6496d1`, appended zero
translations, and reproduced the normalized UART output. It activated 18 of 27
regions, instantiated 18 region descriptors and zero member descriptors, and
reported zero runtime hash validations, callback replays, and recovery lookups.
Region footprint fell from 102,100 to 67,844 bytes; copied native code fell from
71,064 to 63,524 bytes because trusted mode does not materialize safe return
guards.

Twelve fresh-process randomized paired blocks pinned to CPU 0 measured:

| Configuration | Median native time | Speed versus ordinary warm | Paired bootstrap 95% interval |
| --- | ---: | ---: | ---: |
| ordinary warm | 195.735 us | 1.000x | -- |
| safe firmware-inline | 264.939 us | 0.745x | 0.711-0.763x |
| trusted firmware-inline | 256.181 us | 0.762x | 0.755-0.790x |

Trusted versus safe firmware-inline is **1.038x** with interval
**[1.021, 1.092]**. This establishes that the removed bookkeeping was costly,
but it was not the root cause of the whole regression.  This was the observation
that motivated the causal ablations below.  The raw samples and failed
intermediate revisions are in
`fixtures/firmware-inline/trusted-measurement.json`.

## Regression root cause and corrected result

The trusted loader originally found a private region by scanning all 27 region
records.  Each candidate comparison called `sc7_source_index()`, which itself
linearly scanned all 163 public cache records.  In other words, a runtime entry
lookup accidentally nested the region scan with the cache-record scan.  An
legacy ordinary-cache path never entered `sc7_try_region()` and therefore never
paid this cost; it performs its normal key lookup and installs ordinary QEMU chains.

The first 12-block causal campaign used QEMU executable SHA-256
`0a1c895e...ecf5` and the same trusted v7 artifact in every trusted arm.  Each
block randomized ten fresh processes on CPU 0.  Diagnostic counters were kept
out of scored launches.

| Runtime arm | Median native time | Change versus trusted-current |
| --- | ---: | ---: |
| ordinary warm | 202.133 us | -- |
| trusted-current (linear source lookup) | 322.182 us | 1.000x |
| source index only | 177.363 us | **1.822x** `[1.796, 1.829]` |
| trusted-TB hash set only | 328.277 us | 0.980x `[0.970, 0.994]` |
| omit private tree insertion only | 321.647 us | 1.002x `[0.969, 1.011]` |
| allow incoming public chain only | 314.000 us | 1.023x `[1.007, 1.033]` |
| all four changes | 172.734 us | 1.860x `[1.854, 1.872]` |

Disabling all chaining on the ordinary cache cost only about 4% (0.960x warm,
`[0.937, 0.971]`), an upper bound on the private-region chaining loss.  Loading
the trusted v7 container while disabling regions measured 205.931 us, near the
202.133 us warm control.  Neither effect can explain the original 322.182 us
time.  The source index alone can: it changed 3,039 region comparisons to 28 in
the diagnostic run while preserving the same 18 activated regions and 157
admitted internal edges.

The fix constructs `source_region[public_source_index]` once while loading a
trusted artifact and retains the first region for a source, matching the old
first-match behavior.  `linear-source-scan` remains only as a reproduction
ablation.  A fresh matching capture and artifact on corrected QEMU
`2de43765afb5`, executable SHA-256 `97880d47...ee68d`, produced this independent
12-block confirmation:

| Configuration | Median native time | Paired result |
| --- | ---: | ---: |
| ordinary warm | 197.760 us | 1.000x |
| corrected trusted firmware-inline | **170.947 us** | **1.156x** `[1.132, 1.185]` versus warm |
| old linear lookup ablation | 262.956 us | 0.757x `[0.741, 0.765]` versus warm |
| trusted container, regions disabled | 202.749 us | 0.976x `[0.963, 1.011]` versus warm |

Corrected firmware-inline is also 1.177x `[1.163, 1.196]` faster than the same
trusted container with regions disabled.  Its diagnostic launch made 126 region
lookup attempts and only 28 indexed comparisons; the linear reproduction made
the same 126 attempts but 2,978 outer region comparisons.  Both executed 26
region entries, activated 16 regions inside the measured window, admitted 157
internal edges, reproduced normalized UART output, and appended zero
translations.  This isolates the prior regression to the lookup implementation
rather than changed firmware behavior or inactive inlining.

## Matched uncached FastDyn comparison

A separate 30-pair campaign compared the corrected trusted artifact with **uncached
FastDyn**, using the same pinned QEMU, FastDyn plugin, firmware, device configuration,
virtual benchmark markers and fixed boot workload.  The harness derived the uncached
configuration by removing only `[Machine].supercache`; each pair used fresh processes
in randomized order on CPU 0.  All 60 launches were valid, every trusted replay passed
the v7 artifact proof and appended zero translations, and guest output was unchanged.

| Configuration | BENCH median | Full-process median |
| --- | ---: | ---: |
| uncached FastDyn | 1,121.8 us | 0.2767 s |
| corrected trusted firmware-inline | 178.8 us | 0.2956 s |

The trusted cache ran the measured guest window **6.2874x faster** than uncached
FastDyn, with paired bootstrap 95% interval **[6.191, 6.334]** and wins in all 30
pairs.  The full FastDyn-plus-QEMU process did not improve: its paired speedup was
**0.9530x** `[0.934, 0.976]`, because this approximately 179 us boot window is dwarfed
by frontend, deployment and process-start overhead.  Child CPU time was 0.9892x
`[0.975, 1.004]`, not a supported difference.  These figures were recaptured after
the loader switched to canonical v7-only publication (QEMU sha256
`8d130b5b32fa116f099ca428a5dcc76b8ef9592c5f557e28934aa03e97844eac`).

Against the canonical ordinary warm cache, a second 30-pair campaign measured
201.6 us versus 175.1 us: **1.1540x** `[1.147, 1.163]` for trusted firmware-inline.
Its full-process speedup was 0.9933x `[0.971, 1.023]`, an unsupported difference.

The paired campaign is reproduced with the ordinary timing harness; `CONFIG` is the
matching capture's FastDyn TOML and `TRUSTED_ARTIFACT` is the published artifact
directory, not a bare cache file:

```sh
QEMU_SHA=$(sha256sum "$QEMU" | cut -d' ' -f1)
taskset -c 0 env SCOPT_V7_TRUSTED_EXES="$QEMU_SHA" \
  PYTHONPATH=tools/scopt python3 -m scopt run -c "$CONFIG" \
    --arms nocache,trusted --artifact trusted="$TRUSTED_ARTIFACT" \
    --reference nocache --set fixed --qemu "$QEMU" \
    --reps 30 --seed 98914 --out "$WORK/nocache-vs-trusted-30"
```

This is the capability-matched baseline for attributing the optimization.  It is **not
an upstream stock-QEMU comparison**.  Upstream QEMU cannot load the v7 artifact and
does not share FastDyn's plugin, virtual markers, in-window argument modifier or device
configuration.  The existing Trebuchet stock runner also targets a different compute
workload and marker symbols.  A future upstream-QEMU arm may provide contextual
performance, but it must be labelled separately and cannot establish the incremental
firmware-inline effect.

The canonical-v7 raw BENCH samples and identities are in
`fixtures/firmware-inline/canonical-v7-measurement.json`; the earlier progression is
in `measurement.json`, and regression ablations are in `regression-ablations.json`.

## Reproduction skeleton

```sh
QEMU=$PWD/third_party/common/qemu/build/qemu-system-arm
QEMU_SHA=$(sha256sum "$QEMU" | cut -d' ' -f1)
PYTHONPATH=tools/scopt python3 -m scopt prepare -c "$CONFIG" \
  --out "$WORK/prep" --qemu "$QEMU" --json
# Generate bundle/runtime identity, import with the exact firmware ELF, and
# produce EnvLayout for this exact QEMU as described in docs/getting-started.md.
SCOPT_ENVLAYOUT_PATH="$WORK/envlayouts" PYTHONPATH=tools/scopt \
  python3 -m scopt optimize "$SNAPSHOT" --pipeline firmware-inline \
    --prep "$WORK/prep" --elf "$FIRMWARE" --runtime-identity "$RUNTIME" \
    --v7-exe "$QEMU_SHA" --qemu "$QEMU" --warm-baseline "$SNAPSHOT" \
    --store "$WORK/store" --json
PYTHONPATH=tools/scopt python3 -m scopt rundir launch "$RUN_DIR" \
  --v7-stats --json
taskset -c 0 env PYTHONPATH=tools/scopt \
  python3 tools/scopt/scripts/measure_firmware_inline.py \
    --config "$CONFIG" --qemu "$QEMU" --warm "$SNAPSHOT" \
    --current "$CURRENT_ARTIFACT" --firmware-inline "$V7_ARTIFACT" \
    --out "$WORK/timing" --pairs 12 --seed 98912
```

## Remaining limitations

The current policy is exhaustive only; it does not yet provide lazy descriptor
construction or a conditional selection policy.  The real-application result
is boot-only, not steady-state.  Structural loader mutation checks, local semantic
fixtures, export/reimport identity, exact output replay, and code-dependency
tracking are covered; destructive runtime tests for every MMIO, interrupt,
fault, callback, and invalidation combination remain bounded rather than a
proof over all firmware behavior.
