# Host-inline: how recursive it is, and what a recursive O4 version needs

Scope: scopt `o4` at b35cdb6 (`/data/fastdyn-work/fd-int/tools/scopt`, read only),
receipts under `/data/scfix-o4/<w>-w/opt/<arm>/`, campaign summaries in `/data/scfix-o4`.
Nothing was built, timed or edited. File paths below are relative to the scopt checkout
unless they start with `/`.

## 0. Answer in brief

- **Helper inlining is not recursive in the FirmGraal sense.** It never optimizes inside a
  callee and never removes a nested call except in two narrow paths.
  - **O4's mode (`trusted_selected_root`) goes one level deep.** It looks at the root and its
    immediate children only. A child is folded only if it is a call-free, stack-neutral leaf.
  - **The full-graph mode only copies.** It copies the whole transitive call graph byte for
    byte, retargets internal calls to the copies, and removes no nested call
    (`nested_calls_eliminated: 0` in every A4 and A6 receipt).
  - **Recursive expansion exists only for FastDyn plugin callbacks.** The `extreme`/N-level
    framed path removes nested calls, but only along a linear one-call-per-frame chain of
    plugin callbacks.
- **It reaches firmware-region members, but only because of pass order.**
  - host-inline is the single native prefix that runs before every B3 pass.
  - Region members copy the pre-B3 bytes of their source units, so they inherit any splice.
  - Its selection does not know about regions, firmware-inline's pricing does not know about
    helpers, and host-inline cannot run again on region copies, SPEC clones or its own output.
- **Why only 0-1 sites apply in O4: the lowering, not the 1000-call threshold.**
  - Nearly all in-loop helper calls above the threshold go to `helper_lookup_tb_ptr` or
    `helper_udiv`. `lower_selected_root` refuses both (`selected_root_unsupported`).
  - Only `helper_shl_cc` on packet (8,186 calls) can be lowered.
- **The bounded variants (A4 `o4_hotgraph`, A6 `o4_hostall`) were literal graph copies.**
  - Each site carried 20-32 copied bodies, 39-82 retained nested calls and a synthetic
    return. One copy is 4.4-9.0 KB, mostly cold paths.
  - Pooled results: A4 −3.63%, A6 −4.51%.
  - The same mechanism (no context simplification after copying) is the documented cause of
    H2's 0.842×.
- **The gdb replay can be dropped without changing QEMU.**
  - gdb only reads a counter array at `exit_group`.
  - O4 already requires the sealed steady profile. Its TB-entry counts match the gdb site
    counts exactly on uart and within 9-12% on packet for every hot non-lookup site.
  - host-inline already has a dominance proof that turns TB entries into exact site counts.
    `hot_loop_only` simply does not accept them.
- **For O4, a recursive host-inline means partial evaluation, not deeper copying.**
  - The only helper work worth targeting after B3 is the `v7m_msr` + `rebuild_hflags_m32_newel`
    pair: 1.26 M pairs per packet run and 297 k per uart run.
  - The scopt design `docs/design/helper-inlining-design.md` (C1 `hs-msr-fold`, C2
    `hs-hflags-elide`) turns the pair into one store and a NOP, in place, with no code growth.
    It needs no profile and is inherited by every region member.
  - It is designed and not built.
- **Campaign F (`/data/scfix-o4/F.json`, F-most.json) adopted o4r3d, which has no
  host-inline and no SPEC bundle.**
  - o4r3c measured +0.10% pooled against it, one-sided 95% [−0.60%, +0.80%].
  - So the host-inline that o4r3c carries has no measurable value.

## 1. What runs today

- **o4r3c's host-inline entry** (`/data/scfix-o4/design/pipelines/o4r3c.toml`, first
  `[[pass]]`) is:
  `mode="maximal", trusted_all_helpers=true, trusted_rank_by="frequency", trusted_selected_root=true, hot_loop_only=true, min_site_calls=1000`.
  This is sealed-pgo-7's H1 policy (`scopt/o4.py:228-234`, `docs/O4.md:153`).
