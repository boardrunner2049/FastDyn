# Critique of the O4-rec design and its five maps

This is the adversarial review of the recursion investigation. In the campaign, the critique
step was killed before it ran, so the archived design had no review. This review was run
afterwards, on 2026-11-26, while the campaign was being documented. It is read-only: nothing
was built, run, timed or edited, apart from writing this file. It is not a campaign record,
so it is kept here and not in the `o4-campaign/` archive. It has no original under
`/data/scfix-o4`. [`o4-recursive.md`](o4-recursive.md) summarizes it.

**Superseded on the QEMU question; sizing contested (note added 2026-11-26).** A later
read-only analysis, [`o4-campaign/recursive/no-qemu-routes.md`](o4-campaign/recursive/no-qemu-routes.md)
(final, 08:17, 44,484 B, sha256 `eb2f2c0a…`, with its own adversarial check as §5), shows
that no stage needs a QEMU change, and scopt only rewrites the supercache, so §2.2's
conclusion that DESIGN's new member flag is needed no longer holds (see
[`o4-recursive.md`](o4-recursive.md), "Resolved"). It reads the same o4r3c caches and classifies the skipped
member slots differently. It counts a slot as covered when the member's `goto_ptr` exit
already has an internal edge: 770 / 162 / 147 of 799 / 178 / 153, leaving 29 / 16 / 6, all
in root-continuation members (its §2, reproduced in its §5.1). This critique counts a slot as
covered only when one of the member's internal edges goes to the slot's own target: 601 / 117
/ 114, leaving 198 / 53 / 37 (§2.3, §6 item 3). It also disputes this critique's conclusion in
§2.2 that DESIGN's new QEMU member flag is needed: its corrected verdict (its §5.0) is that no
part needs a QEMU change, with root-continuation expansion first and a twin source record as
a fallback that is correct only under sealed or exact-PC loads (its §5.3, §5.7). Neither
analysis has been checked against a build or a timed run. See
[`o4-recursive.md`](o4-recursive.md) §4 item 4 and §8.

**What was reviewed.** These are the archived copies in
[`o4-campaign/recursive/`](o4-campaign/recursive/) (originals in `/data/scfix-o4/rec/`):

- [`DESIGN.md`](o4-campaign/recursive/DESIGN.md), the O4-rec design;
- the five maps: [`map-regions.md`](o4-campaign/recursive/map-regions.md),
  [`map-b3.md`](o4-campaign/recursive/map-b3.md),
  [`map-hostinline.md`](o4-campaign/recursive/map-hostinline.md),
  [`map-others.md`](o4-campaign/recursive/map-others.md) and
  [`map-firmbolt.md`](o4-campaign/recursive/map-firmbolt.md).

**What they were checked against.**

- **scopt:** `/data/fastdyn-work/fd-int/tools/scopt` at HEAD `56c4c80`. Paths below are
  relative to it.
  - The maps were written at `b35cdb6`. The only files `56c4c80` changed are `scopt/o4.py`,
    the pipelines, the docs and three O4 tests (`git show --stat 56c4c80`).
  - So the maps' line citations still hold, except those into `scopt/o4.py`. For example, the
    `measured_neutral` exclusions that DESIGN cites as `o4.py:111-120` are now
    `scopt/o4.py:119-128`.
- **QEMU:** `/data/fastdyn-work/qemu-fu` at `aacca05`. `supercache.c` is `tcg/supercache.c`
  and `cpu-exec.c` is `accel/tcg/cpu-exec.c`.
- **O4-r3's own bytes:** the o4r3c build of each workload under
  `/data/scfix-o4/<w>-w/opt/o4r3c/`. O4-r3 built with `-O4` has the same cache.sc bytes as
  o4r3c. Three files were read there:
  - `optimize.json` (pass receipts);
  - `store/artifacts/*/cache.sc.scopt.json` (the sidecar: B3 plan and region plans);
  - `store/artifacts/*/cache.sc`, through scopt's own readers (`scopt.formats.sc_v6.read_sc6`
    for the sc6 slot table, `scopt.formats.sc_v7.read_sc7` and `decode_directory` for the
    region directory).

Triples are packet / uart / cli. §6 says how each new number was computed.

## 1. Verdict

