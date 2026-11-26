# FirmBOLT -O4's recursion model, mapped onto scopt O4

Read-only reference step. Nothing was built, run or timed. FirmBOLT is `/data/FirmBOLT/firmbolt`
at c448884 (main). scopt is `/data/fastdyn-work/fd-int/tools/scopt` at b35cdb6 (branch o4), and
QEMU is `/data/fastdyn-work/qemu-fu` at aacca05. Paths below are relative to those roots unless
they are absolute. "Receipt" means the optimize.json of the named arm under
`/data/scfix-o4/<w>-w/opt/<arm>/`. I read those with the python one-liners recorded in
section 7.

## 0. Bottom line

- In FirmBOLT, "recursive" means three things.
  1. **A transitive inliner.** Every call copied into a caller becomes a new candidate.
  2. **Context re-applied inside every copy.** Known argument registers fold branches in the
     copied callee, and the values known at a copied call are passed to that nested
     candidate.
  3. **Phase ordering.** The per-function optimizations run before the inliner, so it copies
     callee bodies that are already optimized. The late passes run after it, on the
     inlined code.
- scopt already has (1). Priced exhaustive firmware-inline (A5, in o4r3c) builds regions to
  a fixed point, depth 5 and 360 nested calls on packet. It lacks (2) and (3):
  - Region members copy the **pre-B3** bytes of their source TBs. No SPEC, chain-returns,
    chain-static or B4 edit reaches the code inside a region.
  - Nothing runs after firmware-inline, the terminal stage.
- scopt also has a gap FirmBOLT cannot have. FirmBOLT inlines in place, so every path that
  reaches the caller executes the inlined copy. A scopt region is entered only through the
  dispatcher lookup, and B3's chain slots bypass that lookup (U7).
  - The o4r3c packet receipt prices 1.35M root calls.
  - Its held-out regions replay counted 3,144 region entries.
- So the scopt analogue of "all our optimizations in a recursive manner" is:
  - **R1 (have):** recursive region formation (A5).
  - **R2 (missing):** members inherit every per-TB optimization (post-B3 member copies). This
    is FirmBOLT's "copy cleaned bodies".
  - **R3 (missing):** B3's output chains into regions (U7). This is FirmBOLT's "inline in
    place".
  - **R4 (missing, small):** each nested call is priced separately in exhaustive mode. This is
    FirmBOLT's per-nested-candidate ranking.
  - **R5 (missing, large):** inside regions, specialize and clean up members using facts
    from their inline context. This is FirmBOLT's foldInlined plus the late passes.
  - **Supporting change:** lift the 256-unit B3 cap. R2 and R3 raise slot demand, and the cap
    already binds on packet.
- One recursion is measured harmful in scopt: transitive host-helper graphs. A4 o4_hotgraph
  measured −3.63% pooled and A6 o4_hostall −4.51%. FirmBOLT bounds its own recursion
  (depth 4, 256 steps, flash budget). scopt's host-inline should stay bounded the same way.

## 1. FirmBOLT -O4: what the switch turns on

- `-O4 --flash START:SIZE` is one switch (`src/firmbolt/cli.py:211-226`). It does these things:
  - It exports FreeRTOS facts with the `aapcs_calls` and `immutable_literals` contracts.
  - It sets the FirmGraal flash budget to SIZE and `new_code_address` to START.
  - It enables `firmgraal_recursive` and `firmgraal_intact_return_slots`
    (`src/firmbolt/frontend.py:703-737`).
  - It adds the BOLT arguments `O4_BOLT_ARGUMENTS` (`src/firmbolt/frontend.py:678-689`):
    - literal materialization (`--firmbolt-materialize-literals`, min-count 0, no size cap);
    - leaf-frame elimination;
    - private-stack promotion and promotion across calls;
    - frame-store removal stays **off**, because it deletes stores that only a debugger or a
      host reads (c448884 message; `README.md` -O4 section).
- -O4 declares four contracts: AAPCS calls, immutable literals, intact saved return
  addresses and private frames (`README.md` "-O4" section; `frontend.py:711-719`).
- **Fallback.** If the whole-image rewrite fails, -O4 retries once, with BOLT limited to the
  task callers and inline targets (`cli.py:261-270`, `frontend.py:740-752`).
