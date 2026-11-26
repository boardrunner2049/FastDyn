# No-QEMU routes: B3, SPEC and host-inline inside v7 region members

This step only read code and artifacts. Nothing was built, timed, or edited in either repository.

**Sources and path conventions**

- **scopt:** `/data/fastdyn-work/fd-int/tools/scopt` at 56c4c80. Paths such as `scopt/link.py` are relative to it. DESIGN.md cites b35cdb6, so a few of its line numbers differ from the ones here.
- **QEMU:** `/data/fastdyn-work/qemu-fu` at aacca05.
  - `supercache.c` is `tcg/supercache.c`.
  - `cpu-exec.c` is `accel/tcg/cpu-exec.c`.
  - `region.c` is `tcg/region.c`.
- **Artifacts:** the o4r3c composed caches `/data/scfix-o4/<w>-w/opt/o4r3c/store/artifacts/*/cache.sc` and their sidecars `cache.sc.scopt.json`, the snapshot `units.jsonl`, and the training profile `/data/scfix-o4/<w>-w/in/merged.profile.json`. That profile is one run (`collection.runs` has 1 entry).
- **Triples** are packet / uart / cli.

## 0. Verdict

**No part of this needs a QEMU change.** The `SC7_MEMBER_B3_COPY` flag in DESIGN.md §3.3 (3b) is a convenience, not a requirement. Every useful effect of B3, SPEC, B4 and host-inline inside members can be reached by scopt writing a different supercache. Three facts support this.

1. **Almost all of the B3 that members "lose" is already replaced by something better.** I classified every B3 (sc6) slot that a member copy skips, from the o4r3c cache files (method in §2):
   - 770 of 799 on packet, 162 of 178 on uart and 147 of 153 on cli (91 to 96%) sit on a `goto_ptr` exit that the region has already made an **internal edge**. That edge is a guarded private return or a direct `jmp` to the target member's +15. It beats B3's guard plus a chained jump to +0.
   - This holds for **every** B3 slot on a region-entry member (44 / 23 / 13, one per member). Each one guards the call exit, and the region turns that exit into an internal call edge.
   - So DESIGN.md §1.4(ii) needs a correction: the B3 work on entry sources is dead because the region superseded it, not because it was lost. Its only real cost is the chain-static budget: 44 of 256 units on packet (§3e).
2. **The rest is small, and all of it is on root-continuation members.**
   - Residual B3 slots: 29 / 16 / 6 slot copies, every one in a `continuation` member. The root continuation is never expanded (`scopt/analysis/firmware_regions.py:66-70`).
   - Their targets: 20 / 13 / 6 ordinary public TBs and 9 / 3 / 0 entries of *other* regions.
   - None targets a member of the same region, and none targets the region's own entry.
   - Upper bound on the training lookups they carry: 0.66M / 0.23M / 0.007M. At about 6 ns each that is at most 4.0 / 1.4 / 0.04 ms per training run. For comparison, o4r3c's region profit net is 21.21 / 11.31 / 1.60 ms, and chain-static's model saving on packet is 63.79 ms.
3. **scopt alone can capture all of that residual.** Two routes do it with QEMU unchanged:
   - **(a+) Region formation consumes B3's targets.** Root-continuation expansion (DESIGN.md 4a) covers static exits; a new guarded return expansion covers return exits. Members stay pre-B3 copies with today's skip flags, and the new edges are ordinary internal edges that QEMU already verifies. Cost per event is better than B3.
   - **(f) A twin source record.** A post-B3 continuation copy names, as its "source record", a lookup-invisible copy of its source's record that has no sc6 slots. The loader then requires flags 0 and arms B3's own guard slots as ordinary residual slots. This is exactly the runtime behaviour the QEMU flag would give.
   - SPEC: same-length variant patches on member copies, scopt-only.
   - Host-inline: already present in every member copy.
   - B4 / cleanup rewrites: scopt-only for every member, grown sources included (§3h).

**Routes that do not work.**

- **(c) `member_slot_skip = off`.**
  - It does not produce "want_flags 0 with QEMU arming": with `off`, regions fall back to the alias layout, which arms nothing.
  - It is a net loss.
- **(d) Static rel32 jumps to public TBs or to other regions.**
  - QEMU does not see them at load time.
  - At run time they break: public and other-region descriptors are built lazily, so a jump that arrives first lands on a zeroed descriptor and either crashes or restores PC 0.
  - They are safe only inside the same region.
- **(e) Pointing a region entry at the public post-B3 TB.**
  - The loader rejects it.
  - It would buy nothing anyway, because those B3 slots are already superseded (fact 1).

## 1. Every rule a v7 region member must satisfy (aacca05)

**"Die"** means `sc7_die`/`sc6_die`: QEMU refuses to load the file (exit). **"Fail-closed"** means the file still loads, but QEMU refuses one optimization (for example, a region does not become a T->T destination).

### 1.1 Load-time structure

