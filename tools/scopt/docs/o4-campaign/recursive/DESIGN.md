# O4-rec: scopt O4 with every optimization applied recursively

Design step only. Nothing was built, run, timed or edited outside this file.

**Sources and path conventions**

- **scopt:** `/data/fastdyn-work/fd-int/tools/scopt`, branch o4 at b35cdb6. Paths such as `scopt/link.py` and `docs/O4.md` are relative to it.
- **QEMU:** `/data/fastdyn-work/qemu-fu`, treb-wip at aacca05.
  - `supercache.c` is `tcg/supercache.c`.
  - `cpu-exec.c` is `accel/tcg/cpu-exec.c`.
- **FirmBOLT:** `/data/FirmBOLT/firmbolt` at c448884.
  - `FGR.cpp` is `native/lib/Passes/FirmGraalRecursive.cpp`.
- **Inputs.** The subsystem maps in `/data/scfix-o4/rec/`:
  - `map-regions.md`
  - `map-b3.md`
  - `map-hostinline.md`
  - `map-others.md`
  - `map-firmbolt.md`
- **Receipts:**
  - `/data/scfix-o4/<w>-w/opt/<arm>/optimize.json`
  - the artifact sidecars `.../store/artifacts/*/cache.sc.scopt.json`
  - the replay counters `/data/scfix-o4/packet/timing-R3/o4r3c/heldout-1/regions.json`
  - Every number not taken from a cited file was computed with the one-liners in §7.
- **Triples** are packet / uart / cli.

## 0. Summary

1. **Definition.** O4-rec is one scopt pipeline that meets two conditions.
   - **(a) Every optimization reaches all emitted code.** Every optimization scopt has runs on every kind of code scopt emits:
     - public TBs;
     - B3-grown TBs;
     - SPEC variants;
     - firmware-region member copies at every inline depth, the entry member included;
     - helper splices.
   - **(b) New code gets optimized too.** Every optimization that creates code hands it to every other optimization, to a bounded fixpoint.
   - **FirmBOLT -O4 is the model.** It is a transitive inliner whose inlined copies are already optimized before inlining, get context folding during inlining, and get the late passes after it (§1.1).
   - **The default is "in".** An item is left out only with measured harm or a proven impossibility (§2).
2. **What is already recursive in o4r3c is call expansion, and only that.**
   - Priced exhaustive firmware-inline (A5) expands nested calls transitively, with no depth bound (`scopt/analysis/firmware_regions.py:29-34`, `:141-151`).
   - o4r3c reaches depth 5 / 5 / 5, with 360 / 43 / 32 nested calls and 7,052 / 1,228 / 1,470 members.
   - Nothing else reaches inside a region:
     - members copy the pre-B3 bytes (`scopt/link.py:1725-1731`, `:2001-2002`);
     - the region is admitted or refused as a whole (`scopt/passes/firmware_inline.py:300-304`);
     - no pass runs on members (`scopt/manager.py:1009-1028`, scope `region` = SPEC variant).
3. **Two corrections to earlier assumptions** (§1.4):
   - **(i) The U7 "bypass" premise is a counter artifact.** A B3 GENERAL slot whose target is a region's entry PC already chains into the region (`cpu-exec.c:1111-1149`, `supercache.c:3962-3969`, `:4498-4507`). `region_entries` counts only lookup misses (`supercache.c:3385`).
   - **(ii) B3's work on region-entry sources is dead code.** `lookup_entry` returns the region before it reaches the public TB (`supercache.c:3962-3971`), so a region-entry source's public code never runs while trusted regions are loaded. Of chain-static's guarded units, 44 of 256, 23 of 50 and 13 of 43 are region-entry sources (§7).
     - On packet, 44 capped slots are therefore dead, while 16 profitable plans were refused by the same cap.
     - This is the clearest case for making B3 recursive, that is, copying post-B3 code into members (stage 3).
4. **Plan: six stages in dependency order** (§3).
   - **Stage 1, config only:**
     - A1 (`residual_goto_tb = "chained"`) on exhaustive regions;
     - a repeated chain-static entry, which lifts the cap with no code;
     - an "all-config" sibling that adds the measured-neutral cleanup passes;
     - a no-code perf attribution.
   - **Stage 2, scopt-only pricing and selection:**
     - a nested gate and best-of-three plans for exhaustive;
     - B3-aware pricing per member;
     - the cap lift in code;
     - BL discovery from training evidence;
     - the FirmLooper union;
     - gdb-free host-inline.
   - **Stage 3, B3 inside members:** a linker change plus one QEMU member flag. Rewrites alone need no QEMU change.
   - **Stage 4:** closure extensions and recursive intra-region layout.
   - **Stage 5:** context specialization and cleanup on member copies, and C1/C2 helper partial evaluation.
   - **Stage 6:** variant recursion, a B3 fixpoint, and side-entry regions. Each needs evidence first.
5. **Build first: `o4rec1`** (exact TOML in §4) = o4r3c + A1 + the chain-static repeat. Build its two siblings in the same round.
   - **Expected (§4.2):**
     - packet faster by about 1 to 2%;
     - uart slower by about 1 to 2.5%;
     - cli the same bytes as o4r3c.
   - Under the standing rule that is probably a packet-specific option, not an O4 adoption.
   - The general win needs stage 2b (B3-aware pricing) and stage 3 (B3 in members). Stage 1 measures the mechanism and supplies their targets.

## 1. Definition

### 1.1 The rule and the FirmBOLT analogue

**The user's words, in order:**
- "Whatever the best speed optimizations are there MUST BE included in O4" (`scopt/o4.py:3-5`).
- "Win in most cases is fine" (`/data/scfix-o4/driver/AMENDMENT-F-most-cases.md:7`).
- "even without profile-guided firmlooper should help identify hot regions".
- "the key is to have all our optimizations in recursive manner thats it".

**FirmBOLT -O4 is "one switch for recursive FirmGraal with frame-slot promotion"** (c448884). Its recursion has four properties:

| FirmBOLT property | Where | What it means for scopt |
|---|---|---|
| **R1. Transitive inlining.** Every call copied into a caller becomes a new candidate, with depth+1 and a chain of ancestors. | `FGR.cpp:459-493` | firmware-inline expands nested calls in every inlined callee |
| **R2. Copies of optimized bodies.** Per-function passes run before the inliner, so FirmGraal copies cleaned bodies. | `map-firmbolt.md` §2.4 (patch `:9762-9764`) | members copy **post-B3** code (B3 guards, rewrites, splices) |
| **R3. Context re-applied in each copy.** Known argument registers fold branches inside the copy and are passed to nested candidates. | `FGR.cpp:167-170` and following (`foldInlined`) | specialization inside members from facts of their inline context |
| **R4. Per-candidate pricing and budgets.** Each nested candidate is ranked by weight / marginal bytes, with depth ≤ 4, ≤ 256 steps and a flash budget. | `FGR.cpp:36-43`, `:338-341`; `map-firmbolt.md` §2.3 | each nested call priced on its own, plus explicit safety budgets |
| **R5. Late passes on the merged code.** | `map-firmbolt.md` §2.4 (patch `:9767-9834`) | layout and cleanup on member copies |
| **(R6, scopt-specific.) Fixpoint.** Passes that make new code re-run on it until stable. | – | B3 on variants and members; repeat until `B3State` is stable |

**FirmBOLT's own recursion delivered transitive inlining, not folding.**
- It made 144 inlines at -O0, 29 of them nested, and folded 0 branches (`map-firmbolt.md` §2.6).
- So R3 is expected to be worth little here as well. Its value comes from new fact sources, not from the mechanism.
- The packet, uart and cli images were already built with FirmBOLT -O4 (`/data/scfix-o4/design/O4-design.md:237-238`). Guest-level recursion is therefore already done. O4-rec is about the host-code and TB level.