- **Pass order** inside BOLT (`patches/llvm-bolt-integration.patch`):
  - Literal materialization (`:9718-9719`).
  - Leaf frame optimizer and trivial-frame elimination, while callees are still leaves
    (`:9744-9753`).
  - BOLT's Inliner (`:9754`).
  - Private-stack promotion (`:9755-9756`).
  - Copy cleanup (`:9757-9761`).
  - **FirmGraalRecursive** (`:9762-9766`).
  - Strength reduction, shorten, NormalizeCFG, ICF, ReorderBasicBlocks, TailDuplication,
    Peepholes and layout (`:9767-9806`).
  - Late frame optimizer, trivial-frame elimination, copy cleanup and AllocCombiner
    (`:9829-9834`).
  - Late FirmGraal, which now only validates placement and writes the receipt
    (`:9835-9836`; `native/lib/Passes/FirmGraal.cpp` early return added in 9ef83cc).

## 2. The recursion model (native/lib/Passes/FirmGraalRecursive.cpp)

### 2.1 What is applied recursively

- **Seeding.** Every fact root whose BL is found in its owner becomes a candidate, tagged
  with an annotation so that each copy of a BL is a separate site (`:309-329`, `:56`).
- **Each step.** The best legal candidate that fits is inlined. Its whole callee CFG is
  copied with BOLT's Thumb inliner, including the frame, runtime branches and nested calls
  (`:405-430`; docs `firmgraal-native.md:53-58`).
- **Context re-application (`foldInlined`, `:167-251`).**
  - The known argument registers of the site propagate through the copy while each block
    has a single predecessor.
  - `cbz`/`cbnz` and `cmp #imm` with `beq`/`bne` are folded on them.
  - Unreachable copied blocks are erased (`:449-455`).
  - The registers known at each copied call are recorded (`X.CallArgs`, `:183-188`).
- **Nesting.** Every call copied from the callee becomes a new candidate if the facts list
  that nested edge (`:459-496`). It carries these values:
  - its own known arguments (`:471-481`), so specialization compounds with depth;
  - weight = parent weight × the edge's relative weight (`:486-487`). The facts exporter
    computes the relative weight (`src/firmbolt/firmgraal_facts.py:43-56`);
  - `Depth + 1`, plus a `Chain` of the callee entries above it (`:490-493`).
- **Legality is re-checked against the current program every round** (`Check`, `:332-356`).
  The refusal reasons are `site_removed`, `recursive_edge`, `depth_limit`,
  `unsupported_caller`, `callee_not_inlinable`, `requires_intact_return_slots`,
  `continuation_observes_link` and `literal_reach`. The docs table is at
  `firmgraal-native.md:60-71`.

### 2.2 Depth, cycles and step bounds

- **Cycles.** A candidate whose target is the caller, or is already on its `Chain`, is
  refused as `recursive_edge` and stays an ordinary call (`:338-339`).
- **Depth.** `--firmgraal-max-depth` defaults to 4 (`:36-39`, checked at `:340-341`).
- **Steps.** `--firmgraal-max-inlines` defaults to 256 (`:40-43`). When it is reached, the
  remaining candidates are refused as `inline_limit` (`:399-402`, `:515-517`).
- -O4 does not change either default (`frontend.py:731-737`).
- The tests cover a chain three calls deep, a self-recursive callee and the depth limit
  (`tests/unit/test_firmgraal_recursive.py:69-105`).

### 2.3 Budget

- **The flash budget is hard: it is the board's free flash.**
- **Conservative start.** With relocation, selection starts from a reserve that holds every
  relocated function: the estimate plus alignment (`:288-300`). The estimate includes
  literal pools and one 32-bit branch per block (`:83-94`).
- **Charging.** Each caller is charged its growth over its first estimate (`:301-306`).
- **Ranking.** Candidates are ranked by `(recurring, weight / marginal bytes, weight)`, and
  a candidate that would exceed the budget is skipped (`:358-391`).
- **Re-pricing.** After each inline the caller's charge is updated, so the remaining
  candidates are re-priced against the program as it is now (`:498-501`).
- **Small budgets.** A budget below the relocated baseline selects nothing instead of failing
  (`firmgraal-native.md:75`).