1. **One "not recursive" claim of the maps is false, and the plan inherits the wrong
   priority from it.** map-firmbolt and map-others say B3 slots bypass regions (U7) and rank
   that as the largest lever. The code says otherwise. B3 GENERAL slots are linked by the main
   loop after a lookup, and that lookup returns the region. DESIGN §1.4(i), map-b3 §5.1 and
   map-regions §0.4 are right (§2.1).
2. **DESIGN's headline motivation for stage 3 rests on a misreading.** The "dead" B3 work on
   region-entry sources is a guard on the root call itself, 44 of 44 / 23 of 23 / 13 of 13.
   Inside the region that exit is already the region's internal call edge. Copying post-B3
   bytes into entry members would bring back nothing.
   - Across all members, 601 / 117 / 114 of the 799 / 178 / 153 skipped B3 slots guard a
     target that the same member already reaches through an internal edge.
   - Stage 3 can pay only on the remaining 198 / 53 / 37 slot copies (§2.3, §3.3).
3. **The single-instruction-BL and `blx reg` counts are wrong by a factor of two and of
   thirty.** The 400 / 116 / 78 rows are 200 / 58 / 39 call sites. Only 5 / 2 / 2 of the
   sites are indirect calls. So stage 4b (`blx reg` promotion) reaches almost nothing (§2.4).
4. **Two stages are underestimated or infeasible as written.**
   - The context-keyed nested gate of stage 2a cannot be computed while a region is built,
     because the profile has no per-context counts (§3.1).
   - Stage 3b's composition rule would put B3 guard compares on internal edges that are plain
     jumps today. And the linker failures it relies on to fall back are fatal today (§3.3).
5. **The poll argument of stage 4a is incomplete** (§3.4). A `goto_ptr` into a region entry
   skips the poll. The trusted contract already allows poll-free internal cycles, and 25 / 14
   / 13 o4r3c regions contain them.
6. **"All" is missing at least four optimizations** (§4).
   - The largest is region-aware B3: stop spending chain slots on exits that regions make
     internal.
   - Then the designed helper items C3 (MSR exit chaining) and C6 (inline lookup probe).
   - Then per-context B3 targets, and register promotion across member edges.
7. **Checked and found sound:**
   - the U7 counter correction;
   - the claim that B3 on entry sources never runs;
   - the stage-1 chain-static repeat (it lifts the cap without code);
   - the per-member runtime features: all 53 / 27 / 14 o4r3c regions are linked with member
     descriptors;
   - host-inline inheritance (the one packet splice has one member copy);
   - the A1 re-pricing numbers (16 roots, +81.0 ms model; 7 roots, +5.7 ms; 0).

## 2. Claims checked and found false or misread

### 2.1 FALSE: "B3's guarded slots hard-wire the public TB and never enter regions" (U7)

**Where it is claimed.**
- map-firmbolt §0 (bullet "scopt also has a gap"), §3 (row "Inline in place"), G2 and §5
  item 1.
- map-others §1.3, §4.18 and §7 item 1.

Both rank U7 as the largest expected gain.

**What the code does.**