- **Dispatch.**
  - `trusted_all_helpers` requires `mode="maximal"` and hands the run to
    `HostInlineGraph` (`scopt/passes/host_inline.py:330-336`).
  - `hot_loop_only` requires trusted frequency ranking without the TB-entry proxy
    (`host_inline.py:283-287`).
- **Stage.**
  - `optimize` treats host-inline as the native prefix: it must be the first pass, there is
    exactly one, and B3 passes follow (`scopt/commands/optimize.py:664-697`; the message
    "one native prefix followed by B3 passes is required" is at 673-674).
  - The prefix is compiled to a new snapshot (`_splice_generation`, `optimize.py:103-121`).
  - The B3 stage runs on that snapshot, with the profile re-opened as a `helper_splice` view
    (`optimize.py:124-142`).
  - After the prefix, `INLINE_CALL` is removed from the allowed capabilities
    (`optimize.py:699-700`), so no later pass can inline a helper.

## 2. Is helper inlining recursive? (depth of the helper graph)

| Path | Graph captured | Nested calls removed | Code |
|---|---|---|---|
| **O4: `trusted_selected_root`** | root + **immediate** children only: importer stops at depth 1 when `immediate_helpers` is set (`scopt/importer/native_callee.py:211-213`), which host-inline sets from `trusted_selected_root` (`host_inline.py:418-422`) | **depth 1, leaves only**: each child is folded with `_closed_fallthrough_code(None, child, {})`, so children must themselves be call-free (`scopt/validate_sym/selected_helper.py:39-43`). The root must be a stack-neutral closed CFG (`native_helper_graph.py:244-336`, RSP refusal 263-269) or a tail wrapper into a folded leaf (`selected_helper.py:49-57`), else `selected_root_unsupported` (64-66) | `host_inline_graph.py:53-60, 72-77`; `native_helper_graph.py:462-466` |
| **Full trusted graph** (A4, A6, H2, w7) | **transitive fixed point** over same-image direct edges, each function once; SCCs stay cyclic (`native_callee.py:172-216`; docstring `host_inline_graph.py:16-21`) | **none**: `_compile_graph` emits `lea r11,[rip+cont]; push r11`, the root, every other body and absolute thunks for external targets. Internal CALLs are retargeted to the copies and kept. The result is `HelperCallLowering("synthetic-return", len(graph), 0, retained)` (`native_helper_graph.py:344-449`; prefix 410, return 448-449). Out-of-image RIP operands become 6-instruction push/mov/op/pop sequences, and indirect tail jumps become a `ret 128` trick (`native_helper_graph.py:88-108`) | `native_helper_graph.py:1-16` |
| Root wrapper only / shared wrapper | root only | none: the wrapper keeps a call to the shared QEMU body (`native_helper_graph.py:141-225`); `hot_loop_only` refuses this as `shared_wrapper_retains_call` unless `include_hot_shared_wrappers` (`host_inline_graph.py:84-87`) | — |
| `extreme` / `N-level` (`HostInlineExtreme`) | `discover_chain`, only for `role=="plugin"` with typemask 400 (`native_callee.py:218-240, 400-405`) | **recursive to depth N**: a LEA reserves each return slot, and the frame prefixes are emitted, then the continuations inside out (`scopt/validate_sym/native_virtual.py:1-16, 86-108, 110-159`). It needs one direct child call per frame and a `void(u32, ptr)` plugin callback (`native_virtual.py:136-137`), so it never applies to QEMU helpers | `host_inline_extreme.py:60-71` |
| `splice` / `faulting` (certified) | leaf only (≤256 B, one final RET) | n/a (leaf). UDIV has hand-written partial forms that keep the zero path out of line (`host_inline.py:666-719`) | `scopt/validate_sym/native_splice.py:77-112` |

**Receipt evidence that the full graph only copies.** In the A4 and A6 compile receipts
(`/data/scfix-o4/{packet,uart,cli}-w/opt/{o4_hotgraph,o4_hostall}/store/native-generations/artifacts/*/compile-receipt.json`,
`helper_inline.sites[].call_lowering`):

