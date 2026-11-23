# scopt pass catalog

This page describes every canonical pass registered by the current scopt build. It is
the review index for what a pass changes, what it needs, how to select it, and what
the repository's measurements do (and do not) establish. The registry is authoritative;
the test `tests/unit/test_pass_catalog.py` makes this page fail when a registered pass
is missing.

`host-inline-splice` and `host-inline-extreme` are compatibility pipeline names and
modes of `host-inline`. They are not additional passes. Likewise, "default enabled"
below means that a pass runs when it occurs in the selected pipeline; it does not mean
that every pipeline contains the pass.

## Running one pass

The general form is:

```sh
scopt optimize "$SNAP" --pipeline PIPELINE --only PASS \
  --profile "$PROFILE" --store "$OUT" --json
```

Seven experimental passes are disabled by default and still need `--enable PASS` when
used with `--only`:

```text
local-scalar-cleanup     retire-ordinary-exits   eliminate-state-sync
specialize-returns       chain-returns           specialize-edges
chain-static
```

For example:

```sh
scopt optimize "$SNAP" --pipeline dominant-return \
  --only chain-returns --enable chain-returns \
  --profile "$PROFILE" --store "$OUT" --json
```

Pass parameters are a JSON object keyed by the canonical pass name. Pipeline TOML
supplies the starting values; the JSON values override them:

```json
{
  "page-aware-local-layout": {"max_rounds": 4, "max_swaps": 64}
}
```

Pass that file as `--params params.json`. Always inspect the JSON receipt: requested,
enabled, executed, affected units, reason, statistics, and final artifact hash are the
activation evidence. A published candidate is not by itself evidence of a speedup.

## Pass summary

| Pass | Pipeline example | Default | Main effect |
|---|---|---:|---|
| `align-trace-pages` | `b2-arena-slide` | on | Slide the captured arena to reduce modeled page crossings. |
| `branch-folding` | `b3` | on | Replace a proved conditional branch with a same-size direct form. |
| `chain-returns` | `dominant-return` | opt-in | Chain profiled return exits while retaining ordinary fallback. |
| `chain-static` | `b3` | opt-in | Chain profiled static exits while retaining ordinary fallback. |
| `cluster-hot` | `b2-layout` | on | Pettis-Hansen ordering of hot translation blocks. |
| `constant-propagation` | `b3` | on | Fold proved host constants inside private regions. |
| `dead-host-ops` | `b3` | on | NOP host operations proved dead. |
| `eliminate-state-sync` | `b4` | opt-in | Remove or coalesce redundant guest-state synchronization. |
| `fold-exit-pc-update` | `exit-pc-fold` | on | Fuse a proved ARM-PC update at a translation-block exit. |
| `firmware-inline` | `firmware-inline` | on | Embed verified firmware callees and continuations in private executable regions. |
| `host-inline` | `host-inline` | on | Inline or splice certified native helper code. |
| `hotcold-partition` | `b2-layout` | on | Classify blocks by profiled execution phase. |
| `icount-entry-sfb` | `tcg-entry-sfb` | on | Rewrite the TCG icount-entry shape. |
| `identity` | `identity` | on | Byte-preserving round-trip control. |
| `irq-entry-mem-compare` | `irq-entry` | on | Fuse an eligible IRQ scratch load and sign test. |
| `llvm-cache-layout` | `b2-llvm-layout` | on | Order hot blocks with LLVM Cache-Directed Sort. |
| `local-scalar-cleanup` | `local-scalar` | opt-in | Scalar cleanup at ordinary/public block entries. |
| `move-units` | `activation-layout` | on | Place named blocks at explicit offsets. |
| `native-scalar-cleanup` | `native-program` | on | Scalar cleanup inside private native variants. |
| `order-cold` | `b2-layout` | on | Emit final warm/unknown/boot/cold section order. |
| `page-aware-local-layout` | `b2-page-local` | on | Bounded local swaps to reduce modeled page crossings. |
| `region-state-dse` | `global-region` | on | Delete a private-path state store overwritten before observation. |
| `region-state-forward` | `global-region` | on | Delete a state store already guaranteed by entry facts. |
| `retire-ordinary-exits` | `ordinary-exit` | opt-in | Return selected direct exits to ordinary QEMU dispatch. |
| `reverse-layout` | custom/test | on | Reverse relocatable units to exercise linking and fixups. |
| `specialize-edges` | `global-specialization` | opt-in | Build guarded private successors across supported edges. |
| `specialize-returns` | `b3` | opt-in | Clone and guard a continuation using return-site facts. |