| Step | Where |
|---|---|
| A GENERAL slot is "linked by the main loop after a lookup (stock `tb_add_jump`)" | `scopt/passes/chain_returns.py:8-9` |
| QEMU's own statement: "GENERAL slots chained by the ordinary loop" | `supercache.c:4108-4114` |
| sc6 arms only SPEC links directly (`sc6_try_arm` patches R's slot to the variant) | `supercache.c:2485-2521` |
| The unarmed slot exits; the loop calls `tb_lookup` | `cpu-exec.c:1111` |
| `tb_lookup` misses the jump cache and QHT, then reaches `lookup_entry`. In trusted mode `lookup_entry` calls `sc7_try_region` first and returns the region entry | `cpu-exec.c:200-221`, `supercache.c:3950-3969` |
| The loop chains the exited slot to that entry | `cpu-exec.c:1145-1149` |
| A public → trusted chain is allowed when sealed | `supercache.c:4498-4507` |
| `region_entries` (`sc7.entries`) is incremented only inside `sc7_try_region` | `supercache.c:3385` |

So `region_entries` counts lookups that missed both caches, not executions.

**The counters agree.** In the o4r3c packet held-out replay
(`/data/scfix-o4/packet/timing-R3/o4r3c/heldout-1/regions.json`):

- `region_entries` 2,817;
- `region_activations` 50 of `regions_loaded` 53;
- `chain_installs_trusted_destination` 56.

**What follows.**

- map-firmbolt's "roughly 400×" gap (1.35M priced root calls against 3,144 entries) compares
  training calls with cache misses.
- map-others' "regions are entered about once each" is the same misreading.
- The real non-composition runs the other way: regions contain no B3 (§2.3).
- DESIGN keeps U7 only as conditional stage 6 (row 19, side entries into non-entry members).
  That is consistent with this finding.

### 2.2 FALSE: "a [QEMU] loader change may not be needed" for post-B3 members (map-firmbolt G1, §5 item 2)

The loader requires a trusted member-descriptor member's flags to equal its source's sc6
slot mask exactly (`supercache.c:1309-1316`, `sc7_die` "skip flags … != B3 slot mask"). It
also dies on any flag bit outside the two skip bits (`supercache.c:1301-1303`).

- A post-B3 copy that carries the slots must have skip bits 0. So the source's non-zero mask
  is refused.
- The legacy shared-descriptor layout would load, but it arms no residual slot. The copied B3
  slots would then exit to the dispatcher every time.

DESIGN's version (one new member flag, row 34 and §3.3 3b) is the correct one.

### 2.3 MISREAD: "B3 on region-entry sources is dead code … the clearest case for making B3 recursive" (DESIGN §0 item 3(ii), §1.4(ii), §3.3 "entry members first"; map-b3 §1 item 1)

**The first half is true.**
- `lookup_entry` returns the region before it reaches the public TB (`supercache.c:3962-3971`).
- In a timed trusted run `sc7_try_region` declines only while profiling, single-stepping,
  under break- or watchpoints, with icount, or with the `no-regions` token
  (`supercache.c:3347-3357`, `:3381-3382`).

So the public code of an entry source never runs, and neither does any B3 slot on it.

**The conclusion does not follow.** Every B3 slot on every entry source guards the root
call's own target:

| o4r3c | packet | uart | cli |
|---|---|---|---|
| regions | 53 | 27 | 14 |
| entry sources that carry sc6 slots | 44 | 23 | 13 |
| … whose slot targets the root callee | 44 | 23 | 13 |
| … with a slot to any other target | 0 | 0 | 0 |

Source: §6 item 1.

- In the region, that exit is the `call` edge: a patched `jmp rel32` to the callee member's
  +15 (`scopt/link.py:1826-1851`). That already beats a B3 guard.
- So stage 3's "entry members first" would copy guards whose only target is already internal.
- The waste is in chain-static's budget, not in the lack of recursion. 44 of chain-static's
  256 packet units are these entry sources (DESIGN §7, reproduced in §6 item 2), while 16
  plans were refused by the same cap.

**The same holds, less extremely, for all members.** QEMU skips each B3 slot of a member copy
(`skipped_b3_slots` in the sidecar's `x_b3.firmware_region_links`). Classified against the
member's own internal edges (§6 item 3):

| o4r3c skipped B3 slots in member copies | packet | uart | cli |
|---|---|---|---|
| total (= sidecar `skipped_b3_slots`) | 799 | 178 | 153 |
| on entry members; target reached internally | 44 | 23 | 13 |
| on other members; target reached internally | 557 | 94 | 101 |
| on other members; target left residual | 198 | 53 | 37 |
| SPEC slots | 0 | 8 | 2 |

Source: §6 item 3.

So 75% / 66% / 75% of what member copies "lose" from B3 is already replaced by a cheaper
internal edge. This matches map-b3 §4.3's training-traffic view: 74% / 88% of uart / cli
lookup traffic between member sources.

**It also biases pricing.** `region_exits_chainable` treats a region as unchainable when any
member source is grown (`scopt/link.py:359-380`; `scopt/analysis/firmware_profit.py:218-239`).

- In 10 of the 53 admitted packet regions, the entry source is the only grown member source.
  Its only slot is the internalized root call.
- Counting every member, 13 / 1 / 0 admitted regions have no B3 slot that guards a residual
  target. For them the unchainable price has no basis at all.
- Of the 16 refused packet roots that pass admission when re-priced as chained (DESIGN §4.2),
  13 have a grown entry source, and in each its only slot targets the root callee. The
  receipts do not list the members of refused roots, so whether their other members are grown
  is not known.

Source: §6 items 4 and 5.

### 2.4 MISCOUNTED: unverified nested calls and `blx reg` (DESIGN rows 10 and 12, §3.2 2d gate, §3.4 4b; map-regions §1 and §6 item 1)

`unverified_nested_call_exit` is emitted for every exit of a BL/BLX instruction that has no
discovered call row (`scopt/analysis/firmware_regions.py:100-108`). That includes the
`exit_requested` exit, whenever QEMU binds it to the same instruction
(`scopt/formats/region_v1.py:163-170`).

Split by member site and by static target (§6 item 6):

| o4r3c `unverified_nested_call_exit` | packet | uart | cli |
|---|---|---|---|
| rows | 400 | 116 | 78 |
| member call sites (two rows each: the call exit plus `exit_requested`) | 200 | 58 | 39 |
| direct BL, exit `goto_tb` | 33 | 26 | 24 |
| direct BL, exit `goto_ptr` (an off-page call) | 162 | 30 | 13 |
| indirect (`blx reg`, no static target) | 5 | 2 | 2 |

Source: §6 item 6.

- **Row 12 / stage 4b** (promote `blx reg`) reaches 5 / 2 / 2 sites, not "167 / 32 / 15
  goto_ptr rows". It should drop to the end of the plan or out of it.
- **Row 10 / stage 2d** (discover single-instruction BLs from training evidence) targets the
  195 / 56 / 37 direct sites. Its gate should count sites, not rows.
- **The mechanism is as DESIGN says.** `selected_firmware_units` drops single-instruction
  units without training or instruction evidence (`scopt/analysis/firmware_calls.py:51-64`).
  But discovery also feeds root selection: exhaustive roots are every discovered call. So 2d
  adds new roots as well as deeper nesting, and must be priced and timed as a selection change.

### 2.5 INCOMPLETE: "WHOPR planner: only changes the order" (DESIGN row 18)

Admission is greedy and covering-first, with `captured` flow
(`scopt/passes/firmware_inline.py:412-417`, `:489-491`). So the order is an admission input.

- scopt's own decision text says the partition order "changes admission (legacy: packet +2
  roots, cli 1 swapped)" (`scopt/o4.py:195-198`).
- packet-VERIFY §5 problem 2 records that o4_legacy has 48 regions
  ([`o4-campaign/verify/packet-VERIFY.md`](o4-campaign/verify/packet-VERIFY.md)).

This matters for any stage that changes plans or prices: a root's result depends on the
roots priced before it.

## 3. Stages that are infeasible as written or underestimated

### 3.1 Stage 2a: the context-keyed nested gate is not a small change

DESIGN wants decisions "keyed by (callsite, context)" with "bottom-up memoized subtree gains",
rated small.

- **The profile has no per-context counts.** The profit index holds per-unit exit counts and
  executions (`scopt/analysis/firmware_profit.py:137-180`).
- **The existing gate prices each nested call as a standalone root.** The probe region is
  built with `expand_nested=False` (`:632`) and priced with `self.model.estimate(probe,
  self.snapshot)`, with no context, `captured` or trusted-entry input (`:639`).
- **Context only appears inside one region's Markov flow** (`member_flow`), and that flow
  exists only after the enclosing plan is built.
- **The gate runs during the build**, as the `expansion` callback of
  `build_firmware_region` (`firmware_regions.py:126-133`). So a decision keyed by context
  would get the same global numbers for every context.

**What the plan needs instead.**

- **A real context-sensitive gate is a second phase.** Build the ungated recursive plan,
  solve its flow, prune callee subtrees whose `member_net` sum is negative, then re-price.
  That is a new pruning algorithm: medium effort.
- **The small, safe part of 2a** is the best-of-three choice (gated recursive with today's
  context-free gate, ungated recursive, flat). `rank()` already exists
  (`firmware_inline.py:444-451`). That is what recovers the 0.88 ms model loss of
  map-regions §3.2.
- **The proposed test is not sound as stated.** It requires, for every o4r3c-admitted root,
  a chosen net at least o4r3c's net. Changing one root's plan changes `captured` for later
  roots and can lower their entries (§2.5). The monotonicity test should apply to the total
  `profit_net_ns`, or per root with `captured` held fixed.

### 3.2 Stage 2b: aim it at the exact cause

The proposed per-member B3-aware pricing has three parts: (a) residual `goto_tb` chained per
armed slot, (b) internal-lookup credit at guard cost, (c) residual `goto_ptr` at lookup minus
guard. A simpler and exact rule follows from §2.3, and it needs only the region plan and the
B3 state:

- A grown member source loses nothing in the pre-B3 copy when every B3 slot it registered
  targets a member that the same member reaches by an internal edge.
- Only slots whose target stays residual make the copy worse than the public code.
- Pricing should charge exactly those. Today it charges every residual `goto_tb` of the
  whole region a full dispatch.

That removes the artifact for 13 / 1 / 0 admitted regions, and probably for some refused
roots (§2.3). It should be done before A1-style blanket chaining is judged.

### 3.3 Stage 3b: the composition rule is wrong for internal edges, and its value is about a quarter of what was assumed

**How internal edges are made today** (trusted regions):

- A non-return internal `goto_ptr` edge is made by overwriting the lookup tail with an
  unconditional `jmp rel32` (`scopt/link.py:1838-1846`).
- A return edge gets a private guard stub on a free slot (`:1660-1716`).

**Both need the original lookup tail.** In a post-B3 copy of a grown source, that tail is
already `jmp S` plus `int3` padding. So:

- `lookup_tails` finds nothing, and the link raises `firmware_region_exit_shape` (`:1674-1677`,
  `:1846`);
- or `firmware_region_slot_representation`, when both slots are taken (`:1681-1685`).

These are `LinkError`s that fail the whole link (`:2001-2005`). The "per-member fallback, so
the link never fails" that DESIGN describes does not exist yet. It is new code.

**DESIGN's rule for the collision is "patch that B3 slot's rel32 to the target member".**
That routes an internal edge through B3's guard compares (the `guard-v1` template,
`scopt/guard.py:1-20`), where today it is a plain jump. That makes the most common case slower:
601 / 117 / 114 of the skipped slots guard internal targets (§2.3).

