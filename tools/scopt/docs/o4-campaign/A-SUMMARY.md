# Campaign A (screen): result

Run 2026-11-26 02:01-02:12 EST. O4-design.md §3.4, decided by the registered decide.py (`0b7247476a39abade599000de4e2555c7edb1167b57d0989f4bac54cbaf1a438`, registered: true).

- decide.py output: `/data/scfix-o4/screen.json` (sha256 `3cac638cad722d34f6b3fa817ece1e78322ff8afc0b3d5b6050ab3102ad3755a`); its stdout is `/data/scfix-o4/screen.stdout.txt`. Command: `decide.py packet/timing-A uart/timing-A cli/timing-A --gates gates/all.json --out screen.json` (cwd /data/scfix-o4).
- Driver output: `/data/scfix-o4/{packet,uart,cli}/timing-A/` and logs `/data/scfix-o4/{packet,uart,cli}/timing-A.log`.
- Hygiene records: `/data/scfix-o4/hygiene/A/{before,after}.json`, `launch.log`, `start.marker`.

## Outcome

- **VALID holds on all three workloads**; no rerun was needed (`rerun_required` is empty).
- **BEST holds**: family 56, no arm or scope faster than O4 after Holm. The smallest p is o4_min2 pooled, 0.0281 against a Holm threshold of 0.000893.
- **To A2 (10, combined mode, seed 20261130):** o4_nohi (X1; packet/uart/cli), o4_nocp (X2; uart/cli), o4_noregions (X3; packet/uart/cli), o4_nospec (X4; uart/cli), o4_chained (A1; packet/uart), o4_isb (I1; packet/uart), o4_rank (A3; packet), o4_min2 (A7; packet/uart/cli), o4_legacy (A8; packet), fl3b3 (C1; packet/uart/cli).
- **Decided without A2:**
  - C2 b3l: **challenger not to A2**, pooled d -2.32% <= 0 (95% [-3.47%, -1.17%]); this screen row is its best-check (not faster than O4).
  - C1p fl3b3p: **challenger not to A2**, pooled d -0.70% <= 0 (95% [-1.92%, +0.53%]); this screen row is its best-check (not faster than O4).
  - A4 o4_hotgraph: **excluded, measured_slower** (UB99_packet -1.73%; pooled d -3.63%, 95% [-4.84%, -2.43%]).
- No removal was rejected: no removal has UB99_w < 0 on any workload. The closest are X3 o4_noregions on cli (UB99 +0.24%) and X1 o4_nohi on cli (UB99 +0.44%).
- No item had the same bytes as o4 on all three workloads, so none was decided for free.
- **A7 (o4_min2) survived** to A2: pooled d +1.31%, SE 0.66%, 95% [+0.19%, +2.43%]. **C1p pooled d is -0.70%** (not above 0). So P4b (§3.1: land §2.4 and build o4_tasks) is triggered only if A7 is accepted in A2.

## Run

| | packet | uart | cli |
|---|---|---|---|
| CPU | 10 | 20 | 12 |
| blocks (burn-in block 0 + timed) | 21 (1 + 20) | 11 (1 + 10) | 11 (1 + 10) |
| timed arms (incl. o4, o4-aa) | 17 | 16 | 14 |
| runs | 357 | 352 | 308 |
| settle | 30 s (argument) | 30 s | 30 s |
| 1-min load at start / before block 0 | 0.81 / 4.38 | 0.81 / 4.65 | 0.81 / 3.47 |
| block 0 / last block ended | 02:04:54 / 02:11:40 | 02:04:34 / 02:09:10 | 02:03:07 / 02:03:46 |

- Launched 02:01:09, all three concurrently, each `taskset -c <cpu>` and the driver's own affinity, `--settle-seconds 30`, seed 20261126. Load at launch 0.81 (rule: <= 2.0). The load before block 0 (3.5-4.6) is the three drivers' own staging and warm replays.
- No driver failed. All 16-word checks, warm-cache identities, private-region identities and v7 counter checks passed (the driver raises on any of them). The QEMU identity check ran at staging.
- **Hygiene.** QEMU sha256 `951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60`, inode 87162887, dev 2050, size 137799456, mtime_ns 1795640690884852222: identical before and after (`qemu_unchanged: true`). `find -newer start.marker` over /data/scfix-r2, /data/scfix-campaign, /data/trebuchet-clean and /data/qemu-scfix-bin found nothing. fd-int: branch o4, HEAD ece9b5e, `git status` clean before and after. No .pyc, __pycache__ or .pytest_cache in fd-int or the driver directory. No build ran and no other campaign ran. No grep/find touched a cache while timing ran; the only activity of this step during it was 5-10 s process-alive polls (kill -0, pgrep) and a 3-minute `wc -l` of observations.jsonl.

