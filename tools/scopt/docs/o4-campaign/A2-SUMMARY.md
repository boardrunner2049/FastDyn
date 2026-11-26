# Campaign A2 (head-to-heads, combined mode): result

Run 2026-11-26 03:17-03:41 (launch 03:17:17; last run ended 03:40:41) EST. O4-design.md §3.5 as amended by registration 2 (PREREG.md change 3: one combined plan per workload), decided by the registered decide.py (`0b7247476a39abade599000de4e2555c7edb1167b57d0989f4bac54cbaf1a438`, registered: true) over A2 and H together (§3.7: the ADD family is the add-ons decided in A2 and H).

- decide.py output: `/data/scfix-o4/A2.json` (sha256 `cf630e285e7b0c21790b13298bdba48fb8d6ad15d085e6cac3218ff62173e24b`); its stdout is `/data/scfix-o4/A2.stdout.txt`. Command: `decide.py packet/timing-A2 uart/timing-A2 cli/timing-A2 packet/timing-H uart/timing-H cli/timing-H --gates gates/all.json --screen screen.json --out A2.json` (cwd /data/scfix-o4).
- Plans: `/data/scfix-o4/plans/A2/{packet,uart,cli}.json` (make_plan.py A2 --candidates, see Run), pre-flight `/data/scfix-o4/plans/A2/validation.json` (validate_plans.py: ok, 0 problems).
- Driver output: `/data/scfix-o4/{packet,uart,cli}/timing-A2/` and logs `/data/scfix-o4/{packet,uart,cli}/timing-A2.log`.
- Hygiene records: `/data/scfix-o4/hygiene/A2/{before,after}.json`, `launch.log`, `start.marker`; diagnostics `/data/scfix-o4/hygiene/A2/diag.json`.

## Outcome

