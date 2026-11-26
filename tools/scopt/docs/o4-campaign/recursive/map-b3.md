# Map: the B3 passes and their reach (recursion audit)

Scope: scopt (fd-int `tools/scopt`, branch o4 at b35cdb6) B3 passes, the B3 manager, and the
QEMU runtime that executes B3 slots and firmware-region members (qemu-fu treb-wip, aacca05).
Read only. Every claim cites file:line. Paths without a prefix are relative to
`/data/fastdyn-work/fd-int/tools/scopt/`. `supercache.c` is `/data/fastdyn-work/qemu-fu/tcg/supercache.c`,
`cpu-exec.c` is `/data/fastdyn-work/qemu-fu/accel/tcg/cpu-exec.c`. Receipts are
`/data/scfix-o4/<w>-w/opt/<arm>/optimize.json` (`passes[].stats`). Triples are packet / uart / cli.

## 1. Short answers

1. **Do B3 passes see or change firmware-region members or region copies?** They see and change
   the *public* units, including every unit that later becomes a region member's source. They
   never see or change a region *copy*. Regions do not exist while B3 runs. They are planned
   afterwards by firmware-inline, the last pass, and materialized only at link time. Each member
   copies the **pre-B3** captured bytes (`scopt/link.py:1892`, `:1913`, `:2001-2002`,
   `:1660-1663`; comment `:1725-1731`). QEMU then refuses to arm the B3 slots in those copies
   (skip flags, `supercache.c:1288-1317`, `:3237-3240`). So nothing B3 does applies inside a
   region: no GENERAL chaining, no SPEC variant, no CP/BF/DHO patch, no B4 rewrite.
2. **Order.** In o4r3c the order is host-inline (split off as a separate native prefix stage,
   `scopt/commands/optimize.py:664-700`), then specialize-returns, CP, BF, DHO, chain-returns,
   chain-static, and firmware-inline last (`/data/scfix-o4/design/pipelines/o4r3c.toml:52-90`).
   The manager runs each entry exactly once, in order (`scopt/manager.py:773-777`). A pipeline
   entry accepts only `name`, `version`, `params` and `enabled` (`scopt/manager.py:314`,
   `:340-367`), so there is no repeat or fixpoint construct. firmware-inline never runs again
   after B3. It reads only one thing from B3: the set of grown units, which it uses to price
   regions whose members are grown as unchainable (`scopt/passes/firmware_inline.py:206-214`,
   `scopt/link.py:359-380`, `scopt/analysis/firmware_profit.py:40-46`).
3. **U7** ("B3 slots that enter regions"; `/data/scfix-o4/design/inventory.md:434-436`,
   `O4-design.md:133-137`, `:221`) is an unbuilt design item. Its premise rests on a counter
   that counts only lookup *misses*. In QEMU, a B3 GENERAL slot whose target is a region's entry
   PC already chains **into** the region (§5.1). The real non-composition runs the other way:
   regions contain no B3.
4. **The 256-unit cap** is `MAX_UNITS = 256` (`scopt/passes/chain_returns.py:51`). It is
   enforced in the pass (`:147`, `:208-210`, `:308-310`) and again by the manager
   (`scopt/manager.py:1126-1130`, budget from `chain_returns.py:66`). It binds only on packet
   chain-static: 27 exits refused in scan order (O4-r1 arms), 16 with rank_by_net_work (o4r3c).
   It never binds on uart or cli, and never on chain-returns.
5. **Applying B3 recursively** needs linker work (members copy post-B3 bytes, and B3 stubs are
   composed with the region's private edge stubs), one QEMU loader change (a member flag meaning
   "this copy contains the B3 slots"), validator and pricing updates, and, for SPEC/CP/BF/DHO,
   either a guard-free "region-context specialization" or QEMU support for sc6 links from member
   descriptors (§7).

## 2. Pipeline order and how the manager runs B3

