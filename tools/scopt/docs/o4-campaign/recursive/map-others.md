# Recursion map: the other scopt passes, the runtime features and the pass manager

Step of the "all optimizations recursive" survey. This step changed no code and ran no builds
or timings. Sources were read at these commits:

- scopt: `/data/fastdyn-work/fd-int/tools/scopt`, branch o4 at `b35cdb6`. Paths below are
  relative to `tools/scopt/` unless they start with `/`.
- QEMU: `/data/fastdyn-work/qemu-fu`, treb-wip at `aacca05`. `supercache.c` means
  `tcg/supercache.c`.
- Measurements and design: `/data/scfix-campaign/TIMING.md` (R1), `/data/scfix-r2/TIMING.md`
  (R2), `/data/scfix-o4/*-SUMMARY.md`, and `/data/scfix-o4/design/{O4-design.md,inventory.md}`.

Scope:

- The passes `arena_slide` (registered as `align-trace-pages`), `cluster` (`cluster-hot`),
  `hotcold` (`hotcold-partition`), `icount_entry_sfb`, `identity`, `llvm_layout`
  (`llvm-cache-layout`), `move_units`, `order_cold`, `page_layout`
  (`page-aware-local-layout`) and `reverse_layout`.
- The runtime features in `scopt/o4.py` RUNTIME (o4.py:269-298), plus member skip, residual
  and trusted-to-trusted (T->T) chaining, and the other QEMU defaults O4 relies on.
- `scopt/manager.py` and `scopt/manager_b3.py`, and whether they can iterate passes.

## 1. Bottom line

1. **None of the ten passes is in o4r3c, and none applies inside firmware regions.**
   - The o4r3c pass list is o4r3c.toml:52-90.
   - Five are layout passes and one (`hotcold-partition`) only annotates for them. The linker
     refuses any layout placement in an artifact that has firmware regions:
     `firmware_region_terminal_stage`, link.py:1893-1895. Placement reaches the linker
     whenever the candidate has layout edits (manager_b3.py:1072).
   - `align-trace-pages` and `page-aware-local-layout` also refuse B3 mode outright
     (arena_slide.py:41-42, page_layout.py:33-34).
   - `icount-entry-sfb` cannot apply to icount-off sealed captures (icount_entry_sfb.py:350-351).
   - `identity`, `move-units` and `reverse-layout` are controls or tools.
2. **The only measurement of layout on these three workloads is negative.**
   - The arm is C2 `b3l`: B3, then `hotcold-partition`, then `llvm-cache-layout`, then
     `order-cold`, with no regions.
   - Its `llvm-cache-layout` fell back to Pettis-Hansen on all three workloads: "configure the
     pinned LLVM bridge with SCOPT_LLVM_LAYOUT_BRIDGE"
     (`/data/scfix-o4/{packet,uart,cli}-w/opt/b3l/optimize.json`). So b3l measured
     `cluster-hot` + `order-cold` on the B3 tier.
   - Against O4-r1 it is pooled d −2.32%, 95% [−3.47%, −1.17%], slower_than_o4
     (A-SUMMARY.md:15, 121-124).
   - Against the same B3 tier without layout, every point estimate favours no layout:
     s6b3/b3l 0.9814 on packet, and o4_noregions/b3l 0.9720 on uart and 0.9870 on cli
     (A-SUMMARY.md:172, 175).
   - No layout pass has ever been measured with firmware regions or inside them.
3. **The runtime features O4 uses already apply to every region member, including the
   members of nested callees.** These are member skip flags, per-member residual-slot
   chaining and the trusted entry at +15. T->T chaining links regions only at their entry
   descriptors.
   - **The real runtime gap is U7: B3's guarded slots jump to public TBs and never enter a
     region.** O4-class artifacts enter their regions about once each per run: o4r3c uart 28
     entries for 27 regions, cli 14 for 14, and about 53 for c7b3 on packet. p7 enters
     41,700 times (O4-design.md:133-137).
4. **The pass manager cannot iterate.**
   - It runs the pipeline list once, in order (manager.py:773-774). There is no fixpoint or
     repeat construct.
   - Each pass does see earlier passes' output.
   - Overlap refusals make listing a pass twice useless as a fixpoint.
   - The only staged composition is one native host-inline prefix followed by the B3 pipeline
     (commands/optimize.py:667-707).
   - A composed v7 artifact cannot be fed back in. It imports with its regions as metadata
     that no pass reads (importer/import_cache.py:599-629), and profiling disables region
     entry (supercache.c:3348).

## 2. Where code lands in an O4 artifact today

Every item below depends on these facts.

