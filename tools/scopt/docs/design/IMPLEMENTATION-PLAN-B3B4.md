# Implementation plan for B3 and B4 (work packages WP16-WP30 for parallel worktrees)

This plan implements `B3-B4-design.md` (cited as §n). It continues `IMPLEMENTATION-PLAN.md` (WP0-WP15, B1/B2 done). Each work package (WP) lists its files, inputs, outputs, acceptance tests and dependencies.

Revised after the three reviews of the design draft (not in the repository); the design's §19 maps every finding to its resolution. Changes here: owners for every gate component (R8), unit/integration dependency split (R9), WP17/WP25 acceptance (R10), a powered value probe that gates timing but not builds (R11), Tier 1 optional on a separate branch (R12), a frozen gate binary (R13), per-mode fixture oracles (R14), a minimal vertical slice that executes the simplified bytes before any QEMU build (R15), plus minors m-P9…m-P13 and m-SF13, m-CE7.

## Ground rules (apply to every WP; additions to the B1/B2 rules)

### Repositories and worktrees

- **Python.** Each WP works in `git -C tools/scopt worktree add ../../a development worktree<wp> -b <wp>` and merges to `main` after review. Shared contracts (below) change only by a design-doc edit reviewed by every dependent WP.
- **QEMU.** One new worktree (the gate QEMU tree) on branch `qint-v6` created from `qint` (`git -C src/qemu worktree add ../wt/qemu-v6 -b qint-v6 qint`). **Creating and building it needs explicit maintainer approval** (as WP-QINT did). WP22 commits there and delivers patch `qemu-sc-v6-loader` (now QEMU commit `934890ed51`). The WP-QINT QEMU tree, `src/qemu` and `src/fastdyn` are never modified. Build options: same as the WP-QINT QEMU tree (`meson-info/intro-buildoptions.json`).
- **Gate binary (R13).** When WP22 is accepted, its commit is tagged `qint-v6-gate-1` and its exe sha256 is recorded in the maintainer's evaluation data (not in the repository). Every dynamic gate test (WP27 slice 1, WP28, WP29) and every `qint-v6` timing row runs on that binary. A later commit to `qint-v6` voids the dynamic gate evidence, which is then rerun in full; the sha is printed in the maintainer's evaluation report `B3-gate.md` (not in the repository) and the maintainer's evaluation report `B4-gate.md` (not in the repository).
- **Tier 1 (R12).** WP23 works on a separate branch `qint-v6-t1` created from `qint-v6-gate-1`, delivers the optional Tier-1 patch `qemu-sc-tier1` (not built; not on the QEMU branch), and its binary is used only for Tier-1 capture and `ops-xcheck`. It never runs a gate arm.
- **Fixtures.** New directory `fixtures/b3/` (WP24), built like `fixtures/fault/`.
- **Experiments.** Throw-away scripts and run outputs of WP16 and WP27 slice 0b go under the maintainer's evaluation data (not in the repository) (copies of snapshots only).

### Runtime rules

- QEMU always through `setarch x86_64 -R`, with a timeout, in a fresh run directory; `supercache_flat=false`; profiles software-only.
- **Never run `gdb` without `-nx`, and never with cwd inside a QEMU tree.**
- **Timing rule (m-P10).** Timing is allowed only in: WP16 (probe, n = 90 pairs per copy), WP22 (disabled-path cost, T-B1-0 method, ≥ 30 interleaved warm runs), WP25 (one early B4 data point, ≥ 30 pairs, reported), WP28/WP29 (reported performance rows) and the B5 campaign (WP30 defines it, does not run it). Each timing job uses at most 4 cores, runs alone (one timing job on the host at a time; check `pgrep -af 'campaign|timing' | grep -v pgrep`), and records its CPU budget in its verdict JSON.
- Every arm of a measured comparison runs on the same binary: the WP-QINT QEMU tree before the freeze, the gate binary after it; all baselines are re-collected on the gate binary (§16 D-EV-2).
- Training profiles are taken on the **base** snapshot only, never on optimized artifacts (§10.2).
- Any v5 artifact with guarded slots (WP16, WP27 slice 0b) is experimental and never published; each of its timed or checked runs needs an untimed **diag twin** (gdb `-nx` counting breakpoints on `tlb_flush*` and `tcg_commit_cpu`) that shows 0 calls after `benchmark_start`. A run without a clean twin is discarded (m-CE7).

### Shared contracts frozen by WP17/WP18 at their first merge

- `EnvLayout` v1 (field → env offset, bound to the exe sha256) (§0 G21) and the RuntimeIdentity extension (CPU model, exe sha, EnvLayout id, `tb_start_cb_possible`, `mem_get_value` fact, AC4 exclusions) (§7.2, V20);
- HostOps op kinds and `obs-contract@1` table (§7.2, §8.1), incl. the `can_do_io` direct-chain rule;
- the `.sc` v6 header use and extension table layout (§5.2-5.3), with `sc_v6.py` as the writer and `tools/scopt/independent/sc6parse.py` as the independent parser;
- typed EditKind payloads (§3.7) and the reason-code list (§3.8, §7.5);
- CalleeSummary v1, CacheRegion `multi_tb` rows (§3.3, §3.9);
- the canonical guard template (§4.1).