| # | Rule | Where | Failure |
|---|---|---|---|
| L1 | Directory header: magic `QSC7FWRG`; ext_version 2 (composite) or 3 (trusted); header and entry sizes; reserved = 0 | `supercache.c:1196-1204` | die |
| L2 | A composed file (sc6 + regions) needs a trusted directory and a loaded sc6 extension | `:1191-1193`, `:1205-1208` | die |
| L3 | Table bounds: `table_offset % 8`, size = counts, `max_end` = table end. Checksum is checked unless `user_trusted` | `:1209-1228` | die |
| L4 | Region: flags = COMPOSITE\|TRUSTED, optionally \|MEMBER_DESCRIPTORS (trusted only); `entry_member == 0`; members and edges partitioned in order; `start_offset % 64`; the source maps a record; `end <= table_offset` | `:1243-1261` | die |
| L5 | Arena: records, directory table, sc6 variants, member descriptors, member code and search data are pairwise disjoint | `:1269-1348` | die |
| L6 | Member: its source record exists; region index valid; flags limited to bits 0-1; the member lies inside its region | `:1296-1310` | die |
| **L7** | **Member flags must equal `want_flags`. `want_flags` = the sc6 slot mask of the member's source record when composed && trusted && region has MEMBER_DESCRIPTORS, else 0.** The mask comes from `sc6.slots[k].src`, keyed by record | `:1288-1293`, `:1311-1318` | die |
| L8 | A trusted member's source record must not have icount; descriptor, code and search bounds | `:1323-1336` | die |
| L9 | Entry member's source == region source. Descriptors contiguous, one per member, with MEMBER_DESCRIPTORS (else all alias one). Code contiguous, each member 16-aligned with MEMBER_DESCRIPTORS. Trusted search size 0 | `:1350-1407` | die |
| L10 | Edge: kind 1; flags = `SC7_EDGE_TRUSTED_SKIP_POLL` (trusted); reserved 0; source and target in the region; field inside the source copy | `:1408-1436` | die |
| L11 | Source index: only the **entry** member's source record is indexed; the first region per source wins | `:1436-1456` | silent |

### 1.2 After the code is read in (`sc7_verify_hashes`, `:1605-1650`)

| # | Rule | Where | Failure |
|---|---|---|---|
| H1 | Every trusted member starts with the 11-byte poll `8b 5d f8 85 db 0f 8c`, then `c6 45 fc`, and byte 14 is 0 or 1 | `:1614-1627` | die |
| **H2** | **Residual-slot shape**, only in MEMBER_DESCRIPTORS regions. Slot offsets come from the **member's source record** (`s->jmp_insn_offset`). Every record slot that is neither skipped by a flag nor an internal-edge field must be `e9 00000000`, reset = field + 4, field > 15, inside the copy, and 4-aligned | `:1495-1531` (offsets `:1509-1512`, exemptions `:1514-1518`) | die |
| H3 | Entry poll proof, per region: member 0's `jl` reaches its own stub `lea/movabs rax = entry descriptor \| TB_EXIT_REQUESTED; jmp tb_ret`, and its can_do_io byte is 1 | `:1533-1604` | fail-closed: never a T->T destination (`:4432-4437`) |
| H4 | Every declared edge's rel32 lands exactly on target member + 15 (trusted) | `:1630-1645` | die |
| **H5** | **Not checked for trusted caches:** member code and search hashes, the artifact identity, and any byte that is not a prefix, an unskipped residual slot or a declared edge field | early return `:1648-1650` (the hash loop `:1652-1690` is non-trusted only); skipped and internal slots `:1514-1518`, `:3237-3244` | – |

### 1.3 Runtime

| # | Rule | Where |
|---|---|---|
| X1 | **Region entry.** `lookup_entry` calls `sc7_try_region` before the public TB is activated, so the public TB of an entry source is never initialized while regions are loaded. The region is declined only under profiling, single-step, breakpoints, watchpoints, the `no-regions` token or icount | `supercache.c:3962-3969` vs. activation `:3972-4091`; declines `:3348-3357`, `:3381-3383` |
| X2 | **Descriptor build.** Entry `tc.ptr` = +15; shadow members `tc.ptr` = +0. A slot is armed only if valid in the source record, not flag-skipped and not internal. Armed rel32s are reset at build. Only the entry goes into the host-PC tree; shadows go only into `trusted_tbs` | `:3208-3251`, `:3265-3300`, `:3303-3316`, `:3320-3325` |
| X3 | **Exits.** `last_tb` is kept only for an armed slot. A trusted source chains to an ordinary TB at `tc.ptr` (+0, so the poll runs), or to a trusted entry at +0 only when sealed and H3 passed. Nothing chains into a two-page TB. Public→trusted chains need sealed. No trusted chains under `QEMU_SC_SEALED_SKIP_IRQ` or `no-trusted-exit-chain` | `:4321-4324`, `:4375-4381`, `:4411-4443`, `:4462-4496`, `:4498-4503`, `:3275-3276`; `cpu-exec.c:1137-1139`, `:1144-1155`, `:1178-1180` |
| X4 | **Poll exits.** `TB_EXIT_REQUESTED` from a trusted descriptor must come from a region entry (asserted with V7 stats). The PC is restored from that descriptor. Non-entry members are only ever entered at +15 | `supercache.c:4541-4556`; `cpu-exec.c:475`, `:480-490` |
| X5 | **No recovery inside trusted code.** A host-PC lookup or a search-data request inside a region is fatal | `supercache.c:3546-3560`, `:4219-4228` |
| X6 | **Lifetime.** `tb_flush` forgets trusted descriptors, and a region runs again only after a lookup rebuilds them and resets its armed slots. Supercache mode keeps code across flushes. Trusted mode ignores mapping changes and code writes | `:3509-3540`, `:3261-3263`; `region.c:415-423`; `supercache.c:3488-3492`, `:3617-3618` |
| X7 | **Public descriptors are lazy.** A public TB's descriptor is initialized only by `lookup_entry`. scopt writes only `[tc_offset, end_offset)`, so in the file the descriptor is a zero hole | `supercache.c:3994-4091`; `scopt/formats/sc_v5.py:614-619` |
| X8 | **Public sc6 (composed).** Each slot needs its record to declare that jump, inside the guard and 4-aligned, with no duplicates. SPEC slots must be linked. Guard hashes are checked unless `user_trusted`. Arrays end ≤ 0xFF00. GENERAL slots are unlinked on TLB/MPU changes. SPEC arms straight to the variant | `:2236-2278`, `:2326-2329`, `:2344-2369`, `:2101-2103`, `:2592-2602`, `:2657-2677`, `:2485-2521` |
| X9 | **Records and lookup.** A record must be committed, pass its checksum unless `user_trusted`, and have sane offsets. Sealed lookups walk the flat index head first, never fall back to the bucket hash, and a miss is fatal. `exact_pc` strips `CF_PCREL` from every lookup, and the flat index skips `CF_PCREL` records | `:480-497`, `:530-550`, `:4173-4190`, `:382`, `:399-402`, `:521-523`, `:544`, `:562`; `cpu-exec.c:983`, `:1097-1100`, `:399` |
| X10 | **Fixed arena.** The code buffer is mapped at the recorded `code_address` (`MAP_FIXED_NOREPLACE`), and code is read to `buffer + offset`. So every host address is known at optimize time. A rel32 between two arena offsets would not even need that | `:414-436`, `:2913-2919`, `:2982-2988` |

