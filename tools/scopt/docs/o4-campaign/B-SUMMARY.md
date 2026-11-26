# Campaign B (confirmation and best-check): result

Run 2026-11-26 04:09-05:05 EST by step P6. Design: O4-design.md §3.8, with the hygiene of §3.9. Every decision below comes from the registered decide.py (`0b7247476a39abade599000de4e2555c7edb1167b57d0989f4bac54cbaf1a438`, registered: true in every output).

**Status: BLOCKED.** O4 stays at **O4-r1**, and BEST fails against it: **o4_pwhopr (A5) is faster than O4-r1** (campaign H), and in H2 it is also faster than the B1-retry candidate O4-r2b. A5 has to be absorbed in a form that passes B1. O4-r2, the one composition that carried A5, failed B1 on packet. The registered procedure (one B1 retry) is used up, so the next step is the lead's call. See "What would have to be absorbed" below.

## Outcome

1. **B1, O4-r2 against O4-r1: FAIL.** O4-r2 is A5 + I1 + X1 + X4. The rule was ADD-confirm, chosen and recorded before any B timing in `p6/B1-rule.md` (A5 is a speed claim). The pooled estimate is +0.49% (SE 0.32%), 95% [-0.05%, +1.02%], so LB95 ≤ 0. Per workload:
   - packet: d -1.21% (SE 0.98%), LB95 -2.87%. That is below log 0.98 (-2.02%), so non-inferiority fails.
   - uart: -0.15%, LB95 -1.00%.
   - cli: +1.34%, LB95 +0.58%.

   The REMOVE rule would have failed as well, on packet's LB95. B1's own BEST family (4 hypotheses) finds O4-r2 faster than O4-r1 on cli: p 0.0025 ≤ 0.0125, Holm rank 1. decide.py: "drop o4_pwhopr (smallest A2 pooled d) and repeat B1 once".
2. **Retry build O4-r2b = O4-r2 without A5 = O4-r1 + I1 + X1 + X4.** The pipeline is `design/pipelines/O4-r2b.toml`, built from its copy `o4r2b.toml` as arm o4r2b. It was built on CPUs 21-31 with no timing running, and every G0 and validity gate passes on all three workloads (details below).
3. **B1 retry, O4-r2b against O4-r1: PASS (REMOVE).** No add-on is left, so decide.py applies REMOVE. Pooled +0.18% (SE 0.30%), 95% [-0.32%, +0.68%]. LB95_w: packet -0.60%, uart -0.93%, cli -0.49%, all above log 0.98. Checks: ub_ge_0, lb_gt_log099 and noninferior_every_w all pass. B1-retry's BEST: none.
4. **B2, best-check against O4-r2b (21/11/11 blocks): BEST holds.** Family of 39, no Holm rejection. The smallest p values all lean against O4-r2b, though:
   - o4_pwhopr pooled: +1.56%, 95% [+0.62%, +2.50%], p 0.0043, against a threshold of 0.00128 (rank 1).
   - o4_pwhopr packet: +2.06%, p 0.0072 (rank 2).
   - O4-r2 (arm o4r2a) uart: +2.85%, p 0.0102 (rank 3).
   - O4-r1 pooled: +1.30%, 95% [+0.27%, +2.33%], p 0.0204 (rank 4).

   decide.py's `adopt_o4r2` over B1-retry + B2 (`B.json`) is true.
5. **H2, campaign H rerun against O4-r2b (41/81/41 blocks): BEST FAILS.** It was run because O4-r2b changes firmware-inline (I1) and host-inline (X1). o4_pwhopr is faster than O4-r2b:
   - pooled: d +1.01% (SE 0.31%), 95% [+0.51%, +1.52%], p 0.00060, against a Holm threshold of 0.004167 (rank 1 of 12);
   - cli: d +1.43% (SE 0.50%), LB95 +0.59%, p 0.0033, against 0.004545 (rank 2).

   The step-down stops at uart: +0.92%, p 0.024 > 0.005. packet is +0.34%. w7bd_b3 (pooled -2.17%) and o4_hostall (pooled -4.88%) are slower.
6. **Adoption.** The task's rule is that O4-r2(b) is adopted only if B1, B2 and the H rerun all pass. H2 fails, so **O4-r2b is not adopted and O4 stays at O4-r1**. decide.py's `adopt_o4r2: true` in `B.json` covers only B1-retry and B2; it does not see H2, which the registered decide.py can only decide as a separate B2-type run.
7. **BEST against the final O4, O4-r1, fails:**
   - campaign H: o4_pwhopr cli +1.97% (p 0.00014, rank 1/12) and pooled +0.93% [+0.44%, +1.42%] (p 0.0011, rank 2/12);
   - B1: O4-r2 cli +1.34% (p 0.0025, rank 1/4).

## What would have to be absorbed

**A5**, firmware-inline's priced exhaustive selection (`mode = "exhaustive"`, `cost_model = "profile"`, o4-pwhopr.toml's params, in place of hot-loop). Every campaign that measured A5 found it faster:

| comparison | campaign | packet | uart | cli | pooled [95%] | BEST |
|---|---|---:|---:|---:|---|---|
| o4_pwhopr vs O4-r1 | H (41/81/41) | -0.11% | +0.47% | **+1.97%** | **+0.93% [+0.44%, +1.42%]** | faster (cli, pooled) |
| o4_pwhopr vs O4-r2b | H2 (41/81/41) | +0.34% | +0.92% | **+1.43%** | **+1.01% [+0.51%, +1.52%]** | faster (cli, pooled) |
| o4_pwhopr vs O4-r2b | B2 (21/11/11) | +2.06% | +2.11% | +0.68% | +1.56% [+0.62%, +2.50%] | not shown (p 0.0043 > 0.00128) |
| O4-r2 (A5 + I1 + X1 + X4) vs O4-r1 | B1 (41/81/41) | -1.21% (LB -2.87%) | -0.15% | **+1.34%** | +0.49% [-0.05%, +1.02%] | faster (cli) |
| O4-r2 (o4r2a) vs O4-r2b | B2 (21/11/11) | -0.66% | +2.85% | +1.46% | +1.12% [+0.10%, +2.13%] | not shown |