## VALID (o4 / o4-aa, burn-in dropped, HL with two-sided 99% bootstrap interval)

| workload | blocks | a_w | 99% interval | SE | verdict |
|---|---:|---:|---|---:|---|
| packet | 20 | +0.10% | [-2.83%, +2.99%] | 1.14% | valid |
| uart | 10 | -2.33% | [-6.75%, +3.21%] | 1.96% | valid |
| cli | 10 | +1.27% | [-0.81%, +3.61%] | 0.97% | valid |

Notes, reported for completeness; neither changes the pre-registered decision:

- **The uart driver's own A/A flags harness_bias.** Its statistic (median of o4/o4-aa block ratios over all 11 blocks, 95% percentile interval) is 0.9907x [0.9491, 0.9993], which excludes 1 (o4 ran faster than its own copy). decide.py's pre-registered VALID (HL, burn-in dropped, 99%) does not flag it: -2.33% [-6.75%, +3.21%]. The uart A/A block ratios swing from 0.894 to 1.098; the SD of the A/A block log-ratios over blocks 1-10 is 6.22% (packet 4.68%, cli 2.78%), close to round 2's uart 7.00% (§3.3). Every estimate here is against the pooled o4/o4-aa reference, which is the design's guard against a fast or slow o4.
- **cli block 0 (burn-in) was disturbed:** 17 of its 22 runs took more than 1.3x their arm/case median (max 3.23x), and its A/A block ratio is 0.6830. Blocks 1-10 have one such run. decide.py drops block 0 as pre-registered. All of cli's timed blocks (02:03:07-02:03:46) ran while the packet and uart drivers were still staging (sparse cache copies and warm/regions replays on CPUs 10 and 20, until 02:04:02 / 02:04:21), because cli's staging is short. packet and uart had no run above 1.3x except one uart run in block 5 (1.40x).

## Screen verdicts

| item | arm | role | timed on | same bytes as o4 | UB99_w < 0 | pooled d | pooled 95% | screen verdict |
|---|---|---|---|---|---|---:|---|---|
| C2 | b3l | challenger | packet/uart/cli | - | none | -2.32% | [-3.47%, -1.17%] | challenger not to A2 (screen is its best-check) |
| C1 | fl3b3 | challenger | packet/uart/cli | - | none | +0.75% | [-0.38%, +1.89%] | to A2 |
| C1p | fl3b3p | challenger | packet/uart/cli | - | none | -0.70% | [-1.92%, +0.53%] | challenger not to A2 (screen is its best-check) |
| A1 | o4_chained | addon | packet/uart | cli | none | +0.37% | [-0.90%, +1.63%] | to A2 |
| A4 | o4_hotgraph | addon | packet/uart/cli | - | packet | -3.63% | [-4.84%, -2.43%] | decided: excluded, measured slower |
| I1 | o4_isb | fidelity | packet/uart | cli | none | +1.25% | [-0.14%, +2.63%] | to A2 |
| A8 | o4_legacy | addon | packet | uart/cli | none | +0.21% | [-1.43%, +1.85%] | to A2 |
| A7 | o4_min2 | addon | packet/uart/cli | - | none | +1.31% | [+0.19%, +2.43%] | to A2 |
| X2 | o4_nocp | removal | uart/cli | packet | none | -0.50% | [-2.11%, +1.11%] | to A2 |
| X1 | o4_nohi | removal | packet/uart/cli | - | none | -0.87% | [-1.76%, +0.02%] | to A2 |
| X3 | o4_noregions | removal | packet/uart/cli | - | none | -0.57% | [-1.50%, +0.36%] | to A2 |
| X4 | o4_nospec | removal | uart/cli | packet | none | +0.12% | [-1.49%, +1.72%] | to A2 |
| A3 | o4_rank | addon | packet | uart/cli | none | +0.59% | [-0.76%, +1.94%] | to A2 |

## Estimates (decide.py, §3.3)