- **Final check.** The late pass checks the linked arena against the budget
  (`FirmGraal.cpp:540-591`).

### 2.4 How the other optimizations reach inlined callees

FirmBOLT does **not** re-run every pass on every inlined callee.

- **Bottom-up (before inlining).**
  - Literal materialization, leaf-frame elimination, BOLT's Inliner, private-stack promotion
    (including across calls) and copy cleanup run on each function first. So FirmGraal
    copies bodies that are already optimized (`firmgraal-native.md:53`; patch
    `:9762-9764`).
  - Promotion *cannot* run after inlining, because an inlined -O0 callee sets up its own r7
    frame inside the caller (`firmgraal-native.md:53`).
  - Literal materialization also widens what is inlinable: an unmaterialized literal load
    makes a callee `callee_not_inlinable` (`firmgraal-native.md:65`; `frontend.py:679`).
- **Top-down (during inlining).** Argument specialization and branch folding are re-applied
  at every nesting level (2.1).
- **After inlining.** All late passes, layout and the late frame optimizer, trivial-frame
  elimination and copy cleanup run on the merged functions (patch `:9767-9834`).
- **Return addresses.** Before inlining a callee that saves LR, the pass materializes the
  original return address with `movw`/`movt lr` (`:118-149`, `:410-419`). The callee then
  pushes the same word, so stack contents are unchanged. This is the
  `--firmgraal-intact-return-slots` contract (`firmgraal-native.md:73`).

### 2.5 Frame-slot promotion across calls (FirmBOLTPrivateStackPromotion.cpp)

- **Option.** `--firmbolt-private-stack-promotion-calls` (`:28-33`).
- **What it does.** It promotes word slots `[r7,#k]` of non-leaf functions into unused
  r4-r6 (`:77-86`). A slot qualifies when:
  - every access is a whole-word load or store;
  - a store reaches every load on every path;
  - the frame address never escapes.
- **ABI.** The promoted registers are added to the push and every pop in pairs, which keeps
  SP 8-byte aligned.
- **Not recursive.** Promotion is per function and runs before inlining.
- **Results** (9ef83cc message; `docs/passes.md:309-332`):
  - 2,126 slots in 1,221 -O0 functions;
  - executed stack accesses fell 32-41% in two demos.

### 2.6 What the recursion achieved

- On Common/Minimal at -O0: 144 inlines, 115 at depth 0 and 29 at depth 1 (nested).
- On the -Os task-scoped runs: 54 inlines, 48 at depth 0 and 6 at depth 1.
- **Branches folded: 0** in both (`firmgraal-native.md:83-88`).
- The doc states: "These runs establish correctness and coverage, not a speedup"
  (`:88`). So in FirmBOLT, the recursive specialization part has not yet folded anything on
  the corpus. What it delivered is transitive inlining.

### 2.7 These images were already built with FirmBOLT -O4

The scopt O4 design says the packet, uart and cli images were built with FirmBOLT's `-O4`
switch (`/data/scfix-o4/design/O4-design.md:42-43`, `:237-238`, `:1469`). So guest-level
recursive inlining, literal materialization and frame-slot promotion are already in the guest
code scopt optimizes. I did not check which FirmBOLT revision built them. scopt's recursion
problem is therefore at the host-code and TB level.

## 3. Mapping