### Shared files and owners (m-P12)

| File | Owner WP | Others | Rule |
|---|---|---|---|
| `scopt/validate.py` (V-row registry) | WP18 | WP17 (V20), WP19 (V12-V14, V18), WP21 (V21 budgets), WP23 (V22) | WP18 merges a stub registry with every V-row id on day 1; others fill bodies |
| `scopt/formats/scm_v1.py` | WP18 (TB chunk v2, chunk 9) | WP23 (chunks 5/6) | WP23 rebases on WP18 |
| `scopt/passes/base.py`, `scopt/proposal.py` | WP18 (typed payloads, first merge) | WP21 (passes) | payload changes only through a design edit |
| `scopt/formats/bundle_v1.py` (RuntimeIdentity) | WP17 | WP26 (refusal codes) | — |
| `scopt/formats/region_v1.py`, `schemas/cache_region.v1.json`, `scopt/graph.py` | WP18 | WP21 (calls the writer) | — |
| `scopt/run/*` | WP26 | WP16 (read-only use) | — |

### Testing

`pytest` under `tests/` in scopt; dynamic tests drive QEMU through the existing harness (`scopt run`, `fixtures/lib/fdrun.py`). Every acceptance test writes its evidence under the maintainer's evaluation data (not in the repository) and a verdict JSON. Each WP's acceptance has a **unit part** (synthetic inputs or existing snapshots, runnable as soon as its unit dependencies merge) and an **integration part** (names the fixture or WP it needs) (R9). Items marked **reported outcome** produce a number in the verdict but do not block the merge (m-P9).

---

## Dependency graph

```
unit deps (solid) / integration deps (dotted ..>)

WP16 (probe, experiments) ─────────────(decision: timing + B5 priorities only)──────────────┐
WP17 ─┬→ WP18 ─┬→ WP19 ─┐                                                                   │
      │        ├────────┼→ WP21 ─→ WP27 (0a, 0b) ─→ WP27 (1) ─→ WP28 (B3 gate) ─┐            │
      ├→ WP19 ─┘        │                             ↑            ↑            ├→ WP30 ←────┘
      ├→ WP20 ──────────┘                             │            │            │
      └──────────→ WP25 (B4) ─────────────────────────┼──→ WP29 (B4 gate) ─────┘
WP18 → WP26 (harness) ────────────────────────────────┤        ↑
approval → WP22 (v6 loader, freeze gate binary) ──────┴────────┘
WP22 freeze → WP23 (Tier 1, qint-v6-t1, B3+ reported, optional)
WP24 (fixtures) ..> WP19, WP20, WP21, WP22, WP23, WP25, WP26, WP27, WP28, WP29
WP18 ..> WP19 (guard/variant mutants), WP22 (synthetic artifacts); WP19 ..> WP20 (certification)
```

**Parallel start:** WP16, WP17, WP24 start at once; WP22 starts on approval (unit part with hand-made tables). WP18, WP19 (unit) and WP20 (unit) start when WP17's contracts are merged (stubbed on its first day). WP25 and WP26 start when WP18/WP19 land their APIs. Integration parts wait for WP24's phases, which WP24 delivers in the order F-B3-1, F-B3-2…8, F-B4-1…4, the rest.

**Critical path to the B3 gate:** WP17 → WP18 → WP19 (LDST abstraction) → WP21 → WP27 slice 1 (needs WP22 frozen) → WP28.

**Decision point after WP16 (R11):** threshold = measured paired speedup ≥ 1.0 % with the 95 % CI excluding 0, evaluated separately for the guarded-slot copy and the D1 copy. The outcome sets WP25's timing scope, WP30's campaign arms and B5 priorities; it does **not** stop WP19/WP22/WP25 builds, because the spec gates B3/B4 on correctness and asks for measured benefit as a separate outcome (design D-EV-1, K1).

---

## WP16: value probe and runtime experiments (current binary, no QEMU change)

**Files:** the maintainer's evaluation data (not in the repository) (scripts + results), `tools/scopt/experiments/gce_probe.py` (≈ 350 lines: stub encoder, grow-unit with search-data move, record rewrite, v5 writer reuse; replaced by WP18's encoder once merged), the maintainer's evaluation report `WP16-probe.md` (not in the repository).