**What follows for B3 in members:**

- Rule L7 pins a member's flags to its *source record's* sc6 slot set.
- Rules H2 and X2 arm only slots that this *same record* declares and the flags do not skip.
- B3's guard slots exist only as sc6 slots of R's public record.
- So a member that names R as its source can never have a B3 slot armed by QEMU. That is the whole constraint, and §3f gets around it.

## 2. Where B3 is actually lost in members (o4r3c)

**Method.** Read-only parsing of each cache and its metadata:

- each cache's sc6 slot table (0x140), v7 directory (0xFF00) and record array;
- joined to the sidecar region plans: directory order = plan order, checked by `region_id8`;
- every member with flags ≠ 0 was checked to have flags = its source's sc6 mask;
- `units.jsonl` exits; at most one `goto_ptr` per unit (31,264 units, 1 with two).

For each skipped slot:

- **moot:** the unit's `goto_ptr` exit has an internal edge in that member;
- otherwise the class is decided by the guarded target PC.

Weights are steady-phase `lookup_hit/miss` counts R→T over **all** executions of R. That makes them an upper bound, since public R and other copies are included.

The script is at `/tmp/claude-7133428/-data-FirmBOLT-firmbolt/9f225cf6-3504-4878-92ea-815fbcb748e4/scratchpad/b3_in_members.py`. It is in the session scratchpad, so it is temporary.

| | packet | uart | cli |
|---|---|---|---|
| B3 (sc6) slots in the cache / member copies with skip flags | 303 / 707 | 102 / 143 | 72 / 128 |
| skipped slot copies (= sidecar `skipped_b3_slots`) | 799 | 178 | 153 |
| **moot**: the exit is already an internal edge | **770** | **162** | **147** |
| … of which on entry (caller) members | 44 (all 44) | 23 (all) | 13 (all) |
| … of which in nested-context members | 726 | 139 | 134 |
| residual, target a same-region member (any context) | 0 | 0 | 0 |
| residual, target the region's own entry | 0 | 0 | 0 |
| residual, target another region's entry | 9 | 3 | 0 |
| residual, target an ordinary public TB | 20 | 13 | 6 |
| role of every residual-slot member | `continuation` | `continuation` | `continuation` |
| residual by exit class: return (xc1) / static `goto_ptr` (xc2) | 10 / 19 | 7 / 9 | 0 / 6 |
| SPEC slots among the skipped (moot / residual) | 0 | 6 / 2 | 2 / 0 |
| steady lookups on all B3 slots (public R) | 14.03M | 2.11M | 0.175M |
| … on member-source (R, slot) pairs whose copies are mostly moot | 3.69M | 1.52M | 0.160M |
| … on (R, slot) pairs residual in some copy, upper bound | **0.661M** (xc2 0.449M, xc1 0.212M) | **0.227M** (0.150M / 0.077M) | **0.0068M** (all xc2) |

Pricing check: `firmware_profit.py:101-103` sets lookup 7.0 ns and guard 1.0 ns. So enabling B3 in those copies is worth at most about 6 ns × 0.661M / 0.227M / 0.0068M, which is **≤ 4.0 / 1.4 / 0.04 ms per training run** (upper bound).

This also narrows DESIGN.md 2b(c). The "residual `goto_ptr` on a B3-guarded tail" penalty (`firmware_profit.py:40-46`) only ever applies to root-continuation members.

## 3. Routes

**Legend**

- **Accepted?** Whether the aacca05 loader and runtime accept it. Rule IDs refer to §1.
- **Captures.** Measured against §2.
- **Effort.** none / small / medium / large.

### (a) Resolve B3 targets to existing members as internal edges

- **Accepted?** Yes. Edges are checked only by L10 and H4: rel32 = target + 15.
  - A slot that is both flag-skipped and an edge field is valid. Today's moot return edges on grown sources do exactly that: a private guard on a free slot of the pre-B3 copy (`scopt/link.py:1658-1716`), plus a skip mask for B3's slot.
- **Captures: 0 of the residual.**
  - No residual B3 target is a member of the same region (§2).
  - The moot 91-96% are already internal edges. There they are better than B3: a direct `jmp` to +15 for static exits (`link.py:1836-1843`), or the same guard template for returns (`link.py:1690-1704`), with no lookup and no poll.
- **Changes:** none.
- **Risk:** none.

### (a+) Let region formation consume B3's targets (recommended)

- **What.** Make the residual exits internal by adding members.
  - **Static exits (xc2):** root-continuation expansion (DESIGN.md 4a). Enqueue the root continuation in context `()` (`firmware_regions.py:66-70`). Its static `goto_ptr` exits then become internal branch edges through `compatible_units` (`:170-178`). The compatibility index already resolves the sealed fallback duplicates to the newest record (`analysis/firmware_calls.py:70-77`), the same record the sealed flat lookup returns first (`supercache.c:544-548`, `:4173-4180`).
  - **Return exits (xc1):** guarded return expansion. Add the profiled return target (the B3 slot targets, or the top `lookup_hit` destinations) as a new member in a fresh context. Connect it with a private guard on a free slot of the **pre-B3** continuation copy, exactly as internal returns are emitted today (`link.py:1690-1711`, `register=False`).