| Fact | Where |
|---|---|
| Public units stay at their captured offsets when no layout pass ran. | link.py:1922-1938 |
| B3-grown units (the chained hot exits) are appended at the arena end, in slot order. | link.py:1924, 1936-1938 |
| B3 SPEC variants come next. | link.py:1952-1960 |
| Firmware regions come after the variants, in `plan.firmware_regions` order (sorted region ids, manager_b3.py:964). | link.py:1997-2003, 1739-1777 |
| Inside a region, members are contiguous in plan order: "The layout keeps plan.members order". | link.py:1757-1758, 1801-1807 |
| Plan order is BFS discovery order from the root (caller, callee, continuation, then a `deque` work list). | analysis/firmware_regions.py:40-74 |
| QEMU requires member code contiguous in member-index order. | supercache.c:1375-1391 |
| A trusted region is entered through its first member. | supercache.c:3265-3273, 4424-4427 |
| Every internal region edge is a patched rel32 jump to the target member's +15, so member order is free apart from member 0. | link.py:1828-1851 |
| Region members copy the **pre-B3** bytes of their sources. B3 guards, SPEC variants and CP/BF/DHO rewrites do not exist inside regions; member skip flags exist because of this. | link.py:1723-1733; supercache.c:880-892 |
| The manager has unit scopes `unit`, `snapshot` and `region`. `region` means a B3 SPEC variant, not a firmware region or its members, so no pass can be scoped to region members. | passes/base.py:74; manager.py:1009-1028 |
| The pricing model already computes a per-member flow for every region (per inline context). This is the input a recursive layout would need. | analysis/firmware_profit.py:9-21, 197 |
| QEMU counts region entries, not member executions. | supercache.c:3383-3386, 3436-3438 |

## 3. Summary table

Row N of this table is detailed as entry 4.N (rows 11-24 are under §5; row 25 is §6).

| # | Item | In o4r3c | o4.py decision | Composes with sealed v7 regions | Recursive today | Evidence |
|---|---|---|---|---|---|---|
| 1 | align-trace-pages | no | excluded, not_applicable_sealed (o4.py:94-96) | no (B3 mode, v7 regions, native fragments) | n/a (whole arena) | 1.003×, 0.992× (docs/passes.md:132-133) |
| 2 | cluster-hot | no | excluded, not_composable (o4.py:99-101) | no (link.py:1893) | no | b3l (PH): −2.32% vs O4 |
| 3 | hotcold-partition | no | excluded, not_composable (o4.py:107-108) | yes in fact (annotation only), but has no effect | no | 1.015× byte-identical prefix (passes.md:601-602) |
| 4 | icount-entry-sfb | no | excluded, not_applicable_sealed (o4.py:88-90) | no (icount off) | n/a | ordinary tier 1.073×; SFB+B3 1.161× (inventory.md:272) |
| 5 | identity | no | excluded, control (o4.py:124) | n/a | n/a | control |
| 6 | llvm-cache-layout | no | excluded, not_composable (o4.py:102-104) | no (link.py:1893); B3-aware | no | fixture prefix 1.023×; b3l fell back to PH |
| 7 | move-units | no | excluded, control (o4.py:123) | no (PLACE) | no | activation-layout 1.074× / 1.096× (ordinary v5) |
| 8 | order-cold | no | excluded, not_composable (o4.py:105-106) | no | no | neutral/inconclusive; part of b3l |
| 9 | page-aware-local-layout | no | excluded, not_applicable_sealed (o4.py:97-98) | no (B3 mode) | no | never measured |
| 10 | reverse-layout | no | excluded, control (o4.py:125) | n/a | n/a | test only |
| 11 | QEMU_SC_SEALED=1 (sealed tier, S1/S2/S7) | yes | required (o4.py:270-272) | is the tier | global | 1.0455× [1.0209, 1.0667] |
| 12 | QEMU_SC_TRUSTED=1 (S5) | yes | required (o4.py:273-274) | required | global | load time only |
| 13 | icount off, can_do_io (S3/S4) | yes | required (o4.py:275-276) | required | global | not separable |
| 14 | capable QEMU and artifact identity (Q14) | yes | required (o4.py:277-283) | required | n/a | aacca05 vs a1bdec6 neutral |
| 15 | member skip flags (Q8/F15) | yes (`member_slot_skip = "on"`) | required (o4.py:188-192) | yes | yes, per member including nested | NEUTRAL (enabling) |
| 16 | per-member residual-slot chaining (Q6) | yes (default) | in the QEMU defaults (docs/O4.md:154) | yes | yes, per member | +4-6% point estimates, within noise |
| 17 | trusted-to-trusted chaining (Q7) | yes (default) | in the QEMU defaults | yes | region entries only | NEUTRAL; 0-1 installs in O4 |
| 18 | incoming chain to a region entry (Q4), and U7 | Q4 yes; U7 not built | Q4 default; U7 "unmeasured, design work" (docs/O4.md:208) | Q4 yes; B3 slots bypass it | no | Q4 1.023× on boot; regions entered about once each |
| 19 | lookup structures: source index (Q2), trusted set (Q3), no-tree (Q5), direct-slot patch (Q13) | Q2/Q3 yes; Q5/Q13 no | Q2/Q3 default; Q5/Q13 unmeasured_low (docs/O4.md:206) | yes | global; one region per root TB | Q2 1.822×; Q5 1.002× |
| 20 | sc6 activation on sealed lookups (Q9) | yes (default) | REQUIRED for correctness | yes (public records only) | no, members carry no sc6 slots | 0.9807 / 0.9958 / 1.0057 |
| 21 | QEMU_SC_SEALED_SKIP_IRQ (S6) | no | excluded, unsafe (o4.py:284-285) | loads, but disarms Q6/Q7 | n/a (the public-code analogue of the trusted ABI) | never timed |
| 22 | QEMU_SC_V7_EXPERIMENT tokens (incl. Q11 no-regions, Q12 disable-chaining) | no | excluded, superseded (o4.py:286-289) | ablations | n/a | linear-source-scan 1.822× slower; disable-chaining 0.960× |
| 23 | QEMU_SC_*_STATS, QEMU_SC_PROFILE*, QEMU_SC_CAPTURE* | no | excluded, diagnostic (o4.py:290-294) | n/a | profiling never enters regions | – |
| 24 | QEMU_SC_RESTORE_* | no | excluded, unmeasured_low (o4.py:295-297) | n/a | n/a | never timed on sealed |
| 25 | pass manager iteration (manager.py, manager_b3.py) | – | – | – | no fixpoint, no re-run construct | – |

