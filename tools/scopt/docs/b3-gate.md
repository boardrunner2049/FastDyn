# The B3 gate (WP28)

Design: `docs/design/B3-B4-design.md` §11 ("B3 spec gate", "B3 hardening", "B3+", "B3
performance") and the WP28 implementation note at its end. Plan:
`docs/design/IMPLEMENTATION-PLAN-B3B4.md` `## WP28`. Report: the maintainer's evaluation report `B3-gate.md` (not in the repository).

The gate is one driver, `tests/qemu/wp28_gate.py` (steps in `wp28_steps.py`), run from this
worktree with the scopt venv python, plus the timing script `wp28_timing.sh`. Every dynamic
item runs on the frozen gate binary `qint-v6-gate-1` (exe sha256 in
the maintainer's evaluation data (not in the repository); the `gate` step re-checks sha, tag = HEAD and a clean tree, and
every `run.json` records the sha). Every base is re-collected on that binary.

```
taskset -c 16-23 python tests/qemu/wp28_gate.py --out runs/wp28 --steps \
    gate,bases,artifacts,dynamic,aa,stale,gdbdiag,t_b3_6,o4,mutants,roundtrip,alias,smc17,\
    coverage,ucond,compute,compute_hits
tests/qemu/wp28_timing.sh                       # holds the workspace timing lock, 4 cores
python tests/qemu/wp28_timing_analyze.py runs/wp28/timing --out runs/wp28/timing-analysis.json
python tests/qemu/wp28_gate.py --out runs/wp28 --steps verdict
python tests/qemu/wp28_report.py --runs runs/wp28 --out <reports dir>/B3-gate.md   # report kept by the maintainer, not in the repository
python -m pytest tests/acceptance/test_b3_gate.py    # reads runs/wp28 (WP28_RUNS overrides)
```

## What each step proves

| step | gate item | how |
|---|---|---|
| `bases` | all | every `fixtures/b3` config (31 + the F-B3-16 references) prepared, imported and profiled on the gate binary in icount (shift 0) and noicount; RuntimeIdentity v2 by `scopt admission identity`. F-B3-11's base sees only its train input |
| `artifacts` | 1, 2, 3 | `scopt optimize --pipeline b3 --b3` (all six B3 passes, fixture param `specialize-returns.growth_fraction = 1.0`), static rows V1-V21/V23 (V9, V22 skip; V14 skip only without a SPEC link), slots and variants read from the output `cache.sc`, fixture expectations (SPEC link to the fixture's Vc, or the refusal the fixture is about, with `return-edges analyze` for reasons the pass does not count), `scopt publish` |
| `dynamic` | 1, 2, hardening | `scopt.run.oracles.dynamic_oracles` on the published copy vs the base's warm cache, both modes: O1 (with the fixture's per-mode oracle), O2a (`-d int`), O2b, O3 (`links_armed > 0`, C' hits > 0 for the fixture's Vc, fixture stats such as `variant_entry_exits`/`variant_restores`/`disarm_tlb_events`), O5 |
| `aa` | 2 | warm-vs-warm of the noicount timer phases: O2a differs there too, so noicount O2a of a timer phase is reported, not judged (design §9.4 defines O2a under icount) |
| `stale` | 2 (F-B3-16) | (a)/(b)/(e) deploy refusals, (c) the qint binary (directory refused, bare `.sc` INVALID), (d) a direct rerun with `b3_16d.elf` and `QEMU_SC_V6_STATS` (nothing armed, output = the new image's) |
| `gdbdiag` | 2 (F-B3-12), hardening (F-B3-13) | F-B3-12: a PAUSED stop with the link armed, debug write `cbz` -> `cbnz`, continue, on the F-B3-13 kernel (the F-B3-12 kernel has no PAUSED hook; WP24's `f-b3-12.gdb` never arms a link and is kept as the vacuous row `F-B3-12-wp24bp`), WP22's host-gdb harness (R's slot before/after the write, C' hits before/after) plus the same scenario on the warm cache; 13a-c: WP24's scenarios through WP22's harness; 13d: the direct runner. 13(a)'s `refused[bp] > 0` is unreachable (CF_BP_PAGE) and is recorded as a substituted criterion |
| `t_b3_6` | 1 | CP/BF/DHO each disabled: three distinct non-zero byte deltas, ledger rows, §3.5 byte form by an independent capstone diff |
| `o4` | 3 | every §9.3 mutant outside the static-only set built from the **manager's own plan** of its named fixture (byte-identical to the published artifact), packaged by `package_negative`, detected by O1/O2a/O2b (or the 13(d) scenario) |
| `mutants` | 3 (T-B3-5) | synthetic suite, `scopt hostsym mutants`/`link-mutants` on every base, guard mutants on the manager's own link; a mutant hostsym proves equivalent is re-checked by the full validator |
| `roundtrip`, `ucond` | 5 (T-B3-28), T-B3-42 | CalleeSummary rows for every return unit; V19 graph round trip with `guarded_slot`/`variant_link`; `multi_tb` region rows; U-COND decode check |
| `alias` | hardening (F-B3-15) | the mps2 alias image directly on the gate binary with a **test-only** SPEC artifact (no bundle/RuntimeIdentity exists on that path) |
| `smc17` | hardening (F-B3-17) | the production artifact has no SPEC link into RAM (`target_not_profiled`), so a test-only plan links f_b3_17's return to `smc_cont`'s RAM copy (nothing bypassed): C' runs until the guest rewrites C at K = 300, outputs equal warm |
| `coverage` | 4 (F-B3-23) | the real pipeline under coverage, O6 and C' hits on the published artifact's variants (computed, not hard-coded) |
| `compute` | performance (reported) | compute base on the gate binary, `b3-chain`, `b3-static`, `b3-spec`, `b3`; V10 on held-out inputs |
| `compute_hits` | performance (reported) | diagnostic gdb re-runs of the held-out V10 runs: C' entries of every SPEC variant (never timed) |
| `verdict` | all | the maintainer's evaluation data (not in the repository): spec gate, hardening, reported outcomes, mutation table, ledger stages, blockers |

T-B3-26, T-B3-27 and F-B3-18 are cited from WP22/WP26 runs on the same exe sha (T-B3-27 is
judged from WP22's evidence files, not its verdict text); T-B3-29 is
`not_run: WP23 not approved`. The timing CPU budget is recorded in `timing-analysis.json`
(`cpu_budget`) and `verdict.json` (`timing_cpu_budget`).