### 1.2 The code objects every optimization must reach

| Object | Created by | Optimizations that reach it today |
|---|---|---|
| Public TB | capture; host-inline splices it pre-B3 (`scopt/commands/optimize.py:664-700`) | all public-unit passes |
| B3-grown TB (public TB + guard stubs) | chain-returns, chain-static (`scopt/link.py:1865-1883`) | – (nothing runs after B3) |
| SPEC variant C' | specialize-returns | CP, BF, DHO, region-state-* (`scopt/manager.py:1009-1031`) |
| Region member copy at depth d ≥ 0 | firmware-inline, the terminal stage (`scopt/link.py:1893-1895`) | **host-inline splices only**, because they are pre-B3. Copies are pre-B3 (`scopt/link.py:1725-1731`, `:1913`, `:2001-2002`) |
| Helper splice | host-inline | none: splices are barriers to other passes (`map-hostinline.md` §3) |

### 1.3 O4-rec, concretely

1. **R1: in, keep.** firmware-inline `mode = "exhaustive"` with expansion to a fixed point (`scopt/analysis/firmware_regions.py:22-189`).
2. **R4, recursive pricing (stage 2a/2b).**
   - Each nested call in each context is priced, and the best of the gated, ungated and flat plans is kept.
   - Residual exits are priced per member, aware of B3. This replaces the all-or-nothing gate (`scopt/passes/firmware_inline.py:300-304`) and the region-wide chainability predicate (`scopt/link.py:359-380`).
3. **R2, post-B3 member copies (stage 3).**
   - Members are laid out from the effective units (`eff`, `scopt/link.py:1919`) instead of `by_uid` (`:2001-2002`).
   - Then chain-returns, chain-static, every PURE_REWRITE (B4 / cleanup) and host-inline apply at every inline depth, the entry member first.
4. **R3, context specialization in members (stage 5).**
   - CP, BF, DHO and region-state-forward/dse run on each member copy, under facts carried along internal edges. No runtime guard is needed, because non-entry members are entered only by internal edges (`supercache.c:3253-3259`).
   - Helper partial evaluation (C1/C2) runs pre-B3, so every copy inherits it.
5. **R5, recursive layout (stage 4d).** Member order inside each region follows `member_flow`, with cold nested members last.
6. **R6, fixpoint (stage 6).** Clones of grown units, slots on variants, and a manager loop that repeats the B3 group until `B3State` stops changing (`scopt/manager.py:773-777` runs each pass once today).
7. **Selection.** The profile prices every root. FirmLooper's static task loops become a prior for roots the profile cannot price (stage 2e).
   - A profile-free O4 remains impossible, because B3 needs the profile (`O4-design.md:152`).

### 1.4 What the runtime already does: two corrections

**(i) GENERAL B3 slots already enter regions (U7 premise not supported).**
- How a guard slot enters a region:
  - A guard slot that is not armed exits with `last_tb`/`tb_exit` (guard template, `scopt/guard.py:13-16`).
  - The main loop calls `tb_lookup` (`cpu-exec.c:1111`), which reaches `lookup_entry`.
  - `lookup_entry` calls `sc7_try_region` first and returns the region's entry (`supercache.c:3962-3969`).
  - The loop then chains the slot to that entry (`cpu-exec.c:1145-1149`), which is allowed when sealed (`supercache.c:4498-4507`).
- `tb_lookup` caches the entry in the jump cache (`cpu-exec.c:263`), so `helper_lookup_tb_ptr` also returns it (`cpu-exec.c:405`).
- `region_entries` is incremented only inside `sc7_try_region` (`supercache.c:3385`), so it counts lookup misses, not executions.
- The o4r3c packet replay shows 2,817 such entries but only 56 `chain_installs_trusted_destination`.
- The real gaps are narrower:
  - SPEC slots are armed straight to their variant (`supercache.c:2485-2521`);
  - there are no side entries into non-entry members, by the single-entry design (`supercache.c:1442-1457`).

**(ii) B3 on region-entry sources never runs.**
- `lookup_entry` returns the region (`supercache.c:3962-3969`) before it reaches the public TB at `:3971` and before sc6 activation at `:4115-4121`.
- `sc7_try_region` declines only while profiling, single-stepping, with break- or watchpoints, with icount, or with the `no-regions` token (`supercache.c:3347-3357`, `:3381-3382`).
- So in a timed O4 run the public code of a region-entry source, with its B3 guards, is never executed. The pre-B3 entry member runs instead.
- How much of B3 this affects:

  | | packet | uart | cli |
  |---|---|---|---|
  | region-entry sources that are B3-grown | 44 of 53 | 23 of 27 | 13 of 14 |
  | chain-static guarded units that are region-entry sources | 44 of 256 | 23 of 50 | 13 of 43 |

- SPEC links whose R is an entry source can never arm, for the same reason.

## 2. Every optimization: O4-rec decision

**Legend**
- **O4-rec:** "Yes" means in O4-rec at the stage shown. "No" needs measured harm or a proven impossibility, and the evidence is cited.
- **QEMU:** whether the QEMU runtime must change.
- **Effort:** none / small / medium / large.

### 2.1 Region formation and pricing (firmware-inline)