- **VALID holds on all three A2 workloads**; no rerun at seed + 100 was needed (`rerun_required` is empty). uart is the closest: a_w -1.06%, 99% [-2.40%, +0.17%]. H's three workloads (attempt 2 of H, the one H decided) are valid as recorded in H-SUMMARY.md.
- **BEST holds in A2**: family 31 (every A2 arm and scope), no rejection. The smallest p is fl3b3 packet, 0.00216 against a Holm threshold of 0.001613; then o4_chained packet 0.00775 / 0.001667 and o4_rank packet 0.00883 / 0.001724. **BEST still fails in H** (unchanged, not re-timed here): A5 o4_pwhopr is faster than O4 on cli and pooled, so `composition.absorb_faster_configurations` is `[o4_pwhopr]`.
- **ADD (family 6: A1, A3, A7, A8, A2a from A2 and A6 from H): no add-on is accepted.** The first Holm step fails, so the whole family is retained. A3 o4_rank ranks first with pooled (packet-only) d +1.51%, SE 0.61%, 95% [+0.48%, +2.54%], one-sided p **0.00883 against the threshold 0.05/6 = 0.00833**: it misses by 0.0005. A2a o4_tasks is second (d +0.98%, p 0.1016 > 0.0100), A8 o4_legacy third (+0.37%, p 0.2604), A7 o4_min2 fourth (+0.10%, p 0.3826: the screen's +1.31% did not replicate), A1 o4_chained fifth (-0.09%, p 0.5899) and A6 o4_hostall last (-4.51%, p 1.0000; it also fails non-inferiority on packet and uart).
- **A1 o4_chained is workload-dependent:** packet +1.82% (95% [+0.61%, +3.03%]) but uart -0.99% (95% [-1.81%, -0.17%], slower_than_o4 unadjusted). Pooled it is -0.09%.
- **REMOVE:** X1 o4_nohi **accepted** (pooled +0.24%, 95% [-0.30%, +0.77%]; LB_w packet -0.42%, uart -0.72%, cli -0.97%, all above -2.02%). X2 o4_nocp **accepted** (+0.58%, 95% [+0.02%, +1.14%]; it also passes ADD unadjusted, LB > 0) but moot, because X4 is accepted. X4 o4_nospec **accepted** (+0.32%, 95% [-0.24%, +0.88%]). **X3 o4_noregions rejected:** the regions stay, because cli is slower without them: d -1.65%, LB95_cli -2.50% < log 0.98 = -2.02%, UB95_cli -0.80%. The screen showed the same direction on cli (-2.46%).
- **FIDELITY: I1 o4_isb is included** (pooled +0.19%, 95% [-0.36%, +0.75%]; LB_w packet -0.65%, uart -0.54%; cli has o4's bytes). It is not moot, because X3 was rejected.
- **Challenger C1 fl3b3** is not faster than O4: pooled +0.20%, 95% [-0.28%, +0.68%]. Its packet scope is +2.03% (95% [+0.90%, +3.16%], p 0.00216), which misses BEST's first Holm step (0.001613); uart -0.12%, cli -0.32%.
- **Composition by the registered rule (decide.py §3.7 groups, §3.8):** O4-r2 = O4-r1 + **I1 + X1 + X4**: firmware-inline `isb_boundary = true`; host-inline removed (no INLINE_CALL, no `--pgo-launch`, no gdb helper-PGO replay); the SPEC bundle removed (specialize-returns, constant-propagation, branch-folding, dead-host-ops). It contains no add-on, so B1 uses the REMOVE rule. X2 is moot under X4. The host-inline group has X1 as its only accepted member (A4 was excluded in the screen, A6 here). B2's 'rejected candidate with the highest pooled d' is o4_rank. decide.py also lists o4_pwhopr (H) for absorption, which this composition does not do. That is for the lead to settle under §3.8 before B1/B2.
- **P4b, for the record:** the screen did not trigger it (C1p d <= 0, and A7 had not yet been accepted). A7 is not accepted here either. o4_tasks was timed anyway, as instructed, and was excluded by Holm. Its packet runs include the one slow outlier of this campaign (block 18, 1.57x its median). The HL estimate damps one outlier, but its block log-ratio SD is 8.34%, against 3.4-5.7% for the other packet arms.

## Run

| | packet | uart | cli |
|---|---|---|---|
| CPU | 10 | 20 | 12 |
| blocks (burn-in block 0 + timed) | 41 (1 + 40) | 81 (1 + 80) | 41 (1 + 40) |
| timed arms (incl. o4, o4-aa) | 11 | 10 | 8 |
| candidates timed | o4_nohi, o4_noregions, o4_chained, o4_isb, o4_rank, o4_min2, o4_legacy, fl3b3, o4_tasks | o4_nohi, o4_nocp, o4_noregions, o4_nospec, o4_chained, o4_isb, o4_min2, fl3b3 | o4_nohi, o4_nocp, o4_noregions, o4_nospec, o4_min2, fl3b3 |
| candidates with o4's bytes (not timed) | o4_nocp, o4_nospec | o4_rank, o4_legacy, o4_tasks | o4_chained, o4_isb, o4_rank, o4_legacy, o4_tasks |
| runs | 451 | 1620 | 656 |
| settle | 30 s (argument) | 30 s (argument) | 30 s (argument) |
| 1-min load at start / before block 0 | 0.80 / 2.59 | 0.80 / 2.56 | 0.80 / 2.53 |
| block 0 / last block ended | 03:20:12 / 03:28:46 | 03:19:56 / 03:40:41 | 03:18:41 / 03:20:04 |

- **Plans.** `make_plan.py A2 --candidates o4_nohi,o4_nocp,o4_noregions,o4_nospec,o4_chained,o4_isb,o4_rank,o4_min2,o4_legacy,fl3b3,o4_tasks`, which is the screen's `a2_order` (k 0-9, unchanged) with P4b's o4_tasks appended as k 10. `--screen` cannot add an arm that the screen did not send to A2. In combined mode, k sets no seed: every candidate uses seed 20261130, and decide.py's bootstrap seed is `20261130:<w>:<arm>`. So the position changes nothing but the shuffle's input order. Every candidate's cache.sc sha256 equals the one campaign A timed (o4: packet 7702b19d…, uart ae360ff7…, cli 869f9295…). o4_tasks packet is 5a8a9075…, as P4b recorded; on uart and cli it has o4's bytes.
- **Wall time.** At round 2's throughput (1.9 / 1.6 / 0.56 s per run) the plans projected 14.3 / 43.2 / 6.1 minutes (451 / 1,620 / 656 runs). None exceeds 90 minutes. Actual time from launch to the last run: 11.5 / 23.4 / 2.8 minutes.
- Launched 03:17:17, all three concurrently. Each driver ran under `taskset -c <cpu>` with its own affinity, `--settle-seconds 30` and seed 20261130. The 1-minute load at launch was 0.80 (rule: <= 2.0). The load before block 0 (2.5-2.6) is the three drivers' own staging and warm replays.
- No driver failed. All 16-word checks, warm-cache identities, private-region identities and v7 counter checks passed (the driver raises on any of them). The QEMU identity check ran at staging. H's code-buffer collision (`allocate supercache code buffer: File exists`) did not recur.
- **Outliers (decide nothing).** packet: 2 timed runs above 1.3x their arm/case median, o4_tasks in block 18 (1.573x) and o4_rank in block 36 (1.387x). uart: none. cli: none in timed blocks; burn-in block 0 had 8 of 16 runs above 1.3x (max 1.724x), and its A/A ratio is 1.5487. decide.py drops block 0 as pre-registered. As in A and H, cli's timed blocks (03:18:41-03:20:04) overlapped packet's and uart's staging and settle (their first block-0 runs ended 03:19:59 / 03:19:41), because cli's staging is short.
- During timing, this step only waited on the driver PIDs (`tail --pid`), ran `wc -l` on observations.jsonl three times, and wrote one script in its scratch directory. No grep or find touched a cache, no build ran, and no other campaign ran. make_plan.py and validate_plans.py (cache sha256s, `nice -n 10 taskset -c 21-31`) ran before the launch.
- **Hygiene.** QEMU sha256 `951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60`, inode 87162887, dev 2050, size 137799456, mtime_ns 1795640690884852222: identical before and after (`qemu_unchanged: true`). `find -newer start.marker` over /data/scfix-r2, /data/scfix-campaign, /data/trebuchet-clean, /data/qemu-scfix-bin found nothing. fd-int: branch o4, HEAD b35cdb6, `git status` clean before and after (unchanged: true). No .pyc, __pycache__ or .pytest_cache in fd-int or the driver directory.

## VALID (o4 / o4-aa, burn-in dropped, HL with two-sided 99% bootstrap interval)

| campaign | workload | attempt | blocks | a_w | 99% interval | SE | verdict |
|---|---|---:|---:|---:|---|---:|---|
| A2 | packet | 1 | 40 | +0.13% | [-1.59%, +1.88%] | 0.66% | valid |
| A2 | uart | 1 | 80 | -1.06% | [-2.40%, +0.17%] | 0.48% | valid |
| A2 | cli | 1 | 40 | +0.57% | [-0.93%, +1.90%] | 0.59% | valid |
| H | packet | 1 | 40 | -0.48% | [-2.01%, +1.37%] | 0.62% | valid |
| H | uart | 1 | 80 | +0.35% | [-1.11%, +1.86%] | 0.53% | valid |
| H | cli | 1 | 40 | -0.40% | [-2.10%, +1.24%] | 0.64% | valid |

Notes, reported for completeness; none changes the pre-registered decision:

- **The uart driver's own A/A flags harness_bias again**, as it did in A: 0.9911x [0.9803, 0.9994], median of the o4/o4-aa block ratios over all 81 blocks with a 95% interval. o4 ran slightly faster than its copy. The pre-registered VALID (HL, burn-in dropped, 99%) gives -1.06% [-2.40%, +0.17%], which includes 0, so it is not biased. Every estimate is taken against the pooled o4/o4-aa reference.
- SD of the A/A block log-ratios over the timed blocks: packet 4.38%, uart 4.79%, cli 3.21% (H: 4.05 / 4.82 / 3.93%).

## Decisions (§3.7)

ADD: Holm over the 6 add-ons decided in A2 and H (A3 o4_rank, A2a o4_tasks, A8 o4_legacy, A7 o4_min2, A1 o4_chained, A6 o4_hostall, in Holm order) on the one-sided pooled p, alpha 0.05, plus LB95_w > log 0.98 (-2.02%) on every timed workload. REMOVE / FIDELITY: UB95 >= 0, LB95 > log 0.99 (-1.01%) and LB95_w > log 0.98 on every timed workload. G0 from `gates/all.json` (all true).

| item | arm | rule | timed on | same bytes as o4 | pooled d | SE | pooled 95% [LB, UB] | p | LB95_w > log 0.98 | rule checks | ADD Holm rank / adjusted threshold | verdict |
|---|---|---|---|---|---:|---:|---|---:|---|---|---|---|
| X1 | o4_nohi | REMOVE | packet/uart/cli | - | +0.24% | 0.32% | [-0.30%, +0.77%] | 0.2351 | packet yes, uart yes, cli yes | lb_gt_log099 pass, noninferior_every_w pass, ub_ge_0 pass | - | **accepted**: component removed (REMOVE) |
| X2 | o4_nocp | REMOVE | uart/cli | packet | +0.58% | 0.34% | [+0.02%, +1.14%] | 0.0444 | uart yes, cli yes | lb_gt_log099 pass, noninferior_every_w pass, ub_ge_0 pass | - | **accepted** (REMOVE), not in O4-r2; moot: X4 removes the whole SPEC bundle |
| X3 | o4_noregions | REMOVE | packet/uart/cli | - | -0.18% | 0.28% | [-0.64%, +0.29%] | 0.7322 | packet yes, uart yes, cli NO | lb_gt_log099 pass, noninferior_every_w FAIL, ub_ge_0 pass | - | rejected: component stays (noninferior_every_w) |
| X4 | o4_nospec | REMOVE | uart/cli | packet | +0.32% | 0.34% | [-0.24%, +0.88%] | 0.1692 | uart yes, cli yes | lb_gt_log099 pass, noninferior_every_w pass, ub_ge_0 pass | - | **accepted**: component removed (REMOVE) |
| A1 | o4_chained | ADD | packet/uart | cli | -0.09% | 0.41% | [-0.77%, +0.58%] | 0.5899 | packet yes, uart yes | holm FAIL, noninferior_every_w pass | 5 / 0.025000 | excluded (holm) |
| I1 | o4_isb | FIDELITY | packet/uart | cli | +0.19% | 0.33% | [-0.36%, +0.75%] | 0.2802 | packet yes, uart yes | lb_gt_log099 pass, noninferior_every_w pass, ub_ge_0 pass | - | **included** (FIDELITY) |
| A3 | o4_rank | ADD | packet | uart/cli | +1.51% | 0.61% | [+0.48%, +2.54%] | 0.0088 | packet yes | holm FAIL, noninferior_every_w pass | 1 / 0.008333 | excluded (holm) |
| A7 | o4_min2 | ADD | packet/uart/cli | - | +0.10% | 0.32% | [-0.44%, +0.63%] | 0.3826 | packet yes, uart yes, cli yes | holm FAIL, noninferior_every_w pass | 4 / 0.016667 | excluded (holm) |
| A8 | o4_legacy | ADD | packet | uart/cli | +0.37% | 0.58% | [-0.60%, +1.35%] | 0.2604 | packet yes | holm FAIL, noninferior_every_w pass | 3 / 0.012500 | excluded (holm) |
| A2a | o4_tasks | ADD | packet | uart/cli | +0.98% | 0.76% | [-0.30%, +2.25%] | 0.1016 | packet yes | holm FAIL, noninferior_every_w pass | 2 / 0.010000 | excluded (holm) |
| A6 | o4_hostall (H) | ADD | packet/uart/cli | - | -4.51% | 0.34% | [-5.08%, -3.94%] | 1.0000 | packet NO, uart NO, cli yes | holm FAIL, noninferior_every_w FAIL | 6 / 0.050000 | excluded (holm) |
| C1 | fl3b3 | BEST (challenger) | packet/uart/cli | - | +0.20% | 0.29% | [-0.28%, +0.68%] | 0.2482 | - | - | - | not_shown (BEST Holm rank 15 / 0.002941) |

Carried from the screen (not in the ADD family): A4 o4_hotgraph: excluded (excluded_measured_slower).

## Estimates (decide.py, §3.3)

d is log(sqrt(T(o4) T(o4-aa)) / T(X)) by Hodges-Lehmann; **positive means X is faster than O4**. SE is the block-bootstrap SD; LB95/UB95 and UB99 use Student's t at df = n_w - 1 (pooled: Welch df). p is one-sided for "X faster than O4". BEST Holm is A2's 31-hypothesis family at alpha 0.05 (H's own 12-hypothesis family is in H-SUMMARY.md). ADD Holm is the add-on family of A2 + H on the pooled p (family 6); its rank and adjusted threshold are on the pooled row. The verdict column is the §3.7 item verdict.

| arm | item | scope | d | SE | df | LB95 | UB95 | UB99 | p | BEST Holm rank / threshold | BEST verdict | ADD Holm rank / adjusted threshold | verdict |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|---|
| o4_nohi | X1 | packet | +0.62% | 0.62% | 39.0 | -0.42% | +1.67% | +2.13% | 0.1608 | 12 / 0.002500 | not_shown |  | LB95_w > log 0.98: yes |
| o4_nohi | X1 | uart | +0.03% | 0.45% | 79.0 | -0.72% | +0.78% | +1.10% | 0.4727 | 23 / 0.005556 | not_shown |  | LB95_w > log 0.98: yes |
| o4_nohi | X1 | cli | +0.23% | 0.72% | 39.0 | -0.97% | +1.44% | +1.97% | 0.3726 | 20 / 0.004167 | not_shown |  | LB95_w > log 0.98: yes |
| o4_nohi | X1 | pooled | +0.24% | 0.32% | 155.4 | -0.30% | +0.77% | - | 0.2351 | 14 / 0.002778 | not_shown | - | **accepted**: component removed (REMOVE) |
| o4_nocp | X2 | packet | same bytes as o4, not timed | | | | | | | | same_bytes | | |
| o4_nocp | X2 | uart | +0.51% | 0.44% | 79.0 | -0.23% | +1.25% | +1.56% | 0.1263 | 9 / 0.002174 | not_shown |  | LB95_w > log 0.98: yes |
| o4_nocp | X2 | cli | +0.67% | 0.52% | 39.0 | -0.20% | +1.54% | +1.92% | 0.1018 | 8 / 0.002083 | not_shown |  | LB95_w > log 0.98: yes |
| o4_nocp | X2 | pooled | +0.58% | 0.34% | 113.6 | +0.02% | +1.14% | - | 0.0444 | 4 / 0.001786 | not_shown | - | **accepted** (REMOVE), not in O4-r2; moot: X4 removes the whole SPEC bundle |
| o4_noregions | X3 | packet | +0.67% | 0.64% | 39.0 | -0.41% | +1.75% | +2.22% | 0.1504 | 11 / 0.002381 | not_shown |  | LB95_w > log 0.98: yes |
| o4_noregions | X3 | uart | +0.43% | 0.40% | 79.0 | -0.24% | +1.10% | +1.39% | 0.1462 | 10 / 0.002273 | not_shown |  | LB95_w > log 0.98: yes |
| o4_noregions | X3 | cli | -1.65% | 0.51% | 39.0 | -2.50% | -0.80% | -0.42% | 0.9989 | 31 / 0.050000 | slower_than_o4 |  | LB95_w > log 0.98: NO |
| o4_noregions | X3 | pooled | -0.18% | 0.28% | 152.6 | -0.64% | +0.29% | - | 0.7322 | 27 / 0.010000 | not_shown | - | rejected: component stays (noninferior_every_w) |
| o4_nospec | X4 | packet | same bytes as o4, not timed | | | | | | | | same_bytes | | |
| o4_nospec | X4 | uart | -0.01% | 0.44% | 79.0 | -0.74% | +0.72% | +1.04% | 0.5067 | 24 / 0.006250 | not_shown |  | LB95_w > log 0.98: yes |
| o4_nospec | X4 | cli | +0.80% | 0.53% | 39.0 | -0.09% | +1.69% | +2.08% | 0.0679 | 5 / 0.001852 | not_shown |  | LB95_w > log 0.98: yes |
| o4_nospec | X4 | pooled | +0.32% | 0.34% | 114.8 | -0.24% | +0.88% | - | 0.1692 | 13 / 0.002632 | not_shown | - | **accepted**: component removed (REMOVE) |
| o4_chained | A1 | packet | +1.82% | 0.72% | 39.0 | +0.61% | +3.03% | +3.56% | 0.0077 | 2 / 0.001667 | not_shown |  | LB95_w > log 0.98: yes |
| o4_chained | A1 | uart | -0.99% | 0.49% | 79.0 | -1.81% | -0.17% | +0.18% | 0.9762 | 30 / 0.025000 | slower_than_o4 |  | LB95_w > log 0.98: yes |
| o4_chained | A1 | cli | same bytes as o4, not timed | | | | | | | | same_bytes | | |
| o4_chained | A1 | pooled | -0.09% | 0.41% | 117.9 | -0.77% | +0.58% | - | 0.5899 | 25 / 0.007143 | not_shown | 5 / 0.025000 | excluded (holm) |
| o4_isb | I1 | packet | +0.41% | 0.63% | 39.0 | -0.65% | +1.48% | +1.95% | 0.2586 | 16 / 0.003125 | not_shown |  | LB95_w > log 0.98: yes |
| o4_isb | I1 | uart | +0.11% | 0.39% | 79.0 | -0.54% | +0.76% | +1.04% | 0.3894 | 22 / 0.005000 | not_shown |  | LB95_w > log 0.98: yes |
| o4_isb | I1 | cli | same bytes as o4, not timed | | | | | | | | same_bytes | | |
| o4_isb | I1 | pooled | +0.19% | 0.33% | 116.5 | -0.36% | +0.75% | - | 0.2802 | 18 / 0.003571 | not_shown | - | **included** (FIDELITY) |
| o4_rank | A3 | packet | +1.51% | 0.61% | 39.0 | +0.48% | +2.54% | +2.99% | 0.0088 | 3 / 0.001724 | not_shown |  | LB95_w > log 0.98: yes |
| o4_rank | A3 | uart | same bytes as o4, not timed | | | | | | | | same_bytes | | |
| o4_rank | A3 | cli | same bytes as o4, not timed | | | | | | | | same_bytes | | |
| o4_rank | A3 | pooled (= packet, the only timed workload) | +1.51% | 0.61% | 39.0 | +0.48% | +2.54% | - | 0.0088 | - (same hypothesis as the packet row) | - | 1 / 0.008333 | excluded (holm) |
| o4_min2 | A7 | packet | +0.29% | 0.67% | 39.0 | -0.84% | +1.42% | +1.91% | 0.3337 | 19 / 0.003846 | not_shown |  | LB95_w > log 0.98: yes |
| o4_min2 | A7 | uart | -0.56% | 0.50% | 79.0 | -1.38% | +0.27% | +0.63% | 0.8665 | 29 / 0.016667 | not_shown |  | LB95_w > log 0.98: yes |
| o4_min2 | A7 | cli | +0.78% | 0.56% | 39.0 | -0.16% | +1.72% | +2.14% | 0.0839 | 6 / 0.001923 | not_shown |  | LB95_w > log 0.98: yes |
| o4_min2 | A7 | pooled | +0.10% | 0.32% | 150.2 | -0.44% | +0.63% | - | 0.3826 | 21 / 0.004545 | not_shown | 4 / 0.016667 | excluded (holm) |
| o4_legacy | A8 | packet | +0.37% | 0.58% | 39.0 | -0.60% | +1.35% | +1.77% | 0.2604 | 17 / 0.003333 | not_shown |  | LB95_w > log 0.98: yes |
| o4_legacy | A8 | uart | same bytes as o4, not timed | | | | | | | | same_bytes | | |
| o4_legacy | A8 | cli | same bytes as o4, not timed | | | | | | | | same_bytes | | |
| o4_legacy | A8 | pooled (= packet, the only timed workload) | +0.37% | 0.58% | 39.0 | -0.60% | +1.35% | - | 0.2604 | - (same hypothesis as the packet row) | - | 3 / 0.012500 | excluded (holm) |
| o4_tasks | A2a | packet | +0.98% | 0.76% | 39.0 | -0.30% | +2.25% | +2.81% | 0.1016 | 7 / 0.002000 | not_shown |  | LB95_w > log 0.98: yes |
| o4_tasks | A2a | uart | same bytes as o4, not timed | | | | | | | | same_bytes | | |
| o4_tasks | A2a | cli | same bytes as o4, not timed | | | | | | | | same_bytes | | |
| o4_tasks | A2a | pooled (= packet, the only timed workload) | +0.98% | 0.76% | 39.0 | -0.30% | +2.25% | - | 0.1016 | - (same hypothesis as the packet row) | - | 2 / 0.010000 | excluded (holm) |
| fl3b3 | C1 | packet | +2.03% | 0.67% | 39.0 | +0.90% | +3.16% | +3.66% | 0.0022 | 1 / 0.001613 | not_shown |  |  |
| fl3b3 | C1 | uart | -0.12% | 0.47% | 79.0 | -0.91% | +0.67% | +1.00% | 0.5988 | 26 / 0.008333 | not_shown |  |  |
| fl3b3 | C1 | cli | -0.32% | 0.44% | 39.0 | -1.07% | +0.42% | +0.75% | 0.7643 | 28 / 0.012500 | not_shown |  |  |
| fl3b3 | C1 | pooled | +0.20% | 0.29% | 132.8 | -0.28% | +0.68% | - | 0.2482 | 15 / 0.002941 | not_shown | - | BEST: not_shown |
| o4_hostall (campaign H) | A6 | packet | -4.05% | 0.65% | 39.0 | -5.14% | -2.96% | -2.49% | 1.0000 | H: 8 / 0.010000 | slower_than_o4 |  | LB95_w > log 0.98: NO |
| o4_hostall (campaign H) | A6 | uart | -12.45% | 0.68% | 79.0 | -13.58% | -11.32% | -10.83% | 1.0000 | H: 10 / 0.016667 | slower_than_o4 |  | LB95_w > log 0.98: NO |
| o4_hostall (campaign H) | A6 | cli | -0.32% | 0.51% | 39.0 | -1.18% | +0.53% | +0.91% | 0.7355 | H: 6 / 0.007143 | not_shown |  | LB95_w > log 0.98: yes |
| o4_hostall (campaign H) | A6 | pooled | -4.51% | 0.34% | 120.7 | -5.08% | -3.94% | - | 1.0000 | H: 9 / 0.012500 | slower_than_o4 | 6 / 0.050000 | excluded (holm) |

## Composition (decide.py §3.7 groups and §3.8)

- O4-r2 items: I1, X1, X4; add-ons [], removals ['o4_nohi', 'o4_nospec'], fidelity ['o4_isb']; changes firmware-inline true, host-inline true.
- Group notes: o4_nocp: accepted, moot: X4 removes the whole SPEC bundle.
- Configurations faster than O4 to absorb (BEST, A2 and H): o4_pwhopr.
- B2's 'rejected candidate with the highest pooled d': o4_rank.

## Driver's own statistics (for reference; they decide nothing)

Median of the per-block ratio reference/arm of block-summed time over **all** blocks including burn-in block 0, with a 95% percentile bootstrap interval. **Above 1 means the second-named arm is faster.** The reference is o4 alone, not the pooled o4/o4-aa.

| comparison | packet | uart | cli |
|---|---|---|---|
| A/A o4/o4-aa | 0.9937 [0.9856, 1.0158], harness_bias false | 0.9911 [0.9803, 0.9994], harness_bias true | 1.0143 [0.9930, 1.0190], harness_bias false |
| o4/o4_nohi | 1.0090 [0.9908, 1.0220] within_noise | 0.9918 [0.9791, 1.0066] within_noise | 1.0050 [0.9844, 1.0276] within_noise |
| o4/o4_noregions | 1.0061 [0.9962, 1.0217] within_noise | 0.9965 [0.9904, 1.0084] within_noise | 0.9867 [0.9779, 0.9957] within_noise |
| o4/o4_chained | 1.0097 [0.9921, 1.0368] within_noise | 0.9839 [0.9710, 0.9951] within_noise | - |
| o4/o4_isb | 1.0012 [0.9893, 1.0182] within_noise | 0.9970 [0.9843, 1.0060] within_noise | - |
| o4/o4_rank | 1.0119 [1.0009, 1.0367] within_noise | - | - |
| o4/o4_min2 | 1.0073 [0.9846, 1.0247] within_noise | 0.9892 [0.9782, 0.9993] within_noise | 1.0117 [0.9889, 1.0320] within_noise |
| o4/o4_legacy | 1.0041 [0.9957, 1.0178] within_noise | - | - |
| o4/fl3b3 | 1.0192 [1.0026, 1.0505] within_noise | 0.9912 [0.9825, 1.0024] within_noise | 1.0003 [0.9922, 1.0112] within_noise |
| o4/o4_tasks | 1.0088 [0.9839, 1.0432] within_noise | - | - |
| o4/o4_nocp | - | 0.9978 [0.9919, 1.0170] within_noise | 1.0042 [0.9941, 1.0186] within_noise |
| o4/o4_nospec | - | 0.9904 [0.9768, 1.0063] within_noise | 1.0074 [0.9982, 1.0281] within_noise |

## Next

- P5: build O4-r2 from the accepted items, I1 + X1 + X4 (O4-r1 with `isb_boundary = true`, without host-inline and without the SPEC bundle), fresh, with every §3.2 gate. Then B1 `[o4, o4r2]` under REMOVE (no add-on), seed 20261127, and B2 with o4_rank as the rejected candidate with the highest d.
- BEST still fails in H: A5 o4_pwhopr is faster than O4 on cli and pooled. Under §3.7/§3.8, O4-r2 must absorb it or document why it cannot. A5 replaces hot-loop selection, the base that I1 modifies. This step does not decide that.
- O4-r2 changes firmware-inline (I1) and host-inline (X1). So §3.8 requires H to be rerun with o4r2 as the reference after B2.
