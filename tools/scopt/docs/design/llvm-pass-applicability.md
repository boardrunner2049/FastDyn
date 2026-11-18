# LLVM / BOLT pass applicability to the supercache optimizer (`scopt`)

Status: final survey, 2026-11-17. Author: Student 2 (survey synthesis).
Scope: every LLVM 18 IR pass and analysis, every target-independent and X86 CodeGen pass, every BOLT pass, the
optimization machinery that is not a registered pass (SelectionDAG combines, MC relaxation/padding, X86InstrInfo hooks,
BOLT layout algorithms and dataflow analyses, Propeller), notable passes added on LLVM `main`, and a cross-cutting set of
speculation / deoptimization / PGO mechanisms. Each was reviewed against `scopt`, the offline pass-oriented optimizer for
FastDyn's QEMU supercache (TB host code for an ARM Cortex-M3 Thumb-2 guest on an x86-64 host).

Inputs (the maintainer's notes are not in the repository):
- per-category assessments (9 files) and skeptic reviews (9 files) in the maintainer's notes;
- the pass inventory in the maintainer's notes (README, 8 category files, scripts, raw tool output);
- the TCG baseline in the maintainer's notes (cited as **TB §n**);
- the B3/B4 maps in the maintainer's notes (REP = return-edge profile, SST = state-sync traffic, AMR = ARM-M returns,
  LRV = loader/runtime v6, SCO = semantic capture options);
- the project specification (not in the repository) (cited as **spec §n**), `B1-design.md`, the maintainer's evaluation report `B2-gate.md` (not in the repository);
- the B3/B4 candidate designs and their feasibility review (not in the repository).
  `B3-B4-design.md` and `IMPLEMENTATION-PLAN-B3B4.md` **did not exist** when this survey was written;
  section 4 therefore reconciles against the review's winning design (design A plus grafts G1-G9 and must-fixes M1-M12)
  and should be re-run against the final B3/B4 design once it lands.

Labels used throughout: **MODEL** = arithmetic over exact software profile counts and static instruction counts (not
time); **[M]** = microbenchmark on this host; **[V]** = verified in source or data by an assessor or skeptic;
**[I]** = inferred; **[G]** = guess; **[U]** = uncertain. All dynamic numbers are from the FreeRTOS MPS2 `compute`
fixture (cortex-m3, `CF_USE_ICOUNT`, `CF_PCREL`), steady phase, 9 train runs: 826.1 M TB entries (91.8 M per run),
128.2 M `helper_lookup_tb_ptr` hits (14.25 M per run, 15.5 % of TB entries), BENCH median ≈ 987 ms per run, ≈ 10.7 ns
per TB entry. No hardware counters were used (perf is unavailable: `perf_event_paranoid = 4`).

---

## 0. Executive summary

1. **The cost LLVM/BOLT ideas can actually remove here is dominated by two things TCG leaves on the table: the lookup
   helper on every indirect or cross-page transfer, and a store-forwarding stall that TCG's own icount entry check
   creates on every TB transition.** B2 showed that layout alone buys nothing measurable (held-out 0.9988
   [0.993, 1.005]); only *executed work removed* can pay.
2. **The top transfer is ICP/devirtualization/veneer-elimination**, i.e. guarded or unguarded direct links that replace
   `call helper_lookup_tb_ptr; jmp *%rax`. Each nanosecond of the (still unmeasured) lookup cost `c` is 1.44 % of run
   time; static off-page `bl`/`b` (48 % of lookups), returns (34 %) and one `tbb` (18 %) are the populations.
3. **The single largest measured micro-effect is `x86-avoid-SFB`'s analogue**: the 16-bit `icount_decr.u16.low` store
   at TB entry followed by the next TB's 32-bit load cannot be store-forwarded. On a replica of the hottest TB it costs
   4.5 → 2.2 ns per iteration [M], and it hides every B4 dead-store gain (2.2 → 1.47 ns only after the fix). It is a
   baseline-TCG inefficiency and must be reported as its own arm, not as a caching or B4 effect.
4. **B4 (dead env stores, sinking, compensation) is real but small in instruction share** (intra-TB D1+D2+D3 ≈ 7 %,
   cross-TB X1 5.4 %, X2 9.5 % of modelled fast-path instructions) and concentrated in one CRC loop TB (0x1d5a). Its
   time value is unproven and gated on one hand-patched experiment.
5. **Most of LLVM does not apply**: 449 of 846 inventory rows are not applicable and 90 are concept-only. The deep reason
   is the same everywhere: guest memory accesses are observable events, every TB entry is a contract-protected interrupt
   and icount point, no TB is "internal" (general entries must stay public), and TCG already does the in-EBB scalar work.
   Inlining, GVN, SROA/mem2reg, LICM and loop vectorization survive only in narrow, region-scoped forms.

The shortlist (section 3) and the plan changes (section 4.3) follow from this.

---

## 1. Method and coverage

### 1.1 Versions and sources

| item | value |
|---|---|
| Local tools | `/usr/bin/opt-18`, `/usr/bin/llc-18`: Ubuntu LLVM 18.1.3 (`llvm-18 1:18.1.3-1ubuntu1`), default target x86_64-pc-linux-gnu |
| LLVM release sources | `llvm/llvm-project` branch `release/18.x`, commit `3b5b5c1ec4a3095ab096dd780e84d7ab81f3d7ff` (18.1.8 head); mirrored registries byte-identical to that commit; `PassRegistry.def@18` names = `opt-18 --print-passes` names (scripted) |
| LLVM `main` | fetched 2026-11-16; registries byte-identical to `59551e9f844e27b3e27ff5526e0c922a4ff38426` (2026-11-17 04:06Z) |
| BOLT | `bolt/lib/Rewrite/BinaryPassManager.cpp` 18.x and main (52 registration points in `runAllPasses`), `bolt/lib/Passes/*`, profile and runtime directories |
| QEMU | the WP-QINT QEMU tree, HEAD `1258c6a3f2` (QEMU 10.0.50, FastDyn fork) |
| Pass lists | taken from tools and source registries only (`opt-18 --print-passes`, `-print-pipeline-passes` for 11 pipelines, `llc-18 -debug-pass=Structure` O0-O3, `MachinePassRegistry.def`, `TargetPassConfig.cpp`, all `INITIALIZE_PASS*` in `lib/CodeGen` and `lib/Target/X86`, `X86.h`, `X86TargetMachine.cpp`, BOLT `BinaryPassManager.cpp`); descriptions from source header comments where the assessor was unsure |

### 1.2 Rows examined per category

| category | rows | registered passes (18) | non-pass components | main-only rows | assessor file | skeptic file |
|---|---:|---:|---:|---:|---|---|
| ir-ipo | 40 | 38 | 0 | 2 | `cat-ir-ipo.md` | `challenge-ir-ipo.md` |
| ir-scalar | 50 | 47 | 0 | 3 | `cat-ir-scalar.md` | `challenge-ir-scalar.md` |
| ir-loop | 34 | 31 | 0 | 3 | `cat-ir-loop.md` | `challenge-ir-loop.md` |
| ir-instr-lowering-utility | 216 | 170 | 0 | 46 | `cat-ir-instr-lowering-utility.md` | `challenge-ir-instr-lowering-utility.md` |
| ir-analyses | 95 | 54 | 26 | 15 | `cat-ir-analyses.md` | `challenge-ir-analyses.md` |
| codegen-generic | 228 | 159 | 28 | 41 | `cat-codegen-generic.md` | `challenge-codegen-generic.md` |
| codegen-x86 | 79 | 42 | 28 | 9 | `cat-codegen-x86.md` | `challenge-codegen-x86.md` |
| bolt | 104 | 50 | 45 | 9 | `cat-bolt.md` | `challenge-bolt.md` |
| **inventory total** | **846** | **591** | **127** | **128** | | |
| speculation-deopt-pgo (cross-cutting, no inventory file) | 61 mechanisms | — | — | — | `cat-speculation-deopt-pgo.md` | `challenge-speculation-deopt-pgo.md` |

No category's assessor failed; every category has an assessment and a skeptic review. The 846 inventory rows contain
840 distinct names (6 names are both a pass and an analysis or exist in two subsystems: `no-op-module/-cgscc/-function/-loop`,
`verify`, `kcfi`; each has one row per meaning).

### 1.3 Verdict counts (assessor verdicts, normalized; skeptic changes in 1.4)

Normalization: `now` = applicable now at L1 (host binary) or on the TB graph, including analyses, validators and
tooling, and including items that already exist in scopt B1/B2; `B3`, `B4` = applicable in that milestone; `IR` =
applicable only with captured (L2) or regenerated (L3) TCG IR; `done` = already done by TCG/QEMU; `concept` = the idea
transfers, no pass does; `n/a` = not applicable.

| category | now | B3 | B4 | IR | done | concept | n/a | total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ir-ipo | 3 | 5 | 3 | 5 | 1 | 7 | 16 | 40 |
| ir-scalar | 2 | 7 | 4 | 7 | 5 | 7 | 18 | 50 |
| ir-loop | 1 | 4 | 2 | 4 | 1 | 5 | 17 | 34 |
| ir-instr-lowering-utility | 22 | 17 | 9 | 2 | 7 | 6 | 153 | 216 |
| ir-analyses | 11 | 17 | 10 | 1 | 3 | 16 | 37 | 95 |
| codegen-generic | 21 | 6 | 10 | 9 | 20 | 28 | 134 | 228 |
| codegen-x86 | 8 | 0 | 3 | 4 | 9 | 6 | 49 | 79 |
| bolt | 40 | 8 | 7 | 2 | 7 | 15 | 25 | 104 |
| **total (inventory)** | **108** | **64** | **48** | **34** | **53** | **90** | **449** | **846** |
| speculation-deopt-pgo (mechanisms) | 12 | 11 | 5 | 4 | 2 | 17 | 10 | 61 |

Read with care: 37 of the ir-instr-lowering-utility "now"/B3/B4 rows are validator or tooling analogues (marked [V]
in the assessment: verifiers, debugify, crash/mutation tests, instcount), and 13 of BOLT's 40 "now" rows are already implemented by scopt B1/B2 (Pettis-Hansen
clustering, hot/cold partition, profile join, validators). The number of genuinely new *optimizing* transformations
that survive review is roughly two dozen (section 3), and only about six of them have a modelled benefit above 1 %.

### 1.4 Skeptic outcome over the inventory rows

157 of the 846 rows carry an explicit skeptic disposition (the shortlisted candidates, the other applicable rows the
skeptics chose to challenge, and the n/a/done/concept rows they overturned): **keep 30, keep-with-changes 54,
downgrade 54, reject 5, merged into another pass 3, missed (n/a/done/concept → applies) 10, reclassified to n/a 1**.
The remaining rows were re-checked in bulk and the assessor verdict stood.
Every skeptic also re-ran the coverage comparison for its category (all reported no gaps). The appendix lists the
disposition per row.

### 1.5 Coverage check performed for this survey

