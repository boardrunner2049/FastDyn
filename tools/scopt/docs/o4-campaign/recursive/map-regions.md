# Map: firmware region formation and region-internal optimizations (recursion audit)

Scope: scopt (fd-int `tools/scopt`, branch o4 at b35cdb6) and the QEMU runtime
(qemu-fu treb-wip at aacca05). Nothing was built, timed or edited. Paths are relative
to `/data/fastdyn-work/fd-int/tools/scopt` unless they start with `qemu-fu/` (that is
`/data/fastdyn-work/qemu-fu`) or `/data/`. Receipt numbers are read from
`/data/scfix-o4/<w>-w/opt/<arm>/optimize.json` (the `firmware-inline` pass `detail`).
The runtime counters come from `/data/scfix-o4/<w>/timing-*/<arm>/*/regions.json`.

The user's goal: "the key is to have all our optimizations in recursive manner thats it".
The FirmBOLT analogue is `-O4` (c448884, 9ef83cc). In FirmBOLT, every call that gets
copied into a caller becomes a new inline candidate. Branch folding and frame-slot
promotion then run on the inlined copies.

## 0. Findings in one page

1. **Call expansion is already transitive and unbounded wherever a mode nests.**
   `build_firmware_region` runs a worklist over `(uid, inline-context)` members. It has
   no depth, member, growth or iteration bound (`scopt/analysis/firmware_regions.py:29-34`).
   Only two things stop it:
   - a visited set, which ends CFG back-edges (`:71-78`);
   - a semantic recursion guard, which turns recursive calls into side exits (`:134-140`).

   o4r3c's priced exhaustive regions reach depth 5 / 5 / 5 and nest 360 / 43 / 32 calls.
   O4-r1 (hot-loop) nests 289 / 26 / 19. Unpriced exhaustive (F2) reached depth 8-9
   (`/data/scfix-o4/design/inventory.md:293`).

2. **The other optimizations are what is not recursive.** In o4r3c:
   - **No nested gate, no fallback.** `exhaustive` has neither a nested profit gate nor
     the flat fallback (`scopt/passes/firmware_policy.py:66-67`;
     `scopt/passes/firmware_inline.py:300-304`). Each root's whole recursive closure is
     admitted or refused as one unit. Receipts show 3 packet roots lost this way (§3.2).
   - **Chainability is decided per region.** `region_exits_chainable` judges the whole
     region, not each member (`scopt/link.py:359-380`). One B3-grown member anywhere in
     the closure prices every residual `goto_tb` of the region at a full 40 ns dispatch
     (`scopt/analysis/firmware_profit.py:485-491`). This is why the 2.3M-call CRC root
     is priced at −22.2 ms.
   - **Members are pre-B3 copies.** No B3 transform reaches the inlined code (B3 guards,
     SPEC variants, CP/BF/DHO, B4 rewrites):
     - the linker lays out members from the pre-B3 snapshot units
       (`scopt/link.py:1724-1727`, `:1913`, `:2001-2002`);
     - members are bound to the pre-B3 content hash (`scopt/manager_b3.py:388-393`).
   - **Region-internal passes never touch firmware members, and O4 excludes them.**
     `region-state-dse`, `region-state-forward` and `native-scalar-cleanup` run on SPEC
     variants. `eliminate-state-sync` and `local-scalar-cleanup` run on public units.
     O4 excludes all of them as `measured_neutral` (`scopt/o4.py:111-120`).
   - **The root continuation is never expanded** (`firmware_regions.py:65-70`). So a
     region never closes the caller's loop: each iteration exits and re-enters it.
   - **Some calls are never expanded.** BLs in single-instruction sealed TBs and
     `blx reg` stay side exits (`firmware_regions.py:100-108`). That is 400 / 116 / 78
     residual rows.