| Helper | Lowering | Bodies copied per site | Nested calls retained | Bytes per site |
|---|---|---:|---:|---:|
| `helper_v7m_msr` | synthetic-return | 32 | 82 | 8,958 |
| `helper_rebuild_hflags_m32_newel` | synthetic-return | 20 | 39 | 4,422 |
| `helper_udiv` | synthetic-return | 31 | 80 | 6,966 |
| `helper_v7m_mrs` | synthetic-return | 12 | 38 | 3,462 |
| `helper_shl_cc` | fallthrough | 1 | 0 | 126 |

Every A4 and A6 site has `nested_calls_eliminated: 0`. A6 is 913 / 1,400 / 1,398
synthetic-return sites against 92 / 55 / 74 fallthrough sites. `helper_udiv` copies 31
bodies because its whole exception path (`raise_exception*`) comes with it. A recursive
inliner would leave that noreturn path as an external call.

## 3. Does it apply to firmware-region members?

- **Yes, by pass order.**
  - Members are built from `by_uid`, the pre-B3 units, which already contain the splices
    (`scopt/link.py:1633-1716`; docstring 1718-1733; "A trusted region member copies the
    pre-B3 bytes of its source", `link.py:342-357`).
  - The emission check accepts spliced sources: content hash, relocatable, `elf_verified`
    (`scopt/manager_b3.py:381-400`).
  - Every copy carries the splice, including nested inline copies of the same TB.
- **Receipt evidence.**
  - In o4r3c on packet, firmware-inline's `embedded` list contains the one spliced unit
    (`tb:5d2865bff9520d2464bfc5b3aca8d791#0`, `helper_shl_cc`).
  - In o4_hotgraph, 3 / 7 / 2 of the 11 / 12 / 2 spliced units are region members
    (`optimize.json`, firmware-inline `affected` and `embedded`).
- **What is not region-aware.**
  - Selection uses SCCs of the steady profile's transfer edges (`host_inline.py:432-451`,
    `scopt/analysis/helper_hot_loop.py:92-135`), not the region plan. It runs before
    firmware-inline exists, so it cannot know which units become members.
  - firmware-inline's cost model charges member bytes and gives no credit for helper
    savings. A 9 KB splice in a hot member adds about 2.2 µs of `footprint_ns` per copy at
    `byte_ns = 0.25` (policy constants in the o4r3c firmware-inline receipt).
  - host-inline cannot run on members after linking. INLINE_CALL is prefix-only
    (`optimize.py:673-674, 699-700`), and it refuses already-grown units as
    `grown_unit_unsupported` (`host_inline.py:500-502`).
- **The reverse direction is closed too.** Other passes do not optimize inside a splice:
  - specialize-returns refuses spliced targets as `native_layout_unsupported`
    (`scopt/passes/specialize_returns.py:135-140`);
  - B3 patch placement refuses edits that overlap native certificates
    (`manager_b3.py:656-663`);
  - native-region analysis turns every splice byte into a barrier for scalar cleanup
    (`scopt/analysis/native_region.py:37-42`);
  - re-verification requires byte-exact correspondence (`native_helper_graph.py:491-510`).

## 4. Why only 0-1 sites apply in O4

o4r3c receipts (`/data/scfix-o4/<w>-w/opt/o4r3c/optimize.json`, `native_stage.passes[0]`). Counts
are whole-run calls from the gdb replay (`helper-pgo-*/calls.json`, scope `whole_run`,
`scopt/run/helper_pgo.py:512-516`).

| | packet | uart | cli |
|---|---:|---:|---:|
| sites seen | 18,229 | 14,134 | 19,880 |
| `outside_observed_loop` (transfer SCC, `host_inline.py:489-495`) | 16,366 sites / 7.6 M calls | 13,780 / 2.90 M | 19,369 / 2.4 k |
| `below_min_site_calls` (<1000) | 1,733 / 10.2 k calls | 322 / 10.4 k | 463 / 21.4 k |
| `selected_root_unsupported` | **123: lookup_tb_ptr 117 (12.77 M calls) + udiv 6 (88.7 k)** | **30: lookup 25 (224 k) + udiv 5 (283.7 k)** | **43: lookup 41 (171 k) + udiv 2 (4.5 k)** |
| `call_target_mismatch` | 6 | 2 | 5 |
| applied | **1: `helper_shl_cc`, 8,186 calls, 120 B** | 0 | 0 |