## `align-trace-pages@1`

Slides the entire captured arena by a 64-byte-aligned delta. Relative distances remain
unchanged; the objective is fewer weighted host-page crossings. It requires an exact
steady profile and an original, relocatable v5 layout. It rejects pinned/non-relocatable
layouts and returns no change when the captured alignment already scores best.

- Controls: no pass parameters.
- Example: `--pipeline b2-arena-slide --only align-trace-pages`.
- Evidence: two recorded selections were 1.003x and 0.992x, a neutral result rather
  than a supported runtime win.
- Source: `scopt/passes/arena_slide.py`; related workflow: `docs/workflow.md`.

## `branch-folding@1`

In a private native region, replaces a decided conditional branch with an encoding of
the same length (a direct jump or NOP form). Same-size patching avoids moving following
instructions. Host constants and CFG evidence must prove the direction.

- Controls: `max_patches`, `record_encoding_sites`.
- Example: B3 arguments plus `--pipeline b3 --only branch-folding`.
- Evidence: the combined-fixture prefix was 0.985x [0.951, 0.999]; its
  leave-one-out result was inconclusive. No isolated real-application improvement is
  established.
- Source: `scopt/passes/branch_folding.py`; details: `docs/b3-passes.md`.

## `chain-returns@1`

Redirects sufficiently dominant profiled return exits into GENERAL slots. Failed guards
and unselected targets keep the ordinary execution path. Admission charges guard,
lookup, link, and growth costs rather than treating a removed dispatch as free.

- Opt-in. Requires exact steady return-edge/profile evidence, host operations, and a
  bound environment layout.
- Controls include target count, minimum pass/return share, hit floor, guard cost,
  ranking policy, compact guards, unit/growth limits, and reverse-pair handling.
- Example: `--pipeline dominant-return --only chain-returns --enable chain-returns`.
- Evidence: standalone `dominant-return` improved the CRC32 workload by 1.129x
  [1.080, 1.323]. It also participates in B3 bundles, but bundle results do not isolate
  its contribution or establish generalization.
- Source: `scopt/passes/chain_returns.py`; details: `docs/b3-passes.md`.

## `chain-static@1`

Redirects eligible profiled off-page static exits through GENERAL slots while preserving
ordinary fallback. It uses the same bounded cost model and target ranking family as
return chaining, but applies to static transfers.

- Opt-in. Requires exact steady edge evidence and the native/layout analyses.
- Controls include target count, pass share, hit floor, guard/check/link costs,
  compact guards, ranking, unit limits, and total growth.
- Example: B3 arguments plus `--pipeline b3 --only chain-static --enable chain-static`.
- Evidence: active in a measured B3 bundle; there is no retained final isolated number.
- Source: `scopt/passes/chain_static.py`; details: `docs/b3-passes.md`.

## `cluster-hot@1`

Orders movable hot translation blocks using exact profiled transfer weights and a
Pettis-Hansen-style clustering policy. Cold ordering is left to `order-cold`.

- Requires hotness annotations and an exact steady profile.
- Controls: profile `phase`, included edge `kinds`, and private-predecessor ordering.
- Example: `--pipeline b2-layout --only cluster-hot`.
- Evidence: active on PX4 (thousands of affected units), but those caches appended new
  translations during replay. PX4 timing is therefore invalid/unavailable, and the full
  B2 final campaign remains outstanding.
- Source: `scopt/passes/cluster.py`; details: `docs/b2-layout.md`.