| # | o4r3c entry (toml line) | Scope | Runs on |
|---|---|---|---|
| 0 | host-inline (`:52-55`) | native prefix stage | the input snapshot; produces a new snapshot (`optimize.py:664-700`; receipt `native_stage`, packet output `sha256:6aa36…`) |
| 1 | specialize-returns (`:57-61`) | snapshot | public units of the post-host-inline snapshot |
| 2-4 | CP, BF, DHO (`:63-73`) | "region" = one B3 **variant** C' | only the variants in the overlay (`manager.py:1009-1031`); no variants means `no_change/no_variants` |
| 5 | chain-returns (`:75-79`) | snapshot | public units; slots already claimed by SR are respected (`chain_returns.py:215-216`) |
| 6 | chain-static (`:81-85`) | snapshot | public units, same code path (`chain_static.py:118-119`) |
| 7 | firmware-inline (`:87-90`) | snapshot | builds regions from captured units (`firmware_inline.py:305`, `ctx.b3.snapshot.units()`); reads `grown_uids()` for pricing only (`:211-214`) |

- **B3 overlay.** B3 edits live in an immutable `B3State`: `exits`, `variants`, `rewrites`,
  `unit_code`, `facts` and `firmware_regions` (`manager_b3.py:294-307`). firmware-inline's
  `EMIT_FIRMWARE_REGION` only records plans there (`manager_b3.py:381-401`). No B3 pass reads
  `firmware_regions`. The only pass that reads B3 state is firmware-inline (grep of
  `scopt/passes`: only `specialize_returns.py:101` and `llvm_layout.py:65/90` read `grown_uids`).
- **Edits on clones compose.** CP, BF and DHO see a variant's *current* bytes, through
  `VariantView.analyses()` on the patched code (`manager_b3.py:779-826`). A B4 rewrite of C is
  pushed into every clone of C (`manager_b3.py:609-621`). This is the only "B3 runs on another
  B3 pass's output" path.
- **The B3 passes are not iterated.** DHO alone iterates to a fixpoint within one run, at most 16
  rounds (`dead_host_ops.py:150-185`).
- **Materialize.** A variant whose bytes equal C is downgraded to a GENERAL slot
  (`no_simplification`, `manager_b3.py:918-933`). Patches are composed per original instruction
  (`:873-904`).

## 3. What each B3 pass can reach

| Pass | Edits | Reach | Hard exclusions that block recursion |
|---|---|---|---|
| specialize-returns | CLONE + SPEC REDIRECT_EXIT + ADD_ENTRY + ATTACH_FACT | admitted return exits of public R with certified CalleeSummary facts (`specialize_returns.py:102-158`) | C grown or C == R (`:132-134`); C with native fragments, i.e. host-inline spliced (`:138-140`); budgets 2 per C, 64 in total, 10% growth (`:159-179`); the manager refuses a clone of a grown C (`manager_b3.py:446-451`) |
| constant-propagation | same-length `cp_xor_zero`/`cp_mov_imm` | env loads of fact fields inside a variant (`constant_propagation.py:250-273`) | variants only (`manager.py:1009-1031`) |
| branch-folding | `bf_jcc_to_jmp`/`bf_jcc_to_nop` | hconst-decided jcc inside a variant (`branch_folding.py:59-76`) | variants only |
| dead-host-ops | `dho_nop` | register-only dead ops in a variant, iterated (`dead_host_ops.py:150-185`) | variants only |
| chain-returns / chain-static | GENERAL REDIRECT_EXIT | admitted `return`/`static` goto_ptr exits of public R with profiled targets (`chain_returns.py:197-298`) | R is a variant source (`:205-207`; manager `manager_b3.py:499-500`); 2 goto_tb slots per TB (`:215-219`); 256-unit cap (§5.2) |
| specialize-edges (not in O4) | as SR, on static exits | off-page static exits with an exact static destination (`specialize_edges.py:67-70`) | refuses chains of specializations: a dest that is also a source, or units B4 rewrote (`specialize_edges.py:112-116`) |

Receipts for o4r3c (`passes[].stats`):

| Pass | packet | uart | cli |
|---|---|---|---|
| specialize-returns | 0 variants (`target_not_profiled` 343, `fact_not_constant` 40) | 9 variants | 1 variant |
| CP folded loads | 0 (`no_variants`) | 6 | 1 |
| BF decided branches | 0 | 7 | 1 |
| DHO nopped | 0 | 7 | 1 |
| chain-returns slots / units | 47 / 38 | 43 / 31 | 28 / 21 |
| chain-static slots / units | 256 / 256 (`slot_budget` 16) | 50 / 50 | 43 / 43 |
| grown units (sidecar `x_b3.grown`) | 294 | 88 | 65 |