| # | Optimization | O4-rec | Recursive today? | Change needed | Effort | QEMU | Risk | Stage |
|---|---|---|---|---|---|---|---|---|
| 1 | Recursive nested expansion over (uid, inline-context) | Yes | **Yes, transitive and unbounded**. The builder sets no depth, member or growth bound (`firmware_regions.py:29-34`). Nested callee and continuation are expanded (`:141-151`); branches stay in context (`:161-177`); a recursion guard turns cycles into side exits (`:134-140`) | none for depth; closure gaps are rows 10-12 | none | no | context-cloning growth: unpriced exhaustive had 37k / 17k / 18.5k members (`O4-design/inventory.md:293`); bounded by row 3 pricing and §5 budgets | in (o4r3c) |
| 2 | Exhaustive root selection, priced (A5) | Yes | every discovered call is a root (`firmware_policy.py:66-67`); each root's closure is all-or-nothing (`firmware_inline.py:300-304`) | rows 3-4 | none | no | deep regions; sc6 arrays must end before 0xFF00 (`scopt/formats/sc_v6.py:107-110`) | in |
| 3 | Per-nested-call profit gate (`NestedProfitExpansion`) | Yes | no for exhaustive. hot-loop's gate is one level deep, greedy, and cached per call site whatever the context (`firmware_profit.py:609-652`; cache `:617-620`; probe `expand_nested=False` `:630-632`) | `_exhaustive` returns the gate when priced; decisions keyed by (callsite, context); the probe adds the memoized gains of the callee's own nested calls (bottom-up, FirmBOLT R4) | small | no | a context-insensitive gate prunes deep wins, so row 4 keeps the ungated plan | 2a |
| 4 | Best-of-plans gate: gated recursive, ungated recursive, flat | Yes | hot-loop only (`firmware_inline.py:304`, `:418-466`) | `flat_fallback` whenever priced; three candidates ranked by `rank()` (`:444-451`) | small | no | WHOPR pricing order shifts admissions | 2a |
| 5 | B3-aware residual pricing, per member (F12) | Yes | **no, region-wide**: one grown member makes every residual goto_tb a 40 ns dispatch (`firmware_profit.py:485-491`; `link.py:359-380`; `firmware_profit.py:218-239`). Credits internal lookups at full `lookup_ns` although B3 guards them publicly (`:36-46`, `:467-478`) | (a) goto_tb chained per armed slot; (b) internal-lookup credit = guard where the public exit is B3-guarded; (c) residual goto_ptr on a B3-guarded public tail costs lookup − guard while members are pre-B3 | medium | no | uncalibrated constants (`firmware_profit.py:101-107`, F14). A1 lost on uart (`A2-SUMMARY.md:15`) | 1 (A1 approximation), 2b |
| 6 | Trusted→trusted exits priced at one poll | Yes | conservative full dispatch (`firmware_profit.py:47-50`) though QEMU chains them to +0 (`supercache.c:4411-4443`) | price "blocked" flow at `poll_ns` when counters revision ≥ 4 | small | no | low | 2b |
| 7 | Covering-first pricing and captured flow | Yes | yes (`firmware_inline.py:65-81`, `:412-417`, `:489-491`) | none | none | no | the order is an implicit knob | in |
| 8 | Trusted ABI: +15 internal entry, direct jmp edges, guarded returns | Yes | yes, every depth (`link.py:1829-1852`; `firmware_regions.py:153-160`) | none | none | no | – | in |
| 9 | Member descriptors, residual-slot chaining (Q6), member skip (Q8) | Yes | yes, per member (`link.py:1748-1756`; `supercache.c:3229-3248`) | the skip bits are replaced by a "B3 copy" flag at stage 3 | none | (stage 3) | – | in |
| 10 | Single-instruction BL discovery from training evidence | Yes | no: such BLs are `unverified_nested_call_exit` side exits (`firmware_regions.py:100-108`), 400 / 116 / 78 rows; `verified_entry_pcs` is only fed in FirmLooper modes (`firmware_inline.py:232-238`; `firmware_calls.py:51-64`, `:162-167`) | `verified_entry_pcs` = single-insn units with exact steady executions > 0; feed WHOPR the same set (`firmware_inline.py:254-258`) | small | no | changes the shared firmware-calls analysis | 2d |
| 11 | Root-continuation expansion | Yes | no: the root enqueues only the callee (`firmware_regions.py:62-70`) | enqueue the continuation in context (); **edges into member 0 stay residual** | medium | no. QEMU already self-chains a residual exit to the entry at +0, with its poll (`supercache.c:4411-4443`). A +0 internal edge would need a new edge flag, since every trusted edge must carry SKIP_POLL (`supercache.c:1418-1423`) | an internal back-edge at +15 would make a poll-free task loop: forbidden | 4a |
| 12 | Indirect-call (`blx reg`) promotion | Yes | no: side exit (`firmware_regions.py:100-108`), 167 / 32 / 15 goto_ptr rows | guarded call edge to the dominant `lookup_hit` target through the private guard stub (`link.py:1665-1716`) | medium | no (edges are kind-1 patched fields, `supercache.c:1420`) | mispredicted targets pay guard + lookup | 4b |
| 13 | Return-guard elision (FirmBOLT intact-return analogue) | Yes, where proven | no: every return is guarded (`firmware_regions.py:153-160`) | Thumb LR / stacked-slot proof; drop the guard in `_firmware_member_units` (`link.py:1686-1701`) | medium | no | a wrong proof returns to the wrong context | 4c |
| 14 | Recursive layout: member order by `member_flow`, cold nested last; region and grown-tail order | Yes | no: BFS plan order (`link.py:1755`; `map-others.md` §2) | permutation of `plan.members` with renumbered edges; the entry stays first (`supercache.c:3265-3273`, `:1375-1391`) | medium | no | top-level layout measured −2.32% (b3l, `A-SUMMARY.md:15`); only the intra-region form is in | 4d |
| 15 | FirmLooper v3 roots ∪ exhaustive (`exhaustive-tasks`), FirmLooper as prior | Yes | n/a: priced v3-only roots had 0 training calls (`map-regions.md` §5) | copy the `_hot_loop_tasks` pattern (`firmware_policy.py:299-348`) on `_exhaustive`; admit `profit_estimate_unknown` task-loop roots within a byte budget | small | no | about 0 as priced today | 2e |
| 16 | hot-loop / hot-loop-tasks as the root selector | No (superseded) | – | – | – | – | measured slower than A5: o4r3c +1.06% pooled over O4-r1 (`R3-SUMMARY.md:74`); in F, o4_tasks −0.56% vs o4r3d (`F.json`) | – |
| 17 | ISB side exit (I1) | not a speed item: fidelity | applies at every depth when set (`firmware_inline.py:309-323`) | – | – | – | absent in o4r3c and o4r3d, as decided in F | – |
| 18 | WHOPR planner | Yes | n/a | none | none | no | – | in |
| 19 | U7 side entries into non-entry members (multi-entry regions) | Yes, if stage-1 perf shows the public copies of member sources are hot | GENERAL into entries already works (§1.4) | extra source-index entries, per-member entry descriptors and poll proof (`supercache.c:1442-1457`, `:1533-1604`) | large | **yes** | foreign contexts need every internal return guarded | 6 (conditional) |

### 2.2 B3 passes, B4 and cleanup

| # | Optimization | O4-rec | Recursive today? | Change needed | Effort | QEMU | Risk | Stage |
|---|---|---|---|---|---|---|---|---|
| 20 | chain-returns | Yes | public only; members drop its slots (`link.py:1725-1731`; skip `supercache.c:3237-3240`) | L1 (row 34) | large | yes | – | in; recursive at 3 |
| 21 | chain-static + A3 ranking | Yes | as row 20. 44 / 23 / 13 guarded units are dead entry sources (§1.4) | L1 | large | yes | – | in; recursive at 3 |
| 22 | Lift the 256-unit cap | Yes | n/a. It binds on packet (272 eligible, 16 `slot_budget`); cap at `chain_returns.py:51`, `:66`, `:147`, `:308-310`; manager `manager.py:1126-1130` | stage 1: a second chain-static entry. Duplicate names load (`manager.py:340-367`) and the cap is counted per proposal. Stage 2c: the descriptor budget follows the `max_units` param; O4 sets 512 | small | no (about 2,000 sc6 slots fit, `sc_v6.py:107-110`) | gates and receipts that assume unique pass names | 1, 2c |
| 23 | specialize-returns (SPEC) | Yes | public un-grown C only (`manager_b3.py:446-451`); links whose R is an entry source never arm (§1.4) | guard-free specialization of member copies (L2, row 36); variant recursion (row 37) | large | L2 no; L3 yes | – | in; 5, 6 |
| 24 | constant-propagation | Yes (bundle) | variants only (`manager.py:1009-1031`) | member-copy view (row 36) | large | no | mixed harm evidence, not consistent: fixture 0.943× (`o4.py:80-82`); A2 X2 removal +0.58% [+0.02, +1.14] unadjusted (`A2-SUMMARY.md:16`); but o4r3c vs o4r3d +0.10% (`F-SUMMARY.md`) | in; 5 |
| 25 | branch-folding | Yes | variants only | row 36 | large | no | fixture 0.985× (`o4.py:83-84`) | in; 5 |
| 26 | dead-host-ops | Yes | variants only; a local fixpoint of at most 16 rounds | row 36 plus liveness across members | large | no | – | in; 5 |
| 27 | specialize-edges | **No** | – | reuse its `solve_program` as a fact source for row 36 | – | – | **measured harm**: fixture ablation 0.927× [0.902, 0.992] (`o4.py:109-110`) | – |
| 28 | eliminate-state-sync (B4) | Yes | public only; members pre-B3 | stage 3a: un-grown members copy rewritten bytes | medium | **no** (flags stay 0, `supercache.c:1311-1317`; trusted member code is not hashed, `:1648-1650`) | +0.14% [−0.05, +0.56] (`o4.py:111-112`); never run on sealed | 1 (`o4rec1x`), 3a |
| 29 | local-scalar-cleanup | Yes | public only | 3a | medium | no | 0.992× [0.967, 1.009], not harm (`o4.py:113-114`) | 1x, 3a |
| 30 | native-scalar-cleanup, region-state-forward, region-state-dse | Yes | SPEC variants only | row 36 on member copies | large | no | 0.964× [0.939, 1.011], 0.976×, 0.977× [0.940, 1.001]: intervals contain 1 (`o4.py:115-119`) | 1x, 5 |
| 31 | fold-exit-pc-update | Yes, as the first B3-stage pass | refuses once any B3 state exists (`scopt/passes/pc_update.py:18-24`), so it must run first | 3a carries it into members | small | no | 0.999× (`o4.py:120`) | 1x, 3a |
| 32 | irq-entry-mem-compare | **No** | – | – | – | – | **impossible**: its shape check needs a store sealed TBs lack (`o4.py:91-93`). The trusted +15 entry already drops the poll inside regions | – |
| 33 | retire-ordinary-exits | **No** | – | – | – | – | **impossible to compose**: exclusive pipeline (`manager.py:927-930`); returns exits to dispatch, against chaining (`o4.py:121-122`) | – |
| 34 | **L1: B3 inside member copies** (post-B3 bytes) | Yes | **no** (`link.py:1725-1731`, `:2001-2002`; QEMU skip, `supercache.c:880-892`) | §3 stage 3 | large | **yes**: one member flag | slot shape is fatal at load (`supercache.c:1525`); mapping changes are ignored for trusted regions (`supercache.c:3488-3492`) | 3 |
| 35 | member_slot_skip | Yes until stage 3 | the opposite of recursion (`link.py:341-357`) | replaced by the B3-copy flag | none | – | – | in |
| 36 | **L2: region-context specialization and cleanup on members** | Yes | no: members are relocated copies (`link.py:1816-1824`) | member-copy view like `VariantView` (`manager_b3.py:779-826`); facts on internal edges; patch list per member; V12 hostsym proofs | large | no | soundness relies on members having no other entry | 5 |
| 37 | **L3/L4: variant recursion and B3 fixpoint** | Yes | no (`manager_b3.py:446-451`, `:499-500`; one pass, `manager.py:773-777`) | allow clones of grown C; sc6 slots keyed by variant; manager repeat-until-stable for the B3 group | medium | **yes** (variant slot registration, `supercache.c:2490-2491`) | about 0 here: 0 / 9 / 1 variants | 6 |