d is log(sqrt(T(o4) T(o4-aa)) / T(X)) by Hodges-Lehmann; **positive means X is faster than O4**. SE is the block-bootstrap SD; LB95/UB95 and UB99 use Student's t at df = n_w - 1 (pooled: Welch df). p is one-sided for "X faster than O4". BEST Holm is the campaign's 56-hypothesis family at alpha 0.05.

| arm | item | scope | d | SE | df | LB95 | UB95 | UB99 | p | BEST Holm rank / threshold | BEST verdict |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| o4_chained | A1 | packet | +0.70% | 0.87% | 19.0 | -0.80% | +2.20% | +2.90% | 0.2154 | 11 / 0.001087 | not_shown |
| o4_chained | A1 | uart | -0.56% | 1.45% | 9.0 | -3.21% | +2.09% | +3.52% | 0.6461 | 24 / 0.001515 | not_shown |
| o4_chained | A1 | cli | same bytes as o4, not timed | | | | | | | | same_bytes |
| o4_chained | A1 | pooled | +0.37% | 0.74% | 27.6 | -0.90% | +1.63% | - | 0.3136 | 16 / 0.001220 | not_shown |
| o4_isb | I1 | packet | +0.98% | 0.88% | 19.0 | -0.55% | +2.50% | +3.22% | 0.1419 | 7 / 0.001000 | not_shown |
| o4_isb | I1 | uart | +2.68% | 2.04% | 9.0 | -1.06% | +6.41% | +8.43% | 0.1108 | 4 / 0.000943 | not_shown |
| o4_isb | I1 | cli | same bytes as o4, not timed | | | | | | | | same_bytes |
| o4_isb | I1 | pooled | +1.25% | 0.81% | 25.0 | -0.14% | +2.63% | - | 0.0687 | 3 / 0.000926 | not_shown |
| o4_rank | A3 | packet | +0.59% | 0.78% | 19.0 | -0.76% | +1.94% | +2.58% | 0.2298 | 14 / 0.001163 | not_shown |
| o4_rank | A3 | uart | same bytes as o4, not timed | | | | | | | | same_bytes |
| o4_rank | A3 | cli | same bytes as o4, not timed | | | | | | | | same_bytes |
| o4_hotgraph | A4 | packet | -4.11% | 0.94% | 19.0 | -5.73% | -2.49% | -1.73% | 0.9998 | 44 / 0.003846 | slower_than_o4 |
| o4_hotgraph | A4 | uart | -7.27% | 2.92% | 9.0 | -12.62% | -1.92% | +0.97% | 0.9828 | 40 / 0.002941 | slower_than_o4 |
| o4_hotgraph | A4 | cli | -2.27% | 1.19% | 9.0 | -4.45% | -0.09% | +1.09% | 0.9555 | 36 / 0.002381 | slower_than_o4 |
| o4_hotgraph | A4 | pooled | -3.63% | 0.71% | 30.8 | -4.84% | -2.43% | - | 1.0000 | 46 / 0.004545 | slower_than_o4 |
| o4_min2 | A7 | packet | +1.65% | 0.86% | 19.0 | +0.15% | +3.14% | +3.84% | 0.0358 | 2 / 0.000909 | not_shown |
| o4_min2 | A7 | uart | +0.39% | 1.67% | 9.0 | -2.66% | +3.45% | +5.09% | 0.4097 | 18 / 0.001282 | not_shown |
| o4_min2 | A7 | cli | +1.10% | 1.33% | 9.0 | -1.34% | +3.53% | +4.85% | 0.2154 | 12 / 0.001111 | not_shown |
| o4_min2 | A7 | pooled | +1.31% | 0.66% | 35.5 | +0.19% | +2.43% | - | 0.0281 | 1 / 0.000893 | not_shown |
| o4_legacy | A8 | packet | +0.21% | 0.95% | 19.0 | -1.43% | +1.85% | +2.62% | 0.4140 | 19 / 0.001316 | not_shown |
| o4_legacy | A8 | uart | same bytes as o4, not timed | | | | | | | | same_bytes |
| o4_legacy | A8 | cli | same bytes as o4, not timed | | | | | | | | same_bytes |
| o4_nohi | X1 | packet | -0.66% | 0.70% | 19.0 | -1.87% | +0.55% | +1.11% | 0.8216 | 27 / 0.001667 | not_shown |
| o4_nohi | X1 | uart | +1.29% | 1.42% | 9.0 | -1.30% | +3.89% | +5.29% | 0.1925 | 10 / 0.001064 | not_shown |
| o4_nohi | X1 | cli | -2.33% | 0.98% | 9.0 | -4.13% | -0.53% | +0.44% | 0.9792 | 38 / 0.002632 | slower_than_o4 |
| o4_nohi | X1 | pooled | -0.87% | 0.53% | 34.9 | -1.76% | +0.02% | - | 0.9463 | 35 / 0.002273 | not_shown |
| o4_nocp | X2 | packet | same bytes as o4, not timed | | | | | | | | same_bytes |
| o4_nocp | X2 | uart | +1.49% | 1.81% | 9.0 | -1.83% | +4.80% | +6.59% | 0.2162 | 13 / 0.001136 | not_shown |
| o4_nocp | X2 | cli | -1.19% | 1.07% | 9.0 | -3.14% | +0.77% | +1.82% | 0.8532 | 31 / 0.001923 | not_shown |
| o4_nocp | X2 | pooled | -0.50% | 0.92% | 14.6 | -2.11% | +1.11% | - | 0.7027 | 25 / 0.001563 | not_shown |
| o4_noregions | X3 | packet | +0.46% | 0.73% | 19.0 | -0.81% | +1.73% | +2.33% | 0.2676 | 15 / 0.001190 | not_shown |
| o4_noregions | X3 | uart | -0.19% | 1.64% | 9.0 | -3.20% | +2.83% | +4.45% | 0.5440 | 23 / 0.001471 | not_shown |
| o4_noregions | X3 | cli | -2.46% | 0.96% | 9.0 | -4.21% | -0.71% | +0.24% | 0.9850 | 41 / 0.003125 | slower_than_o4 |
| o4_noregions | X3 | pooled | -0.57% | 0.55% | 33.4 | -1.50% | +0.36% | - | 0.8475 | 30 / 0.001852 | not_shown |
| o4_nospec | X4 | packet | same bytes as o4, not timed | | | | | | | | same_bytes |
| o4_nospec | X4 | uart | +1.80% | 1.38% | 9.0 | -0.73% | +4.33% | +5.70% | 0.1127 | 5 / 0.000962 | not_shown |
| o4_nospec | X4 | cli | -1.25% | 1.25% | 9.0 | -3.54% | +1.04% | +2.27% | 0.8291 | 29 / 0.001786 | not_shown |
| o4_nospec | X4 | pooled | +0.12% | 0.93% | 17.8 | -1.49% | +1.72% | - | 0.4501 | 20 / 0.001351 | not_shown |
| fl3b3 | C1 | packet | +0.98% | 0.96% | 19.0 | -0.68% | +2.63% | +3.41% | 0.1603 | 8 / 0.001020 | not_shown |
| fl3b3 | C1 | uart | +1.56% | 1.66% | 9.0 | -1.49% | +4.61% | +6.25% | 0.1868 | 9 / 0.001042 | not_shown |
| fl3b3 | C1 | cli | +0.05% | 1.14% | 9.0 | -2.04% | +2.14% | +3.27% | 0.4822 | 22 / 0.001429 | not_shown |
| fl3b3 | C1 | pooled | +0.75% | 0.67% | 34.5 | -0.38% | +1.89% | - | 0.1353 | 6 / 0.000980 | not_shown |
| fl3b3p | C1p | packet | -0.61% | 0.85% | 19.0 | -2.07% | +0.86% | +1.55% | 0.7586 | 26 / 0.001613 | not_shown |
| fl3b3p | C1p | uart | +0.70% | 1.86% | 9.0 | -2.71% | +4.12% | +5.97% | 0.3573 | 17 / 0.001250 | not_shown |
| fl3b3p | C1p | cli | -3.00% | 2.09% | 9.0 | -6.83% | +0.83% | +2.90% | 0.9074 | 33 / 0.002083 | not_shown |
| fl3b3p | C1p | pooled | -0.70% | 0.72% | 31.1 | -1.92% | +0.53% | - | 0.8282 | 28 / 0.001724 | not_shown |
| b3l | C2 | packet | -1.64% | 0.89% | 19.0 | -3.17% | -0.10% | +0.62% | 0.9596 | 37 / 0.002500 | slower_than_o4 |
| b3l | C2 | uart | -2.94% | 2.06% | 9.0 | -6.72% | +0.84% | +2.88% | 0.9062 | 32 / 0.002000 | not_shown |
| b3l | C2 | cli | -3.39% | 1.23% | 9.0 | -5.64% | -1.14% | +0.08% | 0.9889 | 42 / 0.003333 | slower_than_o4 |
| b3l | C2 | pooled | -2.32% | 0.68% | 33.6 | -3.47% | -1.17% | - | 0.9991 | 43 / 0.003571 | slower_than_o4 |
| s6 | s6 | packet | -14.20% | 0.99% | 19.0 | -15.92% | -12.49% | -11.68% | 1.0000 | 54 / 0.016667 | slower_than_o4 |
| s6 | s6 | uart | -14.82% | 1.45% | 9.0 | -17.48% | -12.16% | -10.72% | 1.0000 | 47 / 0.005000 | slower_than_o4 |
| s6 | s6 | cli | -21.94% | 1.25% | 9.0 | -24.23% | -19.65% | -18.42% | 1.0000 | 51 / 0.008333 | slower_than_o4 |
| s6 | s6 | pooled | -16.67% | 0.68% | 36.3 | -17.82% | -15.51% | - | 1.0000 | 56 / 0.050000 | slower_than_o4 |
| s6b3 | s6b3 | packet | +0.04% | 0.72% | 19.0 | -1.22% | +1.29% | +1.88% | 0.4808 | 21 / 0.001389 | not_shown |
| s6b3 | s6b3 | uart (alias of o4_noregions) | -0.19% | 1.64% | 9.0 | -3.20% | +2.83% | +4.45% | 0.5440 | 23 / 0.001471 | not_shown |
| s6b3 | s6b3 | cli (alias of o4_noregions) | -2.46% | 0.96% | 9.0 | -4.21% | -0.71% | +0.24% | 0.9850 | 41 / 0.003125 | slower_than_o4 |
| s6b3 | s6b3 | pooled | -0.80% | 0.54% | 33.5 | -1.72% | +0.12% | - | 0.9242 | 34 / 0.002174 | not_shown |
| p7 | p7 | packet | -4.27% | 0.62% | 19.0 | -5.34% | -3.21% | -2.71% | 1.0000 | 48 / 0.005556 | slower_than_o4 |
| p7 | p7 | uart | -4.54% | 1.88% | 9.0 | -7.99% | -1.09% | +0.76% | 0.9805 | 39 / 0.002778 | slower_than_o4 |
| p7 | p7 | cli | -5.11% | 0.80% | 9.0 | -6.58% | -3.63% | -2.84% | 0.9999 | 45 / 0.004167 | slower_than_o4 |
| p7 | p7 | pooled | -4.58% | 0.47% | 31.1 | -5.38% | -3.78% | - | 1.0000 | 53 / 0.012500 | slower_than_o4 |
| c7b3 | c7b3 | packet | same bytes as o4, not timed | | | | | | | | same_bytes |
| c7b3 | c7b3 | uart | same bytes as o4, not timed | | | | | | | | same_bytes |
| c7b3 | c7b3 | cli | same bytes as o4, not timed | | | | | | | | same_bytes |
| fl3 | fl3 | packet | -7.83% | 1.02% | 19.0 | -9.60% | -6.06% | -5.23% | 1.0000 | 49 / 0.006250 | slower_than_o4 |
| fl3 | fl3 | uart | -17.05% | 1.09% | 9.0 | -19.04% | -15.06% | -13.99% | 1.0000 | 50 / 0.007143 | slower_than_o4 |
| fl3 | fl3 | cli | -18.60% | 0.93% | 9.0 | -20.30% | -16.91% | -15.99% | 1.0000 | 52 / 0.010000 | slower_than_o4 |
| fl3 | fl3 | pooled | -14.70% | 0.58% | 31.6 | -15.68% | -13.71% | - | 1.0000 | 55 / 0.025000 | slower_than_o4 |

