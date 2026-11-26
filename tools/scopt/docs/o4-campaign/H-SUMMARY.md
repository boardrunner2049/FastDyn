# Campaign H (heavy and bimodal arms): result

Run 2026-11-26 02:18-02:41 (attempt 1: 02:18:13-02:25:06, failed on uart and not used; attempt 2, the one decided: 02:27:46-02:41:05) EST. O4-design.md §3.6, decided by the registered decide.py (`0b7247476a39abade599000de4e2555c7edb1167b57d0989f4bac54cbaf1a438`, registered: true).

- decide.py output: `/data/scfix-o4/H.json` (sha256 `87c6a38f428a02c75703664d8c770792703bd33c1025fa5da7d02734cedb062f`); its stdout is `/data/scfix-o4/H.stdout.txt`. Command: `decide.py packet/timing-H uart/timing-H cli/timing-H --gates gates/all.json --out H.json` (cwd /data/scfix-o4).
- Plans: `/data/scfix-o4/plans/H/{packet,uart,cli}.json` (make_plan.py H), pre-flight `/data/scfix-o4/plans/H/validation.json` (validate_plans.py: ok, 0 problems).
- Driver output: `/data/scfix-o4/{packet,uart,cli}/timing-H/` and logs `/data/scfix-o4/{packet,uart,cli}/timing-H.log`.
- Hygiene records: `/data/scfix-o4/hygiene/H/{before,after}.json`, `launch.log`, `start.marker`; diagnostics `/data/scfix-o4/hygiene/H/diag.json`.

## Outcome

- **Attempt 1 failed on uart and was not used.** At block 35 of 81 the o4_pwhopr `help` run exited 1 before it ran any firmware: `qemu-system-arm: -accel tcg,supercache=/data/scfix-o4/uart/timing-H/o4_pwhopr/cache.sc: allocate supercache code buffer: File exists`. The driver then raised `RuntimeError: /data/scfix-o4/uart/timing-H/o4_pwhopr/help/block-35.log: exit 1, received []`. packet and cli had completed. The cause is in QEMU's `map_cache()` (tcg/supercache.c): a reused cache maps its 2 GiB code buffer at the header's `code_address` with `MAP_FIXED_NOREPLACE`, and mmap returned EEXIST, so another mapping already sat inside [0x7fff6c000000, 0x7fffec000000). Every H cache on every workload has that address and size, so the failure is not specific to o4_pwhopr. The whole campaign was rerun (attempt 2) with the same plans and seed, all three workloads concurrently. I set that rule before computing any H estimate. Attempt 1 is kept as `<w>/timing-H.attempt1` and `<w>/timing-H.attempt1.log`; the failure record is `hygiene/H/attempt1/failure.json`.
- **VALID holds on all three workloads** (attempt 2); no VALID rerun is needed (`rerun_required` is empty).
- **BEST fails: A5 o4_pwhopr is faster than O4.** Of the 12 hypotheses in the Holm family at alpha 0.05, two are rejected. o4_pwhopr cli: d +1.97%, SE 0.49%, 95% LB +1.14%, p 0.000139 <= 0.004167 (rank 1). o4_pwhopr pooled: d +0.93%, SE 0.30%, 95% [+0.44%, +1.42%], p 0.00112 <= 0.00455 (rank 2). The step-down stops at rank 3, o4_pwhopr uart: d +0.47%, p 0.141 > 0.0050. On packet o4_pwhopr is -0.11% (not shown). decide.py lists o4_pwhopr in `best.H.faster_than_o4` and in `composition.absorb_faster_configurations`.
  - **What follows (§3.6, §3.7 BEST, §3.8):** O4 must absorb A5, which becomes the base candidate for O4-r2, or the reason it cannot be composed must be documented. `test_no_recorded_arm_beats_o4` should fail until then; this step did not touch fd-int. Under decide.py's group rule, X3 (o4_noregions) can no longer apply even if A2 accepts it ("removable, superseded"). A5 replaces hot-loop selection and all of its knobs (§1.8 groups), so how it combines with A2's A1, A7, A8 and I1 is the lead's call.
  - **Context (not timing):** cli/VERIFY.md says o4_pwhopr's 14 cli roots are o4_min2's 13 plus 0x2daa (20 members). A7 o4_min2 is in A2; its screen result on cli was +1.10% (not shown). The driver's medians point the same way: cli o4/o4_pwhopr is 1.0170x [1.0006, 1.0307]. The per-case medians are help 26.7 ms against 27.9 ms for o4, and commands 30.2 ms against 30.4 ms.
  - **Replication in attempt 1 (not a decision input):** registered decide.py on the two complete workloads (`hygiene/H/attempt1/decide-attempt1-packet-cli.json`) gives o4_pwhopr cli +1.84% (SE 0.52%, p 0.00053), packet +0.41% (SE 0.60%), and packet+cli pooled +1.22%. On uart, blocks 0-34 of the failed run, read with `--unregistered` (`hygiene/H/attempt1/decide-attempt1-uart-blocks0-34.unregistered.json`), give +1.03% (SE 0.59%).