### 2.3 Host side

| # | Optimization | O4-rec | Recursive today? | Change needed | Effort | QEMU | Risk | Stage |
|---|---|---|---|---|---|---|---|---|
| 38 | H1 selected-root host-inline | Yes | depth 1. It is the pre-B3 prefix, so every member copy inherits it (`optimize.py:664-700`; `map-hostinline.md` §3); 1 / 0 / 0 sites | stage 2f: replace the gdb counted replay with exact TB-entry counts (`host_inline.py:223-254`, `:432-442`) | small | no | gdb replay flakes and is non-deterministic until 2f (`O4-design.md:401-404`) | in; 2f |
| 39 | Bottom-up selected-root fold (depth-N leaf folding) | Yes | depth 1 (`selected_helper.py:39-43`; `native_callee.py:211-213`) | memoized `fold(body)` over the full graph with an SCC guard and a 32,768 B per-site cap | small | no | about 0 sites (hot roots have frames) | 2g |
| 40 | UDIV guarded lowering in trusted mode | Yes | no (`host_inline.py:404-417`) | route `is_udiv_binding` to the guarded form | small | no | ≤ 0.2% | 2g |
| 41 | Full helper-graph copy: H2 unbounded, A4, A6 | **No** | copies, removes no nested call | – | – | – | **measured harm**: H2 c7b3/w7b3 0.7235 / 0.6180 / 0.7813 (`docs/O4.md:195`); A4 −3.63% [−4.84, −2.43] (`A-SUMMARY.md:17`); A6 −4.51% [−5.08, −3.94] (`H-SUMMARY.md:18`) | – |
| 42 | C1/C2 partial evaluation of the MSR BASEPRI pair | Yes | n/a. It is in place and pre-B3, so it is inherited at every depth | build after the P0 hand-patched probe and the F13 hostsym fix (`docs/design/helper-inlining-design.md:47-64`, `:95`); revalidate for sealed (the design is written for v5, `:59`) | large | no | MODEL only: +1.3% compute (`:81`) | 5b |
| 43 | Framed recursive helper lowering + specialization | Yes, gated | `extreme` is plugin-only (`map-hostinline.md` §2) | generalize `_emit_frames`; new certificate | large | no | without specialization it is A4 again (−3.6%) | 6, gated on 42's P0 |
| 44 | Never inline `helper_lookup_tb_ptr` | rule | – | keep the 32,768 B cap on any helper graph | – | – | H2 copied its 50,345 B graph and removed the lookup tails regions need (`O4-design.md:731`; `inventory.md:323`) | rule |

### 2.4 Runtime

| # | Optimization | O4-rec | Notes |
|---|---|---|---|
| 45 | Sealed, trusted, icount off, capable QEMU, identity (S1-S5, Q14) | Yes, required | `o4.py:269-283` |
| 46 | Q2 source index, Q3 trusted set, Q4 incoming chain, Q6 residual chaining, Q7 T→T chaining, Q9 sc6 activation | Yes (QEMU defaults) | Q7 and Q6 already compose regions per member and per entry (`supercache.c:4411-4443`) |
| 47 | `QEMU_SC_SEALED_SKIP_IRQ` | **No** | **unsafe**: disarms Q6 and Q7 (`o4.py:284-285`) |
| 48 | `QEMU_SC_V7_EXPERIMENT` tokens, Q5 no-tree | **No** | ablations (`o4.py:286-289`) |
| 49 | Q13 direct-slot patch | No for now | unmeasured_low; SPEC links only (`docs/O4.md:206`) |
| 50 | Diagnostic counters for chained region entries and lookups from member code | not a timed setting | stage 2h if perf (§3.1) is not enough; QEMU stats only, off in timing |

## 3. Staged plan

### 3.0 Rules common to every stage

**Correctness gate G0** (`O4-design.md:1108-1121`). A failure on any workload drops the arm.
- `scopt optimize` exits with rc 0.
- The functional check passes:
  - 16 words and 1 BENCH line;
  - `counters_revision == 4` (5 after stage 3) and `counter_problems == []`;
  - every poll entry verified;
  - `chain_skipped_trusted_poll_unverified == 0`;
  - sc6 `invariant_violations == 0` and `flush_resets == 0`.
- The loader requirement is `+sc6+member-skip` (plus the B3-copy flag after stage 3).
- `x_b3.firmware_region_links` has 0 refusals.
- The QEMU sha256 equals the artifact's.

**Regions replay** (`O4-design.md:1123-1126`). Record, per case:
- `regions_loaded`, `region_entries`, `region_activations`;
- `chain_installs_trusted_destination`;
- `trusted_residual_slots_armed` and `trusted_residual_slots_skipped`;
- `trusted_residual_exits`.

**Build budget.**
- Abort above 8 GB RSS or 60 minutes (`O4-design.md` §3.2).
- The o4r3c baseline is 3:03 / 2:04 / 2:55 and 1.92 / 1.40 / 2.05 GB (`/data/scfix-o4/<w>-w/opt/o4r3c/time-v.txt`).

**Same-bytes check.** `cmp` each arm's cache.sc against o4r3c. An equal pair is an alias and is not timed.

**Timing and decision.**
- Use the registered tools unchanged: `driver/decide_r3.py`, which imports `decide.py`. Use the R3/F layout: blocks 40 / 80 / 40, with a W and W-aa reference.
- W is the adopted O4, o4r3d (`F-SUMMARY.md:5`).
- Every campaign also times O4-r1 (`o4`) and o4r3c, so each stage reports head-to-head results against both.
- **Adoption** follows the standing contract (`AMENDMENT-F-most-cases.md:31-35`):
  - the pooled one-sided test against W rejects at 0.05;
  - the arm is not shown slower (UB95_w < 0) on a majority of the workloads where its bytes differ.
