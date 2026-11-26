# O4 and recursion

The user's rule for O4, in their words:

- "Whatever the best speed optimizations are there MUST BE included in O4";
- "Win in most cases is fine";
- "the key is to have all our optimizations in recursive manner thats it".

This document records what the last rule means for scopt, what O4-r3 already does
recursively, what it does not, and the staged plan to close the gaps. It summarizes the
recursion investigation of 2026-11-26.

**Status: nothing in this plan has been built or timed.** The investigation was read-only:
five subsystem maps, a design, a critique written afterwards, and a later analysis of routes
that need no QEMU change. O4-r3 itself is the measured result. Its decision rule and evidence
are in [`O4.md`](O4.md), and the campaign is in [`o4-campaign.md`](o4-campaign.md).

**The records** are in [`o4-campaign/recursive/`](o4-campaign/recursive/):

| File | What it is |
|---|---|
| [`map-regions.md`](o4-campaign/recursive/map-regions.md) | Region formation and the optimizations inside regions |
| [`map-b3.md`](o4-campaign/recursive/map-b3.md) | The B3 passes and what they reach inside regions |
| [`map-hostinline.md`](o4-campaign/recursive/map-hostinline.md) | How recursive host-inline is |
| [`map-others.md`](o4-campaign/recursive/map-others.md) | The other passes, the runtime features and the pass manager |
| [`map-firmbolt.md`](o4-campaign/recursive/map-firmbolt.md) | FirmBOLT -O4's recursion model, mapped onto scopt |
| [`DESIGN.md`](o4-campaign/recursive/DESIGN.md) | O4-rec: the design, written from the five maps |

**The critique** is [`o4-recursive-critique.md`](o4-recursive-critique.md): the adversarial
check of the design and maps against the code. In the campaign the critique step was stopped
before it ran. This critique was written afterwards (2026-11-26) for this documentation, so it
is not a campaign record and has no original under `/data/scfix-o4`.

**The no-QEMU analysis** is
[`o4-campaign/recursive/no-qemu-routes.md`](o4-campaign/recursive/no-qemu-routes.md)
("no-qemu-routes" below; final version 08:17, 44,484 B, sha256 `eb2f2c0a…`). It lists every
rule the QEMU loader applies to a region member and the routes by which scopt alone puts
B3, SPEC and host-inline inside region members. Its §5 is an adversarial check of its own
routes.

## Resolved: scopt-only, no QEMU change

scopt rewrites the supercache file; QEMU only loads and runs it. In the user's words: "scopt
has nothing to do with QEMU. it works on supercache not QEMU, as far as qemu is concerned its
just seeing a new supercache." Every stage of this plan therefore produces a supercache that
the existing loader (aacca05) accepts, and **no stage needs a QEMU change**. This supersedes
the new member flag (`SC7_MEMBER_B3_COPY`) proposed in DESIGN §3.3 (3b) and accepted by the
critique (§2.2). That flag was one convenient way to let a member carry post-B3 code, not a
requirement. (The loader does refuse a member whose skip flags differ from its source's B3
slot mask, `supercache.c:1311-1322`, so a member must not simply be a post-B3 copy of its own
source; the routes below avoid that case.)

How stage 3 is done instead (no-qemu-routes §0, §5.0 and §5.7, after its own check):

1. **Rewrites into members (3a, route (h)).** SPEC patches, cleanup and B4 rewrites are
   same-length rewrites of member copies. Host-inline is already in every member copy.
2. **Region formation consumes B3's targets (route (a+), recommended).** Extend the region
   past the root's return (root-continuation expansion, stage 4a) and add profiled return
   targets as guarded members (return expansion). The exits B3 would chain become internal
   edges, which are cheaper than B3's guard plus a chained jump. Poll rule: in the root
   context and in return-expansion contexts every edge to an existing member stays
   residual, and return targets are added as leaves, so every loop still polls for
   interrupts (its §5.2).
3. **Back-edges into the entry (route (d), own member 0 only).** A direct jump to the
   region's own entry at +0 keeps the poll. It is valid in every load mode, after an offline
   proof (its §5.4). Direct jumps to anything outside the region are unsafe and not used.
4. **Fallback only (route (f)).** A post-B3 continuation copy that names a lookup-invisible
   twin source record with no sc6 slots, so the loader requires flags 0 and arms B3's own
   slots. Correct only under sealed or exact-PC loads unless the twin has an unformable key
   or a full body; GENERAL slots and chainable targets only (its §5.3).
5. **Reclaim chain-static's budget on entry sources** (stage 2j): 44 of the 256 packet units
   go to slots that guard the root call, which the region already makes internal.

What QEMU would add that no supercache can: unlinking member chains on MPU changes, SPEC
arming from member descriptors, and counters. None of them is needed for speed.

**How much is left to gain is not settled.** The two analyses count the B3 slots that member
copies still lose differently (§4 item 4): 29 / 16 / 6 slot copies worth at most 4.0 / 1.4 /
0.04 ms per training run (no-qemu-routes), or 198 / 53 / 37 slot copies with no weight given
(the critique, which counts a slot as covered only when an internal edge goes to the slot's
own target). For comparison, O4-r3's regions are priced at 21.21 / 11.31 / 1.60 ms net per
training run. The perf attribution of stage 1 sizes it before any stage-3 code is written.

The maps and DESIGN were read at scopt `b35cdb6`. The critique and no-qemu-routes were read at
`56c4c80` and QEMU `aacca05`. `56c4c80` changed only `scopt/o4.py`, the pipelines, the docs
and three tests, so the maps' line citations still hold, apart from those into
`scopt/o4.py`.

**Where the critique changed a claim, it is marked [critique].** Where no-qemu-routes disagrees
with the critique, the claim is marked **[contested]** and both readings are given (§4 item 4,
§8); on the QEMU question it is resolved above (no QEMU change). Neither analysis has been checked against a build or a timed run. Triples are packet /
uart / cli.

## 1. The model: FirmBOLT -O4