## 4. How region members are built and run, and why B3 is absent inside them

### 4.1 Linker (scopt)

- **Members copy captured units.** `NativeLinkerV2.link` builds `by_uid` from the captured
  snapshot (`link.py:1892`, `:1913`). Public units get rewrites and growth through
  `effective_units` (`:1865-1883`, `:1919`). Regions are laid out from `by_uid` (`:2001-2002`),
  and each member's preimage is `by_uid[member.source_uid]` (`:1660-1663`). The docstring says
  so: "Members copy the pre-B3 bytes … while QEMU arms slots from the source's output record"
  (`:1725-1731`).
- **Region-internal edges already use B3's guard machinery.** A member exit with an internal
  target (every return in trusted regions) is grown with a private GENERAL guard stub on a
  *free slot of the pre-B3 source*. The slot is unregistered and its reset is `jmp F`
  (`link.py:1665-1716`; "A firmware-region edge is an unregistered private jump", `:1696-1699`).
  The source must still have its original lookup tail (`:1674-1677`), and the source must have a
  free slot (`firmware_region_slot_representation`, `:1681-1685`). So the inliner does apply
  B3's mechanism inside regions, but only to its own internal targets.
- **Skip masks.** For each member, `added_goto_tb_slots(pre-B3 record, output record)` is the
  mask of slots that B3 registered and the copy lacks (`link.py:341-357`, `:1748-1756`). A
  non-zero mask without `member_slot_skip` falls back to the shared descriptor
  (`b3_registered_slot`, `:1750-1751`).
- **Composition rules.** Safe regions plus any B3 edit fail the link
  (`firmware_region_safe_composition`, `link.py:1903-1907`). Layout placement plus regions also
  fails (`firmware_region_terminal_stage`, `:1893-1895`).

### 4.2 QEMU (aacca05)

- **Region entry happens only through lookup of the entry member's public record.** The source
  index maps only the entry member's source (`supercache.c:1448-1456`). `lookup_entry` calls
  `sc7_try_region` first (`:3963-3969`), and in trusted mode it returns the region's entry
  descriptor (`:3378-3391`). Member descriptors are chain sources only and are never put in a
  lookup structure (`:3253-3259`).
- **Chains into regions are allowed.** The main loop installs `tb_add_jump(last_tb, n, tb)` for
  whatever `tb_lookup` returned (`cpu-exec.c:1111`, `:1145-1149`). From a public source into a
  trusted entry the chain is allowed when sealed (`supercache.c:4498-4507`). From a trusted
  member it goes to the destination entry at +0, so the poll runs (`:4464-4496`, `:4411-4443`).
- **Residual goto_tb slots of a member.** `sc7_init_trusted_descriptor` arms a member's
  residual slots from the *public source record's* `jmp_insn_offset`/`jmp_reset_offset`,
  skipping slots with skip flags and internal slots (`supercache.c:3208-3251`). The load-time
  shape check requires `e9 00000000`, `reset == field+4`, 4-byte alignment and field > 15
  (`:1495-1531`). `last_tb` is kept only for armed slots (`:4321-4381`; `cpu-exec.c:1178-1180`).