- A one-workload gain with no majority is recorded as a workload-specific option, not adopted.

**Ablation discipline.** A sub-change with a history of a workload-specific loss (A1) gets its own ablation arm in the same campaign.

### 3.1 Stage 1: config only, plus a no-code perf attribution

**Arms.** Each is built with the o4r3c command (`/data/scfix-o4/<w>-w/opt/o4r3c/command.txt`), with `--pipeline` pointing to the new TOML:
- `o4rec1` (§4.1) = o4r3c + A1 (`residual_goto_tb = "chained"`) + a second chain-static entry.
- `o4rec1a` = `o4rec1` without A1. It isolates the cap repeat.
- `o4rec1x` = `o4rec1` + the cleanup passes that run in a sealed B3 pipeline today (§4.3): rows 28-31.

**Validity checks** (beyond G0):
- **`o4rec1a`:**
  - uart and cli cache.sc are byte-equal to o4r3c's, since the cap never binds there (50 / 43 units).
  - On packet, the first chain-static receipt equals o4r3c's (256 units, `refused:slot_budget` 16).
  - The second chain-static touches ≤ 16 units and refuses 0 by `slot_budget`.
- **`o4rec1`:**
  - The firmware-inline receipt says `residual_goto_tb: "chained"`.
  - Admitted roots are a superset of o4r3c's, about +16 on packet (including the CRC root, 2,317,210 calls) and about +7 on uart. The prediction is in §4.2.
  - cli bytes equal `o4rec1a`'s. The prediction is 0 new cli roots. If cli bytes differ, record why.
- **`o4rec1x`:**
  - Record each added pass's outcome and reason.
  - If optimize fails, remove the added passes one at a time and record the failing pass as "impossible today" in §2.
  - Under `o4rec1x`, members still copy pre-rewrite bytes. Their effect is public-only until stage 3a.

**Perf attribution** (no code, outside the timing window; one held-out run per arm).
- **Why it works without QEMU changes.** The code arena is mapped at the artifact's fixed address (`MAP_FIXED_NOREPLACE`, `supercache.c:414-436`), so host IPs map 1:1 to cache.sc offsets.
- **What to classify.** Sort `perf record` samples (cycles, with LBR call stacks if the CPU has them) into:
  - public units: entry sources versus other member sources versus non-members;
  - B3-grown units;
  - variants;
  - region members by inline depth, the entry member separately;
  - `helper_lookup_tb_ptr`, split by its caller's arena class via LBR;
  - the main loop.
- Member ranges come from the v7 directory, and depth from the sidecar's `x_b3_manager.firmware_regions[].members[].inline_context`.
- **Questions it must answer:**
  - (a) What share of time runs inside regions? This is the ceiling for stages 3-5.
  - (b) How many lookups come from member copies? This is the L1 ceiling: about 6.1 ns per lookup that L1 would chain (`docs/b3-passes.md:215-216`, via `map-b3.md` §8).
  - (c) Is there time in the public copies of non-entry member sources? This is the U7 side-entry question.
  - (d) Is the public code of entry sources at 0%? That confirms §1.4(ii).
- If LBR is unavailable, do stage 2h (QEMU stats-only counters) instead.

**Measurement.** One campaign with the arms `o4r3d` (W), `o4r3d-aa`, `o4`, `o4r3c`, `o4rec1`, `o4rec1a` and `o4rec1x`.

### 3.2 Stage 2: scopt-only pricing, selection and small changes (no QEMU)

| Sub | Change | Files | Tests (tools/scopt/tests/unit) | Gate |
|---|---|---|---|---|
| 2a | Exhaustive gets the nested gate when priced. Decisions are keyed by (callsite, context), with bottom-up memoized subtree gains. Three plans per root: gated recursive, ungated recursive, flat | `scopt/passes/firmware_policy.py:66-67`; `scopt/analysis/firmware_profit.py:578-655`; `scopt/passes/firmware_inline.py:300-304`, `:418-466` | Model monotonicity: for every o4r3c-admitted root, the chosen net is ≥ o4r3c's net for that root. The total `profit_net` is ≥ 21.21 / 11.31 / 1.60 ms (o4r3c receipts). `nested_decisions` and `nested_fallbacks` are present (`firmware_inline.py:587-589`) | Receipts: packet roots 666a8, a59e6 and 3e5a7 are admitted (`map-regions.md` §3.2) |
| 2b | Per-member B3-aware residual and internal pricing (row 5 a-c); T→T priced at a poll (row 6). Pass `view.b3` (`firmware_inline.py:211-214`) into `estimate_region`, not just `grown_uids` | `scopt/analysis/firmware_profit.py:218-239`, `:455-513`; `scopt/link.py:359-380` (pricing only) | (a) a region whose residual goto_tb slots are all armed prices `chained`; (b) an internal lookup on a B3-guarded public exit is credited `guard_ns + poll`; (c) a residual goto_ptr on a B3-guarded tail adds `lookup_ns − guard_ns` per traversal | Receipts list every root that `o4rec1` (A1) admitted and 2b refuses, by workload. The uart losers must be among them |
| 2c | Cap lift in code: the descriptor `max_units` follows the param, and O4 sets 512 for chain-static | `scopt/passes/chain_returns.py:51`, `:66`, `:147`; `scopt/o4.py:209-211` | `max_units = 512` admits all 272 packet plans; the manager budget (`manager.py:1126-1130`) is not exceeded; uart and cli bytes are unchanged | The sc6 arrays fit (`max_ext_entries`, `sc_v6.py:107-110`) |
| 2d | Single-instruction BL discovery from training evidence | `scopt/passes/firmware_inline.py:232-238`, `:254-258`; `scopt/analysis/firmware_calls.py:51-64` | Only units with exact steady executions > 0 are added; the plan for a fixture with such a BL nests through it | `unverified_nested_call_exit` rows drop from 400 / 116 / 78 |
| 2e | `exhaustive-tasks`: exhaustive ∪ FirmLooper v3, with v3 rules only on v3-only roots, `price_after`, and admission of `profit_estimate_unknown` task-loop roots under a byte budget | `scopt/passes/firmware_policy.py:299-348` (pattern), `_BUILTINS :413-422` | Every exhaustive root keeps its plan byte for byte; the union counts are in the receipt | Expected 0 bytes changed today. It exists for completeness and for images where the profile is thin |
| 2f | host-inline `hot_loop_only` accepts exact TB-entry counts; O4-rec drops `--pgo-launch` | `scopt/passes/host_inline.py:223-254`, `:432-442` | The site counts equal gdb's on uart; on packet they are 0.88-1.0× (`map-hostinline.md` §6) | packet still applies `helper_shl_cc` |
| 2g | Bottom-up selected-root fold; UDIV guarded lowering | `scopt/validate_sym/selected_helper.py:39-43`; `scopt/importer/native_callee.py:211-213`; `scopt/passes/host_inline.py:404-417` | Folding refuses SCCs; per-site bytes ≤ 32,768 | 0 to a few new sites |
| 2h | (Only if perf is not enough.) Stats-only counters: chained entries per region, and `helper_lookup_tb_ptr` calls whose return address lies in member code | QEMU `supercache.c:4510-4533`; `cpu-exec.c:384-421` | Off in timing (Q15) | – |
| 2i | Explicit safety budgets, recorded in receipts: at most 4,096 members per region (o4r3c max 971 / 186 / 281) and total members at most 4× o4r3c's. Over budget, refuse the lowest net-per-byte roots first, as FirmBOLT ranks weight / bytes | `scopt/passes/firmware_inline.py` | The budget never binds on the o4r3c inputs | – |