## `constant-propagation@1`

Propagates and folds proved host constants within a private region using same-length
rewrites. It does not apply source-language undefined-behavior assumptions to guest
code.

- Requires host constant and decoded-operation analyses.
- Controls: `max_patches`, `record_encoding_sites`.
- Example: B3 arguments plus `--pipeline b3 --only constant-propagation`.
- Evidence: 0.943x for its combined-fixture prefix and 0.960x for the ablation study;
  on that fixture it was a measured regression, not a win.
- Source: `scopt/passes/constant_propagation.py`; details: `docs/b3-passes.md`.

## `dead-host-ops@1`

Replaces host instructions with NOPs only when CFG, constant, and liveness analyses
prove their results and effects unobservable inside a private region.

- Requires `hostops`, `hcfg`, `hconst`, and `hlive` analyses.
- Control: `max_patches`.
- Example: B3 arguments plus `--pipeline b3 --only dead-host-ops`.
- Evidence: prefix 1.022x and ablation 0.982x with broad intervals; inconclusive.
- Source: `scopt/passes/dead_host_ops.py`; details: `docs/b3-passes.md`.

## `eliminate-state-sync@1`

Deletes or coalesces translated guest-state stores that the B4 observer analysis proves
redundant at every permitted observation boundary. Helper, fault, interrupt, and
fallback visibility are part of admission.

- Opt-in. Requires `syncdead` analysis.
- Controls: eligible `classes`, `max_units`, `coalesce_nops`, `absorb_nops`.
- Example: `--pipeline b4 --only eliminate-state-sync --enable eliminate-state-sync`.
- Evidence: measured +0.14% with interval [-0.05%, +0.56%] on the compute workload: a
  neutral result, not an established benefit.
- Source: `scopt/passes/eliminate_state_sync.py`; details: `docs/b4.md`.

## `fold-exit-pc-update@1`

At proved exits, replaces the recognized ARM-PC load/add/store sequence with one x86
memory add. It only operates on original v5 code with a bound ARM-PC layout and exact
steady evidence.

- Controls: no pass parameters.
- Example: `--pipeline exit-pc-fold --only fold-exit-pc-update`.
- Evidence: two recorded selections were 0.999x and 0.996x: neutral/slightly slower.
- Source: `scopt/passes/pc_update.py`; related workflow: `docs/workflow.md`.

## `firmware-inline@1`

Builds a call-site-specific private executable region from captured QEMU translations.
Calls are discovered only from verified Thumb `BL` instructions joined to capture exit
metadata. The emitted v7 region contains a private caller copy, the captured callee CFG,
and the caller continuation. Static internal transfers are retargeted to private members;
returns retain their guest LR/SP and memory effects, verify the resulting PC and full
translation context, and fall back to the original lookup path on mismatch.

- Requires the exact firmware ELF, complete relocation/correspondence metadata, a bound
  environment layout, and the region-capable v7 QEMU loader. Public TB records remain
  independently available. Missing capture, ambiguous keys, handler/exception returns,
  and recursive call edges are retained and reported rather than assumed impossible.
- Control: `mode`, currently exactly `exhaustive`. In this mode hotness, profile share,
  callee/caller size, region growth, site count, copy count, member count, expansion
  depth, fixed-point iterations, and pass wall time are explicitly unbounded (`None`).
  The physical TCG arena remains a real resource; exhaustion rejects an incomplete
  artifact and records pending work.