3. **FirmLooper ∪ exhaustive is a no-op for speed as built.** With a profile,
   exhaustive already nominates every discovered call.
   - The only FirmLooper-only roots are single-instruction TBs. They come from
     `verified_entry_pcs`, and every one had 0 training executions
     (fl3b3p receipts: `calls 0.0`).
   - FirmLooper's real signal is the CRC task-loop roots that the conservative residual
     pricing refuses.
   - Unpriced, fl3b3 is +2.03% on packet [+0.90, +3.16]. A1 (`residual_goto_tb =
     "chained"`) admits the same roots and is +1.82% on packet but −0.99% on uart
     (`/data/scfix-o4/A2-SUMMARY.md:15,18`).
   - So "FirmLooper finds, PGO prices" needs fixed pricing (item B3-aware pricing),
     not a new selector.

4. **The U7 "bypass" premise is not supported by the runtime counters.**
   - Region entries are counted only when a dispatcher lookup returns the region
     (`qemu-fu/tcg/supercache.c:3343-3385`).
   - In sealed mode, a public TB chains into a trusted entry
     (`supercache.c:4498-4507`). The chain target comes from `tb_lookup`, which returns
     the region entry (`qemu-fu/accel/tcg/cpu-exec.c:1146-1150`,
     `supercache.c:3963-3970`).
   - The counters match this. O4-r1 packet: 52 lookup entries and 52
     `chain_installs_trusted_destination`. o4r3c packet: 2,817 and 56.
     uart and cli exhaustive: 28 / 24 and 14 / 13.
   - So after one lookup, regions are entered through chains, and those entries are
     not counted. "53 entries against 41,700" compares lookups, not executions.
   - Count chained executions before building U7.

## 1. How recursive is formation, per mode

Every mode plans with the same builder, `build_firmware_region`
(`firmware_inline.py:325-330`).