- **Accepted?** Yes. It only adds members and edges (L4-L10, H1-H4). Members stay pre-B3 copies, with today's flags (L7) and the `member_slot_skip` allowlist (`scopt/validate.py:3526-3530`).
- **Captures:** up to **100% of the residual**.
  - xc2 (static): 19 / 9 / 6 copies, ≤ 0.449M / 0.150M / 0.0068M lookups. Each becomes a direct internal edge, saving lookup + poll, which is better than B3.
  - xc1 (return): 10 / 7 / 0 copies, ≤ 0.212M / 0.077M / 0. Each becomes a guarded internal edge, the same as B3's guard but landing on +15 with no lookup.
- **scopt changes:**
  - `scopt/analysis/firmware_regions.py`: root continuation `:62-70`; a new return-expansion rule beside `:153-160`.
  - `scopt/passes/firmware_inline.py`: plans and receipts.
  - `scopt/analysis/firmware_profit.py`: price the new edges in `estimate_region` (`:345`, residual terms `:455-495`).
  - `scopt/link.py`: allow a guarded internal edge on the root continuation's return `:1658-1704`.
  - Tests: the region-builder unit tests; an H4 offline check.
- **Risks:**
  - **New poll-free cycles.** The trusted ABI allows them (`docs/firmware-inline-policies.md:84`) but does not detect them. Keep every edge into member 0 residual (DESIGN.md 4a test).
  - **Duplication.** The 9 / 3 / 0 targets that are other regions' entries pull a copy of that caller into this region. Price them, or leave them residual.
  - **Growth and arena:** `firmware_region_arena_exhausted`, `link.py:2009-2018`.
- **Effort:** medium. 4a is already planned; return expansion is new.

### (b) Use existing residual-exit, member-skip or T->T chaining instead of B3 slots

- **Accepted?** Yes, but it is empty.
  - QEMU arms a member slot only if the member's source record declares it and the flags do not skip it (H2, X2: `supercache.c:3229-3247`).
  - On a grown R, the only extra slots are B3's, and L7 forces them skipped.
  - B3's targets sit on `goto_ptr` exits, which have no TCG slot. Member-skip is exactly the mechanism that disables them.
- **Captures: 0%** with R's record.
  - Residual `goto_tb` exits of the same members are already chained (Q6) and T->T (Q7, `:4462-4496`).
  - For B3's `goto_ptr` exits, (b) only works through a different record: that is (f).
- **Changes:** none.

### (c) `member_slot_skip = off` with post-B3 copies

- **Accepted?** Yes, but not in the form proposed.
  - With `off`, the linker refuses member descriptors for any region with a grown member (`link.py:1748-1751`) and falls back to the alias layout (`:1767-1770`).
  - For alias regions, `want_flags` is 0 (`supercache.c:1311-1313`). But QEMU then neither verifies residual slots (`:1502-1504`) nor arms any (`:3275-3276`), and every exit drops `last_tb` (`:4321-4324`).
  - `want_flags` is 0 **with** member descriptors only when the file is not composed, that is, without B3 at all.
- **Captures: negative.**
  - A B3 guard hit in an alias region falls through its unarmed slot to `lea; jmp tb_ret` (`scopt/guard.py:375-383`), a full dispatcher iteration (about 40 ns against about 7 ns for a lookup, `firmware_profit.py:101-104`).
  - Every residual `goto_tb` loses Q6 chaining: the 2.9M dispatches per packet run that member descriptors removed (`docs/sealed-firmware.md`, "Unchainable residual exits").
- **Do not use.**

### (d) Direct rel32 jumps to known host addresses (fixed arena)

**Why QEMU does not notice them:**

- Trusted member bodies are not hashed (H5: `supercache.c:1648-1650`).
- A flag-skipped slot is never inspected or armed (`:1514-1518`, `:3237-3239`), and the loader *requires* those flags (L7).
- Host addresses are fixed (X10).

**Why they are still unsafe, by target:**

| Target | Safe? | Rule |
|---|---|---|
| Ordinary public TB, +0 | **No** | X7: until some lookup activated it, its descriptor is zero. A poll exit restores PC 0 (`cpu-exec.c:480-490`). A slot exit looks armed (`jmp_reset_offset` 0 ≠ `0xffff`, `supercache.c:4321-4324`), and `tb_add_jump` writes through `tc.ptr = NULL` (`cpu-exec.c:666-673`). After `tb_flush` it also bypasses re-activation (X6). It bypasses the two-page refusal (`cpu-exec.c:1137-1139`) and sc6's TLB unlink (X8) |
| Another region's entry, +0 | **No** | Its descriptors are built only by `sc7_build_trusted_entry` on lookup (`:3265-3340`), and scopt writes them as zeros (`link.py:1859`). It bypasses the sealed and H3 rules of T->T (`:4420-4437`) |
| SPEC variant C' | **No** | Built lazily by `sc6_build_variant` (`:2446-2483`) |
| Same-region member, +15 | Yes | This is an internal edge. **Declare it** so H4 verifies it: route (a) / (a+) |
| Own member 0, +0 | Yes | The region is built before any member runs, and rebuilt after `tb_flush` (X6). This is exactly a T->T self-chain (`chain_installs_trusted_self`, `:4411-4443`). Prove H3 offline, and only for sealed loads |
| Non-entry member, +0 | **No** | X4 assert (`:4554-4556`). H1 allows can_do_io byte 0 on non-entry members, which risks an MMIO recompile, and that is fatal (X5) |

- **Captures:** 0 today, because no residual B3 target is the region's own entry (§2).
- The own-entry form is the scopt-only way to close a `goto_ptr` back-edge into member 0 *with* a poll, once 4a exists. Today such an exit takes `helper_lookup_tb_ptr`, which returns the entry's `tc.ptr` = +15 and so **skips** the poll (`cpu-exec.c:420`; `supercache.c:3226`, `:3283-3286`).
- **Effort:** small, inside (a+). **Do not** use it for any other target.