- Unsafe trusted control is explicit and all-or-nothing:
  `trust="user", interrupts="disabled", icount="ignored"`. The default remains
  `trust="safe", interrupts="preserved", icount="preserved"`. Trusted mode is accepted
  only for an icount-off capture whose every member has the exact 11-byte non-icount IRQ
  poll followed by the captured four-byte `can_do_io` store. Internal edges skip the
  complete 15-byte prefix so MMIO cannot observe a stale false value; trusted root
  entries skip the same prefix. Trusted v7 uses one descriptor per region, no member search
  data or live fault/plugin recovery, no guest-code dependency checks, and a distinct
  loader capability. It also replaces admitted captured lookup tails directly instead of
  emitting the safe mode's return-context guards; an unexpected return/context is therefore
  outside the trusted contract. It is suitable only when the user verifies immutable code,
  mappings, and admitted control contexts and accepts fatal recovery for a real fault.
  An internal edge whose captured native exit has neither a rel32 field nor one verified
  lookup tail is retained as an ordinary side exit and reported as
  `unpatchable_native_exit`; it no longer invalidates every otherwise eligible region.
- Example:

  ```sh
  QEMU_SHA=$(sha256sum "$QEMU" | cut -d' ' -f1)
  scopt optimize "$SNAP" --pipeline firmware-inline --elf firmware.elf \
    --runtime-identity runtime_identity.json --v7-exe "$QEMU_SHA" \
    --store "$OUT" --json
  ```

  Trusted example (params JSON plus the separately admitted loader):

  ```sh
  printf '%s\n' '{"firmware-inline":{"mode":"exhaustive","trust":"user",' \
    '"interrupts":"disabled","icount":"ignored"}}' > trusted.json
  scopt optimize "$SNAP" --pipeline firmware-inline --params trusted.json \
    --elf firmware.elf --runtime-identity runtime_identity.json \
    --v7-trusted-exe "$QEMU_SHA" --store "$OUT" --json
  scopt firmware-region recover --artifact "$OUT/artifacts/$ARTIFACT" \
    --host-pc 0xHOST_PC --json
  ```

- Artifact/receipt: `fastdyn.firmware_region_plan@1` records source content hashes,
  full TB keys, call-site IDs, inline contexts, members, internal/side-exit edges and
  pending work. The v7 directory is arena-resident, so it does not inherit v6's fixed
  64 KiB extension-table limit. Reimport retains `firmware-regions.jsonl`.
- Trusted runtime entry uses a load-time first-match index from public source TB to
  private region. `QEMU_SC_V7_EXPERIMENT=linear-source-scan` is a diagnostic ablation
  that reproduces the rejected nested linear lookup; it is not a production policy.
- Example shape: `boot -> init -> continuation` becomes private members for boot, init,
  and continuation. A loop within init remains a backedge between private members; a
  nested acyclic `init -> clocks` call creates a deeper inline context. The public init
  TB remains available for unrelated callers.
- Evidence: correctness fixtures cover verified direct-call discovery and private-region
  construction. Safe mode measured 0.747x `[0.733, 0.761]` versus ordinary warm on the
  bounded FreeRTOS boot workload. The first trusted runtime measured 0.762x
  `[0.755, 0.790]`; a causal ablation then found a nested region/source linear lookup.
  The corrected indexed runtime measured **1.156x** `[1.132, 1.185]` versus ordinary
  warm across 12 fresh-process paired blocks. Re-enabling only the old lookup on the
  same binary and artifact reproduced 0.757x `[0.741, 0.765]`. Against capability-matched
  uncached FastDyn, the corrected trusted artifact measured **6.5168x**
  `[6.479, 6.573]` inside the guest window over 30 pairs, while full-process speed was
  0.9574x `[0.950, 0.980]`. This is not an upstream stock-QEMU comparison. See
  `docs/firmware-inline-evaluation.md` and
  `fixtures/firmware-inline/regression-ablations.json`.
- Source: `scopt/passes/firmware_inline.py`, `scopt/analysis/firmware_calls.py`,
  `scopt/analysis/firmware_regions.py`, `scopt/formats/sc_v7.py`, and the v7 loader in
  `third_party/common/qemu/tcg/supercache.c`.

## `host-inline@1`

Inlines certified native helper bodies or splices them into translated blocks. Modes
cover bounded recursive depth, partial-site publication, exception-capable helpers,
compact frames, and native unsigned-division lowering. The pass preserves the host ABI,
stack alignment, guest-state effects, fault paths, and recovery metadata.