- **The binding constraint is the selected-root lowering.** On packet, about 99.9% of the
  in-loop, above-threshold calls are refused this way. Lowering a helper needs a
  stack-neutral closed root, or a tail wrapper into a folded leaf
  (`selected_helper.py:44-66`). `lookup_tb_ptr` and `udiv` have frames and calls.
- **The `transfer` loop kind hides the critical-section helpers.** `v7m_msr` and
  `rebuild_hflags` (1.26 M calls each on packet, 297 k on uart) end their TBs with
  `exit_tb(0)`, so they fall outside transfer SCCs. Only `hot_loop_kinds="firmware"` sees
  them (`helper_hot_loop.py:49-58`).
- **The counts overstate lookups.** They are taken on the unchained pre-B3 cache, so they
  include `helper_lookup_tb_ptr` calls that chain-static, chain-returns and the region
  guards later retire.
- **`min_site_calls = 1000` barely matters.** It removes only about 10-21 k calls per
  workload.
- **The replay is not stable.** The design records counted-replay non-determinism
  (`helper_shl_cc` 9,433 against 9,636), and o4r3c recorded 8,186
  (`/data/scfix-o4/design/O4-design.md:401-404`).

## 5. The bounded variants (32,768 B graph cap)

- **A4 `o4_hotgraph`**: maximal, trusted graph, `trusted_max_source_graph_bytes=32768`,
  `hot_loop_only`, `hot_loop_kinds="firmware"`, `min_site_calls=1000`, no selected root.
  - Applied 13 / 20 / 2 sites: `v7m_msr`, `rebuild_hflags`, `udiv`, `shl_cc`, `v7m_mrs`.
  - Added 75,696 / 128,880 / 13,920 bytes.
  - The cap refused 260 / 71 / 41 `lookup_tb_ptr` sites (16.8 M / 2.25 M / 171 k calls).
  - Timing (A): pooled −3.63% [−4.84%, −2.43%]; packet −4.11%, uart −7.27%, cli −2.27%
    (`/data/scfix-o4/A-SUMMARY.md:17, 86-89`). Excluded as measured slower.
  - Its B3, SPEC and region counts equal O4's on packet and uart (identical chain-static,
    chain-returns and specialize-returns stats). The loss is not a SPEC loss. It is the
    copies themselves: 4.4-9 KB of mostly cold code per hot site, also duplicated into the
    region members that carry it.
  - cli lost 2.27% with only 2 udiv sites (4.5 k calls), both region members. This points to
    layout and footprint effects, not call overhead.
- **A6 `o4_hostall`**: `allow_partial`, 32768 cap, no ranking.
  - Applied 1,005 / 1,455 / 1,472 mostly cold sites, adding 1.68 / 3.65 / 3.54 MB.
  - Timing (H): pooled −4.51% [−5.08%, −3.94%]; packet −4.05%, uart −12.45%, cli −0.32%
    (`/data/scfix-o4/H-SUMMARY.md:18, 68-71`).
- **H2 / w7 (unbounded).** `helper_lookup_tb_ptr`'s 50,345 B graph was copied to 1.7-2.2 k sites
  (+124-157 MB).
  - It also removed the lookup tails that firmware-inline and B3 rely on: 760 / 365 / 486
    roots refused `lookup_tail_missing`, and every return exit `exit_not_goto_ptr`
    (`/data/scfix-o4/design/inventory.md:323`).
  - Helper-only compute: 0.842× [0.825, 0.880]. The cause given is "executing enormous
    per-site private helper copies … no context simplification removes their unused paths
    after copying" (`docs/sealed-whopr-7.md:154-171, 183-209`).
  - The same-layout lowering-only control was 0.973× [0.838, 1.007], which includes 1. So a
    synthetic-return penalty is plausible but not established.