## 4. Passes

### 4.1 `align-trace-pages` (passes/arena_slide.py)

- **What it does.** Slides every unit by one 64 B-aligned delta. The delta minimizes weighted
  host-page crossings along profiled `chain0`/`chain1`/`lookup_hit` transfers; relative
  distances are kept (arena_slide.py:9-27, 54-72).
- **In o4r3c.** No. o4.py:94-96 records not_applicable_sealed: "requires a public-only v7
  and no B3 (P1; 1.003x, 0.992x elsewhere)". The same reason is in docs/O4.md:199 and
  O4-design.md:212.
- **Composes with sealed v7 regions.** No, for three independent reasons:
  - it returns `requires_public_v7` when `(version, reserved) != (7, 0)` or `ctx.b3 is not
    None` (arena_slide.py:41-42);
  - it refuses units with native fragments, which host-inline creates, or units already
    moved (arena_slide.py:47-50);
  - its PLACE edits trip `firmware_region_terminal_stage` (link.py:1893-1895).
- **Recursive.** Not meaningful: a uniform slide is a whole-arena transform. Done at the end
  of link it would move region code too, but its score has no profile edges inside regions.
- **Evidence.** 1.003× and 0.992× on ordinary captures (docs/passes.md:132-133;
  inventory.md:260).
- **Verdict.** Not worth porting.

### 4.2 `cluster-hot` (passes/cluster.py)

- **What it does.** Pettis-Hansen chain merge of the units annotated `hot`, weighted by exact
  `chain0 + chain1 + lookup_hit` counts (cluster.py:58-100). It emits one
  `layout_order("hot", …)` (cluster.py:166).
- **In o4r3c.** No. o4.py:99-101 records not_composable: "a layout pass; refused with
  firmware regions (firmware_region_terminal_stage) (P5)", arm b3l.
- **Composes with sealed v7 regions.** No. The pass itself runs in B3 mode, but its
  LAYOUT_ORDER edit makes `has_layout_changes` true (model/snapshot.py:262-264). The B3
  packager then passes `placement` (manager_b3.py:1072), and the linker fails with
  `firmware_region_terminal_stage` (link.py:1893-1895).
- **Recursive today.** No. It orders only public units: `snap.unit_ids()` filtered to hot,
  relocatable and not yet laid out (cluster.py:136-148).
  - Regions are never ordered among themselves (sorted region id).
  - Members are never ordered inside a region (BFS order, §2).
- **Gap.**
  1. The terminal-stage refusal.
  2. No layout of the appended tail (B3-grown units, variants, regions).
  3. No intra-region member order.
- **Change needed (scopt link only, no QEMU change).** This is the recursive form of layout.
  - (a) **Intra-region member order.** Run Pettis-Hansen over each region's member graph.
    Weight it with `member_flow` × exit probability from the pricing model
    (firmware_profit.py:197, 414-488). Keep member 0 (the caller entry, which QEMU requires
    first: supercache.c:3265-3273), and put the cold members of nested callees last. That
    last part is the recursive `order-cold`.
    - Implement it as a permutation of `plan.members` that renumbers `plan.edges`. QEMU's
      contiguity and entry checks then hold unchanged (supercache.c:1354-1391).
  - (b) **Region and tail order.** Place each region next to its hottest public caller, or
    order regions by entry flow. Order the grown units by Pettis-Hansen instead of slot
    order (link.py:1936-1938).
  - (c) **Drop the refusal.** Lay out regions after a placed arena; `cursor` already follows
    the placed extent, link.py:1929-1931. Then `cluster-hot` can run in a composed pipeline.