Script (a throwaway script, not kept; it only reads the maintainer's notes): for each `inventory/<cat>.txt`, take column 1 and
search the numbered rows (`| n | ... |`) of `cat-<cat>.md` for an exact backticked match, a match after stripping the
`algo:`/`component:`/... prefix, or a prefix match for `|`-grouped BOLT option rows.

```
ir-ipo                      40 inventory names, 40 table rows, missing 0
ir-scalar                   50 / 50, missing 0
ir-loop                     34 / 34, missing 0
ir-instr-lowering-utility  216 / 216, missing 0
ir-analyses                 95 / 95, missing 0
codegen-generic            228 / 228, missing 0
codegen-x86                 79 / 79, missing 0
bolt                       104 / 104, missing 0   (13 `algo:` rows matched after prefix stripping)
```

Additional checks:
- `opt-18 --print-passes` was re-run today; its output is byte-identical to `inventory/raw/opt18-print-passes.txt`, and
  `inventory/scripts/verify_src1.py` on the fresh output reports 383 entries placed, `MISSING: EMPTY`, `AMBIGUOUS: NONE`.
- Every pass argument of `llc-18 -O3` and `-O0` (`raw/llc18-O{0,3}-args.txt`) is an inventory name except five legacy-PM
  analysis wrappers (`assumption-cache-tracker`, `lazy-block-freq`, `lazy-branch-prob`, `profile-summary-info`, `tti`),
  each of which is listed in column 2 of its new-PM analysis row (`assumptions`, `block-freq`, `branch-prob`,
  `profile-summary`, `targetir`).
- The normalized verdict tallies of section 1.3 equal each assessor's own summary table.

**Result: no pass in the inventory lacks an assessment; nothing had to be assessed here from scratch.** The only
cosmetic gap found by a skeptic (`irce` named among the speculation category's sources without a row) is assessed in
`cat-ir-loop.md`.

---

## 2. Concept mapping and what TCG already does

### 2.1 LLVM nouns in this runtime

| LLVM concept | Analogue here | Consequence |
|---|---|---|
| Instruction | a host x86 instruction (L1) or a TCG op (L2/L3) | TB bodies are tiny: mean 5.0 guest insns, 41 modelled host insns per TB entry |
| Basic block | at graph level one **TB** (≤ 1 guest basic block, ≤ 512 insns, 21 % single-insn); inside a TB an extended basic block between TCG labels (IT skips, slow-path `jne`s) | block-local passes have almost nothing left after `tcg_optimize`; 0 backward jumps inside any TB |
| Function | **not** the TB. Either the guest function (TBs in an ELF `STT_FUNC` range, never a compilation unit) or a scopt **region / version**: a bounded set of TBs with a guarded specialized entry, side exits to general TBs, and every member keeping its entry check | function-scope passes become meaningful only on regions, which need `.sc` v6 |
| Call | guest `bl`: a direct branch; `goto_tb` only on the same 1 KiB page, otherwise a static-target lookup (93 % of `bl` TBs are off-page) | "inline a call" = link the call edge directly and specialize along it; no frame exists |
| Return | `bx lr` / `pop {..,pc}` / `ldr pc`: **always** `call helper_lookup_tb_ptr; jmp *%rax`; Handler-mode returns go to the main loop via `exit_tb(NULL,0)` | returns are indirect calls; call/return pairing is free (continuation = `bl` site + 4/2 for 100 % of returns) |
| Indirect call / switch | any `goto_ptr` exit: returns, off-page static branches (only because of the page rule), `tbb`/`tbh`, `bx reg` | ICP, WPD, jump-table-to-switch and BOLT veneer elimination all land here |
| Loop | a TB-graph cycle, e.g. the self-chained CRC bit loop TB 0x1d5a (18.4 % of entries); every iteration crosses an entry check | LICM/unrolling/versioning need cyclic regions; trip-batching is contract-blocked |
| Stack slot / alloca | an **env slot**: a `CPUARMState` field at a fixed `%rbp` offset (r0-r15, NF/ZF/CF/VF, `thumb`, `condexec`) plus executor bookkeeping at negative offsets (`icount_decr` −0x8, `can_do_io` −0x4, `plugin_mem_value_low` −0x18) | SROA/mem2reg/DSE/GVN apply to env slots only. The guest stack is guest RAM: every access is an event and is never promoted |
| Global variable | env fields read by QEMU C code (`v7m_push_stack`, gdbstub, the lookup helper) | "address-taken" = read by a helper, a fault path or the main loop (observer points S1-S6, SST §5) |
| Function attributes | per-helper `TCG_CALL_*` flags (`NO_RWG`, `NO_WG_SE`, ...) | inferattrs/function-attrs = a refined helper-effects table and per-guest-function summaries |
| Inlining | region formation / continuation versions: clone + guarded direct link; never removes an entry check, guest access or callback | most inlining value is the removed lookup, not code merging |
| Cold code | out-of-line softmmu slow paths (21 % of code bytes, already at each TB's end); cold TBs (B2 sections); unseen entries (must keep working, spec C7) | hot/cold splitting is done at TB granularity; intra-TB splitting needs v6 |
| Safepoint / poll | the per-TB entry check (icount decrement + exit request) | `place-safepoints` is "done" at every TB entry; thinning polls is forbidden (spec §3) |
| Patchpoint | the `goto_tb` slot (NOP-aligned `jmp rel32`, recorded offsets) | B3 adds loader-registered patch sites |
| Deoptimization | fall back to the general TB (`tc.ptr`, before any version effect) or to the original lookup tail | guards must run before the version's icount-writing entry check |
| Stack maps / debug info | per-TB search data (`insn_start` rows: guest PC, condexec, syndrome) + `.scm` site metadata | it is fault-recovery semantics; extended, never stripped |
| Profile (`!prof`) | Profile v1: exact TB-entry and exit-slot counts per phase; **no intra-TB branch counts, no value profiles** | exact edges remove the need for inference; intra-TB bias is missing (section 3f) |
| Memory SSA / AA | ~20 env slots at fixed offsets + bookkeeping bytes; three disjoint classes: env, guest (softmmu, events), FastDyn `host_abs` | a per-slot bitvector dataflow is exact (SST reproduced TCG's sync invariant, M0 = 0) |
| Linker relocations | B2 NativeLinker fixups (rel32, pool, raddr LEAs, own-struct LEAs) | BOLT's rewriting infrastructure already exists in scopt for whole-TB moves |

IR levels: **L1** = rewrite captured x86-64 bytes (Python + capstone; Tier-0 `.scm` sites; no IR); **L2** = analyse
captured TCG IR (Tier-1 capture, reserved but not implemented: `QEMU_SC_CAPTURE_LEVEL=ops` aborts today); **L3** =
re-translate through TCG with injected facts (needs a translator mode inside the *production* binary, because
`runtime_signature` hashes the executable).

### 2.2 What TCG already does (TB §0, §11, §12)

Inside one EBB `tcg_optimize` does constant and copy propagation, known-zero/sign-bit tracking, single-op algebraic
identities, constant-branch folding and full-width env load forwarding; liveness removes dead ops and dead global
stores *between sync points*; the backend uses LEA/BMI/`test`/`bt`/`sbb` forms and puts softmmu slow paths out of line
at the TB end; helpers carry hand-written effect attributes; the runtime chains same-page `goto_tb` exits, caches
lookups in `tb_jmp_cache`, aligns TB starts to 64 B and `madvise`s the code buffer `HUGEPAGE`.

Absent (the 28 "not done" rows, TB §11): everything across TBs (N1-N4: facts, register allocation, flag/register
liveness, reloads), CSE/GVN even inside a TB (N5), TLB-check reuse (N6), env bookkeeping DSE (N8), loops (N9), helper
inlining (N10), call/return pairing and indirect-target promotion (N11), cross-page direct links (N12), superblocks
(N14), IT if-conversion (N15), PGO layout (N16), short `jcc`/alignment policy (N17), post-selection peepholes
(N18-N20), spill placement (N21), reassociation (N22), branch-implied facts (N23), PRE/sinking (N24), scheduling (N25),
vectorization (N26), profile use (N27), cross-TB sharing (N28). Of these, N7 (guest memory), N13 (entry check) and the
`insn_start` boundaries are **forbidden** to do naively by the contract.

Consequence for every verdict: "already done within a TB" is right for the scalar folding family *when the facts
originate in the same EBB*. Those passes matter again only when B3 injects facts at a TB entry, and then the plan is
to reuse TCG (L3) or a small whitelisted L1 folder, not to re-implement InstCombine.

---

## 3. The shortlist (after skeptic verdicts)

Conventions for every item: **from** = LLVM/BOLT pass(es); **scopt** = pass name@version and EditKind/capability
(EditKinds as in design A §3.5: `REDIRECT_EXIT`, `CLONE`, `ADD_ENTRY`, `ATTACH_FACT`, `PURE_REWRITE`, `COMPENSATE`,
`LAYOUT_ORDER`); **level**; **obligations** (proof/validation; each failure is a stable rejection reason);
**benefit** (MODEL unless marked; `c` = cost of one lookup hit, unmeasured; `c` = 5-10 ns is the plausible range
because the whole run averages 10.7 ns per TB entry and `c` = 20 ns would make lookups 29 % of all time);
**prereq**; **effort** S (≤ 1 week) / M (1-3 weeks) / L (> 3 weeks). Ranks inside a group weigh benefit × confidence
against effort and risk.

### 3.0 The decision experiment that orders everything (do first)

**WP0 value probe** (design A WP0, review must-fix M1), extended by this survey. A hand-built v5 copy of the compute
snapshot, timed with ≥ 10 interleaved pairs on the current binary (no QEMU change, no baseline re-collection), with one
arm per mechanism:
1. direct link on the hottest static off-page `bl` (0x2ae6) → measures `c` (B3-2);
2. guarded return exit 0x1d6a → 0x2aea → `c − c_guard` (B3-3);
3. `tbb` 0x2a36 → 0x2ad4 guarded (B3-4);
4. **new:** guarded link of one `msr basepri` `exit_tb(NULL,0)` exit → cost of a main-loop round trip (B3-5);
5. **new:** forwardable icount entry on all units (B4-1), alone;
6. the 4 dead D1 flag stores of 0x1d5a nop'ed, **with and without arm 5** (B4-2; without arm 5 the stall masks it [M]);
7. **new:** the same-size `cmov` patch of 0x1d5a's IT triangle (D-1).

Software-only bound for `c` in parallel: count instructions on the `helper_lookup_tb_ptr` hit path (static disassembly of
the QEMU binary, or one warm run under valgrind `lackey`/`callgrind`), and add the term to `dynostats@1` (F-1).
**Decision rule:** an arm below ~1 % (the B2 harness CI is about ±0.6 %) is not built further until a workload shows more.

### 3a. B2.1 layout refinements usable with today's v5 loader

B2's Pettis-Hansen clustering already reduced the compute hot span 429 → 105 KiB and hot 4 KiB pages 39 → 21 with no
measurable speedup; the 90 %-hot code is 29.7 KB in 112 TBs, which fits L1i. Between TBs nothing falls through, so the
ext-TSP fallthrough term is zero and only distance matters. Expect ≈ 0 from every item here on the current fixtures;
they are worth doing only as cheap controls and for larger firmware or fuzzing hot sets.

| rank | item | from | scopt | obligations / rejections | benefit | prereq | effort |
|---:|---|---|---|---|---|---|---|
| A-1 | **Diagnose the placement-sensitivity anomaly** (blinky ablation arms 5.5-6.6 % *faster* than warm and than full layout, 30/30 pairs, CI [1.046, 1.072]) | BOLT `reorder-functions=user/random`, `aligner`; Propeller order files | `order-file@1` (`LAYOUT_ORDER` from a user list) + `random-order@1` control; one-TB-move artifacts | none beyond B2 (V1-V10) | an *observed* ±5 % effect that the locality model cannot explain (BTB/DSB set aliasing or 4K aliasing [U]); until explained, any layout or B3 timing result can be confounded | B2 harness | S |
| A-2 | **CDSort / ext-TSP (fallthrough weight 0) as alternatives to Pettis-Hansen** | `algo:reorder-functions=cdsort`, `algo:reorder-blocks=ext-tsp`, `hfsort/hfsort+`, `CodeLayout.cpp` | `cluster-hot@2` with `objective ∈ {ph, cdsort, exttsp0}` | reject `no_locality_gain_in_model` before timing | ≈ 0 expected; report as ablation arms only | none | S |
| A-3 | **Alignment policy as a perturbation control** | `aligner`, `block-placement` alignment, `mc:x86-branch-alignment-jcc-erratum` (n/a on this Sapphire Rapids host [U]) | `align-policy@1`: TB start alignment 16/32/64/128 and seeded padding | keep goto_tb 4-alignment and pool 8-alignment (V-rules exist) | ≈ 0 as optimization; value is quantifying placement noise for A-1 and B5 | A-1 | S |
| A-4 | **Huge-page check of the arena** | BOLT `HugePage`/hugify (done-TCG: `region.c:804` madvise) | no pass: record `AnonHugePages` of the arena VMA and 2 MiB alignment of `code_address` in run provenance | — | ≈ 0 (21 hot 4 K pages are within iTLB reach) | none | S |

Not usable with v5 (moved to 3d): struct/code segregation (`static-data-splitter`, BOLT `reorder-data`: scopt's V1 check
requires `tc − tb == 192`, and whether the v5 loader accepts another distance is [U]), intra-TB hot/cold splitting (`machine-function-splitter`, `hotcoldsplit`,
`split-functions`, `cdsplit`: a v5 record has one host range), intra-TB exit swapping (`reorder-blocks`: needs the
re-encoder). Boot ordering by first dispatch (`instrorderfile`) is ≈ 0 (boot is 0.43 % of entries).

### 3b. B3 passes (cross-TB specialization)

Spec §6 defines B3 as return-edge facts, continuation versions and separate simplification passes; the static-link and
`tbb` items are FormRegions/loader work placed next to B3 because they share its mechanism and carry most of its value.

**B3-2 (rank 1). `link-offpage-direct@1`** (design A `chain-static@1`, design B `LinkCrossPage`)
- **from:** BOLT `veneer-elimination` (analogue), IPO `wholeprogramdevirt` variant (a), `pgo-icall-prom` on a singleton.
- **what:** a static off-page `bl`/`b`/`bcc`/page-end fall-through exit (PC constant at translation) gets a direct link to
  the target's general entry instead of the lookup. On v5 via the stock free-slot route (the main loop links any
  non-two-page target, cpu-exec.c:993-1006); on v6 via a loader-registered site.
- **capability:** `REDIRECT_EXIT` (link STOCK or LOADER); L1 + loader.
- **obligations:** (1) **guard `regs[15] == V_target`** (one load+cmp+jne). Reason: `CF_PCREL` makes a TB run at every
  virtual alias, and MPS2 AN385 mirrors SSRAM1 at 0x00400000 and block RAM four times; from the last 1 KiB of a mirror
  window an off-page target can leave the window. The ir-ipo/bolt skeptics and design A argue no guard is needed; the
  ir-instr-lowering skeptic's alias argument is concrete, so this survey adopts the 1-compare guard, with an optional
  later `alias_window_proof` (target inside the same window under every alias of the bundle memory map) that elides it
  as a separate ablation. (2) **MPU / fetch permission:** a cross-page link skips `get_page_addr_code`; `tlb_flush`
  (every MPU write) clears only the jump cache. Ship the v6 TLB-flush disarm hook (review M4/G6), or for v5 artifacts an
  admitted configuration (MPU never enabled, no memory-region remap), checked by a guard on `MPU_CTRL.ENABLE == 0`
  or rejected `mpu_config_not_frozen` / `bundle_contract_mpu_unknown`. (3) **breakpoints / watchpoints / single-step:**
  `cpu_breakpoint_insert` invalidates nothing and `regions_enabled` does not exist in the tree; the guard reads the
  breakpoint/watchpoint list heads (and `singlestep_enabled`) or a new hook sets the byte. (4) arm only after the target
  is activated (lazy activation, AMR R8). (5) disable in profile mode (`direct_transfer_active_in_profile_mode`) so
  Profile v1 stays exact. (6) reject `two_page_target`; accept that an SMC-regenerated two-page target falls back to
  `exit_tb` (dearer than the lookup) and count it. (7) re-derive the D2 `can_do_io` proof for the combination (the lookup
  was one of its writers; still holds because `cpu_tb_exec` writes `true`).
- **benefit:** 6.83 M lookups per run (47.96 %). At `c` = 5-10 ns: 34-68 ms of 987 ms = **3.5-6.9 %**, guard ≈ 1 cmp.
  Upper bound, MODEL. Largest single item in the survey with a known mechanism.
- **prereq:** WP0 arm 1; for v6: loader TLB-flush hook. **effort:** S on v5 (exists as design A GCE), M with v6 hooks.

**B3-3 (rank 2). `promote-indirect-exit@1`, site kind `return`** (design A `chain-returns@1`)
- **from:** `pgo-icall-prom` (promote-if-then-else on value-profile targets), `wholeprogramdevirt` (single-impl with check),
  BOLT `indirect-call-promotion`, `component:indirect-call-promotion-analysis`, `llvm.experimental.guard` + `deoptimize`.
- **what:** at a return exit, compare the computed target against the profile-hottest continuation(s) (≤ 2 per return TB,
  two free slots) and jump directly to the general continuation; the original lookup tail is the deopt path.
- **capability:** `REDIRECT_EXIT` with `GuardSpec`; L1 (stub after the pool, same-length tail patch, design A).
- **obligations:** all of B3-2, plus a **minimal exact guard**: for `bx lr` (78.7 % of return hits) one compare of LR
  against `cont|1`; for `pop {..,pc}`/`ldr pc` (21 %) compare `regs[15]` and `thumb` in env (there is no constant-LR fact:
  the address is a guest stack load). `condexec` is static after the branch; `cflags_next_tb` is never read by the helper
  and is always −1 while TB code runs (drop it); the per-lookup M bits (FPCCR_S_WRONG, NEW_FP_CTXT_NEEDED, LSPACT,
  MVE_NO_PRED) are constant 0 on cortex-m3 without FPU/MVE/security, licensed by a `cpu-model-constants` fact in the
  artifact identity [U until re-checked per CPU model]; hflags cannot change inside a return TB because MMIO can complete
  only in a TB's last insn (`can_do_io` rule, translator.c:209-221 + cputlb.c:1283) and `msr`/`cps` end the TB. Reject
  `tb_may_change_key_bits` / `helper_may_write_key_state` for TBs with FP/MVE/`msr` or unproven FastDyn helpers. The
  `can_do_io` store of the helper need not be repeated.
- **benefit:** one guard per return TB covers 70.79 % of return lookups (M3) = 3.43 M per run → 17-34 ms at `c` = 5-10 ns
  (**1.7-3.5 %**), two guards ≈ 80 % → about 2-4 %, minus guard cost and ~29 % failing guards that add compares. MODEL.
- **prereq:** B3-2 machinery; WP0 arm 2. **effort:** M.

**B3-4 (rank 3). `promote-indirect-exit@1`, site kind `table`** (design A `chain-indirect@1`)
- **from:** `jump-table-to-switch` (main-only), `lowerswitch`/`component:switch-lowering`, `indirectbr-expand`, ICP.
- **what:** the `tbb [pc,r3]` at 0x2a36 keeps its table byte load (a guest event), computes the target as TCG does, compares
  against the top 1-2 targets, direct exit, lookup as fallback. Same guard as B3-3 minus thumb/condexec (tbb changes
  neither). One implementation with B3-3, separate ablation flag per site kind.
- **benefit:** top target 20,895,252 of 23,071,104 hits (90.6 %) = 2.32 M per run → 12-23 ms (**1.2-2.3 %**) at
  `c` = 5-10 ns. Workload-specific (one state machine). **effort:** S on top of B3-3. Spec §8 lists "indirect targets",
  so it is in scope as a test case.

**B3-5 (rank 4, high risk). `link-msr-exit@1`** — skeptic "missed" item
- **from:** ICP + `llvm.experimental.guard` + deopt, applied to a site every assessor had marked "must return to the main loop".
- **what:** the `exit_tb(NULL,0)` that ends a TB after `msr basepri, rX` / `msr psp, r0` is 100 % monomorphic (the next
  instruction). Guard on `interrupt_request == 0` (plus the B3-2 guard set), then jump to the successor; otherwise the
  original `exit_tb(NULL,0)`.
- **soundness argument [I, needs review and a differential test]:** QEMU leaves the TB after MSR so that a newly unmasked
  pending interrupt can be taken; the NVIC raises the CPU IRQ line regardless of BASEPRI/PRIMASK/FAULTMASK, so a masked
  pending IRQ keeps `interrupt_request` set and the guard falls back exactly when the main loop could deliver something;
  BASEPRI/PSP are not hflags inputs (mmu_idx depends on CONTROL.nPRIV, Handler mode, NEGPRI), so the next key is static;
  QEMU itself relies on the same reasoning for ISB (chainable `goto_tb`). Risk: spec §3 says "QEMU requires returning to
  the execution loop after state changes that can unmask interrupts" — this pass replaces that return by a proof that the
  return would be a no-op, which a reviewer must accept explicitly; the icount-expiry path is [I].
- **benefit:** 9.39 M round trips over 9 runs = 1.04 M per run, each costing more than a lookup hit (epilogue,
  `cpu_handle_interrupt`, key build, `tb_lookup`, prologue); unmeasured — WP0 arm 4. It also lifts the
  "critical-section callee" rejection that covers 17.6 % of returns.
- **effort:** M. **Do not build before WP0 arm 4 and a written soundness review.**

**B3-6 (rank 5, spec-mandated). `specialize-returns@1` + `const-prop@1` + `branch-fold@1` + `dead-host-ops@1`**
- **from:** `ipsccp` (incl. `<func-spec>`), `sccp`, `jump-threading`, `bb-path-cloning`, BOLT `tail duplication`,
  `callsite-splitting` (mirror form), `adce`/`dead-mi-elimination`, `module-summary`/`function-attrs` for the facts.
- **what:** clone continuation `C` → version `C'` reachable only from a certified return exit of `R`; facts (e.g. "env r0 == 0
  at this exit", from a FastDyn modifier or an immediate store) fold `C'`'s test/branch. Three separate passes with their
  own on/off ablation (spec §4).
- **capabilities:** `CLONE`, `ADD_ENTRY`, `REDIRECT_EXIT` (LOADER), `ATTACH_FACT`, `PURE_REWRITE`; L1 (design A) — constant
  exit facts are L1-certifiable because `temp_sync` writes constants as immediate stores (tcg.c:4729-4765); L2/L3 only
  for computed values, ranges and multi-TB facts.
- **obligations:** the guard runs **before** `C'`'s icount-writing entry check, otherwise a deopt double-charges icount
  (mutation test `icount_double_charge`); versions never in the QHT; per-version TB struct with the same `icount`/`cflags`
  and identical search rows; facts carry dependencies (callee bytes, patch plan, guest page, observer contract,
  CPU-feature and MPU contracts); "constant through an interrupt opportunity" is safe because the entry-check exit
  resumes through the general key; a runtime-checked value guard is sound however it was chosen, but folding then nets
  ≈ 0 because the guard costs what the fold saves.
- **benefit:** **≈ 0 time (< 0.1 %)**: 7.56 % of dynamic returns are const-r0-then-test candidates (heuristic), 0.37 M per
  run × 1-2 host insns. Linking to the *general* continuation (B3-3) already removes the lookup; the version adds only the
  fold. Value = the spec B3 gate (two-TB example and its sub-cases) and the invalidation tests of spec §8.
- **prereq:** v6 loader (variants + links), facts store, new fixture with a return-value patch (none exists; the minimal
  fixture's `r0 <- 3` before a `bl` can exercise argument facts only with callee clones, out of v1). **effort:** L (with
  the loader and validator; it is the B3 slice).

**B3-7 (rank 6, conditional). `inline-lookup-probe@1`**
- **from:** `partially-inline-libcalls`, `partial-inliner` (marked "done" for softmmu; applies to the lookup helper),
  `indirectbr-expand` (inline cache).
- **what:** replace the helper call at *every* remaining `goto_ptr` exit by an inline `tb_jmp_cache` probe (hash
  `regs[15]`, compare the full key incl. `cflags`, breakpoint test, `jmp tc.ptr`), helper as fallback. Covers the
  megamorphic critical-section returns (11-15 continuations) and polymorphic tails that B3-3/4 cannot.
- **obligations:** replicate `curr_cflags` bits, the hash, full key compare (rejects `CF_INVALID`), breakpoint list check,
  suppress in profile mode (`scp_lookup_hit/miss` hooks), CPU-model check for the M bits; QEMU-internal layouts
  (`CPUJumpCache`, `TranslationBlock` offsets) become artifact identity. No per-target invalidation registry needed (the
  jump cache is already flushed on invalidation and `tlb_flush`, so the MPU problem disappears).
- **benefit:** `c − c_inline` per remaining lookup (up to 14.25 M per run); unknown; worthwhile only if WP0 shows `c` is
  dominated by the call/key-build rather than the probe. **effort:** M. L1 runtime template, independent of v6.

**B3-8 (rank 7, optional/later). `region-inline-call@1` + `region-budget@1`**
- **from:** `inline`, `module-inline` (priority driver), BOLT `inlining`, `callsite-splitting`, `memprof-context-disambiguation`.
- **what:** clone the callee path per call site so that a shared return becomes monomorphic; FormRegions substrate for L3
  cross-member simplification.
- **benefit:** the advertised 32.6 % (prvRxTask → prvCrc16Byte → prvRxTask) is fully covered by B3-2 (the `bl`) plus
  B3-3 (its return TB has hottest share 0.959). Increment over B3-2 + B3-3 ≈ (100 % − 70.8 %) of return lookups minus the
  `msr` callees ≈ 0.7-1.4 M per run → **0.4-1.4 %** at `c` = 5-10 ns, minus clone growth (top-50 edges ≈ 14 KB).
- **obligations:** multi-member regions (interior entries, merged recovery, patch-site registry), member slots safe
  against `tb_add_jump`. **effort:** L. **Ablate against B3-2 + B3-3, not against the unoptimized cache.**

B3 shared infrastructure (not passes, no ablation of their own): guarded-version utility (`loop-versioning`,
`make-guards-explicit`, `widenable.condition` → `regions_enabled`), deopt contract (`llvm.experimental.deoptimize`),
generic region cleanup after BranchFolding (`loop-simplifycfg`, `unreachable-mbb-elimination`), version dedupe
(BOLT `identical-code-folding`, only if versions are produced).

### 3c. B4 passes (executor-state synchronization)

Spec §6: "bounded executor-state synchronization elimination"; spec §3: start with bounded direct-edge regions. All
shares are of modelled fast-path host instructions (SST §7.3-7.4; helpers and main loop excluded); env is L1-resident,
so instruction share overstates time — **except** that hot TCG code is store-port bound on this host [M], which is why
store removal can matter at all.

**B4-1 (rank 1). `icount-entry-sfb@1`** (applies now; v5)
- **from:** `x86-avoid-SFB` (the hazard, not its XMM-memcpy algorithm).
- **what:** replace the entry `mov -8(%rbp),%ebx; add $-n,%ebx; test; jl; mov %bx,-8(%rbp)` (32-bit load after the
  predecessor's 16-bit store: no store forwarding on Intel cores) by a forwardable equivalent: check
  `u16.high` (`cmpw $0,-6(%rbp); jne`), 16-bit load/subtract of `u16.low`, `jb`, 16-bit store. Equivalent because
  `high ∈ {0, 0xFFFF}` (all writers grepped [V]) and `n ≤ 512`.
- **capability:** `PURE_REWRITE` with **uniform prefix growth** (10 bytes in front of an unchanged body: every intra-TB
  reference is invariant, only the first search delta and record offsets shift) — no general re-encoder needed.
- **obligations:** a V2 rule `new[10+k] == old[k]` outside declared fixups; reject `cpu_noirq_shape` (21 `CF_NOIRQ`
  units) or handle them; the single SFB pattern in the snapshot (verified unique by scan); packager accepts a grown unit
  [I; small packager change if not].
- **benefit:** replica of 0x1d5a: 4.5 → 2.2 ns per iteration [M]; 16.9 M iterations per run → ≈ 39 ms ≈ **4 %** if the
  replica transfers (MODEL from microbenchmark); other short chained TBs pay the floor too, longer bodies hide it [G].
  It also unmasks every B4 store removal. **Reporting rule:** this fixes a one-line baseline TCG inefficiency
  (`gen_tb_start`); B5 must show it as its own arm and must not count it as caching or as cross-TB optimization
  (spec §8). **effort:** S-M.

**B4-2 (rank 2). `dead-env-store@1`** (design A `elim-state-sync@1` D1/D2/D3; applies now; v5)
- **from:** `dse`, `globalopt` (stored-only globals), BOLT `frame-optimizer` (tier a), `dead-mi-elimination`; filler from
  `mc:x86-nop-padding`.
- **what:** same-size long-NOP fill of env stores that no observer reads before an overwrite: D1 flag/register stores kept
  only by TCG's `brcond` pseudo-sync; D2 `can_do_io` stores; D3 `plugin_mem_value_low` under the observer contract.
- **obligations:** (1) for `can_do_io` and `plugin_mem_value_low` **every** helper or callback call is a reader, including
  `NO_RWG` helpers (they are not TCG globals); (2) split D2: D2a (the `0` store, intra-TB) vs D2b (the final `1` store,
  read by a *chained successor's* TB-start plugin/FastDyn callbacks before its own store → needs a dependency on each
  goto_tb successor and the frozen instrumentation hash); (3) allow the `can_do_io` store class inside the prefix
  `[0, insn0_start)` while freezing the icount bytes, else D2 drops 2.77 → 1.78 % (review M3); (4) D3 needs
  `no_mem_value_reader` (no mem-callback in the unit, no `qemu_plugin_mem_get_value` import) and is retired when a memory
  callback is registered (spec §8 "observer registration"); (5) `udiv`/`sdiv` are `NO_RWG` yet trap: the env image there
  must stay the *baseline* image (neither add nor remove visible stores); (6) FastDyn `store_io`/`load_io` absolute
  addresses must not alias env (`abs_addr_aliases_env`, checked at load time); (7) correct env offsets from DWARF
  (`plugin_mem_value_high` is −0x10).
- **benefit:** D1 2.16 % (83 % in 0x1d5a, overlaps D-1) + D2 ≤ 2.77 % + D3 2.39 % of modelled instructions. Replica: 6
  dead stores nop'ed → 2.2 → 1.47 ns per iteration **only after B4-1**; with TCG's entry no change [M]. Time effect on the
  real run unmeasured; expect ≤ 1-2 % [U]. **effort:** M. It is also the cheapest "first real L1 edit" pass.

**B4-3 (rank 3). `sink-sync-stores@1` / `slowpath-materialize@1`** (design A `COMPENSATE`, D4)
- **from:** `machine-sink`, `postra-machine-sink`, `sink`, BOLT `opt:shrink-wrapping`, `fixup-statepoint-caller-saved`,
  OSR exit thunks.
- **what:** a store needed only by a softmmu slow path moves into a compensation stub on that slow path (the fast-path
  `jne` is retargeted); the slow path, its `raddr` and recovery rows are unchanged.
- **obligations:** only values in `rbx`/`r12`-`r15` across the access (guest accesses are `CALL_CLOBBER`); reaching
  definitions must follow register copies; asynchronous readers on other threads (FastDyn device models/virtuals) audited
  (U-1, review M12); MMIO/watchpoint/`cpu_io_recompile` see compensated env because the stub runs first.
- **benefit:** intra-TB D4 ≈ 0.9 %; the X2 − X1 increment ≤ 4.1 % (stores; +7.8 % with dependent ops needs L2
  recomputation). Same population as B4-6: choose per store, never add the two. **effort:** M.

**B4-4 (rank 4, v2, gated). `split-edge@1` / `exit-compensation@1`** (cross-TB D5; design A `cross_tb_sync_not_enabled`)
- **from:** `break-crit-edges`, OSR exit thunks, BOLT `frame-optimizer` tier b, `lcssa` (merged here), `deadargelim`
  (merged here as "treat certified specialized edges as direct edges").
- **what:** an edge-specific landing version of successor B for predecessor A: B's entry check, a fail stub that first
  performs the compensation A skipped (then exits as `B|3` with B's struct, everything stored, because PCREL has no
  `synchronize_from_tb` repair), then B's body; A's other exits keep the stores.
- **obligations:** v6 entries; A's redirected slot pinned against `tb_add_jump` re-chaining after B is invalidated (else
  A jumps without its stores into general B and `v7m_push_stack` reads stale flags); reaching-defs proof of value
  availability (0x1d5a's NF/ZF in r13d, CF r14d, VF r12d survive; the r1 store does not).
- **benefit:** +3.0 % stores (6.4 % with feeding ops, L2) of modelled instructions; ≈ 2.2 of the 3.0 points are 0x1d5a's
  self back edge. **Build only if B4-2 shows a measured gain.** **effort:** L.

**B4-5 (rank 5, post-acyclic B4). `loop-state-promote@1`** with modes
- **from:** `licm` (scalar promotion), `loop-load-elim` (as its loads-only mode), `gvn` load-PRE / `machine-cp`
  (`forward-env-loads`), `indvars` (missed: PC as derived IV), plus the missed icount-counter promotion.
- **what:** v1 only for **access-free self-loops** (today 0x1d5a: 18.5 % of entries, 74 % of X1): keep loop-carried env
  slots (r0/r2/r3, flags; optionally `icount_decr.u16.low` with the per-member check kept as `cmpw $0,-6(%rbp); jne;
  sub $n,%rX; jb`) in callee-saved registers across the back edge, store at the entry-check stub, exits and the
  unlinked-slot fallback; fold the PC triple in multi-TB loops (store `regs[15]` at stubs/exits/slow paths only).
- **obligations:** S1 (`regs[15]` = member start PC at the entry-check exit), slot-fallback observer, CALL_CLOBBER at
  guest accesses (hence v1 access-free only), per-copy `icount`; general form needs a TCG allocator mode (L3).
- **benefit:** loop-specific increment ≈ 1.1 % (reloads) + ≤ 1.2 % (icount promotion) + ≤ 1.5 % (PC IV) of modelled
  instructions [G], one fixture; L1 loads-only mode saves 0 instructions (mov-for-mov) and is only an ablation stage.
  **Not additive** with B4-4 on 0x1d5a. **effort:** L. Spec §4 classes pure loop optimization as optional after the core
  path.

**B4-6 (rank 6, late). `recovery-map@2`** (`state_fixups` hook)
- **from:** `rewrite-statepoints-for-gc`, StackMaps / `stackmap-liveness`, `component:x86-mcinst-lower`
  (STACKMAP/STATEPOINT), `component:x86-instrinfo-commute-remat`.
- **what:** per faultable host PC, the location of each guest slot: env, constant, or a small env expression (`ltu`/`xor`/
  `and` over one slot, enough for CF/VF of `subs`).
- **limits:** covers **fault points only** (the hook sits in `cpu_restore_state_from_tb`, a runtime change); entry-check
  exits, unchained exits and lookups never pass through it, so cross-edge deletions still need code (B4-4). X1 needs no
  records at all; the population is the X2 − X1 increment shared with B4-3. **Design A deliberately does not use it;
  this survey agrees for v1.** **effort:** M (plus QEMU hook).

**B4-7 (cleanup). `dead-host-ops@1`** (`adce`, `dead-mi-elimination`): removes ops whose only consumers were deleted
stores; must keep EFLAGS consumers. At L1 only the "nosink" deltas are available (+0.08 / +0.38 / +2.94 pp for
M1a / X1 / X2); the pde "+dep" deltas need recomputation in stubs (L2). Spec names it, so it stays as a separate pass.

### 3d. Later binary-level (L1) passes

| rank | item | from | scopt | level / obligations | benefit | effort |
|---:|---|---|---|---|---|---|
| D-1 | **IT if-conversion**, first as a same-size in-place patch of 0x1d5a (and 0x1d50), general form later | `early-ifcvt`, `simplifycfg` (if-conversion), `speculative-execution`, BOLT `CMOV conversion`, `x86-cmov-conversion` | `it-ifconvert@1` (`PURE_REWRITE`) | 22-byte window 0x48-0x5e → 17 bytes (`mov r13d,[rbp+8]; xor r13d,ebx; test r12d,r12d; cmovl ebx,r13d; mov [rbp],ebx`, assembled with llvm-mc-18) + 5-byte NOP; r13 and EFLAGS dead, no call or faultable insn, so no search-row change; v5. General pass: reject IT ranges with FastDyn modifiers/inline ops (`instrumentation_in_hammock`); needs IT-bias counts (F-3); L3 form must live in the production binary | 19.4 M IT branches per run on CRC data; 0-6 % **hypothesis** (50 % mispredict assumed, unmeasurable without counters); subsumes D1 of those TBs | S (patch) / L (general) |
| D-2 | Inline divide fast path | `libcalls-shrinkwrap` + `partially-inline-libcalls` (missed) | `inline-div-fastpath@1` | `test den; jz → helper` (+ `-1` check for `sdiv`), inline `div`; DIV_0_TRP and the helper's search row stay on the slow call | ≤ 1.7 M calls per run, 0.5-1 % [U] | M |
| D-3 | Re-encoder + peephole backlog | `peephole-opt` (optimizeCompare), `fold-memory-operand`, MC relaxation, `branch-analysis`, BOLT `reorder-blocks` (exit swap), `simplify-conditional-tail-calls`, `fix-branches`, `regreassign` | `reencode@1` (service) + `host-peephole@1`, `swap-cond-exits@1`, `fold-exit-jcc@1` | full intra-TB re-encoding with regenerated search rows (v5 suffices for single-entry rewrites); never NOP a `test` that fuses with its `jcc` | each ≤ 0.3-1 % or noise [M: test removal 2.19 → 2.18 ns]; short `jcc` saves 1.35 % of code bytes, not 4.3 % | M (re-encoder) |
| D-4 | Intra-TB cold splitting and struct segregation | `machine-function-splitter`, `hotcoldsplit`, `bbsections-prepare`, BOLT `split-functions`/`cdsplit`, `static-data-splitter`, `reorder-data` | `split-tb-cold@1`, `segregate-tb-structs@1` | v6 records with two host ranges; own-struct LEA fixups; loader accepts `tc − tb ≠ 192` | ≈ 0 on current fixtures (slow paths already at TB end; hot set fits L1i) | M-L |
| D-5 | Same-instruction multi-access TLB-check widening | `guard-widening` on TLB checks (A9) | `widen-tlb-checks@1` | one widened check for `push/pop/ldm/stm/ldrd/strd` accesses, original code as deopt; alignment bits; **needs a contract amendment** (fault-detection site) | measured 10.4 % of accesses → ≈ 2.2 % of modelled instructions (not 21 %) | M |

### 3e. Later IR-level (L2/L3) passes

| rank | item | from | level | benefit | why later |
|---:|---|---|---|---|---|
| E-1 | `const-trip-unroll@1` for prvCrc16Byte (exactly 7 iterations per call) with per-copy certified counter facts, reusing B3 ConstProp/BranchFolding | `loop-unroll-full` (`loop-unroll` only as its ablation arm) | L2 analysis + L3 or L1 clones | ≤ 4-4.5 % of modelled instructions (≈ 10 of 38 per copy fold), overlaps B4-5 | spec §4: pure loop optimization is optional after the core path |
| E-2 | `tlb-check-cse@1`: reuse the first access's TLB hit for later same-base accesses | `machine-cse`, `gvn`, `early-cse` on TLB checks | L3 backend change (the addend lives in a caller-saved scratch register) | 24.2 % of dynamic accesses qualify → ≈ 2.9 % of modelled instructions (upper bound; same base ≠ same page) | changes fault-detection sites: separate contract (TB §13) |
| E-3 | `promote-guest-state@1` / `region-regalloc@1`: guest registers in host registers across region members | `mem2reg`, `sroa`, `argpromotion`, `greedy`, `loop-simplify` | L3 + new TCG allocator mode | same pool as B4-3/4/5 (D6 loads 7.2 % + stores) — not additive | research scale (as large as B3 + B4) |
| E-4 | Contract-blocked research notes: per-region entry-check batching (12 % of modelled instructions, the largest loop-shaped cost), guest `memcpy`/`memset` idiom replacement, IV-based TLB range versioning | `loop-predication`/`guard-widening` on polls, `loop-idiom`, `irce` | L3 | large but forbidden today (interrupt opportunities and per-access events, spec §3) | needs an explicit spec amendment; raise it, do not plan it |
| E-5 | Tier-1 op capture as an **observe-only cross-check** of L1 env typing and patch provenance | `embed-bitcode` | L2 | enabler; answers spec §4's "stable operation/type identifiers" | review graft G3: before the final report, not on the B3/B4 critical path; restored TBs have no ops, so a cold-translation capture run with byte matching is needed |

Also concept-only references for an eventual L2 engine: `attributor` (dependency-tracked fixpoint = AnalysisStore
invalidation), `x86-isel`/DAG combines (`region-select`, HQEMU-style), `pgo-memop-opt` (guarded memcpy-length versions;
memcpy is the 6th return site and needs a value profile).

### 3f. Analyses and validators

| rank | item | from | scopt | notes |
|---:|---|---|---|---|
| F-1 | `dynostats@1` with a lookup-cost term | `instcount`, BOLT `print dyno-stats`, `component:dyno-stats` | analysis + report | the only cost proxy while perf is blocked (`sync_traffic.py` is the prototype); must add `c_lookup` (helper hit-path instruction count) and show the share of weight resting on assumed intra-TB splits (22.6 %). S |
| F-2 | `hostsym` validator kernel (review G1) | `verify<safepoint-ir>`, `machineverifier`, `aa-eval`, `verify<memoryssa>` (pre-filter), `debugify`/`mir-debugify` (cheap syntactic origin census + origin tags on new code), `cfi-instr-inserter` (edge-state consistency) | `V-state-at-observers`, `V-origin`, `V-memclass` rows | one symbolic executor, not three: env image equality at S1-S6 including bookkeeping (`icount_decr`, `can_do_io`, `plugin_mem_value_low`); semantic (not syntactic) equality for recomputed flags; observer list derived from the snapshot's helper table; env escapes through helper args, negative offsets, `host_abs` disjointness checked at load time. M-L (exists as a prototype) |
| F-3 | Intra-TB branch and slow-path counts (preparation-only) | BOLT `instrumentation` (missed), `machine-branch-prob`/`machine-block-freq` (missed) | `count-intra-edges@1` | same-size retargeting of `jcc rel32` to counting stubs (EFLAGS-preserving), artifact marked prep-only and rejected by production validation; supplies the IT bias (D-1), the 22.6 % of exit weight split evenly today, and slow-path frequencies (B4-3). M |
| F-4 | Helper effects table | `inferattrs`, `targetlibinfo`, `globals-aa`, BOLT `reg-analysis` | `effects.helpers@1` | refines `TCG_CALL_*`: `lookup_tb_ptr` writes `can_do_io`, reads key fields, may `cpu_loop_exit`; `rebuild_hflags` NO_RWG but writes hflags; `v7m_msr` ends the TB; `udiv`/`sdiv` = "fault point whose image must equal the baseline image"; FastDyn virtuals with register access = S4. S |
| F-5 | Callee summaries and fact store | `function-attrs`, `module-summary`, `assumptions`, `component:must-execute` | `callee-effect-summary@1`, `facts.store@1`, `region.mustexec@1` | constant exit facts at L1; facts depend on callee bytes, patch plan, page, observer contract, **CPU-feature contract** and **MPU contract**; must-execute is a per-TB check in B3 v1. M |
| F-6 | Guard spec and deopt contract | `llvm.experimental.guard`, `make-guards-explicit`, `component:guard-utils`, `widenable.condition`, `deoptimize` | `guard.spec@1` | minimal guard per exit class (B3-3); guard before the version entry check; not findable by `tb_lookup`; `regions_enabled` is a proposal, not code. S |
| F-7 | Specialization advisor | `inline-advisor`, `component:inline-cost`, `component:indirect-call-promotion-analysis` | `advisor.specialize@1`, `sites.hot_targets@1` | one benefit term in B3: count × (`c_lookup` − `c_guard`), capped by exact bytes incl. guard bytes; constants from WP0. S |
| F-8 | Env access classes and host dataflow | `tbaa`, `basic-aa`, BOLT `frame-analysis`/`stack-reaching-uses`/`reaching-defs-uses`, `component:live-phys-regs`, `reaching-deps-analysis`, `demanded-bits`, `stackmap-liveness` | `env.access@1`, `host-dataflow@1`, `host.eflags@1` | `rax` live at `exit_tb` and after the lookup call; follow copies; EFLAGS liveness before any L1 patch. M |
| F-9 | Provably non-faulting guest accesses | `component:mem-derefs` (missed), `component:constant-folding` load-from-constant part (missed) | `guest.nofault@1` | PC-literal loads (9.35 % of weighted accesses) read the TB's own SMC-protected page; a non-faulting access is not an S5 observer, so B4 may delete across it without slow-path compensation; the access and its event stay. Needs an "MPU off/unchanged" precondition and a TLB-fill audit. M |
| F-10 | Return-value profile (priority only, never a fact) | `pgo-instr-gen` value profiling (missed) | `profile.return_values@1` | `r0`/NZCV histogram in the existing `scp_lookup_hit` hook during preparation runs; ranks B3-6 candidates. S |
| F-11 | Profile bookkeeping for versions | `pseudo-probe-update`, `block-freq`, CSPGO, pseudo-probes, `prof-verify`/`prof-inject`, BOLT `bolt-address-translation` | `profile.clone_split@1`, `version-counts@1`, `cs-profile@1`, `origin-probes@1`, `verify-profile-flow@1`, `provenance-map@1` | interior clone counts are estimates and labelled so; profiles for training come only from the base snapshot (review M11). S each |
| F-12 | Graph analyses | `callgraph`, BOLT `analysis:call-graph`, `domtree`/`postdomtree`, `cycles`/`loops`, `component:cfg-sccs` | `cfg.callgraph@1`, `cfg.dom@1`, `cfg.loops@1` | `bl` targets as function entries, ELF symbols only as labels (spec §3: no compiler pattern); dominators matter only for multi-TB regions (B4/C-region). S |
| F-13 | Test discipline | `trigger-verifier-error`, `trigger-crash*`, `verify` / `verify<domtree>` (verify-each, verify-preserved) | mutation and crash-injection suites | add `icount_double_charge`, pinned-slot-reachable-by-`tb_add_jump`, guard-after-entry-check mutants. S |
| F-14 | Validator cross-check of exit target sets | `called-value-propagation` | `exit-target-sets@1` | optional: flags a profiled successor outside the static set; not a B3 prerequisite. S |

### 3g. Dropped candidates (and why)

| candidate (LLVM source) | verdict | reason |
|---|---|---|
| `dead-return-state@1` (`deadargelim`) | reject | +0.09 pp of model instructions beyond direct-edge B4; becomes an option of B4-4 |
| `merge-pools@1` (`constmerge`) | reject | 17,704 B of pools, D-cache only; adds cross-TB invalidation coupling |
| `loop-tlb-invariant-check@1` (`loop-predication`) | reject | ≈ 0.2 %, below noise; a guard kind of `irce` |
| `loop-unswitch-fact@1` (`simple-loop-unswitch`) | reject | `thumb`/`condexec` are TB-key fields (already specialized, hflags.c:610-611); no guest-register instance in 15 hot loops |
| `gototb-nop-absorb@1` (`mc:x86-prefix-padding`) | reject | measured noise; its "first in-place pass" role is served by B4-2 |
| `split-return-context@1` (`callsite-splitting`), `split-continuation@1` | downgrade | benefit belongs to B3-2/B3-3; `pop {pc}` has no constant-LR fact; versions are already per return edge |
| `clone-continuation@1` (`bb-path-cloning`) as a speed pass | downgrade | linking to the general continuation already removes the lookup; kept only as B3-6's container |
| `region-inline-call@1` as a B3 v1 deliverable (BOLT `inlining`) | downgrade | subsumed on the hottest triple; B3-8 later |
| `profile.return_context@1` shadow stack (`ctx-instr-gen`, `ctx-prof-flatten`) | downgrade | depth-1 context is already in Profile v1 (continuation identifies the call site) |
| `hoist-region-guards@1`, `merge-entry-guards@1` (`guard-widening` on entry guards) | downgrade | direct-edge members need no key guard; minimal guards leave nothing to merge (guard codegen rule + validator check) |
| `edge-facts@1`, `facts.edge` LVI solver, predicate-info, sparse propagation | downgrade | no demonstrated consumer; B3 v1 fact paths are acyclic and 1-2 edges long |
| `loop-region-canon@1` (`loop-rotate`, `loop-simplify`), `loop-exit-materialize@1` (`lcssa`) | downgrade / merge | chaining + B2 already link and place members; self-loop rotation = B4-4's edge version; lcssa content = split-edge compensation |
| `backedge-state-forward@1` (`loop-load-elim`), `forward-env-loads@1` (`machine-cp`) | downgrade | 0 instructions saved at L1 (mov-for-mov); store forwarding already makes reloads cheap; mode of B4-5 |
| `loop-region-cleanup@1`, `loop-region-unroll@1`, `env-store-pair@1`, `merge-env-const-stores@1` | downgrade | ≈ 0 / < 0.3 %; folded into generic region cleanup, E-1's ablation arm or the re-encoder |
| `loop-tlb-range-version@1` (`irce`) | downgrade | outside the contract; milestone-sized for a 3.4 % ceiling |
| `recovery.statepoints@1` as "enabler for all of B4" | downgrade | only the X2 − X1 increment (B4-6) |
| `hostcost`/`host-tuning` (`mc:x86-macro-fusion-awareness`, `x86-subtarget-tuning-features`) | downgrade | nothing left to gate; keep the CPU model in timing provenance |
| `redundant-test`, `pc-update-rmw`, `jcc-relax`, `exit-fallthrough`, `region-select` (X86 peephole/isel rows) | downgrade | measured noise in store-bound code or L2-only; D-3 backlog / E concept |
| `split-slowpaths@1` (`hotcoldsplit`), BOLT split/reorder-data, D5/D6/D9 layout rows | downgrade | ≈ 0 after B2's null result; D-4 |
| `exit-target-sets@1` (`called-value-propagation`) as a prerequisite | downgrade | F-14 validator only |

---

## 4. Reconciliation with the B3/B4 design

### 4.1 Which design

`B3-B4-design.md` and `IMPLEMENTATION-PLAN-B3B4.md` do not exist yet. The latest decision document is
the candidate-design reviews (not in the repository): **design A (host-binary rewriting, guarded chainable exits, byte-clone
variants, ≈ 235-line loader patch)** wins, with grafts from C (`hostsym` validator kernel, obligation table O1-O17,
mutation suite, TLB-flush disarm hook) and B (static off-page chaining as a first-class pass and first timed arm,
retire hooks, Tier-1 capture as cross-check). The table below maps the shortlist onto that design. Re-check it when the
final design is written.

### 4.2 Shortlist vs design A (+ grafts)

| shortlist item | status in design A (+ review) | gap / required change |
|---|---|---|
| 3.0 WP0 value probe | **covered** (WP0 E-COST, M1) | **add arms** 4 (msr-exit trip), 5 (icount SFB alone) and 7 (cmov patch), and run arm 6 with and without arm 5 |
| B3-2 `link-offpage-direct` | **covered** as optional `chain-static@1` "no PC guard"; review G4 makes it first-class | **change:** add the 1-compare `regs[15]` guard (PCREL + MPS2 aliases) or an `alias_window_proof`; MPU disarm hook in v1 (M4); profile-mode suppression |
| B3-3 returns | **covered** (`chain-returns@1`, ≤ 2 targets) | **change:** shrink the guard (g1-g4 ≈ 5-6 compares) to LR/`regs[15]`+`thumb` + breakpoint/watchpoint heads + MPU bit; drop `cflags_next_tb`; M bits only via a `cpu-model-constants` fact; consider retire hooks (G7) if WP0 shows guard cost |
| B3-4 `tbb` | **covered** (`chain-indirect@1`, top 2) | implement as one `promote-indirect-exit` code path with B3-3 (site-kind flag), keep separate ablation |
| B3-5 msr-exit link | **missing** (A excludes `exit_null` exits; all assessors treated them as mandatory main-loop returns) | add only as a WP0 measurement plus a written soundness review against spec §3; if both pass, a separate optional pass |
| B3-6 versions + CP/BF/DHO | **covered** (WP5-WP7, `specialize-returns`, `const-prop`, `branch-fold`, `dead-host-ops`) | add the `icount_double_charge` mutation test (guard before `C'` entry check); report its time value as ≈ 0 and keep it separate from lookup removal (M6); a return-value-patch fixture is required (WP8 P1 does this) |
| B3-7 inline lookup probe | **missing** | conditional on WP0: build only if `c` is large and the polymorphic remainder (critical-section returns, `tbb` tail) matters |
| B3-8 region inline | **excluded** (`region_fusion_not_enabled`) | agree; do not attempt in v1 |
| F-4 helper effects | **covered** (`effects@1`, helper whitelist audit in WP1) | encode `udiv`/`sdiv` as "baseline-image fault point"; FastDyn virtuals with register access = S4 |
| F-5 summaries, facts | **partial** (`exit-facts@1`, `patch-join@1`, `callee-summary@1`; facts local to the return TB) | add CPU-feature and MPU contracts as fact/link dependencies |
| F-2 validator | **covered by grafts** (G1 `hostsym`, G2 obligation table) | add bookkeeping slots to the env image, semantic flag equality, helper-table-derived observer list, pinned-slot check |
| B4-1 icount SFB | **missing** | add `icount-entry-sfb@1` (v5, uniform prefix growth) as its own pass and its own B5 arm, labelled "baseline TCG fix" |
| B4-2 dead env stores | **covered** (WP9 `elim-state-sync@1` D1/D2/D3) | D2a/D2b split with successor dependency; *all* calls read `can_do_io`/`plugin_mem_value_low` (A's S4 says "calls without NO_RWG"); allow the prefix `can_do_io` class (M3); fix `plugin_mem_value_high` = −0x10 (M7); `abs_addr_aliases_env`; measure only after B4-1 |
| B4-3 slow-path compensation | **covered** (D4 `COMPENSATE`) | value-availability rule must follow copies; asynchronous-reader audit (M12) |
| B4-4 cross-TB split-edge | **excluded in v1** (`cross_tb_sync_not_enabled`) | agree; v2 only if B4-2 shows a measured gain; then add slot pinning against `tb_add_jump` and the exit-index-3 (`TB_EXIT_REQUESTED`) fail stub |
| B4-5 loop promotion | **out of scope** | agree for B4 v1; post-B4 optional, v1 restricted to access-free self-loops |
| B4-6 recovery map | **not used** (`capability_not_enabled`) | agree |
| B4-7 dead host ops | **partial** (variants only; `dependent_ops_not_enabled` for B4) | fine; L1 gains are small |
| D-1 IT if-conversion | **missing** | add the same-size 0x1d5a patch to WP0 (arm 7); general pass later |
| F-1 dynostats, F-3 intra-TB counts, F-9 nofault, F-10 value profile | **missing** (sync_traffic.py exists as the F-1 prototype) | F-1 with lookup term now; F-3 before D-1/B4-3 profitability; F-9 and F-10 optional |
| A-1 placement anomaly | **not in B3/B4 scope** | schedule as a B2.1 diagnostic before B5 timing; otherwise B5 results can be confounded |

### 4.3 Recommended additions and changes to the plan

1. **Extend WP0** with the msr-exit trip, the icount-SFB entry, and the IT `cmov` patch arms, and time the D1 nop arm both
   with and without the SFB fix. Add a software-only `c_lookup` bound (hit-path instruction count) to `dynostats@1`.
2. **Add `icount-entry-sfb@1`** as a v5 L1 pass (uniform prefix growth, no re-encoder) and report it as a separate
   "baseline TCG inefficiency" arm in B5. Sequence every B4 timing after it.
3. **Guard policy:** always guard `regs[15]` on cross-page links (PCREL aliasing), drop `cflags_next_tb` everywhere,
   derive per-exit minimal guards (LR compare for `bx lr`), and make the cortex-m3 M-bit constancy an explicit
   `cpu-model-constants` fact in artifact identity.
4. **Cross-page soundness:** ship the TLB-flush disarm hook in the first v6 loader; label v5 guarded-link artifacts with
   the admitted configuration (MPU off, no remap, no gdbstub in timing arms); breakpoints are seen only by reading the
   list heads (no `regions_enabled` hook exists yet).
5. **B4 rule fixes:** D2a/D2b split with a successor dependency for the final `can_do_io` store; every call reads
   `can_do_io` and `plugin_mem_value_low`; allow the prefix `can_do_io` store class; `udiv`/`sdiv` keep the baseline
   env image; `host_abs` disjointness checked at load time; env offsets from DWARF.
6. **Unify** returns and `tbb` (and later the inline probe) under one `promote-indirect-exit` implementation with
   per-site-kind enable flags; keep the spec's three simplification passes separate.
7. **Measurement hygiene:** report lookup removal, fact folding, B4 store removal, the SFB fix and layout as separate
   arms (spec §8 "change one mechanism at a time"); ablate region inlining against guarded returns, never against the
   unoptimized cache; never add D1 and IT if-conversion, or B4-3 and B4-6, or B4-4 and B4-5 on 0x1d5a.
8. **Add preparation-only tooling** `count-intra-edges@1` (IT bias, slow-path frequencies) before any cost model that
   needs intra-TB weights.
9. **Second workload:** D1 (83 %), X1 (74 %), the IT branch (100 %) and half the return lookups sit in one CRC routine of
   one fixture. B5 claims of generality need a second compute-type workload.
10. **Raise, do not plan:** a spec amendment question for region-level entry-check batching (12 % of modelled
    instructions) and for TLB-check reuse; both are the largest remaining costs and are contract-blocked.

### 4.4 What the design should NOT attempt

- Region fusion / callee inlining / per-call-site callee clones in B3 v1 (B3-8): small increment over guarded returns,
  large loader and recovery cost.
- A shadow-stack context profile: depth-1 context is already exact.
- `state_fixups`-style recovery records for cross-edge deletions: the hook never sees entry-check exits.
- Loop canonicalization, unswitching, loop predication, loop unrolling without folding, env store pairing, `deadargelim`,
  `constmerge` as separate passes.
- Any removal, merging, forwarding or widening of guest memory accesses (GVN/DSE/memcpyopt on guest memory, inline memcpy,
  read-only-load folding, loop idioms), and any thinning of the per-TB entry check, without a spec amendment.
- TLB-check elimination (CSE, widening, `irce`) inside the B3/B4 contract.
- Register-allocation-level state caching (mem2reg/SROA/greedy over env) before acyclic B4 has shown a measured gain.
- Size-only L1 peepholes (short `jcc`, prefix padding, NOP removal, REX-avoiding renames) before the re-encoder exists
  for another reason; and intra-TB splitting or struct segregation before a front-end-bound workload exists.
- Treating unprofiled TBs with synthesized counts, stale-profile matching or sampled profiles (spec §5).

---

## 5. Notable not-applicable (passes one would expect to matter)

| LLVM pass / idea | why it does not apply here |
|---|---|
| `inline` / `always-inline` / `partial-inliner` as code merging | Helpers are opaque C (L1 cannot inline; L3 is a front-end change). Guest "inlining" can only link and clone; every member keeps its entry check, every access and callback stays. The value is the removed lookup (B3-2/3), which needs no merging. The one real partial-inlining target is the lookup helper itself (B3-7). |
| `sroa` / `mem2reg` on the guest stack | Locals and saved registers live in guest RAM; every `push`/`pop`/`ldr [sp]` is an observable event with its own fault. Only env slots can be promoted, and that is B4 state caching with recovery obligations. An O0 guest gains nothing. |
| `gvn` / `newgvn` / `early-cse` / `dse` / `memcpyopt` on guest memory | Guest load forwarding, dead guest stores and memcpy idioms delete or merge events (spec §3, N7). These passes apply only to env (B4-2/B4-5) and, under a new contract, to TLB checks (E-2). |
| `licm`, `loop-rotate`, `indvars`, `loop-reduce`, unswitching as such | Every loop iteration crosses a TB entry (interrupt + icount point) and guest IVs are architectural state with visible flags. Only scalar promotion of env slots across a back edge (B4-5) and constant-trip unrolling with retained per-copy entry checks (E-1) survive. Unswitching on `thumb`/`condexec` is already done by the TB key. |
| `loop-vectorize`, `slp-vectorizer`, `load-store-vectorizer`, `vector-combine`, `loop-idiom(-vectorize)` | Scalar Cortex-M3 guest, 0 xmm/ymm instructions in TB code; vectorizing guest accesses batches events and interrupt opportunities. Env store pairing is < 0.3 % and overlaps DSE. |
| `loop-deletion`, `dce`/`adce` removing a callee whose result is unused or constant | Guest instructions must retire (icount, coverage); "a known return value is not proof that its callee is removable" (spec §3). |
| `simplifycfg` / `jump-threading` / tail duplication as superblock formation | No fallthrough exists between TBs and no entry check may vanish, so block merging gains at most the `jmp` that chaining already makes direct. Threading survives only inside a version (B3-6). |
| `guard-widening` / `chr` / `loop-predication` / `place-safepoints` policy on the interrupt check | The entry check is the largest loop-shaped cost (≈ 12 % of modelled instructions) and is contract-protected. |
| `globaldce`, `internalize`, closed-world `wholeprogramdevirt`, `rpo-function-attrs`, `norecurse-lto-inference` | No TB is internal: exception return pops arbitrary PCs, the lookup serves any key, the cache is a partial view, unseen entries must work (spec C7). Interprocedural facts attach only to guarded specialized entries. |
| `mergefunc`, `iroutliner`, `machine-outliner`, BOLT `identical-code-folding` of TBs | Per-TB identity (goto_tb slots, exit values, search rows, `tb->icount`, invalidation) is semantics; outlining hot TLB checks adds call/ret. ICF survives only as version dedupe. |
| `deadargelim` / `function-attrs` with AAPCS knowledge | Declaring r0-r3/flags dead across `bl` assumes a compiler ABI (forbidden, spec §3) and an interrupt at any TB entry stacks those registers. |
| Machine schedulers, `break-false-deps`, `execution-domain-fix`, `machine-combiner` | Out-of-order host, ~41-instruction TBs, store-port-bound code, no SSE; nothing measured suggests a scheduling problem. |
| `x86-branch-alignment-jcc-erratum`, `aligner`, `HugePage`, `x86-insert-prefetch`, `loop-data-prefetch` | Host is Sapphire Rapids (not SKX102-affected [U]); TB starts are 64 B aligned; the arena is already `madvise`d HUGEPAGE; working sets are tiny and prefetching needs hardware profiles. |
| `implicit-null-checks` / FaultMaps as "TLB check → host page fault" | A runtime redesign (signal slow path, 1:1 guest RAM, MPU, watchpoints, SMC) that also moves fault detection. |
| Sanitizers, gcov, XRay, `fentry`, `instrprof` in production code | FastDyn's own instrumentation must be preserved, not added to; production gets no counters (spec §5). |
| `sample-profile`, `synthetic-counts-propagation`, `SampleProfileInference`, stale-profile matching, LBR/perf readers | Profiles are exact and software-only; absent counts stay unknown; mismatched profiles are rejected, not salvaged. |
| `lower-allow-check` | Profile-guided removal of checks is exactly what spec §5 forbids. |
| `tailcallelim`, `mergereturn`, `unify-loop-exits` | No host frame to drop; each TB exit is a separately patchable slot with its own exact state. B3 needs the opposite (duplicating return paths). |
| `reg2mem` | This is TCG's baseline; B4 is its inverse. |
| `print<hash-recognize>` (CRC/hash loop recognition) | The hottest loop *is* a CRC bit loop, but replacing it by table or `pclmul` code deletes interrupt opportunities and icount steps; at most a far-later STOKE-style search with extra obligations. |
| Retpoline / LVI / SLH / CET / AArch64 / RISC-V / pointer-auth / AMX / EVEX / Windows EH | Wrong host, or hardening that only adds cost and is not a project goal. |

---

## 6. Open questions that would change rankings

1. `c_lookup` and `c_guard` (WP0 arms 1-3) — decides B3-2/3/4/7 between "1 %" and "7 %".
2. The time value of removing L1-resident env stores once the SFB stall is gone (WP0 arms 5-6) — decides all of B4.
3. Whether the IT branch in 0x1d5a mispredicts (WP0 arm 7; F-3 gives the bias, not the misprediction rate).
4. Cost of one `msr`-exit main-loop round trip and a reviewer's acceptance of B3-5's argument.
5. Whether the cortex-m3 per-lookup M bits are constant for every admitted CPU model (guard minimization).
6. MPU and memory-map behaviour after boot on each fixture (E-MAP) — decides v5 admissibility of cross-page links.
7. The blinky placement anomaly (A-1) — a confounder for every timing result.
8. A second workload: every B4 and loop number is concentrated in one CRC routine.

---

## Appendix A. All passes with verdicts (one line each)

Columns: **#** = row number in the category file; **assessor** = normalized verdict (section 1.3); **skeptic** =
`keep`, `keep*` (keep with changes), `down` (downgraded), `reject`, `merge` (merged into another pass), `missed+`
(assessor said n/a/done/concept, skeptic showed it applies), blank = not challenged (the skeptic agreed with the
assessor's n/a/concept/done verdict in its bulk re-check); **→ shortlist** = item id in section 3 (`F-B3`/`F-B4` = group
3f analyses for that milestone; `infra` = shared infrastructure, not a pass). The last column is the scopt analogue name
when one exists, otherwise the first clause of the assessor's reason (truncated); full reasons are in the cited files.

### A.1 `ir-ipo` (40 rows; reasons in the maintainer's notes (not in the repository), skeptic in `challenge-ir-ipo.md`)

| # | pass | assessor | skeptic | → shortlist | scopt analogue or short reason |
|---:|---|---|---|---|---|
| 1 | `always-inline` | n/a |  |  | Nothing in the cache carries a mandatory-inline attribute |
| 2 | `attributor` | IR | down | E (L2 note) | Transfers as a region/edge fact engine: certify "callee returns r0 = k", "callee path pr… |
| 3 | `attributor-light` | IR | down | E (L2 note) | Same as #2 |
| 4 | `called-value-propagation` | now | down | F (validator) | Exact analogue on the TB graph: for every goto_ptr exit, the candidate target set |
| 5 | `constmerge` | now | reject |  | Constant pools are per TB, deduplicated only within the TB (TB §5.4, N28): 17,704 pool b… |
| 6 | `deadargelim` | B4 | reject |  | Analogue: callee "return state" (flags, scratch registers written in the return TB) that… |
| 7 | `elim-avail-extern` | n/a |  |  | No linkage model |
| 8 | `extract-blocks` | n/a |  |  | Debug utility |
| 9 | `forceattrs` | n/a |  |  | Forcing a fact is exactly what the contract forbids: facts must be certified (spec §3, §… |
| 10 | `function-import` | concept |  |  | Idea: a region needs members that may be missing from this snapshot but present in anoth… |
| 11 | `globalopt` | B4 | keep* | B4-2 | Transferable part: "stored-only global → delete stores" applied to env slots whose reade… |
| 12 | `globalsplit` | n/a |  |  | CPUARMState layout is fixed by QEMU's C code |
| 13 | `hotcoldsplit` | now | down | D | Move out-of-line slow paths (127,637 B = 21% of code), exit_requested stubs and pools ou… |
| 14 | `inferattrs` | B4 | keep | F-B3 | QEMU already declares helper attributes by hand (DEF_HELPER_FLAGS, TB §5.2/§5.4), so the… |
| 15 | `inliner-ml-advisor-release` | n/a |  |  | No trained model, and an opaque advisor conflicts with per-pass rejection reasons |
| 16 | `inliner-wrapper` | concept |  |  | The "inline, then simplify, then re-cost" interleaving transfers as a pass-manager polic… |
| 17 | `inliner-wrapper-no-mandatory-first` | n/a |  |  | Pipeline variant of #16 |
| 18 | `internalize` | concept |  |  | The only "internal" entities are specialized entries, reachable only from registered pat… |
| 19 | `iroutliner` | n/a |  |  | The repeated sequences are the 9-instruction TLB check (34% of body instructions) and ex… |
| 20 | `memprof-context-disambiguation` | concept |  |  | The idea is per-calling-context callee clones making behaviour monomorphic, here the ret… |
| 21 | `mergefunc` | n/a |  |  | Identical guest code translated twice (icount variants, suffix TBs) still needs per-TB i… |
| 22 | `module-inline` | B3 | keep* | B3-8 | The right driver for #38: order candidate call/return edges by profile weight × removed … |
| 23 | `openmp-opt` | n/a |  |  | No OpenMP runtime in guest or host code |
| 24 | `openmp-opt-postlink` | n/a |  |  | Same as #23 |
| 25 | `partial-inliner` | done | missed+ | B3-7 | QEMU already does this to the guest-memory helpers: an inline TLB-hit guard with an out-… |
| 26 | `rpo-function-attrs` | concept |  |  | Top-down "all callers agree" facts (e.g |
| 27 | `scc-oz-module-inliner` | n/a |  |  | Size-tuned inlining |
| 28 | `strip-dead-prototypes` | concept |  |  | Metadata hygiene after B3/B4 transforms: when a specialized version no longer calls look… |
| 29 | `wholeprogramdevirt` | B3 | keep* | B3-2/B3-3 | Transfers in its guarded form: replace call helper_lookup_tb_ptr; jmp %rax with a full-k… |
| 30 | `globaldce` | n/a |  |  | Not-entered ≠ unreachable (spec §1, §5) |
| 31 | `ipsccp` | B3 | keep* | B3-6 | The spec's C1 case: propagate certified exit facts ("r0 = k at the return exit") along r… |
| 32 | `loop-extract` | n/a |  |  | Debug utility |
| 33 | `argpromotion` | IR | down | E-3 | Exact analogy: guest registers are passed by reference through env at each TB edge |
| 34 | `attributor-cgscc` | IR | down | E (L2 note) | As #2 (SCC-bounded run = per-region run) |
| 35 | `attributor-light-cgscc` | IR | down | E (L2 note) | As #3 |
| 36 | `openmp-opt-cgscc` | n/a |  |  | As #23 |
| 37 | `function-attrs` | B3 | keep | F-B3 | Bottom-up callee effect summaries over the guest call graph: guest ld/st present, helper… |
| 38 | `inline` | B3 | keep* | B3-8 | Region inline across a call/return pair: call TB → callee TBs → return TB → continuation… |
| 39 | `noinline-nonprevailing` | concept |  |  | Analogue rule: never fold non-prevailing TB variants into a region |
| 40 | `norecurse-lto-inference` | n/a |  |  | Recursion has no cost here (no host frames; the guest stack is guest RAM) |

### A.2 `ir-scalar` (50 rows; reasons in the maintainer's notes (not in the repository), skeptic in `challenge-ir-scalar.md`)

| # | pass | assessor | skeptic | → shortlist | scopt analogue or short reason |
|---:|---|---|---|---|---|
| 1 | `adce` | B4 | keep | B4-7 | Inside a TB, TCG liveness already does it (TB§5.2) |
| 2 | `aggressive-instcombine` | n/a |  |  | Its main load-combining transform would merge guest loads: forbidden, since access width… |
| 3 | `alignment-from-assumptions` | concept |  |  | The only alignment-sensitive host code is the softmmu TLB compare (lea size-1 vs mov whe… |
| 4 | `assume-builder` | concept |  |  | The idea of carrying certified facts as first-class annotations is B3's ATTACH_FACT |
| 5 | `assume-simplify` | n/a |  |  | No assume bundles exist |
| 6 | `bdce` | IR |  |  | Demanded bits of the flag globals are tiny: observers read NF/VF bit 31, ZF==0, CF bit 0… |
| 7 | `callsite-splitting` | B3 | down | B3-6 | Analogue: clone the callee's return TB (and the callee path) per call context in a regio… |
| 8 | `chr` | concept |  |  | The structure (hot versioned region, one merged check, unmodified cold clone) is the B3 … |
| 9 | `consthoist` | n/a |  |  | x86-64 TCG uses imm8/imm32, RIP-relative LEA and the per-TB pool (TB§6) |
| 10 | `constraint-elimination` | IR |  |  | Inside TBs there is almost no dominating-compare redundancy: guest compilers already rem… |
| 11 | `correlated-propagation` | B3 | down | F | TCG records nothing on either side of a brcond (N23) |
| 12 | `dce` | done |  |  | liveness_pass_1 deletes ops with dead outputs and no side effects, and pure helper calls… |
| 13 | `dfa-jump-threading` | concept |  |  | The one hot switch, the tbb [pc, r3] in prvRxTask (17.99% of lookups, REP§3.3), sits in … |
| 14 | `div-rem-pairs` | n/a |  |  | Thumb-2 has no remainder instruction |
| 15 | `dse` | now | keep* | B4-2 | Globals between TCG sync points: already none (SST§7.3 M0 = 0) |
| 16 | `flattencfg` | concept |  |  | The only intra-TB diamonds are IT/conditional skips |
| 17 | `float2int` | n/a |  |  | Cortex-M3 has no FPU |
| 18 | `guard-widening` | B3 | down | (guard codegen) | Specialized entries must guard the full key before any effect (B1-design §4.9) |
| 19 | `gvn-hoist` | n/a |  |  | In a TB the two brcond arms are mutually exclusive exit sequences (mov 0x3c(%rbp); add $… |
| 20 | `gvn-sink` | n/a |  |  | Its analogue is merging identical exit stubs and slow paths across TBs (N28): code-size … |
| 21 | `infer-address-spaces` | concept |  |  | The idea, knowing which memory each access hits, is the prerequisite for B4: env vs gues… |
| 22 | `infer-alignment` | n/a |  |  | Env accesses are naturally aligned |
| 23 | `instsimplify` | done |  |  | tcg_optimize: identities, constant folding, copy propagation, known bits (TB§4.2–4.3) |
| 24 | `jump-threading` | B3 | keep | B3-6 | This is the spec's B3 acceptance case: callee returns r0 = k, the continuation starts cm… |
| 25 | `libcalls-shrinkwrap` | n/a | missed+ | D-2 | No libm calls |
| 26 | `mem2reg` | B4 | down | E-3 | Analogue: keep guest registers/flags in host registers across region edges (N2, D6) |
| 27 | `memcpyopt` | now | down | D | Guest memcpy idioms are forbidden (guest events) |
| 28 | `mergeicmps` | n/a |  |  | Guest-memory compares keep each load (event) |
| 29 | `move-auto-init` | n/a |  |  | There is no auto-init |
| 30 | `nary-reassociate` | n/a |  |  | x86 addressing modes absorb base+disp |
| 31 | `newgvn` | IR |  |  | Same targets as gvn (row 42) |
| 32 | `partially-inline-libcalls` | B3 | keep* | B3-7 | Analogue: partially inline helper_lookup_tb_ptr at return/off-page/tbb exits as an inlin… |
| 33 | `reassociate` | IR |  |  | TCG never combines (x+a)+b (N22) |
| 34 | `sccp` | B3 | keep | B3-6 | Core B3 pass: propagate certified entry facts through a region path and fold |
| 35 | `separate-const-offset-from-gep` | n/a |  |  | x86 base+disp32 addressing makes this free |
| 36 | `sink` | B4 | keep | B4-3 | Analogue: partial dead-store elimination by sinking |
| 37 | `slsr` | n/a |  |  | Guest multiplies are guest-compiler decisions |
| 38 | `tailcallelim` | n/a |  |  | Guest tail calls are already plain branches |
| 39 | `tlshoist` | n/a |  |  | No TLS in generated code |
| 40 | `early-cse` | IR |  |  | TCG has no expression CSE (N5) but already forwards explicit env loads and constant stor… |
| 41 | `function-simplification` | concept |  |  | scopt needs an ordered region simplification pipeline (section 4), but spec §6 requires … |
| 42 | `gvn` | B4 | keep | B4-5 | Analogue: across a region edge, B's first-use env loads of globals that A holds in a hos… |
| 43 | `instcombine` | done |  |  | Single-op identities, known bits, constant/compare/branch folding and TST canonicalizati… |
| 44 | `mldst-motion` | n/a |  |  | TB brcond arms do not re-join (each ends in an exit), and IT diamonds store asymmetrical… |
| 45 | `simplifycfg` | IR | keep* | D-1 | Block merging across TBs is limited: every member keeps its entry check (interrupt oppor… |
| 46 | `speculative-execution` | IR | keep* | D-1 | Same mechanism as row 45: speculate the predicated register-only Thumb instruction and s… |
| 47 | `sroa` | done | down | E-3 | CPUARMState is already scalarized: 16 registers + 4 flags (+2 exclusive) are separate TC… |
| 48 | `drop-unnecessary-assumes` | n/a |  |  | No assumes |
| 49 | `jump-table-to-switch` | B3 | keep* | B3-4 | Analogue: the single tbb [pc, r3] at 0x2a36 (23.07M lookup hits, 17.99% of all; 7 target… |
| 50 | `logical-sroa` | done |  |  | Same as row 47: guest state is already split into scalar globals |

### A.3 `ir-loop` (34 rows; reasons in the maintainer's notes (not in the repository), skeptic in `challenge-ir-loop.md`)

| # | pass | assessor | skeptic | → shortlist | scopt analogue or short reason |
|---:|---|---|---|---|---|
| 6 | `irce` | IR | down | E (research) | The "range check" here is the softmmu TLB check (9 instructions per access, 34% of body … |
| 11 | `load-store-vectorizer` | now | down | D | Only for env stores |
| 31 | `loop-data-prefetch` | n/a |  |  | The firmware's RAM working set and env are L1/L2-resident; there are no misses to hide |
| 24 | `loop-distribute` | n/a |  |  | It reorders guest accesses across iterations. |
| 25 | `loop-fusion` | n/a |  |  | It reorders guest accesses between the two loops. |
| 2 | `loop-load-elim` | B4 | down | B4-5 (mode) | It maps to SST D6 across the back edge: the next iteration reloads guest registers from … |
| 16 | `loop-sink` | concept |  |  | TCG output has no preheader code computing invariants |
| 5 | `loop-versioning` | B3 | keep* | B3 infra | The mechanism is exactly B3's guarded version with the general entry kept (spec C7, B1 §… |
| 19 | `slp-vectorizer` | n/a |  |  | There are no isomorphic scalar chains in 5-instruction TBs |
| 20 | `vector-combine` | n/a |  |  | There are no vector ops (0 xmm/ymm in the snapshot; vector templates are used only for N… |
| 8 | `loop-unroll` | IR | down | E-1 (ablation arm) | Stitching k copies of a loop TB at L1 only removes the goto_tb jmp plus NOP pad (about 2… |
| 18 | `loop-vectorize` | n/a |  |  | It would batch guest accesses and instructions |
| 27 | `loop-flatten` | n/a |  |  | It changes the guest control flow actually executed. |
| 26 | `loop-interchange` | n/a |  |  | It changes iteration order, and so the guest instruction order. |
| 28 | `loop-unroll-and-jam` | n/a |  |  | It interleaves guest instructions from different iterations. |
| 33 | `canon-freeze` | n/a |  |  | TCG IR and ARM semantics have no poison or undef, and the spec forbids assuming undefine… |
| 13 | `indvars` | concept | missed+ | B4-5 (PC IV) | The analysis (stride, start and trip count of guest-register IVs) is needed by irce, unr… |
| 17 | `loop-bound-split` | concept |  |  | Iteration-range versioning transfers through loop-versioning/irce |
| 23 | `loop-deletion` | n/a |  |  | Guest instructions must retire (icount, coverage, interrupt points) |
| 21 | `loop-idiom` | n/a |  |  | Replacing the guest's byte loop (memcpy 0x34aa, 3.06% of entries; memset 0x331c) with a … |
| 12 | `loop-instsimplify` | done |  |  | TCG already folds constants, copies, known bits and single-op identities within an EBB (… |
| 7 | `loop-predication` | IR | reject |  | For TLB checks: an access whose address range is invariant, such as the 1 KiB CRC table … |
| 14 | `loop-reduce` | concept |  |  | Guest IV arithmetic must stay exact, since the registers are guest state |
| 29 | `loop-reroll` | n/a |  |  | It would change the guest instruction stream |
| 9 | `loop-simplifycfg` | B3 | down | B3 cleanup | Region cleanup after BranchFolding: drop loop exits proven dead, merge straight member c… |
| 3 | `loop-unroll-full` | IR | keep* | E-1 | The CRC bit loop (0x1d50 + 7× 0x1d5a, r3 = 8..1) and 0x2236 (8 words) have constant trip… |
| 30 | `loop-versioning-licm` | n/a |  |  | env and guest RAM are statically disjoint, so there is nothing to check at run time |
| 1 | `licm` | B4 | keep* | B4-5 | Guest registers and flags live in env, so scalar promotion over a loop region is B4 stat… |
| 15 | `lnicm` | concept |  |  | Same idea as licm |
| 4 | `loop-rotate` | B3 | keep* | B4 infra | At TB-graph level this is the loop-region canonicalizer |
| 10 | `simple-loop-unswitch` | B3 | reject |  | A guarded loop version keyed on a fact that the loop cannot change: a register the loop … |
| 22 | `loop-idiom-vectorize` | n/a |  |  | Same as loop-vectorize |
| 32 | `loop-term-fold` | n/a |  |  | The exit test is a guest instruction with architecturally visible flags. |
| 34 | `sandbox-vectorizer` | n/a |  |  | As for slp-vectorizer: no vectorizable host work. |

### A.4 `ir-instr-lowering-utility` (216 rows; reasons in the maintainer's notes (not in the repository), skeptic in `challenge-ir-instr-lowering-utility.md`)

| # | pass | assessor | skeptic | → shortlist | scopt analogue or short reason |
|---:|---|---|---|---|---|
| 1 | `annotation2metadata` | n/a |  |  | Source-annotation plumbing; TBs carry no source annotations (scopt has its own ANNOTATE … |
| 2 | `canonicalize-aliases` | n/a |  |  | No symbol aliases in the cache; helpers are bound by absolute address and checked at loa… |
| 3 | `check-debugify` | B3 | keep* | F | Analogue: after each B3/B4 pass, check that every guest-insn boundary, ldst site, helper… |
| 4 | `coro-cleanup` | n/a |  |  | C++20 coroutine lowering |
| 5 | `coro-early` | n/a |  |  | C++20 coroutine lowering |
| 6 | `cross-dso-cfi` | n/a |  |  | Control-flow-integrity lowering over type metadata |
| 7 | `debugify` | B3 | keep* | F | Analogue: tag every host instruction of a region with (unit, guest insn index, op class:… |
| 8 | `dfsan` | n/a |  |  | Adds host-level checking instrumentation |
| 9 | `dot-callgraph` | B3 |  |  | Tooling: export the guest call/return graph (bl sites, callee entries, return TBs, conti… |
| 10 | `dxil-upgrade` | n/a |  |  | GPU / DirectX / HIP-specific; the host is x86-64 CPU code. |
| 11 | `hipstdpar-interpose-alloc` | n/a |  |  | GPU / DirectX / HIP-specific; the host is x86-64 CPU code. |
| 12 | `hipstdpar-select-accelerator-code` | n/a |  |  | GPU / DirectX / HIP-specific; the host is x86-64 CPU code. |
| 13 | `insert-gcov-profiling` | n/a |  |  | Counters on production hits are forbidden (spec §5); preparation counts are already exac… |
| 14 | `instrorderfile` | done |  |  | TBs are allocated in translation order, i.e |
| 15 | `instrprof` | done |  |  | Counting is done by FastDyn's scp profile hooks on an unchained warm copy (tcg-baseline … |
| 16 | `invalidate<all>` | n/a |  |  | Test utility; AnalysisStore.invalidate exists |
| 17 | `lower-global-dtors` | n/a |  |  | No static destructors in TB code. |
| 18 | `lower-ifunc` | n/a |  |  | No ifuncs; helper binding is the runtime's and is verified against the current maps at l… |
| 19 | `lowertypetests` | n/a |  |  | Control-flow-integrity lowering over type metadata |
| 20 | `memprof-module` | n/a |  |  | Profiles host heap accesses to guide allocation hints; guest data layout is not ours to … |
| 21 | `metarenamer` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer. |
| 22 | `name-anon-globals` | n/a |  |  | No anonymous symbols; units are identified by UnitId. |
| 23 | `no-op-module` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer. |
| 24 | `objc-arc-apelim` | n/a |  |  | Objective-C ARC retain/release optimization; there is no reference counting in TB code o… |
| 25 | `pgo-icall-prom` | B3 | keep* | B3-2/3/4 | Direct analogue: a goto_ptr exit (call helper_lookup_tb_ptr; jmp %rax) becomes 'compare … |
| 26 | `pgo-instr-gen` | done | missed+ | F (value profile) | Profile v1 already gives exact TB-entry and per-exit edge counts, including every lookup… |
| 27 | `pgo-instr-use` | now |  |  | profile.counts@1 |
| 28 | `poison-checking` | n/a |  |  | Host code has no poison/UB semantics to check, and spec §3 forbids assuming source-langu… |
| 29 | `print` | now |  |  | Tooling: per-pass before/after annotated disassembly of every changed unit (site kinds, … |
| 30 | `print-callgraph` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 31 | `print-callgraph-sccs` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 32 | `print-ir-similarity` | now |  |  | Tooling: report byte-identical / register-renamed slow paths and exit stubs across TBs (… |
| 33 | `print-lcg` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 34 | `print-lcg-dot` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 35 | `print-must-be-executed-contexts` | B3 |  |  | Tooling: dump of the effect-prefix analysis (which guest effects, faultable accesses and… |
| 36 | `print-profile-summary` | now |  |  | hotness@1 |
| 37 | `print-stack-safety` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 38 | `print<inline-advisor>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 39 | `print<module-debuginfo>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 40 | `pseudo-probe` | concept |  |  | Idea transfers (identifiers that survive transformation; UnitId + guest insn index alrea… |
| 41 | `pseudo-probe-update` | B3 | keep | F-B3 | Analogue: when B3 clones a continuation into versions, split its counts across versions … |
| 42 | `recompute-globalsaa` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer. |
| 43 | `rel-lookup-table-converter` | n/a |  |  | TCG emits no lookup tables; absolute constant-pool entries are classified and relocated … |
| 44 | `rewrite-statepoints-for-gc` | B4 | down | B4-6 | Analogue: an explicit per-sync-point live-state record for optimized regions: for each g… |
| 45 | `rewrite-symbols` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer. |
| 46 | `sample-profile` | n/a |  |  | Profiles are exact software counts (decision: software-only; perf blocked on this host);… |
| 47 | `sancov-module` | n/a |  |  | Fuzzing coverage is part of FastDyn's observation contract: it must be preserved exactly… |
| 48 | `sanmd-module` | n/a |  |  | Metadata for sanitizer runtimes; our metadata (.scm, region table, recovery rows) has it… |
| 49 | `strip` | n/a |  |  | Debug-info / symbol plumbing; not an optimization |
| 50 | `strip-dead-debug-info` | n/a |  |  | Debug-info / symbol plumbing; not an optimization |
| 51 | `strip-debug-declare` | n/a |  |  | Debug-info / symbol plumbing; not an optimization |
| 52 | `strip-nondebug` | n/a |  |  | Debug-info / symbol plumbing; not an optimization |
| 53 | `strip-nonlinetable-debuginfo` | n/a |  |  | Debug-info / symbol plumbing; not an optimization |
| 54 | `synthetic-counts-propagation` | n/a |  |  | hotness@1 |
| 55 | `trigger-crash` | now |  |  | Test analogue: a pass that raises proves the manager records FAILED, discards its edits … |
| 56 | `trigger-verifier-error` | now |  |  | Test analogue: mutation passes that submit known-bad edits (dropped guest access, missin… |
| 57 | `tsan-module` | n/a |  |  | Adds host-level checking instrumentation |
| 58 | `verify` | now |  |  | Analogue: run the cheap structural checks (refs re-decode of changed units, V1/V2/V4/V5 … |
| 59 | `view-callgraph` | n/a |  |  | Interactive viewer (opens a .dot viewer); not an optimization; covered by the dot-cfg / … |
| 60 | `asan` | n/a |  |  | Adds host-level checking instrumentation |
| 61 | `cg-profile` | now |  |  | Exists in scopt B2: cluster pass orders hot TBs by Pettis-Hansen over profiled TB edge w… |
| 62 | `embed-bitcode` | IR | keep* | E-5 | Analogue: store a renamed, pointer-free capture of each TB's optimized TCG op list (with… |
| 63 | `hwasan` | n/a |  |  | Adds host-level checking instrumentation |
| 64 | `memprof-use` | n/a |  |  | Host-heap allocation hints; guest allocations and guest data layout are out of scope. |
| 65 | `msan` | n/a |  |  | Adds host-level checking instrumentation |
| 66 | `print<structural-hash>` | now |  |  | Exists in substance: unit content hashes key AnalysisStore staleness (dep_units) |
| 67 | `no-op-cgscc` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer. |
| 68 | `coro-split` | n/a |  |  | C++20 coroutine lowering |
| 69 | `aa-eval` | B4 | keep* | F | Analogue: classify every memory operand of every touched TB (env slot by field, executor… |
| 70 | `add-discriminators` | done |  |  | Distinct TBs at the same guest pc are already distinct units (TB key pc/flags/cs_base/cf… |
| 71 | `annotation-remarks` | now |  |  | Exists: per-pass provenance and the outcome ledger (B1 §5.9) are the remark stream. |
| 72 | `bounds-checking` | n/a |  |  | Adds host-level checking instrumentation |
| 73 | `break-crit-edges` | B4 | keep* | B4-4 | Analogue: an edge-specific landing stub A->B' (B's entry check + compensation for this p… |
| 74 | `coro-elide` | n/a |  |  | C++20 coroutine lowering |
| 75 | `count-visits` | now |  |  | Exists: the ledger records each pass invocation and outcome (ablation accounting). |
| 76 | `declare-to-assign` | n/a |  |  | Debug-info / symbol plumbing; not an optimization |
| 77 | `dot-cfg` | now |  |  | Tooling: TB-graph (or region / intra-TB host CFG) .dot export with entry/edge counts and… |
| 78 | `dot-cfg-only` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 79 | `dot-dom` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 80 | `dot-dom-only` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 81 | `dot-post-dom` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 82 | `dot-post-dom-only` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 83 | `fix-irreducible` | n/a |  |  | Region formation can simply reject multi-entry cycles; a dispatcher would add a guarded … |
| 84 | `helloworld` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer. |
| 85 | `inject-tli-mappings` | n/a |  |  | No vectorization of guest scalar code (N26) and no library calls to map. |
| 86 | `instcount` | now | keep | F-1 | Analogue: static and profile-weighted host-instruction mix per unit/region/artifact by c… |
| 87 | `instnamer` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer. |
| 88 | `kcfi` | n/a |  |  | Control-flow-integrity lowering over type metadata |
| 89 | `lcssa` | B4 | merge | B4-4 | Analogue for a multi-TB or self-loop region with register-resident guest state: every va… |
| 90 | `lint` | B3 |  |  | Analogue: lint over rewritten host code: rbp/rsp invariants, env offsets in the field ta… |
| 91 | `loop-simplify` | B4 | down | E-3 | Analogue: canonical form for a loop region in the TB graph (e.g |
| 92 | `lower-constant-intrinsics` | n/a |  |  | No such intrinsics in TB code. |
| 93 | `lower-expect` | n/a |  |  | No source hints; exact profile edge counts already exist; intra-TB branch layout is a co… |
| 94 | `lower-guard-intrinsic` | concept |  |  | Final lowering of a guard; scopt guards are emitted directly as cmp/jne to the general p… |
| 95 | `lower-widenable-condition` | concept |  |  | Corresponds to freezing the guard set of a continuation version once guards are merged; … |
| 96 | `loweratomic` | done |  |  | TCG expands atomics and cmpxchg non-atomically when CF_PARALLEL is clear (one vCPU): tcg… |
| 97 | `lowerinvoke` | n/a |  |  | No invoke/landingpad; guest exceptions leave via longjmp plus search-data unwinding, whi… |
| 98 | `lowerswitch` | concept |  |  | Idea: lower a k-target goto_ptr exit (tbb at 0x2a36 with 7 targets, polymorphic returns)… |
| 99 | `make-guards-explicit` | B3 | keep* | F-B3 guard | Analogue: every assumption of a specialized continuation (full TB key, value facts such … |
| 100 | `memprof` | n/a |  |  | Host-heap access profiling; see memprof-module. |
| 101 | `mergereturn` | n/a |  |  | Each TB exit is a distinct patchable slot with its own exit index and its own exact gues… |
| 102 | `no-op-function` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer. |
| 103 | `objc-arc` | n/a |  |  | Objective-C ARC retain/release optimization; there is no reference counting in TB code o… |
| 104 | `objc-arc-contract` | n/a |  |  | Objective-C ARC retain/release optimization; there is no reference counting in TB code o… |
| 105 | `objc-arc-expand` | n/a |  |  | Objective-C ARC retain/release optimization; there is no reference counting in TB code o… |
| 106 | `pa-eval` | n/a |  |  | Objective-C ARC retain/release optimization; there is no reference counting in TB code o… |
| 107 | `pgo-memop-opt` | IR | down | E | Analogue (low value): a guarded version of a guest copy loop region for a dominant lengt… |
| 108 | `place-safepoints` | done |  |  | Every TB entry already polls (interrupt/icount entry check; tcg-baseline §2.5, §8) |
| 109 | `print-alias-sets` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 110 | `print-cfg-sccs` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 111 | `print-memderefs` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 112 | `print-mustexecute` | B3 |  |  | Tooling: same analogue as print-must-be-executed-contexts (effect-prefix dump), here for… |
| 113 | `print-predicateinfo` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 114 | `print<access-info>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 115 | `print<assumptions>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 116 | `print<block-freq>` | now |  |  | Tooling: dump of TB frequencies (entries per phase, status exact/lower_bound/unknown) th… |
| 117 | `print<branch-prob>` | now |  |  | Tooling: dump of per-exit edge probabilities that layout and B3 selection used. |
| 118 | `print<cost-model>` | B3 |  |  | Tooling: dump of the host-cost estimate (per TB, per exit path, per candidate version) u… |
| 119 | `print<cycles>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 120 | `print<da>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 121 | `print<debug-ata>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 122 | `print<delinearization>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 123 | `print<demanded-bits>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 124 | `print<domfrontier>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 125 | `print<domtree>` | B3 |  |  | Tooling: dump of TB-graph dominators for debugging region formation (single-entry region… |
| 126 | `print<func-properties>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 127 | `print<inline-cost>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 128 | `print<inliner-size-estimator>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 129 | `print<lazy-value-info>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 130 | `print<loops>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 131 | `print<memoryssa-walker>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 132 | `print<phi-values>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 133 | `print<postdomtree>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 134 | `print<regions>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 135 | `print<scalar-evolution>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 136 | `print<stack-safety-local>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 137 | `print<uniformity>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 138 | `redundant-dbg-inst-elim` | n/a |  |  | Debug-info / symbol plumbing; not an optimization |
| 139 | `reg2mem` | n/a |  |  | The baseline already is 'reg2mem at every TB boundary' (all guest state in env at exits;… |
| 140 | `scalarize-masked-mem-intrin` | n/a |  |  | No vector code in the snapshot (0 xmm/ymm instructions; tcg-baseline §10). |
| 141 | `scalarizer` | n/a |  |  | No vector code in the snapshot (tcg-baseline §10). |
| 142 | `strip-gc-relocates` | n/a |  |  | Nothing relocates guest state; no relocates to strip. |
| 143 | `structurizecfg` | n/a |  |  | Structured control flow is a GPU requirement; host x86 code and TB regions need none. |
| 144 | `transform-warning` | now |  |  | Exists: every pass returns an outcome with a stable reason code (skipped_unsupported/unk… |
| 145 | `tsan` | n/a |  |  | Adds host-level checking instrumentation |
| 146 | `unify-loop-exits` | n/a |  |  | Each loop exit of a TB region needs its own exact materialized state and exit slot; a co… |
| 147 | `verify<domtree>` | now |  |  | Analogue: a debug mode of PassManager that recomputes every analysis a pass declared as … |
| 148 | `verify<loops>` | B4 |  |  | Analogue: recompute TB-graph loop/SCC structure after region formation and compare with … |
| 149 | `verify<memoryssa>` | B4 | merge | F (hostsym) | Analogue: independently re-derive env-slot def/use and liveness on the rewritten host co… |
| 150 | `verify<regions>` | B3 |  |  | Analogue: region invariants (guarded single specialized entry, every member keeps its en… |
| 151 | `verify<safepoint-ir>` | B4 | keep* | F (hostsym) | Analogue: no stale or unsynchronized guest state is observable at any sync point (S1-S6 … |
| 152 | `verify<scalar-evolution>` | n/a |  |  | No SCEV-like analysis is planned for TB code. |
| 153 | `view-cfg` | n/a |  |  | Interactive viewer (opens a .dot viewer); not an optimization; covered by the dot-cfg / … |
| 154 | `view-cfg-only` | n/a |  |  | Interactive viewer (opens a .dot viewer); not an optimization; covered by the dot-cfg / … |
| 155 | `view-dom` | n/a |  |  | Interactive viewer (opens a .dot viewer); not an optimization; covered by the dot-cfg / … |
| 156 | `view-dom-only` | n/a |  |  | Interactive viewer (opens a .dot viewer); not an optimization; covered by the dot-cfg / … |
| 157 | `view-post-dom` | n/a |  |  | Interactive viewer (opens a .dot viewer); not an optimization; covered by the dot-cfg / … |
| 158 | `view-post-dom-only` | n/a |  |  | Interactive viewer (opens a .dot viewer); not an optimization; covered by the dot-cfg / … |
| 159 | `cfguard` | n/a |  |  | Control-flow-integrity lowering over type metadata |
| 160 | `ee-instrument` | n/a |  |  | Adds instrumentation; FastDyn's own entry/exit hooks are part of the contract and are pr… |
| 161 | `lower-matrix-intrinsics` | n/a |  |  | No matrix/vector code. |
| 162 | `print<memoryssa>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 163 | `print<stack-lifetime>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 164 | `no-op-loopnest` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer. |
| 165 | `dot-ddg` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 166 | `no-op-loop` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer. |
| 167 | `print<ddg>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 168 | `print<iv-users>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 169 | `print<loop-cache-cost>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 170 | `print<loopnest>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 171 | `alloc-token` | n/a |  |  | Host-allocator hardening; no allocation calls in TB code. |
| 172 | `assign-guid` | done |  |  | Stable identity already exists: QEMU's TB key plus scopt's UnitId/content hashes (B1 des… |
| 173 | `copyprof` | n/a |  |  | C++ copy profiling; no objects or copies in TB code. |
| 174 | `copyprof-module` | n/a |  |  | See copyprof. |
| 175 | `copyprof-stores` | n/a |  |  | See copyprof. |
| 176 | `coro-annotation-elide` | n/a |  |  | C++20 coroutine lowering |
| 177 | `csan` | n/a |  |  | Adds host-level checking instrumentation |
| 178 | `csan-module` | n/a |  |  | Adds host-level checking instrumentation |
| 179 | `ctx-instr-gen` | B3 | down | (reject new collection) | Analogue (preparation runs only): profile return edges per call context (which bl site /… |
| 180 | `ctx-instr-lower` | n/a |  |  | Lowering detail of ctx-instr-gen; our counting lives in QEMU's sc-profile hooks, not in … |
| 181 | `ctx-prof-flatten` | B3 | down | (reject new collection) | Analogue: project per-context counts back onto Profile v1 TB/edge counts and onto cloned… |
| 182 | `ctx-prof-flatten-prethinlink` | n/a |  |  | ThinLTO staging detail; no separate analogue. |
| 183 | `declare-runtime-libcalls` | n/a |  |  | No library calls to declare; helpers are bound by the runtime. |
| 184 | `drop-type-tests` | n/a |  |  | Control-flow-integrity lowering over type metadata |
| 185 | `expand-variadics` | n/a |  |  | No variadic calls in TB code. |
| 186 | `fatlto-cleanup` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer. |
| 187 | `func-properties-stats` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer |
| 188 | `hipstdpar-math-fixup` | n/a |  |  | GPU / DirectX / HIP-specific; the host is x86-64 CPU code. |
| 189 | `instrumentor` | n/a |  |  | FastDyn itself is the instrumentor (modifiers, virtuals, plugin callbacks); scopt must p… |
| 190 | `kernel-info` | n/a |  |  | GPU / DirectX / HIP-specific; the host is x86-64 CPU code. |
| 191 | `lower-allow-check` | n/a |  |  | Profile-guided removal of checks is exactly what the contract forbids: 'profiles do not … |
| 192 | `lower-comment-string` | n/a |  |  | Not an optimization (test/pipeline utility); nothing to transfer. |
| 193 | `memprof-remove-attributes` | n/a |  |  | Host-heap profiling plumbing. |
| 194 | `normalize` | now |  |  | Tooling: normalized disassembly (fixup fields and absolute addresses replaced by symboli… |
| 195 | `nsan` | n/a |  |  | Adds host-level checking instrumentation |
| 196 | `pgo-force-function-attrs` | concept |  |  | hotness@1 |
| 197 | `print<ctx-prof-analysis>` | B3 |  |  | Tooling companion of ctx-instr-gen: dump per-context return-edge counts. |
| 198 | `print<dxil-metadata>` | n/a |  |  | GPU / DirectX / HIP-specific; the host is x86-64 CPU code. |
| 199 | `print<dxil-resources>` | n/a |  |  | GPU / DirectX / HIP-specific; the host is x86-64 CPU code. |
| 200 | `print<hash-recognize>` | concept |  |  | A guest bitwise-CRC loop could be recognized, but replacing a multi-TB guest loop with n… |
| 201 | `print<ir2vec-vocab>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 202 | `print<ir2vec>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 203 | `print<reg-usage>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 204 | `print<scev-division>` | n/a |  |  | Printer of an LLVM analysis; not an optimization |
| 205 | `prof-inject` | B3 |  |  | Test analogue: inject synthetic counts into a test snapshot, run B3 passes, and require … |
| 206 | `prof-verify` | B3 |  |  | Validator analogue: every unit/edge of a candidate (including clones and stubs) has a pr… |
| 207 | `rtsan` | n/a |  |  | Adds host-level checking instrumentation |
| 208 | `simplify-type-tests` | n/a |  |  | Control-flow-integrity lowering over type metadata |
| 209 | `strip-convergence-intrinsics` | n/a |  |  | GPU / DirectX / HIP-specific; the host is x86-64 CPU code. |
| 210 | `strip-dead-cg-profile` | n/a |  |  | B2 rebuilds edge weights from the current snapshot; nothing stale to strip. |
| 211 | `trigger-crash-cgscc` | now |  |  | Same test analogue as trigger-crash (snapshot-scope pass). |
| 212 | `trigger-crash-function` | now |  |  | Same test analogue as trigger-crash (unit-scope pass). |
| 213 | `trigger-crash-loop` | now |  |  | Same test analogue as trigger-crash (region-scope pass). |
| 214 | `trigger-crash-module` | now |  |  | Same test analogue as trigger-crash. |
| 215 | `tysan` | n/a |  |  | Adds host-level checking instrumentation |
| 216 | `verify<cycles>` | B4 |  |  | Same analogue as verify<loops> (cycles also cover irreducible TB-graph cycles). |

### A.5 `ir-analyses` (95 rows; reasons in the maintainer's notes (not in the repository), skeptic in `challenge-ir-analyses.md`)

| # | pass | assessor | skeptic | → shortlist | scopt analogue or short reason |
|---:|---|---|---|---|---|
| 1 | `callgraph` | B3 | keep* | F-B3 | needed as the guest call graph: bl sites → callee entry TB, return TBs → continuations, … |
| 2 | `collector-metadata` | n/a |  |  | no garbage collector anywhere in TB code |
| 3 | `inline-advisor` | B3 | keep | F-B3 | the analogue is the specialization advisor: which (call, callee, return, continuation) c… |
| 4 | `ir-similarity` | concept |  |  | repeated host sequences exist (9-insn TLB check, exit stubs, slow paths), but outlining … |
| 5 | `lcg` | B3 |  |  | bottom-up order over the guest call graph for callee summaries (return facts need summar… |
| 6 | `module-summary` | B3 | keep* | F-B3 | spec §4 asks to "export a summary of the callee and its patches together": per guest fun… |
| 7 | `no-op-module` | n/a |  |  | test plumbing |
| 8 | `pass-instrumentation` | now |  |  | PassManager + ledger already record invocation, outcome, reason, ablation (--disable/--o… |
| 9 | `profile-summary` | now |  |  | hotness.v1 (99% coverage prefix of exact steady entries; unknown never cold) |
| 10 | `stack-safety` | n/a |  |  | TB host frame is not address-taken; the guest stack is guest memory whose accesses must … |
| 11 | `verify` | now |  |  | ValidateAndPackage V1-V10 (design §5.8) is the verifier; B3/B4 need new rows (region ent… |
| 12 | `globals-aa` | B4 | keep* | F-B4 | per-helper and per-TB mod/ref of env offsets; TCG has only three classes (NO_RWG, NO_WG,… |
| 13 | `no-op-cgscc` | n/a |  |  | test plumbing |
| 14 | `fam-proxy` | n/a |  |  | pass-manager plumbing; AnalysisStore scopes (unit/snapshot/region) replace it |
| 15 | `aa` | B4 |  |  | the env alias oracle combining access class (row 51) and base/offset/size (row 47); quer… |
| 16 | `access-info` | n/a |  |  | no loop vectorization; guest memory ops may not be reordered (N7, spec §3) |
| 17 | `assumptions` | B3 | keep* | F-B3 | the analogue is the certified-fact store (ATTACH_FACT, design §5.4) with provenance, gua… |
| 18 | `bb-sections-profile-reader` | now |  |  | Profile v1 ingest (.qprof → profile.json, design §3) is the reader; exact, software-only |
| 19 | `block-freq` | now |  |  | exact TB entry counts (profile.counts.v1); B3 needs per-version counts (exact from edge … |
| 20 | `branch-prob` | now |  |  | from exact edge counts per exit; for goto_ptr exits a successor distribution (return pol… |
| 21 | `cycles` | B4 |  |  | hot TB-graph cycles (self-loop 0x1d5a supplies 74% of X1 dead stores, SST §7.3) bound re… |
| 22 | `da` | n/a |  |  | guest memory accesses are ordered effects; no loop restructuring |
| 23 | `debug-ata` | B4 |  |  | concept and need match exactly: when a B4 pass deletes an env store, the recovery map mu… |
| 24 | `demanded-bits` | now |  |  | host EFLAGS demanded/liveness is mandatory before any L1 patch (guards, nops, test remov… |
| 25 | `domfrontier` | concept |  |  | only needed to build SSA over env slots; regions are a few TBs with ~20 slots, so per-sl… |
| 26 | `domtree` | B3 | down | B4 | over the region graph (TB versions) and, when lifted, the intra-TB host CFG: entry guard… |
| 27 | `func-properties` | n/a |  |  | ML features; code-metrics (row 93) gives the statistics we need |
| 28 | `gc-function` | n/a |  |  | no GC |
| 29 | `inliner-size-estimator` | n/a |  |  | exact host bytes of every TB are known; clone size is measured, not estimated |
| 30 | `lazy-value-info` | B3 | down |  | per-edge facts on guest registers (e.g |
| 31 | `loops` | B4 |  |  | env-slot promotion across a hot loop (keep flags/regs in host registers across iteration… |
| 32 | `memdep` | B4 |  |  | per-env-slot reaching definitions across a region: first-use loads that a predecessor ha… |
| 33 | `memoryssa` | concept |  |  | "every helper call, qemu_ld/st, exit and entry check is a MemoryDef/Use of the env state… |
| 34 | `no-op-function` | n/a |  |  | test plumbing |
| 35 | `opt-remark-emit` | now |  |  | PassResult.reason + ledger; B3/B4 need per-site missed/rejected remarks with stable code… |
| 36 | `phi-values` | n/a |  |  | no SSA phis at any level we construct |
| 37 | `postdomtree` | B4 |  |  | store sinking and compensation placement: a deleted store must be re-materialized on eve… |
| 38 | `regions` | concept |  |  | our regions are single-entry multiple-exit (side exits, interrupt stubs, slow paths, loo… |
| 39 | `scalar-evolution` | IR |  |  | not for B3/B4 |
| 40 | `should-not-run-function-passes` | n/a |  |  | LLVM pipeline plumbing |
| 41 | `should-run-extra-vector-passes` | n/a |  |  | LLVM pipeline plumbing |
| 42 | `ssp-layout` | n/a |  |  | no stack protector in TB code |
| 43 | `stack-safety-local` | n/a |  |  | as row 10 |
| 44 | `targetir` | B3 |  |  | host cost model for profitability (bytes, instruction count, lookup vs guard); calibrate… |
| 45 | `targetlibinfo` | B3 | keep* | F-B3 | table of helper semantics keyed by helper_id: lookup_tb_ptr (NO_WG_SE), MMU slow paths (… |
| 46 | `uniformity` | n/a |  |  | no SIMT |
| 47 | `basic-aa` | B4 | keep | F-B4 | disp(%rbp) env operands are disjoint by (offset, width); SCO §0 found 8 instruction shap… |
| 48 | `objc-arc-aa` | n/a |  |  | no ObjC |
| 49 | `scev-aa` | n/a |  |  | only guest addresses would need it, and guest accesses are never reordered |
| 50 | `scoped-noalias-aa` | n/a |  |  | no source of scope metadata; env/guest/TLB separation is by access class (row 51) |
| 51 | `tbaa` | B4 | keep | F-B4 | access classes replace types: env-global (regs/flags), env-key (thumb, condexec, hflags)… |
| 52 | `ddg` | n/a |  |  | no loop transforms |
| 53 | `iv-users` | n/a |  |  | no LSR; guest IVs live in env and cross a TB entry every iteration |
| 54 | `no-op-loop` | n/a |  |  | test plumbing |
| 55 | `ctx-prof-analysis` | concept |  |  | depth-1 context is already in the edge profile (the return edge identifies the call site… |
| 56 | `dxil-metadata` | n/a |  |  | DirectX target |
| 57 | `dxil-resource-bindings` | n/a |  |  | DirectX target |
| 58 | `dxil-resource-type` | n/a |  |  | DirectX target |
| 59 | `dxil-resources` | n/a |  |  | DirectX target |
| 60 | `ephemerals` | n/a |  |  | facts are explicit guarded records, never instructions in the code |
| 61 | `immutable-module-summary` | concept |  |  | AnalysisStore results are already immutable and content-addressed (design §5.3) |
| 62 | `ir2vec-vocab` | n/a |  |  | ML |
| 63 | `last-run-tracking` | concept |  |  | offline pipelines run once; the AnalysisStore cache key (content hashes) already avoids … |
| 64 | `libcall-lowering-info` | concept |  |  | helper binding/ABI is the .scm HELPER chunk + V7 binding check; effect semantics belong … |
| 65 | `machine-function-info` | n/a |  |  | new-PM plumbing |
| 66 | `reg-usage` | n/a |  |  | the only contract with helpers is SysV; a tighter per-helper clobber set would bind to o… |
| 67 | `runtime-libcall-info` | concept |  |  | as row 64 |
| 68 | `should-run-extra-simple-loop-unswitch` | n/a |  |  | LLVM pipeline plumbing |
| 69 | `static-data-profile-info` | concept |  |  | per-TB constant pools hold helper addresses read on every helper call (4637 call pool(%r… |
| 70 | `component:predicate-info` | B3 | down |  | cmp r0,#0; beq gives r0 == 0 on the taken TB edge; with LVI feeds branch folding in cont… |
| 71 | `component:must-execute` | B3 | keep* | F-B3 | (a) specialized entry guards must fail "before effects" (spec §4); (b) fault prefixes: e… |
| 72 | `component:mem-derefs` | concept | missed+ | F-B4 guest.nofault | reduces to a rule: env, host frame and pool are always dereferenceable; guest memory nev… |
| 73 | `component:cost-model` | B3 |  |  | same analogue as row 44 (cost.host) |
| 74 | `component:inline-cost` | B3 | merge | F-B3 advisor | specialization cost-benefit: bytes of TB versions + guard vs |
| 75 | `component:alias-sets` | concept |  |  | env slots partition trivially by (offset, width); guest memory is one opaque set that is… |
| 76 | `component:memoryssa-walker` | concept |  |  | as row 33; clobber queries are answered by per-slot reaching defs (row 32) |
| 77 | `component:stack-lifetime` | concept |  |  | host-frame spills are rare (SCO X7 open); the useful twin is env-slot liveness, which be… |
| 78 | `component:delinearization` | n/a |  |  | no array dependence analysis |
| 79 | `component:loop-cache-cost` | n/a |  |  | our cache concern is i-cache/iTLB layout (B2), not loop data locality |
| 80 | `component:loopnest` | n/a |  |  | no loop-nest transforms |
| 81 | `component:cfg-sccs` | now |  |  | SCCs of the TB graph: spec §1 "strongly connected hot regions"; usable for B2 clustering… |
| 82 | `component:structural-hash` | now |  |  | content_hash, guest_hash, tb_uid; B3/B4 extend it to callee summaries and regions for in… |
| 83 | `component:value-tracking` | done |  |  | z_mask/s_mask known-bits inside an EBB (TB §4.3); cross-TB only via B3 entry facts re-ru… |
| 84 | `component:instruction-simplify` | done |  |  | tcg_optimize single-op identities and folding (TB §4.2-4.3); B3 reuses TCG at L3 instead… |
| 85 | `component:constant-folding` | done | missed+ | F (literal consts) | TB §4.2 (incl |
| 86 | `component:capture-tracking` | concept |  |  | env escapes into every helper call (mov %rbp,%rdi), so the question is fully replaced by… |
| 87 | `component:memory-location` | B4 |  |  | EnvLoc(class, offset, width) for every env access at L1/L2; the basis of rows 15/47/51 |
| 88 | `component:iterated-dominance-frontier` | concept |  |  | as row 25 |
| 89 | `component:guard-utils` | B3 | keep* | F-B3 guard | representation of specialized-entry guards: full-key + fact guard that, when false, exit… |
| 90 | `component:indirect-call-promotion-analysis` | B3 | keep* | F-B3 | hot successors per goto_ptr exit: hottest return successor covers 70.79% of returns (M3)… |
| 91 | `component:sparse-propagation` | B3 | down |  | worklist solver propagating facts over the region graph (SCCP-style); transfer functions… |
| 92 | `component:value-lattice` | B3 |  |  | the fact lattice for guest regs, flags and key fields (thumb, condexec, hflags-derived) |
| 93 | `component:code-metrics` | now |  |  | host bytes/insns per TB and per path; budgets and the hot-set denominator (99% hot set =… |
| 94 | `component:block-frequency-impl-irreducible` | n/a |  |  | counts are exact; estimating counts for unknown TBs would turn unknown into a number, wh… |
| 95 | `component:synthetic-counts-utils` | n/a |  |  | same: absent counts are unknown; B2 already places unknown TBs in their own section |

### A.6 `codegen-generic` (228 rows; reasons in the maintainer's notes (not in the repository), skeptic in `challenge-codegen-generic.md`)

| # | pass | assessor | skeptic | → shortlist | scopt analogue or short reason |
|---:|---|---|---|---|---|
| 1 | `jmc-instrumenter` | n/a |  |  | No source-level debugger stepping of host TB code; production code must not gain calls (… |
| 2 | `lower-emutls` | n/a |  |  | TB code has no TLS; the only per-vCPU state is env in %rbp. |
| 3 | `shadow-stack-gc-lowering` | n/a |  |  | No GC; the guest stack is guest RAM, not host frames. |
| 4 | `global-merge` | done |  |  | All guest state is already one struct (CPUARMState) addressed off %rbp with disp8/disp32… |
| 5 | `callbrprepare` | n/a |  |  | No inline asm in TB code. |
| 6 | `codegenprepare` | concept |  |  | TCG selects per op, so there is no block-local selector to prepare for |
| 7 | `dwarf-eh-prepare` | n/a |  |  | Guest faults leave by siglongjmp (SST §4 L6/L7); no unwinding through TB frames. |
| 8 | `expand-large-div-rem` | n/a |  |  | No such widths; ARM udiv/sdiv are helper calls (TB-§9.7). |
| 9 | `expand-large-fp-convert` | n/a |  |  | cortex-m3 has no FPU; 0 xmm/ymm instructions in the snapshot (TB-§10). |
| 10 | `expand-memcmp` | n/a |  |  | Guest memcmp is guest code whose loads are guest memory events that must stay (N7); TBs … |
| 11 | `gc-lowering` | n/a |  |  | No GC. |
| 12 | `indirectbr-expand` | B3 | keep* | B3-7 | goto_ptr exits are the indirect branches here: tbb at 0x2a36 (7 targets, 17.99% of looku… |
| 13 | `interleaved-access` | n/a |  |  | No vector IR. |
| 14 | `interleaved-load-combine` | n/a |  |  | It would merge guest loads, which are observable events (N7); no vectors. |
| 15 | `safe-stack` | n/a |  |  | No host stack objects in TBs. |
| 16 | `select-optimize` | n/a |  |  | 18 cmov in 1698 TBs (TB-§10) |
| 17 | `sjlj-eh-prepare` | n/a |  |  | QEMU's sigsetjmp lives in the C main loop, not in TB code. |
| 18 | `stack-protector` | n/a |  |  | No host stack buffers in TBs. |
| 19 | `typepromotion` | done |  |  | TCG ops are i32/i64; redundant ext8/16 after narrow loads are removed by the z_mask/s_ma… |
| 20 | `wasm-eh-prepare` | n/a |  |  | Not this target. |
| 21 | `hardware-loops` | n/a |  |  | x86 has none; guest loops span TBs and keep their entry checks (TB-§2.4). |
| 22 | `win-eh-prepare` | n/a |  |  | Not this target. |
| 23 | `atomic-expand` | n/a |  |  | One vCPU (spec §3); TCG lowers guest ldrex/strex itself. |
| 24 | `complex-deinterleaving` | n/a |  |  | No vectors. |
| 25 | `expand-ir-insts` | n/a |  |  | No such ops. |
| 26 | `expand-reductions` | n/a |  |  | No vectors. |
| 27 | `free-machine-function` | n/a |  |  | Pass-manager utility. |
| 28 | `global-merge-func` | concept |  |  | Analogue: fold near-identical TBs (suffix-TB variants, exit stubs) (N28) |
| 29 | `inline-asm-prepare` | n/a |  |  | No inline asm. |
| 30 | `mir-check-debugify` | now | keep | F | Validator: after every L1 rewrite, each host insn tagged with a guest origin keeps it, a… |
| 31 | `mir-debugify` | now | keep | F | The tagging half of row 30: tag every host insn with (unit, guest insn index, .scm site … |
| 32 | `mir-strip-debug` | n/a |  |  | No debug info. |
| 33 | `pre-isel-intrinsic-lowering` | n/a |  |  | No such intrinsics. |
| 34 | `print-mir` | n/a |  |  | Tooling. |
| 35 | `replace-with-veclib` | n/a |  |  | No vectors. |
| 36 | `static-data-annotator` | now |  |  | Same B2.1 candidate as static-data-splitter (§2.21). |
| 37 | `unreachableblockelim` | done |  |  | reachable_code_pass (TB-§5.1) |
| 38 | `bb-path-cloning` | B3 | down | B3-6 | Analogue: per-edge continuation versions (REP M4: the top 50 return edges cover 89.68% o… |
| 39 | `bbsections-prepare` | now |  |  | cluster-hot@1 |
| 40 | `bbsections-profile-reader` | concept |  |  | scopt has its own exact Profile v1 importer and identity join (B1 §3.6). |
| 41 | `branch-folder` | now |  |  | Nothing to do inside a TB (TCG removes br-to-next, TB-§5.1) |
| 42 | `branch-relaxation` | concept |  |  | rel32 reaches everywhere inside the arena; B2's NativeLinker already rejects ext rel32 o… |
| 43 | `break-false-deps` | n/a |  |  | 0 SSE/AVX instructions; TCG already emits xor before setcc (TB-§6); only 16 bsr. |
| 44 | `CFGuardLongjmp` | n/a |  |  | Windows CFG. |
| 45 | `cfi-fixup` | n/a |  |  | TB code carries no per-function DWARF CFI, and the host never unwinds through TBs. |
| 46 | `cfi-instr-inserter` | B4 |  |  | Validator analogue: on every edge into a member or version, "where each guest value live… |
| 47 | `dead-mi-elimination` | B4 | keep* | B4-7 | TCG does this per TB (TB-§5.2) |
| 48 | `detect-dead-lanes` | n/a |  |  | Flags are separate 32-bit globals; bit-level demand is a TCG-IR question (row 204). |
| 49 | `EHContGuardCatchret` | n/a |  |  | Windows. |
| 50 | `early-ifcvt` | IR | keep* | D-1 | IT-block skips (arm_skip_unless, translate.c:3736-3740) of pure ALU insns → movcond |
| 51 | `early-if-predicator` | n/a |  |  | x86 has only cmov (covered by row 50). |
| 52 | `edge-bundles` | n/a |  |  | Register-allocator infrastructure; see greedy. |
| 53 | `postrapseudos` | n/a |  |  | LLVM-internal. |
| 54 | `expandvp` | n/a |  |  | No vectors. |
| 55 | `fentry-insert` | n/a |  |  | Production code must not gain instrumentation (spec §5). |
| 56 | `finalize-isel` | n/a |  |  | LLVM-internal. |
| 57 | `fixup-statepoint-caller-saved` | B4 | keep* | B4-3 | Analogue D4/X2: values B4 keeps only in host registers must be written to env inside the… |
| 58 | `funclet-layout` | n/a |  |  | Windows EH. |
| 59 | `gc-empty-basic-blocks` | done |  |  | reachable_code_pass merges labels and removes br-to-next and unused labels (TB-§5.1). |
| 60 | `gc-analysis` | concept |  |  | The shape (host return address → recoverable state) is the search-data/recovery table; t… |
| 61 | `if-converter` | n/a |  |  | The x86 host cannot predicate |
| 62 | `implicit-null-checks` | concept |  |  | Analogue: drop the inline TLB check and let the host MMU trap (SIGSEGV + fault map) |
| 63 | `kcfi` | n/a |  |  | Hardening. |
| 64 | `lazy-machine-block-freq` | done | missed+ | F (intra-TB counts) | Exact TB entry and exit-slot counts come from the FastDyn profile hooks in the QEMU fork… |
| 65 | `livedebugvars` | n/a |  |  | No debug info. |
| 66 | `liveintervals` | n/a |  |  | RA infrastructure; see greedy. |
| 67 | `lrshrink` | n/a |  |  | Blocks are tiny |
| 68 | `liveregmatrix` | n/a |  |  | RA infrastructure. |
| 69 | `livestacks` | n/a |  |  | RA infrastructure. |
| 70 | `livevars` | n/a |  |  | RA infrastructure. |
| 71 | `localstackalloc` | n/a |  |  | No frame objects in TBs. |
| 72 | `mir-canonicalizer` | concept |  |  | Tooling: a canonical form of TB host code (register renaming, offsets normalized) to dif… |
| 73 | `mirfs-discriminators` | concept |  |  | Cloned versions need their own profile identity (unit + version) so later profiles map n… |
| 74 | `mir-namer` | n/a |  |  | Tooling. |
| 75 | `mir-printer` | n/a |  |  | Tooling. |
| 76 | `fs-profile-loader` | concept |  |  | Profiles here are exact software counts, not samples (project decision: software-only). |
| 77 | `regallocscoringpass` | n/a |  |  | ML RA tooling. |
| 78 | `machine-block-freq` | done | missed+ | F (intra-TB counts) | As row 64 |
| 79 | `block-placement` | now |  |  | cluster-hot@1 |
| 80 | `block-placement-stats` | now |  |  | B2/B5 report metric: weighted share of TB transitions whose target is the next placed TB… |
| 81 | `machine-branch-prob` | done | missed+ | F (intra-TB counts) | Exact counts per exit slot (N27). |
| 82 | `dot-machine-cfg` | n/a |  |  | scopt already exports graph.json (B1 §4.4). |
| 83 | `machine-cse` | IR | keep* | E-2 | Redundant TLB-check CSE (same base, same page, same access kind) plus env reload CSE |
| 84 | `machine-combiner` | n/a |  |  | No long associative chains (5 guest insns per TB); the hot CRC loop is inherently serial |
| 85 | `machine-cp` | B4 | down | B4-5 | Analogue D6: in a predecessor-specific version, replace the successor's first-use env lo… |
| 86 | `machine-cycles` | now |  |  | TB-graph analysis: cycles spanning TBs (CRC self-loop; the ~15-TB prvRxTask loop) for Fo… |
| 87 | `print-machine-cycles` | n/a |  |  | Tooling. |
| 88 | `machine-domfrontier` | n/a |  |  | Only needed to build SSA; neither host code (L1) nor TCG IR (L2) is SSA. |
| 89 | `machinedomtree` | now |  |  | TB-graph analysis: a region entry must dominate its members; compensation placement |
| 90 | `machineinstr-printer` | concept |  |  | Per-pass before/after disassembly dump in the outcome ledger (tooling). |
| 91 | `machine-function-splitter` | now |  |  | Move slow paths (127,637 B = 21% of code), exit_requested stubs and unchained exit tails… |
| 92 | `unpack-mi-bundles` | n/a |  |  | LLVM-internal. |
| 93 | `finalize-mi-bundles` | n/a |  |  | LLVM-internal. |
| 94 | `machinelicm` | B4 |  |  | On a loop region: hoist invariant env loads (e.g |
| 95 | `early-machinelicm` | IR |  |  | Hoisting computations and promoting loop-carried guest registers need virtual registers … |
| 96 | `machine-latecleanup` | now |  |  | L1: drop mov reg,imm / mov reg,[rbp+off] when the register provably still holds that val… |
| 97 | `machine-loops` | now |  |  | TB graph; like row 86 (cycles preferred, since unknown edges can make the graph irreduci… |
| 98 | `machinemoduleinfo` | n/a |  |  | LLVM-internal. |
| 99 | `machine-opt-remark-emitter` | concept |  |  | The scopt outcome ledger with stable reason codes (B1 §5.9) is the analogue and is alrea… |
| 100 | `machine-outliner` | n/a |  |  | It would put calls on hot paths (TLB check, PC update); per-site slow paths embed per-si… |
| 101 | `pipeliner` | n/a |  |  | Aimed at in-order/VLIW cores; the host is out-of-order; loops span TBs with entry checks… |
| 102 | `machinepostdomtree` | B4 |  |  | Analysis for "overwritten on every path" (D1/D5) and for choosing sink and compensation … |
| 103 | `machine-region-info` | concept |  |  | B3 regions are single-entry, multi-exit traces (side exits everywhere), not SESE; domina… |
| 104 | `machine-scheduler` | n/a |  |  | OoO host; reordering around env stores and guest accesses is constrained by S5 precision… |
| 105 | `postmisched` | n/a |  |  | As row 104. |
| 106 | `machine-sink` | B4 | keep* | B4-3 | Sink partially dead env stores (and the ops that feed them) to the exits or cold paths w… |
| 107 | `postra-machine-sink` | B4 | keep* | B4-3 | The physical-register (L1) implementation route of row 106; same candidate |
| 108 | `machine-trace-metrics` | concept |  |  | A static host cost model (uiCA/llvm-mca style) for if-conversion and region decisions (s… |
| 109 | `machine-uniformity` | n/a |  |  | GPU only. |
| 110 | `print-machine-uniformity` | n/a |  |  | GPU only. |
| 111 | `machineverifier` | now | keep | F | Model for the rewritten-code verifier: bytes decode; branch targets are instruction boun… |
| 112 | `modulo-schedule-test` | n/a |  |  | Test pass. |
| 113 | `opt-phis` | n/a |  |  | No PHIs at L1; TCG IR is not SSA. |
| 114 | `phi-node-elimination` | n/a |  |  | As row 113. |
| 115 | `patchable-function` | done |  |  | goto_tb already emits a NOP-aligned patchable jmp rel32 (TB-§6, tcg-target.c.inc:2689-27… |
| 116 | `peephole-opt` | now | keep* | D-3 | N18/N19: 1995 redundant test sites; the entry check add;test;jl in 1677/1698 TBs; the PC… |
| 117 | `post-RA-hazard-rec` | n/a |  |  | x86 needs none. |
| 118 | `post-RA-sched` | n/a |  |  | As row 104. |
| 119 | `processimpdefs` | n/a |  |  | LLVM-internal. |
| 120 | `prologepilog` | done |  |  | One shared prologue/epilogue for the whole cache; TBs have no frame (TB-§6, tcg-target.c… |
| 121 | `pseudo-probe-inserter` | concept |  |  | Rewritten code must keep stable origin anchors for profile attribution; the .scm sites a… |
| 122 | `reaching-deps-analysis` | now | keep | F-B4 | L1 analysis: "does host reg R still hold the value of guest slot s at point p" |
| 123 | `regallocbasic` | n/a |  |  | Alternative allocator; see greedy. |
| 124 | `regalloc-evict` | n/a |  |  | RA infrastructure. |
| 125 | `regallocfast` | done |  |  | This is TCG's allocator design: greedy and local, globals synced at block ends (TB-§5.3,… |
| 126 | `greedy` | IR | down | E-3 | N2/N21: region-wide allocation that keeps guest registers in host registers across membe… |
| 127 | `regalloc-priority` | n/a |  |  | RA infrastructure. |
| 128 | `RegUsageInfoCollector` | concept |  |  | Helper clobber sets follow the C ABI and are known; the useful change is at the call sid… |
| 129 | `reg-usage-propagation` | IR |  |  | Make the softmmu slow-path stub save and restore the live caller-saved registers itself,… |
| 130 | `register-coalescer` | concept |  |  | TCG's "reuse the dying input register" plus preference sets cover intra-TB copies; the c… |
| 131 | `scavenger-test` | n/a |  |  | Test pass. |
| 132 | `reg-usage-info` | n/a |  |  | LLVM-internal. |
| 133 | `removeredundantdebugvalues` | n/a |  |  | No debug info. |
| 134 | `rename-independent-subregs` | n/a |  |  | LLVM-internal. |
| 135 | `reset-machine-function` | n/a |  |  | LLVM-internal. |
| 136 | `machine-sanmd` | n/a |  |  | Sanitizers. |
| 137 | `shrink-wrap` | concept |  |  | In a region that promotes guest registers, put "load from env" / "store to env" only whe… |
| 138 | `slotindexes` | n/a |  |  | RA infrastructure. |
| 139 | `spill-code-placement` | concept |  |  | The B4 cost decision (store at A's exit vs |
| 140 | `stack-coloring` | n/a |  |  | The env layout is a fixed ABI read by QEMU C code; no frame slots. |
| 141 | `stack-frame-layout` | n/a |  |  | No frames. |
| 142 | `stackmap-liveness` | B4 | keep* | B4-6/F | At every exit and sync point: which env slots and flags the next reader needs (SST §4 L1… |
| 143 | `stack-slot-coloring` | n/a |  |  | No spill slots that matter (TB temps are in one shared frame). |
| 144 | `tailduplication` | B3 |  |  | Duplicate a small successor TB into a predecessor-specific version (return TB bx lr, 80 … |
| 145 | `early-tailduplication` | IR |  |  | Merging a TB into its predecessor so TCG's own optimizer sees both (superblocks, N14) ne… |
| 146 | `targetpassconfig` | n/a |  |  | LLVM-internal (scopt PassManager is its own). |
| 147 | `twoaddressinstruction` | done |  |  | The backend uses LEA for distinct destinations and two-operand forms otherwise (TB-§6). |
| 148 | `unreachable-mbb-elimination` | B3 |  |  | In L1 specialized clones, after fact-based BranchFolding, the dead arm with its exit stu… |
| 149 | `virtregmap` | n/a |  |  | RA infrastructure. |
| 150 | `virtregrewriter` | n/a |  |  | RA infrastructure. |
| 151 | `xray-instrumentation` | n/a |  |  | No production instrumentation; FastDyn callbacks are already the instrumentation. |
| 152 | `cseinfo` | n/a |  |  | GlobalISel infrastructure. |
| 153 | `gisel-known-bits` | done |  |  | z_mask/s_mask tracking (TB-§4.3). |
| 154 | `irtranslator` | n/a |  |  | GlobalISel front end. |
| 155 | `instruction-select` | done |  |  | Per-op templates (TB-§6). |
| 156 | `legalizer` | done |  |  | Generation-time expansion (TB-§3). |
| 157 | `loadstore-opt` | now |  |  | L1: merge adjacent constant env stores (e.g |
| 158 | `localizer` | n/a |  |  | GlobalISel. |
| 159 | `regbankselect` | n/a |  |  | GlobalISel. |
| 160 | `livedebugvalues` | n/a |  |  | No debug info. |
| 161 | `regallocpbqp` | n/a |  |  | Alternative allocator; see greedy. |
| 162 | `post-inline-ee-instrument` | n/a |  |  | No production instrumentation. |
| 163 | `prologepilog-code` | done |  |  | As row 120. |
| 164 | `regalloc` | n/a |  |  | LLVM-internal. |
| 165 | `machine-block-match-infer` | concept |  |  | Stale-profile matching across snapshots |
| 166 | `EHContGuardTargets` | n/a |  |  | Windows. |
| 167 | `init-undef` | n/a |  |  | LLVM-internal. |
| 168 | `insert-code-prefetch` | n/a |  |  | Needs an instruction-prefetch ISA extension (PREFETCHI) [uncertain whether the host has … |
| 169 | `mir2vec-vocab-analysis` | n/a |  |  | ML embedding tooling. |
| 170 | `print-mir2vec-vocab` | n/a |  |  | Tooling. |
| 171 | `print-mir2vec` | n/a |  |  | Tooling. |
| 172 | `machine-block-hash` | concept |  |  | Identity for clones and rewritten blocks; guest hash + unit id already serve. |
| 173 | `remove-loads-into-fake-uses` | n/a |  |  | Debug-only. |
| 174 | `static-data-splitter` | now |  |  | Move TB structs (152 B each, inline before every TB), per-TB pools (17,704 B) and search… |
| 175 | `windows-secure-hot-patch` | n/a |  |  | Windows. |
| 176 | `machine-register-class-info` | n/a |  |  | LLVM-internal. |
| 177 | `finalizebundle-test` | n/a |  |  | Test pass. |
| 178 | `ssa-machine-scheduler` | n/a |  |  | As row 104. |
| 179 | `no-op-machine-function` | concept |  |  | identity@1 |
| 180 | `print<gisel-value-tracking-fpclass>` | n/a |  |  | Tooling. |
| 181 | `print<gisel-value-tracking>` | n/a |  |  | Tooling. |
| 182 | `print<livedebugvars>` | n/a |  |  | Tooling. |
| 183 | `print<live-intervals>` | n/a |  |  | Tooling. |
| 184 | `print<live-stacks>` | n/a |  |  | Tooling. |
| 185 | `print<machine-block-freq>` | n/a |  |  | Tooling. |
| 186 | `print<machine-block-hash>` | n/a |  |  | Tooling. |
| 187 | `print<machine-branch-prob>` | n/a |  |  | Tooling. |
| 188 | `print<machine-dom-tree>` | n/a |  |  | Tooling. |
| 189 | `print<machine-loops>` | n/a |  |  | Tooling. |
| 190 | `print<machine-post-dom-tree>` | n/a |  |  | Tooling. |
| 191 | `print<machine-regions>` | n/a |  |  | Tooling. |
| 192 | `print<reaching-def>` | n/a |  |  | Tooling. |
| 193 | `print<slot-indexes>` | n/a |  |  | Tooling. |
| 194 | `print<virtregmap>` | n/a |  |  | Tooling. |
| 195 | `require-all-machine-function-properties` | n/a |  |  | Test pass. |
| 196 | `trigger-crash-machine-function` | n/a |  |  | Test pass. |
| 197 | `trigger-verifier-error (machine)` | concept |  |  | Negative-test analogue: deliberately corrupt a candidate (drop a search row, misplace a … |
| 198 | `verify<machine-cycles>` | n/a |  |  | LLVM self-check. |
| 199 | `verify<machine-dom-tree>` | n/a |  |  | LLVM self-check. |
| 200 | `verify<machine-trace-metrics>` | n/a |  |  | LLVM self-check. |
| 201 | `component:selectiondag-dag-combiner` | IR |  |  | L2 multi-op combines on captured TCG ops (N22: (x+a)+b, chains of PC arithmetic across f… |
| 202 | `component:selectiondag-isel-driver` | done |  |  | TCG's per-op selection (TB-§6) |
| 203 | `component:selectiondag-legalizer` | done |  |  | TB-§3 (generation-time expansion per host capability). |
| 204 | `component:selectiondag-target-lowering-simplify` | IR |  |  | Demanded-bits on TCG ops: TCG has known bits (TB-§4.3) but no demanded bits (e.g |
| 205 | `component:selectiondag-scheduler` | n/a |  |  | As row 104. |
| 206 | `component:selectiondag-statepoint-lowering` | concept |  |  | Deopt with recorded locations ≈ a B3/B4 side exit that materializes state |
| 207 | `component:fast-isel` | done |  |  | TCG is essentially a FastISel-class selector (TB-§6). |
| 208 | `component:switch-lowering` | B3 | keep* | B3-7 | Lowering of a multi-target goto_ptr inline cache (the 7-target tbb, polymorphic return T… |
| 209 | `component:tail-duplicator` | B3 |  |  | The cloning utility behind row 144; same candidate. |
| 210 | `component:branch-folder-tail-merging` | now |  |  | As row 41 (same candidate). |
| 211 | `component:greedy-ra-splitting-spilling` | n/a |  |  | RA infrastructure; reviewed under greedy. |
| 212 | `component:register-scavenger` | now | keep | F-B4 | Every L1 pass that inserts code (guards, compensation stores, cmov scratch) needs a dead… |
| 213 | `component:live-phys-regs` | now | keep | F-B4 | The basic L1 analysis: EFLAGS liveness for flag reuse, register liveness for dead ops an… |
| 214 | `component:rdf-register-dataflow` | concept |  |  | Too heavy for 41-insn TBs; liveness plus reaching defs (rows 122, 213) suffice. |
| 215 | `component:machine-ssa-updater` | n/a |  |  | Neither L1 nor TCG IR is SSA. |
| 216 | `component:anti-dep-breaker` | n/a |  |  | No scheduling (row 104). |
| 217 | `component:macro-fusion-mutation` | concept |  |  | Cost-model input for row 116: removing a test only saves a uop if the new producer+jcc p… |
| 218 | `component:stackmaps` | B4 | keep* | B4-6/F | The recovery-map format B4 needs: for each faultable host pc and exit stub, where each g… |
| 219 | `component:faultmaps` | done |  |  | Per-TB search data maps host pc → guest pc/condexec (TB-§8); B1 §4.9 extends it to regio… |
| 220 | `component:machine-stable-hash` | concept |  |  | Dedupe continuation versions (REP §9.6: key versions by return insn + continuation key) … |
| 221 | `component:machine-size-opts` | concept |  |  | Already scopt policy: per-pass code-growth budgets, and growth only on hot units. |
| 222 | `component:execution-domain-fix` | n/a |  |  | No SSE/AVX in TB code. |
| 223 | `component:ml-regalloc-advisors` | n/a |  |  | RA infrastructure. |
| 224 | `component:asm-printer-emission` | done |  |  | TB code starts are 64-byte aligned (TB-§7); pool and patch-site alignment exist (TB-§5.4… |
| 225 | `component:gisel-combiner` | IR |  |  | As row 201 (same candidate). |
| 226 | `component:gisel-cse-builder` | n/a |  |  | GlobalISel infrastructure. |
| 227 | `component:codegen-pass-builder-newpm` | concept |  |  | scopt's PassManager with descriptors, preserves/invalidates and budgets (B1 §5.6) is the… |
| 228 | `component:mc-assembler-layout-relaxation` | now | keep* | D-3 | The L1 re-encoder every size-changing pass needs (§0), plus N17: 6655/6655 jcc are 6-byt… |

### A.7 `codegen-x86` (79 rows; reasons in the maintainer's notes (not in the repository), skeptic in `challenge-codegen-x86.md`)

| # | pass | assessor | skeptic | → shortlist | scopt analogue or short reason |
|---:|---|---|---|---|---|
| 1 | `x86argumentstackrebase` | n/a |  |  | TBs have no frame and no stack arguments; the shared prologue owns the only frame (TB §6… |
| 2 | `x86-avoid-SFB` | now | keep* | B4-1 | The algorithm does not transfer (0 vector instructions), but the hazard it targets occur… |
| 3 | `x86-avoid-trailing-call` | concept |  |  | No Windows unwinder |
| 4 | `x86-cf-opt` | n/a |  |  | Helper arguments go in SysV registers (example TB §9.7) |
| 5 | `x86-cmov-conversion` | concept |  |  | 29 static cmov (weight 1.1e6) and no loops inside TBs |
| 6 | `x86-compress-evex` | n/a |  |  | 0 EVEX/VEX instructions in TB code (TB §10); Cortex-M3 has no FP or vector unit to trans… |
| 7 | `x86-domain-reassignment` | n/a |  |  | scalar GPR code only; nothing to move into k-registers |
| 8 | `x86-pseudo` | n/a |  |  | TCG emits final machine bytes; there are no pseudos in captured code |
| 9 | `fastpretileconfig` | n/a |  |  | no matrix operations in the guest or in TCG output (the host has AMX; that is irrelevant… |
| 10 | `fasttileconfig` | n/a |  |  | as row 9 |
| 11 | `x86-fixup-bw-insts` | n/a |  |  | TCG already uses movzbl/movzwl for narrow loads (TB §6) |
| 12 | `x86-fixup-inst-tuning` | n/a |  |  | in LLVM 18 every rule is an AVX-512 shuffle/unpack (VPERMILP, VUNPCK; X86FixupInstTuning… |
| 13 | `x86-fixup-LEAs` | done |  |  | TCG already picks LEA for 3-operand add/shl and ADD otherwise (TB §6 "3-operand arithmet… |
| 14 | `x86-fixup-setcc` | done |  |  | TCG emits xor before setcc (TB §6 last bullet, tcg-target.c.inc:1840-1849) |
| 15 | `x86-fixup-vector-constants` | n/a |  |  | no vector constants; the constant pool holds helper addresses (TB §5.4) |
| 16 | `x86-flags-copy-lowering` | concept |  |  | TCG never copies EFLAGS; ARM flags already live as four env words (TB §2.1) |
| 17 | `x86-codegen` | n/a |  |  | no x87 code |
| 18 | `x86-isel` | IR | down | E (concept) | TCG selects one fixed template per op (TB §6, N19/N20) |
| 19 | `x86-lvi-load` | n/a |  |  | not part of the contract; would only add cost; baseline TCG code has none |
| 20 | `x86-lvi-ret` | n/a |  |  | TBs contain no ret (exits are jumps); not part of the contract |
| 21 | `lower-amx-intrinsics` | n/a |  |  | as row 9 |
| 22 | `lower-amx-type` | n/a |  |  | as row 9 |
| 23 | `lowertilecopy` | n/a |  |  | as row 9 |
| 24 | `x86-optimize-LEAs` | concept |  |  | Inside a TB the only repeated address arithmetic is the per-access TLB check (lea size-1… |
| 25 | `x86-partial-reduction` | n/a |  |  | no vector reductions; guest loops span TBs and their memory accesses are guest events |
| 26 | `tilepreconfig` | n/a |  |  | as row 9 |
| 27 | `x86-return-thunks` | n/a |  |  | no ret in TB code; hardening is not in the contract |
| 28 | `x86-seses` | n/a |  |  | hardening; would multiply the cost of every env access |
| 29 | `x86-slh` | n/a |  |  | hardening; not in the contract |
| 30 | `x86-execution-domain-fix` | n/a |  |  | no SSE code |
| 31 | `tileconfig` | n/a |  |  | as row 9 |
| 32 | `x86-winehstate` | n/a |  |  | Linux host, no Windows EH |
| 33 | `x86-vzeroupper` | n/a |  |  | TB code never touches YMM/ZMM, so the AVX→SSE transition penalty cannot arise from it |
| 34 | `x86-indirect-branch-tracking` | n/a |  |  | The host CPU has IBT, but nothing indicates it is enforced for this user process [U: Lin… |
| 35 | `x86-dyn-alloca-expander` | n/a |  |  | no stack allocation in TBs |
| 36 | `x86-discriminate-memops` | done | n/a (nit) |  | FastDyn capture already gives every guest access an OpMark and an Ldst ordinal joined to… |
| 37 | `x86-retpoline-thunks` | n/a |  |  | The host has eIBRS (Linux bugs line lists eibrs_pbrsb) and the contract has no Spectre r… |
| 38 | `x86-insert-wait` | n/a |  |  | no x87 |
| 39 | `x86-insert-prefetch` | n/a |  |  | It needs hardware cache-miss sampling, which is excluded (profiles are software-only) |
| 40 | `x86-pad-short-functions` | n/a |  |  | Atom-only; TBs have no ret |
| 41 | `x86-global-base-reg` | n/a |  |  | 64-bit code uses RIP-relative addressing; TCG already does (TB §6 "Constants") |
| 42 | `x86-cleanup-local-dynamic-tls` | n/a |  |  | no TLS access in TB code |
| 43 | `x86-asm-printer-begin` | n/a |  |  | emission plumbing; scopt emits bytes directly |
| 44 | `x86-asm-printer-end` | n/a |  |  | as row 43 |
| 45 | `x86-fence-nontemporal-stores` | n/a |  |  | no non-temporal stores; one vCPU |
| 46 | `x86-asm-printer` | n/a |  |  | emission plumbing (scopt's capstone-based dumps already play the printer role for review… |
| 47 | `x86-lfi-rewrite` | n/a |  |  | no sandboxing contract; bundle alignment would inflate the code |
| 48 | `x86-postlegalizer-combiner-pass` | n/a |  |  | only meaningful if an L2 route used LLVM GlobalISel; the combining idea is covered by ro… |
| 49 | `x86-prelegalizer-combiner-pass` | n/a |  |  | as row 48 |
| 50 | `x86-suppress-apx-for-relocation` | n/a |  |  | no APX on this host or in TCG output |
| 51 | `x86-wineh-unwindv2` | n/a |  |  | no Windows unwind info |
| 52 | `mc:x86-instruction-relaxation` | now | down | D-3 backlog | TCG emits every forward jcc as rel32 (TB §6, 6655/6655) |
| 53 | `mc:x86-branch-alignment-jcc-erratum` | n/a |  |  | This host (Sapphire Rapids) is not a Skylake-derived core [U: affected-model list from m… |
| 54 | `mc:x86-prefix-padding` | now | reject |  | Absorbs the 1942 goto_tb alignment NOPs (exact weight 4.57e8, about 1.3% of model instru… |
| 55 | `mc:x86-nop-padding` | B4 | keep* | B4-2 | The NOP encoder is what makes B4 tier (a) an in-place, same-size, v5-compatible deletion… |
| 56 | `mc:x86-macro-fusion-awareness` | now | down | F (provenance) | As a cost-model component: a peephole must not split a fused pair |
| 57 | `mc:x86-encoding-optimization` | done |  |  | TCG uses imm8 forms, inc/dec, xor for zero, mov r32,imm32 and movzx (TB §6 "Immediates",… |
| 58 | `mc:x86-code-emitter` | done |  |  | TCG's tcg_out_sib_offset picks disp8 when it fits (tcg-target.c.inc:786-863) |
| 59 | `component:x86-mcinst-lower` | B4 | keep* | B4-6 | The stackmap and patchpoint concepts are the closest LLVM model of what B3/B4 need |
| 60 | `component:x86-instrinfo-optimize-compare` | now | down | D-3 backlog | 2599 sites (2201 in the entry check) |
| 61 | `component:x86-instrinfo-fold-memory-operand` | now | down | D-3 backlog | 2125 PC-update triples → addl $k,0x3c(%rbp) (N19) |
| 62 | `component:x86-instrinfo-three-address-lea` | done |  |  | TB §6 "3-operand arithmetic": distinct-destination add/shl already become lea |
| 63 | `component:x86-instrinfo-commute-remat` | B4 | keep* | B4-6 | The remat concept decides which B4-deleted or B4-sunk env stores need no spill |
| 64 | `component:x86-instrinfo-partial-reg-deps` | done |  |  | the only such case TCG emits (setcc) already gets xor (TB §6, tcg-target.c.inc:1840-1849… |
| 65 | `component:x86-instrinfo-machine-combiner-patterns` | IR | down | E (concept) | Reassociating dependent add/xor chains for ILP needs the region's ops |
| 66 | `component:x86-instrinfo-branch-analysis` | now | down | D-3 backlog | scopt needs the same primitives to make the hotter goto_tb exit the fallthrough inside a… |
| 67 | `component:x86-instrinfo-outliner-hooks` | n/a |  |  | Slow paths and exit stubs embed per-TB constants (raddr, oi, own-struct LEA), so identic… |
| 68 | `component:x86-isel-lowering-dag-combines` | IR | down | E (concept) | It is the reference catalogue for a region-level selector over captured ops (flag reuse … |
| 69 | `component:x86-isel-dag-peepholes` | IR | down | E (concept) | as row 68 |
| 70 | `component:x86-fast-isel` | done |  |  | TCG's backend is exactly a FastISel-style per-op template selector (TB §6: "one TCG op →… |
| 71 | `component:x86-frame-lowering` | done |  |  | one shared prologue/epilogue for the whole code buffer; TBs have no per-TB frame (TB §6 … |
| 72 | `component:x86-selectiondag-info-memops` | n/a |  |  | Guest memcpy loops are sequences of guest memory events that must stay (N7) |
| 73 | `component:x86-instcombine-intrinsic-hooks` | n/a |  |  | no x86 intrinsics anywhere in the pipeline |
| 74 | `component:x86-target-transform-info` | concept |  |  | scopt needs a host cost model for candidate acceptance (spec §4 "training profitability"… |
| 75 | `component:x86-interleaved-access-hook` | n/a |  |  | no vector code |
| 76 | `component:x86-subtarget-tuning-features` | now | down | F (provenance) | Every L1 peephole decision depends on the host core |
| 77 | `component:x86-globalisel` | n/a |  |  | not the default selector; would matter only inside an L2-via-LLVM backend, where row 18 … |
| 78 | `component:x86-asm-printer` | n/a |  |  | emission plumbing; scopt patches bytes and validates by re-decoding (B1 §5.8 V3) |
| 79 | `component:x86-known-missed-optimizations-readme` | concept |  |  | useful as a checklist for an L1 peephole catalogue |

### A.8 `bolt` (104 rows; reasons in the maintainer's notes (not in the repository), skeptic in `challenge-bolt.md`)

| # | pass | assessor | skeptic | → shortlist | scopt analogue or short reason |
|---:|---|---|---|---|---|
| 1 | `asm-dump` | concept |  |  | scopt already has inspect unit and meta dump |
| 2 | `fix-relaxations` | n/a |  |  | x86-64 host; TB code is emitted by TCG, never relaxed by a linker |
| 3 | `veneer-elimination` | B3 | keep* | B3-2 | Analogue, not algorithm: a static branch whose target is on another 1 KiB guest page goe… |
| 4 | `fix-riscv-calls` | n/a |  |  | Host is x86-64 |
| 5 | `print-stats` | now |  |  | A per-snapshot diagnostic: units without metadata (pinned), hot lookup sites, hot edges … |
| 6 | `profile-stats` | now |  |  | Profile v1 already checks sum(edges) == sum(entries) per phase (REP §1.2) |
| 7 | `validate-internal-calls` | now |  |  | The analogue is code addresses used as data inside a TB: the slow path's lea ra(%rip) (M… |
| 8 | `validate-mem-refs` | now |  |  | No jump tables |
| 9 | `instrumentation` | concept | missed+ | F (prep-only edge counts) | Exact software counts already come from the dispatcher hooks (sc-profile.c, REP §1.2), n… |
| 10 | `HugePage` | done | keep (check) | A-4 | tcg_region_init advises QEMU_MADV_HUGEPAGE over the whole code buffer (tcg/region.c:804-… |
| 11 | `shorten-instructions` | done |  |  | TCG's templates already pick imm8, inc/dec, movzx, xor r,r, mov r32,imm and short consta… |
| 12 | `remove-nops` | n/a |  |  | The NOPs in TB bodies (3017 bytes; 1.4% of modelled dynamic instructions, SST §7.2) alig… |
| 13 | `normalize CFG` | done |  |  | reachable_code_pass merges labels, removes br to the next label and dead tails (TB §5.1) |
| 14 | `strip-rep-ret` | n/a |  |  | TBs contain no ret |
| 15 | `identical-code-folding` | B3 | keep (cond.) | F (version dedupe) | Whole TBs cannot be folded: exit_tb returns the TB pointer, and search data, jmp lists, … |
| 16 | `specialize-memcpy` | concept |  |  | Guest memcpy is guest code |
| 17 | `inline-memcpy` | n/a |  |  | It would collapse per-access guest memory events, per-access fault PCs and per-instructi… |
| 18 | `indirect-call-promotion` | B3 | keep* | B3-3/4 | The analogue of the most valuable BOLT pass here: a guarded direct transfer at return (b… |
| 19 | `jt-footprint-reduction` | n/a |  |  | No host jump tables exist |
| 20 | `simplify-read-only-loads` | n/a |  |  | Guest literal-pool/ROM loads are guest memory events and must stay (spec §3, N7) |
| 21 | `regreassign` | now | down | D-3 backlog | TCG allocates rbx, r12-r15 first (TB §5.3) |
| 22 | `inlining` | B3 | down | B3-8 | Analogue: a call-return region |
| 23 | `PLT call optimization` | done |  |  | Helper calls are already in the GOT form call pool(%rip) (TB §5.4, §10) |
| 24 | `three way branch` | n/a |  |  | A TB ends at its single guest conditional branch (TB §2.4) |
| 25 | `reorder-blocks` | now | down | D-3 backlog | cluster-hot@1 |
| 26 | `eliminate-unreachable` | done |  |  | Inside a TB this is done by reachable_code_pass (TB §5.1) |
| 27 | `split-functions` | now | down | D-4 | Whole-TB hot/cold sections are done in B2 |
| 28 | `loop-inversion-opt` | concept |  |  | No loop exists inside a TB |
| 29 | `tail duplication` | B3 | keep* | B3-6 | The successor here is the continuation TB, copied once per incoming return edge so the c… |
| 30 | `CMOV conversion` | IR | keep* | D-1 | IT blocks are branches around one instruction (N15) |
| 31 | `fix-branches` | now | down | D-3 backlog | The re-encoder's branch fixer: recompute every intra-TB jcc/jmp after reordering or spli… |
| 32 | `reorder-functions` | now |  |  | cluster-hot@1 |
| 33 | `print dyno-stats after optimizations` | now | keep | F | The software-only evaluation backbone: profile-weighted executed host instructions, take… |
| 34 | `stoke-get-stat` | now |  |  | Analogue: export the pure host spans between effects (no guest memory access, no call, n… |
| 35 | `simplify-conditional-tail-calls` | now | down | D-3 backlog | Every exit block ends in a goto_tb jmp |
| 36 | `peepholes` | n/a |  |  | None of the three patterns occurs: no block is a lone jmp except goto_tb slots, which mu… |
| 37 | `aligner` | done | keep (control) | A-3 | TB code starts are 64-byte aligned (TB §7) and B2 keeps d % 64 == 0 |
| 38 | `reorder-data` | now | down | D-4 | Guest data layout is guest-visible, and CPUARMState layout is compiled into QEMU (hot fi… |
| 39 | `adr-relaxation` | n/a |  |  | AArch64 only |
| 40 | `long-jmp` | done |  |  | Out-of-range helper targets already use the pool-indirect form (TB §5.4) |
| 41 | `finalize-functions` | concept |  |  | No CFI per TB |
| 42 | `frame-optimizer` | B4 | keep* | B4-2/4 | Env slots are the "frame" (row 1 of §1) |
| 43 | `alloc-combiner` | n/a |  |  | TB code never adjusts rsp; the prologue sets up one shared frame (TB §5.3) |
| 44 | `retpoline-insertion` | n/a |  |  | Hardening that slows the only indirect jump (jmp %rax after lookup) |
| 45 | `assign-sections` | now |  |  | B2 NativeLinker section order hot → warm → unknown → boot → cold (B1 §5.7) |
| 46 | `patch-entries` | concept |  |  | TB entry is by key lookup into the record, so moved TBs need no trampoline |
| 47 | `inst-lowering` | n/a |  |  | Pipeline utility; scopt has no pseudo-instructions |
| 48 | `check-large-functions` | now |  |  | The same policy as B2's per-unit rollback (B1 §5.6) and the right rule for in-place B4 e… |
| 49 | `lower-annotations` | n/a |  |  | Utility |
| 50 | `clean-mc-state` | n/a |  |  | Utility |
| 51 | `pointer-auth-cfi-analyzer` | n/a |  |  | AArch64 pointer authentication |
| 52 | `estimate-edge-counts` | concept |  |  | TB-level edges are exact (REP §1.2) |
| 53 | `set dyno-stats before optimizations` | now |  |  | Pairs with row 33 (per-pass deltas) |
| 54 | `profile-quality-stats` | now |  |  | Add held-out stability of hot edges (REP §10.2) and the share of unknown status (spec §5… |
| 55 | `populate-output-functions` | n/a |  |  | Utility |
| 56 | `aarch64-relaxation` | n/a |  |  | AArch64 |
| 57 | `pointer-auth-cfi-fixup` | n/a |  |  | AArch64 |
| 58 | `riscv-long-jmp` | n/a |  |  | RISC-V |
| 59 | `pauth-gadget-scanner` | n/a |  |  | Security scan for AArch64 |
| 60 | `algo:reorder-blocks=ext-tsp` | now | down | A-2 (ablation only) | At TB level the fallthrough term is zero (no inter-TB fallthrough, §1) |
| 61 | `algo:reorder-blocks=cache` | now | down | A-2 (ablation only) | Same objective class as B2's Pettis-Hansen |
| 62 | `algo:reorder-blocks=branch` | concept |  |  | At TB level every chained transfer is taken anyway |
| 63 | `algo:reorder-blocks=normal` | now |  |  | cluster-hot@1 |
| 64 | `algo:reorder-blocks=reverse/cluster-shuffle/none` | now |  |  | scopt has reverse_layout (negative control) and the identity pipeline |
| 65 | `algo:reorder-functions=hfsort` | now | down | A-2 (ablation only) | Edge weights are exact |
| 66 | `algo:reorder-functions=hfsort+` | now | down | A-2 (ablation only) | As row 65 |
| 67 | `algo:reorder-functions=cdsort` | now | down | A-2 (ablation only) | The best-matching objective, because only distances between TB bodies matter here |
| 68 | `algo:reorder-functions=pettis-hansen` | now |  |  | cluster-hot@1 |
| 69 | `algo:reorder-functions=exec-count/random/user/none` | now | keep | A-1 | none = identity pipeline |
| 70 | `component:cache-metrics` | now |  |  | The report's "Hot-set locality (model)" and "Static locality model" sections are this (l… |
| 71 | `algo:split-strategy=profile2` | now |  |  | hotcold-partition@1 |
| 72 | `algo:split-strategy=cdsplit` | now | down | D-4 | Warm fragments map to B2's warm section |
| 73 | `algo:split-strategy=random2/randomN/all` | now |  |  | The right fault-injection test for a future splitter: split everything, then require V3/… |
| 74 | `option:split-all-cold / split-eh / split-threshold / split-align-threshold` | now | down | D-4 | Analogues: slow paths, exit_requested stubs and unchained tails; noreturn exception-rais… |
| 75 | `analysis:dataflow-framework` | now |  |  | scopt needs it at L1 over each TB's host CFG built from .scm sites |
| 76 | `analysis:liveness` | now |  |  | Needed for every L1 rewrite: EFLAGS liveness for test/jcc peepholes and CMOV, scratch re… |
| 77 | `analysis:reaching-defs-uses` | B4 | keep | F | Value availability at compensation points: which host register still holds a dropped sto… |
| 78 | `analysis:reaching-insns` | now |  |  | Utility for the above |
| 79 | `analysis:dominators` | B3 | keep | F | Inside a TB, trivial |
| 80 | `analysis:stack-pointer-tracking` | n/a |  |  | Host rsp is invariant inside TBs |
| 81 | `analysis:stack-allocation` | n/a |  |  | No host stack allocations in TBs |
| 82 | `analysis:stack-available-expressions` | B4 | keep | F | = which env slot's value is available in which host register: D6 forwarding of first-use… |
| 83 | `analysis:stack-reaching-uses` | B4 | keep | F | = SST's backward "needed in memory" analysis per env slot (D1/D2/D5) |
| 84 | `analysis:frame-analysis` | B4 | keep | F | = classify every disp(%rbp) access (guest reg, flag, bookkeeping, TLB fields) plus what … |
| 85 | `analysis:reg-analysis` | B4 | keep* | F | = helper/callback effect summaries: env slots and host registers each helper may read or… |
| 86 | `analysis:dataflow-info-manager` | now |  |  | scopt AnalysisStore with declared preserve/invalidate sets (B1 §5.3, §5.6) |
| 87 | `analysis:call-graph` | B3 | keep | F | The guest call graph from bl sites + return edges, with exact weights |
| 88 | `analysis:mcf-profile-inference` | concept |  |  | As row 52: exact TB-level counts make it unnecessary there, and it cannot resolve intra-… |
| 89 | `opt:shrink-wrapping` | B4 | keep* | B4-3 | Analogue: sink state stores from the hot path (e.g |
| 90 | `profile:data-aggregator` | n/a |  |  | Hardware counters are excluded by the task rules, and perf is blocked on this host (SST … |
| 91 | `profile:fdata-reader` | n/a |  |  | Profile v1 has its own identity-keyed format (B1 §3.8) |
| 92 | `profile:yaml-reader-writer` | concept |  |  | Already realized by Profile v1's identity join: unit keys + guest hashes, not host offse… |
| 93 | `profile:stale-profile-matching` | concept |  |  | The spec forbids silent application of mismatched profiles (§5) and requires recollectio… |
| 94 | `profile:bolt-address-translation` | B3 | keep* | F | Layout is transparent to Profile v1 (keyed by TB identity) |
| 95 | `profile:heatmap` | now |  |  | Profile-weighted arena heatmap per artifact |
| 96 | `profile:instrumentation-runtime` | concept |  |  | sc-profile.c dispatcher counting in preparation runs only |
| 97 | `component:dyno-stats` | now | keep | F | Implementation behind rows 33/53 |
| 98 | `infra:rewrite-instance` | concept |  |  | scopt's importer/NativeLinker/ValidateAndPackage play this role over .sc + .scm, not ELF |
| 99 | `infra:binary-function-cfg-jump-tables` | now |  |  | Per-TB host CFG from .scm site kinds (LABEL_JCC, LDST_FAST_JCC, GOTO_TB, GOTO_PTR, EXIT_… |
| 100 | `infra:binary-context` | concept |  |  | scopt snapshot/WorkingSet (B1 §5.2) |
| 101 | `infra:x86-mcplus-builder` | now |  |  | scopt decodes with capstone but cannot encode |
| 102 | `infra:pass-manager` | now |  |  | scopt PassManager exists (B1 §5.6) |
| 103 | `docs:optimizing-clang` | concept |  |  | B2 mirrors its layout half |
| 104 | `propeller:basic-block-sections-workflow` | IR |  |  | The "recompile instead of rewrite" alternative = re-translation through TCG with injecte… |

### A.9 `speculation-deopt-pgo` (61 cross-cutting mechanism rows; no inventory file; reasons in the maintainer's notes (not in the repository), skeptic in `challenge-speculation-deopt-pgo.md`)

| id | mechanism | assessor | skeptic | → shortlist | scopt analogue or short reason |
|---|---|---|---|---|---|
| A1 | `llvm.experimental.deoptimize` + `"deopt"` operand bundle (incl. the… | B3 | keep | B3 deopt contract | The generic version always exists: the general TB for the current guest PC, reached … |
| A2 | `llvm.experimental.guard` | B3 | keep* | F-B3 guard | A specialized continuation entry is a guard on the full TB key plus any speculated value… |
| A3 | `llvm.experimental.widenable.condition` | B3 | keep | F-B3 (regions_enabled) | The loader-maintained `regions_enabled` byte (B1 §4.9: cleared under gdbstub, breakpoint… |
| A4 | `make-guards-explicit` | concept |  |  | scopt guards are born explicit: host code `cmp; jne general` |
| A5 | `lower-guard-intrinsic` | n/a |  |  | No intrinsic form exists to lower. |
| A6 | `lower-widenable-condition` | n/a |  |  | The `regions_enabled` byte must stay dynamic (debugger, ablation) |
| A7 | `GuardUtils` (component: `isGuard`, `parseWidenableBranch`, ...) | concept |  |  | Guards are recorded by construction in CacheRegion metadata (a guard-site table) |
| A8 | `guard-widening`, as used for entry guards | B3 | down | (guard codegen) | Merges the key guard (`regs[15]`, `thumb`, flags) and any value guard of a specialized c… |
| A9 | `guard-widening`, applied to softmmu TLB checks | IR | keep* | D-5 (measured 2.2%) | Two accesses through the same TLB entry (e.g |
| A10 | `loop-predication` | IR | down | E | For TLB checks in TB-graph loops (memcpy, CRC32), the loop entry checks that the whole a… |
| A11 | `loop-versioning`, `loop-versioning-licm` (cross-reference: ir-loop;… | IR | down | E | Same mechanism as A10: a guarded fast copy of a multi-TB loop region next to the general… |
| A12 | `place-safepoints` | done |  |  | QEMU polls at every TB entry (`translator.c:43-86`; tcg-baseline §2.5), a superset o… |
| A13 | `gc.statepoint` / `gc.relocate` / `gc.result` + `StatepointLowering`… | concept |  |  | What transfers is "the state a runtime may read at a call is explicit in the IR" |
| A14 | `rewrite-statepoints-for-gc` | concept |  |  | No relocation |
| A15 | `strip-gc-relocates` | n/a |  |  | No relocations exist. |
| A16 | `verify<safepoint-ir>` (`SafepointIRVerifier`) | B4 | keep | F (hostsym) | The validator analogue: at every observer point (S2-S6 of map-state-sync-traffic §5), ev… |
| A17 | `fixup-statepoint-caller-saved` | B4 | keep* | B4-3 | Exactly D4 of the sync-traffic map |
| A18 | `stackmap-liveness` | B4 | keep | F-B4 | Needed analysis: which host register holds which guest value at each compensation point … |
| A19 | StackMaps (component, .llvm_stackmaps) + llvm.experimental.stackm… | B4 | keep* | B4-6 | The recovery map v2: per faultable host PC, per guest-state slot, a location kind {E… |
| A20 | `llvm.experimental.patchpoint` (+ X86 shadow/NOP lowering) | done |  |  | The goto_tb slot is a patch point: NOP-aligned jmp rel32 with recorded jmp_insn_off… |
| A21 | `implicit-null-checks` + FaultMaps (component, `.llvm_faultmaps`) + … | concept |  |  | The analogue is "implicit TLB checks": guest RAM mapped so that host page faults stand f… |
| A22 | OSR exit / exit thunks (JIT practice, no LLVM pass: WebKit FTL OSR e… | B4 | keep* | B4-4 | Compensation stubs on the edge-specific exits of a B4 region |
| A23 | OSR entry (JIT practice, no LLVM pass) | concept |  |  | Re-entry happens naturally |
| A24 | `speculative-execution` | n/a |  |  | Guest loads cannot be hoisted: fault order and guest-event order |
| A25 | `callsite-splitting` (cross-reference: ir-scalar) | B3 | down | B3-6 | Mirror image at a join: a continuation reached from several return TBs that carry differ… |
| B1 | branch-prob (+ machine-branch-prob, print<branch-prob>, main … | now | keep | F | Per-exit successor probabilities from exact counts |
| B2 | block-freq (+ machine-block-freq, lazy-machine-block-freq, Bl… | B3 | keep | F-B3 | Not needed for original TBs: counts are exact |
| B3 | profile-summary / ProfileSummaryInfo (+ print-profile-summary, … | now |  |  | `hotness.v1` is a PSI analogue: the hot prefix covers 0.99 of exact steady entries, the … |
| B4 | `!prof` metadata: `branch_weights`, `function_entry_count`, `VP` val… | now |  |  | Profile v1 already has the equivalents: edge counts per exit kind, TB entries, lookup_h… |
| B5 | `synthetic-counts-propagation` (+ `SyntheticCountsUtils`) | n/a |  |  | The spec forbids treating absent counts as data: "Treat absent counts as unknown" (§5) |
| C1 | `pgo-instr-gen` | concept | missed+ (minor) | F | Profiling here counts in the dispatcher on an unchained copy |
| C2 | `instrprof` (`InstrProfilingLoweringPass`) | concept |  |  | There are no counters in generated code (spec §5: production gets no counters) |
| C3 | pgo-instr-use (incl. the CFG-hash mismatch check, no-pgo-warn-mis… | now |  |  | `ProfileView.open` already hard-rejects on bundle, contract or runtime identity, joins b… |
| C4 | CSPGO (cs-profile-generate, cs-profile-path; PGOInstrumentation… | B3 | keep | F-B3 | Re-profile the B3 artifact so that continuation versions get exact counts for B2 lay… |
| C5 | Contextual profiling (main only): `ctx-instr-gen`, `ctx-instr-lower`… | concept |  |  | Depth-1 context is free here: 100% of dynamic return targets are the address after a bl… |
| C6 | sample-profile (+ SampleContextTracker, inline replay, MIR fs-p… | n/a |  |  | No sampler: `perf_event_paranoid=4` (B1 §3) |
| C7 | `SampleProfileInference` ("profi", `sample-profile-use-profi`) | concept |  |  | Exact profiles need no inference |
| C8 | Stale-profile matching (`SampleProfileMatcher`, in-file in 18 and it… | concept |  |  | Spec §5: "mismatched profiles cannot be silently applied", and caches are recollected pe… |
| C9 | `pseudo-probe`, `pseudo-probe-update`, `pseudo-probe-inserter` | B3 | keep | F-B3 | Origin probes for versions: every cloned or specialized member carries (original Un… |
| C10 | `add-discriminators`, `mirfs-discriminators` (FS-AFDO) | concept |  |  | Identities are exact: the TB key plus suffix and icount variants |
| C11 | `prof-verify`, `prof-inject` (main only) | B3 | keep | F-B3 | Validator: after every B3 edit, each version and its fallback carry counts whose sum equ… |
| C12 | `insert-gcov-profiling` | n/a |  |  | Coverage is FastDyn's own observation contract |
| C13 | memprof, memprof-module, memprof-use, main memprof-remove-att… | n/a |  |  | Generated code makes no host heap allocations |
| C14 | `memprof-context-disambiguation` (cross-reference: ir-ipo) | concept |  |  | The idea of cloning callees per profiled context is B3's per-edge versions (M4 in map-re… |
| C15 | `copyprof`, `copyprof-module`, `copyprof-stores` (main only) | n/a |  |  | Main-only and outside LLVM 18 |
| C16 | MisExpect / `llvm.expect` checks | n/a |  |  | There are no source annotations |
| D1 | `pgo-icall-prom` (+ `ICallPromotionAnalysis`: remaining ≥30%, total … | B3 | keep* | B3-2/3/4 | The direct analogue of return-edge specialization. The `goto_ptr` exit of a return, … |
| D2 | `pgo-memop-opt` | IR | down | E | A value-profiled length at guest `memcpy` entry gives a guarded unrolled region for the … |
| D3 | `chr` (Control Height Reduction) | concept |  |  | It is speculation with an original-code fallback, which fits the deopt model |
| D4 | `hotcoldsplit` | concept |  |  | TB code has no per-TB frame |
| D5 | machine-function-splitter (mfs-psi-cutoff=999950, mfs-count-thr… | now | down | D-4 | `split-tb-cold`: move softmmu slow paths, `exit_requested` stubs and (optionally) TB str… |
| D6 | `bbsections-prepare` (BasicBlockSections) | now | down | D-4 | The mechanism under D5: a TB becomes 2-3 *fragments* (hot body, cold slow paths, exit st… |
| D7 | `bb-path-cloning` | B3 | keep* | B3-6 | Cloning (return TB →) continuation (→ next member) per hot return edge is path cloning *… |
| D8 | `bbsections-profile-reader` (Propeller cluster/path profile) | concept |  |  | scopt passes emit LAYOUT_ORDER and clone edits through ProposalBuilder; no external clus… |
| D9 | block-placement (MachineBlockPlacement, block-placement-stats, … | now | down | D-3 backlog | TB-level ordering is done in B2 (`cluster-hot`, Pettis-Hansen; `order-cold`) |
| D10 | ExtTSP layout (CodeLayout.cpp computeExtTspLayout, llc enable-ext… | now | keep (control) | A-2 | A drop-in alternative to Pettis-Hansen in `cluster-hot` |
| D11 | CDSort (CodeLayout.cpp computeCacheDirectedLayout; BOLT reorder-f… | now | keep (control) | A-2 | Fits TBs better than ExtTSP because all transfers are jumps whose cost depends on distan… |
| D12 | `cg-profile` | now |  |  | B2 `cluster-hot` already consumes the weighted TB graph: chain0, chain1 and `lookup_hit`… |
| D13 | `instrorderfile` | now |  |  | Boot-phase TBs could be ordered by first dispatch |
| D14 | MachineSizeOpts / PGSO (component), llc profile-guided-section-pr… | now |  |  | B2 sections hot/warm/unknown/boot/cold already mirror the section prefixes |
| D15 | Main only: static-data-splitter, static-data-profile-info, stat… | concept |  |  | The analogue is the per-TB constant pool: helper addresses, 17,704 bytes in the snapshot… |