**Measurement.** `o4rec2` = the stage-1 winner + 2a-2g + 2i, against W, `o4`, `o4r3c` and the stage-1 winner. Also include `o4rec2-noA1`, which is 2b's pricing with `residual_goto_tb = "auto"`.

**Target.** Keep A1's packet gain without its uart loss.

### 3.3 Stage 3: B3 inside member copies (L1; FirmBOLT R2)

**3a. Rewrites into members (scopt only).**
- For an un-grown source with PURE_REWRITE edits, the member copies `eff[uid]` (`link.py:1919`) instead of `by_uid[uid]` (`:1660-1663`).
- QEMU accepts this unchanged:
  - the source has no sc6 slots, so the flags stay 0 (`supercache.c:1311-1317`);
  - trusted member code is not hashed (`supercache.c:1648-1650`).
- Update the validator for "member bytes = effective source bytes".
- It matters once `o4rec1x`'s passes or B4 produce rewrites: o4r3c has 0 public rewrites (sidecar `x_b3.rewritten`).

**3b. Grown sources into members (scopt + QEMU).**
- **Linker.** Lay out members from `eff` for grown sources.
  - Slot offsets match the public output record QEMU arms from (`supercache.c:3229-3247`).
  - The slot shape matches the loader check (`scopt/guard.py:13-16` against `supercache.c:1512-1527`).
  - The reset LEA is emitted as a MOVI_EXIT RIPREL32 site (`guard.py:377-379`), which `retarget_private_descriptor_refs` rebinds to the member descriptor (`link.py:294-310`). Prove this with a unit test.
  - The failure path's pool call is RIP-relative to the unit's own pool and moves with it (`guard.py:35-38`).
- **Composition with internal edges.** A grown source's tail is already `jmp S`, so `lookup_tails` misses it (`link.py:1674-1677`), and both slots may be taken (`:1681-1685`).
  - If the region's internal target on that exit is one of B3's guard targets, patch that slot's rel32 to the target member. QEMU treats it as internal (`supercache.c:1471-1484`, `:3241`).
  - Otherwise that member falls back to its pre-B3 copy with skip bits. This is a per-member choice, so the link never fails.
- **QEMU.** Add a new member flag `SC7_MEMBER_B3_COPY` (bit 2).
  - The loader accepts `flags == B3_COPY`, with skip bits 0, for a source with sc6 slots (`supercache.c:1301-1317`).
  - `sc7_verify_residual_slots` and `sc7_init_trusted_descriptor` then verify and arm those slots as residual slots, which they already do for unskipped slots (`:1512-1527`, `:3229-3247`).
  - Bump the counters revision.
- **scopt support.** Mirror the flag in `read_sc7`, `loader_requirement` and `scopt/data/qemu_capabilities.json`, and add an allowlist flag next to `--v7-member-skip-exe`.
- **Order of work:** entry members first. 44 / 23 / 13 entry sources are grown and their public code never runs (§1.4).

**3c. Pricing switch.** For members laid out from post-B3 code, row 5(c)'s penalty is 0. `chained` becomes sound for them.

**Tests:**
- QEMU: a loader unit test on a synthetic composed cache, with a B3-copy member, a skip-flag member and a deliberately bad slot (expect `sc7_die`).
- scopt:
  - `decode_guard` readback of every member stub;
  - offline verification of every armed slot (`e9 00000000`, reset at +4, 4-aligned, > 15);
  - a retarget test of the reset LEA;
  - a link test for the internal-edge-equals-guard-target case.

**Gates:**
- G0 and the regions replay.
- `trusted_residual_slots_skipped` falls toward 0 from 764 / 178 / 153, and `trusted_residual_slots_armed` rises by about the same.
- **Mapping-change hazard.** Trusted regions ignore TLB/MPU changes (`supercache.c:3488-3492`), so B3 slots inside members are not unlinked the way public sc6 slots are. Run a functional case that reprograms the MPU. If any workload does so, restrict 3b to members whose guard targets are in flash.

**Measurement.** `o4rec3` against the stage-2 winner, W, `o4` and `o4r3c`.
- The expected size comes from stage-1 perf, items (b) and (d).
- **Note.** If 3b is adopted, `member_slot_skip` becomes moot (row 35).

### 3.4 Stage 4: closure extensions and recursive layout (scopt only)

**4a. Root-continuation expansion.**
- Change: enqueue the root continuation in context () (`firmware_regions.py:62-70`). Edges into member 0 stay residual, and QEMU self-chains them at +0 with a poll (`supercache.c:4411-4443`; counter `chain_installs_trusted_self`).
- Test: no internal edge targets member 0.
- Gate: functional checks with RTOS preemption (the BENCH output is unchanged); `trusted_poll_entry_exits` is recorded.

**4b. `blx reg` promotion.**
- Change: a private guarded call edge to the dominant `lookup_hit` target, priced as a guard plus a residual lookup on misses.
- Test: a guard-correctness test, like the return-guard tests (`trusted_return_guard_problem`, `firmware_inline.py:376-381`).

**4c. Return-guard elision.**
- Change: only with a Thumb proof that LR, or the stacked slot, is unchanged between the call and the return.
- Test: mutation tests that clobber LR must keep the guard.

**4d. Intra-region layout.**
- Change: permute `plan.members` by `member_flow` (`firmware_profit.py:197`), keeping member 0 first and putting cold nested members last. Optionally, order regions by entry flow and the grown tail by Pettis-Hansen (`link.py:1935-1938`).
- Lift `firmware_region_terminal_stage` (`link.py:1893-1895`) only for this internal permutation, never for top-level layout passes (b3l −2.32%).
- Test: QEMU's contiguity and entry checks hold (`supercache.c:1375-1391`).

**Measurement.** One arm per sub-step, against the stage-3 winner, W, `o4` and `o4r3c`.

### 3.5 Stage 5: context specialization on members, and helper partial evaluation

**5a. L2 (row 36).**
- Add a member-copy view with facts on internal edges.
- The first fact sources:
  - CalleeSummary rows at private returns (`manager_b3.py:178-218`);
  - call-edge argument constants via `solve_program` (`specialize_edges.py:142-143`).
- Run CP, BF, DHO, native-scalar-cleanup and region-state-forward/dse on member copies.
- Add per-member patch lists (`FirmwareRegionMember` has none) and V12 hostsym proofs.
- Gate: every member patch carries a proof certificate, and composed-artifact audit support is added (`map-b3.md` §7, `docs/b3-passes.md:163-164`).
- Expected: small. The SPEC bundle is near-inert (0 / 9 / 1 variants), and FirmBOLT folded 0 branches.

**5b. C1/C2.**
- First, the P0 hand-patched probe and its go/no-go (`docs/design/helper-inlining-design.md:95`). Then the F13 hostsym fix (`:64`).
- Then `hs-hflags-elide` (C2), then `hs-msr-fold` (C1), in the pre-B3 native stage. Every member copy at every depth then inherits them.
- Expected: MODEL +1.3% (v1 core, compute; `:81`). `map-hostinline.md` §7 scales this to about +2% / +0.8% / 0 on these workloads.

### 3.6 Stage 6: completeness items that need evidence first

- **L3/L4.**
  - Variant recursion: allow clones of grown C (`manager_b3.py:446-451`) and slots on variant sources (`:499-500`), with QEMU variant-keyed sc6 slots.
  - A manager loop over a named B3 group, until the `B3State` digest is stable, at most 3 rounds.
  - Value is about 0 today (0 / 9 / 1 variants).
- **Framed recursive helper lowering with specialization** (row 43). Only if 5b's P0 shows helper work is measurable.
- **U7 side entries** (row 19). Only if stage-1 perf (c) shows ≥ 1% of samples in the public copies of non-entry member sources.
  - Training transfers into those sources are 3.51M / 1.63M / 0.16M (`map-b3.md` §5.1).