- **Earlier compute sweep** (`experiments/helper-inline-sweetspot/README.md:9-27`):
  - true-leaf fallthrough 1.003× [0.971, 1.030];
  - frequency 1 MiB 1.002×;
  - density 256 KiB 0.950×;
  - the regressions follow the number of `rebuild_hflags` copies (suggestive, not causal).
  - Its stated next step is "not more literal copying" but partial evaluation of the MSR pair
    (lines 70-74).

## 6. Can the helper-PGO (gdb) dependency be avoided?

- **What gdb does.** The counted replay patches every helper CALL to a counter stub
  (`helper_pgo.py:228-290`). gdb is used only to read the counter array at the
  `exit_group` catchpoint (`helper_pgo.py:477-497`). The replay failed intermittently twice
  (`inventory.md:322, 349`).
- **Option 1: no QEMU change, no gdb.** Use the steady profile that O4 already requires
  (`docs/O4.md:70-73`).
  - `_tb_entries_are_call_count` already proves when TB entries equal the executions of a
    site: no earlier call or backward branch, and not `ldst_slow`
    (`host_inline.py:223-254`). It is used for trusted frequency ranking without
    `hot_loop_only` (`host_inline.py:509-515`).
  - But `hot_loop_only` insists on `load_site_counts` from the replay
    (`host_inline.py:432-442`).
  - **Change:** let `hot_loop_only` accept exact TB-entry counts when the proof holds, and
    allow a preceding call when that helper cannot longjmp, using the helper-effects or
    flags table.
  - **Data check:** whole-run TB entries in `<w>-w/in/merged.profile.json`, compared with the
    gdb counts for every non-lookup site at ≥20 k calls, give a ratio of exactly 1.000 on
    uart and 0.878-1.000 on packet (`v7m_msr`, `rebuild_hflags`, `v7m_mrs` 0.88-0.91;
    `shl_cc` and `udiv` 1.000). That is good enough to rank against a 1000-call threshold.
- **Option 2: no QEMU change.** An `LD_PRELOAD` destructor scans for the `SCHPROF1` magic
  and writes the counter array at exit, replacing the gdb catchpoint.
- **Option 3: QEMU change** (qemu-fu, `tcg/supercache.c`). The runtime dumps the diagnostic
  counters itself, as it already writes qprof at exit.
- **Option 4: avoid the need altogether.** C1/C2 partial evaluation (section 7) is
  same-length and grows no code. It can apply at every matching site with no hotness
  selection, so no profile is needed at all. This is the "without profile" route the user
  asked for.

## 7. What a recursive-but-bounded host-inline that applies inside regions would need

The analogue of FirmBOLT `-O4` (FirmGraal recursive) is a worklist that inlines the whole
callee CFG at a selected site, folds branches decided by known argument registers, and
treats each copied call as a new candidate, under a depth limit, a recursion guard, a step
limit and a byte budget. Mapped onto scopt:

1. **Partial evaluation first: the only measured-worthwhile target. Large effort, no QEMU
   change.**
   - Build C1 `hs-msr-fold` (`call helper_v7m_msr(env,0x811,val)` becomes
     `movzx edx,dl; mov [rbp+0xc04],edx; nop`) and C2 `hs-hflags-elide` (the following
     rebuild call becomes NOPs), as designed in `docs/design/helper-inlining-design.md:45-75`.
   - They are proven by `hostsym` with an exe-bound axiom table, and F13 must be fixed first
     (`:64, 648`).
   - They are in place and same-length, so they need no profile, no growth and no budget.
   - Put them in the native prefix stage (pre-B3) and every region member and nested
     inline copy inherits them through `link.py:1633-1716`. Do not make them B3
     PURE_REWRITEs: members copy pre-B3 bytes (`link.py:1725-1727`).
   - Recheck SPEC's `native_layout_unsupported` refusal: it keys on `native_fragments`
     (`specialize_returns.py:138-140`), and an in-place rewrite need not set it.
   - Estimate: design MODEL 13.5 ns per pair (`helper-inlining-design.md:77-90`, v1-core row at 81). That is
     about 17 ms per packet run (1.26 M pairs; runs are about 0.85-0.9 s in
     `packet/timing-F/observations.jsonl`), about 4 ms per uart run, about 0 on cli. So
     roughly +2% / +0.8% / 0, a MODEL estimate.
   - The C3 stretch, chaining the MSR TB's `exit_tb(0)` round trip, is estimated at 1.5-4.3%
     on compute, but needs a spec review (`:105-108`).