- Requires a bound environment layout and certified native callees.
- Controls include `mode`, `bridge`, phase, callee/CFG/site/growth/time limits,
  `max_inline_depth`, `allow_partial`, `compact_frame`, `allow_faulting`, and UDIV mode.
- Examples: `--pipeline host-inline --only host-inline`; or
  `--pipeline host-inline=extreme,faulting,4-level --only host-inline`. The explicit
  aggressive experiment is `--pipeline host-inline=maximal --only host-inline`.
- Evidence: 0.998x on the combined fixture; a separate active RBIT workload measured
  1.138x with interval [1.089, 1.204]. Treat that as workload-specific.
- Source: `scopt/passes/host_inline.py`; details: `docs/host-inline.md`,
  `docs/host-inline-splice.md`, `docs/host-inline-extreme.md`,
  `docs/host-inline-maximal.md`, `docs/native-program.md`.

## `hotcold-partition@1`

Annotates translation blocks as hot, warm, unknown, boot, or cold from exact phase
evidence. It changes classification, not bytes; layout passes consume the annotations.

- Requires exact profile counts and reference analysis.
- Controls: profile `phase`, hot `coverage` target.
- Example: `--pipeline b2-layout --only hotcold-partition`.
- Evidence: a 1.015x prefix was byte-identical, so it cannot be attributed to this
  annotation pass.
- Source: `scopt/passes/hotcold.py`; details: `docs/b2-layout.md`.

## `icount-entry-sfb@1`

Rewrites the TCG instruction-count entry sequence into the supported store-forwarding
barrier shape. This is documented as a baseline code-shape repair, not as an ordinary
optimizer result.

- Control: `max_units`.
- Example: `--pipeline tcg-entry-sfb --only icount-entry-sfb`; the supported packaging
  workflow is `scopt sfb package`.
- Evidence: the final SFB-only result was 1.073x [1.066, 1.079].
- Source: `scopt/passes/icount_entry_sfb.py`; details: `docs/b4.md`.

## `identity@1`

Makes no edits. Its output must be byte-identical to the input, making it the control
for import, linking, validation, publication, and loading.

- Controls: none.
- Example: `--pipeline identity --only identity`.
- Evidence: this is a round-trip correctness control, not an optimization.
- Source: `scopt/passes/identity.py`; details: `docs/getting-started.md`,
  `docs/workflow.md`, and `docs/link-validate.md`.

## `irq-entry-mem-compare@1`

Fuses the native scratch load and sign test at an eligible non-icount IRQ poll. It
requires a bound native layout, dead scratch value, original v5 code, whole-block
equivalence, and exact steady execution evidence.

- Controls: none.
- Example: `--pipeline irq-entry --only irq-entry-mem-compare`.
- Evidence: the development candidate failed selection on NSICHNEU; the final run used
  ordinary warm bytes. No speedup is established.
- Source: `scopt/passes/irq_entry.py`; related workflow: `docs/workflow.md`.

## `llvm-cache-layout@1`

Orders hot translation blocks with LLVM Cache-Directed Sort. It can model reuse of
GENERAL blocks and optionally order private blocks by predecessor.

- Requires hotness annotations, exact steady profile counts, and the native bridge.
- Controls: `phase`, edge `kinds`, private-predecessor ordering, `bridge`, and estimated
  GENERAL reuse.
- Example: `--pipeline b2-llvm-layout --only llvm-cache-layout`.
- Evidence: prefix 1.023x [1.001, 1.042]; its ablation was inconclusive, and a FreeRTOS
  comparison was 0.976x with a wide interval.
- Source: `scopt/passes/llvm_layout.py`; details: `docs/llvm-layout.md`.

## `local-scalar-cleanup@1`

Runs bounded scalar and known-bit cleanup at ordinary/public translation-block entries,
refreshing affected analysis between edit rounds. Unlike private-region cleanup, it must
respect the public entry contract.

- Opt-in. Requires native-region, decoded host operations, CFG, liveness, and environment
  layout analyses.