| | exhaustive (o4r3c) | hot-loop (O4-r1) | firmlooper-v3 | hot-loop-tasks (A2a) |
|---|---|---|---|---|
| roots | every discovered call (`firmware_policy.py:66-67`) | verified BLs in observed loops with ≥ `min_call_count` exact transfers (`:70-116`) | BLs at task-loop call PCs (`:273-288`) | hot-loop roots plus v3-only roots (`:299-318`) |
| nested calls | always inlined: `expand_nested=True`, no callback (`:413`) | `nested="profit"`: `NestedProfitExpansion` per call site (`:126-148`); `"public"` keeps them side exits (`firmware_regions.py:118-125`) | always inlined unless an RTOS boundary or ISB rule says PUBLIC (`firmware_policy.py:266-271`) | hot roots as hot-loop; v3-only roots get v3 rules first, then the profit rule (`:330-338`) |
| flat (one-level) fallback | **no** (`firmware_inline.py:304`: `policy.expand_nested` is True) | yes; the whole-region gate picks the better (`:418-466`); used for 10 / 3 / 2 roots | no | yes (`flat_expansion`, `firmware_policy.py:347-348`) |
| profit gate | whole region only (`cost_model="profile"`) | whole region plus nested | none unless `cost_model="profile"` (fl3b3p) | whole region plus nested; required (`:305-308`) |
| `nested` param | ignored | read (`:126-128`) | ignored | must be `profit` |
| verified single-instruction BLs | no (`analysis/firmware_calls.py:160-165`) | no | yes, via `verified_entry_pcs = loops.call_pcs` (`firmware_inline.py:232-236`) | no (discovery is hot-loop's) |

**What recursion means in the builder** (`firmware_regions.py:22-189`):

- **Member identity is `(uid, context)`.** The context is the tuple of call-site IDs on
  the inline path (`:46-54`, `:141`). Every distinct call path clones the callee again.
  This is virtual cloning, recorded as `virtual_clones: "inline_context"` at
  `firmware_inline.py:576`.
- **Branches stay in the current context** (`:161-177`).
- **The nested continuation is expanded.** A nested call enqueues both its callee (new
  context) and its continuation (enclosing context) (`:141-151`). So calls after an
  inner return are inlined too.
- **The root continuation is not expanded.** The root enqueues only the callee
  (`:62-70`). The continuation member's exits, and the caller's exits other than the
  call, are reported as `continuation_exit` and `caller_exit`
  (`firmware_inline.py:35-62`). o4r3c has 119 / 57 / 32 and 53 / 27 / 14 such rows.
- **Returns are guarded private edges** to the frame's `return_member` (`:153-160`).
- **Recursion and cycles:**
  - `active_calls` carries `(callee uid, key)` down the path. A nested call to a callee
    already on the path becomes a `recursive_call_edge` side exit (`:134-140`). No
    bounded unrolling exists. No o4r3c receipt has this reason.
  - The visited set ends intra-context CFG cycles (`:71-78`).
  - Pricing solves member flow SCC by SCC. A closed loop with no leak takes the
    observed executions (`firmware_profit.py:431-447`).
- **Side exits that stop recursion:**
  - `unverified_nested_call_exit` (`:100-108`): 400 / 116 / 78 rows in o4r3c (packet:
    200 `exit_requested`, 167 `goto_ptr`, 33 `goto_tb`).
  - ineligible nested calls (`:111-117`).
  - `missing_capture` / `target_key_ambiguous` (`:178`): 7 / 3 / 5.
  - `observation_boundary` for `goto_ptr` / `exit_null` / `exit_requested`
    (`:179-181`). There are 6,199 / 999 / 1,307 rows, but almost all are
    `exit_requested` poll exits that trusted members never take
    (`firmware_profit.py:51-52`).
  - `unpatchable_native_exit` from trusted sanitizing (`firmware_inline.py:370-398`):
    3 / 10 / 14.

**Hot-loop's nested gate is greedy, one level deep and context-insensitive**
(`firmware_profit.py:578-655`):

- The gate prices each nested call as a one-level probe region: `expand_nested=False`,
  with no `captured` flow and no trusted entries (`:630-639`).
- The gain counts only the callee members, the call edge and an avoided unchained
  dispatch (`:555-575`).
- The decision is cached per `callsite_id` across all roots and contexts (`:617-620`).
- So a nested call whose own body is unprofitable is refused, even when its callees
  would pay. A call reached through a cold context is decided on its global flow.

## 2. What bounds recursion

- **No explicit bounds.** `Budgets` are all `None` (`firmware_inline.py:89-91`). The
  receipt says `max_depth: None` when nesting (`:597`). The builder docstring states
  "deliberately no depth, iteration, member, site, hotness, or growth bound"
  (`firmware_regions.py:31-33`).
- **Implicit bounds:**
  - **The firmware's acyclic call depth**, through the recursion guard.
  - **Price** (priced modes only):
    - footprint at `byte_ns` 0.25 ns per member byte per run
      (`firmware_profit.py:105`, `:502-506`);
    - root admission needs `saved ≥ 1.25·added`, `net > 0` and
      `net/call ≥ 1 ns` (`:516-523`);
    - in hot-loop only, the per-call nested gate.
  - **The link arena.** `firmware_region_arena_exhausted` (`scopt/link.py:2010-2017`);
    the sc6 arrays must end before 0xFF00 (O4-design risk 8).
  - **The per-planned-call `ctx.budget()` tick** (`firmware_inline.py:298-299`), whose
    wall budget is `None`.
- **Context cloning is the growth driver.** Unpriced exhaustive had 37k / 17k / 18.5k
  members (inventory F2). o4r3c has 7,052 / 1,228 / 1,470 members; its largest region
  has 971 / 186 / 281 members.

## 3. Residual exits, regions calling regions, regions inside regions

### 3.1 Residual exits

- A residual `goto_tb` is priced 0 ns when the region is chainable and 40 ns when it is
  not (`firmware_profit.py:485-491`).
- `auto` makes a trusted region unchainable when **any** member's source is B3-grown
  (`:218-239`, `scopt/link.py:359-380`). This holds even with `member_slot_skip = on`,
  which makes those exits chainable at runtime (Q8: residual exits 10→1, 7,673→2,
  10,488→3; `/data/scfix-o4/design/inventory.md:244`).
- Residual `goto_ptr` exits are priced like public code ("nothing",
  `firmware_profit.py:51-52`). The member copy, however, has lost B3's guard
  (`:40-46`), so it pays a full `helper_lookup_tb_ptr` where public code pays a guard.
- Runtime: member descriptors arm residual `goto_tb` slots after their first use (Q6).
  B3-registered slots are skipped (Q8; `scopt/link.py:1747-1764`; o4r3c packet has 764
  `trusted_residual_slots_skipped`).

### 3.2 What o4r3c's whole-region gate costs

These are packet roots that O4-r1 admitted and o4r3c refused (same callsite IDs; the
host-inline params are identical, so the source hashes match).

- **666a8…** (86,306 calls). O4-r1 admitted it as a **flat** fallback at
  +741,190 ns. o4r3c's recursive plan adds one `residual_goto_tb_unchained` per call
  (3,452,240 ns added), so it nets −415,735 ns and is refused.
- **a59e6…** (14,567 calls). Flat +17,077 ns against recursive −139,276 ns. Same cause.
- **3e5a7…** (14,568 calls). In O4-r1 it had 5 members at depth 1 with its nested
  calls kept public (not a flat fallback), +118,720 ns.
  Recursive: 31,865 B and −14,318 ns, again from one unchained residual per call.
- **Two roots are not lost.** 84046… and dc3e1… went to `calls = 0` / `1` because
  bigger o4r3c regions now cover their callers (`captured`). That is the intended
  covering behaviour.

The model loses 0.88 ms of the 21.21 ms `profit_net_ns` on packet; uart and cli lose
nothing.

### 3.3 Regions calling regions (T->T, FU1/Q7)

- **At plan time there is no cross-region call edge.** A nested call is cloned inline
  (edge kinds are call, branch, return and side_exit; `firmware_regions.py:105-185`).
- **At runtime**, an armed residual slot whose destination is a trusted entry chains to
  that entry at +0, where its poll runs (`supercache.c:4395-4443`, `:4462-4489`).
  Destinations are limited to regions whose entry poll verified (`:1534-1560`).
- **Pricing charges each such exit a full dispatch** (`firmware_profit.py:47-50`,
  `:486-488` through `entries_into`). It is conservative since counters revision 4
  (inventory F12).
- **Observed T->T installs:** packet 0 (O4-r1, o4r3c); cli o4_pwhopr 1; w7bd_b3 9.

### 3.4 Regions inside regions

- Overlap is resolved by cloning plus covering-first pricing, not by merging.
- `pricing_order` prices a root after the regions that contain its caller
  (`firmware_inline.py:65-81`).
- `captured[uid]` subtracts the flow that admitted regions already run privately from a
  later root's entries (`:412-414`, `:490-491`; `firmware_profit.py:426-428`).
- At runtime, one region per caller source TB wins: the first in the directory
  (`supercache.c:1449-1456`).

## 4. Region-internal optimizations

None of these touches firmware-region members today.

| Pass (file) | What it does | Scope | O4 decision (`scopt/o4.py`) |
|---|---|---|---|
| region-state-dse (`passes/region_state_dse.py`) | NOPs D1 env stores that a SPEC variant's specialized CFG makes dead (`:64-80`) | one SPEC variant (`unit_scope="region"`, `passes/constant_propagation.py:33-43`; `manager_b3.py:23-26`) | excluded, measured_neutral, fixture 0.977× (`:117`) |
| region-state-forward (`passes/region_cleanup.py:286-316`) | NOPs a store of a value hconst proves is already there | SPEC variant | excluded, 0.987× / 0.976× (`:118-119`) |
| native-scalar-cleanup (`region_cleanup.py:207-229`) | fixed-point removal of idempotent, forwardable and available scalar ops; range branch folding | SPEC variant | excluded, 0.964× (`:115-116`) |
| local-scalar-cleanup (`region_cleanup.py:232-283`) | the same, on every public unit | public units | excluded, 0.992× (`:113-114`) |
| eliminate-state-sync, B4 (`passes/eliminate_state_sync.py:81-140`) | NOPs syncdead D1/D2/D3 stores | public units | excluded, +0.14% (`:111-112`) |
| fold-exit-pc-update (`passes/pc_update.py`) | folds relative PC updates before exits | public units; refuses once any B3 state exists (`:21-24`) | excluded, 0.999× (`:120`) |
| irq-entry-mem-compare (`passes/irq_entry.py`) | fuses the entry IRQ-poll load and test | public units; needs canonical public v7 (`:23-26`) | not_applicable_sealed (`:91-93`) |
| retire-ordinary-exits (`passes/ordinary_exit.py`) | returns low-reuse direct exits to dispatch | exclusive pipeline only (`scopt/manager.py:927-930`) | unmeasured_low, works against chaining (`:121-122`) |

**Why none of them reaches inside regions:**

1. The `"region"` passes edit SPEC variants (B3 multi-TB variants), not firmware
   regions.
2. The public-unit passes do edit public units, but members copy the pre-B3 bytes
   (`scopt/link.py:1724-1727`; `by_uid` at `:1913` is passed at `:2001-2002`, never
   `eff`).
3. SPEC links can never arm on a region's caller source: `sc6.live` is set only on the
   public lookup path (`supercache.c:4120`; cli c7b3's one SPEC link never arms,
   `/data/scfix-r2/TIMING.md:302`).

**Why O4 excludes them:** they were measured neutral on compute, fixture or FreeRTOS
workloads (`o4.py:111-120`; `docs/O4.md:202`). None was ever measured on the members
of firmware regions, because none of them can reach those members.

**What does already apply inside regions, at every depth:**

- **Trusted ABI.** Members are entered at +15 on internal edges, which skips the IRQ
  poll and the `can_do_io` store (Q1; `firmware_profit.py:364`).
- **Direct internal jumps.** Internal `goto_tb` and trusted `goto_ptr` tails become
  direct `jmp rel32` (`scopt/link.py:1830-1850`, tail rewrite at `:1842`).
- **Guarded private returns** (378 / 85 / 63).
- **Host-inline splices.** host-inline runs as a native stage before B3
  (`commands/optimize.py:691`, `:710`), so every clone copies the spliced unit. It
  applies to 1 / 0 / 0 sites.

## 5. Can FirmLooper task-loop roots be unioned with exhaustive priced selection?

- **Mechanically, yes.** `hot-loop-tasks` shows the pattern
  (`firmware_policy.py:299-348`): union the roots, apply v3's boundary rules only to the
  v3-only roots, and price those roots last (`price_after`). An "exhaustive-tasks" mode
  is the same pattern with `_exhaustive` in place of `_hot_loop`.
- **But exhaustive already contains every v3 root that discovery finds.** The union only
  adds:
  - v3's single-instruction BL TBs (`verified_entry_pcs`; `firmware_calls.py:51-62`).
    fl3b3 packet has 7 of its 15 roots there (0x48ec, 0x49dc, 0x4a02, 0x4a28, 0x4a70,
    0x4b1c and 0x85aba, each a second, single-instruction TB). fl3b3p prices every one
    at `calls 0.0`, so they never ran in training.
  - fidelity rules (RTOS boundaries, ISB), which only remove inlining.
- **What FirmLooper does find that matters on packet** is the CRC task-loop roots
  0x49dc, 0x4a02, 0x4a28 and 0x4a70, plus 0x85aba. Pricing refuses them in every
  priced mode, because each call adds one unchained residual dispatch. For 0x4a70:
  2,317,210 × 40 ns, net −22.2 ms.
- **Unpriced, they make fl3b3 fast on packet:** +2.03% [+0.90, +3.16] against O4-r1,
  with uart −0.12% and cli −0.32%. A1 admits the same roots: packet +1.82%
  [+0.61, +3.03], uart −0.99% [−1.81, −0.17].
- **So:**
  - The profile-free signal becomes useful once pricing stops refusing roots for exits
    that `member_slot_skip` makes chainable (item "B3-aware residual pricing").
  - A profile-free O4 is impossible anyway, because B3 needs the profile
    (`/data/scfix-o4/design/O4-design.md:151`).
  - Where a root is `profit_estimate_unknown`, FirmLooper could be its prior.
    o4r3c has none.

## 6. Items

These are all the optimizations in this subsystem, plus the gaps found. The effort
scale is none, small, medium, large.

1. **Recursive call expansion (`build_firmware_region`)**. In o4r3c.
   - Fully transitive (§1).
   - Gap: the root continuation is not expanded, and BLs in single-instruction TBs and
     `blx reg` are not expanded.
2. **Exhaustive selection**. In o4r3c.
   - Every call is a root; its nesting is ungated.
3. **Profit-gated nesting (`NestedProfitExpansion`, `nested="profit"`)**. Not in o4r3c.
   - Recursive, but one level deep, context-insensitive and greedy.
   - Change: return it from `_exhaustive` when it is priced.
4. **Flat fallback (whole-region gate)**. Not in o4r3c.
   - Change: enable it when `profit is not None`, and compare N plans: gated recursive,
     ungated recursive, flat.
   - Expected: +0.88 ms model on packet.
5. **Whole-region admission (`firmware-dispatch-v1`)**. In o4r3c.
   - Applies to the whole recursive closure.
   - Its per-region chainability predicate is not recursive-aware.
6. **Covering-first pricing and `captured` flow**. In o4r3c.
   - Handles regions inside regions correctly.
7. **Residual `goto_tb` pricing (`auto` / A1 `chained`)**. `auto` in o4r3c.
   - Region-wide predicate.
   - Change: per-member B3-aware pricing. A1 alone is a no-code arm.
8. **T->T pricing (F12) and runtime chaining (Q7)**. Runtime chaining is in; pricing is
   conservative.
   - Change: price trusted→trusted exits at one poll for counters revision ≥ 4.
9. **Trusted ABI, internal edges and guarded returns**. In o4r3c.
   - Already recursive.
10. **Member descriptors and residual chaining (Q6), member skip (Q8)**. In o4r3c.
    - Recursive (per member).
11. **Discovery of single-instruction BLs with training evidence**. Not built.
12. **Indirect-call (`blx reg`) promotion inside regions**. Not built.
13. **Root-continuation expansion (closing the caller loop)**. Not built.
14. **FirmLooper v3 selection**. Not in o4r3c; RTOS boundaries apply recursively.
15. **hot-loop-tasks union (A2a)**. Not in o4r3c; +0.98% packet, p 0.10.
16. **Exhaustive ∪ FirmLooper**. Not built; no-op as priced.
17. **ISB side exit (I1)**. Not in o4r3c (fidelity).
18. **WHOPR planner**. In o4r3c; only changes the order.
19. **B3 inside region members**. Not built; large, plus QEMU work.
20. **U7 B3 slots entering regions**. Premise unverified; add a counter first.
21. **SPEC on region sources**. Never arms.
22. **The eight region and state passes in §4**. Excluded; they never reach members.
23. **New cross-member state DSE and return-guard elision**, the FirmBOLT
    frame-slot-promotion analogue. Not built; high risk, because trusted members have
    no restore data (`scopt/link.py:1808-1810`).
24. **Host-inline inherited by clones**. In o4r3c; recursive by construction.

**Ranked recommendations** (speed per effort, with evidence):

- **(a) Close o4r3c's flat and nested gap.**
  - Change: in `_exhaustive`, return `NestedProfitExpansion` with
    `expand_nested=True` when `inputs.profit` is set. At `firmware_inline.py:304`,
    allow the fallback whenever the selection carries a profit-gated expansion, and also
    try the ungated recursive plan.
  - Never worse in the model. +0.88 ms model on packet.
- **(b) B3-aware, per-member residual and internal pricing.**
  - Change these three things:
    - price `goto_tb` exits chained when `member_slot_skip` is on;
    - credit internal lookups only above B3's guard cost;
    - charge residual `goto_ptr` exits on B3-guarded tails at `lookup − guard`.
  - This is what admits the CRC roots soundly. Evidence: A1 +1.82% on packet and fl3b3
    +2.03% on packet, but A1 is −0.99% on uart.
- **(c) A no-code probe: o4r3c + `residual_goto_tb = "chained"`.**
- **(d) Verify U7 with a chained-entry counter** before designing it.
- **(e) Root-continuation expansion**, with back-edges into the entry member linked
  at +0 so every iteration still polls.
- **(f) Single-instruction BL discovery** by training evidence.
- **(g) B3 guards copied into members.** Members would copy the `eff` bytes, and QEMU
  would arm the B3 slots instead of skipping them.

Items 23 and the region-internal passes in §4 are low or negative value on current
evidence.