2. **Recursive lowering for framed helpers, to replace synthetic-return copying. Medium-large
   effort.**
   - Generalize `_emit_frames` (`native_virtual.py:86-108`, reserve the slot with LEA)
     from linear plugin chains to general helper CFGs in `_compile_graph`
     (`native_helper_graph.py:344-449`).
   - Each in-graph CALL becomes `lea rsp,[rsp-8]` plus the body. Each RET becomes
     `lea rsp,[rsp+8]; jmp cont`.
   - Bodies that read their return slot (QEMU `GETPC()`) keep a pushed continuation.
   - Noreturn subgraphs (`raise_exception*`, `cpu_loop_exit`) stay external calls.
   - SCC guard: keep the call. Bound depth, steps and per-site and total bytes (keep the
     32768 cap; it is what keeps `lookup_tb_ptr` out).
   - Count `nested_calls_eliminated`.
   - **Only worth it together with item 3.** Without it this is A4 with fewer RETs, and A4
     was −3.6%.
3. **Context specialization inside the copied graph** (the FirmGraal "fold branches decided
   by known argument registers").
   - Seed known argument registers from the TCG argument setup at the site (`mov esi, imm`;
     host constants are already computed by `scopt/analysis/hconst.py`) and from TB-key
     facts (`helper-inlining-design.md` §4.2).
   - Fold branches, prune unreachable bodies, then rerun the worklist on the newly exposed
     calls.
   - This needs a new splice kind and certificate, because the current verifier demands a
     byte-exact transplant (`native_helper_graph.py:491-510`). Large effort.
4. **Making it apply inside regions.** Two options:
   - **(a) Cheap:** select by the firmware loop kinds (`hot_loop_kinds="firmware"`, already
     there) or by firmware-inline's hot-loop roots. Pass order then carries the splices into
     members.
   - **(b) Truly recursive:** add a helper-splice step to `_firmware_member_units`
     (`link.py:1633-1716`) so each member copy, with its `inline_context` facts, can get its
     own specialized splice. That needs:
     - INLINE_CALL allowed after B3 (`optimize.py:664-697`);
     - firmware-inline pricing (`firmware_profit`) that credits removed helper work and
       charges spliced bytes;
     - splice re-verification on member preimages.
   - Splice code is position-independent after absolutization (`native_helper_graph.py:88-108,
     405-420`), so no QEMU loader change is expected. The trusted contract already excludes
     live fault recovery for these copies (`native_helper_graph.py:14-15`).
5. **Never target `helper_lookup_tb_ptr`.** It is the hottest helper (12.77 M in-loop calls
   on packet), but B3 slots and region guards retire it. Copying it destroys the lookup tails
   they need (H2).
   - The recursive treatment there is the B3/region side: U7 (B3 slots that enter regions),
     lifting the 256-unit cap, and C6 `inline-lookup-probe`, the fast-path probe only
     (`helper-inlining-design.md:736-749`, §4.8.2).
6. **Composition.** Let SPEC clone spliced units by re-certifying the splice on the clone
   (`specialize_returns.py:135-140`). Under the trusted contract, let scalar cleanup see
   inside `HELPER_GRAPH_KIND` splices instead of treating them as barriers
   (`native_region.py:37-42`).
7. **Drop gdb** (section 6, option 1). This is small and makes the O4 build deterministic.

**Recommendation for "all optimizations recursive" in O4.**
- Do not put any copying host-inline variant into O4. H1 is inert (+0.10% for o4r3c over
  o4r3d in F, not significant), and A4 and A6 are −3.6% and −4.5%.
- Build item 1 (C2 first, then C1) as a pre-B3 in-place stage. It is profile-free, applies
  at every site, and regions, nested inline copies and B3 inherit it automatically. Keep
  item 7 if H1 is kept.
- Items 2-4 are the recursive inliner proper. They have no supporting measurement yet and
  should be gated on a P0 hand-patched probe, as the design prescribes
  (`helper-inlining-design.md:95`).

## 8. Items (every optimization in the subsystem)

| # | Item | In o4r3c | Recursive today | Gap | Change | Effort |
|---|---|---|---|---|---|---|
| 1 | H1 hot-loop selected-root helper splice (`maximal`, trusted, selected root, `hot_loop_only`, `min_site_calls=1000`) | yes (o4r3d, adopted in F, drops it) | depth 1 at most; pre-B3 only, so members inherit it; 1 / 0 / 0 sites | lowering refuses every framed hot root | replace with item 13, or with items 4 + 12 | none to drop it |
| 2 | Selected-root leaf-child folding (`lower_selected_root`) | yes (inside 1) | depth 1, call-free leaves only (`selected_helper.py:39-43`) | no bottom-up fold of grandchildren; importer stops at depth 1 (`native_callee.py:211-213`) | memoized bottom-up substitution over the full graph with an SCC guard | small (but almost nothing qualifies) |
| 3 | Trusted full helper-graph transplant, unbounded (H2) | no | copies the transitive graph and removes no nested calls (`native_helper_graph.py:448-449`) | no inlining, no simplification; copies `lookup_tb_ptr` | do not use; superseded by item 12 | — |
| 4 | Graph size cap `trusted_max_source_graph_bytes=32768` (H3; A6 `o4_hostall`) | no | as item 3, bounded | as item 3; cold sites | keep the cap as the budget of any recursive inliner | none |
| 5 | A4 `o4_hotgraph`: `hot_loop_kinds="firmware"` with full graphs | no | as item 3; 3 / 7 / 2 spliced units became region members | copies 20-32 bodies per site; region-blind pricing | the firmware loop kinds are reusable as the selector for items 12-13 | none |
| 6 | Root-wrapper and shared-wrapper lowering (`trusted_root_wrapper_only`, `include_hot_shared_wrappers`, tail-leaf fallthrough) | no (tail-leaf is inside 1) | none (it keeps the shared call) | — | none | — |
| 7 | Selection and ranking knobs (`hot_loop_only`, `hot_loop_kinds`, `min_site_calls`, `trusted_rank_by` frequency / frequency-density, `trusted_min_tb_entries`, `trusted_max_total_growth`, TB-entry proxy) | partly (frequency, transfer, 1000) | n/a; region-blind | they select per site of the pre-B3 cache | region- or firmware-loop-aware selection for items 12-13 | small |
| 8 | Helper-PGO counted replay (gdb) | yes (input to 1) | n/a | gdb, non-deterministic | exact TB-entry counts (`host_inline.py:223-254`) | small |
| 9 | Certified leaf splice (`splice` mode, bridge, argument-load fusion) | no | leaf only | needs the LLVM/BOLT bridge; no hot leaf remains | none | — |
| 10 | Island mode (bridge fragment out of line) | no | none | superseded by splice | none | — |
| 11 | UDIV exception-capable lowering (`faulting`: full / guarded / cleanup / partial-cleanup / reciprocal29) | no | partial inlining by hand (zero path out of line) | unreachable from trusted mode (`HostInlineGraph._prepare_request` bypasses it) and needs a bridge (`host_inline.py:404-417`) | route `is_udiv_binding` in `HostInlineGraph` to the guarded form | small; ≤320 k calls, below 0.2% |
| 12 | `extreme` / N-level framed recursive expansion with `compact_frame` cleanup | no | **yes, to depth N**, plugin callbacks only | not applicable to QEMU helpers (linear one-call frames, plugin typemask) | generalize `_emit_frames` to helper CFGs (section 7, item 2) | medium-large |
| 13 | Designed, not built: `hs-msr-fold` (C1), `hs-hflags-elide` (C2), C3 msr-exit-chain, C6 inline-lookup-probe | no | in place, so regions, nested copies and B3 inherit it | not built; needs F13 and an axiom table | `docs/design/helper-inlining-design.md` §4, §7 | large (14-18 weeks per the design) |