- Controls: `max_patches`, `max_rounds`.
- Example: `--pipeline local-scalar --only local-scalar-cleanup --enable local-scalar-cleanup`.
- Evidence: byte-identical in one combined arm; an active FreeRTOS run measured 0.992x
  [0.967, 1.009], a neutral result.
- Source: `scopt/passes/region_cleanup.py`; details: `docs/native-program.md`.

## `move-units@1`

Places named translation blocks at explicit arena offsets or relative deltas. It is a
fixture and activation tool for linker/relocation validation, and is also used by the
explicit `activation-layout` workflow; it is not a profitability policy.

- Controls: `moves` maps unit IDs to offsets; `deltas` maps IDs to signed deltas. A unit
  may not occur in both maps.
- Example: `--pipeline activation-layout --only move-units --params placements.json`.
- Evidence: the `activation-layout` experiment measured 1.074x [1.065, 1.083] on Nettle
  AES and 1.096x [1.052, 1.130] on SLRE. These benchmark results do not establish a
  general placement policy or real-application win.
- Source: `scopt/passes/move_units.py`; details: `docs/activation-layout.md` and
  `docs/b2-fixtures.md`.

## `native-scalar-cleanup@1`

Runs bounded scalar and known-bit cleanup inside a private native variant. Private entry
facts permit rewrites that are not legal at an ordinary public block entry.

- Requires native-region analysis.
- Controls: `max_patches`, `max_rounds`.
- Example: native-program/B3 arguments plus
  `--pipeline native-program --only native-scalar-cleanup`.
- Evidence: a 1.015x prefix was byte-identical on the combined fixture, so it provides
  no attributable performance result.
- Source: `scopt/passes/region_cleanup.py`; details: `docs/native-program.md`.

## `order-cold@1`

Emits the final warm, unknown, boot, and cold section order after hot placement. The
optional fallback also includes hot blocks when an earlier hot-ordering pass did not
place them.

- Requires hotness annotations.
- Control: `hot_fallback`.
- Example: `--pipeline b2-layout --only order-cold`.
- Evidence: neutral/inconclusive on the native fixture. It is active on PX4, but the
  appended-translation cache defect makes PX4 timing invalid.
- Source: `scopt/passes/order_cold.py`; details: `docs/b2-layout.md`.

## `page-aware-local-layout@1`

Starts from the dense capture order and performs bounded adjacent swaps that reduce
weighted host-page crossings. It protects exact directed adjacency and reports when a
potential page gain is blocked by original adjacency or native-distance constraints.

- Requires an original, dense, unplaced, relocatable v5 arena and an exact steady
  profile; it does not run in B3 mode.
- Controls: positive `max_rounds` and `max_swaps` (defaults 3 and 32 in the pass).
- Example: `--pipeline b2-page-local --only page-aware-local-layout`.
- Evidence: no retained runtime measurement was found. This was the one canonical pass
  with no Markdown documentation before this catalog.
- Source: `scopt/passes/page_layout.py`.

## `region-state-dse@1`

Removes a store on a private path only when every path overwrites that state before any
observer. It checks helper-effect freshness and the observer contract; unknown or stale
evidence causes rejection rather than an optimistic rewrite.

- Requires native-region, observer-contract, decoded-operation, and CFG analyses.
- Control: `max_patches`.
- Example: global-region/B3 arguments plus `--pipeline global-region --only region-state-dse`.
- Evidence: dedicated enabled/disabled fixture measured 0.977x [0.940, 1.001], neutral
  to regressive.
- Source: `scopt/passes/region_state_dse.py`; details: `docs/global-specialization.md`.

## `region-state-forward@1`

Removes an immediate state store when guarded entry facts prove the exact state field
already contains the stored value. Unresolved state paths are rejected.

- Requires native-region and host-constant analyses.
- Control: `max_patches`.
- Example: global-region/B3 arguments plus
  `--pipeline global-region --only region-state-forward`.
