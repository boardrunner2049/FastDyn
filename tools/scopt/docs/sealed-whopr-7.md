# Configuration 7: trusted sealed PGO

The `sealed-pgo-7` pipeline collects fixed-input training data and applies two
passes. The native pass selects helper instructions inside observed guest cycles
by their actual call counts. The firmware pass embeds verified guest call edges in
the same cycles and, recursively, nested calls whose marginal profit is positive;
each planned region is admitted only when the training-profile
[profit model](firmware-inline-policies.md#profit-model) predicts a net gain.
Both use a tunable minimum of 1,000 observed calls. Training keeps the
`steady` phase when the launch (its phases or its `benchmark_start` virtual) or
kernel has a work-start marker, and fails if the replay never reaches it
(`--pgo-phase`, see [passes](passes.md#firmware-inline1)). The combined compiler preserves the original training
phase and checks the guest unit identities and transfers across the helper
splice. A helper count of zero at a frequently entered TB is a valid result.

```sh
scopt optimize "$SNAP" --pipeline sealed-pgo-7 --pgo-launch "$TRAIN/launch.json" \
  --elf "$ELF" --runtime-identity "$RUNTIME_JSON" --v7-trusted-exe "$QEMU_SHA256_ID" \
  --store "$OUT" --json
QEMU_SC_SEALED=1 QEMU_SC_TRUSTED=1 "$DEPLOYED/run.sh"
```

The direct launcher must select the emitted cache. The trusted region ABI
requires icount off and an execution contract that permits omitted internal
interrupt polls and live recovery. Set `QEMU_SC_V7_STATS` to an absolute path
in a diagnostic replay for the loaded region ID, activation counts and
`trusted_residual_exits` (region exits that went through the dispatcher).
`trusted_tb_calls` counts trusted-membership lookups, not exits; see
[v7 stats counters](qemu.md#v7-dispatcher-return-counters).

The remainder of this document describes the historical exhaustive graph
experiment, still available as `sealed-whopr-7` and the Trebuchet recipe
`whopr7`. Choose `pgo7` in current Trebuchet candidate plans.
`sealed-whopr-7-bounded` keeps `helper_lookup_tb_ptr` out of line; it removes
`sealed-whopr-7`'s regression but is not shown to beat `sealed-pgo-7`
([bounded helper graphs](#bounded-helper-graphs-sealed-whopr-7-bounded)).

The separate `sealed-firmlooper` pipeline uses task registration and static loop
structure to select guest calls. See [Firmware inlining policies](firmware-inline-policies.md).

## Mapping from GCC WHOPR

The implementation follows GCC's three-stage structure, adapted to captured
QEMU translations rather than GIMPLE:

| GCC mechanism | scopt configuration 7 |
| --- | --- |
| LGEN local summaries | Per-TB source identity, native size, guest instruction count, and verified firmware-call edges. |
| WPA global decisions | Exact-key call graph, Tarjan SCCs, recursive-edge retention, deterministic balanced partitions, and inline-context lineage. |
| LTRANS parallel transforms | Independent call-site region construction with a shared immutable compatibility index, followed by deterministic admission and link. |
| Virtual clones | A call-site plus inline context names a private copy before native bytes are materialized. |

GCC's WHOPR documentation describes the LGEN/WPA/LTRANS separation and virtual
clones.  Its current balanced partitioner linearizes the call graph, keeps
strongly connected material together, aims for roughly equal partitions, and
searches a bounded size neighborhood for lower-cost cuts.  Configuration 7
adopts those orchestration ideas, not GCC's GIMPLE summaries, profitability
model, or source-language assumptions:

- <https://gcc.gnu.org/onlinedocs/gcc-15.2.0/gccint/WHOPR.html>
- <https://raw.githubusercontent.com/gcc-mirror/gcc/master/gcc/lto/lto-partition.cc>

## Policy and implementation

`pipelines/sealed-whopr-7.toml` has two ordered stages:

1. `host-inline` with `mode="maximal", trusted_all_helpers=true`. Every captured
   QEMU helper site is considered, including cold and unknown-profile sites. The
   resolver follows exact, same-image direct function edges to a fixed point.
   Each source function is copied once per call site, so recursive SCCs remain
   recursive calls and do not require an integer depth limit. There is no helper,
   instruction, site, per-TB, total-growth, or pass-wall-time profitability bound.
2. `firmware-inline` with an explicit unbounded exhaustive policy, trusted
   no-recovery contract, four partitions, and four LTRANS workers. Partition
   count distributes work; it never limits call count, hotness, depth, region
   size, or total growth.

The helper transplant replaces the outer TCG helper `CALL` with a flag-neutral
return-address setup and the copied root. Direct edges between copied functions
are retargeted internally. Direct edges outside the exact QEMU image and indirect
calls/jumps remain external and are counted as residual transfers; they are not
misreported as inlined. The trusted contract excludes CET shadow-stack enforcement,
asynchronous unwind, and live fault recovery inside these private copies.

## Assurance and command contract

Configuration 7 is intentionally **user-vouched**. `trusted_all_helpers=true` asks the
compiler to transplant the exact captured QEMU helper graph without the ordinary LLVM
bridge and per-helper certification gate. The compile receipt records
`assurance="compiler-produced/user-vouched"` and `audited=false`; publication does not
turn that trust assertion into a proof.

The command boundaries are:

| command | what it establishes | what it does not establish |
| --- | --- | --- |
| `scopt optimize` | Pass execution plus linker-internal format, bounds, overlap, branch-range, relocation, continuation, and emitted-byte readback checks. | Reimported source correspondence, recovery/provenance proof, symbolic equivalence, or runtime behavior. |
| `scopt load` | Minimal canonical-v7 loadability and optional runtime-header compatibility. | Transformation correctness, helper provenance, or an audited status. |
| `scopt audit` | Optional expensive static proof work; `--dynamic` adds held-out execution and oracle checks. | A universal proof beyond the declared contract and supplied evidence. |

`load` and `audit` are therefore separate gates, not aliases for publication. A loadable
configuration-7 artifact remains user-vouched until the requested audit completes, and
an audit may refuse if required proof inputs or support for the artifact shape are
missing. Timing evidence must additionally bind the exact artifact loaded by the
runtime.

“Unbounded” means there is no configured depth, helper, instruction, site, growth,
fixed-point, or pass-wall-time profitability limit. It does not promise infinite host
resources. Address-space, memory, encoding, or representation exhaustion must be
reported as pending/refused work; silently truncating the graph is not a complete
configuration-7 run.

### Current compiler activation result

On the 38,252-unit compute capture, the exhaustive compiler run discovered 22,025
native call sites. It embedded all 21,977 QEMU-helper sites from 25 distinct finite
direct-call graphs; the 48 residual sites were FastDyn plugin callbacks and are outside
the QEMU-helper policy. Native growth was 1,179,469,856 bytes. The two-stage
compile/link/publish completed in 569.08 seconds with 15.35 GiB peak RSS. This is setup
cost, not a runtime speed result. The final cache passed `scopt load` with 38,252 units,
`max_end=1199284522`, and no loader problems. Its assurance remains
`compiler-produced/user-vouched`, `audited=false`.

The trusted native stage emitted no derived capture and its receipt reports zero
pending QEMU sites. The later firmware-region stage emitted 62 regions and separately
reported 198 pending/refused firmware-call opportunities. Those firmware-region
refusals do not reduce the complete QEMU-helper count above.

### Current all-helper runtime result

Eight randomized paired blocks compared the exact artifact above with an independently
deployed copy of the same 38,252-record compiler input. Cache production, deployment,
Python startup, and the 569.08-second compile were outside the firmware timing window.
Both arms used the same populated input RAM. All 16 reported registers matched in every
run, both launchers were sealed, and neither cache hash changed.

| measurement | trusted sealed baseline | all-helper configuration 7 |
| --- | ---: | ---: |
| median native firmware window | 1057.798 ms | 1247.853 ms |
| cache SHA-256 | `6eb1d216...c41e769` | `097e4103...c33351f` |

The median paired baseline/configuration-7 ratio was **0.844469x**, with a paired
bootstrap 95% interval of **0.826329-0.876616x**. Thus the exhaustive all-helper
artifact is a supported **measured regression**, about 18.4% slower than its matched
baseline; it must not be selected for this workload. The raw block values are in
[`sealed-whopr-7-current-results.json`](sealed-whopr-7-current-results.json).

The earlier six-pair result reported 0.914279x. It is invalid: diagnostic execution had
appended the purported baseline from 38,252 to 39,757 records. It did not match the
compiler input and is retained only as an explicit invalidated result in the JSON.

The corrected isolated helper-only ablation establishes the primary cause. Its median
was 1251.724 ms versus 1057.768 ms for the exact matched baseline; the median paired
baseline/helper ratio was **0.841903x** with interval **0.825344-0.879657x**. The helper
stage alone therefore takes about 18.8% longer when the paired ratio is inverted. It
contains no firmware regions, and its result is essentially identical to the complete
configuration-7 result. The previously reported 38.9% helper loss was also confounded
by a cache that had been allowed to append and is superseded by this sealed comparison.

This “inlining” copied 1,179,601,718 bytes at 21,977 sites (1,179,469,856 bytes of
net growth), with a median **55,934 copied bytes per site**. The public native code
grew from 5,144,523 bytes to 1,184,614,379 bytes. MMU graphs account for 90.6% of
the copied bytes: `helper_ldul_mmu` alone was cloned at 11,314 sites and
`helper_stl_mmu` at 5,566 sites. These are complete transitive slow/fault/device
graphs, not small leaf bodies, and no context simplification removes their unused
paths after copying.

Size is not the same as executed cost. MMU helpers run only on a TLB miss or MMIO
access, so most of the 11,314 `helper_ldul_mmu` and 5,566 `helper_stl_mmu` copies are
unexecuted bloat: a hardware breakpoint counted 0 calls at a `helper_lduw_mmu` site
whose TB was entered about 28.6M times
([sweet-spot site counts](../experiments/helper-inline-sweetspot/README.md#original-call-site-counts)).
The copies that do execute are more plausibly `helper_lookup_tb_ptr` (1,750 sites,
median 61,598 B each, run on every `goto_ptr` exit) and the `v7m_msr`, `rebuild_hflags`
and exception helpers. This is not isolated: the shared-graph control below redirects
all 21,952 sites together.

The bottleneck was then isolated with controlled executable variants rather than
inferred from code size:

| controlled comparison | median paired ratio | 95% interval | result |
| --- | ---: | ---: | --- |
| Same 1.2-GB physical file, but original record table and shared-helper bodies, versus exact baseline | 0.958969x | 0.936743-1.002276x | Includes 1.0, borderline: the upper bound rests on one block and 6 of 8 blocks are below 1. QEMU maps cached code only up to `max_end` (`tcg/supercache.c`), 13,087,520 B here, so this tests on-disk allocation, not in-process memory. |
| Original shared QEMU helpers versus private transformed copies, with the helper artifact's TB layout retained | 0.884347x | 0.866058-0.936235x | Private transformed execution is 13.1% longer. |
| Executed-clone control: same large file with original records (footprint control) versus helper-exact (`paired8-executed-clone-control`) | 0.863282x | 0.830950-0.877555x | Executing the private copies, with the file size held fixed. |
| One shared transformed copy of each of 25 graphs versus 21,952 per-site private copies | 0.937317x | 0.882027-0.982124x | Per-site placement/locality adds approximately 6.7%. |
| Layout + lowering: shared transformed graphs versus exact original baseline (16,303 relocated TBs, 1.2 GB loaded) | 0.961978x | 0.939952-0.984046x | 4.0% from layout and lowering together; not a lowering-only effect. |
| Same-layout lowering control: original shared helpers versus shared transformed graphs, identical records (`paired8-lowering-control`) | 0.973415x | 0.838142-1.006662x | Includes 1.0; no supported lowering-only penalty. |

The components come from separate 8-pair campaigns with different baselines. They
are approximate and do not compose: 0.937 x 0.962 = 0.902 against the direct 0.884,
and 0.959 x 0.863 = 0.828 against the direct helper-only 0.842. The identical
helper-exact cache measured campaign medians from 1185.5 to 1251.7 ms within 11
minutes (5.6% drift), so components of 3-6% are at the size of campaign-level noise.
The conclusion stands: executing enormous per-site private helper copies is the
dominant cost. The split between placement, layout and lowering is not established,
and the controls do **not** support blaming the raw cache-file mapping itself. All
controls and their per-block ratios are in the
[results JSON](sealed-whopr-7-current-results.json) (`bottleneck_controls`).

For an inspected MMU site, the original six-byte `call qword ptr [rip+0xb]` became
`lea r11,[rip+continuation]; push r11; <55 KiB copied graph>`. A partial control changed
8,399 eligible sites back to a real call while preserving their continuation. Its
control/original ratio was 1.041364x with interval 0.971228-1.065424x, so it established
no improvement. Return-stack prediction is therefore **not** claimed as the cause.
Out-of-arena references and external edges use additional thunks, but hardware event
counters were unavailable (`perf_event_paranoid=4` and no matching kernel `perf`). The
causal executable controls above establish the regression without inventing cache-miss
counts.

The corrected exact helper-only and final results have overlapping intervals and nearly
identical point estimates. Consequently this campaign establishes no incremental
firmware-region loss in the complete artifact. The same-artifact `no-regions`
diagnostic of this artifact (`paired8-full-vs-no-regions-diag`) was not run through
`measure_sealed_whopr.py` and has no seal or cache-hash guarantee. It showed
no-regions/default 0.935 with 7 of 8 blocks below 1, which runs contrary to the
conclusion above, but it is inadmissible and remains unreconciled. It is distinct from
the historical 169-region same-artifact ablation below.

All configuration 7 numbers in this document predate chainable trusted residual exits.
Their artifacts use the legacy shared descriptor, so every residual exit of a region
(about 2.9M per packet run, nearly all from the never-expanded root continuation)
returned to the dispatcher. Artifacts linked since then carry per-member descriptors
whose residual goto_tb exits QEMU chains after first use; compare the two behaviours
on the same bytes with `QEMU_SC_V7_EXPERIMENT=no-trusted-exit-chain`.

The current seven-number dashboard retains rows 1-6 from their original campaigns and
uses the new valid all-helper measurement for row 7. Cross-campaign subtraction is not
an attribution test; the fresh paired comparison above is authoritative for rows 6-7.

| # | configuration | firmware-window median |
| ---: | --- | ---: |
| 1 | stock QEMU, O0 | 1102.650 ms |
| 2 | FastDyn, O0 | 1334.988 ms |
| 3 | FastDyn + FirmBOLT | 1319.501 ms |
| 4 | ordinary warm cache | 1242.790 ms |
| 5 | normal scopt | 1100.832 ms |
| 6 | trusted sealed cache | 1042.037 ms |
| 7 | trusted sealed WHOPR, all captured QEMU helpers | **1247.853 ms** |

Exact-PC import validates the recorded PC instead of pinning every non-PCREL
TB.  The optimizer models QEMU's newest-record selection for duplicate exact
keys.  Unique one-instruction exhaustive records remain valid public fallback
entries but are not optimization roots.  An internal edge may target only the
entry of the selected captured TB, never an interior instruction.

The real compute cache contained 38,252 public records.  The original
firmware-region-only configuration 7
discovered and emitted 169 call-site regions containing 3,494 copied members,
3,994 internal transfers and 1,071,165 private bytes.  It reached acyclic
depth four.  Three calls retained unpatchable native exits; recursive/missing
evidence remains residual rather than being guessed.

## Bounded helper graphs: `sealed-whopr-7-bounded`

`sealed-whopr-7-bounded` is `sealed-whopr-7` with one change: host-inline sets
`trusted_max_source_graph_bytes = 32768`. `sealed-whopr-7-bounded-b3` is
`sealed-whopr-7-b3` with the same change. `sealed-whopr-7` and `sealed-whopr-7-b3` stay
unchanged so archived provenance remains reproducible.

**Why.** Round 2 of the sealed follow-ups (2026-11-25, `/data/scfix-r2/TIMING.md`) timed
`sealed-whopr-7` on the FirmBOLT `-O4` packet, UART and CLI captures on QEMU `aacca05`.
It is a regression: time(configuration 6) / time(`sealed-whopr-7`) is 0.8513 (packet),
0.6921 (UART) and 0.9109 (CLI), and time(`sealed-pgo-7`) / time(`sealed-whopr-7`) is
0.7448, 0.6278 and 0.7756, all below the A/A floor. Its unbounded prefix inlines
`helper_lookup_tb_ptr`, whose captured source graph is 50,345 B, at 1,747, 1,980 and
2,184 sites. The copies also remove the lookup tails that firmware-inline needs, so
760, 365 and 486 roots are refused as `lookup_tail_missing`.

**Why 32768.** The bound charges a helper's whole captured source graph, the root and
its direct-call graph ([host-inline](passes.md#host-inline1)). On these captures 32768
excludes `helper_lookup_tb_ptr` and nothing else: the next largest graph is
`helper_v7m_msr` at 6,500 B. 65536 is above 50,345 B and is a no-op: its packet and UART
caches are byte-identical to `sealed-whopr-7`'s. The bound is on bytes, not helper
names, so on another QEMU build or capture the receipt's `helper_graph_size_policy`
refusals show which helpers it excludes.

What the bound changes (`sealed-whopr-7` -> `sealed-whopr-7-bounded`):

| | packet | UART | CLI |
|---|---:|---:|---:|
| `helper_lookup_tb_ptr` sites inlined | 1,747 -> 0 | 1,980 -> 0 | 2,184 -> 0 |
| host-inline growth | 124.1 -> 1.7 MB | 142.4 -> 3.7 MB | 156.6 -> 3.5 MB |
| `lookup_tail_missing` refusals | 760 -> 0 | 365 -> 0 | 486 -> 0 |
| firmware regions | 30 -> 193 | 27 -> 202 | 25 -> 208 |
| allocated cache | 192.5 -> 43.4 MB | 171.6 -> 26.7 MB | 204.9 -> 32.4 MB |

**Measured.** Round 2 timed this policy as the arm `w7_nolookup`, built from a private
TOML with the same passes and parameters under another name. On packet, a rebuild from
the same snapshot with `--pipeline sealed-whopr-7-bounded` gives a `cache.sc` that is
byte-identical (`cmp`) to the timed one; only the artifact id differs. Paired,
block-randomised fresh-process blocks: 20 on packet, 10 on UART and CLI. Ratios are
time(reference) /
time(`sealed-whopr-7-bounded`), so above 1 means the bounded pipeline is faster; each
cell gives the block-ratio median, its 95% bootstrap interval, the blocks above 1 and
the verdict against the workload's A/A band (packet [0.9501, 1.0526], UART
[0.9700, 1.0309], CLI [0.9802, 1.0202]).

| Reference | packet | UART | CLI |
|---|---|---|---|
| `sealed-whopr-7` | 1.2979 [1.2593, 1.5067], 20/20, above floor | 1.4432 [1.1959, 1.5167], 10/10, above floor | 1.3254 [1.2947, 1.4188], 10/10, above floor |
| `sealed-pgo-7` | 0.9845 [0.9538, 1.0036], 8/20, within noise | 0.9163 [0.8678, 0.9413], 0/10, below floor | 1.0513 [1.0089, 1.0941], 9/10, within noise |
| configuration 6 | 1.0934 [1.0615, 1.1109], 19/20, above floor\* | 0.9910 [0.9395, 1.0289], 4/10, within noise | 1.2147 [1.1863, 1.2647], 10/10, above floor |

\* Packet's A/A interval excludes 1 (configuration 6 against a byte-identical copy of
its own cache: 1.0322 [1.0035, 1.0526]) because the configuration 6 reference ran slow,
which inflates every configuration-6-referenced packet ratio. Against its byte-identical
twins the bounded pipeline is 1.0635 (`sealed-firmlooper-v2`, which selects nothing on
packet) and 1.0529 (the A/A copy), both within noise.

The bound removes the regression against configuration 6 (within noise or above the
floor on all three workloads), but the bounded pipeline is not shown to beat
`sealed-pgo-7`: it is within noise of it on packet and CLI, and about 8% slower on UART
(below the floor). On CLI its point estimate is 5% faster and the interval excludes 1,
but the interval overlaps the A/A band, so neither parity nor a gain is shown.
`sealed-pgo-7` (Trebuchet `pgo7`) stays the configuration 7 to choose.

Not established:

- The bound changes code size and region coverage together (6-8 times as many regions),
  and round 2 cannot separate the two. `sealed-whopr-7` is slower than configuration 6,
  which has no regions at all, so losing regions cannot by itself explain its
  regression; that the inlined lookup copies cost time on their own is an inference.
  Code size was not separated from page-fault cost.
- `sealed-whopr-7-bounded-b3` has not been built or timed. Build it with
  `--v7-member-skip-exe` for a QEMU that honours member skip flags: round 2 built
  `sealed-whopr-7-b3` without it, so 6 of 30 (packet), 1 of 27 (UART) and 5 of 25 (CLI)
  of its regions kept the shared descriptor.

```sh
scopt optimize "$SNAP" --pipeline sealed-whopr-7-bounded --elf "$ELF" \
  --runtime-identity "$RUNTIME_JSON" --v7-trusted-exe "$QEMU_SHA256_ID" \
  --store "$OUT" --json
```

## Reproduction

The artifact was built with:

```sh
SNAP=$(find "$IMPORT/store/snapshots" -mindepth 1 -maxdepth 1 -type d | head -1)
QEMU_SHA=$(sha256sum third_party/common/qemu/build/qemu-system-arm | cut -d' ' -f1)
PYTHONPATH=tools/scopt scopt-env/bin/scopt optimize "$SNAP" \
  --pipeline sealed-whopr-7 --store "$OUT" --b3 \
  --runtime-identity runtime-identity.json --v7-trusted-exe "$QEMU_SHA" \
  --prep "$SEALED_PREP" --elf RTOSDemo.out --json
```

`--pipeline sealed-whopr-7-b3` composes the same regions with B3 in one artifact. It also
needs a sealed steady `--profile` and `--enable specialize-returns --enable chain-returns
--enable chain-static`; see [B3 passes](b3-passes.md#sealed-firmware-regions--b3-configuration-7--b3).

Check the exact published artifact before deployment. The first command is the minimal
loader/runtime gate; the second is the optional proof path and can be run separately:

```sh
PYTHONPATH=tools/scopt scopt-env/bin/scopt load "$ARTIFACT" \
  --runtime-identity runtime-identity.json --json
PYTHONPATH=tools/scopt scopt-env/bin/scopt audit "$ARTIFACT" \
  --snapshot "$SNAP" --json
```

For held-out behavior evidence, use `scopt audit "$ARTIFACT" --dynamic` with the
matching configuration, inputs, baseline, and output directory. Do not interpret a
successful `load` as a successful static or dynamic audit.

Deploy and compile the launcher before timing:

```sh
PYTHONPATH=tools/scopt scopt-env/bin/scopt deploy "$ARTIFACT" \
  -c "$SEALED_PREP/run.toml" --run-dir "$DEPLOY" --qemu "$QEMU" --json
fastdyn-env/bin/fastdyn compile-launch -c "$DEPLOY/run.toml" \
  -o "$DIRECT" --no-build-fmu --preserve-memory
PYTHONPATH=tools/scopt scopt-env/bin/python \
  tools/scopt/scripts/measure_sealed_whopr.py \
  --baseline "$SEALED_BASELINE/run.sh" --candidate "$DIRECT/run.sh" \
  --pairs 8 --cpu 2 --out "$TIMING"
```

The firmware input RAM must be the original immutable input in both compiled
launchers.  A deploy oracle may consume an input file, and `--reset-memory`
creates zero-filled memory rather than restoring a populated input; the
measurement driver rejects output changes but cannot reconstruct a missing
input.

## Historical firmware-region-only result

The numbers below predate the unbounded helper-graph prefix. They remain the
baseline for the firmware WHOPR mechanism but are not performance evidence for
the current two-stage configuration 7. A current result must show the helper
receipt and loaded artifact identity before it replaces this table.

All twelve scored runs reproduced the same sixteen register values and left
both caches unchanged.  Only the firmware `[BENCH][fw]` window is reported.

| block | trusted sealed baseline | configuration 7 | baseline / config 7 |
| ---: | ---: | ---: | ---: |
| 1 | 1.106849318 s | 1.074213552 s | 1.030381x |
| 2 | 1.059691856 s | 1.039873897 s | 1.019058x |
| 3 | 1.110859679 s | 1.090809514 s | 1.018381x |
| 4 | 1.056277517 s | 1.053651909 s | 1.002492x |
| 5 | 1.117485638 s | 1.086958246 s | 1.028085x |
| 6 | 1.056302613 s | 1.049350256 s | 1.006625x |
| median | **1.083270587 s** | **1.063932731 s** | **1.018720x** |

The paired bootstrap 95% interval is **1.004559-1.029233x**.  A second six-pair
campaign compared the same artifact with regions enabled and disabled.  The
enabled region median was 1.056296 s versus 1.106924 s disabled, a paired
**1.032092x** improvement with interval **1.009597-1.065389x** (its archive keeps
the medians and interval only, no per-block rows).  This proves
the embedded regions execute and remove enough dispatch to help; container
lookup/activation overhead consumes part of the isolated gain in the net
baseline comparison.

For historical continuity, the old seven-number dashboard is below. Row 7 in this
table is the firmware-region-only predecessor, not the current all-helper artifact.

| # | configuration | firmware-window median |
| ---: | --- | ---: |
| 1 | stock QEMU, O0 | 1102.650 ms |
| 2 | FastDyn, O0 | 1334.988 ms |
| 3 | FastDyn + FirmBOLT | 1319.501 ms |
| 4 | ordinary warm cache | 1242.790 ms |
| 5 | normal scopt | 1100.832 ms |
| 6 | trusted sealed cache | 1042.037 ms |
| 7 | trusted sealed WHOPR, firmware regions only | 1063.933 ms |

## Limits

- This is one real FreeRTOS compute image and one fixed meaningful input.  It
  establishes an application result, not a universal policy.
- Public exhaustive translations remain available for missing contexts and
  residual exits.  Configuration 7 does not make indirect targets known.
- The trusted contract intentionally omits guest-code hash checks, live QEMU
  fault recovery, and icount.  The user is responsible for immutable code and
  acceptable fault behavior.  Public sealed TBs keep their entry interrupt poll.
  Trusted firmware regions are entered at +15, past the poll, and their internal
  edges skip it too, so an interrupt pending inside a region is taken only when
  the region exits.
- LTRANS uses Python threads; region construction releases no explicit GIL, so
  the current benefit is work isolation and deterministic scheduling more than
  guaranteed CPU-parallel speedup.  Process workers are a future setup-time
  optimization and do not affect the measured native result.
- The unbounded helper prefix can be extremely large. This is an explicit policy,
  not permission to silently truncate it: resource or representation exhaustion
  makes configuration 7 incomplete and leaves pending/refused sites in the receipt.
