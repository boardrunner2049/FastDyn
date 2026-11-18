# Gate close-out design: the LOG_REG coverage block as one host event (`HostLog`), a flags-after-helper detector, and the re-gate plan

Date: 2026-11-17. Student 2. Status: **design only** (no code changed on `main`, which is at 08a6834). Scope: the two open gate failures that share one root cause, plus the B4 item-1 detector gap:

- B3 gate 4a, F-B3-23 (O6 under coverage): vacuous, because no validated artifact has a variant under coverage (the maintainer's evaluation report `B3-gate.md` (not in the repository) §1.4, the maintainer's evaluation data (not in the repository)).
- B4 gate item 4, F-B4-10 / T-B4-13: vacuous, because B4 deletes nothing on coverage units (the maintainer's evaluation report `B4-gate.md` (not in the repository) item 4, the maintainer's evaluation data (not in the repository)).
- B4 gate item 1: `flag_store_before_helper` has no dynamic detector (the maintainer's evaluation report `B4-gate.md` (not in the repository) item 1 and Known gap 2, the maintainer's evaluation data (not in the repository)).

Rules that apply to the work below: `src/qemu`, `src/fastdyn`, the WP-QINT QEMU tree and the gate QEMU tree are never modified. The gate binary `qint-v6-gate-1` (exe sha256 `56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90`) is used for every gate run. QEMU is launched only through the existing scopt/test drivers. Failures fail closed. No check is weakened. Any change to a gate criterion is disclosed in the report.

Evidence for this analysis came from throwaway scripts outside the repository (not kept), with no repository edits: `dump_logreg.py`, `survey.py` (output in `survey.json`), `liveout.py` (`liveout.txt`), `proto.py` (`proto-syncdead.txt`), `proto_re.py` (`proto-return-edges.txt`) and `unitdump.py`. The prototypes import `tools/scopt` read-only and monkeypatch nothing in the repository.

---

## A. The LOG_REG block as one atomic host event `HostLog(pc)`

### A.1 What the block is (source and real bytes)

**Source.** In FastDyn `core/core.c:848-865`, `coverage = true` registers an inline `QEMU_PLUGIN_INLINE_LOG_REG` on instruction 0 of every TB, with these arguments:

- `entry.offset = &core_cc_ret.list->log_buf` (a `Buffy {uint8_t *buffer; uint16_t index;}`);
- `entry.data = &tracer_consumed_index` (the consumer index, `static uint16_t`, core.c:802);
- `reg = 15`.

The QEMU side is `gen_inline_log_reg_cb` / `log_reg()` in `target/arm/tcg/translate.c:233-279`. Because `consumer_index != NULL`, it emits the following:

```
wait:  idx  = ld16u [&index]
       next = ext16u(idx + 4)
       cons = ld16u [&tracer_consumed_index]
       brcond EQ next, cons -> wait          # spin while the ring is full
       p    = buffer + (intptr)idx           # ext_i32_ptr + add_ptr  (add r64,r64)
       st32 [p] = cpu_R[15]                  # the coverage event (guest PC of the TB)
       st16 [&index] = idx + 4
```

`buffer` is `malloc(UINT16_MAX + 1)` (65,536 bytes), made by the tracer thread (core.c:979) and never freed. `index` is written only by these blocks. The static initial value is 0 and every write adds 4 (16-bit wrap), so the index is always a multiple of 4 in `[0, 0xfffc]`. The tracer only reads `index` and writes `tracer_consumed_index` (core.c:986-991). The FP-register variant (`reg >= 16`, `gen_helper_arm_log_fp`) and the logger variant (`consumer_index == NULL`, no spin) are not used by coverage and are **not** matched (see A.7).

**Real units.** The census covers the four coverage snapshots on the gate binary: F-B4-10 and F-B3-23, each in icount and noicount (`survey.json`). It found 238 blocks in 238 units: 62 + 62 + 57 + 57. The other 9 units in each snapshot are `CF_MEMI_ONLY|count 1` io-recompile TBs (cflags `0xff033001`/`0xff031001`). Plugin registration is skipped for those, so they have no block. In the census:

- There is **exactly one byte form**. It always starts at `insn0_start` (0x16 in icount, after the 22-byte icount+can_do_io prefix; 0xf in noicount, after the 15-byte prefix). It is always inside guest instruction 0's host range.
- The icount and noicount forms differ **only** in the three rel32 displacement fields (byte offsets 3, 23 and 42 of the block). There are no register, spill or encoding differences.
- All 238 blocks resolve to the same three absolute pointers. They are the same in both fixtures and both modes, because ASLR is off and process layout is deterministic:
  - `IDX = 0x7fff63f6e570` (`&core_cc_list.log_buf.index`; importer class `abs.plugin_data`, `libfastdyn.so+0x24f6e570`);
  - `CONS = 0x7fff43da945c` (`&tracer_consumed_index`; `abs.plugin_data`, `libfastdyn.so+0x4da945c`);
  - `BUF = 0x7fff68000b70` (the ring; importer class **`abs.other`**, `anon@0x7fff68000000+0xb70`, which is the tracer thread's malloc arena).
- **First-insn and spill effects** show up only *after* the block. In 29 of 62 (F-B4-10) and 30 of 57 (F-B3-23) units, guest instruction 0 reuses `r14`, which holds `env.regs[15]`, loaded inside the block (CF_PCREL: pc-relative addresses are computed from `regs[15]`). No other block register is read before it is redefined. One unit's `sbb r13d,r13d` shows up as a "read" in capstone's operand list, but it is the zero idiom and its value depends only on CF. No unit reads host flags after the block (`liveout.txt`). Two units start their first instruction with the TCG barrier `lock or [rsp],0`.

Why the form is fixed: the block is the first code after a `set_label` at the TB's first `insn_start`. At that point TCG has no live temporaries and every global is in memory. The register allocator therefore starts from an empty state, and its fixed allocation order gives `rbx`, `r12`, `r13`, `r14` every time. The loop body is 36 bytes, so the backward branch is always `je rel8`.

Canonical form (65 bytes; `dN` are rel32 holes; offsets are relative to the block start `S`):

| off | bytes | insn | role |
|---|---|---|---|
| +0x00 | `48 8d 1d d1` | `lea rbx,[rip+d1]` | I := IDX (MOVI_CONST site) — **spin label target** |
| +0x07 | `44 0f b7 23` | `movzx r12d, word [rbx]` | X := idx (host load, IDX) |
| +0x0b | `45 8d 6c 24 04` | `lea r13d,[r12+4]` | N := X + 4 |
| +0x10 | `45 0f b7 ed` | `movzx r13d, r13w` | N := ext16u(N) |
| +0x14 | `4c 8d 35 d2` | `lea r14,[rip+d2]` | C := CONS (MOVI_CONST site) |
| +0x1b | `45 0f b7 36` | `movzx r14d, word [r14]` | C := consumed (host load, CONS) |
| +0x1f | `45 3b ee` | `cmp r13d, r14d` | |
| +0x22 | `74 dc` | `je S` | **spin** (LABEL_JCC site, target = S) |
| +0x24 | `4d 63 ec` | `movsxd r13, r12d` | P := sext(X) |
| +0x27 | `4c 8d 35 d3` | `lea r14,[rip+d3]` | B := BUF (MOVI_CONST site) |
| +0x2e | `4d 03 ee` | `add r13, r14` | P := BUF + X (the `add r64,r64` HostOps cannot type today) |
| +0x31 | `44 8b 75 3c` | `mov r14d,[rbp+0x3c]` | V := env.regs[15] (ENV_LOAD, exact field `regs[15]`) |
| +0x35 | `45 89 75 00` | `mov [r13], r14d` | **ring store** (the coverage event) |
| +0x39 | `41 83 c4 04` | `add r12d, 4` | X := X + 4 |
| +0x3d | `66 44 89 23` | `mov word [rbx], r12w` | **index store** (IDX) |

### A.2 The matcher (one shared, pure function; fail closed)

New module **`scopt/decode/logreg.py`**:

```
match_logreg(code, base, insn0_start, sites, bindings, insn_ranges) -> LogBlock | NoMatch(rule)
```

It is pure: no HostOps, no store, no analysis cache. HostOps and hostsym's `Program` both call it, so there is one definition of "the block". Both clients still decode and execute every other byte themselves (A.5 explains how hostsym keeps its independence). A block is matched **only if every** rule below holds; otherwise `NoMatch(rule)` is returned.

1. **Position.** `S == insn0_start`. `[S, S+65)` lies inside guest instruction 0's host range (the `.scm` insn row). It does not overlap the prefix, an LDST fast or slow range, an exit stub, a guard stub or the pool.
2. **Bytes.** `code[S:S+65]` equals the canonical form above at every byte outside the three rel32 holes (+0x03, +0x17, +0x2a). Register renaming, the `movabs`/`mov r32,imm32` constant forms, a `rel32` `je`, and any inserted, reordered or extra instruction all fail. If a future build or allocator emits another form, it is unmatched and stays pinned (A.7). The census counts it, so the regression is visible.
3. **Metadata agrees with the bytes.** The leas at +0x00, +0x14 and +0x27 each carry exactly one `MOVI_CONST` site, and the site value equals the rip-resolved target (IDX, CONS, BUF). The `je` at +0x22 carries a `LABEL_JCC` site whose value is `base + S`. No other site (HELPER_CALL, LDST, GOTO_*, EXIT_*) and no OpMark other than the `insn_start` at S falls inside `(S, S+65)`.
4. **No other entry.** No other branch or site in the unit targets any offset in `[S, S+65)`. The only branch into `S` is the block's own `je`. Fall-through from the prefix into `S` is allowed.
5. **Pointer classes** (from the importer's `unchecked_host_pointers` bindings of the three sites):
   - IDX and CONS are both `abs.plugin_data` and are anchored in the same module (`libfastdyn.so`).
   - BUF is `abs.other` with an `anon@` anchor (a nameless anonymous mapping). It is never `abs.heap`, `arena.*`, `prologue.*`, `abs.guest_ram`, `abs.qemu_data`, `abs.plugin_data` or file-backed. BUF and IDX/CONS are distinct.
   - **Amended (closeout2, `scopt` commit 8c3b46f):** the merged close-out (36a28a9) also accepted `abs.heap`. That was unsound on the gate binary: env lives in `[heap]` there (the maintainer's evaluation report `B4-gate.md` (not in the repository), env at `0x5555594951c0`) and gate runs pass no `env_abs`, so neither this rule nor V12 `_abs_violations` could tell a ring pointer from an env pointer. Any ring-base class other than `abs.other anon@` is now `NoMatch("pointers", "ring_base_class_refused: ...")`. All 238 real blocks have BUF in `anon@0x7fff68000000+0xb70` and still match (the maintainer's evaluation report `B5-plan.md` (not in the repository), "Re-freeze (close-out)").
   - The pointers are bound, not just decoded. An unbound site is a NoMatch.
6. **Field.** The +0x31 load is an exact `regs[15]` access under the bound EnvLayout (`rbp + 0x3c` on the gate binary's layout; the offset is resolved via EnvLayout and not hard-coded).
7. **Snapshot consistency.** All matched blocks of one snapshot, and of an original/candidate pair, share **one** `(IDX, CONS, BUF)` triple, because there is one coverage ring per process. If a second distinct triple appears anywhere, every block in that snapshot is unmatched.

Result record:

```
LogBlock(start=S, end=S+65, spin_jcc=S+0x22, head_end=S+0x24, ring_store=S+0x35,
         idx_store=S+0x3d, value_load=S+0x31, idx_abs, cons_abs, ring_abs,
         ring_extent=0x10003, value_field="regs[15]",
         regs={I:rbx, X:r12, N:r13, C:r14, P:r13, B:r14, V:r14},
         canon=sha256(bytes with the holes replaced by the resolved absolute targets))
```

The ring extent is `[BUF, BUF + 0xffff + 4)`. That is the largest range a 4-byte store at a 16-bit index can reach. It covers the 65,536-byte allocation plus a 3-byte tail that is reachable only if the index were not a multiple of 4 (see A.6).

### A.3 HostOps (`hostops@1`, WP17 code)

- `_Builder.build` decodes, then calls `match_logreg` before typing. The resulting offsets are passed to `_mem_op`.
- The **ring store** (`mov [P], V32`) is typed `HOST_ABS_STORE` with `abs_addr = BUF` (the ring base), `mem_base = P` and a new field `log = k` (the block index). It no longer pins `unexplained_host_store`. This is the only pin the match removes, and only at that one offset.
- The **index store** and the two loads already type correctly through the constant tracking (`abs_addr = IDX` / `CONS`). They get `log = k`.
- Every other op in `[S, S+65)` keeps its current kind and also gets `log = k`. The `je` additionally gets `spin = True`.
- `UnitHostOps` gains `log_blocks: tuple[LogBlock, ...]`.
- `HostOp.to_json` writes `log`/`spin` **only when set**. The content digest of every unit without a block is therefore byte-for-byte unchanged, and so are the analysis cache keys of all non-coverage units.
- Census adds `log_blocks_matched` and `logreg_unmatched{rule: n}`. A unit with a HOST_ABS store or a backward LABEL_JCC that fails the match is counted there and keeps its existing pin.

### A.4 hcfg, syncdead and the other dataflow analyses

**hcfg (`hcfg@1`).**
- The `je` with `spin=True` gets an edge `S+0x22 -> S` of kind `logreg_spin`, **only in the full view** (`succ_full`). In the atomic view it has only its fall-through edge.
- `back_edges` and `cyclic` are computed on the atomic view, as today, so a unit whose only cycle is a matched spin is acyclic for path enumeration. The spin edge is recorded in a new field `spin_edges`.
- Defensively, if HostOps reports a block but the op indices are not contiguous, or the `je` target is not op `S`, hcfg sets `ok = False` with `logreg_block_malformed`.
- Any other backward edge stays a back edge, so the graph is cyclic and gives `path_budget_exhausted` (unchanged).

**hlive** uses the full view, including the spin edge. Its fixpoint loop already handles cycles. The result is that registers live around the loop stay live.

**syncdead (B4).**
- No transfer change is needed. The block's ops have known `abs_addr`, so `_gen_kill` no longer marks the unit unknown. The `regs[15]` ENV_LOAD gens `regs[15]`, which is not a tracked byte.
- The block contains no env store, no call, no LDST and no exit, so it is **not an observer** of D1/D2/D3 in `mode="b4"`.
- In `mode="tcg"`, the spin `je` and the label at S remain P points (TCG really does `la_bb_sync` there; nothing is dirty at that point). M0 = 0 is unaffected.
- New placement refusal `edit_inside_log_block` (added to `B4Reason.FAILED`) applies to any candidate whose bytes overlap a block. It is vacuous for env stores today, but it is the rule V15 checks.

**hconst (B3 CP/BF).** Today a `HOST_ABS_STORE` kills every env fact (`hconst.py:335`). For an op with `log is not None` it must **not** kill env facts. The ring and index stores are outside env (A.6), and register effects go through the per-op transfer as usual. Without this change, C''s entry facts (for example `r0 == 0` on F-B3-23's F-B3-1 shape) die at the ring store, and CP/BF have nothing to fold. UPDATE_MEM and other unmatched host stores keep killing facts.

**exit_facts / return_edges / callee_summary** need no change beyond acyclicity and the classified ring store. `exit_facts` kills only on `abs_addr is None`. `callee_summary` lists the block's stores as `host_abs_store` effects of R, which is correct.

**Prototype check (scratch, `proto.py`, `proto-syncdead.txt`).**
- With the matcher, the ring-store classification and the atomic spin edge removed, every coverage unit becomes HostOps-ok and acyclic, and syncdead succeeds: 62/62 on F-B4-10 and 57/57 on F-B3-23.
- F-B4-10's dead stores are **D1 17 / D2 45 / D3 129** in both modes. These are exactly F-B4-1's numbers (17/45/129, the maintainer's evaluation report `B4-gate.md` (not in the repository) item 2), which is what is expected because F-B4-10 is F-B4-1 under coverage.
- `return-edges` on F-B3-23 (`proto-return-edges.txt`) goes from `unit_not_relocatable` 31 and `exit_not_goto_ptr` 31 to the same reason profile as the non-coverage F-B3-1 base (`exit_not_goto_ptr` 2, no `unit_not_relocatable`). The R exit that F-B3-1 specializes therefore becomes eligible under coverage too.

### A.5 hostsym (V12/V13/V14; WP19 code)

**Program** (`validate_sym/program.py`):
- `Program.logblocks: {S: LogBlock}`, built by `match_logreg` from the Program's own code bytes, sites and bindings.
- `abs_classes[ring_abs] = "abs.logreg_ring"` is set only for a matched block's BUF.
- The patched copy (`patched()`) re-runs the matcher on the candidate bytes. Candidate blocks are **not** inherited from the original.

**Paths** (`validate_sym/paths.py`):

- **Loop head.** At `pc == S` of a matched block, `_logblock_head` executes the instructions in `[S, S+0x22)` with hostsym's own `execute()`. The only exception is the two `movzx` loads, which return fresh terms `("logld", k, "idx"|"cons", seq)` and not `("hmem", hver, addr, w)`. Those cells are shared with the tracer thread, so no equality with any other host-memory term may be assumed. The `je` is then **not forked and not executed**, and stepping continues at `S+0x24`. The path records `assume ("logreg_spin_exit", S)`, which is not part of the path signature.
- **Rest of the block.** `[S+0x24, S+65)` is executed normally by hostsym's own semantics, so `V` really is `env.read(regs[15])`, `P = BUF + sext(X)`, and so on.
- **Ring store.** `_Mem.write` intercepts the ring store at `pc == ring_store`. It emits **one** `Event("host_log", "coverage", (ring_abs, value_term), pc=S)`, records `abs_refs (pc, ring_abs, "w", ring_abs)` (class `abs.logreg_ring`), and does `hver += 1`.
- **Index store.** The index store stays an ordinary `host_store` event at the constant IDX (`abs.plugin_data`). Its value term `X + 4` is therefore compared too, so an "add 8" mutant is caught.
- **Backward branches.** `_jump` raises `backward_branch` exactly as today **unless** `pc == lb.spin_jcc` and the target is `lb.start` for a matched block. That pc is never reached anyway, because the head abstraction skips it; the exemption exists for defence. Entering `(S, S+65)` anywhere other than S raises `Unsupported("enter_log_block_middle")`.

**Equiv** (`validate_sym/equiv.py`):
- `_structural`: original and candidate (through `mu` for C'/C) must have matched blocks in 1:1 correspondence at mapped offsets, with equal `canon`, equal `(IDX, CONS, BUF)` and equal register bindings. Otherwise the verdict is a mismatch `edit_inside_log_block`, in the FAILED class (not inconclusive). A candidate may not gain or lose a block.
- `compare_traces`: `host_log` events compare on `(ring_abs, value_term)` and on their position in the event sequence, like `host_store`. The LOG_REG stores can therefore be neither removed nor reordered against guest accesses, calls or other host stores.
- `ALLOWED_ABS_CLASSES` gains `abs.logreg_ring`, with an extra condition in `_abs_violations`: if `env_abs` is given, `[ring_abs, ring_abs + 0x10003)` must not intersect `env_abs` (numeric check). Without `env_abs`, the class is allowed on the argument in A.6. A `abs.logreg_ring` reference that is not the ring store of a matched block is a violation (fail closed).

**V15 placement:** a patch overlapping any `LogBlock` gives `edit_inside_log_block`. B3's CP/BF/DHO also refuse such edits in their own placement checks: DHO "never inside the prefix" becomes "nor inside a LogBlock", and CP never rewrites the block's `regs[15]` load. C' copies the block verbatim. The relocator's existing `rel_minus_delta` fixups for the three leas apply, and the C' block matches again with the same absolute triple.

### A.6 Soundness argument

What the abstraction claims, and why each claim holds:

1. **The block writes no env byte.**
   - The index store writes `IDX` (2 bytes), a plugin `.bss` object.
   - The ring store writes inside `[BUF, BUF+0x10003)`. BUF is the start of a live 65,536-byte `malloc` block (core.c:979, never freed), and env lives inside the live ARMCPU QOM object, so two live allocations cannot overlap.
   - The index is 0 initially (static), and its only writers are these blocks (`+4`, 16-bit; FastDyn source grep: `log_buf.index` is written nowhere else), so it stays a multiple of 4 and the ring store lands inside the allocation. The 3-byte tail in the extent is a conservative bound only.
   - When the env address is known (`env_abs`, the gdb diag path), the check is also numeric.
2. **The block reads exactly `env.regs[15]` from env**, plus host memory at IDX and CONS. hostsym executes the post-loop part with its own semantics, so the value logged and the live-out `V` are derived, not assumed.
3. **The spin loop equals one iteration with arbitrary loads.**
   - The loop body `[S, S+0x24)` has no store, no env access, no call and no exit. Its only effects are register and flag definitions from two host loads.
   - Any number of iterations ends in a state that a single iteration with fresh, unconstrained load values (subject to `next != cons`) also reaches.
   - Termination is not part of equivalence. Original and candidate run the identical block (byte-frozen, A.5) at the same position after identical event prefixes, so they spin the same way. The ring memory is not touched by any B3/B4 edit.
   - Concurrency with the tracer thread is covered by the fresh-load terms: no relation between loads is assumed.
4. **Nothing can enter the loop except at S** (matcher rule 4), so collapsing the back edge loses no path.
5. **The stores are never removed or changed.**
   - Byte-freeze: V15 placement plus equiv `_structural` `edit_inside_log_block`.
   - V12 event equality on `host_log` and the index `host_store`.
   - The static mutation suite (A.8).
   - O6 dynamically. The ring is guest-observable coverage output.

**What a wrong match could hide, and why the matcher fails closed.** If a non-FastDyn sequence were accepted as a block:

- (a) An env write through the "ring" pointer would be invisible to syncdead and hconst. B4 could then delete a store whose bytes the block overwrites, and CP could fold a stale fact.
- (b) An env read through IDX/CONS would make a "dead" deletion change the spin or the slot.
- (c) A store inside the loop would repeat per iteration, which the one-iteration model ignores.
- (d) A second entry into the loop would add paths the collapsed graph does not have.

Rules 2-7 exclude every one of these by construction:

- The bytes are exact, so only the canonical 15 instructions exist, with only loads in the loop.
- IDX, CONS and BUF must be importer-bound non-env classes; BUF in particular cannot be the arena, guest RAM or a module image.
- The value load must be `regs[15]`.
- There is no other entry, and one triple per snapshot.

Any deviation produces `NoMatch`, and **the unit is treated exactly as today**: `unexplained_host_store` pin, cyclic hcfg (`path_budget_exhausted`), and hostsym `backward_branch` (UNSUPPORTED, so `validation_inconclusive`). The matcher only adds acceptance for one exact, fully explained form; it never relaxes the treatment of anything else.

**Residual dependency.** HostOps and hostsym share the matcher, so a matcher bug would affect both the pass side and the validator. Three things contain this:

- hostsym abstracts only the loop head and the ring-store event. Every other byte, including the value stored, is executed by hostsym's own semantics.
- equiv requires the candidate block to be byte-identical to the original's.
- The negative unit suite in A.8 tests the matcher.

The baseline's own LOG_REG behaviour (for example, whether BUF is still valid in a later process) is the baseline's property and not something the artifact changes. Every artifact keeps the original block bytes.

### A.7 What B3/B4 may now edit around the block

| region | B4 (D1/D2/D3 nop-fill) | B3 (specialize-returns / CP / BF / DHO / chain-*) |
|---|---|---|
| prefix `[0, S)` | unchanged rules: icount bytes frozen; the single prefix `can_do_io` store is D2a-deletable when dead (the block reads no `can_do_io`; the first real reader is usually insn 0's LDST/call, S5/S4) | unchanged: no CP/BF/DHO in the prefix; §3.5 C' patches as today |
| block `[S, S+65)` | **nothing** (`edit_inside_log_block`, FAILED) | **nothing**; C' carries a verbatim, relocated copy |
| after the block | all existing rules, now actually applied: the predicted F-B4-10 deletions are D1 17 / D2 45 / D3 129 | all existing rules; CP facts survive the block (A.4 hconst) |
| R units that contain a block | eligible as R (lookup tail at the end; the guard stub is emitted at the tail as today; hlive over the full view chooses scratch registers) | |

C' logs C's guest PC. R's guard atom `g_pc` checks `env.regs[15] == C.pc` before jumping to C', R's `regs[15]` store is `pc_store_required`, and the block reads `env.regs[15]`. This is the attribution that WP27's test-only X-REHY run measured: C' 999 hits, O6 ring equal.

Unmatched variants (logger LOG_REG without spin, FP `arm_log_fp`, a `movabs` form, a different allocation) keep today's fail-closed behaviour. No admitted configuration uses them.

### A.8 Tests and mutants added with the change

**Unit tests (matcher).**
- Positive: the four real snapshots give 238/238 matched, one triple, and spin edges equal to blocks.
- Negative, all `NoMatch`, each with its rule: every single-byte flip outside the holes; a hole retargeted to env, to the arena, or to an unbound address; the `je` retargeted or turned into `rel32`; a block not at `insn0_start`; an extra site inside the block; a second branch into the block; two different triples in one snapshot; the logger form (no spin); a `movabs` form.

**hostsym synthetic suite (V18 self-test).** WP19's `log_reg_delete` is kept. These are added:

| id | mutation | expected rejection |
|---|---|---|
| L1 | nop the ring store | `edit_inside_log_block` and a V12 `host_log` mismatch |
| L2 | nop the index store | same rule |
| L3 | `add r12d,4` → `add r12d,8` | same rule |
| L4 | nop the spin `je` | same rule |
| L5 | retarget the `je` to S+7 | `NoMatch`, then `backward_branch`, inconclusive |
| L6 | repoint BUF's lea into env | `host_abs_aliases_env` with `env_abs`; `edit_inside_log_block` without |
| L7 | `regs[15]` load disp `0x3c` → `0x38` | same rule |
| L8 | a D1 store hoisted into the block | same rule |

All must be rejected. No equivalent mutants are allowed.

**Real B4 mutants on F-B4-10 and F-B3-23 units.** Mutants of every existing kind are now generated on coverage units, which are admitted from now on. L1, L2 and L7 are also real-instance kinds (`log_reg_*`). The dynamic detector for L1, L2 and L7 is O6 (ring differs), as for `log_reg_delete` today. They are reported and not gating; §9.3's gating kinds are unchanged.

---

## B. `flag_store_before_helper`: why it was never detected, and a detector

### B.1 Why no current fixture observes the stale flags

**The deletion is observable in admitted configurations.** `v7m_mrs` and `v7m_msr` are `DEF_HELPER_2/3` with no flags (`target/arm/tcg/helper.h:73-74`), so TCG flag class 0. TCG stores every dirty global, NZCV included, before such a call and reloads globals from env after it. A deleted NZCV store therefore leaves env stale, and the stale value is seen by:

- (i) the helper itself, when it reads NZCV: `mrs rX, APSR`, where `HELPER(v7m_mrs)` case 0..7 calls `v7m_mrs_xpsr`, which calls `xpsr_read(env)` (`m_helper.c:2429-2436`);
- (ii) any flag reader after the call in the same TB (it reloads from env);
- (iii) the successor TB, which reads env;
- (iv) exception stacking (xPSR in the frame).

The mutant is correctly specified, so a reclassification would be wrong.

**Why the 18 instances were never detected.** Every instance in the gate fixture set stores flags that are **dead at the guest level**. They come from Thumb-1 16-bit flag-setting encodings whose NZCV the program overwrites before any reader. The sites were checked against the unit dumps and `fixtures/b3/b3.lst`:

- **F-B3-21, TB at 0xabc** (unit `tb:66d8afac…#0`): `adds r5,#1`, then `cpsid i` (`v7m_msr` PRIMASK, flags-0), then the TB ends. The successor is `mrs r1,PRIMASK; add r9,r1; cpsie i; adds r4,#1; cmp r4,r7; bcc`, and `adds r4,#1` redefines NZCV before the `bcc`. No IRQ is pending at `cpsie i`, so stacking never sees the stale xPSR.
- **F-B3-5:** `adds r0,#1`, then the `randstate` plugin callback at `f_b3_5_mod`, then `bx lr`. Next comes `vc_b3_5`: `cbz` (no flags), then `movs`/`adds` (which set NZCV), then `cmp`.
- **main's start-up `v7m_mrs` instances:** same class. Flags are left by compiled C, and C never keeps NZCV live across the intrinsic.
- **F-B4-1** (§9.5's named detector): its printreg TB begins after the `bne`, so no flag is dirty there and the kind has no instance (as WP29 found).

### B.2 New fixture phase F-B4-11 `b4-flags` (a WP24 follow-up)

**Goal.** Within one TB, a flag-setting instruction, then a flags-0 call, then an NZCV reader, with deterministic stale values that always differ from the true value for some iteration. The design has three sub-phases, one per observer class, and a **poison** TB before each:

```
for k in 0..7 (pair table (a_k, b_k)), for p in {0x00000000, 0xF8000000}:
  TB P:  msr   APSR_nzcvq, rP          @ v7m_msr writes NZCVQ := p; MSR ends the TB (gen_lookup_tb)
  TB A:  cmp   r0, r1                  @ NZCV := flags(a_k - b_k): NF/ZF/CF/VF dirty
         mrs   r2, APSR                @ v7m_mrs (flags 0) READS NZCV from env   [observer (i)]
         ubfx  r2, r2, #28, #4          @ no flag-setting encodings between cmp and the readers
         str   r2, [r9], #4            @ result array (logged after the loop)
         b     next                    @ ends TB A
  TB P again
  TB B:  cmp   r0, r1
         mrs   r3, PRIMASK             @ v7m_mrs (flags 0), does not read NZCV; TCG reloads after it
         mov.w r3, #0
         it eq ; orreq.w r3, r3, #8    @ readers after the helper                [observer (ii)]
         it mi ; orrmi.w r3, r3, #4
         it cs ; orrcs.w r3, r3, #2
         it vs ; orrvs.w r3, r3, #1
         str   r3, [r9], #4 ; b next
  TB P again
  TB C:  cmp   r0, r1
  b4_11_pp: nop                        @ printreg virtual here (plugin cb, RW_REGS = flags 0)
         (same four IT readers into r3) ; str ; b next
after the loop: for each stored word i: b3_res(0x10 + i, word)  (RES only after all readers:
RES's `movs r0,#idx` sets flags)
```

**Pair table** (covers each flag at both values): `(5,5) (3,5) (5,3) (0x80000000,1) (0x7fffffff,0xffffffff) (0,0) (0,1) (0xffffffff,0)`.

**Oracle.** The correct output is **independent of the poison**: every word equals NZCV(a_k − b_k). `expected.py` derives it in Python from the table as named invariants, and the icount `.norm` is frozen as usual. noicount has the same invariants with no timer.

**Effect of a deletion.** A deleted store of flag f leaves env.f equal to the poison bit. Exactly the iterations whose poison bit differs from the true f differ, and for every f and every pair one of the two poisons differs. So **every** CF/VF/NF/ZF instance in A, B or C is detected, and the divergent log records are predicted exactly.

**Build without touching existing images (hard requirement).** FastDyn fingerprints firmware **size and mtime**, and every existing base (62 B3, 18 + 11 B4, compute) depends on `b3.elf`, `b3_O0.elf`, `b3_Os.elf` and `b3_16d.elf`. A `make` after editing `Makefile`, `phases.s` or `b3.c` rebuilds all of them and invalidates every base. Instead:

- New sources: `fixtures/b3/b4_11.s` (the kernel, defining global `phase_b4_1`, with its own copies of the FUNC/LABEL/RES macros and a `.data` result array) and `fixtures/b3/Makefile.b411`. Neither `Makefile`, `phases.s` nor `b3.c` is edited.
- `phases_nob41.o` is made with `objcopy --localize-symbol=phase_b4_1 phases.o`, and the image is linked from `startup.o phases_nob41.o b4_11.o b3_O1.o` into **`b3_b411.elf`** (`.lst/.map/.nm` as in `LINK`). The phase table in `b3_O1.o` resolves `phase_b4_1` to the new kernel. This is the same "variant image" precedent as `b3_16d.elf`.
- Config `b4-11` in `b3conf.py`: `Config("b4-11", "F-B4-11", 0x201, elf="b3_b411.elf", ...)`, with a printreg virtual on `b4_11_pp`. Then run `gen_tomls.py`. It writes only the new `b3-b4-11.toml.in` and `inputs/b4-11.bin`; this must be checked, and the other templates must be byte-equal.
- **Checks, stated in the report:** `stat` and sha256 of the four existing ELFs and of all `*.o` are equal before and after. `ELF.sha256` and `SOURCES.sha256` gain the new files only. `b3.lst` shows no 16-bit flag-setting encoding between each `cmp` and its readers. A unit test greps `b3_b411.lst` for this.
- Qualify `b4-11` with the existing `qualify.sh` on the gate binary in both modes (nocache ×2, cold, warm ×2, fresh run dirs). This adds rows to the fixture qualification evidence.

### B.3 How the dynamic detection is confirmed

1. **Base.** `wp20_fixture_snapshots.py --configs b4-11 --profile` (icount and noicount) into the close-out run dir, on the gate binary.
2. **Instances (static, no QEMU).**
   - `b4_mutants.find_mutants(per_kind=∞)` on the b4-11 base must list `flag_store_before_helper` instances in the TB-A, TB-B and TB-C units, at least one per flag per sub-phase (12 expected: 4 flags × 3). The note must show `call:global v7m_mrs` for A and B and `call:plugin` for C.
   - Each instance's unit has profile entries of at least 16.
   - If fewer instances appear, for example because TCG emits a store only for flags it computes, the report lists exactly which flags are covered and why. It is never silently reduced.
3. **Static rejection.** Every instance goes through `package_b4`. It must be rejected (V12, S4 observation). This is added to the T-B4-2 counts.
4. **O4, every instance.**
   - Run each instance as a negative control (`scopt.run.negative`) against the b4-11 warm base, on the gate binary, in icount (the gate mode) and also noicount (reported).
   - Detection means O1 differs. The driver also checks the **predicted** divergence: the records `0x10+i` for exactly the (k, p) iterations where poison bit f ≠ NZCV_f(a_k − b_k), in the sub-phase of the instance. A difference anywhere else is reported as an anomaly and is not counted as the specific detection.
   - `b4_mutants.DETECTORS["flag_store_before_helper"] = "F-B4-11"`. In `wp29_gate.py` the O4 fixture list for the kind becomes `["b4-11", …]` and `CONFIGS` gains `b4-11`.
5. **Real-pass row (reported).** The real B4 artifact on b4-11 in both modes must keep these stores (`observed_at_sync_point`) and pass O1/O2a/O2b. This is reported next to item 2. It is not part of the §11 item-2 fixture list.
6. **Item 1** then passes on "`flag_store_before_helper` detected by F-B4-11". **Disclosure:** §9.5 named F-B4-1 (printreg) as the detector. F-B4-1 has no instance of the kind on the gate base, so the detector is a new fixture. The criterion "rejected statically and detected dynamically" is unchanged.

If a reviewer finds an admitted configuration in which F-B4-11 cannot be built, the fallback is **not** a silent pass. The item stays FAIL with a disclosed reclassification proposal. B.1 shows the deletion is observable, so such a reclassification is not expected.

---

## C. Implementation and re-gate plan

### C.1 Work packages (branch per WP from `main`; review, then merge `--no-ff`; trailers as required)

| WP | content | acceptance |
|---|---|---|
| CO-1 | `scopt/decode/logreg.py` (A.2); HostOps integration (A.3); census counters | unit tests A.8 (positive 238/238 on the four real snapshots, all negative cases); HostOps digest of every non-coverage unit of all gate snapshots (wp28 fx, wp29 fx, fx-b3, compute) unchanged |
| CO-2 | hcfg spin edge (full view only), `spin_edges`, `logreg_block_malformed`; syncdead `edit_inside_log_block`; hconst no fact kill for `log` stores | prototype numbers reproduced: F-B4-10 syncdead ok 62/62, D1/D2/D3 17/45/129 per mode; F-B3-23 57/57; T-B4-1 compute census unchanged (469/1665/5602, M0 D1 = 0) |
| CO-3 | hostsym Program/paths/equiv (A.5), `abs.logreg_ring`, V15 placement, B3 pass placement | V18 self-test incl. L1-L8 (100 % rejected); existing self-test and mutant suites unchanged; adversarial review of the soundness argument A.6 |
| CO-4 | mutants: L-kinds as real instances on coverage units; `b4_mutants` DETECTORS/O4 lists | static suite counts recorded (they will grow; nothing previously rejected may become accepted) |
| CO-5 | fixture F-B4-11 (B.2), qualification on the gate binary | B.2 checks, existing images untouched (stat + sha256) |
| CO-6 | drivers: `wp29_gate.py` (`b4-11` config, `b4-10` step reports matched blocks, O4 predicted-divergence check); `wp28_steps.py` new step `coverage-neg` (C.2); both report generators append a carried section file (C.4) | unit tests of the drivers/verdicts on synthetic dirs (existing pattern) |
| CO-7 | static rehearsal (no QEMU): b3 pipeline + publish on the F-B3-23 bases, b4 package on F-B4-10 | F-B3-23 artifact v6 with ≥ 1 SPEC link to Vc and V12/V13/V14/V19 pass; F-B4-10 published with D1 > 0. **If specialize-returns still refuses, the new reason is reported and the gate stays FAIL; nothing is bypassed.** |
| CO-8 | full pytest on merged `main` (precedent: 1333 passed) | rc 0 |

Design implementation note to append to `B3-B4-design.md`: "Close-out (LOG_REG / HostLog, F-B4-11)". It covers the model extension (A.2-A.6), the new reason code `edit_inside_log_block`, the new class `abs.logreg_ring` and the new fixture.

### C.2 Re-gate: run directories and steps

Use **fresh** run dirs: the maintainer's evaluation data (not in the repository). The WP28/WP29 evidence is kept, not overwritten.

- Bases are not re-collected. The snapshot capture does not depend on scopt code, and the importer is unchanged, so snapshot ids are unchanged.
- The new dirs reference the WP28/WP29 base `snap.json` files: copy `fx/*/*/snap.json`, `config.toml` and the profile references, plus the `compute` base info. The snapshot stores are used read-only.
- Take a sha256 manifest of the maintainer's evaluation data (not in the repository) and both compute bases before and after the re-gate. They must be equal.
- Evidence files of steps that are not re-run are **copied** (not symlinked) into the new dir, and the report marks each one "carried from WP28/WP29 (sha256 …)".
- `gate` runs first in each driver to re-check the exe sha256 and the tag.

**B3 (`tests/qemu/wp28_gate.py --out <evaluation dir>/b3 --steps …`):**

| step | re-run? | why / expected |
|---|---|---|
| `gate` | yes | binary identity |
| `bases` | **no** | snapshots unchanged |
| `artifacts` | **yes, all 62** | static re-validation with the new code. Expected: `cache.sc` sha256 **equal** to WP28's published artifact for the 60 non-coverage artifacts (artifact ids may differ: `validation.json` gains the new self-test id and rows, as in the WP25-int recheck precedent). For `23` (both modes): v5 identity becomes v6 with a SPEC link to `vc_b3_1`-equivalent. **Any other `cache.sc` change is investigated, and that config's dynamic rows are re-run.** |
| `dynamic` | `--configs 23` (+ any config whose bytes changed) | others carried by sha256 equality |
| `aa`, `stale`, `gdbdiag`, `alias`, `smc17`, `ucond`, `t_b3_6` | `t_b3_6` yes (static, cheap); others no (carried; their artifacts' bytes are proven equal by `artifacts`) | |
| `o4` | no (every §9.3 B3 kind already detected; bytes equal) | |
| **`coverage-neg` (new)** | yes | negative control on the new F-B3-23 artifact: C''s ring store nopped (test-only, `package_negative`, V12/V15 bypassed). O6 **must** detect it (ring index lower by C' hits). This is a stricter, added check (O6 non-vacuity on a variant), disclosed. |
| `mutants` | **yes** | `scopt hostsym mutants` / `link-mutants` / manager-link guard mutants now include F-B3-23's units and link: counts change (3034 / 16881 / 5818 grow); still 100 % rejected except the known equivalent `drop_g_cond` on 3b |
| `roundtrip` | yes | T-B3-28 must hold 62/62 including 23's new SPEC link (`multi_tb` row) |
| `coverage` | **yes** (blocking 4a) | criterion unchanged: O6 equal warm **and** C' hits > 0 (gdb `-nx`) in both modes |
| `compute` | **static part yes** | the 4 compute arms (b3, b3-chain, b3-spec, b3-static) re-packaged. Expected `cache.sc` byte-identical (compute has 0 HOST_ABS ops, so no block). V10 and `compute_hits` only if bytes differ. |
| timing | **no**, unless a compute arm's `cache.sc` sha256 differs from the timed one (the maintainer's evaluation data (not in the repository) keeps them) | then re-run `wp28_timing.sh` for that arm under `flock the workspace timing lock`, `taskset` 4 cores, alone |
| `verdict` | yes | |

**B4 (`tests/qemu/wp29_gate.py --out <evaluation dir>/b4 …`):**

| step | re-run? | why / expected |
|---|---|---|
| `gate` | yes | |
| `compute-base` | no | same snapshot (B5 `prep` already proved WP28 = WP29 compute base) |
| `package` | **yes, all configs incl. `b4-11`** | `cache.sc` equal to WP29's for b4-1…b4-9 (both modes). **b4-10 changes**: 0 deletions becomes the prediction D1 17 / D2 45 / D3 129 per mode. b4-11 is new. |
| `package-compute`, `compute-host-abs`, `package-sfb-b4` | yes (static) | expected byte-identical; `compute-host-abs` still 0 ops |
| `dynamic` | `--only b4-10,b4-11` (+ any config whose bytes changed) | b4-10 gated O1/O2a/O2b/O5 + **O6** (item 4) |
| `deletions` | yes for b4-10 and b4-11 | executed D1 > 0 under gdb (non-vacuity, same rule as item 2) |
| `b4-10` | yes | diagnosis must now show 62 matched, 0 pinned, 0 cyclic; the O6 warm-vs-warm control again |
| `mutants` | **yes** | totals change (640 grows: coverage units and b4-11 are now admitted; L-kinds); 100 % static rejection required |
| `o4-extra`, `o4-cover` | yes for `flag_store_before_helper` (B.3 step 4: every instance) and the L-kinds (reported) | others carried |
| `host-abs` | no (F-B4-7 bytes equal) | |
| `compute` (V10) | only if compute bytes differ | |
| timing | **no**, unless a timed compute arm's `cache.sc` sha256 differs from WP29's | then re-run `wp29_timing.sh` under the lock for all arms (paired design) |
| `verdict` | yes | items 1 and 4 expected to flip; items 2, 3, 5 must stay pass (2 and 3 recomputed from the copied plus re-run evidence) |

**Also re-run:**
- pytest `t-b4-static` (T-B4-1/2/2b).
- The `scopt hostsym` self-test (`selftest.json`), whose id enters every artifact's `validation.json`.
- Full pytest (CO-8).

### C.3 Which verdicts and artifacts may change, and the B5 freeze

- **Must change:**
  - B3: 4a (expected FAIL to pass); the F-B3-23 artifact rows; mutation-suite counts; T-B3-28 count text.
  - B4: item 4 (FAIL to pass), item 1 (FAIL to pass), package/dynamic/deletions rows for b4-10, the new b4-11 rows, the mutant totals.
- **Must not change:** every other `cache.sc` (62 B3 minus 2, 16 B4 fixture rows, 4 + 6 compute arms), the T-B4-1 census, items 2/3/5, every carried dynamic verdict. A difference in any of these is a finding that is investigated and reported, never absorbed.
- **Artifact ids** may change everywhere. This is because `validation.json` carries the self-test id and new V-row details. Identity of behaviour is shown by `cache.sc` sha256, as in WP25-int.
- **B5 frozen campaign** (`campaigns/b5-freertos.toml`, sha256 `9b937039…`):
  - `b5_campaign.py check` compares the scopt git tree at HEAD, so it **will fail** after the merge. This is expected; nothing in B5 has been timed.
  - Re-freeze with `b5_campaign.py freeze` on the merged `main`, then run `check`.
  - Show that every deploy arm's frozen `cache.sc` sha256 is equal to the old freeze's. The compute workload has no coverage block.
  - Record the old and new freeze sha256 in the maintainer's evaluation report `B5-plan.md` (not in the repository) as a disclosed re-freeze.
  - If any arm's bytes differ, that arm is re-frozen with the new bytes and the B3/B4 timing rows for it are re-run (C.2).
- **Timing:** none is re-run unless a timed artifact's `cache.sc` bytes change, as stated in the C.2 tables.

### C.4 Reports

- Regenerate with `python tests/qemu/wp28_report.py --runs <evaluation dir>/b3 --out B3-gate.md` (the maintainer's evaluation report, not in the repository) and `python tests/qemu/wp29_report.py` pointed at the maintainer's evaluation data (not in the repository). The current reports are archived first as the maintainer's evaluation report `B3-gate.wp28.md` (not in the repository) and the maintainer's evaluation report `B4-gate.wp29.md` (not in the repository).
- **Carry §6.** Before regenerating, extract B3-gate.md "## 6. Audit corrections" (lines 265-285 today) verbatim into the maintainer's evaluation report `B3-gate.audit6.md` (not in the repository). CO-6 makes `wp28_report.py` append it unchanged (`--append-section`). Check that the regenerated file contains it byte-for-byte. A regenerated report without §6 is invalid.
- **New hand-checked section in both reports: "Close-out disclosures".** It covers:
  1. The validator model extension: the `HostLog` abstraction, and the backward-branch exemption only for an exact match (A.2-A.6), with unmatched code fail-closed as before.
  2. The new reason `edit_inside_log_block` and the class `abs.logreg_ring`.
  3. B4 item 1's detector fixture changed from F-B4-1 (§9.5) to the new F-B4-11, with the B.1 analysis of why the old instances are undetectable (guest-dead flags).
  4. The added `coverage-neg` O6 control (stricter).
  5. Which evidence is carried versus re-run, with sha256 equality of the carried artifacts.
  6. The B5 re-freeze.
  7. "No gate criterion of design §11 was changed."
- Update the Known gaps sections: F-B3-23 and F-B4-10 removed once they pass; `flag_store_before_helper` removed; unmatched LOG_REG variants (logger, FP, `movabs`) added as fail-closed and not exercised.

### C.5 Order of execution

CO-1 → CO-2 → CO-3 → CO-4 (each reviewed) → CO-7 (static rehearsal; stop and report if F-B3-23 or F-B4-10 is still vacuous) → CO-5 (fixture, in parallel with CO-1..4, since it is independent) → CO-6 → CO-8 → merge → C.2 B4 run, then B3 run (QEMU-using steps serialized, never during a timing job) → verdicts → C.4 reports → C.3 B5 re-freeze → audit.