- **The composition is what failed, on packet only.** O4-r2's packet estimate is -1.21%, with SE 0.98% (the projection was 0.53%) and UB95 +0.44%, so a loss is not shown either. o4_pwhopr itself is A5 on top of O4-r1, with host-inline and SPEC kept and no ISB rule. Its packet scope never falls below LB95 -1.36%: -0.11% (H), +0.34% (H2) and +2.06% (B2).
- **On packet, O4-r2 differs from o4_pwhopr by I1, X1 and X4.** I1 makes 36 isb_boundary exits in 17 regions and changes the members of 14 roots (P5 `p5/packet-o4r2-isb-vs-pwhopr.json`). X1 drops the one host-inline site. Which of the three, if any, costs on packet is not measured; it is a hypothesis.
- **The evident next candidate is o4_pwhopr's own pipeline,** O4-r1 + A5, as O4-r3. On campaign H's data it would already meet ADD-confirm against O4-r1: pooled LB95 +0.44% > 0, and LB95_w packet -1.36%, uart -0.25%, cli +1.14%, all above -2.02%. It would still need its own B1, B2 and H rerun.
- Repeating B1 for O4-r2 with more packet blocks is the other route. Either is a new registration (the registered B1 retry is used up), so it is the lead's decision.

## B1 rule (settled before any B timing)

`p6/B1-rule.md` (sha256 `4c53346f…`, as recorded in the B1 plans), written before any B plan or timing existed. The first draft of the B1 plans was deleted and remade before any timing, because the text describing the order of `items` was wrong in the rule file and in the wrapper (the list itself was unchanged).

- **The choice was ADD-confirm,** not the REMOVE that `P5.decide.json`'s `composition.o4r2` (`contains_addon: false`) would have implied. O4-r2 carries A5 as a speed claim, and ADD-confirm is the stricter rule.
- **The B1 plans carry the amended composition** (`x_composition` and `x_composition_amendment`):
  - addons `[o4_pwhopr]`, `contains_addon: true`;
  - items A5, X1, X4, I1;
  - `a2_pooled_d[o4_pwhopr] = 0.009299589` (its H pooled d, so that decide.py's retry rule can name it).
- **It made no difference to the outcome.** With REMOVE, B1 would also have failed, on packet non-inferiority.

## O4-r2b (B1 retry build and gates)

- **Pipeline.** `design/pipelines/O4-r2b.toml`, name "O4-r2b", sha256 `a82f3eadb71d0c22…` for the build copy. It is O4-r2.toml with A5 undone: the firmware-inline entry is exactly o4-isb.toml's (hot-loop, min_call_count 1000, isb_boundary true, planner whopr, member_slot_skip on), and the passes are chain-returns, chain-static and firmware-inline. tomllib checks:
  - passes = O4-r1's minus host-inline and the SPEC bundle, with o4-isb's firmware-inline;
  - capabilities = o4-nohi's (no INLINE_CALL);
  - the build copy `o4r2b.toml` differs only in name and description.

  `p6/make_r2b.py` wrote both. "O4-r2b" does not match scopt's O4 preflight name pattern `O4(-r\d+)?`; the copy is used as P5 did for o4r2.
- **Build.** `p6/build_r2b.sh` ran at fd-int b35cdb6, 04:22:46-04:23:39, through each workload's own wrapper (nice -n 10 taskset -c 21-31). The argv is P5's o4r2 line with only `--pipeline` and `--store` changed (`p6/o4r2b-commands.txt`; `cli/cmds.json` gains `o4r2b`, with a backup in `p6/backup/cli/`).

  | | rc | wall | peak RSS | cache.sc sha256 |
  |---|---:|---:|---:|---|
  | packet | 0 | 53 s | 936,088 KiB | `5c87d78be87a3437…` |
  | uart | 0 | 37.0 s | 785,176 KiB | `77e0ffef7c528051…` |
  | cli | 0 | 48.5 s | 1,041,840 KiB | `dba92a79e7784902…` |

  QEMU sha256 and inode were unchanged before and after (`p6/qemu-identity.txt`).
- **G0.** `p6/gates_r2b.sh` ran the same per-workload functional-check scripts as P5: packet `gate.py`, uart `fcheck.py`, cli `gates.py`. All three report OK, with regions replays OK. The aggregators were rerun: packet `receipts.py` + `assess.py`, uart `receipts.py` + `gates.py`, cli `summarize.py`. `packet/assess.py`, `uart/gates.py` and `cli/summarize.py` gained an o4r2b branch, and `driver/make_gates.py` gained o4r2b in BUILT. `make_gates.py --force` then rewrote `gates/{packet,uart,cli,all}.json` (all.json sha256 `c0895ec5…`): **o4r2b g0 true on packet, uart and cli**.
  - Every other arm's record is unchanged, key by key, against the backups in `p6/backup/`. In the gates files only the evidence strings changed (they quote the regenerated records' sha256); g0, checks and cache sha256 are unchanged.
  - The driver tests pass (60 passed).