**Inputs:** compute snapshot copy (the maintainer's evaluation data (not in the repository) read-only → copied), B2 timing harness, §4.1 guard template, §15.

**Work:**
1. E-COST (R11): copy A = v5 compute copy with GENERAL guarded slots on **every admissible return and static exit in the train top set** (≤ 2 targets each; the modelled share of removed lookups is written down before timing); copy B = D1 dead stores of the hot units nop'ed (hostsym-checked by hand on those units if WP19 is not ready; otherwise by WP19). Labelled `experimental_unsound_without_v6_tlb_hook`; admitted configuration: no MPU writes, no memory remap, no gdbstub. n = 90 interleaved pairs per copy vs warm on train inputs (B2's design: half-width ≈ 0.6 %, minimum detectable effect ≈ 1 %), ≤ 4 cores. Every timed run has a clean diag twin (ground rules). Derive `c_lookup − c_guard` and the per-store saving.
2. E-DET: two warm runs each of F-B3-1, F-B3-7 (as soon as WP24 has them) and compute train-1 with `log_options="int"`; diff outputs, `-d int` traces and O2b handler records (stacked PC + SysTick CVR).
3. E-MAP / E-BP: gdb `-nx -batch -p` (cwd outside QEMU trees) counting `tlb_flush*` / `tcg_commit_cpu` calls after `benchmark_start`, reading `first_cpu->breakpoints.tqh_first`.
4. X-REHY (baseline part): coverage-enabled FastDyn config on the minimal fixture: compare coverage/log output of warm vs copy A; the variant part is WP27 slice 1 / F-B3-23.
5. X-ASYNC: finish the audit of §0 G24 for every virtual/device model used by the fixtures, including `fuzz_aflnet.c:143`, `ardupilot.c:94`, `gcs_receiver.c:322-323` (or confirm their AC4 exclusion).

**Outputs:** the maintainer's evaluation data (not in the repository) (constants with CIs, consumed by WP21 profitability); E-DET/E-MAP/E-BP/X-ASYNC verdicts; the decision-point outcome.

**Acceptance:** every probe run passes the B1 post-run proof, V10 output equality and its diag twin; the report states both measured speedups with 95 % CIs, the modelled shares, and the decision outcome under the threshold above.