- **The loader pins skip flags to the sc6 slot set.** In a composed member-descriptor region,
  each member's flags must equal the B3 (sc6) slot mask of its source, or the load dies with
  "skip flags … != B3 slot mask" (`supercache.c:1288-1317`; comment `:880-890`: "Skipping only
  ever removes a chaining opportunity").
- **Residual goto_ptr exits** in a member copy run the original lookup tail (pre-B3), i.e.
  `helper_lookup_tb_ptr` then `tb_lookup` (`cpu-exec.c:384-421`), with no B3 guard.
- **SPEC links bypass lookup entirely.** `sc6_try_arm` patches R's slot straight to the variant
  built from the live general C (`supercache.c:2485-2521`). It arms when R or G is activated by a
  lookup (`:2524-2537`). A SPEC link whose R executes inside a region never fires from the copy.
  R2 saw this on cli: "c7b3's single B3 SPEC link never arms (links_armed 0), because it sits
  inside regions" (`/data/scfix-r2/TIMING.md:302`).
- **Trusted regions ignore mapping changes** (`supercache.c:3488-3492`). sc6 GENERAL slots on
  public code are unlinked on TLB/MPU changes (`docs/b3-passes.md:85-89`).

### 4.3 How much of B3 is lost inside regions (o4r3c bytes)

Computed from the sidecars `…/opt/o4r3c/store/artifacts/*/cache.sc.scopt.json` (`x_b3.grown`,
`x_b3.firmware_region_links`, `x_b3_manager.firmware_regions`, `x_b3_manager.regions`) and the
QEMU replays (`/data/scfix-o4/packet/timing-R3/o4r3c/heldout-1/regions.json`; on uart and cli
o4r3c has o4_pwhopr's bytes, R3-SUMMARY "alias", so `/data/scfix-o4/{uart,cli}/timing-R3/o4_pwhopr/commands/regions.json`):

| | packet | uart | cli |
|---|---|---|---|
| regions / member copies / distinct member sources | 53 / 7,052 / 826 | 27 / 1,228 / 434 | 14 / 1,470 / 433 |
| B3-grown units that are region member sources | 120 of 294 (41%) | 72 of 88 (82%) | 56 of 65 (86%) |
| region entry (caller) sources that are B3-grown | 44 of 53 | 23 of 27 | 13 of 14 |
| member copies of grown sources | 707 | 143 | 128 |
| B3 slots skipped in copies (sidecar `skipped_b3_slots`) | 799 | 178 | 153 |
| skipped at runtime (`trusted_residual_slots_skipped`) | 764 | 178 | 153 |
| residual slots armed (`trusted_residual_slots_armed`) | 73 | 42 | 41 |
| SPEC links whose R is a region member | 0 of 0 | 5 of 9 | 1 of 1 |
| steady lookup traffic between member sources, share of all lookups (training profile) | 4.00M of 18.19M (22%) | 1.68M of 2.27M (74%) | 0.165M of 0.187M (88%) |

The last row matches inventory's "74% (uart) and 87% (cli) of B3's converted lookups lie on
edges that the regions make internal" (`/data/scfix-o4/design/inventory.md:87-88`). Some of that
traffic becomes internal edges inside regions, which already beat B3. The rest leaves member
copies through residual goto_ptr exits that have lost their B3 guard. No counter measures how
large that rest is (§8).

## 5. U7 and the 256-unit cap

### 5.1 U7: "B3 slots that enter regions"

- **Definition.** "U7: let B3-chained edges enter trusted regions. Today B3 chain slots bypass the
  lookup that enters regions (53 entries against 41,700 on packet). This needs linker or QEMU
  design work; nothing is implemented" (`/data/scfix-o4/design/inventory.md:434-436`). It is
  excluded from O4 as "unmeasured, design work" (`O4-design.md:221`; `docs/O4.md:208`) and is
  named as the fix for risk 5 (`O4-design.md:1450-1451`) and for A1 (`:467-473`).
- **The counter behind the premise counts lookup misses, not entries.** `region_entries` is
  `sc7.entries` (`supercache.c:1845`, `:1884`). It is incremented only inside `sc7_try_region`
  (`:3385`, `:3437`). That function is reached only from `lookup_entry` (`:3963-3969`), which is
  reached only after a jmp-cache and qht miss (`cpu-exec.c:249-258`, `:216-218`).
  - Entries through an installed chain are not counted, and neither are jmp-cache hits.
  - In most arms `region_entries` is about equal to `region_activations`: O4-r1 packet 52 / 46,
    uart 28 / 27, cli 14 / 14. o4r3c packet is the exception at 2,817 / 50, which is still only
    lookup misses.
  - `chain_installs_trusted_destination` is about one per region: 56 / 24 / 13 (O4-r1 packet 52).
  - So "53 entries" (O4-r1) means each region was looked up roughly once and was then reached
    through chains. It does not show that regions are bypassed. R2 itself noted that the large
    drop in region entries B3 causes "did not show up as a loss" (`/data/scfix-r2/TIMING.md:414`).
- **Which B3 slots already enter regions.**
  - A **GENERAL** slot whose target is a region's *entry* PC already chains into the region. The
    first hit exits to the main loop, `tb_lookup` returns the region entry (`supercache.c:3963-3969`),
    and `tb_add_jump` patches the slot to it (`cpu-exec.c:1145-1149`, allowed at `supercache.c:4498-4507`).
    No code is needed for this. Training traffic from grown units to entry sources is
    0.44M / 0.12M / 4.5k.
  - A **SPEC** slot never enters a region: it is armed directly to the variant
    (`supercache.c:2504-2511`). Today no SPEC target is a region entry source (0 of 0 / 9 / 1).
  - A slot whose target is a **non-entry** member (an inlined callee or continuation) reaches the
    public copy. That is by design: a region has one entry, and its members carry an inline
    context. Training traffic from grown units to non-entry member sources is
    3.51M / 1.63M / 0.16M, against 10.29M / 0.37M / 0.013M to non-members.
- **What a real U7 would need.**
  - (a) First, measure. Attribute `chain_installs_trusted_destination` to sc6-registered slots
    (`supercache.c:4510-4533` already sees `from`/`to`), and count chained region entries, for
    example by sampling.
  - (b) Multi-entry ("side-entry") trusted regions. The source index would map extra member
    sources to (region, member) (`supercache.c:1442-1457`). Each side-entry member would need its
    own registered descriptor, and the poll and can_do_io proof that `sc7_verify_poll_entries`
    does for the entry (`:1533-1604`). scopt would select side entries only where the member's
    internal returns are all guarded, so a foreign context falls back. QEMU change plus linker
    and validator work: large.

### 5.2 The 256-unit cap

- **Where it is enforced.**
  - `MAX_UNITS = 256` (`chain_returns.py:51`), also the descriptor budget
    `Budgets(max_units=MAX_UNITS)` (`:66`).
  - The pass reads `max_units` (`:147`). It refuses in scan order when unranked and without a
    byte limit (`:208-210`), and again during selection in every mode (`:308-310`).
  - The manager independently fails the whole proposal when it touches more than
    `budgets.max_units` units (`scopt/manager.py:1126-1130`, BUDGET_EXHAUSTED). So the existing
    `max_units` param (`chain_returns.py:55`) cannot raise the cap above 256: "raising it is a
    code change (B2)" (`scopt/o4.py:209-211`).
  - The cap is also the design budget "≤2 slots/unit, 256 units"
    (`docs/design/B3-B4-design.md:196`; `manager_b3.py:342-345`).
- **How often it binds.** These are all 23 arms per workload under `/data/scfix-o4/<w>-w/opt/*/optimize.json`.
  - packet, chain-static: 256 units, `refused:slot_budget` 27 in every unranked arm. In the
    ranked arms (o4_rank, o4r3c, o4r3d) it is 16, with `eligible_exit_plans` 272 = 256 kept + 16
    refused.
  - uart and cli, chain-static: 50 and 43 units, no binding.
  - chain-returns never binds (38 / 31 / 21 units). Its `slot_budget` 6 (uart) and 1 (cli) are
    not the cap: they come from `chain_returns.py:239-240`, exits whose targets SPEC already
    guards. They disappear in the no-SPEC arms (uart o4_nospec 52 slots, `slot_budget` 0), as
    `O4-design.md:1640-1642` says.
- **Capacity elsewhere.** The sc6 arrays must end by 0xFF00 in a composed file: 64 B per variant,
  32 B per slot, 16 B per link from 0x140 (`scopt/formats/sc_v6.py:71`, `:95`, `:107-110`). That
  is roughly 2,000 slots, against 303 (47 + 256) on packet today. QEMU needs no change.
- **Value.**
  - Per the design's own condition (`O4-design.md:220`), lifting the cap is worth doing if packet
    is sensitive to which exits get slots. A3 (ranking) measured +1.51% on packet [+0.48%, +2.54%]
    (`/data/scfix-o4/A2-SUMMARY.md:14`, `:72`). In R3 o4r3c measured packet +0.76% against
    o4_pwhopr's -0.36%.
  - Under ranking the 16 dropped plans have the lowest estimated savings. Their total is not in
    the receipts, which give only `estimated_net_saving_ns` 63.79 ms for the 256 kept.
- **Cheapest experiment.** `load_pipeline` does not reject a repeated pass name
  (`manager.py:340-367`), and each run's 256 is counted per proposal (`chain_returns.py:183`,
  `:308`; `manager.py:1127`). So listing chain-static twice would add the 16 leftovers without a
  code change. This is unverified: receipts and gates keyed by pass name may assume unique names.

## 6. Recursion versus FirmBOLT -O4

In FirmBOLT -O4 (c448884, 9ef83cc) the inliner's optimizations run again inside inlined callees.
The scopt analogue:

- the inliner is firmware-inline, whose exhaustive mode already expands nested calls
  (`firmware_inline.py:300-305`; packet `actual_expansion_depth` 5, `nested_inlined_calls` 360);
- the optimizations are B3.

Today, B3 has three properties:

- **(i)** It runs before the inliner and never after it.
- **(ii)** Its results are stripped from the inlined copies (pre-B3 members plus skip flags).
- **(iii)** It does not recurse on its own output: no slots on variants, no variants of variants,
  no SR→CP/BF/DHO→chain fixpoint.

The one recursive-looking piece is firmware-inline's private guard stubs on internal edges
(`link.py:1665-1716`), which is B3's GENERAL mechanism restricted to region-internal targets.

## 7. What recursive B3 requires, by level

**L1. GENERAL chaining inside member copies (residual goto_ptr exits).**

1. **Linker.** Lay out members from the effective (rewritten and grown) units instead of
   `by_uid`. Today `_link_firmware_regions(cursor, by_uid, …)` is called at `link.py:2001-2002`,
   and the same change applies to `_firmware_member_units` at `:1633-1716`.
   - **Slot offsets stay valid.** QEMU arms member slots from the public output record's offsets
     (`supercache.c:3229-3247`). A copy of the grown R has those offsets, because guard stubs are
     appended after the pool (`guard.py:2-5`).
   - **Shape check passes.** The GENERAL slot template is `jmp rel32 0` with a 4-aligned field
     and the reset right after it (`guard.py:13-16`), which is the shape QEMU verifies
     (`supercache.c:1515-1527`). Member code is 16-aligned (`link.py:330`, `:1804-1805`).
   - **Reset binds to the member descriptor.** The registered reset's struct LEA is a
     `MOVI_EXIT` RIPREL32 site (`guard.py:377-379`), so `retarget_private_descriptor_refs`
     already rebinds it (`link.py:277-317`, `:1822-1824`). `exit_tb` then names the shadow
     descriptor, and the main loop chains it (`supercache.c:4464-4496`).
2. **Composing with internal edges.** On a grown source the B3 tail is already `jmp S`, so
   `lookup_tails` misses it (`link.py:1674-1677`), and both slots may be taken (`:1681-1685`).
   - When the internal target is already one of B3's targets, patch that slot's rel32 to the
     member. QEMU treats it as internal (`supercache.c:1472-1484`, `:3241`).
   - Otherwise put an internal-target compare in front of B3's stub. This needs an unregistered
     private slot that does not collide with the 2 registered ones, or a stub template with three
     targets.
3. **QEMU.** Add a member flag such as `SC7_MEMBER_B3_COPY`: "this copy carries its source's sc6
   slots".
   - The loader's equality check (`supercache.c:1311-1317`) accepts it with skip bits 0.
   - `sc7_verify_residual_slots` and `sc7_init_trusted_descriptor` then verify and arm those
     slots as ordinary residual slots (`:1495-1531`, `:3229-3247`).
   - Mirror the rule in scopt's `read_sc7` and sidecar `loader_requirement`
     (`docs/b3-passes.md:145-149`) and in the capability table (`scopt/data/qemu_capabilities.json`).
4. **Pricing (F12).** Stop treating grown members as unchainable (`link.py:359-380`;
   `firmware_profit.py:40-46`, `:219-239`). Credit B3 only for lookups that B3 still chains in the
   copy.
5. **Per-context targets (optional).** Today a member copy would inherit the public R's targets,
   which come from the aggregate profile.
6. **Validation.** Composed artifacts are compile-only, and `validate_b3` refuses them
   (`composed_v7_audit_unsupported`, `docs/b3-passes.md:163-164`).
7. **Fail-closed hazard.** A wrong slot shape is `sc7_die` at QEMU load (`supercache.c:1525`), so
   readback must check it offline.

**L2. SPEC, CP, BF and DHO inside regions (region-context specialization).** Non-entry members are
entered only through internal edges (`supercache.c:3253-3259`, `:1448-1456`). Each inline context
has its own copy. So a member copy whose in-edges all carry a certified fact of their source exit
(CalleeSummary rows, `manager_b3.py:178-218`) can be patched **without a runtime guard**. This is
the direct analogue of FirmBOLT's recursive inlining. It needs:

- a member-copy view analogous to `VariantView` (`manager_b3.py:779-826`), with facts from
  in-edges;
- patch support in member layout, which today relocates the unpatched source body
  (`link.py:1816-1819`);
- a hostsym (V12) proof for member copies;
- new fact sources, such as call-edge argument facts, to find anything beyond today's near-inert
  return facts.

The alternative is a QEMU change so that sc6 links can originate from member descriptors (today
`sc6.live[s->src]` indexes public records, `supercache.c:2490-2491`). That is large.

**L3. Recursion on variants.** It needs three things:

- allow clones of grown units, and growth on variant sources (remove `manager_b3.py:446-451`,
  `:499-500` and `chain_returns.py:205-207`);
- have the linker clone the *grown* effective C;
- confirm that QEMU's variant build is consistent with that. `sc6_build_variant` copies G's jmp
  offsets and resets them (`supercache.c:2463-2476`), which is safe only if C' has G's layout.

Guarding a variant's own exits needs sc6 slot entries keyed by variant (QEMU indexes slots by
public record). Value on these workloads is about 0, since there are 0 / 9 / 1 variants.