- **Expected effect.**
  - Top-level clustering is measured slightly negative on these workloads (b3l, §1).
  - Intra-region ordering is unmeasured. It only matters for code that executes inside
    regions, and O4 enters regions about once each per run (§4.18).
  - No QEMU counter reports member executions, so we cannot tell how much code runs inside
    regions.
- **Effort.** Medium: link.py and firmware-plan code. Risk: every arm's bytes change; link
  correctness (edges, member skip masks and descriptors are per member index).

### 4.3 `hotcold-partition` (passes/hotcold.py)

- **What it does.** Annotation only. It classifies each unit as hot, warm, boot_only,
  cold_zero or unknown from exact steady counts (hotcold.py:61-82).
- **In o4r3c.** No. o4.py:107-108 records not_composable: "annotation for the layout passes
  (P12; 1.015x unattributable)".
- **Composes with sealed v7 regions.** Yes, in fact:
  - ANNOTATE edits are not layout edits (model/snapshot.py:262-264), so the terminal-stage
    refusal does not fire.
  - O4 would have to add `ANNOTATE` to `allowed_capabilities`, which o4r3c lacks
    (o4r3c.toml:50).
  - It changes no bytes, so "not_composable" really means "useless without a layout pass".
- **Recursive.** No. It classifies public units only, with no per-member or per-context
  class.
- **Change needed.** None for speed. If 4.2(a) is built, member hotness can come straight
  from `member_flow`.
- **Evidence.** 1.015× on a byte-identical prefix, so it is unattributable
  (docs/passes.md:601-602). In b3l it marked 677 / 215 / 170 units hot.

### 4.4 `icount-entry-sfb` (passes/icount_entry_sfb.py)

- **What it does.** Rewrites the TCG icount entry (32-bit load after a 16-bit store) into a
  store-forwardable form, +16 B per unit (icount_entry_sfb.py:1-45). It is packaged by
  `scopt sfb package` (commands/b4.py:160-190), not by the B3 packager, and it is "not part
  of any B3/B4 pipeline" (icount_entry_sfb.py:39-43).