**`-O4` in FirmBOLT is one switch.** It turns on "recursive FirmGraal with frame-slot
promotion" (`/data/FirmBOLT/firmbolt`, commit `c448884`; `src/firmbolt/cli.py:211-226`). Its
recursion has these parts ([`map-firmbolt.md`](o4-campaign/recursive/map-firmbolt.md) §1-§2):

| FirmBOLT -O4 property | Where (FirmBOLT) | What it means for scopt |
|---|---|---|
| **R1. Transitive inlining.** Every call copied into a caller becomes a new candidate, with depth + 1 and its chain of ancestors. | `native/lib/Passes/FirmGraalRecursive.cpp:490-492` | firmware-inline expands the nested calls of every inlined callee |
| **R2. Copies of optimized bodies.** Per-function passes run before the inliner. | `map-firmbolt.md` §2.4 | region members should copy post-B3 code |
| **R3. Context in each copy.** Known argument registers fold branches in the copy and travel to nested candidates. | `FirmGraalRecursive.cpp:171` (`foldInlined`) | specialize member copies from the facts of their inline context |
| **R4. Per-candidate pricing and hard bounds.** Ranking by weight / marginal bytes; depth ≤ 4, ≤ 256 steps, a flash budget; a recursion guard. | `FirmGraalRecursive.cpp:36-43`, `:338-341` | price each nested call on its own, under explicit safety budgets |
| **R5. Late passes on the merged code.** | `map-firmbolt.md` §2.4 | layout and cleanup on member copies |
| **(R6, scopt only.) Fixpoint.** | – | passes that make code re-run on it until stable |