## 4. The stage-1 pipeline to build first

### 4.1 `o4rec1` (exact TOML)

**Save as** `/data/scfix-o4/design/pipelines/o4rec1.toml`.

**Build** with the per-workload command of `/data/scfix-o4/<w>-w/opt/o4r3c/command.txt`, replacing:
- `--pipeline` with this file;
- `--store` with `/data/scfix-o4/<w>-w/opt/o4rec1/store`.

The pass blocks are o4r3c's (`/data/scfix-o4/design/pipelines/o4r3c.toml:52-90`) verbatim, plus:
- one repeated chain-static block;
- one firmware-inline parameter.

```toml
# scopt O4-rec stage 1, arm o4rec1 (experiment arm, not shipped).
# Design: /data/scfix-o4/rec/DESIGN.md sections 3.1 and 4.
#
# The rule (the user's words): "the key is to have all our optimizations in recursive
# manner thats it"; "Whatever the best speed optimizations are there MUST BE included
# in O4"; "Win in most cases is fine".
#
# o4rec1 is o4r3c (o4r3c.toml; pass blocks copied verbatim) with two config-only changes:
#   A1 on priced exhaustive regions: firmware-inline residual_goto_tb = "chained".  The
#      recursive closure of a root is priced with its residual goto_tb exits chained, as
#      member_slot_skip makes them at run time (Q8), instead of one 40 ns dispatch per
#      exit whenever any member source is B3-grown (link.py:359-380).
#   Cap lift without code: chain-static is listed twice with the same params.  The
#      256-unit cap is counted per proposal (chain_returns.py:183, :308;
#      manager.py:1126-1130), so the second entry can guard the 16 packet exits the first
#      refused as slot_budget.  uart and cli never reach the cap (50 / 43 units).
#
# Needs exactly o4r3c's inputs: a trusted sealed capture of an icount-off run, its sealed
# steady profile (--profile), --pgo-launch (host-inline helper PGO), --prep, --elf,
# --runtime-identity and the three runtime allowlists (--v6-exe, --v7-trusted-exe,
# --v7-member-skip-exe) naming a QEMU with counters revision 4 (aacca05 or later).
# The name avoids scopt's O4 preflight pattern ^O4(-r\d+)?$.
schema = "scopt.pipeline"
version = 1
name = "o4rec1"
description = "O4-rec stage 1: o4r3c + A1 (residual_goto_tb chained on exhaustive regions) + chain-static repeated (256-unit cap lift)"
allowed_capabilities = ["INLINE_CALL", "EMIT_FIRMWARE_REGION", "CLONE", "REDIRECT_EXIT", "ADD_ENTRY", "ATTACH_FACT", "PURE_REWRITE"]

[[pass]]
name = "host-inline"
version = 1
params = { mode = "maximal", trusted_all_helpers = true, trusted_rank_by = "frequency", trusted_selected_root = true, hot_loop_only = true, min_site_calls = 1000 }

[[pass]]
name = "specialize-returns"
version = 1
enabled = true
params = { targets_per_exit = 2, max_variants_per_c = 2, max_variants = 64, growth_fraction = 0.10 }

[[pass]]
name = "constant-propagation"
version = 1

[[pass]]
name = "branch-folding"
version = 1

[[pass]]
name = "dead-host-ops"
version = 1

[[pass]]
name = "chain-returns"
version = 1
enabled = true
params = { targets_per_exit = 2 }

[[pass]]
name = "chain-static"
version = 1
enabled = true
params = { targets_per_exit = 2, rank_by_net_work = true, c_target_check = 0.0 }

[[pass]]
name = "chain-static"
version = 1
enabled = true
params = { targets_per_exit = 2, rank_by_net_work = true, c_target_check = 0.0 }

[[pass]]
name = "firmware-inline"
version = 1
params = { mode = "exhaustive", cost_model = "profile", residual_goto_tb = "chained", phase = "steady", trust = "user", interrupts = "disabled", icount = "ignored", planner = "whopr", partitions = 4, workers = 4, member_slot_skip = "on" }
```

**Siblings built in the same round:**
- **`o4rec1a`:** the same file with `name = "o4rec1a"` and without `residual_goto_tb = "chained"`.
- **`o4rec1x`:** §4.3.

### 4.2 Expected effect, with evidence

**Cap repeat (packet only).**
- The first chain-static keeps 256 of 272 eligible plans and refuses 16 by `slot_budget`. Its kept plans sum to `estimated_net_saving_ns` 63.79 ms (o4r3c receipt).
- The 16 refused plans are the lowest-saving ones, because ranking sorts by saving (`chain_returns.py:300`).
- Expected: at most a few tenths of a percent on packet, well below A3's +1.51% [+0.48, +2.54] (`A2-SUMMARY.md:14`).
- uart and cli bytes are expected identical to o4r3c.

**A1 on exhaustive regions.** I re-priced o4r3c's refused roots from their receipt terms, removing only the `residual_goto_tb_unchained × 40 ns` penalty (§7). This leaves out the extra descriptor bytes that `chained` charges (`firmware_profit.py:502-506`).

| | packet | uart | cli |
|---|---|---|---|
| o4r3c admitted roots / `not_profitable` refusals | 53 / 140 | 27 / 175 | 14 / 194 |
| refused roots that pass admission when chained | 16 (model +81 ms; the CRC root alone +70.5 ms at 2,317,210 calls) | 7 (model +5.7 ms) | 0 |
| o4r3c admitted model `profit_net` | 21.21 ms | 11.31 ms | 1.60 ms |