- **In o4r3c.** No. o4.py:88-90 records not_applicable_sealed: "sealed TBs have no icount
  entry (P13; the ordinary tier's SFB+B3, c5, compute 1.161x)".
- **Composes with sealed v7 regions.** No.
  - The pass rejects any unit without `CF_USE_ICOUNT` (icount_entry_sfb.py:350-351).
  - QEMU rejects trusted members captured with icount (supercache.c:1323-1325).
  - It never runs a region while icount is enabled (supercache.c:3381-3382).
- **Recursive.** Not applicable. Inside regions the entry prefix is already gone: trusted
  members are entered at +15, past the poll (link.py:324, 1844-1845; supercache.c:3196-3200).
  The sealed analogues are the IRQ-poll items (P15, outside this map; S6, item 21).
- **Evidence.** Ordinary tier: compute 1.073× [1.066, 1.079]; SFB+B3 1.161× [1.156, 1.171]
  (inventory.md:272).

### 4.5 `identity` (passes/identity.py)

- **What it does.** No edits; byte-identical round trip (identity.py:15-22).
- **In o4r3c.** No. o4.py:124 records control (P14).
- **Composes and recursive.** Not applicable: it is the import/link/validate correctness
  control (docs/passes.md:617-626).

### 4.6 `llvm-cache-layout` (passes/llvm_layout.py)

- **What it does.** A `ClusterHot` subclass that orders hot units with LLVM Cache-Directed
  Sort through an external bridge (llvm_layout.py:37-41, 101-112). Without the bridge it
  falls back to Pettis-Hansen (llvm_layout.py:44-52).
- **Composes with other passes' output (B3).** It previews B3-grown extents and guarded
  lookups through `materialize` + `NativeLinkerV2.effective_units`, and drops the private
  sources of B3 variants as separate nodes (llvm_layout.py:58-86).
- **In o4r3c.** No. o4.py:102-104 records not_composable: "refused with firmware regions
  (P16; fixture 1.023x prefix); challenger C2 (b3-llvm) is the B3-tier alternative", arm b3l.
- **Composes with sealed v7 regions.** No; same refusal as 4.2 (link.py:1893-1895).
- **Recursive.** No. It has no region or member nodes.
- **Gap and change.** The same three changes as 4.2, feeding members as nodes with
  `member_flow` counts. The LLVM bridge must also be built and pinned
  (`SCOPT_LLVM_LAYOUT_BRIDGE`), because the timed b3l never used it.
- **Evidence.**
  - Fixture prefix 1.023× [1.001, 1.042]; ablation inconclusive; FreeRTOS 0.976×, wide
    (docs/passes.md:649-650).
  - b3l was in fact Pettis-Hansen (§1): −2.32% pooled vs O4-r1. Its best-check is "not
    faster than O4" (A-SUMMARY.md:15); its final row is R3-SUMMARY.md:217.
- **Effort.** Medium to large.

### 4.7 `move-units` (passes/move_units.py)

- **What it does.** Explicit PLACE edits from params (move_units.py:54-76). It is a
  fixture/activation tool, "not a profitability policy" (docs/passes.md:667-679).
- **In o4r3c.** No. o4.py:123 records control: "explicit placements, a tool, not a policy
  (P18)".
- **Composes.** No: PLACE trips the terminal-stage refusal.
- **Recursive.** No.
- **Evidence.** activation-layout on ordinary v5 restores: Nettle AES 1.074×, SLRE 1.096×
  with hand-chosen placements (docs/passes.md:676-678). Its planner is specific to ordinary
  v5 restore traces (analysis/activation_layout.py:1).

### 4.8 `order-cold` (passes/order_cold.py)

- **What it does.** Emits warm / unknown / boot / cold sections in original slot order, with
  the hot section as a fallback (order_cold.py:52-95).
- **In o4r3c.** No. o4.py:105-106 records not_composable: "refused with firmware regions
  (P20; neutral or inconclusive)".
- **Composes.** No (layout edits).
- **Recursive.** No.
  - Exhaustive regions (o4r3c's mode) copy every captured callee, so a region's cold nested
    paths sit between its hot members in BFS order.
  - The recursive analogue is the "cold members last" part of 4.2(a).
- **Evidence.** Neutral or inconclusive (docs/passes.md:704-705). In b3l it moved
  29,847 / 21,903 / 34,108 units to the cold section (b3l optimize.json).

### 4.9 `page-aware-local-layout` (passes/page_layout.py)

- **What it does.** Starts from a dense capture order and makes bounded adjacent swaps to
  cut weighted page crossings (page_layout.py:31-110).
- **In o4r3c.** No. o4.py:97-98 records not_applicable_sealed: "requires no B3 (P21; never
  measured)".
- **Composes.** No.
  - It returns `requires_public_v7` in B3 mode (page_layout.py:33-34).
  - It needs unplaced, unspliced, densely packed units (page_layout.py:39-48), which B3 and
    host-inline both break.
- **Recursive.** No.
- **Evidence.** None retained (docs/passes.md:718-720).
- **Verdict.** Not worth porting; 4.2(a) covers the in-region page-locality idea.

### 4.10 `reverse-layout` (passes/reverse_layout.py)

- **What it does.** A linker stress test: reverses every relocatable unit
  (reverse_layout.py:31-39).
- **In o4r3c.** No. o4.py:125 records control (P25).
- **Composes and recursive.** Not applicable.

## 5. Runtime features

### 4.11 `QEMU_SC_SEALED=1`: the sealed whole-firmware tier (S1, with S2 and S7)

- **What it does.**
  - It sets sealed and exact-PC mode (supercache.c:342-383).
  - Pre-translation clears `CF_PCREL` (accel/tcg/cpu-exec.c:965-985).
  - Replay never JITs.
  - Firmware regions require it: incoming chains into trusted entries are allowed only
    under sealed (supercache.c:4498-4502).
- **In o4r3c.** Yes: the launch's `env_set` has `QEMU_SC_SEALED=1`
  (`/data/scfix-o4/packet-w/arms/o4r3c/launch.json`). o4.py:270-272 records required,
  PROVEN_GAIN.
- **Recursive.** Global. It covers public, grown, variant and region code alike.
- **Evidence.**
  - Compute sealed vs ordinary warm: 1.0455× [1.0209, 1.0667].
  - Hot-TB retention (S2): 1.882 s vs 1.042 s.
  - Cached getenv (S7): 1.44 s to 1.04 s.
  - Sources: inventory.md:224-230.

### 4.12 `QEMU_SC_TRUSTED=1` (S5)

- **What it does.** Trusted record input. It skips guest-hash and record-checksum checks at
  load and at region admission (supercache.c:375-379, 3403-3405).
- **In o4r3c.** Yes (launch env). o4.py:273-274 records required.
- **Recursive.** Global; load-time only.
- **Evidence.** Outside the timed window (inventory.md:228).

### 4.13 icount off and the `can_do_io` encoding (S3, S4)

- **What it does.** Part of the trusted contract.
  - QEMU never enters a trusted region with icount on (supercache.c:3381-3382).
  - QEMU rejects icount-captured trusted members (supercache.c:1323-1325).
  - The sealed producer encodes `can_do_io` (accel/tcg/translator.c:220).
- **In o4r3c.** Yes. o4.py:275-276 records required (`trusted_icount_active`).
- **Recursive.** Global.
- **Evidence.** Not separable from S1 (inventory.md:226-227).

### 4.14 Capable QEMU and artifact identity (Q14, capability table)

- **What it does.** O4 needs counters revision 4 and member skip (aacca05). The v7 runtime
  signature binds the artifact to the capturing binary.
- **In o4r3c.** Yes. o4.py:277-283 records required. docs/O4.md:54-64 explains it.
- **Recursive.** Not applicable.
- **Evidence.** R2 s6_prev/s6 0.9885 / 1.0099 / 1.0299, within noise; aacca05 is needed for
  Q7 and Q8 (inventory.md:250).

### 4.15 Member skip flags (Q8/F15)

- **What it does.**
  - Members copy pre-B3 bytes.
  - A member descriptor takes its goto_tb offsets from the source's public record, where B3
    may have registered guard slots.
  - Flag bit n marks such a slot, and QEMU then neither verifies nor arms it
    (supercache.c:880-892, 1287-1317, 3230-3240).
  - scopt computes the masks per member (link.py:1747-1777).
- **In o4r3c.** Yes: `member_slot_skip = "on"` (o4r3c.toml:90). o4.py:188-192 records
  required, NEUTRAL (enabling).
- **Recursive today.** Yes in coverage. Every member of every region gets its own mask,
  including the nested-callee members of exhaustive regions (link.py:1747-1777).
  - The feature exists because B3 is *not* recursive into regions: members lose B3's guards
    (link.py:1723-1733).
- **Gap.** This is the B3 map's subject. To make B3 recursive, members would have to copy
  post-B3 (grown) bytes, which needs link changes plus QEMU's member verification and arming
  (supercache.c:1489-1520, 3201-3248). Once that is done, skip flags become moot.
- **Evidence.**
  - c7b3_noskip/c7b3: 1.0131 / 1.0274 / 0.9769, within noise.
  - Residual exits: 10 to 1, 7,673 to 2, 10,488 to 3 (R2 TIMING.md:47-50, 118, 185, 256).
  - o4r3c uart: 42 armed / 178 skipped (`/data/scfix-o4/uart-w/fcheck/o4r3c/commands/v7-stats.json`).

### 4.16 Per-member residual-slot chaining (Q6)

- **What it does.** Each member descriptor's residual goto_tb slots (not internal, not
  skipped) are armed and chained after first use (supercache.c:3196-3248, 3265-3277).