- **Validity** (`p6/o4r2b_validity.py`, adapted from P5's checker; `p6/<w>-o4r2b-validity.json`): every item true on every workload:
  - pipeline_copy_equals_O4_r2b, passes_are_O4_r2b, stage_gate_all_edited_or_no_change;
  - no_host_inline_receipt, spec_passes_absent;
  - firmware_inline_hot_loop, isb_boundary_true, member_slot_skip_enabled;
  - build_within_8GiB_60min.

  The firmware-inline roots equal o4_isb's on every workload (48 / 19 / 7 regions, members 5,952 / 614 / 820; isb_boundary exits 33 / 2 / 0). The cache is not byte-equal to o4, o4_isb, o4_nohi, o4_nospec, o4_pwhopr or o4r2 on any workload, so it was timed on all three.
- **Regions replay** (gate runs, untimed):
  - packet: 48 loaded, 46 activated, 52 region entries, 48/0 polls verified/unverified;
  - uart commands: 19 loaded and activated, 19/0 polls.

## Naming in the B1-retry, B2 and H2 runs (deviation, recorded in every plan)

The registered decide.py decides B1 only for the arm named `o4r2`, and fixes B2's reference to `o4r2`/`o4r2-aa`. `make_plan.py B1 --retry` would resolve `o4r2` from `opt/o4r2/store`, which holds the first O4-r2 build.

- **Renamed arms.** In `plans/B1-retry`, `plans/B2` and `plans/H2`, the plan arm **`o4r2` is O4-r2b** (opt/o4r2b, `arms/o4r2b/launch.json`), and the first O4-r2 build is timed as **`o4r2a`** (decide.py role "other"). Each plan records this in `x_o4r2_is` / `x_arm_map`. In decide.py's tables, the item label "O4-r2" for arm o4r2 therefore means O4-r2b in those runs.
- **Plan generation.** The plans were made by make_plan.py's own code (`plans_B1(retry=True, drop=o4_pwhopr)`, `resolve_all`, `build_plan`), with a resolver that maps the names: `p6/make_b1_retry_plan.py` and `p6/make_b2_r2b_plans.py`. B1's own plans came from `p6/make_b_plans.py`.
- **G0 for the retry.** decide.py reads G0 for `o4r2` from `p6/gates-B1-retry.json` (sha256 `010edf2a…`): gates/all.json with arms.o4r2 replaced by its o4r2b entry, and nothing else changed.
- **Pre-flight results.** validate_plans.py reports exactly these expected problems and nothing else:
  - `gates g0/sha … o4r2`: gates/<w>.json's o4r2 is the first build;
  - `anchor sha … o4r2a`: not a gates entry name.

  The mapping was checked: the plans' o4r2 sha256 = gates o4r2b's (g0 true), and o4r2a's = gates o4r2's (g0 true).
- **H2's label.** H2 carries `x_campaign: "B2"` (`x_campaign_label: "H2"`), because the registered decide.py fixes H's reference to o4/o4-aa. It was decided in its own decide.py run, with BEST family = H2's arms. Its seed is the task's 20261128 + 100 = 20261228, at x_attempt 1.
- **B2's arms beyond §3.8's list:**
  - o4_nocp: X2, accepted but moot, so still an accepted single-item arm;
  - o4_pwhopr: the dropped add-on, faster than O4-r1 in H;
  - o4r2a: O4-r2, faster than O4-r1 on cli in B1.

  c7b3 has O4-r1's bytes on every workload, so it is an alias of o4 and was not timed.

## Files

- **decide.py outputs** (cwd /data/scfix-o4):

  | output | runs decided | sha256 | stdout |
  |---|---|---|---|
  | `B1.json` | B1 | `8f748cd7…` | `B1.stdout.txt` |
  | `B1-retry.json` | B1-retry | `2d96a217…` | `B1-retry.stdout.txt` |
  | `B2.json` | B2 | `f884eeb0…` | `B2.stdout.txt` |
  | `H2.json` | H2 | `32ae3283…` | `H2.stdout.txt` |
  | `B.json` | B1-retry + B2 together (`adopt_o4r2`) | `42947dbe…` | `B.stdout.txt` |

  The gates file was `gates/all.json` for B1, and `p6/gates-B1-retry.json` for the others.
- **Plans:** `plans/{B1,B1-retry,B2,H2}/{packet,uart,cli}.json`, each with `validation.json`.
- **Driver output:** `<w>/timing-{B1,B1-retry,B2,H2}/` and the `.log` files.
- **Hygiene:** `hygiene/{B1,B1-retry,B2,H2}/{before,after,diag}.json`, `launch.log` and `start.marker`.
- **Step files:** `p6/`, holding the scripts, the B1 rule, the O4-r2b build/gate/validity outputs, the backups, `final_o4_vs.json` and the `runinfo-*.json` files.

## Hygiene (every campaign)

- **Launch.** The three workloads of one campaign were launched concurrently, one driver each, under `taskset -c` on CPU 10 / 20 / 12, with `--settle-seconds 30`. Each campaign launched at a 1-minute load at or below 2.0:

  | campaign | launched | 1-min load |
  |---|---|---:|
  | B1 | 04:11:18 | 1.43 |
  | B1-retry | 04:32:33 | 1.45 |
  | B2 | 04:42:18 | 1.23 |
  | H2 | 04:50:16 | 1.83, after waiting for the load to fall below 2.0 (it was 2.78 right after B2) |

  Loads before block 0 (2.1-3.2) include the drivers' own staging. The 1-minute load reached 3.37 at 04:56 during H2, from the three drivers plus other users' processes.
- **Isolation.** Campaigns ran one at a time. The O4-r2b build and gates ran between B1 and B1-retry, with no driver running. No grep or find touched a cache while timing ran: only `kill -0`, `wc -l` on observations.jsonl and log tails.
- **QEMU** `/data/qemu-scfix-bin/aacca05/qemu-system-arm`: sha256 `951775b0…eebc60`, inode 87162887, dev 2050, size 137799456, mtime_ns 1795640690884852222. It was identical before and after every campaign (`qemu_unchanged: true` ×4).
- **find -newer start.marker** over /data/scfix-r2, /data/scfix-campaign, /data/trebuchet-clean, /data/qemu-scfix-bin, /data/fastdyn/.git/worktrees/fd-int and /data/fastdyn-work/qemu-fu found nothing, for all four campaigns.
- **fd-int** is on branch o4 at HEAD b35cdb6, `git --no-optional-locks status --porcelain` empty before and after every campaign. Nothing was committed. git ran with `--no-optional-locks` throughout, to avoid the index.lock write noted in P5.
- **Bytecode.** No .pyc, __pycache__ or .pytest_cache in fd-int, the driver directory or p6. QEMU_SC_SEALED_SKIP_IRQ appears in no plan or launch (validate_plans "no skip-irq" passed).
- **Driver checks.** No driver failed. All 16-word checks, warm-cache identities, private-region identities, v7 counter checks and QEMU identity checks passed; the driver raises on any of them. H's code-buffer collision (`allocate supercache code buffer: File exists`) did not recur.

## B1: O4-r2 against O4-r1 (ADD-confirm)

| | packet | uart | cli |
|---|---|---|---|
| CPU | 10 | 20 | 12 |
| seed | 20261127 | 20261127 | 20261127 |
| blocks (burn-in block 0 + timed) | 41 (1 + 40) | 81 (1 + 80) | 41 (1 + 40) |
| timed arms (incl. the A/A copy) | 3 | 3 | 3 |
| runs | 123 | 486 | 246 |
| settle | 30 s (argument) | 30 s (argument) | 30 s (argument) |
| 1-min load at start / before block 0 | 1.43 / 2.29 | 1.43 / 2.29 | 1.43 / 2.12 |
| block 0 ended / last block ended | 04:12:28 / 04:14:52 | 04:12:28 / 04:18:48 | 04:12:09 / 04:12:45 |
| SD of A/A block log-ratios (timed) | 6.09% | 4.29% | 3.24% |
| runs > 1.3x arm/case median: burn-in / timed (max) | 0 / 0 (-x) | 0 / 0 (-x) | 0 / 0 (-x) |
| timed arms | o4, o4r2, o4-aa | o4, o4r2, o4-aa | o4, o4r2, o4-aa |

| campaign | workload | attempt | blocks | a_w | 99% interval | SE | verdict |
|---|---|---:|---:|---:|---|---:|---|
| B1 | packet | 1 | 40 | +0.79% | [-1.47%, +3.06%] | 0.87% | valid |
| B1 | uart | 1 | 80 | +0.91% | [-0.33%, +2.17%] | 0.51% | valid |
| B1 | cli | 1 | 40 | -0.25% | [-1.61%, +1.21%] | 0.53% | valid |

BEST family 4 at alpha 0.05; faster than the reference: ['o4r2']; holds: False

| arm | item | scope | d | SE | df | LB95 | UB95 | ratio T(arm)/T(ref) | p | Holm rank / threshold | verdict |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| o4r2 | O4-r2 | packet | -1.21% | 0.98% | 39.0 | -2.87% | +0.44% | 1.0122 | 0.8878 | 4 / 0.050000 | not_shown |
| o4r2 | O4-r2 | uart | -0.15% | 0.51% | 79.0 | -1.00% | +0.70% | 1.0015 | 0.6159 | 3 / 0.025000 | not_shown |
| o4r2 | O4-r2 | cli | +1.34% | 0.45% | 39.0 | +0.58% | +2.10% | 0.9867 | 0.0025 | 1 / 0.012500 | faster_than_o4 |
| o4r2 | O4-r2 | pooled | +0.49% | 0.32% | 115.0 | -0.05% | +1.02% | 0.9952 | 0.0660 | 2 / 0.016667 | not_shown |

B1 o4r2: rule ADD-confirm, passed False, g0 True, retry False, checks {'noninferior_every_w': False, 'pooled_lb_gt_0': False}, next: drop o4_pwhopr (smallest A2 pooled d) and repeat B1 once

| scope | d | SE | df | LB95 | UB95 | ratio T(o4r2)/T(o4) | p | LB95 > log 0.98 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| packet | -1.21% | 0.98% | 39 | -2.87% | +0.44% | 1.0122 | 0.887802 | NO |
| uart | -0.15% | 0.51% | 79 | -1.00% | +0.70% | 1.0015 | 0.615941 | yes |
| cli | +1.34% | 0.45% | 39 | +0.58% | +2.10% | 0.9867 | 0.002537 | yes |
| pooled | +0.49% | 0.32% | 115.0 | -0.05% | +1.02% | 0.9952 | 0.065987 | - |


## B1 retry: O4-r2b against O4-r1 (REMOVE; the arm o4r2 here is O4-r2b)

| | packet | uart | cli |
|---|---|---|---|
| CPU | 10 | 20 | 12 |
| seed | 20261127 | 20261127 | 20261127 |
| blocks (burn-in block 0 + timed) | 41 (1 + 40) | 81 (1 + 80) | 41 (1 + 40) |
| timed arms (incl. the A/A copy) | 3 | 3 | 3 |
| runs | 123 | 486 | 246 |
| settle | 30 s (argument) | 30 s (argument) | 30 s (argument) |
| 1-min load at start / before block 0 | 1.45 / 2.10 | 1.45 / 2.10 | 1.45 / 2.14 |
| block 0 ended / last block ended | 04:33:42 / 04:36:06 | 04:33:42 / 04:39:57 | 04:33:23 / 04:33:55 |
| SD of A/A block log-ratios (timed) | 7.72% | 4.43% | 3.51% |
| runs > 1.3x arm/case median: burn-in / timed (max) | 0 / 0 (-x) | 0 / 0 (-x) | 2 / 0 (1.736x) |
| timed arms | o4, o4r2, o4-aa | o4, o4r2, o4-aa | o4, o4r2, o4-aa |

| campaign | workload | attempt | blocks | a_w | 99% interval | SE | verdict |
|---|---|---:|---:|---:|---|---:|---|
| B1 | packet | 1 | 40 | +1.13% | [-1.53%, +4.00%] | 1.06% | valid |
| B1 | uart | 1 | 80 | +0.59% | [-0.74%, +1.97%] | 0.50% | valid |
| B1 | cli | 1 | 40 | -0.24% | [-1.64%, +1.15%] | 0.55% | valid |

BEST family 4 at alpha 0.05; faster than the reference: none; holds: True

| arm | item | scope | d | SE | df | LB95 | UB95 | ratio T(arm)/T(ref) | p | Holm rank / threshold | verdict |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| o4r2 | O4-r2 | packet | +0.85% | 0.86% | 39.0 | -0.60% | +2.29% | 0.9916 | 0.1643 | 1 / 0.012500 | not_shown |
| o4r2 | O4-r2 | uart | -0.10% | 0.50% | 79.0 | -0.93% | +0.73% | 1.0010 | 0.5764 | 4 / 0.050000 | not_shown |
| o4r2 | O4-r2 | cli | +0.22% | 0.42% | 39.0 | -0.49% | +0.93% | 0.9978 | 0.3009 | 3 / 0.025000 | not_shown |
| o4r2 | O4-r2 | pooled | +0.18% | 0.30% | 113.7 | -0.32% | +0.68% | 0.9982 | 0.2725 | 2 / 0.016667 | not_shown |

B1 o4r2: rule REMOVE, passed True, g0 True, retry True, checks {'lb_gt_log099': True, 'noninferior_every_w': True, 'ub_ge_0': True}

| scope | d | SE | df | LB95 | UB95 | ratio T(o4r2)/T(o4) | p | LB95 > log 0.98 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| packet | +0.85% | 0.86% | 39 | -0.60% | +2.29% | 0.9916 | 0.164304 | yes |
| uart | -0.10% | 0.50% | 79 | -0.93% | +0.73% | 1.0010 | 0.576436 | yes |
| cli | +0.22% | 0.42% | 39 | -0.49% | +0.93% | 0.9978 | 0.300921 | yes |
| pooled | +0.18% | 0.30% | 113.7 | -0.32% | +0.68% | 0.9982 | 0.272501 | - |


## B2: best-check against O4-r2b (arm o4r2 = O4-r2b, o4r2a = O4-r2)

d > 0 means the arm is faster than O4-r2b. The ratio is T(arm)/T(O4-r2b), so a value below 1 means the arm is faster. The burn-in block 0 on cli had 10 runs above 1.3x their median (max 8.05x, o4r2-aa help); decide.py drops block 0 as pre-registered. uart has one timed run above 1.3x (o4_isb help, block 1, 1.46x).

| | packet | uart | cli |
|---|---|---|---|
| CPU | 10 | 20 | 12 |
| seed | 20261129 | 20261129 | 20261129 |
| blocks (burn-in block 0 + timed) | 21 (1 + 20) | 11 (1 + 10) | 11 (1 + 10) |
| timed arms (incl. the A/A copy) | 11 | 12 | 11 |
| runs | 231 | 264 | 242 |
| settle | 30 s (argument) | 30 s (argument) | 30 s (argument) |
| 1-min load at start / before block 0 | 1.23 / 3.15 | 1.23 / 3.14 | 1.23 / 3.24 |
| block 0 ended / last block ended | 04:45:14 / 04:49:34 | 04:45:22 / 04:48:38 | 04:44:04 / 04:44:35 |
| SD of A/A block log-ratios (timed) | 4.36% | 4.95% | 4.45% |
| runs > 1.3x arm/case median: burn-in / timed (max) | 0 / 0 (-x) | 0 / 1 (1.464x) | 10 / 0 (8.052x) |
| timed arms | o4r2, o4, s6b3, p7, fl3, o4_isb, o4_nohi, o4_pwhopr, o4r2a, o4_rank, o4r2-aa | o4r2, o4, s6b3, p7, fl3, o4_isb, o4_nohi, o4_nocp, o4_nospec, o4_pwhopr, o4r2a, o4r2-aa | o4r2, o4, s6b3, p7, fl3, o4_nohi, o4_nocp, o4_nospec, o4_pwhopr, o4r2a, o4r2-aa |

| campaign | workload | attempt | blocks | a_w | 99% interval | SE | verdict |
|---|---|---:|---:|---:|---|---:|---|
| B2 | packet | 1 | 20 | +1.20% | [-1.11%, +3.71%] | 0.91% | valid |
| B2 | uart | 1 | 10 | -1.71% | [-6.03%, +2.85%] | 1.76% | valid |
| B2 | cli | 1 | 10 | +0.90% | [-3.48%, +3.88%] | 1.38% | valid |

BEST family 39 at alpha 0.05; faster than the reference: none; holds: True

| arm | item | scope | d | SE | df | LB95 | UB95 | ratio T(arm)/T(ref) | p | Holm rank / threshold | verdict |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| c7b3 | c7b3 | packet (alias of o4) | +0.92% | 1.06% | 19.0 | -0.92% | +2.76% | 0.9909 | 0.1994 | 18 / 0.002273 | not_shown |
| c7b3 | c7b3 | uart (alias of o4) | +1.63% | 1.16% | 9.0 | -0.50% | +3.76% | 0.9838 | 0.0972 | 12 / 0.001786 | not_shown |
| c7b3 | c7b3 | cli (alias of o4) | +1.38% | 0.96% | 9.0 | -0.38% | +3.14% | 0.9863 | 0.0928 | 11 / 0.001724 | not_shown |
| c7b3 | c7b3 | pooled (alias of o4) | +1.30% | 0.61% | 31.6 | +0.27% | +2.33% | 0.9871 | 0.0204 | 4 / 0.001389 | not_shown |
| fl3 | fl3 | packet | -6.35% | 1.43% | 19.0 | -8.83% | -3.87% | 1.0655 | 0.9999 | 33 / 0.007143 | slower_than_o4 |
| fl3 | fl3 | uart | -13.98% | 1.36% | 9.0 | -16.49% | -11.48% | 1.1501 | 1.0000 | 35 / 0.010000 | slower_than_o4 |
| fl3 | fl3 | cli | -16.81% | 1.20% | 9.0 | -19.02% | -14.61% | 1.1831 | 1.0000 | 37 / 0.016667 | slower_than_o4 |
| fl3 | fl3 | pooled | -12.96% | 0.76% | 30.1 | -14.26% | -11.66% | 1.1384 | 1.0000 | 39 / 0.050000 | slower_than_o4 |
| o4 | O4-r1 | packet | +0.92% | 1.06% | 19.0 | -0.92% | +2.76% | 0.9909 | 0.1994 | 18 / 0.002273 | not_shown |
| o4 | O4-r1 | uart | +1.63% | 1.16% | 9.0 | -0.50% | +3.76% | 0.9838 | 0.0972 | 12 / 0.001786 | not_shown |
| o4 | O4-r1 | cli | +1.38% | 0.96% | 9.0 | -0.38% | +3.14% | 0.9863 | 0.0928 | 11 / 0.001724 | not_shown |
| o4 | O4-r1 | pooled | +1.30% | 0.61% | 31.6 | +0.27% | +2.33% | 0.9871 | 0.0204 | 4 / 0.001389 | not_shown |
| o4_isb | I1 | packet | +0.10% | 0.87% | 19.0 | -1.42% | +1.61% | 0.9990 | 0.4571 | 26 / 0.003571 | not_shown |
| o4_isb | I1 | uart | +1.39% | 2.55% | 9.0 | -3.29% | +6.06% | 0.9862 | 0.2999 | 23 / 0.002941 | not_shown |
| o4_isb | I1 | cli (alias of o4) | +1.38% | 0.96% | 9.0 | -0.38% | +3.14% | 0.9863 | 0.0928 | 11 / 0.001724 | not_shown |
| o4_isb | I1 | pooled | +0.72% | 0.63% | 29.1 | -0.35% | +1.79% | 0.9928 | 0.1304 | 15 / 0.002000 | not_shown |
| o4_nocp | X2 | packet (alias of o4) | +0.92% | 1.06% | 19.0 | -0.92% | +2.76% | 0.9909 | 0.1994 | 18 / 0.002273 | not_shown |
| o4_nocp | X2 | uart | +2.71% | 1.77% | 9.0 | -0.54% | +5.95% | 0.9733 | 0.0804 | 10 / 0.001667 | not_shown |
| o4_nocp | X2 | cli | -0.43% | 1.29% | 9.0 | -2.79% | +1.94% | 1.0043 | 0.6254 | 27 / 0.003846 | not_shown |
| o4_nocp | X2 | pooled | +0.79% | 0.75% | 35.2 | -0.47% | +2.05% | 0.9922 | 0.1489 | 16 / 0.002083 | not_shown |
| o4_nohi | X1 | packet | +0.49% | 0.90% | 19.0 | -1.07% | +2.04% | 0.9951 | 0.2974 | 22 / 0.002778 | not_shown |
| o4_nohi | X1 | uart | +2.28% | 1.15% | 9.0 | +0.17% | +4.39% | 0.9775 | 0.0394 | 7 / 0.001515 | not_shown |
| o4_nohi | X1 | cli | -1.22% | 1.05% | 9.0 | -3.14% | +0.70% | 1.0123 | 0.8634 | 30 / 0.005000 | not_shown |
| o4_nohi | X1 | pooled | +0.42% | 0.59% | 35.7 | -0.57% | +1.41% | 0.9959 | 0.2414 | 20 / 0.002500 | not_shown |
| o4_nospec | X4 | packet (alias of o4) | +0.92% | 1.06% | 19.0 | -0.92% | +2.76% | 0.9909 | 0.1994 | 18 / 0.002273 | not_shown |
| o4_nospec | X4 | uart | +1.89% | 0.96% | 9.0 | +0.13% | +3.65% | 0.9813 | 0.0404 | 8 / 0.001563 | not_shown |
| o4_nospec | X4 | cli | +0.18% | 0.99% | 9.0 | -1.64% | +2.00% | 0.9982 | 0.4304 | 25 / 0.003333 | not_shown |
| o4_nospec | X4 | pooled | +1.02% | 0.58% | 31.1 | +0.04% | +2.00% | 0.9898 | 0.0440 | 9 / 0.001613 | not_shown |
| o4_pwhopr | A5 | packet | +2.06% | 0.77% | 19.0 | +0.74% | +3.39% | 0.9796 | 0.0072 | 2 / 0.001316 | not_shown |
| o4_pwhopr | A5 | uart | +2.11% | 1.72% | 9.0 | -1.04% | +5.27% | 0.9791 | 0.1254 | 13 / 0.001852 | not_shown |
| o4_pwhopr | A5 | cli | +0.68% | 0.92% | 9.0 | -1.00% | +2.36% | 0.9933 | 0.2396 | 19 / 0.002381 | not_shown |
| o4_pwhopr | A5 | pooled | +1.56% | 0.56% | 32.3 | +0.62% | +2.50% | 0.9845 | 0.0043 | 1 / 0.001282 | not_shown |
| o4_rank | A3 | packet | +0.68% | 1.06% | 19.0 | -1.15% | +2.50% | 0.9933 | 0.2650 | 21 / 0.002632 | not_shown |
| o4_rank | A3 | uart (alias of o4) | +1.63% | 1.16% | 9.0 | -0.50% | +3.76% | 0.9838 | 0.0972 | 12 / 0.001786 | not_shown |
| o4_rank | A3 | cli (alias of o4) | +1.38% | 0.96% | 9.0 | -0.38% | +3.14% | 0.9863 | 0.0928 | 11 / 0.001724 | not_shown |
| o4_rank | A3 | pooled | +1.22% | 0.61% | 31.7 | +0.19% | +2.24% | 0.9879 | 0.0269 | 5 / 0.001429 | not_shown |
| o4r2a | o4r2a | packet | -0.66% | 0.96% | 19.0 | -2.31% | +1.00% | 1.0066 | 0.7492 | 29 / 0.004545 | not_shown |
| o4r2a | o4r2a | uart | +2.85% | 1.01% | 9.0 | +0.99% | +4.70% | 0.9719 | 0.0102 | 3 / 0.001351 | not_shown |
| o4r2a | o4r2a | cli | +1.46% | 1.19% | 9.0 | -0.73% | +3.65% | 0.9855 | 0.1259 | 14 / 0.001923 | not_shown |
| o4r2a | o4r2a | pooled | +1.12% | 0.60% | 34.4 | +0.10% | +2.13% | 0.9889 | 0.0361 | 6 / 0.001471 | not_shown |
| p7 | p7 | packet | -3.69% | 0.75% | 19.0 | -4.98% | -2.39% | 1.0376 | 1.0000 | 34 / 0.008333 | slower_than_o4 |
| p7 | p7 | uart | -7.00% | 1.51% | 9.0 | -9.77% | -4.22% | 1.0725 | 0.9994 | 32 / 0.006250 | slower_than_o4 |
| p7 | p7 | cli | -7.69% | 0.62% | 9.0 | -8.82% | -6.55% | 1.0799 | 1.0000 | 36 / 0.012500 | slower_than_o4 |
| p7 | p7 | pooled | -6.14% | 0.46% | 24.7 | -6.92% | -5.36% | 1.0634 | 1.0000 | 38 / 0.025000 | slower_than_o4 |
| s6b3 | s6b3 | packet | +0.64% | 0.64% | 19.0 | -0.47% | +1.76% | 0.9936 | 0.1650 | 17 / 0.002174 | not_shown |
| s6b3 | s6b3 | uart | +0.29% | 1.21% | 9.0 | -1.92% | +2.50% | 0.9971 | 0.4080 | 24 / 0.003125 | not_shown |
| s6b3 | s6b3 | cli | -2.22% | 0.92% | 9.0 | -3.92% | -0.53% | 1.0225 | 0.9802 | 31 / 0.005556 | slower_than_o4 |
| s6b3 | s6b3 | pooled | -0.20% | 0.48% | 35.7 | -1.02% | +0.62% | 1.0020 | 0.6585 | 28 / 0.004167 | not_shown |




## H2: campaign H against O4-r2b (arm o4r2 = O4-r2b)

| | packet | uart | cli |
|---|---|---|---|
| CPU | 10 | 20 | 12 |
| seed | 20261228 | 20261228 | 20261228 |
| blocks (burn-in block 0 + timed) | 41 (1 + 40) | 81 (1 + 80) | 41 (1 + 40) |
| timed arms (incl. the A/A copy) | 5 | 5 | 5 |
| runs | 205 | 810 | 410 |
| settle | 30 s (argument) | 30 s (argument) | 30 s (argument) |
| 1-min load at start / before block 0 | 1.83 / 2.45 | 1.83 / 2.29 | 1.83 / 2.72 |
| block 0 ended / last block ended | 04:52:20 / 04:56:45 | 04:52:05 / 05:03:15 | 04:51:40 / 04:53:36 |
| SD of A/A block log-ratios (timed) | 4.54% | 4.22% | 4.00% |
| runs > 1.3x arm/case median: burn-in / timed (max) | 0 / 0 (-x) | 0 / 2 (1.438x) | 4 / 0 (3.967x) |
| timed arms | o4r2, w7bd_b3, o4_hostall, o4_pwhopr, o4r2-aa | o4r2, w7bd_b3, o4_hostall, o4_pwhopr, o4r2-aa | o4r2, w7bd_b3, o4_hostall, o4_pwhopr, o4r2-aa |

| campaign | workload | attempt | blocks | a_w | 99% interval | SE | verdict |
|---|---|---:|---:|---:|---|---:|---|
| B2 | packet | 1 | 40 | +1.16% | [-0.64%, +3.36%] | 0.80% | valid |
| B2 | uart | 1 | 80 | +0.83% | [-0.37%, +1.94%] | 0.43% | valid |
| B2 | cli | 1 | 40 | +0.22% | [-1.46%, +1.85%] | 0.62% | valid |

BEST family 12 at alpha 0.05; faster than the reference: ['o4_pwhopr']; holds: False

| arm | item | scope | d | SE | df | LB95 | UB95 | ratio T(arm)/T(ref) | p | Holm rank / threshold | verdict |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| o4_hostall | A6 | packet | -3.74% | 0.65% | 39.0 | -4.85% | -2.64% | 1.0381 | 1.0000 | 8 / 0.010000 | slower_than_o4 |
| o4_hostall | A6 | uart | -11.59% | 0.68% | 79.0 | -12.71% | -10.46% | 1.1229 | 1.0000 | 11 / 0.025000 | slower_than_o4 |
| o4_hostall | A6 | cli | -1.25% | 0.55% | 39.0 | -2.17% | -0.32% | 1.0126 | 0.9857 | 7 / 0.008333 | slower_than_o4 |
| o4_hostall | A6 | pooled | -4.88% | 0.36% | 127.5 | -5.48% | -4.29% | 1.0501 | 1.0000 | 10 / 0.016667 | slower_than_o4 |
| o4_pwhopr | A5 | packet | +0.34% | 0.75% | 39.0 | -0.92% | +1.59% | 0.9966 | 0.3260 | 5 / 0.006250 | not_shown |
| o4_pwhopr | A5 | uart | +0.92% | 0.46% | 79.0 | +0.16% | +1.68% | 0.9909 | 0.0240 | 3 / 0.005000 | not_shown |
| o4_pwhopr | A5 | cli | +1.43% | 0.50% | 39.0 | +0.59% | +2.27% | 0.9858 | 0.0033 | 2 / 0.004545 | faster_than_o4 |
| o4_pwhopr | A5 | pooled | +1.01% | 0.31% | 142.7 | +0.51% | +1.52% | 0.9899 | 0.0006 | 1 / 0.004167 | faster_than_o4 |
| w7bd_b3 | w7bd_b3 | packet | -1.27% | 0.68% | 39.0 | -2.42% | -0.12% | 1.0128 | 0.9644 | 6 / 0.007143 | slower_than_o4 |
| w7bd_b3 | w7bd_b3 | uart | -9.53% | 0.75% | 79.0 | -10.78% | -8.28% | 1.1000 | 1.0000 | 12 / 0.050000 | slower_than_o4 |
| w7bd_b3 | w7bd_b3 | cli | +0.27% | 0.47% | 39.0 | -0.52% | +1.06% | 0.9973 | 0.2820 | 4 / 0.005556 | not_shown |
| w7bd_b3 | w7bd_b3 | pooled | -2.17% | 0.34% | 103.9 | -2.74% | -1.60% | 1.0219 | 1.0000 | 9 / 0.012500 | slower_than_o4 |




## Final O4 (O4-r1) against every recorded arm

Each arm's latest estimate with O4-r1 as the reference: A (screen), A2, H, B1 and B1-retry, from `p5/P5.decide.json`, `B1.json` and `B1-retry.json`. The ratio is T(arm)/T(O4-r1), so **below 1 means the arm is faster than O4-r1**. `p6/final_o4_vs.json` has d, the bounds and p.

| arm | measured in | T(arm)/T(O4-r1) pooled | packet | uart | cli | same bytes as O4-r1 on |
|---|---|---:|---:|---:|---:|---|
| b3l | A | 1.0234 | 1.0165 | 1.0299 | 1.0345 | - |
| c7b3 | A | 1 (same bytes) | - | - | - | cli, packet, uart |
| fl3 | A | 1.1583 | 1.0814 | 1.1859 | 1.2044 | - |
| fl3b3 | A2 | 0.998 | 0.9799 | 1.0012 | 1.0032 | - |
| fl3b3p | A | 1.007 | 1.0061 | 0.993 | 1.0304 | - |
| o4_chained | A2 | 1.0009 | 0.982 | 1.01 | - | cli |
| o4_hostall | H | 1.0461 | 1.0413 | 1.1325 | 1.0032 | - |
| o4_hotgraph | A | 1.037 | 1.0419 | 1.0754 | 1.0229 | - |
| o4_isb | A2 | 0.9981 | 0.9959 | 0.9989 | - | cli |
| o4_legacy | A2 | 0.9963 | 0.9963 | - | - | cli, uart |
| o4_min2 | A2 | 0.999 | 0.9971 | 1.0056 | 0.9922 | - |
| o4_nocp | A2 | 0.9942 | - | 0.9949 | 0.9933 | packet |
| o4_nohi | A2 | 0.9977 | 0.9938 | 0.9997 | 0.9977 | - |
| o4_noregions | A2 | 1.0018 | 0.9933 | 0.9957 | 1.0166 | - |
| o4_nospec | A2 | 0.9968 | - | 1.0001 | 0.992 | packet |
| o4_pwhopr | H | 0.9907 | 1.0011 | 0.9953 | 0.9805 | - |
| o4_rank | A2 | 0.985 | 0.985 | - | - | cli, uart |
| o4_tasks | A2 | 0.9903 | 0.9903 | - | - | cli, uart |
| o4r2 (O4-r2: A5+I1+X1+X4) | B1 | 0.9952 | 1.0122 | 1.0015 | 0.9867 | - |
| o4r2b (O4-r2b: I1+X1+X4) | B1-retry | 0.9982 | 0.9916 | 1.001 | 0.9978 | - |
| p7 | A | 1.0469 | 1.0437 | 1.0465 | 1.0524 | - |
| s6 | A | 1.1814 | 1.1526 | 1.1597 | 1.2453 | - |
| s6b3 | A | 1.008 | 0.9996 | 1.0019 | 1.0249 | - |
| w7bd_b3 | H | 1.0363 | 1.0319 | 1.1081 | 0.995 | - |

## Driver's own statistics (decide nothing)

The median of per-block reference/arm ratios over all blocks, including burn-in. Above 1 means the second-named arm is faster.

```
== B1
-- packet
A/A o4/o4-aa                 1.0054x [0.9909, 1.0286] 23/41 >1 p=0.533 not_significant band [0.9722, 1.0286]
o4/o4r2                      1.0005x [0.9648, 1.0173] 21/41 >1 p=1 within_noise
-- uart
A/A o4/o4-aa                 1.0136x [0.9995, 1.0201] 49/81 >1 p=0.0748 not_significant band [0.9803, 1.0201]
o4/o4r2                      1.0004x [0.9814, 1.0166] 41/81 >1 p=1 within_noise
-- cli
A/A o4/o4-aa                 0.9974x [0.9908, 1.0134] 19/41 >1 p=0.755 not_significant band [0.9867, 1.0134]
o4/o4r2                      1.0133x [0.9958, 1.0210] 24/41 >1 p=0.349 within_noise
== B1-retry
-- packet
A/A o4/o4-aa                 1.0099x [0.9858, 1.0232] 23/41 >1 p=0.533 not_significant band [0.9773, 1.0232]
o4/o4r2                      1.0060x [0.9885, 1.0312] 22/41 >1 p=0.755 within_noise
-- uart
A/A o4/o4-aa                 1.0088x [0.9890, 1.0223] 44/81 >1 p=0.505 not_significant band [0.9782, 1.0223]
o4/o4r2                      1.0025x [0.9915, 1.0150] 45/81 >1 p=0.374 within_noise
-- cli
A/A o4/o4-aa                 1.0012x [0.9838, 1.0117] 22/41 >1 p=0.755 not_significant band [0.9838, 1.0165]
o4/o4r2                      1.0044x [0.9881, 1.0145] 23/41 >1 p=0.533 within_noise
== B2
-- packet
A/A o4r2/o4r2-aa             1.0092x [0.9889, 1.0312] 12/21 >1 p=0.664 not_significant band [0.9698, 1.0312]
o4/o4r2                      1.0002x [0.9677, 1.0138] 11/21 >1 p=1 within_noise
s6b3/o4r2                    0.9843x [0.9675, 1.0080] 8/21 >1 p=0.383 within_noise
p7/o4r2                      1.0334x [1.0210, 1.0567] 18/21 >1 p=0.00149 within_noise
fl3/o4r2                     1.0476x [1.0313, 1.1077] 20/21 >1 p=2.1e-05 above_floor
o4_isb/o4r2                  0.9952x [0.9761, 1.0130] 10/21 >1 p=1 within_noise
o4_nohi/o4r2                 0.9953x [0.9649, 1.0143] 9/21 >1 p=0.664 within_noise
o4_pwhopr/o4r2               0.9831x [0.9494, 0.9928] 5/21 >1 p=0.0266 within_noise
o4r2a/o4r2                   0.9964x [0.9793, 1.0316] 9/21 >1 p=0.664 within_noise
o4_rank/o4r2                 0.9909x [0.9594, 1.0258] 10/21 >1 p=1 within_noise
-- uart
A/A o4r2/o4r2-aa             0.9832x [0.9473, 1.0366] 5/11 >1 p=1 not_significant band [0.9473, 1.0556]
o4/o4r2                      0.9900x [0.9778, 1.0301] 4/11 >1 p=0.549 within_noise
s6b3/o4r2                    1.0025x [0.9650, 1.0425] 7/11 >1 p=0.549 within_noise
p7/o4r2                      1.0761x [1.0348, 1.1176] 11/11 >1 p=0.000977 within_noise
fl3/o4r2                     1.1473x [1.1311, 1.1663] 11/11 >1 p=0.000977 above_floor
o4_isb/o4r2                  0.9761x [0.9447, 1.0359] 5/11 >1 p=1 within_noise
o4_nohi/o4r2                 0.9763x [0.9676, 1.0118] 3/11 >1 p=0.227 within_noise
o4_nocp/o4r2                 0.9942x [0.9494, 1.0094] 4/11 >1 p=0.549 within_noise
o4_nospec/o4r2               0.9873x [0.9601, 1.0117] 3/11 >1 p=0.227 within_noise
o4_pwhopr/o4r2               0.9853x [0.9659, 1.0093] 4/11 >1 p=0.549 within_noise
o4r2a/o4r2                   0.9838x [0.9622, 1.0080] 3/11 >1 p=0.227 within_noise
-- cli
A/A o4r2/o4r2-aa             1.0069x [0.9658, 1.0266] 7/11 >1 p=0.549 not_significant band [0.9658, 1.0354]
o4/o4r2                      0.9730x [0.9496, 1.0069] 4/11 >1 p=0.549 within_noise
s6b3/o4r2                    1.0108x [0.9879, 1.0267] 8/11 >1 p=0.227 within_noise
p7/o4r2                      1.0679x [1.0409, 1.1009] 10/11 >1 p=0.0117 above_floor
fl3/o4r2                     1.1820x [1.1185, 1.2103] 10/11 >1 p=0.0117 above_floor
o4_nohi/o4r2                 1.0013x [0.9784, 1.0375] 6/11 >1 p=1 within_noise
o4_nocp/o4r2                 0.9895x [0.9727, 1.0143] 4/11 >1 p=0.549 within_noise
o4_nospec/o4r2               0.9876x [0.9563, 1.0241] 3/11 >1 p=0.227 within_noise
o4_pwhopr/o4r2               0.9821x [0.9364, 1.0130] 4/11 >1 p=0.549 within_noise
o4r2a/o4r2                   0.9831x [0.9459, 0.9979] 2/11 >1 p=0.0654 within_noise
== H2
-- packet
A/A o4r2/o4r2-aa             1.0054x [0.9930, 1.0231] 22/41 >1 p=0.755 not_significant band [0.9774, 1.0231]
w7bd_b3/o4r2                 1.0148x [0.9920, 1.0310] 25/41 >1 p=0.211 within_noise
o4_hostall/o4r2              1.0416x [1.0026, 1.0613] 27/41 >1 p=0.0596 within_noise
o4_pwhopr/o4r2               0.9869x [0.9731, 1.0088] 17/41 >1 p=0.349 within_noise
w7bd_b3/o4_pwhopr            1.0178x [0.9982, 1.0330] 25/41 >1 p=0.211 within_noise
o4_hostall/w7bd_b3           1.0312x [1.0143, 1.0419] 29/41 >1 p=0.0115 within_noise
-- uart
A/A o4r2/o4r2-aa             1.0091x [1.0001, 1.0186] 50/81 >1 p=0.0448 above_one band [0.9817, 1.0186]
w7bd_b3/o4r2                 1.1016x [1.0676, 1.1291] 77/81 >1 p=1.45e-18 above_floor
o4_hostall/o4r2              1.1124x [1.0951, 1.1406] 78/81 >1 p=7.33e-20 above_floor
o4_pwhopr/o4r2               0.9895x [0.9837, 0.9987] 31/81 >1 p=0.0448 within_noise
w7bd_b3/o4_pwhopr            1.1088x [1.0745, 1.1306] 80/81 >1 p=6.78e-23 above_floor
o4_hostall/w7bd_b3           1.0131x [0.9994, 1.0260] 49/81 >1 p=0.0748 within_noise
-- cli
A/A o4r2/o4r2-aa             1.0069x [0.9834, 1.0208] 24/41 >1 p=0.349 not_significant band [0.9796, 1.0208]
w7bd_b3/o4r2                 0.9967x [0.9789, 1.0091] 19/41 >1 p=0.755 within_noise
o4_hostall/o4r2              1.0068x [0.9934, 1.0209] 25/41 >1 p=0.211 within_noise
o4_pwhopr/o4r2               0.9890x [0.9730, 0.9997] 14/41 >1 p=0.0596 within_noise
w7bd_b3/o4_pwhopr            1.0112x [0.9958, 1.0237] 24/41 >1 p=0.349 within_noise
o4_hostall/w7bd_b3           1.0102x [1.0027, 1.0250] 28/41 >1 p=0.0275 within_noise
```
