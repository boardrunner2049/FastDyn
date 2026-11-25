# QEMU smoke scripts

Standalone scripts that exercise a built `qemu-system-arm` (no pytest needed).
Each takes the binary path explicitly, launches through `setarch x86_64 -R`
with a timeout, and uses a fresh run directory per run.

| Script | WP | What it checks |
|---|---|---|
| `base_warm_smoke.sh QEMU OUT_ROOT` | WP0 | minimal fixture: cold run appends records, warm run in a fresh dir (sparse copy of the cold cache) appends 0, leaves the `.sc` untouched and prints the B0 guest-visible lines |
| `scm_smoke.py RUN.scm [--sc CACHE.sc]` | WP3 | `.scm` header, chunk trailers, seq, END, per-TB structure; with `--sc`: RUN_INFO binding, record_count, 104 record bytes per slot, `code_fnv64` |
| `scc_identity.py QEMU OUT_ROOT` | WP3 | T-B1-1 locally: 3 capture-off + 2 capture-on cold runs of minimal in one recreated run dir, same binary; whole-file sha256 equality (or the design 2.6 per-key mode), plus `scm_smoke.py` on each capture |
| `qprof_smoke.py --qemu QEMU --out OUT_ROOT` | WP5 | minimal fixture: cold snapshot; warm with `QEMU_SC_*` unset appends 0 and matches B0; warm profile runs give a valid `.qprof` (header, phase/TB/edge rows, trailer, clean_exit, no overflow, §3.4 flow conservation, §3.6 join to the staged `.sc`); `total_dispatches` and per-key entries equal the `Trace` lines of `-d exec,nochain` runs; capture+profile and every other configure rejection exit non-zero. `--check FILE.qprof` validates one file |
| `wp12_fixtures_smoke.sh QEMU OUT_ROOT` | WP12 | fault fixture (all four variants) and compute fixture (train-1, heldout-1): nocache/cold/warm in fresh run dirs equal the expected outputs; model check; a train-1 cache warm-run with heldout-1 appends exactly the new keys, without invalidation |
| `wp24_fixtures_qualify.sh QEMU OUT_ROOT [-j N]` | WP24 | B3/B4 fixture (`fixtures/b3`): every phase config and input, icount and no-icount, nocache ×2 / cold / warm ×2 in fresh run dirs under its per-mode oracle (exact frozen `.norm` in icount, named invariants in no-icount), extra artifacts (variable_watch events, b3_tbirq counts) equal across runs; the F-B3-15 alias image directly on QEMU |
| `t_b1_0.py --out OUT [--pairs 30] [--pairs-minimal N] [--layout random\|fixed]` | WP-QINT | T-B1-0 with the integrated binary (the WP-QINT build, see `docs/qemu.md`). Part A: nocache/cold/warm×2 on minimal (`b0-*.toml`, outputs equal the maintainer's evaluation data (not in the repository)) and the FreeRTOS icount fixture (guest.norm and final RAM equal the qualified ones). Warm runs need proof PASS and appended 0; capture and profile stay off. Part B: interleaved warm pairs, B0 binary + its cache vs the integrated binary + its own cache, BENCH medians with bootstrap CIs, >1% flagged (not gated). Default is setup-randomized layout (run-dir name length, env pad), because a fixed setup carries a ±5% layout bias |

## WP22: `.sc` v6 loader (the gate QEMU tree, branch `qint-v6`)

| Script | What it checks |
|---|---|
| `v6_smoke.sh QEMU OUT_ROOT` | runs the two drivers below |
| `v6_unit.py --qemu QEMU --out OUT` | prepares + imports the minimal fixture **on that binary**, then: v5 control (proof PASS, appended 0); identity-v6 (empty extension table: same guest output, file stays v6); a guarded file from `v6_synth.py` (7 SPEC links with relocated-clone variants, 2 GENERAL slots: same output, links armed, no invariant violation); stale runtime signature (`discarding v6 file` + stock truncate); one malformed file per `v6_synth.MUTATIONS` entry (exit non-zero with `supercache: invalid v6 extension: <reason>`, no guest line, file unchanged). Verdict in `OUT/v6-unit.json` |
| `v6_gdb_diag.py --qemu QEMU --out OUT` | QEMU started directly from the deploy oracle's argv under `gdb -nx` (cwd `src/fastdyn`): `exec` (hardware breakpoints count variant entries), `tlb` (`tlb_flush` at a variant entry: every slot unlinked, disarmed links stay off), `tbflush` (`tb_flush` from `cpu_exec` after a forced exit: links cleared, re-armed in the new epoch), `hmp` (HMP `one-insn-per-tb on` with the vCPU stopped: setter on the main thread, disarm and every `tb_unlink_jump` on the vCPU thread in the exclusive context). Verdict in `OUT/v6-gdb-diag.json` |
| `v6_synth.py` | the hand-made v6 generator (identity, grown R with the §4.1 guard stub, relocated variants, mutants); independent of the WP18 writer. Offline tests: `tests/unit/test_wp22_v6_synth.py` |

`QEMU_SC_V6_STATS=/abs/file` makes the v6 binary write its counters (JSON) at exit; the run harness scrubs it from every arm unless a driver sets it explicitly.

### WP22 integration stage (WP18 writer + WP24 fixtures, T-B3-26)

| Script | What it checks |
|---|---|
| `wp22_stress.py --qemu QEMU --out OUT` | base snapshots of WP24 configs `b3-1/6r/12/13/14` captured **on that binary** (icount arm), v6 artifacts written by WP18's `package_b3` (SPEC links R → identity clone C', a GENERAL slot, identity-v6, on `b3-14` log_pair's return -> SPEC -> b3_res's `msr PRIMASK` TB and b3_res's pop TB with two GENERAL slots, on `b3-14g` a variant of a grown C on the phase's hot loop (call TB -> SPEC -> `f_b3_14`, itself grown with a GENERAL slot); R/C addresses come from `b3.nm`/`b3.lst`); plain warm runs (proof PASS, appended 0, the fixture's icount oracle, stats); then the T-B3-26 stress under `gdb -nx` on the QEMU process (`wp22_stress_gdb.py`: variant-entry hardware breakpoints, before/after slot state at every loader event), some with a guest debugger on the gdbstub (WP24 `diag/f-b3-*.gdb`, WP22 `wp22gdb/*.gdb`): arm, guest MPU write (TLB flush with `C'|n` linked, on `b3-14g` the cloned GENERAL slot of the grown C), `tlb_flush` in 6r, HMP one-insn-per-tb, plugin reset, invalidate C / invalidate R at a PAUSED stop, breakpoint stops (tb_flush), watchpoint. Every scenario also compares the loader's stats counters with the counts derived from the observed events. Verdict in `OUT/wp22-stress.json` |
| `wp22_ext_xcheck.py --qemu QEMU --artifact ART --config TOML --input BIN --out OUT` | 32 one-field mutants of a WP18-written v6 file: WP18's `ext_problem` (code, message) must equal the loader's `invalid v6 extension: <message>`, exit non-zero, no guest output, file unchanged. Verdict in `OUT/ext-xcheck.json` |

Offline tests of the verdict logic: `tests/unit/test_wp22_integ.py`.

## WP26 integration stage (run harness on the gate binary)

| Script | What it checks |
|---|---|
| `wp26_integ.py --out OUT [--only f_b3_18 o2b harness]` | Runs on the frozen gate binary (the maintainer's evaluation data (not in the repository); the script checks the exe sha256, that the tag and HEAD are at the frozen commit and that the tree is clean) and on `qint`. **f_b3_18** (F-B3-18): F-B3-1 base prepared on the gate binary; WP18 writes identity-v6, the F-B3-1 SPEC artifact and the synthetic plan of `tests/synth/b3.py`. Each artifact directory must run on the gate binary with proof PASS, `v6_header`/`ext` true, the fixture oracle clean and O1 equal to the v5 warm reference. On `qint` the directory is refused (`runtime_mismatch:qemu_sha256`), and the bare `cache.sc` must give `INVALID_artifact_not_used` with `v6_header` failed. Discriminating control: a base prepared on `qint` passes there as v5, and its identity-v6 (`v6_exes=[qint]`, test only) is INVALID with only the version and feature flags changed. **o2b** (F-B4-3): v5 reference, identity-v6, and identity-v6 under `arm-none-eabi-gdb -nx` (control: watch `g_sweep`; shift: advance the SysTick sweep pointer by one entry). O2b (icount) must pass, pass, pass and **fail**, and O4's rule must count the shift as detected. **harness**: `scopt validate --dynamic --oracles` refuses the v6 artifact on `qint` (exit 2) and passes O1/O2a/O2b/O3/O5 on the gate binary. Verdict in `OUT/wp26-integ.json` |

Offline tests of the helpers: `tests/unit/test_wp26_integ.py`.

## WP25 integration stage (B4 on the WP24 fixtures and on compute, current binary)

| Script | What it checks |
|---|---|
| `wp20_fixture_snapshots.py --out OUT/fx --configs b4-1,...,b4-7` | (WP20's script, reused) base snapshots of F-B4-1…F-B4-7 on `qint` in icount and noicount |
| `wp25_integ.py --out OUT [package dynamic mutants compute verdict]` | **package**: v2 RuntimeIdentity from each fixture config's admission facts, `propose_b4` + `package_b4` (v5, published only when V12 certifies every rewritten unit), plus the static fixture checks (F-B4-4 prefix `can_do_io` store kept as `observed_at_sync_point`; F-B4-5 the IT-block `regs[1]` store kept; F-B4-7 no D3 deletion, UPDATE_MEM host stores untouched). **dynamic**: `dynamic_oracles` O1/O2a/O2b/O5 against the warm base with the fixture's per-mode oracle (icount 2 reps, noicount 4; O2a is not gated in noicount for the timer phase b4-3 and is reported next to a warm-vs-warm control), non-vacuity by gdb hardware-breakpoint counts on the entries of rewritten units, F-B4-4 `cpu_io_recompile` counts (artifact == warm), F-B4-7 `events.tsv` (artifact == warm). **mutants**: every B4 mutant kind of `scopt.b4_mutants` (first instance in the snapshot and in the fixture's loop unit) plus the real UPDATE_MEM host-store deletion on F-B4-7, packaged through `package_b4` (must be rejected), and twelve of them as negative controls (bypass V12-V17, O4 mode). **compute**: V10 of the WP25 compute artifacts (b4 all, D1, D2, D3, sfb) on the held-out inputs. Evidence in `OUT/{package,dynamic,mutants,compute-v10,verdict}.json` |
| `wp25_timing.sh` | the one early B4 timing point (reported outcome): `scopt run` arms warm, b4, b4-d1/d2/d3, sfb on the compute train inputs, 30 interleaved reps, under `flock $FASTDYN_ROOT/runs/.timing.lock`, `taskset` 4 cores, load averages sampled to `timing-load.log` |
| `wp25_timing_analyze.py TIMING_DIR` | paired speedups t_warm/t_arm per (rep, input), median with a 95 % bootstrap CI (WP16's method) |

Offline tests of the helpers: `tests/unit/test_wp25_integ.py`.

## WP27 unit stage (first vertical slice, slices 0a and 0b, current `qint` binary)

| Script | What it checks |
|---|---|
| `wp27_slice0.py --out OUT [--steps bases,summary,static,slice0b] [--modes icount,noicount]` | Nothing is timed; QEMU is the WP-QINT QEMU tree only. **bases**: WP24 phase F-B3-1 prepared, imported and profiled on `qint` (WP20's driver) plus its RuntimeIdentity v2. **summary**: `scopt return-edges summaries --certify` (r0 == 0, `source = patch`, certified by V14; the CalleeSummary row). **static** (slice 0a): `scopt optimize --pipeline b3 --b3` full / CP, BF, DHO off / all three off, with the test-only `--v6-exe <qint sha>` override of V7 into an evidence store (never deployed); the full case without the override must be rejected by V7 alone. Variant bytes are diffed against the general C from the output `cache.sc` (capstone, WP21's checker), per-pass ledger rows and the multi_tb FactRef's CalleeSummary row hash are checked. **slice0b**: `experiments/wp27_inplace.py` writes the v5 copy `experimental_unsound_in_place_specialization` (R's registered GENERAL guarded slot + the slice-0a C' patches written into the general C; never published), then O1/O2a/O2b/O5 against the warm base, gdb `-nx` hardware-breakpoint hits on C, R's stub S, its fallback F and the slot reset, the m-CE7 twin, and the WP27 ordering twin (hit counts of C and R's stub at every TLB flush after `benchmark_start`). Verdict in `OUT/verdict.json` |

Offline tests of the copy builder and the ordering-twin parser: `tests/unit/test_wp27_inplace.py`.

| Script | What it does |
|---|---|
| `wp27_slice1.py --out OUT [--steps bases,summary,artifact,dynamic,o4,xrehy] [--modes icount,noicount]` | WP27 integration stage (slice 1). Nothing is timed; every QEMU run is on the frozen gate binary (the maintainer's evaluation data (not in the repository); the driver refuses to start when the tag, `qint-v6` HEAD or the exe sha differ). **bases**: F-B3-1 and F-B3-23 (coverage) prepared/imported/profiled on the gate binary + RuntimeIdentity v2. **summary**: CalleeSummary rows (certified). **artifact** (step 6): `scopt optimize --pipeline b3 --b3 --disable chain-returns --disable chain-static` (the `b3-spec` arm): one variant, one SPEC slot, one link, checked from the output `cache.sc`; then `scopt publish`. **dynamic** (step 7): `scopt validate --dynamic --oracles O1,O2a,O2b,O3,O5` on the published copy; O3 = `links_armed == 1`, C' hits in [N-5, N-1], F in [1, 5], C' + F = S. **o4**: hand-built positive control (must equal the manager's variant) and two negative controls packaged by `scopt.run.negative`: `falsified_fact_link` (claims r0 == 1, folded for it; V14 + V19 `stale_fact` bypassed) and `fold_flip` (same fold citing the true fact; V12-V17 bypass); both run with `--negative-control`. **xrehy**: the real pipeline on F-B3-23 (no variant: fail closed) and the same §3.5 variant in the coverage C as a test-only experiment (WP26 packager, the failing V12/V13/V14/V19 rows bypassed, never publishable); O1/O2a/O2b/O3/O6 (LOG_REG ring) read individually. Verdict in `OUT/verdict.json` |

Offline tests of the slice-1 helpers and verdict: `tests/unit/test_wp27_slice1.py`.

## WP28: the B3 gate (frozen gate binary `qint-v6-gate-1`)

| Script | What it does |
|---|---|
| `wp28_gate.py --out runs/wp28 --steps …` | The whole B3 gate (design §11) on the gate binary. Steps: `gate` (exe sha, tag, clean tree), `bases` (every `fixtures/b3` config, icount + noicount, prepared/imported/profiled on the gate binary; F-B3-11 on its train input only; RuntimeIdentity v2), `artifacts` (`scopt optimize --pipeline b3 --b3` + `scopt publish` on every base; static rows; slots from `cache.sc`; refusal reasons), `dynamic` (`dynamic_oracles` O1/O2a/O2b/O3/O5 with the fixture's per-mode oracle, gdb hit counts on every C' and SPEC fallback), and the steps of `wp28_steps.py`: `aa` (noicount warm-vs-warm of the timer phases), `stale` (F-B3-16 a-e), `gdbdiag` (F-B3-12, F-B3-13 a-d), `t_b3_6`, `o4` (negative controls from the manager's own plan), `mutants` (static suite), `roundtrip` (T-B3-28), `alias` (F-B3-15, direct mps2), `coverage` (F-B3-23), `ucond` (T-B3-42), `compute` (b3 arms + V10 held-out), `verdict`. Each step writes the maintainer's evaluation data (not in the repository). Nothing is timed |
| `wp28_timing.sh` | compute timing rows (warm, b3-chain, b3-static, b3-spec, b3; held-out inputs, 30 interleaved reps) under `flock $FASTDYN_ROOT/runs/.timing.lock`, 4 cores, load log |
| `wp28_timing_analyze.py TIMING_DIR --out FILE` | paired speedups (median of t_warm/t_arm, bootstrap CI, seed sensitivity) |
| `wp28_report.py --runs runs/wp28 --out <reports dir>/B3-gate.md` | the gate report from the JSON evidence |

Facts a user of the driver needs: `dynamic_oracles(..., quiet=True)` changes FastDyn's
`log_level`, which is part of `contract_id`, so every deploy is refused `bundle_mismatch`;
WP22's host-gdb loader-event harness (`wp22_stress.gdb_run`) does not finish F-B3-13(d) on a
manager-built artifact within 600 s (1401 `tb_flush` events), so 13(d) runs through
`wp28_steps.direct_guest_scenario`; `scopt.run.diag.gdb_hits` accepts a WP8 run dir or a
direct run dir (`direct.json`). F-B3-12 runs as a PAUSED-stop scenario on the F-B3-13 kernel
(`wp28_steps.scenario_12_paused`): WP24's `f-b3-12.gdb` never arms a link (CF_BP_PAGE) and is
kept only as the vacuous row `F-B3-12-wp24bp`. Steps added by the review fix: `smc17`
(F-B3-17 with a test-only SPEC link into the RAM continuation) and `compute_hits` (C' entries
of the compute SPEC arms on the held-out V10 runs; diagnostic, never timed).
`WP28_GDBDIAG_ONLY=F-B3-12,F-B3-13d` re-runs only those `gdbdiag` rows.

## WP29: the B4 gate on the frozen gate binary

Every dynamic run uses the gate QEMU build (`qint-v6-gate-1`, sha256 `56d8bfb3…`; the `gate` step re-checks sha, tag, HEAD and a clean tree, and every row records the `qemu_sha256` of its runs). Report: the maintainer's evaluation report `B4-gate.md` (not in the repository).

| script | what |
|---|---|
| `wp20_fixture_snapshots.py --qemu <gate> --configs b4-1,…,b4-7,b4-9,b4-10 --profile` | gate-binary bases of the B4 fixtures (the maintainer's evaluation data (not in the repository)); `--modes icount --configs 3b,4,5,8,8p,9,10,21,22,6h,7` the B3 fixture bases used only as extra O4 detectors (the maintainer's evaluation data (not in the repository)) |
| `wp29_gate.py --out OUT [steps]` | `gate`; `compute-base` (compute TOML re-rendered with `qemu_path` = gate, prepare/bundle/import/admission, `b4 census`); `package` (fixtures + §9.5 static checks, incl. F-B4-9's last-insn `can_do_io = 1` kept); `package-compute` (b4, D1/D2/D3 ablations, `icount-entry-sfb`; V12/host_abs per rewritten unit); `compute-host-abs` (HostOps census of HOST_ABS ops, all and rewritten compute units); `sfb-row` / `recheck-compute` (rebuild the sfb row; re-run package-compute into a fresh store and compare); `package-sfb-b4` (B4 on the re-imported sfb artifact); `dynamic` (O1/O2a/O2b/O5, O6 for F-B4-10, non-vacuity, F-B4-4 io-recompile, F-B4-7 events.tsv, F-B4-9 counts.txt + `cpu_abort` count); `deletions` (gdb entry counts of **every** rewritten unit, icount); `b4-10` (why F-B4-10 is vacuous + an O6 warm-vs-warm control); `mutants` (static on every instance up to 6 per kind and fixture + compute + WP19 synthetic; O4 round 1); `o4-extra` (O4 round 2: B3 fixtures as detectors, phase-specific units first, every instance; LOG_REG deletion detected by O6); `host-abs` (real `host_abs_in_env` on F-B4-7: env from a gdb diag re-run, imm64 repointed into `vfp.zregs`, static with/without `env_abs`, O4, gdb check of the pointer in the control's run); `o4-cover` (every executed, untried instance of `flag_store_before_helper` in all gate fixture snapshots); `compute` (V10 held-out, icount); `verdict` (`tests/qemu/verdict_wp29.py`) |
| `wp29_timing.sh` | T-B4-11 timing (reported): arms warm, b4, b4-d1/d2/d3, sfb, sfb-b4 on compute train inputs, 30 interleaved reps, `flock $FASTDYN_ROOT/runs/.timing.lock`, `taskset` 4 cores, load averages to `OUT/timing-load.log` |
| `wp29_timing_analyze.py TIMING_DIR [--seeds 6]` | paired speedups (each arm vs warm; sfb-b4 also vs sfb); CI per bootstrap seed, `borderline` when a bound's sign depends on the seed |

Offline tests: `tests/unit/test_wp29_gate.py`; the gate evidence: `tests/acceptance/test_b4_gate.py`.

## WP30: the B5 campaign (defined, not run)

Every arm runs on the frozen gate binary (`qint-v6-gate-1`). WP30 runs only the untimed
commands (`prep`, `freeze`, `check`, `plan`); `run` (timing, under `flock
$FASTDYN_ROOT/runs/.timing.lock`, `taskset` <= 4 CPUs) and `diag` belong to the B5 execution stage. See
`docs/b5-campaign.md` and the maintainer's evaluation report `B5-plan.md` (not in the repository).

| script | what |
|---|---|
| `b5_campaign.py prep` | resolves every arm of `campaigns/b5-freertos.def.toml`: reuses WP28/WP29 published compute artifacts (with their held-out V10 verdict), builds identity, identity-v6, layout (+2 ablations), b3-no-<pass> (6) on the WP28 gate-binary base, publishes verified copies, re-validates and runs V10 on the held-out inputs (the maintainer's evaluation data (not in the repository)) |
| `b5_campaign.py freeze` / `check` | writes / checks the frozen `campaigns/b5-freertos.toml` (every sha256, code ids, final-input refusal) |
| `b5_campaign.py plan` | `scopt run --dry-run` of every set (preflight + interleaved schedule, no QEMU) into the maintainer's evaluation data (not in the repository) |
| `b5_campaign.py run` / `diag` / `report` | timing sets (R1-R6 in `b5_campaign.json`), untimed v6-stats/C'-hit and base-profile diagnostics, one `scopt report --b5-campaign` |

Offline tests: `tests/unit/test_wp30_b5.py`.

## Sealed v7 counters

| Script | What it checks |
|---|---|
| `v7_counters.py LAUNCH.json OUT` | a compiled sealed launch with exercised trusted firmware regions (for example packet config 7), on private cache copies: default, `QEMU_SC_V7_EXPERIMENT=no-regions` and `disable-chaining` print the same guest output; every `QEMU_SC_V7_STATS` file has `counters_revision` 2 and no `scopt.run.v7_stats.counter_problems`; `trusted_tb_calls == loop_returns_nonnull + trusted_tb_chain_probes`, `0 < trusted_residual_exits <= loop_returns_nonnull`, no chained trusted source, and no residual exits without regions. Writes `OUT/v7-counters.json` |
| `trusted_cache.py`, `safe_region_index.py` | also require an empty `counter_problems` |

Offline tests of the counter rules: `tests/unit/test_v7_stats.py`.