- **A6 o4_hostall (add-on, ADD family):** pooled d -4.51%, SE 0.34%, 95% [-5.08%, -3.94%], one-sided pooled p ("faster than O4") 1.0000. LB95_w: packet -5.14%, uart -13.58%, cli -1.18%, against log 0.98 = -2.02%. Non-inferiority (ADD condition 3) fails on packet and uart, so A6 is excluded whatever the combined A2 + H Holm family turns out to be. In this H-only decide.json it shows as `excluded` with reason `holm`, because the family has size 1 here. It is measured slower: d is -4.05% on packet, -12.45% on uart and -0.32% on cli.
- **w7bd_b3 (challenger):** not faster than O4. Pooled d -3.57%, 95% [-4.18%, -2.95%]. It is slower_than_o4 on packet (-3.14%), uart (-10.27%) and pooled; on cli it is +0.50% (p 0.190, not shown).
- The H-only decide.json's composition reads "nothing accepted (O4 stays r1)". That only means H accepts no add-on. O4-r2 is composed after A2, from A2 and H together, and must also handle the A5 absorption above.

## Run

| | packet | uart | cli |
|---|---|---|---|
| CPU | 10 | 20 | 12 |
| blocks (burn-in block 0 + timed) | 41 (1 + 40) | 81 (1 + 80) | 41 (1 + 40) |
| timed arms (incl. o4, o4-aa) | 5 | 5 | 5 |
| runs | 205 | 810 | 410 |
| settle | 30 s (argument) | 30 s (argument) | 30 s (argument) |
| 1-min load at start / before block 0 | 1.30 / 3.81 | 1.30 / 3.55 | 1.30 / 3.59 |
| block 0 / last block ended | 02:29:49 / 02:34:17 | 02:29:35 / 02:41:05 | 02:29:09 / 02:31:02 |

- Attempt 2 was launched at 02:27:46, all three workloads concurrently. Each driver ran under `taskset -c <cpu>` with its own affinity, `--settle-seconds 30` and seed 20261128. The load at launch was 1.30 (rule: <= 2.0); attempt 1 had launched at 02:18:13 at load 0.52. The load before block 0 (3.55-3.81) is the three drivers' own staging and warm replays.
- No driver failed in attempt 2. All 16-word checks, warm-cache identities, private-region identities and v7 counter checks passed (the driver raises on any of them). The QEMU identity check ran at staging.
- No timed block on any workload has a run above 1.3x its arm/case median. cli's burn-in block 0 had 6 of 10 runs above 1.3x (max 1.862x) and an A/A ratio of 1.1561; decide.py drops block 0 as pre-registered. As in A, cli's timed blocks (02:29:09-02:31:02) overlapped the tail of the packet driver's staging and settle and the first packet and uart blocks, because cli's staging is short.
- **Hygiene.** QEMU sha256 `951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60`, inode 87162887, dev 2050, size 137799456, mtime_ns 1795640690884852222: identical before and after (`qemu_unchanged: true`). `find -newer start.marker` over /data/scfix-r2, /data/scfix-campaign, /data/trebuchet-clean, /data/qemu-scfix-bin found nothing. fd-int: branch o4, HEAD ece9b5e, `git status` clean before and after. No .pyc, __pycache__ or .pytest_cache in fd-int or the driver directory. No build ran and no other campaign ran. No grep or find touched a cache while timing ran. During both attempts this step only polled the driver processes (kill -0 every 10 s), read small source and JSON files, and wrote scripts in its scratch directory. Between the attempts, with no driver running, it read the failed run's log, the QEMU source (/data/fastdyn-work/qemu-fu, read only) and the 80-byte headers of the H caches. Attempt 1 has its own before/after records in hygiene/H/attempt1/ (QEMU unchanged, find -newer empty, fd-int clean).