| FirmBOLT -O4 component | scopt counterpart | In o4r3c | Recursive in scopt today |
|---|---|---|---|
| Transitive inlining worklist (2.1) | firmware-inline region expansion (`scopt/analysis/firmware_regions.py:22-189`) | yes, `mode = "exhaustive"` (A5) | **Yes.** Nested calls expand into members with a longer `inline_context` (`:109-152`); branches are followed (`:161-177`); there is no depth bound (`:29-33`). o4r3c receipts: depth 5 / 5 / 5; nested calls 360 / 43 / 32; regions 53 / 27 / 14 |
| Per-nested-candidate ranking and budget (2.3) | profit model and `NestedProfitExpansion` (`scopt/analysis/firmware_profit.py:555-655`) | priced whole regions only (`cost_model = "profile"`) | **Partly.** Exhaustive attaches no expansion callback (`scopt/passes/firmware_policy.py:64-65`), and `flat_fallback` is false because `policy.expand_nested` is true (`scopt/passes/firmware_inline.py:300-304`). So A5 prices only the whole recursive region (`O4-design.md:211`). Only hot-loop and hot-loop-tasks gate each nested call (`firmware_policy.py:126-148`, `:307-347`), per callsite and context-insensitively (`firmware_profit.py:617-620`) |
| Cycle, depth and step guards (2.2) | `active_calls` semantic guard (`firmware_regions.py:135-140`) and `visited` set (`:71-78`) | yes | **Yes** for cycles; depth and steps are unbounded by design (`firmware_inline.py:597`) |
| Fact-gated nested edges and legality per round (2.1) | firmware-calls eligibility, `finalize` trusted checks per member (`firmware_inline.py:346-400`) | yes | **Yes**, per member and edge. Remaining boundaries (o4r3c packet): 6,199 `observation_boundary` exits (goto_ptr, exit_null, exit_requested; `firmware_regions.py:179-181`) and 400 `unverified_nested_call_exit` (`:100-108`) |
| Context specialization in each copy (`foldInlined`) | SPEC bundle: specialize-returns, constant-propagation, branch-folding, dead-host-ops | yes (O4-r1 bundle) | **One level, outside regions only.** CP, BF and DHO run once per variant made by specialize-returns ("edits on clones", `scopt/manager_b3.py:23-25`; `scopt/manager.py:1009-1020`). A variant is one TB (`specialize_returns.py:62-75`, `max_region_members=2` at :70), and no facts flow into region members |
| Copies inherit pre-inline optimizations (2.4 bottom-up) | region member bytes | – | **Only host-inline is inherited.** host-inline is a native generation before B3 (`scopt/commands/optimize.py:666-693`), so members copy host-inlined units. Members copy **pre-B3** bytes (`scopt/link.py:341-357`, `:1718-1730`; `scopt/formats/sc_v7.py:15-17`; QEMU `tcg/supercache.c:857-863`). They drop B3 GENERAL and SPEC slots and every PURE_REWRITE. `member_slot_skip` only makes that loss safe (`sc_v7.py:76-83`) |
| Late passes on inlined code (2.4 after) | nothing | – | **No.** firmware-inline is the terminal stage: layout passes are refused with regions (`link.py:1893-1896`; `scopt/o4.py:99-108`). Built-in: internal edges enter members at +15, past the poll and can_do_io prefix (`supercache.c:1534-1538`) |
| Inline in place, so every path runs the copy | regions entered via lookup only | – | **No (U7).** A region is entered in `lookup_entry` → `sc7_try_region` (`supercache.c:3950-3969`, `:3343-3391`). B3 chain slots bypass that lookup (inventory.md:434-436; `O4-design.md:133-137`). o4r3c packet: 1.35M root calls priced against 3,144 region entries in the held-out replay (`packet-w/gates/arms/o4r3c/result.json`, `region_entries`). O4-r1: 54 |
| BOLT Inliner before FirmGraal | host-inline (`hot_loop_only`, `min_site_calls 1000`, `trusted_selected_root`) | yes; 1 / 0 / 0 sites | **Bounded.** The selected root plus its direct children (`scopt/passes/host_inline_graph.py:53-60`, `:72-77`). A full transitive graph exists (`:15-21`, `:78-79`) but is measured slower: A4 −3.63% (A-SUMMARY.md:17, :86-89), A6 −4.51% (A2-SUMMARY.md:14). It skips already-spliced units (`host_inline.py:501-503`) |
| Literal materialization | none | – | no host-level analogue; already applied at the guest level (2.7) |
| Frame-slot promotion across calls | env-state traffic passes: eliminate-state-sync, region-state-dse and -forward, local- and native-scalar-cleanup | no (all excluded `measured_neutral`, `o4.py:111-120`) | no; unit- or variant-scope only, never region members |
| Intact return slots and link materialization | guarded `return` edges in regions (`firmware_regions.py:153-160`; `trusted_return_guard_problem`, `firmware_inline.py:376-381`) | yes | **Yes**: every nested return is a guarded internal edge |
| Greedy re-pricing after each accept | `pricing_order` covered-first, plus `captured` flow of earlier admitted regions (`firmware_inline.py:65-81`, `:412-417`, `:489-491`); WHOPR planner | yes | **Yes**: regions are priced against regions admitted earlier |
| Retry at a reduced scope | flat-plan fallback (`firmware_inline.py:419-460`) | **inactive in exhaustive** (`flat_fallback` false) | – |
| (B3 ranking) | chain-static `rank_by_net_work` (A3), 256-unit cap (`scopt/passes/chain_returns.py:51`, `:66`; enforced at `manager.py:1126-1130`) | yes | **No.** GENERAL slots never enter a variant and refuse variant sources (`chain_returns.py:10-16`); they are absent in region members. The cap binds on packet: 256 / 256 units, 16 `slot_budget` refusals in o4r3c and 27 in O4-r1 |

