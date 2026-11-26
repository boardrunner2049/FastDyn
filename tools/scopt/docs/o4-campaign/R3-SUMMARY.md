# Campaign R3: result

Run 2026-11-26 05:55-06:05 EST and decided at 06:05, under the rule pre-registered in `driver/PREREG-R3.md` (exact digest below). The decision is the output of `driver/decide_r3.py` (sha256 `e6554ab034b85d021df575b2eb401c1cf288e26614e37a66c9477c517409969c`, the digest PREREG-R3.md and `DECIDE_R3.sha256` name). decide_r3.py imports the registered decide.py unchanged (`0b7247476a39abade599000de4e2555c7edb1167b57d0989f4bac54cbaf1a438`, `registered: true`). Neither file was changed.

- PREREG-R3.md sha256: `7d63ada3cb2a6d39c42bb536ce3c0368fef0da1b6ae442c69884e6cc37e3a9d4`
- Decision: `/data/scfix-o4/R3.json` (sha256 `f5449daca3236bf992785ba8b88577504b079decdab893bf3b508847cdc974db`). Its console output is in `R3.stdout.txt`.

**Winner: o4r3d (A5 + A3 + X1 + X4).** The decision is final: VALID passed on all three workloads, so no rerun was needed, nothing is pending and no workload has a reference-bias flag. All four variants pass ADD-confirm against O4-r1. o4r3c (A5 + A3) has the highest pooled d. The pre-registered tie-break picked o4r3d because it is within 0.3% of that d and is the only variant in the band with X1 and X4, which give a deterministic build.

The F plans are written and validated in `plans/F/` but have not been run. This step does not run F. O4 is not changed by R3 alone: adoption needs BEST to hold in F.

## Outcome

1. **VALID: ok on every workload.** The 99% interval of the reference / A/A log-ratio contains 0 on packet, uart and cli. There is no rerun, `provisional: false` and no warnings.
2. **ADD-confirm: all four variants pass.** Each passes all three checks:
   - G0 is true in `gates/all.json`;
   - Holm rejects its pooled hypothesis (family of 4, α 0.05);
   - LB95_w > log 0.98 (−2.02%) on every workload.
3. **Choice.**
   - The best pooled d is o4r3c's: +1.060% (exactly 0.0105991).
   - The band is d ≥ +0.760% (0.0075991). It holds o4r3a (+0.967%), o4r3c (+1.060%) and o4r3d (+0.764%, exactly 0.0076407).
   - o4r3b (+0.685%) is outside the band.
   - Tie-break step 1, prefer X1 + X4: of the band, only o4r3d has them, so the set becomes {o4r3d}.
   - Tie-break step 2, prefer no A3: no variant in {o4r3d} is without A3, so under the rule's fallback the set stays {o4r3d}.
   - **Winner o4r3d** (arm o4r3d).
4. **o4r3d made the band by a very small margin.** Its pooled d is 0.0042 percentage points above the band edge (0.0076407 against 0.0075991). Had o4r3d's d been that much lower, the winner would have been o4r3c, the highest d without X1 + X4. This is the registered rule applied as written. It is flagged here for the lead.
5. **The variants are not distinguishable from each other in this campaign.** These figures are descriptive and not part of the rule (see "Direct comparisons between variants"). The direct paired estimate of o4r3c against o4r3d is pooled +0.11% (SE 0.34%), one-sided 95% [−0.45%, +0.67%]. The 0.30-point gap between their estimates against O4-r1 is mostly reference noise. Campaign F, with o4r3d as the reference and o4r3c, o4_pwhopr (o4r3a) and o4r3b among its arms, is the registered check that none of them is faster than the winner.

## Estimates (decide_r3.py / decide.py, d = log speedup vs O4-r1, positive = faster)

Every estimate follows the registered method:
- Hodges-Lehmann over the timed blocks (block 0 is burn-in);
- SE from a 20,000-round block bootstrap;
- Student-t reference with df_w = n_w − 1;
- inverse-variance pooling with Welch df.

LB95 and UB95 are one-sided 95% bounds. p is P(T > d/SE) for "the variant is faster than O4-r1".

### VALID (reference o4 vs A/A o4-aa; decide.py's 99% bootstrap interval)