**The rule should be the reverse: the internal edge wins.**
- Rewrite the B3 tail jump of that exit to the target member.
- Keep only the B3 slots whose targets stay residual.
- Mark them so that `sc7_region_internal_slot` and the skip mask agree.

**Value.** Stage 3 can pay only on the 198 / 53 / 37 residual-target slot copies (§2.3), and
on none of the entry members. The perf attribution of stage 1 (item b, lookups from member
code) should be read against that ceiling before any QEMU work.

**The mapping-change hazard is not new.** DESIGN treats it as new: B3 slots in members are
not unlinked on TLB/MPU changes. But member residual slots armed under Q6 are already chained
and never unlinked, because trusted mode ignores mapping changes (`supercache.c:3488-3492`).
The MPU functional test is still right. It tests an existing property of trusted regions.

### 3.4 Stage 4a: the poll argument holds only for `goto_tb` back-edges

DESIGN says edges back into member 0 stay residual and "QEMU self-chains them at +0 with a
poll", and that an internal back-edge at +15 "would make a poll-free task loop: forbidden".
Three facts qualify this.

- **A `goto_ptr` back-edge does not poll.** It goes through `helper_lookup_tb_ptr`, which
  returns `tb->tc.ptr` (`cpu-exec.c:405-420`). For a region entry that is +15, past the poll:
  "dispatcher entries, incoming public chains, goto_ptr and internal edges all start at +15"
  (`supercache.c:1533-1537`). So a residual `goto_ptr` back-edge closes a poll-free loop.
  - o4r3c already has residual exits whose static target is another region's entry: 18 / 2 /
    0 `goto_ptr` and 5 / 0 / 1 `goto_tb` (§6 item 7).
  - Whether any of the `goto_ptr` ones closes a cycle was not checked.