**FirmBOLT's own recursion delivered transitive inlining, not folding.** On its corpus it made
144 inlines at -O0, 29 of them nested, and folded 0 branches
(`docs/rtos-elf/firmgraal-native.md:83-88` in FirmBOLT: "These runs establish correctness and
coverage, not a speedup").

**The guest code is already done.** The packet, uart and cli images were built with FirmBOLT
-O4 ([`o4-campaign/design/O4-design.md`](o4-campaign/design/O4-design.md), as cited in
`map-firmbolt.md` §2.7). So guest-level recursive inlining is already in the code scopt sees.
The recursion question for scopt is at the host-code and TB level.

**For scopt, then, "all our optimizations in recursive manner" means two things**
([`DESIGN.md`](o4-campaign/recursive/DESIGN.md) §0, §1):

- **(a) Every optimization reaches all emitted code.** It runs on public TBs, B3-grown TBs,
  SPEC variants, helper splices, and firmware-region member copies at every inline depth,
  the entry member included.
- **(b) Code made by one optimization is handed to the others,** to a bounded fixpoint.
- **The default is "in".** An item stays out only with measured harm or a proven
  impossibility.

## 2. What O4-r3 already does recursively

### 2.1 Region formation

**Region formation is the one part of scopt that is recursive.** O4-r3's firmware-inline uses
priced exhaustive selection (A5):

- `mode = "exhaustive"`, `cost_model = "profile"` (`pipelines/O4-r3.toml`).
- Every discovered call is a root (`scopt/passes/firmware_policy.py:66-67`).
- `build_firmware_region` expands each root to a fixed point
  (`scopt/analysis/firmware_regions.py:22-189`):
  - **Members are `(unit, inline context)` pairs.** Every distinct call path gets its own copy
    of the callee: virtual cloning (`:46-54`, `:141`).
  - **Both the nested callee and its continuation are expanded,** so calls after an inner
    return are inlined too (`:141-151`). Branches stay in the current context (`:161-177`).
  - **Returns become guarded private edges** to the right return member (`:153-160`).
  - **A call to a callee already on the path becomes a side exit** (`recursive_call_edge`,
    `:134-140`). The visited set ends CFG cycles (`:71-78`).
  - **It has no explicit depth, member or growth bound** ("deliberately no depth, iteration,
    member, site, hotness, or growth bound", `:31-33`). Price and the link arena bound it.
- **The steady profile prices each root's whole recursive closure** and admits or refuses it
  as one unit (`scopt/analysis/firmware_profit.py`, admission at `:516-523`).

**What it produces.** Source: `/data/scfix-o4/<w>-w/opt/<arm>/optimize.json`,
firmware-inline `stats` and `detail.embedded`. O4-r1 is arm `o4`; O4-r3 is arm `o4r3c`
(the same cache.sc bytes as `-O4`).

| | O4-r1 (hot-loop) | O4-r3 (priced exhaustive) |
|---|---|---|
| regions | 48 / 19 / 7 | 53 / 27 / 14 |
| member copies | 5,967 / 614 / 820 | 7,052 / 1,228 / 1,470 |
| largest region (members) | 769 / 184 / 281 | 971 / 186 / 281 |
| maximum inline depth | 5 / 4 / 5 | 5 / 5 / 5 |
| nested inlined calls | 289 / 26 / 19 | 360 / 43 / 32 |
| guarded private returns | 271 / 53 / 41 | 378 / 85 / 63 |
| model net saving (`profit_net_ns`) | 19.95 / 10.46 / 1.28 ms | 21.21 / 11.31 / 1.60 ms |
| roots refused `not_profitable` | 33 / 18 / 8 | 140 / 175 / 194 |

**Unpriced exhaustive selection was never timed on its own.**
- Unpriced, it grew to 37k / 17k / 18.5k members at depth 8-9
  (`/data/scfix-o4/design/inventory.md:293`, row F2).
- Every timed arm with unpriced exhaustive selection also changed host-inline:
  - **w7** also copies unbounded helper graphs. The design lists its s6/w7 0.8513 / 0.6921 /
    0.9109 under "Unbounded trusted helper graphs (H2)"
    ([`O4-design.md`](o4-campaign/design/O4-design.md) §1.3). The same pipeline with the
    graphs bounded (w7bd) is faster: w7/w7bd 1.2576 / 1.4003 / 1.3149 (round 2's bounded
    WHOPR timing, `inventory.md:324`, row H3).
  - **w7bd_b3** also carries bounded unrestricted host-inline. It measured -3.57% pooled
    against O4-r1 in H and -2.17% against O4-r2b in H2. A6, that host-inline alone on O4-r1,
    measured -4.51% in H ([`H-SUMMARY.md`](o4-campaign/H-SUMMARY.md):18-19;
    [`B-SUMMARY.md`](o4-campaign/B-SUMMARY.md):28).
  - The inventory labels exhaustive selection "NEUTRAL on packet and cli", and on uart "the
    cause is confounded with host-inline growth (H3)" (`inventory.md:293`).
- What is measured is that **priced** exhaustive selection (A5) beats hot-loop selection
  (campaigns H and R3). `scopt/o4.py:141-142` and `:180-182` and `docs/O4.md` ("Unpriced, the
  same selection regressed") still give s6/w7 as the cost of unpriced selection.

**Timing.**
- Against O4-r1 (campaign R3), O4-r3 is pooled +1.06%, one-sided 95% LB +0.61%
  ([`R3-SUMMARY.md`](o4-campaign/R3-SUMMARY.md):74). Per workload: packet +0.76%, uart +0.43%,
  cli +1.74% (`:61-63`).
- In the best-check (campaign F) it ties o4r3d: +0.10% [−0.60%, +0.80%]
  ([`F-SUMMARY.md`](o4-campaign/F-SUMMARY.md):113).

### 2.2 What else already applies at every depth

| Feature | Applies to | Evidence |
|---|---|---|
| Trusted ABI: members entered at +15, past the poll and the `can_do_io` store | every member, every depth | `map-regions.md` §4; `scopt/link.py:1826-1851` |
| Internal `goto_tb` and trusted `goto_ptr` tails patched to direct `jmp rel32` | every internal edge | `scopt/link.py:1838-1846` |
| Guarded private returns | every nested return | 378 / 85 / 63 (table above) |
| Member descriptors, per-member residual-slot chaining (Q6), member skip flags (Q8) | every member | all 53 / 27 / 14 regions are linked with member descriptors (sidecar `x_b3.firmware_region_links`; [`o4-recursive-critique.md`](o4-recursive-critique.md) §6) |
| Covering-first pricing with `captured` flow (regions inside regions) | every root | `scopt/passes/firmware_inline.py:65-81`, `:412-417`, `:489-491` |
| Trusted→trusted chaining (Q7) | region entries only, at +0 | `map-others.md` §4.17 |
| Host-inline splices | every copy of a spliced unit, because host-inline runs before B3 and members copy those bytes | packet's one splice (`helper_shl_cc`) has 1 member copy ([`o4-recursive-critique.md`](o4-recursive-critique.md) §1) |

### 2.3 What does not reach inside regions

Nothing else ([`DESIGN.md`](o4-campaign/recursive/DESIGN.md) §0 item 2;
[`map-b3.md`](o4-campaign/recursive/map-b3.md) §1):

- **No B3 transform reaches a member.** Members copy the pre-B3 bytes of their sources
  (`scopt/link.py:1724-1733`, `:2001-2002`). QEMU skips the B3 slots in the copies through the
  skip flags. So no GENERAL chaining, no SPEC variant, no CP/BF/DHO patch and no B4 rewrite
  runs inside a region.
- **No pass is scoped to members.** The manager's `region` scope means a SPEC variant
  (`scopt/manager.py:1009-1031`).
- **At the time of this campaign, firmware-inline was the terminal stage.** The current
  linker composes public placement with private firmware regions. See the subsequent
  [O4-layout replay](O4.md#public-layout-and-uncapped-static-chaining).
- **The pass manager cannot iterate.** It runs each entry once, in order
  (`scopt/manager.py:773-774`).
- **Profiling never sees regions.** `sc7_try_region` returns NULL while profiling
  (`supercache.c:3347-3349`), so there is no profile-the-optimized-artifact loop
  (`map-others.md` §4.23, §6).

## 3. Every optimization: in O4? recursive today? what is needed

The columns:
- **In O4-r3:** whether the pipeline has it.
- **Recursive today:** whether it reaches member copies at every depth.
- **Change needed** and **Effort:** none / small / medium / large.
- **QEMU:** whether the QEMU runtime must change.
- **Stage:** the stage of §5.

Source: [`DESIGN.md`](o4-campaign/recursive/DESIGN.md) §2 (its rows 1-50, condensed), with
the critique's changes ([`o4-recursive-critique.md`](o4-recursive-critique.md) §2-§4).

### 3.1 Region formation and pricing

| Optimization | In O4-r3 | Recursive today | Gap / change needed | Effort | QEMU | Stage |
|---|---|---|---|---|---|---|
| Recursive nested expansion | yes | **yes**, transitive and unbounded | closure gaps: the rows below | none | no | in |
| Priced exhaustive root selection (A5) | yes | each root's closure is all-or-nothing (`firmware_inline.py:300-304`) | best-of-plans (next row) | none | no | in |
| Per-nested-call profit gate and best-of-three plans (gated recursive, ungated recursive, flat) | no (hot-loop only) | no for exhaustive: `flat_fallback` is false when `policy.expand_nested` (`firmware_inline.py:304`) | return the gate from `_exhaustive` when priced; rank three plans with the existing `rank()` (`:444-451`). Recovers 3 packet roots, 0.88 ms model (`map-regions.md` §3.2). **[critique]** A context-keyed gate is not small: the profile has no per-context counts, so it needs a second pruning phase. | small (best-of-three); medium (context pruning) | no | 2a |
| Residual `goto_tb` pricing (`auto`; A1 = `chained`) | `auto` | **no, region-wide:** one grown member prices every residual exit of the region at 40 ns (`scopt/link.py:359-380`; `firmware_profit.py:485-491`) | per-member pricing. **[critique]** Exact rule: charge only the B3 slots whose target the member leaves residual. Today 13 / 1 / 0 admitted regions are priced unchainable with no such slot. | medium | no | 2b |
| Trusted→trusted exits priced at one poll | no (priced as a full dispatch) | runtime chains them at +0 | price at `poll_ns` for counters revision ≥ 4 | small | no | 2b |
| Single-instruction BL discovery from training evidence | no | no: 200 / 58 / 39 member call sites are `unverified_nested_call_exit` side exits **[critique: DESIGN counted 400 / 116 / 78 rows, two per site]** | `verified_entry_pcs` = single-instruction units with exact steady executions > 0. It also adds roots, so it is a selection change | small | no | 2d |
| FirmLooper v3 ∪ exhaustive (`exhaustive-tasks`) | no | – | about 0 as priced today (v3-only roots had 0 training calls, `map-regions.md` §5) | small | no | 2e |
| Root-continuation expansion (close the caller's loop) | no | no: the root enqueues only the callee (`firmware_regions.py:62-70`) | back-edges to member 0 stay residual. **[critique]** Only `goto_tb` back-edges poll (self-chain at +0); a `goto_ptr` back-edge enters at +15 with no poll. | medium | no | 4a |
| `blx reg` promotion | no | no | guarded call edge to the dominant target. **[critique]** Reaches only 5 / 2 / 2 sites, not 167 / 32 / 15 | medium | no | 4b (last) |
| Return-guard elision | no | every return is guarded | only with a Thumb proof that LR is intact | medium | no | 4c |
| Intra-region layout (member order by `member_flow`, cold nested members last) | no | BFS plan order | permute `plan.members`, keeping member 0 first | medium | no | 4d |
| Explicit safety budgets (members per region, total members) | no | – | at most 4,096 members per region and 4× o4r3c's total; FirmBOLT-style ranking when over | small | no | 2i |
| hot-loop / hot-loop-tasks as selector | no (superseded) | – | R3 +1.06% for A5 over hot-loop; in F, o4_tasks −0.56% (`scopt/o4.py:154-158`) | – | – | – |
| ISB side exit (I1) | no | applies at every depth when set | a fidelity choice, kept off (`scopt/o4.py:191-194`) | – | – | – |
| U7 side entries into non-entry members | no | GENERAL slots already enter regions at their entry **[critique confirms DESIGN; map-firmbolt and map-others were wrong]** | multi-entry regions, only if perf shows hot public copies of member sources | large | **yes** | 6 |

### 3.2 B3, B4 and cleanup

| Optimization | In O4-r3 | Recursive today | Gap / change needed | Effort | QEMU | Stage |
|---|---|---|---|---|---|---|
| chain-returns, chain-static (GENERAL slots; A3 ranking) | yes | public units only; members drop their slots | **L1, B3 inside members:** members copy post-B3 bytes. **[critique]** The internal edge must win, and only residual-target slots are worth copying: 198 / 53 / 37 of the 799 / 178 / 153 skipped slots. **[contested]** no-qemu-routes §2 (reproduced in its §5.1) counts 29 / 16 / 6 residual slots, all in root-continuation members, worth at most 4.0 / 1.4 / 0.04 ms per training run (§4 item 4) | large | no (resolved; see "Resolved" above): root-continuation expansion (4a) plus return expansion make the residual exits internal; a twin source record is a fallback correct only under sealed or exact-PC loads (§5.3 below). DESIGN and the critique had proposed one member flag (`SC7_MEMBER_B3_COPY`) | 3 |
| **[critique] Region-aware B3:** no slots for exits that regions make internal, or chain the regions' residual exits after firmware-inline | no | – | 44 of chain-static's 256 packet units are entry sources whose only slot is the root call; 601 / 117 / 114 skipped member slots guard internal targets (770 / 162 / 147 by no-qemu-routes' criterion, §4 item 4) | medium (two-round plan) | no | 2j |
| Lift the 256-unit cap | no | – | it binds only on packet chain-static: 16 refused under ranking. Stage 1 lists chain-static twice; stage 2c makes `max_units` a real parameter | small | no (about 2,000 sc6 slots fit) | 1, 2c |
| specialize-returns + CP, BF, DHO (the SPEC bundle) | yes | variants of public, un-grown units only; SPEC links whose R is an entry source never arm | **L2:** guard-free region-context specialization of member copies (non-entry members are entered only by internal edges) | large | no | 5 |
| Variant recursion and a B3 fixpoint (L3/L4) | no | no: no clones of grown units; each pass runs once | clones of grown C, variant-keyed sc6 slots, repeat the B3 group until `B3State` is stable | medium | **yes** | 6 |
| eliminate-state-sync (B4), local-scalar-cleanup, fold-exit-pc-update | no (`measured_neutral`, `scopt/o4.py:119-128`) | public units only | stage-1 sibling `o4rec1x`; stage 3a lets un-grown members copy rewritten bytes | medium | no | 1, 3a |
| native-scalar-cleanup, region-state-forward, region-state-dse | no (`measured_neutral`) | SPEC variants only | L2 on member copies | large | no | 1, 5 |
| specialize-edges | **no** | – | measured harm: fixture ablation 0.927× [0.902, 0.992] (`scopt/o4.py:117-118`) | – | – | – |
| irq-entry-mem-compare | **no** | – | impossible on sealed TBs (`scopt/o4.py:99-101`); the +15 entry already drops the poll inside regions | – | – | – |
| retire-ordinary-exits | **no** | – | exclusive pipeline; works against chaining (`scopt/o4.py:129-130`) | – | – | – |
| **[critique] Per-context B3 targets in member copies** | no | – | a copy inherits the aggregate targets (`map-b3.md` §7 L1 item 5) | small after L1 | no | 3 |
| **[critique] Register promotion across member edges** (the FirmBOLT frame-slot-promotion analogue) | no | – | today's state passes only NOP stores; none carries a value across a member boundary | large | no | 6 |

### 3.3 Host side

| Optimization | In O4-r3 | Recursive today | Gap / change needed | Effort | QEMU | Stage |
|---|---|---|---|---|---|---|
| H1 host-inline (selected root, hot loops, ≥ 1000 calls) | yes | depth 1; every member copy inherits the splice; 1 / 0 / 0 sites | drop the gdb counted replay for exact TB-entry counts (`scopt/passes/host_inline.py:223-254`, `:432-442`) | small | no | 2f |
| Bottom-up selected-root fold; guarded UDIV lowering in trusted mode | no | depth 1 | memoized fold with an SCC guard and the 32,768 B cap | small | no | 2g |
| Full helper-graph copying (H2 unbounded, A4, A6) | **no** | copies bodies, removes no nested call | measured harm: A4 −3.63% [−4.84%, −2.43%] ([`A-SUMMARY.md`](o4-campaign/A-SUMMARY.md):17), A6 −4.51% [−5.08%, −3.94%] ([`H-SUMMARY.md`](o4-campaign/H-SUMMARY.md):18) | – | – | – |
| C1/C2 partial evaluation of the MSR / `rebuild_hflags` pair | no (designed, not built) | in place and pre-B3, so every copy would inherit it | P0 hand-patched probe first (`docs/design/helper-inlining-design.md`) | large | no | 5b |
| **[critique] C3 `hs-msr-exit-chain`, C6 `inline-lookup-probe`** | no (designed stretch items) | – | C3 ESTIMATE 1.5-4.3% (`helper-inlining-design.md:105-108`); regions end at 192 / 10 / 0 `exit_null` boundaries | large | no | 5 (gated) |
| Framed recursive helper lowering with specialization | no | `extreme` mode only, for plugin callbacks | only together with specialization; otherwise it is A4 again | large | no | 6 |
| Rule: never inline `helper_lookup_tb_ptr` | rule | – | H2 copied its 50,345 B graph and removed the lookup tails regions need (`map-hostinline.md` §5) | – | – | rule |

### 3.4 Runtime

The sealed, trusted and icount contract items are required and global (`scopt/o4.py:286-300`).
Q2/Q3/Q6/Q7/Q9 are QEMU defaults that already work per member or per region entry.
`QEMU_SC_SEALED_SKIP_IRQ` stays out as unsafe: it disarms Q6 and Q7 (`scopt/o4.py:301-302`).
Top-level layout passes stay out: b3l measured −2.32% [−3.47%, −1.17%]
([`A-SUMMARY.md`](o4-campaign/A-SUMMARY.md):15), and only the intra-region form (4d) is
planned.

## 4. Key findings

1. **Only region formation is recursive.** Every other optimization stops at the region
   boundary (§2.3).

2. **The U7 premise was a counter artifact.** DESIGN §1.4(i) and `map-b3.md` §5.1 found it;
   **[critique]** confirmed it against the code.
   - B3 GENERAL slots are linked by the main loop after a lookup
     (`scopt/passes/chain_returns.py:8-9`; `supercache.c:4108-4114`).
   - In trusted mode that lookup returns the region entry (`supercache.c:3950-3969`), and the
     public → trusted chain is allowed when sealed (`:4498-4507`).
   - `region_entries` counts only lookups that missed the jump cache and QHT (`:3385`).
   - So o4r3c's packet held-out replay counts 2,817 `region_entries` with 56
     `chain_installs_trusted_destination`
     (`/data/scfix-o4/packet/timing-R3/o4r3c/heldout-1/regions.json`).
   - map-firmbolt (G2) and map-others (§4.18) say B3 slots bypass regions and rank U7 first.
     **That is false.**

3. **B3's work on region-entry sources is dead code.**
   - `lookup_entry` returns the region before the public TB (`supercache.c:3962-3971`). So the
     public code of an entry source, with its B3 guards, never runs in a timed run.
   - 44 of 53 / 23 of 27 / 13 of 14 entry sources are B3-grown (DESIGN §1.4(ii), §7).
   - **[critique]** Every one of those B3 slots guards the root call itself, which the region
     already turns into an internal call edge. So copying B3 into entry members would revive
     nothing. The loss is in chain-static's budget: 44 of its 256 packet units, while the cap
     refused 16 plans.

4. **Most of what member copies "lose" from B3 is already covered.** There are 799 / 178 /
   153 B3 slots that QEMU skips in member copies (sidecar `skipped_b3_slots`). Two read-only
   analyses of the same o4r3c caches size the rest differently. **[contested]**

   | | the critique (§2.3, §6 item 3) | no-qemu-routes (§2; reproduced in its §5.1) |
   |---|---|---|
   | a slot counts as covered when | one of the member's internal edges goes to the slot's own target | the member's `goto_ptr` exit, which the slot guards, already has an internal edge |
   | covered | 601 / 117 / 114 | 770 / 162 / 147 |
   | left (the stage 3 ceiling) | 198 / 53 / 37 | 29 / 16 / 6, all in `continuation` members |
   | weight | not given | at most 0.661M / 0.227M / 0.0068M steady lookups, at most 4.0 / 1.4 / 0.04 ms per training run |

   Sources: [`o4-recursive-critique.md`](o4-recursive-critique.md) §2.3;
   [`no-qemu-routes.md`](o4-campaign/recursive/no-qemu-routes.md) §2 and §5.1. Both agree that every
   B3 slot on an entry member (44 / 23 / 13) guards the root call, which the region already
   makes internal. Whether a slot on an internalized exit whose target differs from the
   internal edge's target is still worth arming has not been settled.

5. **The 256-unit cap** is `MAX_UNITS = 256` (`scopt/passes/chain_returns.py:51`). The pass
   enforces it at `:308-310`, and the manager enforces it per proposal at `scopt/manager.py:1128`.
   - It binds only on packet chain-static: 272 eligible plans, 256 kept, 16 refused
     `slot_budget` under O4-r3's ranking (o4r3c `optimize.json`). Unranked O4-r1 arms refused
     27.
   - `scopt/o4.py:224-226` still quotes the O4-r1 figure (27).
   - Listing chain-static twice lifts the cap with no code. The second run skips claimed slots
     and counts its own 256 ([`o4-recursive-critique.md`](o4-recursive-critique.md) §3.5).
   - The sc6 arrays have room for about 2,000 slots against 303 today
     (`scopt/formats/sc_v6.py:107-110`; `map-b3.md` §5.2).

6. **The chainability rule is region-wide and partly an artifact.**
   - One grown member makes every residual `goto_tb` exit of the region cost a 40 ns dispatch
     in the model.
   - That refuses the CRC task-loop root (2,317,210 calls, model −22.2 ms; `map-regions.md`
     §0, §5).
   - A1 (`residual_goto_tb = "chained"`) is workload dependent: packet +1.82% [+0.61%,
     +3.03%], uart −0.99% [−1.81%, −0.17%] ([`A2-SUMMARY.md`](o4-campaign/A2-SUMMARY.md):15).
     In F it was packet +1.54%, uart −2.38% ([`F-SUMMARY.md`](o4-campaign/F-SUMMARY.md):193-194).
   - **[critique]** 10 of the 53 admitted packet regions are "grown" only through their entry
     source's dead root-call guard. 13 / 1 / 0 regions have no B3 slot that guards a residual
     target.

7. **The whole-region gate loses roots.** Exhaustive has no flat fallback. Three packet roots
   that O4-r1 admitted (86,306, 14,567 and 14,568 calls) are refused by O4-r3, a 0.88 ms model
   loss (`map-regions.md` §3.2; the O4-r1 and o4r3c receipts).

8. **Host-inline is nearly inert, and its one site is unstable.**
   - It applies 1 / 0 / 0 sites (`helper_shl_cc` on packet). The binding constraint is the
     selected-root lowering, which refuses `helper_lookup_tb_ptr` and `helper_udiv`, not the
     1,000-call threshold (`map-hostinline.md` §4).
   - The gdb counted replay of that site varied between builds:
     - 8,372 (o4);
     - 8,514 (c7b3 rebuild);
     - 2,344-2,438 (six builds started together);
     - 3,464 (o4_nospec);
     - 8,651-8,969 (others);
     - 9,433 in round 2.

     All are against `min_site_calls` 1,000
     ([`verify/packet-VERIFY.md`](o4-campaign/verify/packet-VERIFY.md) §5, problem 1).
   - A count below 1,000 would make `-O4` drop the site.

9. **Unverified nested calls are 200 / 58 / 39 sites, almost all direct.** **[critique]**
   Only 5 / 2 / 2 are `blx reg`. The rest are direct BLs (33 / 26 / 24 exit through `goto_tb`,
   162 / 30 / 13 through `goto_ptr`) that call discovery skipped
   (`scopt/analysis/firmware_calls.py:51-64`).

10. **Poll-free paths already exist inside the trusted contract.** **[critique]**
    - `docs/sealed-firmware.md:323-326` names two: internal cycles within one region, and a
      `goto_ptr` lookup into a region entry at +15.
    - 25 / 14 / 13 o4r3c regions contain internal cycles.

11. **FirmLooper ∪ exhaustive is a no-op as priced** (`map-regions.md` §5). A profile-free O4
    is impossible anyway, because B3 needs the profile.

## 5. Staged plan

DESIGN §3 gives the plan in dependency order. The critique's changes are marked.

### 5.0 Rules for every stage (DESIGN §3.0)

- **Correctness gate G0** (from [`O4-design.md`](o4-campaign/design/O4-design.md) §3).
  A failure on any workload drops the arm.
  - `scopt optimize` exits with rc 0.
  - The functional check passes: 16 words and one BENCH line, `counters_revision` 4 (5 after
    stage 3) with no counter problems, every poll entry verified, and no sc6 invariant
    violations or flush resets.
  - The QEMU sha256 equals the artifact's.
- **Regions replay counters.** Record `regions_loaded`, `region_entries`,
  `region_activations`, `chain_installs_trusted_destination`,
  `trusted_residual_slots_armed` / `_skipped` and `trusted_residual_exits`.
- **Build budget.** Abort above 8 GB RSS or 60 minutes.
- **Same bytes.** `cmp` each arm's cache.sc against o4r3c; an equal pair is an alias and is
  not timed.
- **Timing.** Use the registered `decide_r3.py` with the R3/F block layout. Every campaign
  also times O4-r1 and o4r3c.
- **Adoption** follows the standing contract
  ([`prereg/AMENDMENT-F-most-cases.md`](o4-campaign/prereg/AMENDMENT-F-most-cases.md)):
  - the pooled one-sided test rejects;
  - the arm is not shown slower on most of the workloads where its bytes differ.

  A one-workload gain is recorded as a workload-specific option, not adopted.

### 5.1 Stage 1: config only, plus a no-code perf attribution

**Arms.** Each is built with o4r3c's command and a new pipeline:

| Arm | What it is |
|---|---|
| `o4rec1` | o4r3c + A1 (`residual_goto_tb = "chained"`) + a second chain-static entry (the exact TOML is in DESIGN §4.1) |
| `o4rec1a` | the same without A1: it isolates the cap lift |
| `o4rec1x` | `o4rec1` + every measured-neutral cleanup pass that composes with a sealed B3 pipeline (DESIGN §4.3) |

**Checks.**
- `o4rec1a` is byte-equal to o4r3c on uart and cli. Its second chain-static on packet touches
  at most 16 units.
- `o4rec1` admits a superset of o4r3c's roots: about +16 on packet and +7 on uart, from the
  A1 re-pricing (reproduced in [`o4-recursive-critique.md`](o4-recursive-critique.md) §6
  item 5). On cli it is byte-equal to `o4rec1a`.

**Perf attribution** (no QEMU change; the code arena is mapped at a fixed address,
`supercache.c:419-426`). Classify samples by code class and inline depth. It must answer:
- (a) the share of time inside regions;
- (b) the lookups made from member code;
- (c) time in public copies of member sources;
- (d) whether entry sources' public code is at 0%.

**Expected** (DESIGN §4.2): packet about +1 to +2%, uart about −1 to −2.5%, cli the same
bytes. So `o4rec1` is probably a packet-specific option, not an adoption. It is worth building
because it measures A1's mechanism on exhaustive regions.

### 5.2 Stage 2: scopt-only pricing and selection

| Sub | Change | Test / gate |
|---|---|---|
| 2a | best-of-three plans per root for exhaustive. **[critique]** The context-sensitive gate is a separate, medium pruning phase. | Total `profit_net` ≥ 21.21 / 11.31 / 1.60 ms. **[critique]** Per-root monotonicity holds only with `captured` fixed. Packet roots 666a8, a59e6 and 3e5a7 are admitted. |
| 2b | per-member B3-aware pricing. **[critique]** Charge only B3 slots with residual targets. T→T at one poll. | Receipts list every root A1 admitted that 2b refuses; the uart losers must be among them |
| 2c | the cap as a real parameter; O4 sets 512 | all 272 packet plans admitted; uart and cli bytes unchanged |
| 2d | single-instruction BL discovery from training evidence | **[critique]** count sites: 200 / 58 / 39 today; also a root-selection change |
| 2e | `exhaustive-tasks` (FirmLooper union) | every exhaustive root keeps its plan; expected 0 bytes changed |
| 2f | host-inline from exact TB-entry counts, no gdb | site counts match gdb's on uart; packet keeps `helper_shl_cc` |
| 2g | bottom-up selected-root fold; guarded UDIV | per-site bytes ≤ 32,768 |
| 2i | recorded safety budgets | never binds on o4r3c's inputs |
| **2j [critique]** | region-aware B3: give no chain slot to an exit a planned region makes internal (needs a firmware-inline plan before the chain passes, or a second chain round after it) | frees at least the 44 packet entry-source slots; packet's cap stops binding |

**Measurement.** `o4rec2` (the stage-1 winner + stage 2) against W, O4-r1, o4r3c and the
stage-1 winner, with `o4rec2-noA1` as the ablation. **Target:** keep A1's packet gain
without its uart loss.

### 5.3 Stage 3: B3 inside member copies (L1; FirmBOLT R2)

- **3a (scopt only).** Un-grown members with rewrites copy the rewritten bytes. QEMU accepts
  this: their skip flags stay 0 (`supercache.c:1309-1316`), and trusted member code is not
  hashed (`:1648`).
- **3b, as DESIGN first wrote it (superseded).** Grown members copy post-B3 bytes under a new
  QEMU member flag, which QEMU then verifies and arms as residual slots. Not used: scopt must
  work within the format today's loader accepts (see "Resolved" above). The scopt-only order
  is the one from no-qemu-routes below.
  - **[critique]** The internal edge wins. Rewrite the B3 tail of an internally-targeted exit
    to the member jump. Keep only the residual-target slots: 198 / 53 / 37 slot copies.
  - **[critique]** Today a missing lookup tail or exhausted slots fail the whole link, so the
    per-member fallback is new code.
  - **[critique]** Drop "entry members first": their B3 slots are all on the root call.
  - **[contested]** no-qemu-routes puts the residual at 29 / 16 / 6 slot copies (§4 item 4)
    and finds that no part of stage 3 needs a QEMU change (its §0, kept after its own check in
    §5.0). Its corrected order (its §5.7):
    1. 3a: rewrite-only member copies;
    2. root-continuation expansion (4a) first, with a stronger poll rule (see 4a below);
    3. member 0 at +0 for back-edges into the entry, after an offline H3 proof;
    4. the twin source record (its route (f)) only as a fallback, with an unformable twin key
       or a full-body twin, GENERAL slots only and chainable targets only;
    5. reclaim the chain-static budget on entry sources.

    Its §5.3 found that the twin record, as first specified, is correct only under
    `QEMU_SC_SEALED=1` or exact-PC loads: loaded without them, a lookup can activate the twin
    and corrupt the arena. Its §5.5 found that route (g) is not loader-compatible. What still
    needs QEMU, by its account, is only what no route provides (unlinking member chains on MPU
    changes, SPEC arming from member descriptors, counters), and the 3b flag would not provide
    it either.
- **Tests.** scopt-side only: offline readback of every member against the loader's rules
  (no-qemu-routes §1: flags equal the source's B3 slot mask, slot shapes, edges at +15, the
  trusted prefix); the poll rule (every cycle keeps a polling edge); the internal-edge
  collision case; and a load-and-run of each workload on the unchanged QEMU (G0).
- **Gates.** G0. `trusted_residual_slots_skipped` falls toward 0 from 764 / 178 / 153 (the
  runtime counter in one replay per workload, `map-b3.md` §4.3; the sidecar's static count is
  799 / 178 / 153). An MPU reprogramming functional case (trusted regions ignore mapping
  changes, `supercache.c:3488-3492`; **[critique]** an existing property, already true of Q6
  slots).
- **Measurement.** Against the stage-2 winner, W, O4-r1 and o4r3c. Size the expectation from
  stage-1 perf item (b).

### 5.4 Stage 4: closure extensions and recursive layout (scopt only)

- **4a. Root-continuation expansion.** Back-edges to member 0 stay residual.
  **[critique]** A `goto_ptr` back-edge skips the poll. The test must cover residual
  `goto_ptr` exits to the region's own entry PC, not only internal edges.
  no-qemu-routes §5.2 finds a further hole: with 4a, a loop in the caller after the call that
  does not re-execute the call unit (for example `f(); while (!flag) {}`) becomes an internal
  cycle that never passes member 0, so it never polls. Its fix: in the root context and in
  every return-expansion context, an edge to an existing member stays residual, except an
  edge to member 0; return-expansion targets are leaves; and an offline test checks that the
  internal-edge graph restricted to those contexts is acyclic.
- **4b. `blx reg` promotion.** **[critique]** 5 / 2 / 2 sites; last.
- **4c. Return-guard elision,** with an LR proof and mutation tests.
- **4d. Intra-region member order by `member_flow`,** with member 0 first; QEMU's contiguity
  and entry checks must hold. The top-level layout refusal stays (b3l −2.32%).
- **Measurement.** One arm per sub-step.

### 5.5 Stage 5: context specialization and helper partial evaluation

- **5a. L2.** A member-copy view with facts on internal edges (CalleeSummary rows at private
  returns, call-edge argument constants). CP, BF, DHO and the region-state passes run on
  member copies, with per-member patch lists and hostsym proofs. Expected to be small:
  0 / 9 / 1 variants today, and FirmBOLT folded 0 branches.
- **5b. C1/C2,** after the P0 hand-patched probe and the F13 hostsym fix. **[critique]** Add
  C3 and C6 as gated stretch items.

### 5.6 Stage 6: completeness items that need evidence first

- **L3/L4:** variant recursion and a B3 fixpoint of at most 3 rounds. About 0 value today.
- **Framed helper lowering with specialization,** only if 5b's P0 shows measurable helper
  work.
- **U7 side entries,** only if stage-1 perf shows ≥ 1% of samples in public copies of
  non-entry member sources.
- **[critique] Register promotion across member edges.**

## 6. Risks and bounds

DESIGN §5, with the critique's additions:

1. **Unbounded recursion.** H2 copied `helper_lookup_tb_ptr`'s 50,345 B graph and lost the
   lookup tails regions need.
   - Host side: no helper-graph copying; any future helper recursion keeps the 32,768 B cap
     and FirmBOLT-style depth and step limits.
   - Region side: price, the link arena, and the stage-2i budgets.
2. **Code growth** from context cloning, and from stubs in post-B3 members. Bounded by
   pricing, budgets and the arena. Member B3 slots add no sc6 entries.
3. **Build time and memory.** Memoize probes and keep 4 WHOPR workers. The o4r3c baseline is
   about 3 minutes and 2 GB per workload (`/data/scfix-o4/<w>-w/opt/o4r3c/time-v.txt`, via
   DESIGN §3.0).
4. **QEMU loader limits.**
   - Skip flags must equal the sc6 slot mask (`supercache.c:1309-1316`).
   - The residual-slot shape check is fatal (`:1525`).
   - Trusted edges must carry SKIP_POLL (`:1418`).
   - The first region per root source TB wins (`:1442-1457`).
   - Two `goto_tb` slots per TB are shared by B3 and private edges.
5. **Model error.** The constants were never swept, and internal-lookup credit is overstated.
   No stage is adopted on the model; every stage is timed. **[critique]** The pricing order is
   an admission input (`scopt/o4.py:195-198`).
6. **Workload-specific losses** (A1 on uart). Recorded, not adopted; 2b and 3b are the
   mechanism fixes.
7. **Fidelity.**
   - Poll-free task loops are excluded by policy (4a). **[critique]** The trusted contract
     already allows poll-free internal cycles and `goto_ptr` entries at +15.
   - Return-guard elision needs a proof.
   - Guard-free specialization relies on single-entry regions and must be re-proved if side
     entries are ever built.
8. **Tooling.** Repeated pass names: `--params`, `--enable` and `--disable` resolve by name
   and would apply to both chain-static entries.

## 7. What would change the plan (DESIGN §6)

- **Under 5% of time inside regions:** stages 3-5 can pay at most that share. Shift the
  effort to 2b, 2c and 2j.
- **Few lookups from member copies:** skip stage 3's B3 part (no QEMU flag is needed either way; see "Resolved" above). **[critique]** The ceiling
  is already 198 / 53 / 37 slot copies. **[contested]** no-qemu-routes puts it at 29 / 16 / 6,
  at most 4.0 / 1.4 / 0.04 ms per training run, and needs no QEMU flag (§4 item 4, §5.3).
- **No uart loss for `o4rec1`:** A1 joins O4 at stage 1.
- **`o4rec1x` fails to build:** each failing pass is recorded as "impossible today", with its
  reason.

## 8. Claims the critique changed

Rows marked [contested] are disputed by no-qemu-routes (final, archived). The QEMU question is
resolved: no QEMU change (see "Resolved" above). The sizing is not; nothing has been built to
settle it.

| Claim | Where | Correction |
|---|---|---|
| B3 slots bypass regions (U7 is the largest lever) | `map-firmbolt.md` G2, §5; `map-others.md` §1.3, §4.18, §7 | False. GENERAL slots chain into regions after one lookup. DESIGN §1.4(i) and map-b3 §5.1 were right. |
| A post-B3 member may need no QEMU loader change | `map-firmbolt.md` G1 | A member that is a post-B3 copy of its own source is refused: the loader dies unless the skip flags equal the B3 slot mask. The critique concluded that DESIGN's new flag is needed. **Resolved: no QEMU change** (see "Resolved" above). no-qemu-routes §3 (f), §5.3: the mask is taken from the member's source record, so a post-B3 continuation copy that names a twin record with no sc6 slots must have flags 0, and QEMU then arms B3's slots. Its own check found this legal and correct under `QEMU_SC_SEALED=1` only; a plain load can activate the twin and corrupt the arena, so the twin needs an unformable key or a full body, GENERAL slots only and chainable targets only. Its verdict that no QEMU change is needed stands (§5.0). |
| B3 on entry sources is the clearest case for recursive B3; do entry members first | DESIGN §0, §1.4(ii), §3.3 | Every such slot is on the root call, which the region already makes internal. Fix the budget (2j), not the copy. |
| 400 / 116 / 78 unverified calls; 167 / 32 / 15 `blx reg` | DESIGN rows 10 and 12; `map-regions.md` §1, §6 | 200 / 58 / 39 sites; 5 / 2 / 2 indirect. |
| Context-keyed nested gate is small | DESIGN row 3, 2a | The profile has no per-context counts; it needs a second pruning phase (medium). |
| On a collision, patch the B3 slot to the member | DESIGN §3.3 3b | That puts guard compares on internal edges. The internal edge must win. Value: 198 / 53 / 37 slot copies. **[contested]** no-qemu-routes agrees that the internal edge is better (its route (a)) but sizes the value at 29 / 16 / 6 slot copies (§4 item 4). |
| Back-edges to member 0 poll through the self-chain | DESIGN row 11, 4a | Only `goto_tb` ones do. `goto_ptr` enters at +15; poll-free cycles already exist in 25 / 14 / 13 regions. |
| WHOPR only changes the order | DESIGN row 18 | The order changes admission (`scopt/o4.py:195-198`). |
| The list of "all" optimizations | DESIGN §2 | Adds region-aware B3, C3/C6, per-context B3 targets and register promotion across member edges. |