### (e) Point region entries at the post-B3 public TB

- **Accepted? No.**
  - Member 0 must be a member copy inside the region span, with the trusted prefix (L4, L5, L9, H1).
  - The public R's descriptor is never activated while its region is loaded (X1), so its exits would name a zero descriptor (X7).
- **Not needed.** All 44 / 23 / 13 B3 slots on entry members guard the call exit, and that exit is an internal call edge (§2).
- **What remains is a budget issue, fixable in scopt.** 44 of chain-static's 256 units on packet guard code that never runs in timing, while 16 profitable plans were refused for `slot_budget` (map-b3 §5.2). Fixes:
  - the stage-1 repeated chain-static entry or the 2c cap lift;
  - or exclude region-entry sources from the chain-static budget, by re-running chain-static after firmware-inline with the entry sources excluded.
  - Entry-source public code runs only when `sc7_try_region` declines (X1), so dropping their B3 changes only profiling and debug runs.
- **A loader-legal alternative that is pointless.** A callee-rooted region (entry = callee) keeps the caller public. But it routes every caller of that callee through one region (L11), and returns from other callers miss the guard.
- **Effort:** none to small (cap).

### (f) Twin source record: arm B3's own slots with today's loader

- **What.** For a root-continuation member with residual B3 exits:
  - lay it out from `eff[uid]` (post-B3 grown) instead of `by_uid[uid]` (`link.py:1913`, `:1919`, `:2001-2002`, `:1660-1663`);
  - add a **twin record**: R's output record with new `[tb, tc, end)` (a descriptor-sized hole plus one code byte, kept disjoint per L5) and a key no sealed lookup can form, for example `cflags | CF_PCREL` (X9);
  - point the member's `source_tb_offset` at the twin (`scopt/formats/sc_v7.py:247`).
- **Accepted?** Yes, by every current check.
  - The twin is a valid record (`supercache.c:480-497`).
  - It is never returned by a lookup: CF_PCREL is stripped from every sealed lookup, the flat index skips CF_PCREL records, and a sealed miss never reaches the bucket hash (X9).
  - It has no sc6 slots, so L7 requires flags 0.
  - H2 checks B3's slot shape, which the guard template already produces: 4-aligned `e9 00000000` with the reset right after (`guard.py:359-366`).
  - X2 arms the slots. The registered reset's descriptor LEA is a `MOVI_EXIT_TB_PTR` RIPREL32 site (`guard.py:375-379`), so `retarget_private_descriptor_refs` binds the exit to the member descriptor (`link.py:277-317`, `:1822-1824`).
  - After first use the main loop chains the slot: +0 into a public TB, or T->T +0 into another region (X3).
  - Nothing checks that a member's source record is the record a lookup would return for that PC (L6-L9). The rationale stated at `supercache.c:880-889`, "offsets describe the copy", still holds.
- **Captures:** **100% of the residual GENERAL** (29 / 16 / 6 copies), at B3's own cost: guard plus chained `jmp` to +0.
  - The 2 residual SPEC copies (uart) chain to the general C, not C', because `sc6.live` and links are keyed by public records (`:2485-2521`).
  - Moot members keep pre-B3 copies and flags. Twins are only worth it where B3 adds something.
- **scopt changes:**
  - `link.py`: choose the source per member; synthesize the twin.
  - `formats/sc_v7.py:180-260`: per-member source override.
  - `formats/sc_v5.py` writer: the twin body.
  - The re-import, V-rows and capture join in `validate.py` and `derive_scm_v2` must recognize twins as non-units.
  - `residual_goto_tb_slots` (`link.py:383-416`) run against the twin.
  - Pricing: set the 2b(c) penalty to 0 for these members.
- **Risks:**
  - It relies on an unchecked loader property. A future QEMU that cross-checks member PCs against lookup records, or refuses CF_PCREL records in sealed caches, would refuse to load the file (fail-closed, not silent).
  - It is valid only for sealed loads (exact_pc). scopt must refuse it otherwise.
  - The mapping-change hazard is the same as the QEMU-flag route: trusted chains are never unlinked (X6).
- **Effort:** medium to large, with no QEMU work. It costs about as much scopt work as the QEMU-flag route minus the QEMU part. It captures less than (a+) per event, but does not grow regions.

### (g) Variant of (f): record-registered, sc6-unregistered B3 slots on continuation sources

- **What.** B3 grows R and declares the slot in R's record, but emits no sc6 entry.
- **Accepted?** Yes.
  - The sc6 checks cover only listed slots (X8).
  - `lookup_entry` copies and resets any record slot (`:4048-4075`).
  - The member's mask becomes 0, so QEMU arms the slot in a post-B3 copy with R as the source.
- **Captures:** the same as (f).
- **Risk: public semantics change.** Public R's guarded chains are no longer unlinked on TLB/MPU changes (`:2592-2602`), its guard loses hash coverage (`:2361-2368`), and SPEC is impossible.
- **Effort:** medium.
- **Not recommended:** (f) gives the same result without touching public behaviour.

### (h) The SPEC bundle, host-inline, B4 and cleanup rewrites

- **SPEC, moot copies (uart 6, cli 2).**
  - R's return exit is an internal edge to C's member copy. Apply the variant's same-length CP/BF/DHO patches to that C copy when every internal in-edge to it carries the CalleeSummary facts.
  - Non-entry members are entered only through declared edges (X2, X4).
  - The loader never hashes the bytes (H5). scopt must keep the patches off the prefix (H1), unskipped slot fields (H2) and edge fields (H4). `apply_pure_rewrite` already takes a `forbid` list (`link.py:1351`; used for rewrites at `:1396` and for variants at `:1973-1975`).
  - Scopt-only. Value is tiny: 0 / 9 / 1 variants in o4r3c.