| workload | attempt | blocks | a_w = HL log(o4/o4-aa) | SE | 99% interval | biased |
|---|---:|---:|---:|---:|---|---|
| packet | 1 | 40 | -0.41% | 0.74% | [-2.18%, +1.59%] | no |
| uart | 1 | 80 | +0.68% | 0.55% | [-0.77%, +2.06%] | no |
| cli | 1 | 40 | -0.35% | 0.57% | [-1.80%, +1.09%] | no |

rerun_required: none; provisional: false; reference_bias workloads: none; warnings: none.

### Per variant and workload

The "timed as" column shows how each variant was measured on each workload. An alias has the same cache.sc bytes as the timed arm it names, and its row is that arm's estimate.

| variant (arm) | parts | workload | timed as | d | SE | df | LB95 | UB95 | p | LB95 > log 0.98 |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---|
| o4r3a (o4_pwhopr) | A5 | packet | o4_pwhopr | -0.36% | 0.88% | 39 | -1.84% | +1.11% | 0.65979 | yes |
| o4r3a (o4_pwhopr) | A5 | uart | o4_pwhopr | +0.43% | 0.43% | 79 | -0.28% | +1.14% | 0.16060 | yes |
| o4r3a (o4_pwhopr) | A5 | cli | o4_pwhopr | +1.74% | 0.41% | 39 | +1.06% | +2.43% | 0.00006 | yes |
| o4r3b (o4r3b) | A5 + X1 + X4 | packet | o4r3b | -0.63% | 0.72% | 39 | -1.85% | +0.58% | 0.80697 | yes |
| o4r3b (o4r3b) | A5 + X1 + X4 | uart | o4r3b | +0.70% | 0.48% | 79 | -0.10% | +1.50% | 0.07569 | yes |
| o4r3b (o4r3b) | A5 + X1 + X4 | cli | o4r3b | +1.31% | 0.50% | 39 | +0.46% | +2.15% | 0.00644 | yes |
| o4r3c (o4r3c) | A5 + A3 | packet | o4r3c | +0.76% | 0.67% | 39 | -0.38% | +1.90% | 0.13344 | yes |
| o4r3c (o4r3c) | A5 + A3 | uart | alias of o4_pwhopr | +0.43% | 0.43% | 79 | -0.28% | +1.14% | 0.16060 | yes |
| o4r3c (o4r3c) | A5 + A3 | cli | alias of o4_pwhopr | +1.74% | 0.41% | 39 | +1.06% | +2.43% | 0.00006 | yes |
| o4r3d (o4r3d) | A5 + A3 + X1 + X4 | packet | o4r3d | -0.03% | 0.65% | 39 | -1.14% | +1.07% | 0.52093 | yes |
| o4r3d (o4r3d) | A5 + A3 + X1 + X4 | uart | alias of o4r3b | +0.70% | 0.48% | 79 | -0.10% | +1.50% | 0.07569 | yes |
| o4r3d (o4r3d) | A5 + A3 + X1 + X4 | cli | alias of o4r3b | +1.31% | 0.50% | 39 | +0.46% | +2.15% | 0.00644 | yes |

### Pooled, Holm and ADD-confirm

| variant (arm) | pooled d | SE | df | LB95 | UB95 | p | Holm rank | threshold | rejected | G0 | LB_w ok | **ADD-confirm** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|---|
| o4r3a (o4_pwhopr) | +0.97% | 0.28% | 120.9 | +0.50% | +1.43% | 0.00036 | 2 | 0.01667 | True | True | True | **True** |
| o4r3b (o4r3b) | +0.69% | 0.31% | 141.8 | +0.17% | +1.20% | 0.01512 | 4 | 0.05000 | True | True | True | **True** |
| o4r3c (o4r3c) | +1.06% | 0.27% | 130.6 | +0.61% | +1.51% | 0.00007 | 1 | 0.01250 | True | True | True | **True** |
| o4r3d (o4r3d) | +0.76% | 0.31% | 144.7 | +0.26% | +1.27% | 0.00692 | 3 | 0.02500 | True | True | True | **True** |

The Holm family has 4 hypotheses at α 0.05. Each variant's pooled estimate is its own hypothesis, because o4r3c and o4r3d each have their own packet timing. The step-down rejects all four, in this order:

| rank | variant | p | threshold |
|---:|---|---:|---:|
| 1 | o4r3c | 0.000068 | 0.0125 |
| 2 | o4r3a | 0.00036 | 0.0167 |
| 3 | o4r3d | 0.0069 | 0.025 |
| 4 | o4r3b | 0.0151 | 0.05 |

### Choice and tie-break

| step | set |
|---|---|
| passing ADD-confirm | o4r3a, o4r3b, o4r3c, o4r3d |
| best pooled d | o4r3c, +1.0599% |
| within 0.3% (d ≥ +0.7599%) | o4r3a (+0.9668%), o4r3c (+1.0599%), o4r3d (+0.7641%) |
| prefer X1 + X4 (deterministic build) | **o4r3d** |
| prefer without A3 | no candidate without A3, so it stays o4r3d (the rule's fallback) |
| highest d among the rest | **o4r3d** |

**Winner o4r3d (A5 + A3 + X1 + X4), status "winner", final true.**

## Direct comparisons between variants (descriptive, not part of the rule)

`r3/direct_pairs_r3.py` writes `r3/R3-direct-pairs.json`. It uses decide.py's own HL, block bootstrap and t bounds on the direct paired block log-ratio between two variants over the same timed blocks. d > 0 means the first variant is faster. Workloads where the two variants have the same bytes are left out.

| pair | packet | uart | cli | pooled [one-sided 95%] |
|---|---:|---:|---:|---|
| o4r3c vs o4r3d (X1 + X4 on top of A5 + A3) | +0.88% (SE 0.72%) | −0.70% (SE 0.52%) | +0.63% (SE 0.58%) | +0.11% [−0.45%, +0.67%], p 0.37 |
| o4r3a vs o4r3b (X1 + X4 on top of A5) | +0.23% (SE 0.89%) | −0.70% | +0.63% | −0.05% [−0.64%, +0.53%], p 0.56 |
| o4r3c vs o4r3a (A3 on top of A5; packet only) | +0.97% (SE 0.81%) | same bytes | same bytes | +0.97% [−0.39%, +2.33%], p 0.12 |
| o4r3d vs o4r3b (A3 on top of A5 + X1 + X4; packet only) | +0.36% (SE 0.99%) | same bytes | same bytes | +0.36% [−1.31%, +2.03%], p 0.36 |
| o4r3a vs o4r3d | −0.54% | −0.70% | +0.63% | −0.18% [−0.76%, +0.40%], p 0.69 |

What these show:
- **X1 + X4 (no host-inline, no SPEC bundle) has no measurable cost on top of A5.** Pooled −0.05% to +0.11%, every interval inside ±0.8%. It is slightly faster on uart (+0.70%) and slightly slower on cli (−0.63%).
- **A3 points the right way on packet,** with +0.97% and +0.36%, but neither estimate is significant on its own.
- **I1 is the likely cause of O4-r2's packet loss.** A5 + X1 + X4 without I1 (o4r3b) has packet LB95 −1.85%. In B1, A5 + I1 + X1 + X4 (O4-r2) had packet −1.21%, LB95 −2.87%. This is a comparison across campaigns, so it is a hypothesis, not a measurement.
- **R3 reproduces campaign H's A5 numbers.** o4r3a against O4-r1:

  | | packet | uart | cli | pooled |
  |---|---:|---:|---:|---|
  | campaign H | −0.11% | +0.47% | +1.97% | +0.93% |
  | campaign R3 | −0.36% | +0.43% | +1.74% | +0.97% [+0.50%, +1.43%] |

The driver's own console lines in `<w>/timing-R3.log` are descriptive too. They are median block ratios over all 41/81/41 blocks, burn-in included, so they are not the registered statistic.

## Campaign R3 run

Every timed arm ran with its gated cache. The plans recorded each cache's sha256 and the driver recorded each identity (`identities.json`).

| | packet | uart | cli |
|---|---|---|---|
| plan | `plans/R3/packet.json` (`8b73c8d2…`) | `plans/R3/uart.json` (`cd381191…`) | `plans/R3/cli.json` (`cf2b5617…`) |
| output | `packet/timing-R3/` (+ `.log`) | `uart/timing-R3/` | `cli/timing-R3/` |
| CPU | 10 | 20 | 12 |
| seed | 20261201 | 20261201 | 20261201 |
| blocks | 41 (1 burn-in + 40) | 81 (1 + 80) | 41 (1 + 40) |
| timed arms (incl. A/A) | o4, o4_pwhopr, o4r3b, o4r3c, o4r3d, o4-aa (6) | o4, o4_pwhopr, o4r3b, o4-aa (4) | o4, o4_pwhopr, o4r3b, o4-aa (4) |
| aliases | none | o4r3c → o4_pwhopr, o4r3d → o4r3b | o4r3c → o4_pwhopr, o4r3d → o4r3b |
| runs (complete grid) | 246 | 648 | 328 |
| settle | 30 s (argument; the plan says 30 too) | 30 s | 30 s |
| 1-min load at launch / before block 0 | 0.47 / 2.75 | 0.47 / 2.11 | 0.47 / 2.17 |
| block 0 ended / last block ended | 05:56:54 / 06:01:44 | 05:56:25 / 06:04:53 | 05:56:01 / 05:56:49 |
| SD of A/A block log-ratios (timed) | 5.49% | 4.95% | 3.57% |
| runs > 1.3x arm/case median: burn-in / timed (max) | 0 / 0 | 0 / 1 (o4-aa help, 1.478x, block 20) | 2 / 0 (1.737x) |

- **Launch.** The three drivers were launched together at 05:55:03 by `p6/launch.sh R3 plans/R3 timing-R3`, the launcher of campaigns B1-H2, used unchanged. Each driver ran `taskset -c <cpu> $PY -B compare_rtos.py … --settle-seconds 30`.
- **No failures.** No driver failed, and the known "allocate supercache code buffer: File exists" error did not occur, so there is no attemptN.
- **Records.**
  - Run facts: `r3/runinfo-R3.json`.
  - Diagnostics: `hygiene/R3/diag.json`.
  - Launch log: `hygiene/R3/launch.log`.

## Hygiene (O4-design §3.9)

Recorded in `hygiene/R3/{before,after}.json` by `p6/hygiene.py`, the script of campaigns B1-H2, used unchanged. It runs git with `--no-optional-locks`.

- **Load.** The 1-minute load was 0.43 at the before-check (05:54:58) and 0.47 at launch, both ≤ 2.0, so the campaign started without waiting. No other driver, QEMU or scopt process was running.
- **No builds** ran during the campaign. One campaign ran at a time. Nothing grepped or searched the caches during timing.
- **QEMU** `/data/qemu-scfix-bin/aacca05/qemu-system-arm` is unchanged before and after:
  - sha256 `951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60`;
  - inode 87162887, dev 2050, size 137799456, mtime_ns 1795640690884852222;
  - `qemu_unchanged: true`.
- **fd-int** stayed at HEAD `b35cdb62b970a2b69da790734de46b9769e9d831` on branch o4, with empty `status --porcelain` before and after (`fd_int_unchanged: true`).
- **find -newer `hygiene/R3/start.marker`** listed nothing in any of these trees:
  - `/data/scfix-r2`
  - `/data/scfix-campaign`
  - `/data/trebuchet-clean`
  - `/data/qemu-scfix-bin`
  - `/data/fastdyn/.git/worktrees/fd-int`
  - `/data/fastdyn-work/qemu-fu`
  - the whole of `/data/fastdyn`, checked separately after the timing (`hygiene/R3/newer-fastdyn-full.txt`, empty)
- **No bytecode.** There are no `__pycache__`, `.pyc` or `.pytest_cache` entries in fd-int, `driver/`, `p6/`, `r3/` or `plans/`.
- **Digests unchanged:**
  - driver `compare_rtos.py`: `f7046c86…`;
  - decide.py: `0b724747…`;
  - decide_r3.py: `e6554ab0…`;
  - PREREG-R3.md: `7d63ada3…`;
  - make_r3_plans.py: `3c750827…`;
  - gates/all.json: `bad5bf48…`.

  These were recorded before launch in `hygiene/R3/before-r3-sha256.txt` and are the same afterwards.

## Campaign F plans (written and validated, not run)

`r3/make_r3_plans.py F --r3 /data/scfix-o4/R3.json` wrote `plans/F/{packet,uart,cli}.json` (log `r3/make-F-plans.log`). It refuses to plan without a final R3 winner, and the winner is o4r3d.

- **Reference.** o4r3d, with A/A arm o4r3d-aa.
- **Run settings.** 31 / 21 / 21 blocks (30 / 20 / 20 + burn-in), seed 20261202 (VALID rerun 20261302), settle 30 s, CPUs 10 / 20 / 12, x_attempt 1.
- **Binding.** Each plan is bound to this decision: `x_r3_decision` = `/data/scfix-o4/R3.json`, sha256 `f5449dac…74db`. `x_winner` is o4r3d.

| plan | sha256 | timed arms (+ o4r3d-aa) | same bytes as o4r3d (dropped) | aliases | ~minutes |
|---|---|---|---|---|---:|
| `plans/F/packet.json` | `129f925caca122eb6f477a14a4ca8cf94b62b85f52cd4ca04ee69b800cecda26` | o4r3d, o4, o4_pwhopr, o4r3b, o4r3c, o4r2, o4r2b, fl3b3, fl3b3p, o4_chained, o4_isb, o4_legacy, o4_min2, o4_nohi, o4_noregions, o4_rank, o4_tasks, s6b3 (18) | none | o4_nocp → o4, o4_nospec → o4, c7b3 → o4 | 15.6 |
| `plans/F/uart.json` | `8bb0c1ffb1f477dea05876119e9299006813f253d7dc41aed59bc5ce1e62d7ce` | o4r3d, o4, o4_pwhopr, o4r2, o4r2b, fl3b3, fl3b3p, o4_chained, o4_isb, o4_min2, o4_nocp, o4_nohi, o4_noregions, o4_nospec (14) | o4r3b | c7b3, o4_legacy, o4_rank, o4_tasks → o4; o4r3c → o4_pwhopr; s6b3 → o4_noregions | 10.4 |
| `plans/F/cli.json` | `2abcfae5629a66ffa82dde01a4227b07f0266ff8ef05d41a9aac99c030d0eafa` | o4r3d, o4, o4_pwhopr, o4r2b, fl3b3, fl3b3p, o4_min2, o4_nocp, o4_nohi, o4_noregions, o4_nospec (11) | o4r2, o4r3b | c7b3, o4_legacy, o4_rank, o4_tasks, o4_chained, o4_isb → o4; o4r3c → o4_pwhopr; s6b3 → o4_noregions | 2.8 |

**Registered F arm set** (`decide_r3.f_arms("o4r3d")`, 21 arms): o4r3d, o4, o4_pwhopr, o4r3b, o4r3c, o4r2, o4r2b, c7b3, fl3b3, fl3b3p, o4_chained, o4_isb, o4_legacy, o4_min2, o4_nocp, o4_nohi, o4_noregions, o4_nospec, o4_rank, o4_tasks, s6b3. Each plan covers exactly this set once its timed, same-bytes and alias arms are combined.

### Inclusion criterion

`r3/make_r3_plans.py criterion` recomputed the criterion from the recorded decide outputs (`screen.json`, `A2.json`, `H.json`, `B1.json`, `B1-retry.json`; output `r3/F-criterion.json`). The result equals the registered `decide_r3.F_CRITERION`; make_r3_plans would refuse to write the F plans otherwise. The criterion is each arm's latest pooled estimate against O4-r1, kept when UB95 > log 0.99 = −1.005%.

| arm | campaign | pooled d | pooled UB95 | in F |
|---|---|---:|---:|---|
| o4_rank | A2 | +1.513% | +2.542% | yes |
| o4_tasks | A2 | +0.978% | +2.251% | yes |
| o4_legacy | A2 | +0.374% | +1.346% | yes |
| o4_nocp | A2 | +0.577% | +1.135% | yes |
| o4_nospec | A2 | +0.325% | +0.884% | yes |
| o4_nohi | A2 | +0.235% | +0.773% | yes |
| o4_isb | A2 | +0.194% | +0.747% | yes |
| fl3b3 | A2 | +0.199% | +0.681% | yes |
| o4_min2 | A2 | +0.097% | +0.634% | yes |
| o4_chained | A2 | −0.093% | +0.581% | yes |
| fl3b3p | A | −0.696% | +0.531% | yes |
| o4_noregions | A2 | −0.176% | +0.293% | yes |
| s6b3 | A | −0.800% | +0.122% | yes |
| c7b3 | A | same bytes as O4-r1 on every workload | — | yes (alias of o4) |
| b3l | A | −2.316% | −1.166% | **no** |
| w7bd_b3 | H | −3.566% | −2.951% | **no** |
| o4_hotgraph | A | −3.634% | −2.425% | **no** |
| o4_hostall | H | −4.509% | −3.938% | **no** |
| p7 | A | −4.580% | −3.777% | **no** |
| fl3 | A | −14.697% | −13.713% | **no** |
| s6 | A | −16.668% | −15.512% | **no** |

Also in F:
- **By rule:** o4_pwhopr / o4r3a (H: +0.930%, UB +1.425%), o4r2 = O4-r2 (B1: +0.486%, UB +1.017%), o4r2b = O4-r2b (B1-retry: +0.183%, UB +0.681%).
- **The other R3 variants,** o4r3b and o4r3c, and O4-r1 (o4).
- **Never a campaign arm:** o4_pinned.

### Validation

`driver/validate_r3.py --scratch <session scratchpad>/r3/validateF --record plans/F/validation.json plans/F/{packet,uart,cli}.json` returned rc 0, **ok: true, problems: []**. Log: `r3/validate-F.log`. It ran:
- every validate_plans.py check: 43 launches, cache sha256, runtime identity, gates g0/sha for 42 arms plus 1 anchor, no QEMU_SC_SEALED_SKIP_IRQ, 17 aliases, 20 not-timed sha, outputs free;
- the round checks: decide_r3 sha, PREREG sha, seed / blocks / burn-in / settle, the F arm set, the binding to R3.json, the plan wrapper sha;
- decide_r3 accepting each plan;
- a full `decide_r3.py F` over synthetic null observations, with a BEST family of 58 hypotheses.

Its "adopted: true" was computed on synthetic data. It only shows the plumbing works and is **not a result**. Expected wall time is 15.6 min (packet-bound).

How to run F, per PREREG-R3.md (not done in this step):
```
# after the §3.9 load check; one driver per workload, concurrently (p6/launch.sh F /data/scfix-o4/plans/F timing-F)
$PY -B driver/decide_r3.py F packet/timing-F uart/timing-F cli/timing-F --r3 R3.json --gates gates/all.json --out F.json
```

## Deviations and notes

1. **`--settle-seconds 30` was passed on the driver command line,** as in campaigns A-H2, so host.json records `settle_source: "argument"`. PREREG-R3.md's "How to run" line leaves it out and relies on the plan's `settle_seconds: 30`. The value is the same and decide_r3 checks the plan's 30.
2. **The F plans were made with `--r3 /data/scfix-o4/R3.json`,** an absolute path, where PREREG's line has the relative `R3.json`. It is the same file. The absolute path makes `x_r3_decision.path` resolvable from any directory, and the binding is by sha256.
3. **Hygiene and launch used the earlier steps' scripts unchanged** (`p6/hygiene.py`, `p6/launch.sh`, `p6/diag.py`, `p6/runinfo.py`). New in `r3/`:
   - `tables_r3.py`: tables only;
   - `direct_pairs_r3.py`: the descriptive pairwise estimates above, not part of the rule;
   - outputs `R3-tables.md`, `R3-direct-pairs.json`, `runinfo-R3.json`, `F-criterion.json`, `make-F-plans.log`, `validate-F.log`.
4. **No VALID rerun and no launch-error rerun** were needed.
5. **For the lead:**
   - The winner comes from the pre-registered tie-break with a margin of 0.0042 percentage points of d. The highest-d variant, o4r3c (+1.06%), keeps host-inline and the SPEC bundle, so its build depends on helper PGO (gdb, `--pgo-launch`).
   - o4r3d drops both and its build is deterministic.
   - Their direct difference is +0.11% ± 0.34% (SE) for o4r3c, not distinguishable.
   - F's BEST, with o4r3d as the reference, tests o4r3c, o4_pwhopr, o4r3b, o4_rank and the rest against it.
   - o4r3d is adopted as O4 only if BEST holds there.