- Evidence: prefix 0.987x and ablation 0.976x; no supported speedup.
- Source: `scopt/passes/region_cleanup.py`; details: `docs/native-program.md`.

## `retire-ordinary-exits@1`

Returns selected low-reuse direct exits to ordinary QEMU dispatch. Admission depends on
a matching training manifest and a complete, exact, chain-disabled steady profile so
the pass does not infer safety or profitability from partial edge observations.

- Opt-in.
- Controls: `training_manifest`, `max_transfers`.
- Example: `--pipeline ordinary-exit --only retire-ordinary-exits --enable retire-ordinary-exits`.
- Evidence: the documentation requires independent timing; no retained accepted result
  was found.
- Source: `scopt/passes/ordinary_exit.py`; details: `docs/ordinary-exit.md`.

## `reverse-layout@1`

Reverses relocatable translation blocks to stress relocation, linker, recovery, and
fault validation. It is a test pass and is intentionally absent from production
pipelines.

- Control: `exclude`, a list of unit IDs not to reverse.
- Example: use a custom test pipeline containing `reverse-layout`, then select it with
  `--only reverse-layout`.
- Evidence: functional validation only; it is not performance evidence.
- Source: `scopt/passes/reverse_layout.py`; details: `docs/b2-fixtures.md` and
  `docs/link-validate.md`.

## `specialize-edges@1`

Builds guarded private successors across supported static edges. It can follow a bounded
number of state-preserving hops, but retains the ordinary path for guard failures and
unseen values. Coverage mode and evidence are explicit because private control flow must
not silently change feedback semantics.

- Opt-in. Requires the global host/fact/effect/profile analyses and a bound environment.
- Controls include per-context and total variant caps, growth limits, target count,
  pass share, guard/lookup/failure costs, partitions, preserving-hop limit, and coverage
  mode/evidence.
- Example: global-specialization/B3 arguments plus
  `--pipeline global-specialization --only specialize-edges --enable specialize-edges`.
- Evidence: some small fixture prefixes improved, but the full-pipeline ablation was
  0.927x [0.902, 0.992], showing a regression on that combined fixture. There is no
  isolated real-application result.
- Source: `scopt/passes/specialize_edges.py`; details: `docs/global-specialization.md`
  and `docs/native-program.md`.

## `specialize-returns@1`

Clones and guards a callee continuation using certified return-site facts. Unmatched
returns retain ordinary execution. Variant and growth caps bound duplication.

- Opt-in. Requires exact steady return/fact/host/layout analyses.
- Controls include per-context and total variant caps, growth limits, target count,
  minimum pass share, and lookup/guard/failure costs.
- Example: B3 arguments plus
  `--pipeline b3 --only specialize-returns --enable specialize-returns`.
- Evidence: it created no variant on the reviewed FirmBOLT workload. That makes the
  experiment inactive/untested there, not a measured negative result.
- Source: `scopt/passes/specialize_returns.py`; details: `docs/b3-passes.md`.

## Reading measured status

The evidence notes above summarize repository-retained results; they are not universal
rankings. A pass can be a conditional win on one helper or firmware and a regression on
another. Byte-identical or inactive artifacts are controls, not performance results.
When a catalog entry says that no retained number was found, reproduce and record the
experiment before making a performance claim. See `docs/timing-and-report.md` and
`docs/workflow.md` for paired timing, artifact identity, and publication rules.

The principal checked-in evidence summarized here is:

- `docs/native-program-evaluation/README.md` and its `per-pass.csv` for prefix,
  ablation, identity, and active-fixture comparisons;
- `docs/review-2026-11-21/README.md` for the FirmBOLT compute B3 and SFB runs;
- `docs/design/B3-B4-design.md` for the retained B4/SFB report;
- `docs/selected-warm-benchmark/README.md` for the six-firmware selection campaign;
- `docs/compiled-launch-benchmark/README.md` for direct-QEMU frontend and cache isolation; and
- `../trebuchet/experiments/px4_memset_layout/host-experiment-manifest.json` for PX4
  activation and the appended-translation timing blocker.