- **SPEC, residual copies (uart 2).** Through (a+), with C' (the patched clone) as the new member behind B3's SPEC guard. Or leave them.
- **Host-inline: already inside every member at every depth.** The native prefix stage replaces the snapshot before B3 (`scopt/commands/optimize.py:664-700`), and members copy from that snapshot (`link.py:1913`). Constraint X5: a splice must never need unwinding. Nothing to do.
- **B4 and cleanup rewrites (3a).** Scopt-only for **all** members, not just un-grown sources.
  - Copy `rewrite_unit(by_uid[uid], plan.rewrites[uid])` (`link.py:1383`): the rewritten but un-grown unit, which `effective_units` builds before growth (`:1865-1883`).
  - The copy still lacks B3's slots, so L7's mask stays correct, and H5 means no hash.
  - **Effort:** small to medium.

## 4. Does any part need a QEMU change?

**No.**

| B3-side effect inside members | scopt-only route | QEMU |
|---|---|---|
| GENERAL on exits the region already made internal (91-96%) | already better than B3 | none |
| GENERAL on root-continuation residual exits (29 / 16 / 6) | (a+): 4a + return expansion, better than B3; or (f) twin record, B3-equivalent; own-entry back-edges via (d) at +0 | none |
| "Dead" B3 on entry sources (44 / 23 / 13) | superseded by internal call edges; reclaim the chain-static budget (§3e) | none |
| SPEC | variant patches on member copies (h); C' as a member via (a+) | none |
| B4 and cleanup rewrites | rewrite-only member copies (h) | none |
| host-inline | already inherited | none |

**A QEMU change would be needed only for things that no route provides, including the proposed `SC7_MEMBER_B3_COPY` flag:**

- (i) Unlinking member chains on TLB/MPU changes, the way public sc6 slots are. Trusted mode ignores mapping changes for every member chain (`supercache.c:3488-3492`). The 3b flag would arm B3 slots as the same never-unlinked residual slots.
- (ii) Arming SPEC from a member descriptor straight into a public variant (`sc6.live` is per public record, `:2490-2491`).
- (iii) Diagnostic counters, for example chained region entries and lookups from member code. These are stats only, not timed.

None of these is needed under the trusted contract that O4 already uses.

**Recommended replacement for DESIGN.md §3.3:**

1. **3a:** rewrite-only member copies for all sources.
2. **(a+):** 4a plus guarded return expansion, priced. Use (d) +0 only for own-entry back-edges.
3. **(f):** only if (a+)'s region growth is refused by pricing on the continuation exits.
4. **Budget:** reclaim the entry-source chain-static budget (stage 1/2c).
5. Drop the QEMU flag, the counters-revision bump and the allowlist flag from 3b.

The whole L1 ceiling is ≤ 4.0 / 1.4 / 0.04 ms per training run (§2), so the order should follow cost. Run 4a first: it is already planned and covers the static 68% / 66% / 100% of the residual.

## 5. Refutation (adversarial check)

**Scope.** Every route above that is called loader-compatible was run against the aacca05 load path (`sc7_load_ext`, `sc7_verify_hashes`), the descriptor build, the dispatcher's chain path, and the lookup path in **every** load mode. Every rejection was derived again from the code. The §2 script was re-run read-only on the three o4r3c caches. Nothing was built, timed or edited in either repository.

### 5.0 Corrected verdict

**The headline survives:** no part of this needs a QEMU change. Five findings change:

1. **(a+)** is loader-legal, and it is the only route that is safe in every load mode. But its poll rule is too weak. "Edges into member 0 stay residual" does not exclude poll-free task loops (§5.2).
2. **(f)** is loader-legal and correct under `QEMU_SC_SEALED=1`. As specified, though, it **corrupts the arena** when the same file is loaded without sealed or exact-PC mode. "scopt must refuse it otherwise" cannot be enforced, because the load mode is a runtime environment variable. (f) also needs three more conditions (§5.3):
   - an unformable twin key, or a full-body twin;
   - GENERAL slots only, because SPEC slots never chain;
   - chainable targets only.
3. **(g)** is **not** loader-compatible as described (§5.5).
4. **(d), own entry at +0,** is valid in every load mode. The "sealed only" restriction can be dropped (§5.4).
5. **Confirmed unchanged** (§5.6): (a), (b), (c), (d) for every other target, (e), and (h).

### 5.1 The §2 numbers reproduce exactly

`b3_in_members.py` was re-run on `packet-w/…/e831f759…`, `uart-w/…/a8df0ed9…` and `cli-w/…/e113476e…`.

- **Classification.**
  - moot: 770 / 162 / 147;
  - moot on entry members: 44 / 23 / 13;
  - moot on nested-context members: 726 / 139 / 134.
- **Every residual slot is in the root context** (`ctx0`, role `continuation`):
  - packet: 20 public + 9 other-region entry;
  - uart: 13 + 3;
  - cli: 6 + 0.
  - No residual slot is on a nested-context member or on an entry member.
- **Residual weights** (distinct (R, slot), steady phase):
  - packet: xc2 317,731 + 131,633 = 0.449M, and xc1 148,007 + 63,864 = 0.212M;
  - uart: xc2 0.150M, xc1 0.077M;
  - cli: xc2 6,795.

So "all residual is on root-continuation members" holds, and 4a plus return expansion is the right target set.

### 5.2 (a+): the loader accepts it; the poll rule must be stronger

**Why the loader accepts it.**
- New members are captured units, so they carry the H1 prefix. They are non-icount (L8). They lie inside the region span, contiguous and 16-aligned (L9).
- New edges are kind 1 with SKIP_POLL (`supercache.c:1418-1436`) and land on +15 (`:1630-1645`).
- The root continuation stays a pre-B3 copy, so its flags still equal R's sc6 mask (`:1311-1318`), and H2 skips B3's slot (`:1514-1518`).
- xc1 needs a private guard. `trusted_return_guard_problem` requires cortex-m3, a source that is not already grown, exactly one lookup tail, and a free slot (`scopt/link.py:458-481`). A root continuation with a B3 slot had a free slot in its pre-B3 copy, because B3 claims only free slots (`link.py:341-349`).