**Dependencies:** none (WP24 for the F-B3 parts of E-DET; may borrow WP18's encoder).

---

## WP17: EnvLayout, HostOps, hcfg, hlive, effects, observation contract, admission identity

**Files:** `scopt/analysis/envlayout.py`, `hostops.py`, `hcfg.py`, `hlive.py`, `effects.py`, `obs_contract.py`; `scopt/formats/envlayout_v1.py` (+ schema); `scopt/formats/bundle_v1.py` (RuntimeIdentity extension: exe sha256, EnvLayout id, CPU model, `tb_start_cb_possible`, `mem_get_value_outside_mem_cb`, AC4-excluded virtuals present) (R8); V20 body in `validate.py`; tests `tests/unit/test_hostops.py`, `test_envlayout.py`, `test_runtime_identity_v2.py`.

**Inputs:** §0 G21, G30, G33; §7.1-7.2; §8.1; B1 §2.3-2.7 metadata; the maintainer's evaluation data (not in the repository); the maintainer's notes (not in the repository) §2.3.

**Outputs:**
- `EnvLayout` produced by `gdb -nx -batch -ex 'ptype/o …'` on the target binary (cwd outside QEMU trees), stored in RuntimeIdentity with the exe sha256; `plugin_mem_value_high = −0x10`.
- HostOps typed view per unit; `hcfg@1`; `hlive@1` (host register + flag liveness); `effects@1`; `obs-contract@1` incl. the `can_do_io` direct-chain rule.
- `tb_start_cb_possible` from the FastDyn config (TB-translation hooks registered by configured virtuals, twintrace mode), cross-checked against Tier 0 (prefix plugin calls); unknown ⇒ true.

**Acceptance (unit, R10):**
- HostOps on minimal, fault, blinky and compute: every `ENV_STORE` site matches a captured sync mark or explicit store of the Tier-0 metadata; the count of `rbp` operands left unclassified (pinned `unclassified_env_access`) is a **reported outcome** (target 0).
- EnvLayout offsets equal `offsets.txt` for the `qint` binary; a different exe sha256 yields `guard_layout_unbound`.
- V20: `cortex-m3` admitted, any other model `unsupported_guest`; `tb_start_cb_possible` is true for a twintrace config and a config with `variable_watch`, false for the compute config, and the Tier-0 cross-check fails when a synthetic prefix plugin call contradicts a false claim.

**Dependencies:** none.

---

## WP18: NativeLinker v2, v6 writer and parsers, regions, graph, structural V-rows

**Files:** `scopt/link.py` (v2: `grow_unit`, `place_variant`, `apply_pure_rewrite`, slot registration, guard-template emitter); `scopt/formats/sc_v6.py` (writer + reader); `tools/scopt/independent/sc6parse.py` (independent parser, written by a different engineer than `sc_v6.py`); `scopt/formats/scm_v1.py` (derived TB chunk v2 with `stub_start`, Python-only chunk 9 `VARIANT`); **`scopt/formats/region_v1.py` + `schemas/cache_region.v1.json`** (multi_tb branch, typed `GuardAtom`/`FactRef`/`extra_guards`/`state_fixups` items, §3.9); **`scopt/graph.py`** (edge kinds `guarded_slot`, `variant_link`); `scopt/passes/base.py` and `scopt/proposal.py` (typed payloads of §3.7, first merge); `scopt/validate.py` (V-row registry stub; bodies of **V5** variant relaxation, **V7** v6 clause (`loader_requirement` `v6` with a v6-capable RuntimeIdentity), **V9'** identity-v6, V11, V13 structural, V15, V16 incl. clone pointer bindings, V17, **V19**, V21) (R8); tests `tests/unit/test_link_v2.py`, `test_sc_v6.py`, `test_region_multi_tb.py`, `test_graph_b3.py`.

**Inputs:** §2, §3.4, §3.7, §3.9, §4.1, §5.2-5.4, §9.1; B1 §5.7-5.8; G1-G3.

**Outputs:** grown units with stubs after the pool (GENERAL and SPEC reset paths), moved search rows, rewritten records; relocated variants; same-length patch application with old-bytes check; v6 writer (header version 6 + `feature_flags`, ext table at 0x100, FNV checksums, `variants_max_end` with the strict bound) and the v5 writer when there is no variant and no guarded slot; multi_tb rows and graph edges.

**Acceptance (unit):**
- T-B3-1: round trip through both parsers on synthetic and real tables; each corruption case (incl. unknown feature bit, EXT_TABLE clear, `end_offset` equal to the bound) gives the loader's reason.
- T-B3-2/T-B3-3: grow every relocatable return/static unit of compute with a dummy guard and clone every relocatable unit: V1-V5, V13 (structural), V15, V16, V17 pass; derived `.scm` re-imports (V8).
- T-B3-28: on compute with synthetic slots/variants, export → import of the graph reproduces `guarded_slot`/`variant_link` edges and multi_tb rows; V19 passes; unknowns explicit.
- V7 accepts a v6 artifact only with a v6-capable RuntimeIdentity; V9' holds for identity-v6.

**Acceptance (integration):** a v5 artifact with grown units (GENERAL slots not registered) runs V10-equal on minimal and fault (current binary).

**Dependencies:** WP17.

---

## WP19: `hostsym` validator kernel

**Files:** `scopt/validate_sym/` (`state.py`, `semantics.py`, `paths.py`, `equiv.py`, `selftest.py`, `mutants.py`); V12, V13 (path-condition entailment), V14, V18 bodies; tests `tests/unit/test_hostsym.py`, `tests/mutation/`.

**Inputs:** §9.2-9.3; the maintainer's evaluation data (not in the repository), `proto-results.txt`; obs-contract and `tb_start_cb_possible` (WP17).

**Outputs:** `check_equiv(O, N, P, mu, contract)` with the revised guarded-chain rule (can_do_io forced on the original side only); `certify_fact(R', slot, field)` per R unit; `check_guarded_chain(R', slots)`; the LDST abstraction with `GuestAccess(..., env|obs(S5))`; host_abs-outside-env assertion; native `ctypes`+`mmap(PROT_EXEC)` self-test; the mutation suite of §9.3 with its static-only classification.

**Acceptance (unit, WP17 only):** prototype tests T1-T6 reproduced on the compute snapshot copy, T6 analysed (not UNSUPPORTED) with the LDST abstraction; V18 self-test passes; the mutants that need no guard emitter (fold, fact, guest/UPDATE_MEM/LOG_REG store, flag stores, brcond-reload T-B4-2b, last-insn `can_do_io`, IT-block D4, icount immediate, search row, host_abs) are 100 % rejected on synthetic units. Coverage of compute steady weight without UNSUPPORTED: **reported outcome** (target ≥ 99 %).

**Acceptance (integration, WP18):** guard-atom drops, guard-before-effect, falsified-fact link and shared-variant mutants built with WP18's emitter/placement are 100 % rejected.

**Dependencies:** WP17 (unit); WP18 (integration).

---

## WP20: return edges, facts, provenance, CalleeSummary

**Files:** `scopt/analysis/return_edges.py`, `exit_facts.py`, `patch_join.py`, `callee_summary.py`; `scopt/decode/guest_symbols.py` (guest ELF `STT_FUNC` reader, optional input); `scopt/formats/callee_summary_v1.py` (+ JSON schema); tests.

**Inputs:** §3.1-3.3; Thumb decoder `scopt/decode/thumb.py`; profile v1 (base snapshot, steady); bundle patch-plan content.

**Outputs:** admitted exits with reason codes (incl. `ldr pc,[rX]` → `indirect_multiway`, rule 6); continuation candidates ranked by train weight aggregated per (return-insn vaddr, continuation key) but proposed **per R unit**; proposer facts per R unit; provenance rows (`unsupported_contract_fact_source`, `no_patch_bindings` where they apply); `summaries/callee_summaries.jsonl` for every return unit with `patches[]` and optional `callee`.

**Acceptance (unit):** on compute: the natural `prvOpIs` edge (0x1ede) proposed with `source=guest_code`; every suffix unit of 0x1eb8 that lacks `movs r0,#1` gets `fact_not_local_to_return_tb`; T-B3-42 (U-COND decode check) passes or reports the counterexample units. U-HELD held-out stability: **reported outcome**.

**Acceptance (integration, WP19 + WP24):** on F-B3-1/1p/2/3/3b/4/8: fact `r0 == 0` proposed with `source=patch` and certified by WP19; on F-B3-6 `f5`: `no_fact`; on F-B3-5: `fact_killed_by_helper`; on F-B3-20: the suffix unit is refused; proposer and certifier agree everywhere (`fact_derivation_mismatch` = 0).

**Dependencies:** WP17 (unit); WP19, WP24 (integration).

---

## WP21: B3 passes and manager changes

**Files:** `scopt/passes/specialize_returns.py`, `constant_propagation.py`, `branch_folding.py`, `dead_host_ops.py`, `chain_returns.py`, `chain_static.py`; `scopt/analysis/hconst.py`; `scopt/manager.py` (region scope, edits on clones, downgrade `no_simplification`, budgets incl. per-R-unit variants, `ADD_REGION` via WP18's writer); `scopt/analysis/profitability.py` (per-pass and B4 savings, §10.3); `pipelines/b3.toml`; docs `docs/b3-passes.md` (pass-writer example for a semantic pass); tests incl. T-B3-41.

**Inputs:** §3.4-3.9, §10.3; WP16 calibration (defaults until then); WP18-WP20 APIs.

**Outputs:** candidates with GENERAL/SPEC guarded slots, variants and same-length patches; per-pass ledger rows with byte deltas, `origins`, `deps_added`; profitability verdicts.

**Acceptance (unit):** T-B3-7: identity and all-`no_change` pipelines are sha256-identical to the input (V9); T-B3-41: each pass has a rejected synthetic fixture with its stable reason; every PassResult outcome is one of the six; unsupported/unknown never reported as failed.

**Acceptance (integration, WP24):** T-B3-6 on F-B3-1: accepted statically (V1-V21, V23); CP, BF and DHO each disabled in turn give distinct non-zero byte deltas matching §3.5's expected bytes; all three disabled → `no_simplification` downgrade to GENERAL.

**Dependencies:** WP18, WP19, WP20 (unit); WP24 (integration).

---

## WP22: QEMU `.sc` v6 loader (Q1-Q11) — needs explicit maintainer approval

**Files:** in the gate QEMU tree: `tcg/supercache.c` (Q1-Q7, Q10 counters), `accel/tcg/cputlb.c` (4 hook calls), `accel/tcg/tb-maint.c` + `accel/tcg/internal-common.h` (`tb_unlink_jump`), `accel/tcg/tcg-all.c` (Q9: atomic flag + `async_safe_run_on_cpu`), `accel/tcg/cpu-exec.c` and `accel/tcg/translate-all.c` (Q10), `plugins/loader.c:381`, `plugins/core.c:258` (Q11); smoke scripts `scopt/tests/qemu/v6_*.sh`; patch `qemu-sc-v6-loader` (now QEMU commit `934890ed51`); the maintainer's evaluation data (not in the repository).

**Inputs:** §5 (all), §4.6 (probe via `probe_access_full_mmu` with `FIELD_EX32(G->flags, TBFLAG_ANY, MMUIDX)`), G1-G33; hook lines of §5.9.

**Outputs:** the `qint-v6` binary, frozen as the gate binary (tag `qint-v6-gate-1`, sha256 recorded); patch file; `QEMU_SC_V6_STATS` support.

**Acceptance (unit, synthetic artifacts):**
- Malformed v6 files (bad ext magic/sizes/checksum/ranges/hashes, **unknown feature bit, EXT_TABLE clear**) → QEMU exits with the expected `invalid v6 extension` reason before guest execution, and the file is **not** truncated (R2); a v6 file with a stale runtime signature is discarded with the `discarding v6 file` warning; valid ext with zero entries (identity-v6) → behaviour identical to v5.
- Q9: setting one-insn-per-tb from HMP while armed sets the flag on the monitor thread and the disarm runs in exclusive context (gdb `-nx` diag: no `tb_unlink_jump` frame on a non-vCPU thread).
- Disabled-path cost measured as in T-B1-0 (≥ 30 interleaved warm runs; median reported; > 1 % is a **reported outcome**, flagged).

**Acceptance (integration, WP18 + WP24):**
- T-B3-27: v5 regression: B1 T-B1-0…9 and B2 F-B2-1…6 re-run on the new binary with identical verdicts.
- T-B3-26 stress (F-B3-1, F-B3-6 recursion, F-B3-12, F-B3-14 artifacts): arm, then invalidate C (gdb debug write), invalidate R, `tb_flush` (breakpoint stop), `tlb_flush` (guest MPU write, also while a variant slot C'|n is linked), one-insn-per-tb via HMP, plugin reset; each time the slot is reset (gdb `-nx` diag reads `R->jmp_target_addr[n]` and the variant's slots) and the stats counters match; the "no variant reachable after a TLB flush" invariant holds (debug assert).

**Dependencies:** maintainer approval (unit, hand-made tables to start); WP18, WP24 (integration).

---

## WP23: Tier-1 capture (Q12) and `ops-xcheck` — optional (B3+), needs explicit maintainer approval

**Files:** branch `qint-v6-t1` (from `qint-v6-gate-1`): `tcg/sc-capture.c` (level `ops`, chunks 5 OPS and 6 PLUGIN_CBS), `tcg/tcg.c` (hook after the `CPU_LOG_TB_OP_OPT` block, :6937), `accel/tcg/plugin-gen.c` (`inject_cb`, :353), `include/tcg/sc-capture.h`; scopt `scopt/formats/scm_v1.py` (stop skipping chunk types 5/6, rebased on WP18), `scopt/analysis/ops_xcheck.py`, V22 body; the optional Tier-1 patch `qemu-sc-tier1` (not built; not on the QEMU branch).

**Inputs:** §8.2; B1 §2.2 rules R1-R6, §2.8; the candidate-design reviews (not in the repository) §4.

**Outputs:** `.scm` with OPS/PLUGIN_CBS chunks from the `qint-v6-t1` binary; `ops-xcheck@1`; V22.

**Acceptance (reported outcome, T-B3-29):** capture at level `ops` leaves the `.sc` byte-identical to level `reloc` (T-B1-1 method) on the `qint-v6-t1` binary; `ops_bytes_mismatch` count on minimal, fault, F-B3-1…F-B3-8 and compute; PLUGIN_CBS agreement with the patch-plan join on every fixture fact. Any mismatch is investigated as a possible HostOps typing bug (it pins the unit).

**Dependencies:** WP22 freeze, maintainer approval (unit); WP20, WP24 (integration). Not a dependency of WP28.

---

## WP24: fixtures `fixtures/b3/`

**Files:** `fixtures/b3/{startup.s, b3.c, link.ld, Makefile, gen_tomls.py, expected.py, qualify.sh, README.md}`, configs `b3-<phase>.toml.in` (incl. coverage-enabled configs for F-B3-23/F-B4-10 and the F-B3-16(e) config), `expected/*.norm` (icount) and `expected/*.inv.json` (noicount invariants), builds at `-O1` plus `-O0`/`-Os` for F-B3-24, the rebuilt-callee image for F-B3-16(d), mps2 alias image `alias/` with a direct-QEMU runner, gdb `-nx` diag scripts (`diag/*.gdb`) for O3 and for F-B3-12/13 (incl. the PAUSED-stop scenario (d)).

**Inputs:** §9.5 table; `fixtures/fault/` scaffolding (HardFault reporter, printreg virtuals, 0xFFFFFFFF as the unmapped address); G25 (SYST_CSR COUNTFLAG); O2b handler-record format (§9.4).

**Week 1 first (before writing the dependent phases):** U-VIRT (non-RW_CFI r0/xPSR writer for F-B3-5/22b), U-TBCB (TB hook raising an IRQ for F-B4-9), U-MPS2 (mps2 alias run). Each answer, or the documented fallback, goes into the README and the gate reports' known-gap list (`src/fastdyn` is not modified).

**Outputs:** one image with phases F-B3-1…F-B3-14, F-B3-17, F-B3-20…F-B3-24 (+1p, 3b, 8p) and F-B4-1…F-B4-7, F-B4-9, F-B4-10, selected by an input word; the alias image (F-B3-15); stale-dependency images/configs (F-B3-16 a-e); expected outputs; every handler writes O2b records.

**Acceptance (R14):** every phase qualifies 3/3 on nocache vs cold vs warm under its **per-mode oracle**: exact `.norm` in icount, the named invariants of `expected.py` in noicount; E-DET holds for the image in icount (with WP16).

**Dependencies:** none.

---

## WP25: B4 `eliminate-state-sync`

**Files:** `scopt/analysis/syncdead.py`, `scopt/passes/eliminate_state_sync.py`, `pipelines/b4.toml`, tests `tests/unit/test_syncdead.py`, `tests/acceptance/test_b4_*.py`.

**Inputs:** §7 (all, incl. the env-load use rule and the `can_do_io` direct-chain rule); WP17 HostOps/obs-contract/`tb_start_cb_possible`; WP19 hostsym; WP18 patch application and v5 writer.

**Outputs:** v5 artifacts with D1/D2/D3 nop-fills; per-class ledger rows (`--param classes=D1|D2|D3`); placement rule incl. the D2 prefix exception; `special_cflags_unit` skip.

**Acceptance (unit, T-B4-1, T-B4-2, T-B4-2b static, R10):**
- `syncdead` under TCG's own sync set reproduces M0 = 0 on compute (map-state-sync §7.3).
- D1/D2/D3 static counts on compute within ±5 % of the model (D2 including the prefix class) under `tb_start_cb_possible = false`; the D2 share lost under `true` is a **reported outcome** (K11).
- Every B4 mutant rejected statically; no edit outside the allowed placement.

**Acceptance (integration, WP24):** F-B4-1…F-B4-7 pass O1/O2a/O2b on the current binary; compute B4 artifact V10-equal on held-out in icount; one early paired timing data point (≥ 30 pairs, train) as a **reported outcome**.

**Why B4 runs in parallel with B3 (m-P13):** spec:157 orders B2, B3, B4 for implementation and measurement. B4-only artifacts are v5 files with no loader dependency, so WP25 can be built while B3 waits for WP22; its timing stays in its own category (`b4` arm) and the B4 gate report is written after the B3 gate's static items, so the order of reported results follows the spec.

**Dependencies:** WP17, WP18, WP19 (unit); WP24 (integration).

---

## WP26: run harness for v6 and dynamic oracles

**Files:** `scopt/run/proof.py` (v6: header version 6, ext bytes [0x100, 0x10000) unchanged); **extend the existing** `scopt/run/diag.py` (keeps `parse_exec_log`/`scan_exec_log`/`diag_exec`; adds stats-file collection, gdb `-nx` hit counters, the diag-twin TLB counter) (m-SF13); `scopt/run/oracles.py` (O1; O2a `-d int` diff; **O2b** handler-record parser and comparator, exact or invariant per mode; O3; O5; **O6** coverage diff); `scopt/run/negative.py` (test-only packager that bypasses V12-V17 for mutants; artifacts tagged `negative_control`, never publishable); `scopt/run/arms.py` (arms of §10.1); `scopt/run/deploy.py` (**test policy `allow_ram_resident`** for F-B3-15/17 artifacts, tagged test-only; **non-FastDyn import/deploy path** for the mps2 alias run: bare `.sc` with `identity.checked = false`; the refusal codes F-B3-16(a)/(e) produce, pinned by unit tests) (R8); report rows.

**Inputs:** §9.4, §10; B1 §4.8; WP18 artifacts.

**Outputs:** `scopt validate --dynamic` covering O1-O6 for v5 and v6 artifacts; arm definitions.

**Acceptance (unit):** O2b comparator on recorded logs (equal, shifted PC, shifted CVR); a negative-control artifact is refused by `scopt publish`; refusal-code unit tests for changed patch plan and coverage-enabled config.

**Acceptance (integration, WP24, WP22):** on a WP18 synthetic v6 artifact and on identity-v6: proof PASS on the gate binary, INVALID on `qint` (F-B3-18); O2b detects an injected interrupt-position difference on F-B4-3 (handler record from a run with a shifted SysTick reload).

**Dependencies:** WP18 (unit); WP24, WP22 (integration).

---

## WP27: first vertical slice (two-TB example end to end) (R15)

F-B3-1: R = `adds r0,r0,#1; bx lr`, C = the 1-insn TB `[cbz r0, Lz]`; neither has an LDST.

**Minimum each WP must have merged for slice 0 ("-min"):**
- WP17-min: `envlayout`, `hostops` (prefix, insn, exit stubs, pool; no LDST typing needed), `hcfg`, `hlive`.
- WP18-min: `grow_unit` (GENERAL + SPEC stub), `place_variant`, `apply_pure_rewrite`, V13 structural, V15, V16, V17; the existing v5 writer. No v6 writer, no region writer.
- WP19-min: `check_equiv` and `certify_fact` without the LDST abstraction; `check_guarded_chain`.
- WP20-min: `exit_facts` proposer, `patch_join` against the bundle patch plan, one CalleeSummary row.
- WP21-min: `specialize-returns`, CP, BF, DHO, `hconst`; manager region scope.
- WP24: phase F-B3-1 (icount) with its diag script.
- WP26-min: O1 and the gdb hit counter of `run/diag.py`.

**Slice 0a (static, current binary):**
1. Build F-B3-1 with modifier `r0 <- 0` at `f`'s `bx lr`; Tier-0 capture + Profile v1; import.
2. `exit-facts` proposes `r0 == 0` (`source=patch`), hostsym certifies it (V14); CalleeSummary row exported.
3. `specialize-returns` → CLONE C→C', SPEC slot on R; CP, BF, DHO patch C'; the static validators of WP18-min/WP19-min accept; byte diff and per-pass ledger rows match §3.5.

**Slice 0b (dynamic, current binary, experimental, never published):**
4. Build a v5 copy with R's GENERAL guarded slot and **the slice-0a C' patches written in place into the general C**, labelled `experimental_unsound_in_place_specialization` (valid for F-B3-1 only: C has one predecessor, the modifier makes r0 = 0 at every entry, no handler touches r0).
5. Run in icount with a clean diag twin: O1 equal warm; gdb `-nx` diag shows the patched C executed ≈ N times and R's stub `F` not taken after warm-up. This executes the simplified bytes before any QEMU build.

**Slice 1 (gate binary, after WP22 freeze):**
6. v6 writer emits one variant, one SPEC slot, one link; deploy a copy on the gate binary.
7. O1/O2a/O2b equal; O3: `armed = 1`, C' hits ≈ N − warm-up; O4: falsified-fact mutant changes O1; X-REHY repeated with the variant (coverage config).

**Acceptance:** steps 1-7 pass with evidence under the maintainer's evaluation data (not in the repository); T-B3-8. (F-B3-7 and F-B3-9 are WP28 items.)

**Dependencies:** WP17-min…WP21-min, WP24 (F-B3-1), WP26-min (slice 0); WP22 frozen (slice 1).

---

## WP28: B3 gate integration

**Files:** `tests/acceptance/test_b3_*.py` (T-B3-6…T-B3-42), the maintainer's evaluation report `B3-gate.md` (not in the repository) generator rows (prints the gate binary sha and the known-gap list).

**Acceptance = the B3 gate (§11), on the frozen gate binary:**
1. **Spec gate:** two-TB example (T-B3-6, T-B3-8); sub-cases F-B3-2, 3, 3b, 4, 5, 6 (+ sub-phases), 7, 20, 1p, 8, 8p, 9, 10, 11, 12, 16(a)-(e), each O1 + O2a/O2b per mode with its non-vacuity counter > 0; V1-V21 and V23 on every accepted artifact; mutation suite 100 % static, O4 for every dynamic-set mutant; F-B3-23 (O6); F-B3-24; T-B3-27; T-B3-28.
2. **Hardening:** F-B3-13 (a)-(d), F-B3-14, F-B3-15, F-B3-17, F-B3-18, F-B3-21, F-B3-22 (+22b if U-VIRT), T-B3-26. A failure blocks; `not_run:<unknown>` for U-MPS2/U-VIRT does not.
3. **B3+ (reported):** T-B3-29 from WP23 if it exists.
4. **Performance (reported):** compute B3 artifacts (`b3-chain`, `b3-static`, `b3-spec`) accepted and V10-equal on held-out inputs; paired timing with regressions.

**Dependencies:** WP21, WP22 (frozen), WP24, WP26, WP27.

---

## WP29: B4 gate integration

**Files:** `tests/acceptance/test_b4_*.py` (T-B4-1…T-B4-13), the maintainer's evaluation report `B4-gate.md` (not in the repository) generator rows (gate binary sha, §7.6 deferral statement, known gaps).

**Acceptance = the B4 gate (§11), dynamic items on the frozen gate binary:**
1. T-B4-1, T-B4-2, T-B4-2b; every B4 mutant rejected statically and detected dynamically (O4), incl. the IT-block D4 shape (F-B4-5) and the brcond-reload mutant (F-B4-1).
2. F-B4-1…F-B4-9 pass O1/O2a/O2b per mode (normal exit, callback, precise fault, interrupt recovery, MMIO/can_do_io, divide-by-zero, guest memory events and memory callbacks, compute, TB-start callback), with D1 and D2 deletions > 0 where the fixture allows them; F-B4-9 replaced by its static control only if U-TBCB failed (known gap).
3. V12 event preservation and the host_abs assertion on every rewritten unit of the compute artifact.
4. T-B4-13 (F-B4-10 coverage, O6).
5. Per-class ablations validated; timing measured and reported (not a pass/fail item).

**Dependencies:** WP24, WP25, WP26, WP22 (frozen).

---

## WP30: B5 hooks

**Files:** `campaigns/b3b4-freertos.toml` (frozen artifacts, input splits, arms of §10.1 shaped by the WP16 decision, ablations, ≥ 30 interleaved repetitions), report sections for removed lookups/stores (model vs measured), code growth, memory, full-harness throughput, region coverage, cold-path latency, fallback frequency, preparation cost.

**Acceptance:** `scopt report` produces every row from one command, including `skipped_unsupported` arms; campaign file frozen (candidate and profile sha256, gate binary sha) before any held-out/final timing; nothing is timed in this WP.

**Dependencies:** WP28, WP29 (and the WP16 decision).

---

## Gates (copied from design §11)

**B3 gate:** WP28 items 1-2 (spec gate and hardening); items 3-4 are reported outcomes.
**B4 gate:** WP29 items 1-4; item 5 is a reported outcome.

Both gates are correctness gates; measured benefit is reported as its own outcome (spec §6 "Source inspection, successful compilation, accepted rewriting and measured benefit are separate reported outcomes").

---

## Suggested staffing and order

| Week | Work |
|---|---|
| 1 | A: WP16 (probe build; timing once copies pass their diag twins). B: WP17 (contracts and V-row stub on day 1, via WP18's registry). C: WP24 (U-VIRT, U-TBCB, U-MPS2 first; then F-B3-1). D: WP22 unit part (after approval). |
| 1-3 | B: WP18. E: WP19 unit (LDST abstraction first). F: WP20 unit. D: WP22 unit + T-B3-27. C: WP24 phases in delivery order. |
| 3-4 | B+F: WP21. A: WP26, then WP25 with E. WP27 slice 0a/0b as soon as the "-min" set lands. |
| 4-5 | WP22 integration (stress) and freeze; WP27 slice 1; WP23 (optional) on `qint-v6-t1`. |
| 5-7 | WP28, WP29, WP30. |

Estimated effort to slice 1: 4-6 person-weeks for one developer, shorter with WP22/WP24 in parallel (I; the feasibility review's correction of the 3-4-week estimate).

**Earliest evidence without any QEMU change:** WP16 calibration and E-DET; WP18 T-B3-2/T-B3-3 on compute; WP19 T1-T6; WP27 slice 0a (static simplification) and slice 0b (the simplified bytes executing, V10-equal, in icount).