## VALID (o4 / o4-aa, burn-in dropped, HL with two-sided 99% bootstrap interval)

| workload | attempt | blocks | a_w | 99% interval | SE | verdict |
|---|---:|---:|---:|---|---:|---|
| packet | 1 | 40 | -0.48% | [-2.01%, +1.37%] | 0.62% | valid |
| uart | 1 | 80 | +0.35% | [-1.11%, +1.86%] | 0.53% | valid |
| cli | 1 | 40 | -0.40% | [-2.10%, +1.24%] | 0.64% | valid |

Notes, reported for completeness; none changes the pre-registered decision:

- The table's attempt column is decide.py's VALID attempt (the plan's x_attempt). The crash rerun reused the same plans, so it is VALID attempt 1, and no VALID rerun at seed + 100 was needed.
- The driver's own A/A, over all blocks including burn-in: packet 1.0001x [0.9771, 1.0168], uart 1.0109x [0.9848, 1.0225], cli 1.0003x [0.9850, 1.0139]. harness_bias is false on all three.
- SD of the A/A block log-ratios over the timed blocks: packet 4.05%, uart 4.82%, cli 3.93%. For comparison, A had 4.68 / 6.22 / 2.78% and round 2's c7b3_not2t/c7b3 had 3.66 / 7.00 / 3.12%.
- packet w7bd_b3 is the one estimate that moved between attempts: -3.14% (SE 0.69%) in attempt 2 against -0.96% (SE 0.85%) in attempt 1, about 2 SE apart. Only attempt 2 decides. o4_pwhopr is consistent between the two launches on every workload: each difference is under 1 SE (see Outcome).

## H verdicts

| item | arm | role | timed on | same bytes as o4 | LB95_w > log 0.98 (-2.02%) | pooled d | pooled 95% | pooled one-sided p | H verdict |
|---|---|---|---|---|---|---:|---|---:|---|
| A6 | o4_hostall | addon | packet/uart/cli | - | packet NO, uart NO, cli yes | -4.51% | [-5.08%, -3.94%] | 1.0000 | excluded (ADD): LB_w < log 0.98 on packet and uart; measured slower (pooled UB95 < 0) |
| A5 | o4_pwhopr | challenger | packet/uart/cli | - | packet yes, uart yes, cli yes | +0.93% | [+0.44%, +1.42%] | 0.0011 | **faster than O4** (BEST rejected on cli and pooled): O4 must absorb A5 (§3.6, §3.8) |
| w7bd_b3 | w7bd_b3 | challenger | packet/uart/cli | - | packet NO, uart NO, cli yes | -3.57% | [-4.18%, -2.95%] | 1.0000 | not faster than O4; slower on packet, uart and pooled |

## Estimates (decide.py, §3.3)

d is log(sqrt(T(o4) T(o4-aa)) / T(X)) by Hodges-Lehmann; **positive means X is faster than O4**. SE is the block-bootstrap SD; LB95/UB95 and UB99 use Student's t at df = n_w - 1 (pooled: Welch df). p is one-sided for "X faster than O4". BEST Holm is the campaign's 12-hypothesis family at alpha 0.05.

| arm | item | scope | d | SE | df | LB95 | UB95 | UB99 | p | BEST Holm rank / threshold | BEST verdict |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| o4_hostall | A6 | packet | -4.05% | 0.65% | 39.0 | -5.14% | -2.96% | -2.49% | 1.0000 | 8 / 0.010000 | slower_than_o4 |
| o4_hostall | A6 | uart | -12.45% | 0.68% | 79.0 | -13.58% | -11.32% | -10.83% | 1.0000 | 10 / 0.016667 | slower_than_o4 |
| o4_hostall | A6 | cli | -0.32% | 0.51% | 39.0 | -1.18% | +0.53% | +0.91% | 0.7355 | 6 / 0.007143 | not_shown |
| o4_hostall | A6 | pooled | -4.51% | 0.34% | 120.7 | -5.08% | -3.94% | - | 1.0000 | 9 / 0.012500 | slower_than_o4 |
| o4_pwhopr | A5 | packet | -0.11% | 0.75% | 39.0 | -1.36% | +1.15% | +1.70% | 0.5564 | 5 / 0.006250 | not_shown |
| o4_pwhopr | A5 | uart | +0.47% | 0.44% | 79.0 | -0.25% | +1.20% | +1.51% | 0.1406 | 3 / 0.005000 | not_shown |
| o4_pwhopr | A5 | cli | +1.97% | 0.49% | 39.0 | +1.14% | +2.80% | +3.16% | 0.0001 | 1 / 0.004167 | faster_than_o4 |
| o4_pwhopr | A5 | pooled | +0.93% | 0.30% | 144.0 | +0.44% | +1.42% | - | 0.0011 | 2 / 0.004545 | faster_than_o4 |
| w7bd_b3 | w7bd_b3 | packet | -3.14% | 0.69% | 39.0 | -4.31% | -1.97% | -1.46% | 1.0000 | 7 / 0.008333 | slower_than_o4 |
| w7bd_b3 | w7bd_b3 | uart | -10.27% | 0.70% | 79.0 | -11.43% | -9.10% | -8.60% | 1.0000 | 12 / 0.050000 | slower_than_o4 |
| w7bd_b3 | w7bd_b3 | cli | +0.50% | 0.56% | 39.0 | -0.45% | +1.45% | +1.87% | 0.1897 | 4 / 0.005556 | not_shown |
| w7bd_b3 | w7bd_b3 | pooled | -3.57% | 0.37% | 126.6 | -4.18% | -2.95% | - | 1.0000 | 11 / 0.025000 | slower_than_o4 |

## Driver's own statistics (for reference; they decide nothing)

Median of the per-block ratio reference/arm of block-summed time over **all** blocks including burn-in block 0, with a 95% percentile bootstrap interval. **Above 1 means the second-named arm is faster.** The reference is o4 alone, not the pooled o4/o4-aa.

| comparison | packet | uart | cli |
|---|---|---|---|
| A/A o4/o4-aa | 1.0001 [0.9771, 1.0168], harness_bias false | 1.0109 [0.9848, 1.0225], harness_bias false | 1.0003 [0.9850, 1.0139], harness_bias false |
| o4/w7bd_b3 | 0.9642 [0.9491, 0.9885] within_noise | 0.9105 [0.8847, 0.9287] below_floor | 1.0049 [0.9890, 1.0166] within_noise |
| o4/o4_hostall | 0.9642 [0.9473, 0.9756] below_floor | 0.8826 [0.8694, 0.9092] below_floor | 0.9993 [0.9863, 1.0058] within_noise |
| o4/o4_pwhopr | 1.0028 [0.9684, 1.0209] within_noise | 1.0033 [0.9939, 1.0217] within_noise | 1.0170 [1.0006, 1.0307] within_noise |
| w7bd_b3/o4_pwhopr | 1.0299 [1.0114, 1.0542] within_noise | 1.1041 [1.0945, 1.1244] above_floor | 1.0047 [0.9977, 1.0273] within_noise |
| o4_hostall/w7bd_b3 | 1.0088 [0.9895, 1.0299] within_noise | 1.0149 [0.9970, 1.0318] within_noise | 1.0086 [0.9938, 1.0313] within_noise |

## Next

- A2 runs next (combined mode, seed 20261130) from `screen.json`: `make_plan.py A2 --screen /data/scfix-o4/screen.json`. The ADD Holm family is A2's add-ons plus A6. A6's pooled p of 1.0000 ranks it last, and A6 is excluded in any case by LB_w on packet and uart.
- BEST fails in H. Under §3.7 and §3.8, O4-r2 must absorb A5 (`o4-pwhopr.toml`: firmware-inline mode exhaustive, cost_model profile, phase steady, planner whopr) or document why it cannot. This interacts with A2's hot-loop knobs (A1, A7, A8, I1) and makes X3 moot. The lead decides the composition, then B1 and B2.
- **Runtime finding, outside this step.** The fixed-address code-buffer collision (QEMU `map_cache`, EEXIST at 0x7fff6c000000) is a latent launch failure for every sealed cache, O4 included. It hit 1 of about 3,400 timed launches in campaigns A and H, plus their staging runs. A run that hits it fails loudly with exit 1; it never produces wrong output.