## 4. The structural gaps

### G1. Members copy the pre-B3 bytes, so B3 and B4 do not apply inside regions

- **Pricing.** firmware-inline prices a region unchainable when a member's source is
  B3-grown: "the pre-B3 member copies drop B3's guards" (`firmware_inline.py:206-214`;
  `firmware_profit.py:36-46`; `link.py:359-380`).
- **How many regions this affects** (receipts, `residual_exits_chainable` false):
  - O4-r1: packet 47 of 48, uart 19 of 19, cli 7 of 7.
  - o4r3c: packet 51 of 53, uart 26 of 27, cli 13 of 14.
- **The A1 result.** A1 priced these regions chained anyway. It measured packet +1.82% and
  uart −0.99% (A2-SUMMARY.md:15). The design attributes the risk to members that lose B3's
  guard on residual `goto_ptr` tails (`O4-design.md:467-470`).
- **FirmBOLT's equivalent.** Copy bodies that are already optimized.
- **The scopt fix.** Link each member from the post-B3 bytes of its source.
  - The QEMU trusted loader does not hash member code. Its comment says member code hashes
    are not checked for trusted caches (`supercache.c:1556`), and `sc7_verify_hashes`
    returns before hashing (`:1648`).
  - Its residual-slot check requires `e9 00000000` with the reset at field + 4
    (`supercache.c:1494-1530`). That is the slot shape of a B3 GENERAL slot
    (`scopt/guard.py:151`, `reset_off == field_off + 4`).
  - So a loader change may not be needed. This is unverified.
- **What scopt must do.**
  - Retarget the GENERAL reset `lea rax,[rip -> R_struct + n]` to the member's descriptor.
    `retarget_private_descriptor_refs` handles only captured `MOVI_EXIT_TB_PTR` sites
    (`link.py:277-300`), and the guard template is at `guard.py:4-19`.
  - Fix up `jmp tb_ret` and the pool call displacement (`guard.py:33-36`).
  - Reset SPEC slots in members to the F path, or give variants member predecessors.
  - Teach the V13 guard decoding to accept members.
  - Then the skip masks become 0 and `member_slot_skip` is moot.

### G2. Regions are bypassed by B3 chains (U7)

- **FirmBOLT has no counterpart.** An inlined copy is reached on every path to the call.
- **In scopt.** A trusted region is reached only when the caller's lookup runs
  (`supercache.c:3962-3969`). QEMU already chains into a trusted region entry at +0 when
  the chain's `to` is the entry descriptor (`supercache.c:4411-4443`, `:4469-4483`), but B3
  GENERAL and SPEC slots link the public TB.
- **Evidence.**
  - In the design: c7b3's regions are entered about 53 times per packet run, against
    41,700 for p7 (inventory.md:434-436; `O4-design.md:133-137`).
  - In this read: O4-r1's packet held-out replay counted 54 region entries; o4r3c's counted
    3,144 (`/data/scfix-o4/packet-w/gates/arms/{o4,o4r3c}/result.json`).
  - The o4r3c model prices 1,347,217 root calls on the training profile, which is
    dispatch-exact with chaining disabled (O4.md:83-84).
- **Caveat.** The replay input is held out, so the counts are not directly comparable. But
  the gap is roughly 400×.
- **What it means.** The recursive region work is mostly not executed today. G2 is the
  largest structural lever.