- **The model over-credits these roots.** It credits each internal lookup at the full `lookup_ns`, although B3 guards those exits in public code (`firmware_profit.py:36-46`, `:467-478`). So the timing evidence is what matters:
  - A1 on O4-r1 (hot-loop): packet +1.82% [+0.61, +3.03], uart −0.99% [−1.81, −0.17] (`A2-SUMMARY.md:15`).
  - A1 in F against o4r3d: packet +1.54% (p 0.040), uart −2.38% (`F.json:174` and following; the task's F rows).
  - fl3b3, which admits the same CRC roots unpriced: packet +2.03% [+0.90, +3.16] (`A2-SUMMARY.md:18`).
- **Prediction for `o4rec1` against o4r3c:**
  - packet about +1 to +2%;
  - uart about −1 to −2.5%;
  - cli byte-identical to `o4rec1a`, since there are 0 new roots and the linker never calls the pricing predicate (`link.py:371`).
- **Adoption.** The bytes differ on two workloads and one is slower, so the "most workloads" condition fails. `o4rec1` would be recorded as a packet-specific option, not adopted.
- **Why build it anyway.**
  - It is the only config-only arm that changes what the recursive closure admits.
  - It measures A1's mechanism on exhaustive regions, the roots stage 2b must keep (packet) and refuse (uart).
  - It costs one build round of about 3 minutes per workload.
- **Expectation for `o4rec1x`.** The added passes were measured neutral elsewhere and never on sealed. Expect about 0, with a build-composition risk that the gate records.

### 4.3 `o4rec1x` (the "all-config" sibling)

Same header as `o4rec1`, with `name = "o4rec1x"`. Pass order:

1. `host-inline`, unchanged. The single native prefix must come first (`optimize.py:664-700`).
2. `fold-exit-pc-update`, version 1. It must be the first B3-stage pass, because it refuses once any B3 state exists (`pc_update.py:18-24`).
3. `local-scalar-cleanup`, version 1, with `enabled = true` (default-disabled, `manager.py:108-111`).
4. `eliminate-state-sync`, version 1, with `enabled = true` and `params = { classes = "D1|D2|D3" }`, as in `pipelines/b4.toml`.
   - It goes before specialize-returns: a later rewrite that overlaps a variant patch refuses the whole proposal (`manager_b3.py:612-618`).
5. `specialize-returns`, `constant-propagation`, `branch-folding`, `dead-host-ops`, unchanged.
6. `native-scalar-cleanup`, `region-state-forward`, `region-state-dse`, each version 1. This is the variant-cleanup order of `pipelines/global-region.toml`.
7. `chain-returns`, `chain-static`, `chain-static`, `firmware-inline` (chained), as in §4.1.

No existing scopt pipeline combines these passes with chain-* (checked over `pipelines/*.toml`), so this composition is untested. That is what the build gate is for.

## 5. Risks and how each is bounded

1. **Unbounded-recursion regressions like `helper_lookup_tb_ptr`.**
   - **The failure we know.** H2 copied the 50,345 B graph of `helper_lookup_tb_ptr` into 1.7-2.2k sites (+124-157 MB, `docs/O4.md:195`). It also removed the lookup tails that B3 and regions need: 760 / 365 / 486 roots refused as `lookup_tail_missing` (`inventory.md:323`).
   - **Bound for the host side:**
     - no helper-graph copying in O4-rec (row 41);
     - any future helper recursion keeps the 32,768 B per-site cap (`O4-design.md:731`), depth and step limits like FirmBOLT's (depth 4, 256 steps, `FGR.cpp:36-43`), and never targets `helper_lookup_tb_ptr` (row 44).
   - **Bound for the region side.** Region recursion is unbounded by design (`firmware_regions.py:29-34`). It is bounded by:
     - the price (`firmware_profit.py:516-523`, plus 0.25 ns per byte);
     - the arena (`link.py:2012-2018`);
     - the new recorded safety budgets of stage 2i.
   - **Why the price matters.** Admitting everything is slower: unpriced exhaustive with bounded host-inline measured c7b3/w7bd_b3 0.9730 / 0.9285 / 0.9978 (`docs/O4.md:196`).
2. **Code growth.**
   - **Sources.** Context cloning, plus stubs in post-B3 members (stage 3).
   - **Bound:**
     - the pricing footprint;
     - the stage-2i member budgets;
     - the arena check.
   - **No sc6 growth.** Member B3 slots do not add sc6 entries: they are armed from the public record's offsets (`supercache.c:3229-3247`). The sc6 arrays therefore grow only through the cap lift: 303 slots today on packet, against about 2,000 that fit (`sc_v6.py:107-110`).
3. **Build time and memory.**
   - Stage 2a adds one probe build per nested call. Memoize per (callsite, context), and charge `ctx.budget()` per probe (`firmware_inline.py:298-299`).
   - Keep 4 WHOPR workers.
   - Abort at 8 GB or 60 minutes. The o4r3c baseline is about 3 minutes and 2 GB.
   - Stage 2f removes the gdb replay, which is the only non-deterministic build step.
4. **QEMU loader limits.**
   - **The skip-flag equality check** (`supercache.c:1311-1317`) forbids post-B3 grown members. Stage 3b needs a new flag and a capability revision. Stage 3a needs neither.
   - **The residual-slot shape check is fatal** (`sc7_die`, `supercache.c:1525`). Verify offline before any launch.
   - **Edge flags must equal SKIP_POLL in trusted regions** (`supercache.c:1418-1423`). So there are no +0 internal edges; loop back-edges stay residual (4a).
   - **One region per root source TB:** the first in the directory wins (`supercache.c:1449-1456`). Recursive formation must never emit two regions with the same root source.
   - **Two goto_tb slots per TB.** They are shared by B3 guards and private internal edges (`link.py:1681-1685`). When they collide, that member falls back per member to its pre-B3 copy.
   - **Trusted regions ignore mapping changes** (`supercache.c:3488-3492`). This needs the MPU functional test in stage 3.
5. **Model error.**
   - The constants were never swept (F14).
   - Internal-lookup credit is overstated (§4.2: model +70.5 ms for one root, against about +1.8% measured).
   - **Bound:** no stage is adopted on the model; every stage is timed. 2b fixes the two known biases, and stage-1 perf calibrates them.
6. **Workload-specific losses** (A1 on uart).
   - The standing contract records them and does not adopt.
   - 2b and 3b are the mechanism fixes. The stage-2 ablation `o4rec2-noA1` attributes them.
7. **Fidelity.**
   - Closed loops without a poll are excluded by construction (4a).
   - Return-guard elision only with a proof (4c).
   - The ISB/PendSV deferral is unchanged and decided separately (row 17).
   - Guard-free specialization (5a) depends on members having no other entry. This holds by the single-entry design (`supercache.c:3253-3259`), and it must be re-proved if U7 side entries are ever built.
8. **Tooling.** Repeated pass names (§4.1) may break name-keyed gate scripts.
   - Stage 2c replaces the repeat.
   - Until then, gates key on the pass index (`PassRecord.index`, `manager.py:812`).

## 6. What would change this plan

- **Stage-1 perf (a) shows under 5% of time inside regions.** Stages 3-5 can then pay at most that share. Shift the effort to 2b, 2c, and the region-admission side.
- **Stage-1 perf (b) shows few lookups from member copies.** L1 (3b) is low value. Do 3a, and skip the QEMU flag.
- **`o4rec1` shows no uart loss on exhaustive.** A1 then joins O4 at stage 1 under the standing contract, and 2b becomes calibration only.
- **`o4rec1x` fails to build.** Each failing pass moves to "impossible today" in §2, with the failure reason as evidence.

## 7. How the new numbers were read

All one-liners were run with `PYTHONDONTWRITEBYTECODE=1 python3`, read only.

**Grown region-entry sources** (§1.4). Source: `x_b3.grown` and `x_b3_manager.firmware_regions[]` (`entry_member`, `members[].source_uid`) of `/data/scfix-o4/<w>-w/opt/o4r3c/store/artifacts/*/cache.sc.scopt.json`.

| | packet | uart | cli |
|---|---|---|---|
| region-entry sources | 53 | 27 | 14 |
| of which B3-grown | 44 | 23 | 13 |

**B3 units that are region sources.** Intersect `passes[].affected` of `/data/scfix-o4/<w>-w/opt/o4r3c/optimize.json` with the entry sources, and then with all member sources.

| | packet | uart | cli |
|---|---|---|---|
| chain-static units: total / entry sources / member sources | 256 / 44 / 85 | 50 / 23 / 47 | 43 / 13 / 41 |
| chain-returns units: total / entry sources / member sources | 38 / 0 / 35 | 31 / 0 / 20 | 21 / 0 / 14 |

**A1 re-pricing** (§4.2). For each firmware-inline `residual` row with reason `not_profitable` in `optimize.json` (`passes[firmware-inline].detail`):
- added' = `added_ns − 40 × terms.residual_goto_tb_unchained`;
- net' = `saved_ns − added' − footprint_ns`;
- admitted when `saved ≥ 1.25 · added'`, `net' > 0` and `net'/calls ≥ 1` (`firmware_profit.py:516-523`);
- `profit_net` = Σ `embedded[].profit.net_ns`.

**Other receipt values:**
- chain-static stats: `passes[chain-static].stats` (`eligible_exit_plans` 272, `refused:slot_budget` 16, `estimated_net_saving_ns` 63,786,968).
- Member counts and depth: `embedded[].members` and `actual_expansion_depth`.
- Replay counters: `/data/scfix-o4/packet/timing-R3/o4r3c/heldout-1/regions.json` (`region_entries` 2,817, `region_activations` 50, `chain_installs_trusted_destination` 56, `trusted_residual_slots_armed` 73 and `_skipped` 764, `trusted_residual_exits` 2).
- Build cost: `/data/scfix-o4/<w>-w/opt/o4r3c/time-v.txt`.
