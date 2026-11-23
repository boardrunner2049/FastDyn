# Configuration 7: sealed WHOPR

Configuration 7 first applies an unbounded trusted QEMU-helper graph transplant,
then applies a WHOPR-style whole-program firmware-region pipeline to the resulting
sealed SuperCache.  It does not modify the firmware ELF and does not infer guest
control flow from exhaustive coverage alone.

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

Six randomized paired blocks compared the exact artifact above with the matched trusted
sealed baseline. Cache production, deployment, Python startup, and the 569.08-second
compile were outside the firmware timing window. Both arms used the same populated input
RAM. All 16 reported registers matched in every run, and neither cache hash changed.

| measurement | trusted sealed baseline | all-helper configuration 7 |
| --- | ---: | ---: |
| median native firmware window | 1132.067 ms | 1238.646 ms |
| cache SHA-256 | `c6d2c171...ea2fd49` | `097e4103...c33351f` |

The median paired baseline/configuration-7 ratio was **0.914279x**, with a paired
bootstrap 95% interval of **0.876645-0.945899x**. Thus the exhaustive all-helper
artifact is a supported **measured regression**, about 9.4% slower than its matched
baseline; it must not be selected for this workload. The raw block values are in
[`sealed-whopr-7-current-results.json`](sealed-whopr-7-current-results.json).

The isolated helper-only ablation establishes the primary cause. After explicit
warmups, its median was 1518.736 ms versus 1105.460 ms for the matched baseline;
the median paired baseline/helper ratio was **0.720162x** with interval
**0.700361-0.838369x**. The helper stage alone therefore takes about 38.9% longer
when the paired ratio is inverted. It contains no firmware regions, so region
activation and lookup cannot cause that isolated loss.

This “inlining” copied 1,179,601,718 bytes at 21,977 sites (1,179,469,856 bytes of
net growth), with a median **55,934 copied bytes per site**. The public native code
grew from 5,144,523 bytes to 1,184,614,379 bytes. MMU graphs account for 90.6% of
the copied bytes: `helper_ldul_mmu` alone was cloned at 11,314 sites and
`helper_stl_mmu` at 5,566 sites. These are complete transitive slow/fault/device
graphs, not small leaf bodies, and no context simplification removes their unused
paths after copying.

The entry lowering also does not preserve the original hardware call/return pair.
For an inspected MMU site, the original six-byte `call qword ptr [rip+0xb]` became
`lea r11,[rip+continuation]; push r11; <55 KiB copied graph>`. The copied root still
returns with `ret`, but the synthetic `push` does not enter a return address in the
x86 return-stack predictor as a `call` would. Out-of-arena RIP references and external
edges additionally use register-save/mov/jump thunks. Hardware cache/TLB/branch-miss
counters were unavailable on this host, so their individual shares are not claimed;
the measured helper-only ablation and 230x executable expansion establish the policy
failure without that attribution.

Firmware-region activation is a secondary cost in the combined artifact, not the
primary cause. A same-artifact eight-pair diagnostic compared the default runtime with
`QEMU_SC_V7_EXPERIMENT=no-regions`: no-regions had a 1221.744 ms median versus
1293.337 ms default, with paired no-regions/default ratio **0.935331** and interval
**0.832489-0.965306**. All register transcripts matched. Diagnostic counters showed
54 region activations overall and three inside the benchmark window. Thus the active
regions add about 6.9% here, while the much larger independently measured loss already
exists in the helper-only artifact.

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
| 7 | trusted sealed WHOPR, all captured QEMU helpers | **1238.646 ms** |

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
  --pairs 6 --cpu 2 --out "$TIMING"
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
**1.032092x** improvement with interval **1.009597-1.065389x**.  This proves
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
  acceptable fault behavior.  Sealed entry interrupt polling remains enabled.
- LTRANS uses Python threads; region construction releases no explicit GIL, so
  the current benefit is work isolation and deterministic scheduling more than
  guaranteed CPU-parallel speedup.  Process workers are a future setup-time
  optimization and do not affect the measured native result.
- The unbounded helper prefix can be extremely large. This is an explicit policy,
  not permission to silently truncate it: resource or representation exhaustion
  makes configuration 7 incomplete and leaves pending/refused sites in the receipt.