- **The trusted contract already allows poll-free cycles.** The documented contract names
  "two poll-free paths … cycles of internal edges within one region, and a `goto_ptr` lookup
  that enters a region entry at +15" (`docs/sealed-firmware.md:323-326`).
  - 25 / 14 / 13 o4r3c regions contain an internal cycle (§6 item 8).
  - So "forbidden" is a fidelity policy for the outer task loop, not a loader or contract
    constraint. The loader accepts an internal edge into member 0: any target member in the
    region is valid (`supercache.c:1416-1432`).
- **The 4a test is not enough.** "No internal edge targets member 0" does not make every
  iteration poll. The test must also cover residual `goto_ptr` exits whose target is the
  region's own entry PC.

### 3.5 Stage 1: the chain-static repeat works as designed

This is a positive check. The second `chain-static` entry:

- reads the B3 state, skips slots already claimed (`chain_returns.py:215-219`) and targets
  already guarded (`:220-222`);
- counts its 256-unit cap per run (`touched`, `:183`, `:308-310`), as the manager does per
  proposal (`scopt/manager.py:1128`).

So it should add exactly the 16 packet plans the first entry refused. As map-b3 §5.2 warns,
`--params`, `--enable` and `--disable` resolve by name (`scopt/manager.py:300-310`), so they
would apply to both entries.