**Hole: poll-free cycles that do not pass through member 0.**
- `member()` returns the existing index for a known `(uid, context)` (`scopt/analysis/firmware_regions.py:46-52`), and the branch rule appends an internal `branch` edge to it (`:170-175`). "Ordinary CFG backedges terminate through the visited-member set" (`:31-33`).
- So with 4a, any loop in the caller after the call that does not re-execute the call unit becomes an internal cycle without member 0. Example: `f(); while (!flag) {}`. The continuation's self-branch becomes an internal edge to its own +15.
- Today that exit is a residual `goto_tb`. It is armed and chained to the ordinary TB's `tc.ptr` = +0 (`supercache.c:4465`, `:4044`), so every iteration polls. After 4a it never polls, and a flag set by an ISR is never observed.
- `docs/firmware-inline-policies.md:84` accepts this as a trusted-ABI property. But DESIGN.md §2.1 row 11 and §5 ("Closed loops without a poll are excluded by construction (4a)", DESIGN.md:603) claim it is excluded. Under the member-0 rule alone, it is not.

**Return expansion makes it worse.**
- The profiled return target T is in the loop that calls the entry's function.
- If T is expanded, T's call into that function gets a nested context. The loop then closes through context copies of the entry unit, never through member 0, and the whole task loop becomes poll-free.

**Fix (scopt only).**
- In context `()` and in every return-expansion context, an edge whose target `(uid, context)` is already a member stays residual. The one exception is an edge to member 0, which may use (d)'s +0 entry.
- A return-expansion target is added as a **leaf**: its exits stay residual.
- Offline test: the internal-edge graph restricted to those contexts is acyclic.

**What it captures.** "Up to 100%" remains an upper bound. `compatible_units` returns nothing for ambiguous or unverified groups (`scopt/analysis/firmware_calls.py:79-93`), and the expansion and pricing callbacks may refuse an edge (`firmware_regions.py:163-169`). Back-edges that are kept residual also lose their share.

### 5.3 (f), the twin record: legal and correct only under sealed or exact-PC loads

**Confirmed under `QEMU_SC_SEALED=1`:**
- The twin is a valid record (`supercache.c:484-490`). sc6 only rejects duplicate `tb_offset` (`:2131-2141`).
- The flat index skips `CF_PCREL` records (`:520-523`, `:544`, `:562`). A sealed miss is fatal before the bucket hash is consulted (`:4184-4191`).
- `want_flags` is 0 (`:1288-1293`, `:1311-1318`). H2 holds for the guard slot: `e9` plus 4 zero bytes, aligned so that `(pos+1) % 4` is 0, with the reset at field + 4 (`scopt/guard.py:362-367`).
- The reset LEA is a `MOVI_EXIT` site with aux = `slot_n` (`guard.py:374-379`), so `retarget_private_descriptor_refs` binds it to the member descriptor (`link.py:277-317`).
- The slot is armed (`supercache.c:3229-3247`) and chained by the main loop (`cpu-exec.c:1145-1155`, `:1178-1180`).

**(f-1) Not lookup-invisible in every load mode: memory corruption.**
- ARM always sets `CF_PCREL` (`target/arm/cpu.c:1971`). Only exact-PC mode strips it (`supercache.c:399-402`), and exact-PC is `sealed || QEMU_SCOPT_SEALED_RANGES` (`:382`).
- Nothing requires sealed mode to load a composed trusted cache (`:2925-2942`, `:1187-1208`).
- In a plain load, every lookup carries `CF_PCREL`. Such a key uses pc 0 and matches on `page_addr0` (`:212-221`, `:4126-4133`). So the twin, which has R's `page_addr0`, flags, `cs_base` and `cflags | CF_PCREL`, is the **only** record that matches R's PC. The real records are exact-PC and miss.
- `lookup_entry` then activates the twin as a public TB (`:3960-4091`). `tb_reset_jump` writes rel32s at twin.tc + the post-B3 slot offsets (`:4071`), which is past a 1-byte body and into neighbouring arena code. The twin's body then executes.
- Today that mode merely misses every record (`scopt/run/profile_run.py:11-15`).
- **Fix (scopt only), either of:**
  - **Preferred:** a key no lookup can form in any mode. Keep `CF_PCREL` clear and R's page-offset bits, and set `page_addr0` to a physical address outside all memory. `key_equal` (`supercache.c:205-209`, `:3960`) then rejects the twin even when it sits in R's flat chain. The shadow descriptor inherits the fields (`:3219-3229`), but a shadow is never in page tables or the host-PC tree (`:3290-3300`), and non-entry members never take a `TB_EXIT_REQUESTED` restore.
  - A complete post-B3 body for the twin, bound to its own descriptor. It is then a correct TB if a plain load ever finds it, at the cost of R's code size per twin.

**(f-2) Twin SPEC slots never chain.**
- A SPEC slot's reset path is `jmp F`, the fallback lookup, not an `exit_tb` (`guard.py:374-389`, `rk = "jmp_F"`). The dispatcher never sees `last_tb` for it, so it never patches it.
- Public SPEC slots are armed only by sc6 activation (`supercache.c:2485-2521`).
- So the 2 uart residual SPEC copies would pay for the guard and still take the lookup. The claim in §3(f), "chain to the general C", is wrong. **Twin GENERAL slots only.**

**(f-3) Gains only where QEMU arms and chains the slot. Elsewhere it is a regression.**
- The slot is unarmed under `QEMU_SC_SEALED_SKIP_IRQ` and `no-trusted-exit-chain` (`supercache.c:3275-3276`, `:4478-4480`).
- A chain to a trusted target is refused:
  - when the load is not sealed (`:4420`);
  - under `no-trusted-to-trusted-chain`;
  - when the target region's H3 failed (`:4432`).