- **In o4r3c.** Yes, as a QEMU default (no `no-trusted-exit-chain` token). docs/O4.md:154
  records REQUIRED.
- **Recursive.** Yes: per member, nested members included.
- **Evidence.**
  - R1 p7_nochain/p7: 1.0634 / 1.0401 / 0.9895, within noise.
  - Residual exits: 2.96M to 12 on packet (R1 TIMING.md:13, 118-120).
  - o4r3c residual exits: uart 5, cli 8.

### 4.17 Trusted-to-trusted chaining (Q7)

- **What it does.** A trusted member's residual exit may chain into another region's
  *entry*, at +0, so the destination's poll still runs (supercache.c:4399-4434, 4466-4495).
  - It applies only to region entry descriptors. `tb_lookup` never returns a member
    descriptor (supercache.c:4424-4427).
  - Self-chains take the same path.
- **In o4r3c.** Yes (default). docs/O4.md:154 lists it as a QEMU default.
- **Recursive.** It composes regions with one another at runtime, at root granularity. A
  region cannot be entered at a non-root member, which is inherent: members carry inline
  context and return members.
- **Evidence.**
  - Every not2t pair is within noise (R2 TIMING.md:36-40, 115-117, 182-184, 253-255).
  - c7b3 makes 0 T->T attempts on packet and uart and 1 install on cli.
  - o4r3c: uart 0, cli 1 install (`/data/scfix-o4/cli-w/regions/o4r3c/result.json`).
- **Change.** None.

### 4.18 Incoming chain into a region entry (Q4), and U7: B3 slots that enter regions

- **What it does.**
  - Under sealed, an ordinary TB's goto_tb chains to whatever `tb_lookup` returned
    (supercache.c:4498-4510).
  - For a region root, lookup returns the region entry through `sc7_try_region`
    (supercache.c:3958-3970, 3343-3388).
  - B3's guarded slots (chain-static, chain-returns) hard-wire the public TB's address, so
    they bypass that lookup and never enter regions.
- **In o4r3c.** Q4 is on by default. U7 is not built: docs/O4.md:208 says "unmeasured,
  design work"; see also O4-design.md:221, 473, 1450-1451 and inventory.md:434-439.
- **Recursive.** No. This is the biggest reason regions and B3 do not add up.
  - c7b3: about 53 region entries per packet run, against 41,700 for p7 (O4-design.md:133-137).
  - O4-r2b on packet: 52 entries (B-SUMMARY.md:90).
  - o4r3c uart: 27 regions loaded, 28 entries, 21 in the benchmark window.
  - o4r3c cli: 14 regions, 14 entries.
- **Change needed (scopt link plus QEMU).**
  - In scopt: when a B3 guard target is a region root, chain-* would target that region's
    entry +0. The linker would have to lay regions out before it fixes the B3 stubs; today
    they come after (link.py:1997-2003 vs 1922-1938).
  - In QEMU: region descriptors are built lazily on the first lookup and forgotten on
    `tb_flush` (supercache.c:3265-3290, 4399-4410 comment). A guard jump straight into
    region code would therefore need either eager descriptor instantiation or a
    lookup-through-first-use stub.