## 4. Optimizations missing from "all"

| # | Missing item | Why it belongs | Evidence | Effort / QEMU |
|---|---|---|---|---|
| M1 | **Region-aware B3.** Do not give B3 slots to exits that a region makes internal, or run the chain passes again on the regions' residual exits after firmware-inline. | It frees B3 slots and fixes the pricing bias. It is B3 applied to the region's output, which is the recursion the user asked for. | 44 of 256 packet chain-static units are entry sources whose only slot is the root call, while the cap refused 16 plans. 601 / 117 / 114 skipped member slots guard internal targets (§2.3). | Medium: pass order. firmware-inline needs the grown set to price, so it is a two-round plan. No QEMU change. |
| M2 | **C3 `hs-msr-exit-chain` and C6 `inline-lookup-probe`**, designed in `docs/design/helper-inlining-design.md` §1.3 and §4.8. DESIGN row 42 keeps only C1/C2. | MSR and CPS TBs end in `exit_tb(0)`, which ends a region (`observation_boundary`). C6 would cover the residual lookup tails that member copies keep. | C3 ESTIMATE 1.5-4.3% (`helper-inlining-design.md:105-108`). o4r3c has 192 / 10 / 0 `exit_null` observation-boundary rows (§6 item 9). C6 targets about 4-5 M unguarded lookups per run (`:109`). | Large. C3 needs the signed spec review the design requires (`:108`). C6 needs the B3-7 owner's agreement (`:109`). |
| M3 | **Per-context B3 targets in member copies.** | A member copy inherits the public unit's targets, which come from the aggregate profile. | Listed as map-b3 §7 L1 item 5; no row in DESIGN. | Small once L1 exists. |
| M4 | **Register promotion across member edges**, the FirmBOLT frame-slot-promotion analogue: carry guest registers in host registers along internal edges. | DESIGN's state rows only NOP stores (region-state-dse and -forward, eliminate-state-sync). None forwards a value across a member boundary. | FirmBOLT promotion: executed stack accesses −32 to −41% in two demos (`map-firmbolt.md` §2.5). No scopt measurement. | Large: a new member-scope transform. No QEMU change. Trusted members have no restore data (map-regions §6 item 23). |
| M5 | **SPEC on spliced units, and cleanup through splices.** | Splices are barriers today (`specialize_returns.py:135-140`; `analysis/native_region.py:37-43`). | map-hostinline §7 item 6; near-inert today (1 / 0 / 0 sites). | Small to medium. |

## 5. What the corrections change in the plan

1. **Stage 1:** unchanged (`o4rec1`, `o4rec1a`, `o4rec1x`, perf attribution). Read A1's
   result knowing that part of the "unchainable" price is the artifact of §2.3.
2. **Stage 2:**
   - 2a splits into the small best-of-three (keep) and a medium context-sensitive pruning
     (later).
   - 2b uses the exact slot-target rule of §3.2.
   - M1 (region-aware B3) joins as 2j.
   - 2d's gate counts sites.
3. **Stage 3:** drop "entry members first". Use "internal edge wins". Size it to the 198 / 53
   / 37 residual-target slot copies and gate the QEMU flag on stage-1 perf.