**L4. Fixpoint and ordering.** With L1-L3 absent, running B3 after firmware-inline or twice
changes nothing except the cap (§5.2). The pipeline has no iteration key (`manager.py:314`), so a
fixpoint would be a manager feature: repeat the B3 group until `B3State` stops changing.

## 8. Measurements missing before building L1, L2 or U7

- **Region entries.** There is no QEMU counter for region entries through chains. Today's
  `region_entries` counts lookup misses (§5.1).
- **Residual lookups inside regions.** There is no counter of `helper_lookup_tb_ptr` calls made
  from trusted member code. This is the upper bound of L1's benefit: about
  (c_lookup − c_guard) = 6.1 ns per lookup that L1 would chain (`docs/b3-passes.md:215-216`).
  The training profile cannot see regions, because `sc7_try_region` returns NULL while
  profiling (`supercache.c:3348`).
- **The 16 cap-refused plans.** Their per-exit savings are not in the receipts.

## 9. Items (also returned as structured output)

1. **chain-returns.** In O4; public only; L1 needed for recursion.
2. **chain-static, with A3 ranking.** In O4; public only; L1; the cap binds on packet.
3. **Lift the 256-unit cap (B2/U2).** Small code change, or a repeated pass entry.
4. **specialize-returns.** In O4; public, un-grown C only; L2 or L3.
5. **constant-propagation.** Variants only.
6. **branch-folding.** Variants only.
7. **dead-host-ops.** Variants only; already a local fixpoint.
8. **specialize-edges.** Not in O4 (measured slower); refuses chains of specializations.
9. **B3 GENERAL chaining inside region member copies (L1).** The core recursion gap.
10. **U7, B3 slots that enter regions.** Partly already works for GENERAL slots targeting an
    entry PC; measure first.
11. **Region-context specialization (L2).** Guard-free CP/BF/DHO/SPEC on member copies.
12. **Variant recursion and a B3 fixpoint (L3/L4).**
13. **member_slot_skip.** In O4; the composition workaround that L1 replaces.