- **Effort.** Large. This item is shared with the B3 map.
- **Expected effect.**
  - Unmeasured. Regions alone are worth 1.1259 / 1.1094 / 1.1690 over s6 (p7, R2).
  - On top of B3 they are NEUTRAL, about +1.3% (o4.py:69-73).
  - U7 is the only runtime lever that could make region gains add to B3's.

### 4.19 Lookup structures: source index (Q2), trusted set (Q3), no-tree (Q5), direct-slot index (Q13)

- **What it does.**
  - Q2 is a first-match index from source TB to region (supercache.c:1439-1457).
  - Q3 is an O(1) trusted-membership set.
  - Q5 is a token that skips host-PC-tree insertion.
  - Q13 is an optional QEMU patch for sc6 direct links, not in aacca05.
- **In o4r3c.** Q2 and Q3 yes (defaults). Q5 and Q13 no: docs/O4.md:206 records
  unmeasured_low.
- **Recursive constraint.** The source index keeps exactly one region per root source TB:
  the first in directory order (supercache.c:1449-1455). Recursive region formation that
  emits two regions with the same root TB can only ever enter the first.
- **Evidence.**
  - Q2: 1.822× over linear.
  - Q3: boot 0.980×.
  - Q5: 1.002×.
  - Q13: untimed; affects SPEC links only.
  - Sources: inventory.md:238-241, 249.

### 4.20 sc6 activation on sealed lookups (Q9)

- **What it does.** Arms SPEC links and variants, and unlinks guarded slots on TLB/MPU
  change (supercache.c:2761).
- **In o4r3c.** Yes (default). REQUIRED for correctness: `no-sealed-sc6` reopens the TLB
  hole (inventory.md:245).
- **Recursive.** No. Region members carry no sc6 slots; their B3 slots are skipped (4.15).
- **Evidence.** R1 s6b3_nosc6/s6b3: 0.9807 / 0.9958 / 1.0057, within noise
  (R1 TIMING.md:15, 115-117).

### 4.21 `QEMU_SC_SEALED_SKIP_IRQ=1` (S6)

- **What it does.**
  - At load it NOPs the 11-byte IRQ poll of every public sealed TB (supercache.c:4031-4042).
  - It disarms every member residual slot (supercache.c:3275-3276).
  - It refuses every trusted-source chain (supercache.c:4478-4480).
- **In o4r3c.** No. o4.py:284-285 records unsafe: "changes the interrupt contract and
  disables trusted exit and T->T chaining (S6)".
- **Recursive.** Not applicable. It is the public-code analogue of what the trusted ABI
  already does inside regions (members entered at +15). It cannot coexist with 4.16 or 4.17.
- **Evidence.** Never timed (inventory.md:229). Residual exits rise from 12 to about 2.96M
  on packet p7.
- **Verdict.** Excluded on safety; needs explicit user sign-off.

### 4.22 `QEMU_SC_V7_EXPERIMENT` tokens

- **What it does.** Ablation tokens (supercache.c:1920-1952): `linear-source-scan`,
  `no-tree`, `no-regions` (Q11), `disable-chaining` (Q12), `no-trusted-exit-chain`,
  `no-trusted-to-trusted-chain` and `no-sealed-sc6`. `source-index`, `trusted-set` and
  `incoming-chain` are only reported.
- **In o4r3c.** None set. o4.py:286-289 records superseded.
- **Evidence.**
  - linear-source-scan: 1.822× slower.
  - Q11 regions vs no-regions: compute 1.0321×, boot 1.177×.
  - Q12: disable-chaining 0.960× (inventory.md:238, 247-248).

### 4.23 `QEMU_SC_*_STATS`, `QEMU_SC_PROFILE*`, `QEMU_SC_CAPTURE*`

- **What they are.** Counters and qprof/capture instrumentation (supercache.c:1920, 2839;
  accel/tcg/sc-profile.c:691-722; tcg/sc-capture.c:573-604).
- **In o4r3c.** No. o4.py:290-294 records diagnostic.
- **Recursion-relevant fact.** `sc7_try_region` returns NULL while profiling is active
  (`scp_active`, supercache.c:3348).
  - A profile of an O4 artifact never sees region-internal execution.
  - The O4 profile itself must be a chaining-disabled exact profile of the unoptimized
    snapshot (docs/O4.md:83-84).
  - So there is no "profile the optimized artifact, re-optimize" loop for regions.

### 4.24 `QEMU_SC_RESTORE_*`

- **What it does.** Exception-restore replay hints (supercache.c:3832-3843).
- **In o4r3c.** No. o4.py:295-297 records unmeasured_low: O4 ships no `.restore-hints`, and
  it was never timed on sealed.