- A two-page target is never chained (`cpu-exec.c:1137-1139`).
- In each of these cases every guard hit exits to the dispatcher (about 40 ns) instead of taking the pre-B3 lookup (about 7 ns).
- O4 requires sealed and excludes SKIP_IRQ and `QEMU_SC_V7_EXPERIMENT` (`scopt/o4.py:287`, `:301-306`). Within O4 the condition therefore binds only on the 9 / 3 / 0 other-region targets and on two-page targets. scopt must prove the target region's H3 offline, as in (d), and refuse two-page targets.

**(f-4) Not "exactly" the 3b flag.**
- An entry member's source must be the region source (`supercache.c:1358-1361`), and a region is found only through its entry source's lookup (`:1440-1456`, `:3361-3372`). So entry members cannot use a twin.
- This is harmless, because all 44 / 23 / 13 entry-member B3 slots are moot (§5.1).

**(f-5) More scopt work.**
- `profile_run` refuses a `mixed` key mode under `--sealed` (`scopt/run/profile_run.py:108-113`, `:171-172`). A `CF_PCREL` twin trips this check; the page-address key of (f-1) does not.
- The importer's exact groups (`firmware_calls.py:36-44`) and validate must also skip twins.
- **Effort: large.**

### 5.4 (d): own member 0 at +0 is valid in every load mode

- **Loader:** nothing scans member bodies beyond the prefix, unskipped slots and declared edge fields (`supercache.c:1605-1650`).
- **Runtime:** the poll exit names the entry descriptor, which satisfies the stats assert (`:4554-4556`), and the PC is restored from that descriptor (`cpu-exec.c:480-490`).
- **Correction:**
  - "Sealed only" is neither enforceable by scopt nor needed. The jump sits inside the region, and the region runs only after `sc7_build_trusted_entry` has built its descriptors in any mode (`supercache.c:3268-3340`). `tb_flush` runs between TBs and forces a rebuild before re-entry.
  - Keep the offline H3 proof: byte 14 = 1, and the stub LEA names the entry descriptor.
  - SKIP_IRQ removes only public polls (`:4031-4042`), so the member-0 poll still runs.
- **Non-entry +0: the rejection holds, for a narrower reason.**
  - In a timing run the poll exit would name the shadow descriptor and restore the member's own PC correctly.
  - The real blockers:
    - the V7-stats `g_assert` (`:4554-4556`) aborts gate and diagnostic runs;
    - H1 allows can_do_io byte 0 (`:1621-1625`), so an MMIO access would hit `cpu_io_recompile`, which is fatal (X5).

### 5.5 (g) is not loader-compatible as described

Its "Accepted? Yes" is wrong.

- Dropping R's sc6 entries makes `want_flags` 0 for **every** member whose source is R (`supercache.c:1288-1293`, `:1311-1318`). H2 then verifies B3's slot offset in every copy of R.
- Pre-B3 copies lack that slot, so `f + 4 > tc_size` or the bytes are not `e9 00000000`. That leads to `sc7_die("trusted member %u residual slot %d is not a reset goto_tb")` (`:1519-1527`). This hits all 44 / 23 / 13 entry members and every moot nested copy.
- The only way out is to make every copy post-B3, and that fails too:
  - it hits `return_guard_source_already_grown` (`link.py:466-468`) and "unit is already grown" (`link.py:1242-1243`);
  - the post-B3 tail is `jmp S`, which `lookup_tails` misses (DESIGN.md 3b).
- So the 770 / 162 / 147 moot internal edges would need 3b's slot-patching composition. (g) is not a viable scopt route.

### 5.6 Confirmed without change

- **(a):** a skipped slot that is also an edge field passes (`supercache.c:1514-1518`). It captures nothing, because no residual target is in the same region (§5.1).
- **(b):** arming is per source record and per unskipped slot (`:3229-3247`).
- **(c):** `want_flags` is fixed by the file (composed, trusted and MEMBER_DESCRIPTORS), never by scopt's `member_slot_skip` (`:1311-1313`).
  - With `off`, the linker falls back to alias (`link.py:1748-1751`), and alias arms nothing (`supercache.c:3275`, `:4321-4324`).
  - The computed task's premise, that `off` gives `want_flags` 0 with arming, is false.
- **(d) to public, other-region or variant targets:**
  - A zero descriptor plus `tb_set_jmp_target` through `tc.ptr` NULL (`cpu-exec.c:629-640`, `:652-673`).
  - PC 0 on a poll exit (`cpu-exec.c:480-490`).
  - scopt's own `TaskBoundaries` says the same (`scopt/analysis/rtos_boundaries.py:74-75`).
- **(e):** member 0 must be a copy inside the region (`supercache.c:1323-1336`, `:1350-1407`). The public entry TB is never activated while its region is loaded (`:3962-3969`).
- **(h):**
  - `rewrite_unit` is same-length (`link.py:1383-1400`).
  - B3 growth appends after the original code (`link.py:1253-1254`) and claims only free slots (`:341-349`), so rewrite-only copies keep L7's mask and H2's offsets.
  - Host-inline splices before B3 (`scopt/commands/optimize.py:664-700`).

### 5.7 Corrected order

1. 3a/(h): rewrite-only member copies.
2. (a+): 4a first, with the §5.2 rule (root-context and return-context back-edges residual; return targets as leaves).
3. (d): own member 0 at +0 for back-edges into the entry, after the offline H3 proof, in any load mode.
4. (f): only as a fallback. Use an unformable `page_addr0` key or a full-body twin, GENERAL slots only, and chainable targets only.
5. Reclaim the chain-static budget on entry sources.

**What still needs QEMU:** only what no route provides, and the 3b flag would not provide it either:
- unlinking member chains on MPU changes;
- SPEC arming from member descriptors;
- counters.

The 3b flag would hit the same SKIP_IRQ and chainability limits as (f-3).