- **The fix is linker or QEMU design work.** Either make the slot targets that are region
  callers resolve to the region entry, or make the main loop's `tb_add_jump` for such a
  target chain to the entry at +0.
- **Risk.** Regions become hot, so these move onto hot paths: entry descriptor rebuilds,
  the +0 poll, and the deferred PendSV at ISB edges (I1).

### G3. No context specialization or cleanup inside regions

- **FirmBOLT's `foldInlined` has no scopt counterpart in regions.** The SPEC bundle is one
  TB deep and runs only on B3 variants. Its facts come only from certified callee-summary
  rows at return exits (`specialize_returns.py:1-26`).
- **The analogue.**
  - Run hconst, CP, BF and DHO, and the B4 and region-state passes, over each member under
    facts valid in that inline context.
  - Patch the member copy: per-member patched bytes in `_link_firmware_regions`, plus
    validation.
  - No QEMU change, by the hash fact in G1.
- **Expected effect: modest.**
  - The SPEC bundle is near-inert on public code: 0 / 9 / 1 variants.
  - X4 (remove the SPEC bundle) passed REMOVE (A2-SUMMARY.md:19).
  - FirmBOLT's own recursive folding folded 0 branches on its corpus (2.6).
- **The blocker is the fact source.** scopt has no "call-site arguments" fact for a member.

### G4. Exhaustive regions are priced only as wholes

- **FirmBOLT prices every nested candidate on its own:** weight × edge weight, divided by
  marginal bytes.
- **In A5.** A whole recursive region is accepted or refused. o4r3c refused 140 / 175 / 194
  roots as `not_profitable` (receipts).
- **The fix is small: attach `NestedProfitExpansion` in `_exhaustive`.** Do it when the cost
  model is on and `nested = "profit"`, as `_hot_loop` does (`firmware_policy.py:133-148`).
  Also enable the flat fallback, so a root whose full recursion is unprofitable can still
  be admitted in a pruned form.
- **No QEMU change.**
- **Expected.** It could admit some of those roots. They matter only once G2 is fixed.

## 5. What "all our optimizations in a recursive manner" means for scopt O4

These are in priority order. Each one is the scopt image of a FirmBOLT -O4 property.

1. **R3: B3 output enters regions (U7).** This corresponds to "inline in place". It needs
   linker and QEMU design work and is large. It is the largest expected gain (G2).
2. **R2: members inherit post-B3 code (G1).** This corresponds to "copy cleaned bodies". It
   is medium to large, in scopt's linker and validator. A QEMU change is probably not
   needed (unverified). It removes the cause of A1's uart loss, and with R4 it lets more
   regions be admitted without a loss.
3. **R4: price nested calls separately in exhaustive mode, with the flat fallback (G4).**
   This corresponds to "rank every nested candidate". It is a small change in scopt.
4. **Lift the 256-unit B3 cap.** It is small, the `MAX_UNITS` / descriptor budget in
   `chain_returns.py:51` and `:66`. The cap binds on packet, and R2 and R3 raise slot
   demand. The A3 prediction was at most 1-2% on packet (`O4-design.md:488-492`).
5. **R5: context specialization and cleanup inside members (G3).** This corresponds to
   `foldInlined` plus the late passes. It is large, with a modest expected gain.
6. **Keep host-inline bounded.** The transitive helper graph is the one recursion measured
   harmful: A4 and A6, and unbounded w7 at 0.69-0.91× (O4.md:195).

## 6. Per-optimization detail

See the StructuredOutput items. They cover every FirmBOLT -O4 component above, each with its
scopt counterpart, what is missing for it to apply recursively, the concrete change, effort,
blockers, expected effect and risk.

## 7. How the receipt numbers were read

```
PYTHONDONTWRITEBYTECODE=1 python3 -c '...json.load(open("/data/scfix-o4/<w>-w/opt/<arm>/optimize.json"))...'
```

I read the `passes[*].stats` of each pass. For firmware-inline I read
`json.loads(detail)["embedded"]` (with `residual_exits_chainable`, `expansion_depth` and
`profit.calls`) and `["residual"]` reasons.

Region entries came from `grep region_entries /data/scfix-o4/packet-w/gates/arms/<arm>/result.json`.
The uart and cli gate results for o4r3c were not at the same path, so I have entry counts
for packet only.