- **Recursive.** Not applicable.

## 6. Pass manager: fixpoints and re-running passes

| Question | Answer | Where |
|---|---|---|
| Fixpoint or iteration construct? | **No.** `execute()` walks `self.pipeline.passes` once, in order. Pipeline entries accept only `name`, `version`, `params` and `enabled`, and pipelines only `schema`, `version`, `name`, `description`, `allowed_capabilities` and `pass`. | manager.py:773-774, 313-314 |
| Does a pass see earlier passes' output? | **Yes.** WorkingSet generations: each CANDIDATE's B2 edits make `ws.apply → new_ws`, then the store rebases and invalidates. B3 edits go to the `B3State` overlay, which later passes see through `B3WorkingSetView`. firmware-inline prices against `b3_state.grown_uids()`. | manager.py:897-925 (b3_state at 908, ws.apply at 916), 845-848; manager_b3.py:314-318 |
| Can a pass be listed twice? | **Syntactically yes**: no uniqueness check. But `--params`, `--enable`, `--disable` and `--only` resolve by name, and the `-O4` stage gate lists names. Repeats are also refused on overlap, which rejects the whole proposal: a region emitted twice is `edit_precondition`, a re-claimed slot is `no_free_slot`, and a unit laid out by an earlier pass is a conflict unless the pass declares `overrides={"layout"}`. So a naive repeat is not a fixpoint; a pass would have to skip what it already did. | manager.py:342-366, 272-299, 719-723; commands/optimize.py:447; manager_b3.py:383-385, 517-525; manager.py:1172-1176 |
| Does any pass run over firmware-region members? | **No.** Scope `region` means a B3 SPEC variant (`_run_regions` iterates `b3_state.variants`). There is no member or inline-context scope. | manager.py:1009-1028; passes/base.py:74 |
| Staged composition inside one optimize? | Exactly one native prefix (host-inline) is compiled to in-memory IR, then the B3 pipeline runs on that IR: "one native prefix followed by B3 passes is required". | commands/optimize.py:103-121, 664-707 |
| Re-optimizing an artifact? | v5 artifacts can be re-imported and stacked (B4 on top of SFB, docs/b4.md:175-177). A composed v7 imports its regions as metadata (`firmware-regions.jsonl`) that no pass reads, so regions cannot be re-optimized. Profiling never enters regions (4.23). | importer/import_cache.py:599-629, 976-977 |

**Implication for "recursive".**

- In scopt, the recursive behaviour that exists lives inside firmware-inline's own region
  construction:
  - `build_firmware_region` expands to a fixed point (analysis/firmware_regions.py:1-37);
  - `exhaustive` uses `expand_nested=True`.
- The manager has no generic mechanism. Making a pass recursive therefore means one of:
  - a pass-level loop;
  - a new member scope, where the manager hands each region's members, with their contexts,
    to a pass;
  - a link-time transform over `plan.firmware_regions`.
- A manager-level "repeat until no change" would also need passes to tolerate their own
  earlier edits, which today they do not (the overlap refusals above).

## 7. What would make this subsystem recursive, ranked

1. **U7: B3 slots that enter regions (4.18).** This is the only item here whose upside is
   the full region gain (p7 vs s6, 1.11-1.17×), and O4 forfeits that gain today because its
   regions are entered about once each.
   - Needs scopt link plus QEMU (eager descriptors or a first-use stub). Large.
   - Shared with the B3 map.
2. **Recursive layout (4.2(a), 4.8): order members inside each region by `member_flow`.**
   - Keep member 0 first and put the cold nested-callee members last. Optionally order
     regions and the grown-unit tail by heat.
   - Pure scopt (link.py plus a plan permutation), no QEMU change. Medium.
   - Its value depends on how much execution stays inside regions, which no counter
     measures today. A QEMU stats counter of member executions would be a diagnostic-only
     QEMU change.
   - Top-level layout measured −2.32% (b3l), so do not add top-level `cluster-hot` or
     `order-cold` to O4 without new evidence.
3. **Lift `firmware_region_terminal_stage` (4.2(c))** only together with item 2. On its own
   it just admits the measured-negative top-level layout.
4. **Manager.** No generic fixpoint is needed for this subsystem. If other maps need
   "re-run pass X on firmware-inline's output", add an explicit member scope rather than a
   repeat loop: repeats fail on overlap (§6).
5. **Leave as is.** Member skip (4.15), residual chaining (4.16) and T->T chaining (4.17)
   are already per member or per region. The sealed, trusted and icount contract items
   (4.11-4.14) and sc6 activation (4.20) are global.
6. **Do not pursue.** These are not applicable or are controls: `align-trace-pages`,
   `page-aware-local-layout`, `icount-entry-sfb`, `identity`, `move-units`,
   `reverse-layout`. SKIP_IRQ is unsafe and disarms 4.16 and 4.17. The experiment tokens
   are ablations, and the diagnostics and restore hints are not speed settings.