4. **Stage 4:** 4a must handle `goto_ptr` back-edges. 4b moves to the end (5 / 2 / 2 sites).
5. **Stage 5:** add C3 and C6 as gated stretch items next to C1/C2.
6. **U7:** stays conditional (stage 6). Its stated motivation (regions bypassed) is void.

## 6. How the new numbers were read

All reads were with `PYTHONDONTWRITEBYTECODE=1 python3 -B`, read-only, over the o4r3c files
named at the top. `<art>` is `/data/scfix-o4/<w>-w/opt/o4r3c/store/artifacts/<id>/`. scopt
was imported from `/data/fastdyn-work/fd-int/tools/scopt` at `56c4c80`.

1. **Entry-source slots.**
   - Read the sc6 slots of `<art>/cache.sc` (`read_sc6(...).ext.slots`: `src_tb_offset`,
     `target_vaddr`, `kind`) and the v7 directory (`decode_directory(read_sc7(...))`).
   - Pair each directory region with its sidecar plan
     (`x_b3_manager.firmware_regions[]`) by `callsite_id8` = the first 16 hex digits of the
     plan's `callsite_id`.
   - For each region, compare the slots whose `src_tb_offset` is the entry member's
     `source_tb_offset` with the plan's `callsite.target_vaddr`.
2. **chain-static units that are entry or member sources.** Intersect `optimize.json`
   `passes[chain-static].affected` with the sidecar's entry and member `source_uid`s: 256 / 44
   / 85, 50 / 23 / 47, 43 / 13 / 41 (as DESIGN §7).
3. **Skipped slots, internal or residual.**
   - Walk every member of every region in directory order (the linker keeps
     `plan.members` order, `link.py:1755`).
   - For each sc6 slot on the member's `source_tb_offset`, call it "internal" when some
     plan edge of that member with a non-null `target_member` has `guest_target` equal to the
     slot's `target_vaddr`.
   - The totals equal the sidecar's summed `x_b3.firmware_region_links[].skipped_b3_slots`
     (799 / 178 / 153).
   - Every region has `member_descriptors: true` and no refusal there: 53 / 27 / 14.
4. **Regions by grown members.**
   - Grown units are `x_b3.grown` (294 / 88 / 65).
   - Packet: 2 regions have no grown member source, 10 have only the entry source, 41 have a
     grown non-entry member.
   - Regions whose every slot is internal per item 3: 13 / 1 / 0.
5. **A1 re-pricing.**
   - DESIGN §7's formula over `optimize.json` `residual` rows with reason `not_profitable`
     reproduces 16 / 7 / 0 roots and +81.0 / +5.7 / 0 ms. The largest is 70.5 ms at 2,317,210
     calls.
   - 13 / 3 / 0 of those roots have an entry `source_uid` in `x_b3.grown`.
   - For each, find the record ending at the BL with the callsite's `source_key`. Its sc6
     slots all target the root callee.
6. **Unverified nested calls.**
   - `residual` rows with reason `unverified_nested_call_exit`, counted by `exit_kind`, by
     `guest_target is None`, and by `(callsite_id, source_member)`.
   - Packet: 200 sites; exit kinds `exit_requested` 200, `goto_ptr` 167, `goto_tb` 33; 5 + 5
     rows without a static target.
7. **Residual exits into region entries.**
   - `residual` rows of admitted roots with a non-null `guest_target` (low bit cleared)
     equal to a directory entry member's `guest_vaddr`, excluding `exit_requested`.
   - None targets its own region. Packet: 12 `unverified_nested_call_exit` `goto_ptr`,
     6 `continuation_exit` `goto_ptr`, 5 `continuation_exit` `goto_tb`. uart: 2
     `continuation_exit` `goto_ptr`. cli: 1 `continuation_exit` `goto_tb`.
8. **Internal cycles.** A depth-first search over each sidecar plan's edges with a non-null
   `target_member`. None of the edges targets member 0.
9. **Observation boundaries.** `residual` rows with reason `observation_boundary` by
   `exit_kind`: packet `exit_requested` 6,006, `exit_null` 192, `goto_ptr` 1; uart 984 / 10 / 5;
   cli 1,305 / 0 / 2.