s6b3 on uart and cli is read off o4_noregions (identical cache.sc); those scopes share o4_noregions' Holm result. c7b3 is byte-identical to o4 on all three workloads.

## Driver's own statistics (for reference; they decide nothing)

Median of the per-block ratio reference/arm of block-summed time over **all** blocks including burn-in block 0, with a 95% percentile bootstrap interval. **Above 1 means the second-named arm is faster.** The reference is o4 alone, not the pooled o4/o4-aa.

| comparison | packet | uart | cli |
|---|---|---|---|
| A/A o4/o4-aa | 1.0162 [0.9745, 1.0295], harness_bias false | 0.9907 [0.9491, 0.9993], harness_bias true | 1.0039 [0.9843, 1.0292], harness_bias false |
| o4/o4_chained | 1.0181 [0.9831, 1.0286] within_noise | 1.0047 [0.9475, 1.0299] within_noise | - |
| o4/o4_isb | 1.0127 [0.9750, 1.0353] within_noise | 1.0296 [0.9514, 1.0792] within_noise | - |
| o4/o4_rank | 1.0013 [0.9831, 1.0130] within_noise | - | - |
| o4/o4_hotgraph | 0.9635 [0.9240, 0.9885] within_noise | 0.9236 [0.8788, 0.9924] within_noise | 0.9894 [0.9441, 1.0163] within_noise |
| o4/o4_min2 | 1.0034 [0.9945, 1.0423] within_noise | 0.9601 [0.9464, 1.0426] within_noise | 1.0178 [0.9880, 1.0365] within_noise |
| o4/o4_legacy | 1.0000 [0.9756, 1.0176] within_noise | - | - |
| o4/o4_nohi | 0.9907 [0.9726, 1.0054] within_noise | 1.0072 [0.9465, 1.0478] within_noise | 0.9748 [0.9579, 1.0100] within_noise |
| o4/o4_noregions | 1.0032 [0.9843, 1.0284] within_noise | 0.9698 [0.9403, 1.0254] within_noise | 0.9846 [0.9665, 1.0041] within_noise |
| o4/fl3b3 | 1.0236 [0.9812, 1.0287] within_noise | 0.9946 [0.9665, 1.0217] within_noise | 1.0144 [0.9695, 1.0331] within_noise |
| o4/fl3b3p | 0.9938 [0.9780, 1.0167] within_noise | 1.0071 [0.9347, 1.0595] within_noise | 0.9753 [0.9221, 1.0140] within_noise |
| o4/b3l | 0.9765 [0.9488, 1.0112] within_noise | 0.9737 [0.9399, 1.0298] within_noise | 0.9704 [0.9222, 1.0023] within_noise |
| s6/o4 | 1.1552 [1.1290, 1.1749] above_floor | 1.1602 [1.1243, 1.2020] above_floor | 1.2504 [1.2033, 1.3007] above_floor |
| s6b3/o4 | 1.0036 [0.9893, 1.0183] within_noise | - | - |
| p7/o4 | 1.0465 [1.0293, 1.0648] within_noise | 1.0716 [0.9880, 1.0899] within_noise | 1.0596 [1.0290, 1.0754] within_noise |
| fl3/o4 | 1.0680 [1.0491, 1.1141] above_floor | 1.1912 [1.1582, 1.2074] above_floor | 1.2054 [1.1694, 1.2252] above_floor |
| fl3/fl3b3 | 1.1005 [1.0643, 1.1139] above_floor | 1.1980 [1.1232, 1.2124] above_floor | 1.2064 [1.1776, 1.2435] above_floor |
| fl3b3/fl3b3p | 0.9799 [0.9568, 0.9899] within_noise | 0.9940 [0.9749, 1.0409] within_noise | 0.9733 [0.9476, 1.0129] within_noise |
| s6b3/o4_noregions | 0.9982 [0.9816, 1.0227] within_noise | - | - |
| s6b3/b3l | 0.9814 [0.9715, 1.0003] within_noise | - | - |
| o4/o4_nocp | - | 1.0037 [0.9622, 1.0616] within_noise | 0.9878 [0.9554, 1.0318] within_noise |
| o4/o4_nospec | - | 1.0125 [0.9638, 1.0243] within_noise | 0.9973 [0.9295, 1.0061] within_noise |
| o4_noregions/b3l | - | 0.9720 [0.9498, 1.0101] within_noise | 0.9870 [0.9517, 1.0047] within_noise |

## Next

Campaign H (§3.6) runs next, then A2 (combined, one plan per workload, seed 20261130) from this screen: `make_plan.py A2 --screen /data/scfix-o4/screen.json`. This step wrote no A2 or H plans.
