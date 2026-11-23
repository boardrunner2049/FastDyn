# Configuration 7: sealed WHOPR

Configuration 7 applies a WHOPR-style whole-program pipeline to a trusted,
sealed SuperCache.  It does not modify the firmware ELF and does not infer
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

`pipelines/sealed-whopr-7.toml` enables `firmware-inline` with an explicit
unbounded exhaustive policy, trusted no-recovery contract, four partitions,
and four LTRANS workers.  Partition count distributes work; it never limits
call count, hotness, depth, region size, or total growth.

Exact-PC import validates the recorded PC instead of pinning every non-PCREL
TB.  The optimizer models QEMU's newest-record selection for duplicate exact
keys.  Unique one-instruction exhaustive records remain valid public fallback
entries but are not optimization roots.  An internal edge may target only the
entry of the selected captured TB, never an interior instruction.

The real compute cache contained 38,252 public records.  Configuration 7
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

## Result

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

For continuity, the seven-number dashboard is below.  Rows 1-6 retain their
previous published medians; row 7 is this campaign.  Because row 6's retained
absolute value came from an earlier block, the fresh paired 6-to-7 comparison
above—not subtraction between cross-campaign medians—is the attribution test.

| # | configuration | firmware-window median |
| ---: | --- | ---: |
| 1 | stock QEMU, O0 | 1102.650 ms |
| 2 | FastDyn, O0 | 1334.988 ms |
| 3 | FastDyn + FirmBOLT | 1319.501 ms |
| 4 | ordinary warm cache | 1242.790 ms |
| 5 | normal scopt | 1100.832 ms |
| 6 | trusted sealed cache | 1042.037 ms |
| 7 | trusted sealed WHOPR | 1063.933 ms |

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
