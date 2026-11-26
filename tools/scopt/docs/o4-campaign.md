# scopt O4: the investigation and campaign record

This is the consolidated record of how scopt O4 was designed, built, timed and decided on
2026-11-25 and 2026-11-26. It is the "TIMING.md" that `docs/O4.md` and `pipelines/O4-r3.toml`
cite. Two identical copies exist: fd-int `tools/scopt/docs/o4-campaign.md` and
`/data/scfix-o4/TIMING.md`.

**Conventions.**

- d is decide.py's estimate: the Hodges-Lehmann (HL) median of the block log-ratios
  log(T(reference) / T(arm)), in percent. **Positive means the arm is faster than the
  reference.** SE is the block-bootstrap SD. LB and UB are one-sided 95% bounds. p is the
  one-sided p for "the arm is faster than the reference".
- A cell "d (SE) [LB, UB]" gives all four. "same bytes" means the arm's cache.sc equals the
  reference's on that workload, so it was not timed there. "alias of Y" means the arm has Y's
  bytes there and its row is Y's estimate.
- Ratios (round 1 and 2 numbers) are time(reference) / time(arm): above 1, the arm is faster.
- Triples are packet / uart / cli. Times are EST on 2026-11-26 unless a date is given.
- Source paths are relative to `/data/scfix-o4/` unless they start with `/`, `docs/`,
  `scopt/`, `pipelines/` or `scripts/` (those four are fd-int `tools/scopt/`). The repo keeps
  copies of most of the cited files under `docs/o4-campaign/`; §10 maps them.
- Arm names: `o4` is O4-r1 as built by `-O4`; `o4-aa` is its A/A copy (the same cache, timed
  as a second arm). `o4r2` is O4-r2, `o4r2b` O4-r2b, `o4r3a`-`o4r3d` the round-3 variants.
  Items (A1, X3, ...) are defined in §4.

## 1. Summary

**What O4 is now.** O4 is revision **O4-r3** (`pipelines/O4-r3.toml`, equal to
`pipelines/O4.toml`; `scopt optimize -O4` builds it). It is O4-r1 with two changes:

- **A5, recursive priced regions.** firmware-inline uses `mode = "exhaustive"` with
  `cost_model = "profile"` instead of hot-loop with `min_call_count = 1000`. Exhaustive
  selection expands every call inside a region recursively, and the sealed steady profile
  prices each region's whole recursive closure. On these images the regions reach depth
  5 / 5 / 5 with 7,052 / 1,228 / 1,470 members (`docs/O4.md`; `rec/DESIGN.md` §0).
- **A3, ranked static chaining.** chain-static uses `rank_by_net_work = true` and
  `c_target_check = 0.0`.

Every other pass of O4-r1 stays: host-inline, the SPEC bundle (specialize-returns,
constant-propagation, branch-folding, dead-host-ops), chain-returns, chain-static and member
skip. O4-r1 itself is round 2's c7b3 under a new name (same cache.sc bytes). Built with
`-O4`, O4-r3's cache.sc equals the timed arm `o4r3c` byte for byte: packet `8ecd5af0…`, uart
`c4eae805…`, cli `cdb87351…` (`evidence/r3/identity.json`). It was committed as fd-int
`56c4c80` and pushed (origin/treb-wip).

**Headline numbers.** Sources: `R3-SUMMARY.md`, `F-SUMMARY.md`, `H-SUMMARY.md`,
`evidence/r3/decide.json`.

| comparison | campaign | packet | uart | cli | pooled [LB, UB] |
|---|---|---:|---:|---:|---|
| O4-r3 (o4r3c) against O4-r1 | R3, registered | +0.76% | +0.43% | +1.74% | **+1.06% [+0.61%, +1.51%]**, p 0.00007 |
| O4-r3 (o4r3c) against o4r3d (O4-r3 without host-inline and SPEC) | F, registered row | +0.06% | -0.13% | +0.25% | +0.10% [-0.60%, +0.80%], p 0.4051 |
| O4-r1 against o4r3d | F, registered row | -1.55% | +0.73% | -1.14% | -1.01% [-1.76%, -0.26%] |
| O4-r3 (o4r3c) against O4-r1, direct pair | F, descriptive | +1.73% | -0.56% | +1.39% | +1.11% [+0.25%, +1.98%], p 0.017 |
| A5 alone (o4_pwhopr) against O4-r1 | H, registered | -0.11% | +0.47% | +1.97% | +0.93% [+0.44%, +1.42%], p 0.0011 |

- **Best-check.** In campaign F (reference o4r3d) no arm is faster than the reference under
  the strict rule (58 hypotheses) or under the "most cases" rule (19 pooled and 40
  per-workload hypotheses). Re-read post hoc with O4-r3 (o4r3c) as the reference, BEST also
  holds (57 hypotheses, nothing rejected; §5.11.4).
- **How O4-r3 was chosen.** The registered R3 rule picked o4r3d by a tie-break (prefer the
  variant without host-inline and SPEC), and F adopted o4r3d. After the user's correction
  "the key is to have all our optimizations in recursive manner thats it", the lead adopted
  o4r3c instead: it keeps every optimization, had the highest pooled d in R3, and ties o4r3d
  in F. This is a deviation from the registered choice (§7.13).

**What is open** (§9): no campaign has been registered with O4-r3 itself as the reference;
only region formation is recursive today (B3, SPEC and host-inline do not reach inside region
members). A staged plan exists and nothing in it has been built. In the campaign the plan's
critique step was stopped before it ran; a critique was written afterwards
(`docs/o4-recursive-critique.md`), and a later analysis (`rec/no-qemu-routes.md`, archived
as `docs/o4-campaign/recursive/no-qemu-routes.md`) shows that no stage needs a QEMU change
and contests the critique's sizing (§8.6). Host-inline's gdb
counted replay is non-deterministic and occasionally fails; a rare QEMU code-buffer launch
failure (EEXIST) was recorded, not fixed.

Earlier rounds: round 1 is `/data/scfix-campaign/TIMING.md`, round 2 is
`/data/scfix-r2/TIMING.md` (its summary is in the scopt docs through fd-int commit `29af67d`).

## 2. Timeline

Sources: the files named in the last column, and `git log` in fd-int. "file time" means the
time comes from the file's modification time, not from text inside it.

| when | step | outcome | record |
|---|---|---|---|
| 2026-11-25 | Design inputs: inventory and feasibility of every scopt optimization; first probe builds (no timing) | inventory rows, probe receipts | `design/inventory.md`, `design/feasibility.md`, `design/probe/` |
| 2026-11-25 | Design revision 2, after a 12-finding review; 24 dry probe2 builds | O4-r1 = c7b3; 15 arms plus the deferred A2a; decision rule written before data | `design/O4-design.md` (§0-§5, Appendix A), `design/O4-design.v1.md` |
| 2026-11-25 22:55 - 11-26 00:41 | Implementation: 19 commits `b970979`..`ece9b5e` (§3.8) | `enabled` key, `isb_boundary`, `hot_loop_kinds`, capability table, O4 pipelines, `-O4` preflight and stage gate, `scopt/o4.py`, evidence generator, `docs/O4.md` | fd-int `git log` |
| 00:01 (file time) | O4-r1 identity at `7245da7` | `-O4` = c7b3 bytes on all three workloads; round-2 observations copied for the evidence seed | `evidence/r1-seed/identity.json` |
| 00:45 - before 01:50 | P0-P2: rebuild checks, O4-r1 identity at `ece9b5e`, 17 builds per workload, G0, regions replays | all pass; same-bytes pairs found | `{packet,uart,cli}/VERIFY.md` §1-§5 |
| 01:50 - 01:57 | Plans for A; decide.py re-registered (registration 2: Student-t, aliases, combined A2) | before any O4 timing | `driver/PREREG.md` |
| 02:01 - 02:12 | Campaign A, the screen | 10 items to A2; A4 excluded; C1p and C2 stop | `A-SUMMARY.md` |
| 02:18 - 02:25 | Campaign H, attempt 1 | uart driver failed at block 35: "allocate supercache code buffer: File exists"; not used | `hygiene/H/attempt1/failure.json` |
| 02:27 - 02:41 | Campaign H, attempt 2 (decided) | **A5 faster than O4-r1** (cli and pooled); A6 excluded | `H-SUMMARY.md` |
| 03:00 | fd-int `b35cdb6` (hot-loop-tasks, A2a); P4b build and gates | o4_tasks differs from o4 on packet only | `{packet,uart,cli}/VERIFY.md` "P4b" |
| 03:17 - 03:41 | Campaign A2, head-to-heads (combined mode) | no add-on passes Holm (A3 misses by 0.0005); X1, X2, X4 removable; X3 rejected; I1 included | `A2-SUMMARY.md` |
| 03:48 - 04:04 | P5: decisions; O4-r2 = A5 + I1 + X1 + X4 composed and built (03:53-03:54) | G0 pass | `decisions.json`, `p5/`, VERIFY.md "P5" |
| 04:10 | B1 rule fixed: ADD-confirm | before any B plan | `p6/B1-rule.md` |
| 04:11 - 04:19 | Campaign B1, O4-r2 against O4-r1 | **fail** (packet non-inferiority) | `B-SUMMARY.md` |
| 04:22 - 04:23 | O4-r2b = I1 + X1 + X4 built | G0 pass | `B-SUMMARY.md` |
| 04:32 - 04:40 | B1-retry, O4-r2b against O4-r1 | pass (REMOVE) | `B-SUMMARY.md` |
| 04:42 - 04:50 | B2, best-check against O4-r2b | BEST holds | `B-SUMMARY.md` |
| 04:50 - 05:03 | H2, campaign H against O4-r2b | **fail**: A5 faster than O4-r2b | `B-SUMMARY.md` |
| 05:07 (file time) | Campaign B closed | BLOCKED: O4 stays O4-r1, A5 must be absorbed | `B-SUMMARY.md` |
| 05:18 - 05:25 | R3 builds: o4r3b, o4r3c, o4r3d (plus two uart check builds) | G0 pass | `r3/build.log`, VERIFY.md "R3" |
| 05:42 | Round R3 pre-registered (`decide_r3.py`) | before any R3 timing | `driver/PREREG-R3.md` |
| 05:55 - 06:05 | Campaign R3 | all four variants pass ADD-confirm; registered winner o4r3d | `R3-SUMMARY.md` |
| 06:12 - 06:28 | Campaign F timed (reference o4r3d) | | `F-SUMMARY.md` |
| ~06:27 | User: "Win in most cases is fine"; the lead writes the amendment while F packet is at 482 of 558 runs (the amendment's count; F's packet plan has 589 runs, §3.2) | BEST-most rule | `driver/AMENDMENT-F-most-cases.md` |
| 06:30 - 06:38 | F decided | strict BEST and BEST-most hold; o4r3d adopted under the registered rules | `F-SUMMARY.md` |
| by 06:40 | User: "the key is to have all our optimizations in recursive manner thats it" | quoted by the recursion maps | `rec/map-regions.md`, `rec/DESIGN.md` §1.1 |
| 06:40 - 07:03 (file times) | Recursion investigation: five maps and a staged design | design only; in the campaign its critique step was stopped before it ran (a critique was written afterwards, two rows below) | `rec/` |
| 07:16 - 07:19 (file times) | O4-r3 = o4r3c chosen; `-O4` identity; F re-read post hoc with o4r3c as reference | identity equal on all three | `evidence/r3/` |
| 07:20 | fd-int `56c4c80` "Make O4 recursive: O4-r3 adds priced recursive regions and ranked chaining" | pushed | fd-int `git log` |
| by 07:55 | Critique of the recursion design and maps, written afterwards for this documentation (read-only) | U7 premise confirmed false; stage 3 sized at 198 / 53 / 37 slot copies; DESIGN's QEMU member flag judged needed | `docs/o4-recursive-critique.md` (no original under `/data/scfix-o4`) |
| 08:01 - 08:17 (file times) | Later read-only analysis: routes for B3, SPEC and host-inline inside region members with QEMU unchanged; at 08:17 it gained an adversarial check of its own routes (its §5) | contests the critique: 29 / 16 / 6 residual slot copies; no QEMU change needed (§8.6). Final at 08:17; archived as `docs/o4-campaign/recursive/no-qemu-routes.md` | `rec/no-qemu-routes.md` (08:17 version: 44,484 B, sha256 `eb2f2c0a…`) |

## 3. Method

### 3.1 Workloads, inputs and runtime

Sources: `design/O4-design.md` §1.5-§1.6, `{packet,uart,cli}/VERIFY.md`, `F-SUMMARY.md`.

- **Workloads.** The packet, uart and cli FreeRTOS images, built with FirmBOLT's `-O4`. Cases:
  packet one (`heldout-1`); uart and cli two each (`commands`, `help`). Every run must print
  the 16 oracle words and one BENCH line.
- **Inputs.** Private copies of round 2's sealed inputs in `/data/scfix-o4/<w>-w/in`
  (snapshot, sealed steady profile, prep, ELF, runtime.json) and a retargeted launch. Profile
  markers: packet 0x833e8, uart 0x822fc, cli 0x82fc4.
- **QEMU.** `/data/qemu-scfix-bin/aacca05/qemu-system-arm`, sha256 `951775b0…eebc60`,
  unchanged throughout (checked before and after every build step and campaign).
- **Runtime.** `QEMU_SC_SEALED=1`, `QEMU_SC_TRUSTED=1`, no icount, `-accel
  tcg,supercache=<cache>`, plugin `libfastdyn.so` with `coverage=0`, `FASTDYN_TIMING=1`.
- **Builds** ran under `nice -n 10 taskset -c 21-31` with `/usr/bin/time -v`, a 3,600 s
  timeout and an 8 GiB RSS watchdog on the heavy builds, never while a campaign was timing.

### 3.2 Campaign settings

Sources: the campaign summaries; `r3/runinfo-F.json`; `hygiene/<C>/launch.log`.

Every campaign ran the three workloads concurrently, one driver each, on CPUs packet 10,
uart 20, cli 12 (`taskset -c` plus the driver's own affinity), with `--settle-seconds 30`.
Blocks include the burn-in block 0, which decide.py drops.

| campaign | purpose | reference / A/A arm | blocks p / u / c | seed | launched (1-min load) | runs p / u / c | decided by |
|---|---|---|---|---|---|---|---|
| A | screen of every light arm | o4 / o4-aa | 21 / 11 / 11 | 20261126 | 02:01:09 (0.81) | 357 / 352 / 308 | decide.py → `screen.json` |
| H attempt 1 | heavy arms | o4 / o4-aa | 41 / 81 / 41 | 20261128 | 02:18:13 (0.52) | failed on uart | not used |
| H | heavy arms A5, A6, w7bd_b3 | o4 / o4-aa | 41 / 81 / 41 | 20261128 | 02:27:46 (1.30) | 205 / 810 / 410 | decide.py → `H.json` |
| A2 | head-to-heads, combined | o4 / o4-aa | 41 / 81 / 41 | 20261130 | 03:17:17 (0.80) | 451 / 1,620 / 656 | decide.py over A2 + H → `A2.json` |
| B1 | confirm O4-r2 | o4 / o4-aa | 41 / 81 / 41 | 20261127 | 04:11:18 (1.43) | 123 / 486 / 246 | decide.py → `B1.json` |
| B1-retry | confirm O4-r2b | o4 / o4-aa | 41 / 81 / 41 | 20261127 | 04:32:33 (1.45) | 123 / 486 / 246 | decide.py → `B1-retry.json` |
| B2 | best-check against O4-r2b | O4-r2b (named o4r2) / o4r2-aa | 21 / 11 / 11 | 20261129 | 04:42:18 (1.23) | 231 / 264 / 242 | decide.py → `B2.json`, `B.json` |
| H2 | campaign H against O4-r2b | O4-r2b / o4r2-aa | 41 / 81 / 41 | 20261228 | 04:50:16 (1.83) | 205 / 810 / 410 | decide.py → `H2.json` |
| R3 | four compositions against O4-r1 | o4 / o4-aa | 41 / 81 / 41 | 20261201 | 05:55:03 (0.47) | 246 / 648 / 328 | decide_r3.py → `R3.json` |
| F | best-check against the R3 winner | o4r3d / o4r3d-aa | 31 / 21 / 21 | 20261202 | 06:12:24 (0.13) | 589 / 630 / 504 | decide_r3.py → `F.json`; best_most.py → `F-most.json` |

A VALID rerun would have used seed + 100. None was needed in any campaign.

### 3.3 The estimator

Source: `design/O4-design.md` §3.3; `driver/PREREG.md`.

For arm X on workload w, over the timed blocks b ≥ 1 (block 0 is burn-in):

    l_b = log( sqrt(T_b(ref) · T_b(ref-aa)) / T_b(X) )

where T_b is the block sum over cases.

- d_w is the HL estimate of the l_b (the median of the Walsh averages).
- SE_w is the SD of 20,000 block-bootstrap resamples of d_w, seeded `f"{seed}:{w}:{X}"`.
- Pooled over the workloads where X's bytes differ from the reference's:
  d = Σ(d_w / SE_w²) / Σ(1 / SE_w²) and SE = (Σ 1 / SE_w²)^-½.
- Bounds and p use Student's t: df_w = n_w - 1 per workload, and the Welch-Satterthwaite df
  for the pool. LB = d - t(0.95, df)·SE and UB = d + t(0.95, df)·SE. The screen also uses
  UB99_w = d_w + t(0.99, df_w)·SE_w.

**Why these choices** (O4-design §3.3). Round 2's byte-identical pair c7b3_not2t / c7b3 had a
block log-ratio SD of 3.66% / 7.00% / 3.12%. At round 2's 20 / 10 / 10 blocks the minimum
detectable effect of the median was 2.9% / 7.8% / 3.5%, too coarse for add-ons of 2% or less.
HL had a smaller bootstrap SE than the median (0.86% against 0.94% on packet, 2.50% against
3.47% on uart). The pooled reference sqrt(o4 · o4-aa) stops a slow or fast o4 from moving every
comparison together. A head-to-head at 40 / 80 / 40 blocks was projected to give SE about
0.53% / 0.77% / 0.46%, and about 0.32% pooled.

The driver's own statistics (median block ratios over all blocks, burn-in included) are
reported in each summary and decide nothing.

### 3.4 decide.py and the Student-t re-registration

Source: `driver/PREREG.md`; `driver/sim/fwer_reg2.out`, `fwer_sim.out`, `fwer_t.out`.

decide.py is the pre-registered decider. Its sha256 is written into every plan
(`x_decide_sha256`), and `make_plan.py` refuses to write a plan when decide.py differs.

| registration | sha256 | made | reference distribution |
|---|---|---|---|
| 1 (superseded, archived in `driver/prereg/`) | `75e7b12e4e2cb252bc359208cf2c469c9b384dfca3f034f72c187fb5b0e84b52` | by the infra step (the 00:01 evidence seed used it) | normal |
| **2 (used by every campaign)** | `0b7247476a39abade599000de4e2555c7edb1167b57d0989f4bac54cbaf1a438` | 01:56, before any O4 timing | Student-t |

The switch was the lead's decision, made after the infra step showed that HL / SE_boot has
heavier tails than a normal at 10-20 blocks. `driver/sim/fwer_reg2.py` re-ran the infra
step's null data with registration 2's own functions (`random.Random(42)`, 2,000 bootstrap
rounds): a null 15-arm campaign A with o4 and o4-aa, 20 / 10 / 10 blocks after burn-in, round
2's pair SDs.

| null 15-arm campaign A | normal (registration 1) | Student-t (registration 2) | nominal |
|---|---:|---:|---:|
| BEST false "faster than O4", 400 simulations | 0.203 | **0.052** | ≤ 0.05 |
| the same, 2,000 simulations | 0.188 | **0.051** | ≤ 0.05 |
| screen: a null item has UB99_w < 0 on some workload, 400 / 2,000 simulations | 0.0515 / 0.0551 | 0.0237 / 0.0263 | ≤ 0.0297 |
| single one-sided 5% tests, packet / uart / cli, 2,000 simulations | 0.060 / 0.066 / 0.062 | 0.052 / 0.050 / 0.047 | 0.05 |

Registration 2 also changed the minimum to 2 timed blocks, added same-bytes aliases between
timed arms (§7.2) and the combined A2 mode (§7.3). Everything else (HL, the bootstrap,
pooling, Holm, the thresholds, the groups, B1 and B2, VALID) was unchanged. Its t functions
are pure Python; against scipy 1.11.4 the largest error was 1.55e-8 (scipy's own, at p 0.99,
df 2). The driver tests: 60 passed at registration (39 before).

### 3.5 The decision rules

Source: `design/O4-design.md` §3.4, §3.7, §3.8; `driver/PREREG-R3.md`;
`driver/AMENDMENT-F-most-cases.md`; `p6/B1-rule.md`.

- **VALID.** Per workload, a_w = HL of log(T(ref) / T(ref-aa)) with a two-sided 99% bootstrap
  interval. If the interval excludes 0, the workload is biased and its plan is rerun once at
  seed + 100; if still biased, every row carries `reference_bias`.
- **G0, correctness** (O4-design §3.2): optimize rc 0 and published; the functional check (16
  words, 1 BENCH line, counters revision 4 with no counter problems, every poll entry
  verified, `chain_skipped_trusted_poll_unverified` 0, sc6 invariant violations and flush
  resets 0); the loader requirement; 0 region-link refusals; the launched QEMU's sha256 equals
  the artifact's. An arm that fails G0 is `unsafe`, whatever its speed.
- **Screen** (campaign A): an add-on or removal with UB99_w < 0 on some workload is decided
  without a head-to-head (add-on excluded as measured_slower; removal rejected). A challenger
  goes to A2 only if its pooled d is above 0.
- **ADD** (add-ons A1, A3, A4, A6, A7, A8, A2a): G0; Holm step-down over the add-ons decided
  in A2 and H on the pooled p at family α 0.05; and LB_w > log 0.98 (-2.02%) on every
  workload where the bytes differ.
- **REMOVE** (X1-X4) and **FIDELITY** (I1): G0; UB ≥ 0; LB > log 0.99 (-1.01%); LB_w > log 0.98
  on every differing workload.
- **BEST** (the "best" property): in every campaign, every arm other than the reference and
  its A/A copy gets a pooled and per-workload one-sided p for "faster than the reference".
  One Holm family per campaign covers all of them. A rejection means a configuration is
  faster than O4, which O4 must then absorb.
- **Groups.** Host-inline (A4, A6, X1): highest pooled d, within 0.3% prefer X1, then A4,
  then A6. X3 applies only if no firmware add-on is accepted. X2 is moot if X4 is accepted.
  A1, A7, A8 and I1 compose.
- **B1** (confirmation of the composition, 41 / 81 / 41 blocks): ADD-confirm (pooled LB > 0
  and LB_w > log 0.98) if the composition carries an add-on, otherwise REMOVE. On failure,
  drop the add-on with the smallest A2 pooled d and repeat once. **B2**: best-check against
  the composition (21 / 11 / 11 blocks). If the composition changes firmware-inline or
  host-inline, campaign H is rerun against it (H2).
- **R3** (`decide_r3.py`, sha256 `e6554ab0…`; imports decide.py registration 2 unchanged).
  ADD-confirm of each variant against O4-r1: G0; Holm over the variants on the pooled p; and
  LB_w > log 0.98 on every differing workload. Choice: the highest pooled d among the
  passing variants; within 0.003 of it, prefer a variant with X1 and X4 (deterministic build:
  no helper PGO, no gdb, no `--pgo-launch`), then one without A3, then the highest d.
- **F** (after R3): strict BEST against the R3 winner W over a registered arm set (every
  built arm whose latest pooled UB95 against O4-r1 was above log 0.99, plus the R3 variants,
  O4-r1, O4-r2 and O4-r2b). Adoption: W passed ADD-confirm in R3 and BEST holds in F.
- **BEST-most** (the amendment, written during F timing; §7.12): a pooled Holm family (one
  hypothesis per arm) and a separate per-workload Holm family. It holds when no arm is
  rejected pooled and no arm is rejected on a majority of its differing workloads (2 of 3,
  or 2 of 2). An arm faster on one workload only is a **workload-specific win**: recorded,
  not blocking, and not absorbed into O4.

### 3.6 Hygiene

Source: `design/O4-design.md` §3.9; `hygiene/<campaign>/{before,after}.json`; the summaries.

- The private driver `driver/compare_rtos.py` (sha256 `f7046c86…`) is round 2's driver
  (`74b122dc…faee`) plus `--settle-seconds N` (sync and sleep after staging; N = 30), the
  burn-in block 0, and a check that the launched QEMU's sha256 equals the artifact's
  `qemu_sha256`. It raises on any failed 16-word check, warm-cache identity, private-region
  identity or v7 counter check.
- A campaign started only at a 1-minute load of 2.0 or less (H2 waited for the load to fall
  from 2.78 after B2). One campaign ran at a time, and no build ran during timing.
- During timing only process polls, `wc -l` of observations.jsonl and log tails ran; nothing
  grepped or searched the caches.
- Before and after every campaign: the QEMU sha256, inode, size and mtime were identical
  (`qemu_unchanged: true`); `find -newer start.marker` over `/data/scfix-r2`,
  `/data/scfix-campaign`, `/data/trebuchet-clean` and `/data/qemu-scfix-bin` (from B1 on also
  the fd-int worktree admin directory and `/data/fastdyn-work/qemu-fu`) found nothing; fd-int
  `git status` was clean (HEAD `ece9b5e` for A and H, `b35cdb6` from A2 on); no `.pyc`,
  `__pycache__` or `.pytest_cache`.

### 3.7 Build gates

Source: `design/O4-design.md` §1.7, §3.2; `{packet,uart,cli}/VERIFY.md`; `gates/all.json`.

- **Rebuild check (P0).** c7b3, fl3 and the probe's `hl`, rebuilt at `ece9b5e`, are byte-equal
  to round 2 and the probe on all three workloads. So the new `enabled` key, `isb_boundary`
  (absent) and `hot_loop_kinds` (default) change no existing bytes.
- **Identity (P1).** O4-r1 via `-O4` equals c7b3: packet `7702b19d…`, uart `ae360ff7…`, cli
  `869f9295…`. So O4-r1 inherits c7b3's round-1 and round-2 measurements.
- **G0** (§3.5) on every arm and workload, and the anchors on packet.
- **Regions replay** for every arm with regions: activations, lookup entries, residual exits,
  slots armed and skipped, per case.
- **Validity**: per-arm checks that the arm tests what it claims (receipt counts, absent
  passes, member checks).
- **Same bytes**: `cmp` of each cache.sc against o4's (later against every build). An equal
  pair is not timed on that workload.

`gates/all.json` has `all_g0: true` for all 23 built entries (the 22 arms of §4 plus the
identity aid `o4_pinned`).

### 3.8 The implementation commits

Source: fd-int `git log 5ff088c..ece9b5e`, plus `b35cdb6` and `56c4c80`.

| commit | time (2026-11-25/26) | subject |
|---|---|---|
| b970979 | 11-25 22:55 | Let a pipeline entry opt a default-disabled pass in |
| b4b6e4b | 11-25 23:00 | Make the ISB side exit a firmware-inline rule for any mode |
| 26d636e | 11-25 23:05 | Let hot-loop host-inline follow firmware loop kinds and full graphs |
| 348406e | 11-25 23:47 | Record known QEMU loader capabilities and refuse incapable digests |
| a6d4628 | 11-25 23:48 | Add the scopt O4 pipelines: frozen O4-r1 and the O4 alias |
| 7245da7 | 11-25 23:57 | Add scopt optimize -O4 with a preflight and a post-run stage gate |
| 30206f3 | 11-26 00:03 | Generate the O4 timing evidence from pinned campaign outputs |
| 9300eff | 11-26 00:06 | Record every O4 decision in scopt/o4.py and write the O4 note |
| a83822d | 11-26 00:35 | Test that the -O4 stage gate sees the host-inline native prefix |
| 87f39c4 | 11-26 00:35 | Refuse an active B3 cost calibration in the O4 preflight |
| 155d230 | 11-26 00:36 | Match O4 pipeline names with the design's ^O4(-r\d+)?$ |
| fed083f | 11-26 00:36 | Keep unpriced firmware-inline roots out of the O4 gate's no_change |
| 9c53c42 | 11-26 00:37 | Refuse host-inline hot_loop_kinds without hot_loop_only |
| 5750f35 | 11-26 00:39 | Require -O4's runtime identity to be the capturing QEMU's |
| 49e7d4a | 11-26 00:39 | Describe the capability table's per-capability admission |
| dc2e3f7 | 11-26 00:39 | Tighten the O4 manifest and pipeline tests |
| e99f72d | 11-26 00:40 | Correct three O4 decision evidence strings |
| ced0c01 | 11-26 00:40 | Correct the O4 note's claims to match its own evidence |
| ece9b5e | 11-26 00:41 | Drop the blank line at the end of docs/O4.md |
| b35cdb6 | 11-26 03:00 | Add firmware-inline mode hot-loop-tasks (O4 candidate A2a) |
| 56c4c80 | 11-26 07:20 | Make O4 recursive: O4-r3 adds priced recursive regions and ranked chaining |

At `b35cdb6` the scopt unit suite had 24 failures, exactly the pre-existing base set, with
1,940 passed and 490 skipped (`packet/VERIFY.md` "P4b").

## 4. Arms

Every single-item arm is `O4-r1.toml` plus exactly one change, generated and checked by
`design/gen_arms.py`. `design/commands.txt` has one optimize line per arm and workload (18 per
workload: 17 arms plus the identity aid `o4_pinned`). The compositions came later (P5, B, R3).

Sources: `design/O4-design.md` §1.8; `design/pipelines/`; `gates/all.json` (cache sha256,
G0); `{packet,uart,cli}/VERIFY.md` (regions, sites, same bytes, validity).

### 4.1 Single-item arms and challengers (P2, plus o4_tasks at P4b)

| item | arm | change against O4-r1 | pipeline file | packet | uart | cli | regions p / u / c |
|---|---|---|---|---|---|---|---|
| O4-r1 | `o4` | none; built with `-O4` | `O4-r1.toml` (= `O4.toml`) | `7702b19d` (= c7b3) | `ae360ff7` (= c7b3) | `869f9295` (= c7b3) | 48 / 19 / 7 |
| A1 | `o4_chained` | firmware-inline `residual_goto_tb = "chained"` | `o4-chained.toml` | `907da326` | `d11b4d7e` | same bytes | 56 / 23 / 7 |
| I1 | `o4_isb` | firmware-inline `isb_boundary = true` | `o4-isb.toml` | `ed26095f` | `425ed606` | same bytes | 48 / 19 / 7 |
| A3 | `o4_rank` | chain-static `rank_by_net_work = true`, `c_target_check = 0.0` | `o4-rank.toml` | `c618a47e` | same bytes | same bytes | 48 / 19 / 7 |
| A4 | `o4_hotgraph` | host-inline hot-loop with full helper graphs, `hot_loop_kinds = "firmware"`, 32,768 B graph cap | `o4-hotgraph.toml` | `00c0f056` | `34218f0e` | `1642b5a8` | 48 / 19 / 7 |
| A7 | `o4_min2` | firmware-inline `min_call_count = 2` | `o4-min2.toml` | `aac88e88` | `e412891d` | `039ffe30` | 52 / 26 / 13 |
| A8 | `o4_legacy` | firmware-inline `planner = "legacy"` | `o4-legacy.toml` | `115b2734` | same bytes | same bytes | 48 / 19 / 7 |
| X1 | `o4_nohi` | host-inline removed (no `--pgo-launch`) | `o4-nohi.toml` | `a3aba379` (= probe hl) | `8ecb39f6` (= probe hl) | `9a0dbd6d` (= probe hl) | 48 / 19 / 7 |
| X2 | `o4_nocp` | constant-propagation removed | `o4-nocp.toml` | same bytes | `7d4c4211` | `7622b90d` | 48 / 19 / 7 |
| X3 | `o4_noregions` | firmware-inline removed | `o4-noregions.toml` | `50548483` | `a8f1020e` (= s6b3) | `a3b91907` (= s6b3) | none |
| X4 | `o4_nospec` | SPEC bundle removed | `o4-nospec.toml` | same bytes | `b9a5ee92` | `accc25ae` | 48 / 19 / 7 |
| C1 | `fl3b3` | FirmLooper v3 + B3, unpriced, no host-inline | `o4x-fl3b3.toml` | `dfe9b96a` | `e569349d` (= probe2 c1) | `fc935ad4` (= probe2 c1) | 15 / 7 / 5 |
| C1p | `fl3b3p` | as C1, priced by the steady profile ("FirmLooper finds, PGO prices") | `o4x-fl3b3p.toml` | `45f98a80` | `53717050` (= probe2 c1p) | `ec1febf2` (= probe2 c1p) | 3 / 3 / 2 |
| C2 | `b3l` | `b3-llvm` with B3 opted in (layout passes, no regions) | fd-int `pipelines/b3-llvm.toml` | `54909b63` | `2736aa2e` | `87888a11` | none |
| A5 | `o4_pwhopr` | firmware-inline `mode = "exhaustive"`, `cost_model = "profile"` | `o4-pwhopr.toml` | `e6a723f8` | `c4eae805` | `cdb87351` | 53 / 27 / 14 |
| A6 | `o4_hostall` | bounded unrestricted host-inline (`allow_partial`, 32,768 B cap) | `o4-hostall.toml` | `619000e0` | `a84d4588` | `763be97b` | 48 / 19 / 7 |
| A2a | `o4_tasks` | firmware-inline `mode = "hot-loop-tasks"` (built at P4b, `b35cdb6`) | `o4-tasks.toml` | `5a8a9075` | same bytes | same bytes | 49 / 19 / 7 |

- **Anchors**, round-2 artifacts timed from private copies: s6, s6b3 (packet `25d8d8e6…`; on
  uart and cli its bytes are o4_noregions'), p7, c7b3 (O4-r1's bytes everywhere), fl3, and
  w7bd_b3 (the bounded round's artifact, in H).
- **Host-inline sites applied** (packet / uart / cli): O4-r1 1 / 0 / 0 (`helper_shl_cc` on
  packet); A4 13 / 20 / 2; A6 1,005 / 1,455 / 1,472. `helper_lookup_tb_ptr` was applied 0
  times in every arm.
- **G0**: pass for every arm on every workload. The anchors also passed on packet.
- **Validity**: every check passed, with these differences from the predictions: o4_legacy
  has 48 packet regions, not about 50; o4_rank's packet refusals are not_profitable +11 and
  slot_budget -11 (no `no_free_slot` or `inexact_lookup_traffic`); A1's added regions are
  entered ("entered, possibly slower"); o4_rank and o4_legacy have o4's bytes on uart or cli,
  which was not predicted.
- **Build cost** (P2 builds) of o4_pwhopr (A5, the selection O4-r3 adopted): packet 210 s and
  1,922,900 KiB peak RSS (the slowest P2 build on packet); uart 135.5 s and 1,398,588 KiB; cli
  203.2 s and 2,054,076 KiB. The heaviest P2 arm was o4_hostall (A6): packet 203 s and
  2,066,704 KiB, uart 149.7 s and 1,564,408 KiB, cli 215.3 s and 2,245,140 KiB, the highest
  peak RSS on all three and the slowest P2 build on uart and cli. Limits: 8 GB and 60
  minutes. Sources: `{packet,uart,cli}/VERIFY.md` P2 build tables; `packet/arms-built.json`,
  `cli/arms-built.json`.

### 4.2 Compositions (P5, B, R3)

| name | arm | parts | pipeline file | packet | uart | cli | regions p / u / c |
|---|---|---|---|---|---|---|---|
| O4-r2 | `o4r2` (timed as `o4r2a` in B2) | A5 + I1 + X1 + X4 | `O4-r2.toml`, built from its copy `o4r2.toml` | `805e430a` | `839cf96b` | `a4f6af65` | 53 / 27 / 14 |
| O4-r2b | `o4r2b` (timed as `o4r2` in B1-retry, B2, H2) | I1 + X1 + X4 | `O4-r2b.toml`, built from `o4r2b.toml` | `5c87d78b` | `77e0ffef` | `dba92a79` | 48 / 19 / 7 |
| o4r3a | timed as `o4_pwhopr` | A5 | `o4r3a.toml` (o4-pwhopr.toml's passes) | `e6a723f8` | `c4eae805` (rebuild equal) | `cdb87351` | 53 / 27 / 14 |
| o4r3b | `o4r3b` | A5 + X1 + X4 | `o4r3b.toml` | `df826da5` | `7f93d0d7` | `a4f6af65` (= O4-r2) | 53 / 27 / 14 |
| o4r3c | `o4r3c` | A5 + A3 | `o4r3c.toml` | `8ecd5af0` | `c4eae805` (= o4_pwhopr) | `cdb87351` (= o4_pwhopr) | 53 / 27 / 14 |
| o4r3d | `o4r3d` | A5 + A3 + X1 + X4 | `o4r3d.toml` | `1260bf79` | `7f93d0d7` (= o4r3b) | `a4f6af65` (= o4r3b, O4-r2) | 53 / 27 / 14 |
| **O4-r3** | `-O4` | = o4r3c | `pipelines/O4-r3.toml` | `8ecd5af0` | `c4eae805` | `cdb87351` | 53 / 27 / 14 |

Every composition passed G0, its validity checks and a regions replay (VERIFY.md "P5", "R3";
`B-SUMMARY.md` "O4-r2b"). A uart rebuild of `o4r3a.toml` equals o4_pwhopr's bytes (the
pipeline name does not reach cache.sc), and o4r3b built without `--pgo-launch` equals o4r3b
(`--pgo-launch` is inert without host-inline).

## 5. The campaigns

### 5.1 Campaign A: the screen

Source: `A-SUMMARY.md`, `screen.json`, `hygiene/A/`. 02:01-02:12; 21 / 11 / 11 blocks; seed
20261126. Arms timed: 17 / 16 / 14 including o4 and o4-aa.

**VALID** (all valid, no rerun):

| workload | blocks | a_w | 99% interval | SE |
|---|---:|---:|---|---:|
| packet | 20 | +0.10% | [-2.83%, +2.99%] | 1.14% |
| uart | 10 | -2.33% | [-6.75%, +3.21%] | 1.96% |
| cli | 10 | +1.27% | [-0.81%, +3.61%] | 0.97% |

**Estimates** against O4-r1, "d (SE) [LB, UB]":

| arm (item) | packet | uart | cli | pooled | screen verdict |
|---|---|---|---|---|---|
| o4_chained (A1) | +0.70% (0.87%) [-0.80%, +2.20%] | -0.56% (1.45%) [-3.21%, +2.09%] | same bytes | +0.37% (0.74%) [-0.90%, +1.63%] | to A2 |
| o4_isb (I1) | +0.98% (0.88%) [-0.55%, +2.50%] | +2.68% (2.04%) [-1.06%, +6.41%] | same bytes | +1.25% (0.81%) [-0.14%, +2.63%] | to A2 |
| o4_rank (A3) | +0.59% (0.78%) [-0.76%, +1.94%] | same bytes | same bytes | = packet | to A2 |
| o4_hotgraph (A4) | -4.11% (0.94%) [-5.73%, -2.49%] | -7.27% (2.92%) [-12.62%, -1.92%] | -2.27% (1.19%) [-4.45%, -0.09%] | -3.63% (0.71%) [-4.84%, -2.43%] | **excluded, measured slower** (UB99 packet -1.73%) |
| o4_min2 (A7) | +1.65% (0.86%) [+0.15%, +3.14%] | +0.39% (1.67%) [-2.66%, +3.45%] | +1.10% (1.33%) [-1.34%, +3.53%] | +1.31% (0.66%) [+0.19%, +2.43%] | to A2 |
| o4_legacy (A8) | +0.21% (0.95%) [-1.43%, +1.85%] | same bytes | same bytes | = packet | to A2 |
| o4_nohi (X1) | -0.66% (0.70%) [-1.87%, +0.55%] | +1.29% (1.42%) [-1.30%, +3.89%] | -2.33% (0.98%) [-4.13%, -0.53%] | -0.87% (0.53%) [-1.76%, +0.02%] | to A2 |
| o4_nocp (X2) | same bytes | +1.49% (1.81%) [-1.83%, +4.80%] | -1.19% (1.07%) [-3.14%, +0.77%] | -0.50% (0.92%) [-2.11%, +1.11%] | to A2 |
| o4_noregions (X3) | +0.46% (0.73%) [-0.81%, +1.73%] | -0.19% (1.64%) [-3.20%, +2.83%] | -2.46% (0.96%) [-4.21%, -0.71%] | -0.57% (0.55%) [-1.50%, +0.36%] | to A2 |
| o4_nospec (X4) | same bytes | +1.80% (1.38%) [-0.73%, +4.33%] | -1.25% (1.25%) [-3.54%, +1.04%] | +0.12% (0.93%) [-1.49%, +1.72%] | to A2 |
| fl3b3 (C1) | +0.98% (0.96%) [-0.68%, +2.63%] | +1.56% (1.66%) [-1.49%, +4.61%] | +0.05% (1.14%) [-2.04%, +2.14%] | +0.75% (0.67%) [-0.38%, +1.89%] | to A2 |
| fl3b3p (C1p) | -0.61% (0.85%) [-2.07%, +0.86%] | +0.70% (1.86%) [-2.71%, +4.12%] | -3.00% (2.09%) [-6.83%, +0.83%] | -0.70% (0.72%) [-1.92%, +0.53%] | not to A2 (d ≤ 0); screen is its best-check |
| b3l (C2) | -1.64% (0.89%) [-3.17%, -0.10%] | -2.94% (2.06%) [-6.72%, +0.84%] | -3.39% (1.23%) [-5.64%, -1.14%] | -2.32% (0.68%) [-3.47%, -1.17%] | not to A2; slower than O4 |
| s6 (anchor) | -14.20% (0.99%) [-15.92%, -12.49%] | -14.82% (1.45%) [-17.48%, -12.16%] | -21.94% (1.25%) [-24.23%, -19.65%] | -16.67% (0.68%) [-17.82%, -15.51%] | slower |
| s6b3 (anchor) | +0.04% (0.72%) [-1.22%, +1.29%] | alias of o4_noregions | alias of o4_noregions | -0.80% (0.54%) [-1.72%, +0.12%] | not shown |
| p7 (anchor) | -4.27% (0.62%) [-5.34%, -3.21%] | -4.54% (1.88%) [-7.99%, -1.09%] | -5.11% (0.80%) [-6.58%, -3.63%] | -4.58% (0.47%) [-5.38%, -3.78%] | slower |
| c7b3 (anchor) | same bytes | same bytes | same bytes | – | same bytes as O4-r1 |
| fl3 (anchor) | -7.83% (1.02%) [-9.60%, -6.06%] | -17.05% (1.09%) [-19.04%, -15.06%] | -18.60% (0.93%) [-20.30%, -16.91%] | -14.70% (0.58%) [-15.68%, -13.71%] | slower |

**Verdicts.**

- **BEST holds** (family 56). The smallest p is o4_min2 pooled, 0.0281 against a Holm
  threshold of 0.000893.
- **To A2 (10):** X1, X2, X3, X4, A1, I1, A3, A7, A8, C1. A4 is excluded. C1p and C2 stop at
  the screen. No removal was rejected (closest: X3 cli UB99 +0.24%, X1 cli UB99 +0.44%). No
  item had o4's bytes on all three workloads.
- **A7 survived** (pooled LB +0.19%), and C1p's pooled d was -0.70%. This led to P4b (§5.3).
- **Notes** (decide nothing): the uart driver's own A/A flags `harness_bias` (0.9907x
  [0.9491, 0.9993]) while decide.py's VALID holds; the uart A/A block log-ratio SD is 6.22%
  (packet 4.68%, cli 2.78%). cli's burn-in block was disturbed (17 of 22 runs above 1.3x their
  median, A/A ratio 0.6830), because all of cli's timed blocks ran while packet and uart were
  still staging.

### 5.2 Campaign H: heavy arms (attempt 1 failed)

Source: `H-SUMMARY.md`, `H.json`, `hygiene/H/` (attempt 1 in `hygiene/H/attempt1/`).

**Attempt 1** (02:18:13-02:25:06) failed on uart at block 35 of 81. The o4_pwhopr `help` run
exited 1 before running firmware: `qemu-system-arm: -accel
tcg,supercache=/data/scfix-o4/uart/timing-H/o4_pwhopr/cache.sc: allocate supercache code
buffer: File exists`. QEMU's `map_cache()` maps a reused cache's 2 GiB code buffer at the
header's `code_address` with `MAP_FIXED_NOREPLACE`, and mmap returned EEXIST: another mapping
already sat inside [0x7fff6c000000, 0x7fffec000000). Every H cache has that address, so the
failure is not specific to o4_pwhopr. Packet and cli had completed. The rule, fixed before any
H estimate: a driver crash voids the attempt for all three workloads, and the campaign reruns
whole with the same plans and seed. Attempt 1 is kept as `<w>/timing-H.attempt1`.

**Attempt 2** (02:27:46-02:41:05, decided): 41 / 81 / 41 blocks, seed 20261128; arms o4,
o4-aa, o4_pwhopr (A5), o4_hostall (A6), w7bd_b3.

**VALID** (all valid): packet -0.48% [-2.01%, +1.37%] (SE 0.62%); uart +0.35% [-1.11%, +1.86%]
(0.53%); cli -0.40% [-2.10%, +1.24%] (0.64%).

| arm (item) | packet | uart | cli | pooled | verdict |
|---|---|---|---|---|---|
| o4_pwhopr (A5) | -0.11% (0.75%) [-1.36%, +1.15%] | +0.47% (0.44%) [-0.25%, +1.20%] | **+1.97% (0.49%) [+1.14%, +2.80%]**, p 0.0001, Holm 1 / 0.004167: faster | **+0.93% (0.30%) [+0.44%, +1.42%]**, p 0.0011, Holm 2 / 0.004545: faster | **BEST fails: A5 is faster than O4-r1** |
| o4_hostall (A6) | -4.05% (0.65%) [-5.14%, -2.96%] | -12.45% (0.68%) [-13.58%, -11.32%] | -0.32% (0.51%) [-1.18%, +0.53%] | -4.51% (0.34%) [-5.08%, -3.94%] | excluded: LB_w < log 0.98 on packet and uart; slower |
| w7bd_b3 | -3.14% (0.69%) [-4.31%, -1.97%] | -10.27% (0.70%) [-11.43%, -9.10%] | +0.50% (0.56%) [-0.45%, +1.45%] | -3.57% (0.37%) [-4.18%, -2.95%] | not faster; slower on packet, uart, pooled |

- The step-down stops at rank 3 (o4_pwhopr uart, p 0.141 > 0.0050).
- **Replication** (not decision inputs): attempt 1 gives o4_pwhopr cli +1.84% (SE 0.52%, p
  0.00053), packet +0.41% (SE 0.60%), packet + cli pooled +1.22%; uart blocks 0-34, read
  `--unregistered`, +1.03% (SE 0.59%). w7bd_b3 packet moved from -0.96% (attempt 1) to -3.14%
  (attempt 2), about 2 SE.
- Consequence (§3.7 BEST): O4 must absorb A5, which replaces hot-loop selection and all its
  knobs. X3 can no longer apply.

### 5.3 P4b: hot-loop-tasks (A2a)

Source: `{packet,uart,cli}/VERIFY.md` "P4b"; `p4b/`.

fd-int `b35cdb6` added the firmware-inline mode `hot-loop-tasks`: every hot-loop root with its
plan and price unchanged, plus the FirmLooper v3 task-loop roots the profile did not nominate,
priced after the hot roots (`price_after`). The commit says it landed because A7 survived the
screen (see §7.5 on the trigger).

- **Byte spot check.** O4-r1 rebuilt on uart at `b35cdb6` equals `ae360ff7…`. Its first
  attempt failed with the helper-PGO gdb flake (§8.4); the retry passed.
- **o4_tasks** differs from o4 only on packet: one added region, root 0x48ec (memset, 7
  members, 14 calls, `net_ns` 1,621.3). The other four task-loop-only roots were refused by
  v3's RTOS boundary. All 48 o4 roots are embedded unchanged (largest `net_ns` difference
  0.0). On uart the one task-loop-only root (0x8235a, 1 call) is refused `not_profitable`; on
  cli both task-loop roots are also hot roots. So uart and cli have o4's bytes.
- G0, validity and the regions replay pass on all three workloads. o4_tasks joined A2 on
  packet.

### 5.4 Campaign A2: head-to-heads (combined mode)

Source: `A2-SUMMARY.md`, `A2.json`, `hygiene/A2/`. 03:17-03:41; 41 / 81 / 41 blocks; seed
20261130; one combined plan per workload (§7.3). decide.py ran over A2 and H together, so the
ADD family is A2's add-ons plus A6.

**VALID** (all valid): packet +0.13% [-1.59%, +1.88%] (SE 0.66%); uart -1.06% [-2.40%, +0.17%]
(0.48%); cli +0.57% [-0.93%, +1.90%] (0.59%).

| arm (item, rule) | packet | uart | cli | pooled | p | verdict |
|---|---|---|---|---|---:|---|
| o4_nohi (X1, REMOVE) | +0.62% (0.62%) [-0.42%, +1.67%] | +0.03% (0.45%) [-0.72%, +0.78%] | +0.23% (0.72%) [-0.97%, +1.44%] | +0.24% (0.32%) [-0.30%, +0.77%] | 0.2351 | accepted: removable |
| o4_nocp (X2, REMOVE) | same bytes | +0.51% (0.44%) [-0.23%, +1.25%] | +0.67% (0.52%) [-0.20%, +1.54%] | +0.58% (0.34%) [+0.02%, +1.14%] | 0.0444 | accepted, moot under X4 |
| o4_noregions (X3, REMOVE) | +0.67% (0.64%) [-0.41%, +1.75%] | +0.43% (0.40%) [-0.24%, +1.10%] | **-1.65% (0.51%) [-2.50%, -0.80%]** | -0.18% (0.28%) [-0.64%, +0.29%] | 0.7322 | **rejected**: cli LB_w < log 0.98; regions stay |
| o4_nospec (X4, REMOVE) | same bytes | -0.01% (0.44%) [-0.74%, +0.72%] | +0.80% (0.53%) [-0.09%, +1.69%] | +0.32% (0.34%) [-0.24%, +0.88%] | 0.1692 | accepted: removable |
| o4_chained (A1, ADD) | **+1.82% (0.72%) [+0.61%, +3.03%]** | **-0.99% (0.49%) [-1.81%, -0.17%]** | same bytes | -0.09% (0.41%) [-0.77%, +0.58%] | 0.5899 | excluded (Holm 5 / 0.025) |
| o4_isb (I1, FIDELITY) | +0.41% (0.63%) [-0.65%, +1.48%] | +0.11% (0.39%) [-0.54%, +0.76%] | same bytes | +0.19% (0.33%) [-0.36%, +0.75%] | 0.2802 | included (FIDELITY) |
| o4_rank (A3, ADD) | **+1.51% (0.61%) [+0.48%, +2.54%]** | same bytes | same bytes | = packet | 0.0088 | excluded: Holm 1, p 0.00883 > 0.00833 |
| o4_min2 (A7, ADD) | +0.29% (0.67%) [-0.84%, +1.42%] | -0.56% (0.50%) [-1.38%, +0.27%] | +0.78% (0.56%) [-0.16%, +1.72%] | +0.10% (0.32%) [-0.44%, +0.63%] | 0.3826 | excluded (Holm 4 / 0.016667) |
| o4_legacy (A8, ADD) | +0.37% (0.58%) [-0.60%, +1.35%] | same bytes | same bytes | = packet | 0.2604 | excluded (Holm 3 / 0.0125) |
| o4_tasks (A2a, ADD) | +0.98% (0.76%) [-0.30%, +2.25%] | same bytes | same bytes | = packet | 0.1016 | excluded (Holm 2 / 0.0100) |
| o4_hostall (A6, ADD, from H) | -4.05% (0.65%) [-5.14%, -2.96%] | -12.45% (0.68%) [-13.58%, -11.32%] | -0.32% (0.51%) [-1.18%, +0.53%] | -4.51% (0.34%) [-5.08%, -3.94%] | 1.0000 | excluded (Holm 6; non-inferiority fails) |
| fl3b3 (C1, BEST) | +2.03% (0.67%) [+0.90%, +3.16%], p 0.0022 | -0.12% (0.47%) [-0.91%, +0.67%] | -0.32% (0.44%) [-1.07%, +0.42%] | +0.20% (0.29%) [-0.28%, +0.68%] | 0.2482 | not faster (packet misses Holm rank 1, 0.001613) |

**Verdicts.**

- **No add-on is accepted.** The Holm step-down (family 6) fails at rank 1: A3 has p 0.00883
  against 0.05 / 6 = 0.00833. A7's screen signal (+1.31%) did not replicate (+0.10%).
- **REMOVE accepted** for X1, X2 and X4; X2 is moot because X4 removes the whole SPEC bundle.
  **X3 rejected**: without regions cli is slower (the screen showed the same direction, -2.46%).
- **FIDELITY**: I1 included (not moot, since X3 was rejected).
- **BEST holds in A2** (family 31). It still fails in H (A5).
- **Registered composition**: O4-r2 = O4-r1 + I1 + X1 + X4 (no add-on). decide.py also lists
  o4_pwhopr for absorption. B2's "rejected candidate with the highest pooled d" is o4_rank.
- **Notes**: the uart driver's own A/A flags `harness_bias` again (0.9911x [0.9803, 0.9994])
  while VALID holds. cli's burn-in block had 8 of 16 runs above 1.3x (A/A ratio 1.5487).
  On packet two timed runs were above 1.3x their median: o4_tasks in block 18 (1.573x) and
  o4_rank in block 36 (1.387x).

### 5.5 P5: decisions and the O4-r2 build

Source: `decisions.json`, `p5/P5.decide.json`, `p5/`, VERIFY.md "P5".

- decide.py over A, A2 and H (`p5/P5.decide.json`) reproduces the verdicts above. Its
  registered composition is I1 + X1 + X4 with `contains_addon: false`.
- **O4-r2 = O4-r1 + A5 (absorbed under BEST) + I1 + X1 + X4.** Passes: chain-returns,
  chain-static and firmware-inline `{mode exhaustive, cost_model profile, phase steady,
  isb_boundary true, ...}`. The A5 absorption and its composition with I1 were the lead's
  call (§7.6).
- `--pipeline O4-r2.toml` was refused by scopt's O4 preflight (a pipeline named O4-rN must be
  a packaged revision), so the arm was built from the copy `o4r2.toml`, which differs only in
  name and description (§7.7).
- Build (03:53-03:54): rc 0 on all three, 62.0 / 40.8 / 51.5 s. Regions 53 / 27 / 14, as
  o4_pwhopr. On packet I1 makes 36 isb_boundary exits in 17 regions and changes the members of
  14 roots against o4_pwhopr (`p5/packet-o4r2-isb-vs-pwhopr.json`). G0 and validity pass. Its
  bytes differ from every other arm.

### 5.6 Campaign B1: O4-r2 against O4-r1 (failed)

Source: `B-SUMMARY.md`, `B1.json`, `p6/B1-rule.md`. 04:11-04:19; 41 / 81 / 41; seed 20261127.
Rule: ADD-confirm (§7.8).

**VALID**: packet +0.79% [-1.47%, +3.06%] (SE 0.87%); uart +0.91% [-0.33%, +2.17%] (0.51%);
cli -0.25% [-1.61%, +1.21%] (0.53%).

| arm | packet | uart | cli | pooled | verdict |
|---|---|---|---|---|---|
| o4r2 (O4-r2) | **-1.21% (0.98%) [-2.87%, +0.44%]** | -0.15% (0.51%) [-1.00%, +0.70%] | +1.34% (0.45%) [+0.58%, +2.10%], p 0.0025: faster (Holm 1 / 0.0125) | +0.49% (0.32%) [-0.05%, +1.02%], p 0.0660 | **fail**: pooled LB ≤ 0; packet LB < log 0.98 |

REMOVE would have failed too, on packet. decide.py's next step: drop o4_pwhopr (the only
add-on) and repeat B1 once. Note that B1's own BEST family (4) finds O4-r2 faster than O4-r1
on cli.

### 5.7 Campaign B1-retry: O4-r2b against O4-r1 (passed)

Source: `B-SUMMARY.md`, `B1-retry.json`. O4-r2b = I1 + X1 + X4 (O4-r2 without A5), built
04:22-04:23. 04:32-04:40; 41 / 81 / 41; seed 20261127. In this run the arm named `o4r2` is
O4-r2b (§7.9). Rule: REMOVE (no add-on left).

**VALID**: packet +1.13% [-1.53%, +4.00%] (SE 1.06%); uart +0.59% [-0.74%, +1.97%] (0.50%);
cli -0.24% [-1.64%, +1.15%] (0.55%).

| arm | packet | uart | cli | pooled | verdict |
|---|---|---|---|---|---|
| O4-r2b | +0.85% (0.86%) [-0.60%, +2.29%] | -0.10% (0.50%) [-0.93%, +0.73%] | +0.22% (0.42%) [-0.49%, +0.93%] | +0.18% (0.30%) [-0.32%, +0.68%], p 0.2725 | **pass** (UB ≥ 0, LB > log 0.99, LB_w > log 0.98) |

### 5.8 Campaign B2: best-check against O4-r2b (passed)

Source: `B-SUMMARY.md`, `B2.json`, `B.json`. 04:42-04:50; 21 / 11 / 11; seed 20261129;
reference O4-r2b (arm `o4r2`) with `o4r2-aa`; O4-r2 timed as `o4r2a`. d > 0: the arm is
faster than O4-r2b.

**VALID**: packet +1.20% [-1.11%, +3.71%] (SE 0.91%); uart -1.71% [-6.03%, +2.85%] (1.76%);
cli +0.90% [-3.48%, +3.88%] (1.38%).

| arm (item) | packet | uart | cli | pooled | p, Holm rank / threshold |
|---|---|---|---|---|---|
| o4_pwhopr (A5) | +2.06% (0.77%) [+0.74%, +3.39%] | +2.11% (1.72%) [-1.04%, +5.27%] | +0.68% (0.92%) [-1.00%, +2.36%] | +1.56% (0.56%) [+0.62%, +2.50%] | 0.0043, 1 / 0.001282 |
| o4 (O4-r1; c7b3 is its alias) | +0.92% (1.06%) [-0.92%, +2.76%] | +1.63% (1.16%) [-0.50%, +3.76%] | +1.38% (0.96%) [-0.38%, +3.14%] | +1.30% (0.61%) [+0.27%, +2.33%] | 0.0204, 4 / 0.001389 |
| o4_rank (A3) | +0.68% (1.06%) [-1.15%, +2.50%] | alias of o4 | alias of o4 | +1.22% (0.61%) [+0.19%, +2.24%] | 0.0269, 5 / 0.001429 |
| o4r2a (O4-r2) | -0.66% (0.96%) [-2.31%, +1.00%] | +2.85% (1.01%) [+0.99%, +4.70%] | +1.46% (1.19%) [-0.73%, +3.65%] | +1.12% (0.60%) [+0.10%, +2.13%] | 0.0361, 6 / 0.001471 |
| o4_nospec (X4) | alias of o4 | +1.89% (0.96%) [+0.13%, +3.65%] | +0.18% (0.99%) [-1.64%, +2.00%] | +1.02% (0.58%) [+0.04%, +2.00%] | 0.0440, 9 / 0.001613 |
| o4_nocp (X2) | alias of o4 | +2.71% (1.77%) [-0.54%, +5.95%] | -0.43% (1.29%) [-2.79%, +1.94%] | +0.79% (0.75%) [-0.47%, +2.05%] | 0.1489, 16 / 0.002083 |
| o4_isb (I1) | +0.10% (0.87%) [-1.42%, +1.61%] | +1.39% (2.55%) [-3.29%, +6.06%] | alias of o4 | +0.72% (0.63%) [-0.35%, +1.79%] | 0.1304, 15 / 0.002000 |
| o4_nohi (X1) | +0.49% (0.90%) [-1.07%, +2.04%] | +2.28% (1.15%) [+0.17%, +4.39%] | -1.22% (1.05%) [-3.14%, +0.70%] | +0.42% (0.59%) [-0.57%, +1.41%] | 0.2414, 20 / 0.002500 |
| s6b3 | +0.64% (0.64%) [-0.47%, +1.76%] | +0.29% (1.21%) [-1.92%, +2.50%] | -2.22% (0.92%) [-3.92%, -0.53%] | -0.20% (0.48%) [-1.02%, +0.62%] | 0.6585, 28 / 0.004167 |
| p7 | -3.69% (0.75%) [-4.98%, -2.39%] | -7.00% (1.51%) [-9.77%, -4.22%] | -7.69% (0.62%) [-8.82%, -6.55%] | -6.14% (0.46%) [-6.92%, -5.36%] | 1.0000 |
| fl3 | -6.35% (1.43%) [-8.83%, -3.87%] | -13.98% (1.36%) [-16.49%, -11.48%] | -16.81% (1.20%) [-19.02%, -14.61%] | -12.96% (0.76%) [-14.26%, -11.66%] | 1.0000 |

**Verdict.** BEST holds (family 39, no rejection), although the smallest p values all point
against O4-r2b (o4_pwhopr pooled, o4_pwhopr packet, O4-r2 uart, O4-r1 pooled). decide.py's
`adopt_o4r2` over B1-retry + B2 (`B.json`) is true; it does not see H2.

### 5.9 Campaign H2: campaign H against O4-r2b (failed)

Source: `B-SUMMARY.md`, `H2.json`. 04:50-05:03; 41 / 81 / 41; seed 20261228; reference O4-r2b.
Run because O4-r2b changes firmware-inline (I1) and host-inline (X1).

**VALID**: packet +1.16% [-0.64%, +3.36%] (SE 0.80%); uart +0.83% [-0.37%, +1.94%] (0.43%); cli
+0.22% [-1.46%, +1.85%] (0.62%).

| arm (item) | packet | uart | cli | pooled | verdict |
|---|---|---|---|---|---|
| o4_pwhopr (A5) | +0.34% (0.75%) [-0.92%, +1.59%] | +0.92% (0.46%) [+0.16%, +1.68%], p 0.024 | **+1.43% (0.50%) [+0.59%, +2.27%]**, p 0.0033, Holm 2 / 0.004545: faster | **+1.01% (0.31%) [+0.51%, +1.52%]**, p 0.0006, Holm 1 / 0.004167: faster | **BEST fails** |
| o4_hostall (A6) | -3.74% (0.65%) [-4.85%, -2.64%] | -11.59% (0.68%) [-12.71%, -10.46%] | -1.25% (0.55%) [-2.17%, -0.32%] | -4.88% (0.36%) [-5.48%, -4.29%] | slower |
| w7bd_b3 | -1.27% (0.68%) [-2.42%, -0.12%] | -9.53% (0.75%) [-10.78%, -8.28%] | +0.27% (0.47%) [-0.52%, +1.06%] | -2.17% (0.34%) [-2.74%, -1.60%] | slower |

**Outcome of campaign B** (05:07): the step's adoption rule required B1, B2 and H2 to pass, so
O4-r2b was not adopted and **O4 stayed at O4-r1**, with BEST failing against it (A5 in H, O4-r2
on cli in B1). The registered B1 retry was used up, so the next step was the lead's call. The
summary named O4-r1 + A5 as the evident next candidate: on H's data it would already meet
ADD-confirm (pooled LB +0.44%; LB_w packet -1.36%, uart -0.25%, cli +1.14%).

### 5.10 Campaign R3: four compositions against O4-r1

Source: `R3-SUMMARY.md`, `R3.json`, `driver/PREREG-R3.md`, `r3/R3-direct-pairs.json`. 05:55-06:05;
41 / 81 / 41; seed 20261201. Arms: o4, o4-aa, o4_pwhopr (= o4r3a), o4r3b, o4r3c, o4r3d on packet;
on uart and cli o4r3c is an alias of o4_pwhopr and o4r3d of o4r3b (A3 changes bytes only on
packet).

**VALID**: packet -0.41% [-2.18%, +1.59%] (SE 0.74%); uart +0.68% [-0.77%, +2.06%] (0.55%);
cli -0.35% [-1.80%, +1.09%] (0.57%). No rerun; final.

| variant (parts) | packet | uart | cli | pooled | p | Holm rank / threshold | ADD-confirm |
|---|---|---|---|---|---:|---|---|
| o4r3a = o4_pwhopr (A5) | -0.36% (0.88%) [-1.84%, +1.11%] | +0.43% (0.43%) [-0.28%, +1.14%] | +1.74% (0.41%) [+1.06%, +2.43%] | +0.97% (0.28%) [+0.50%, +1.43%] | 0.00036 | 2 / 0.01667 | pass |
| o4r3b (A5 + X1 + X4) | -0.63% (0.72%) [-1.85%, +0.58%] | +0.70% (0.48%) [-0.10%, +1.50%] | +1.31% (0.50%) [+0.46%, +2.15%] | +0.69% (0.31%) [+0.17%, +1.20%] | 0.01512 | 4 / 0.05 | pass |
| **o4r3c (A5 + A3)** | +0.76% (0.67%) [-0.38%, +1.90%] | alias of o4_pwhopr: +0.43% | alias of o4_pwhopr: +1.74% | **+1.06% (0.27%) [+0.61%, +1.51%]** | 0.00007 | 1 / 0.0125 | pass |
| o4r3d (A5 + A3 + X1 + X4) | -0.03% (0.65%) [-1.14%, +1.07%] | alias of o4r3b: +0.70% | alias of o4r3b: +1.31% | +0.76% (0.31%) [+0.26%, +1.27%] | 0.00692 | 3 / 0.025 | pass |

**Choice** (registered rule): best pooled d is o4r3c's, +1.0599%. The band d ≥ +0.7599%
holds o4r3a (+0.9668%), o4r3c and o4r3d (+0.7641%). Preferring X1 + X4 leaves o4r3d.
**Registered winner: o4r3d.** It made the band by 0.0042 percentage points of d; slightly
lower and the winner would have been o4r3c.

**Direct pairs** (descriptive, not part of the rule; `r3/R3-direct-pairs.json`):

| pair | packet | uart | cli | pooled [one-sided 95%], p |
|---|---:|---:|---:|---|
| o4r3c vs o4r3d (X1 + X4 on top of A5 + A3) | +0.88% (SE 0.72%) | -0.70% (SE 0.52%) | +0.63% (SE 0.58%) | +0.11% [-0.45%, +0.67%], p 0.37 |
| o4r3a vs o4r3b (X1 + X4 on top of A5) | +0.23% (SE 0.89%) | -0.70% | +0.63% | -0.05% [-0.64%, +0.53%], p 0.56 |
| o4r3c vs o4r3a (A3 on top of A5; packet only) | +0.97% (SE 0.81%) | same bytes | same bytes | +0.97% [-0.39%, +2.33%], p 0.12 |
| o4r3d vs o4r3b (A3 on top of A5 + X1 + X4) | +0.36% (SE 0.99%) | same bytes | same bytes | +0.36% [-1.31%, +2.03%], p 0.36 |
| o4r3a vs o4r3d | -0.54% | -0.70% | +0.63% | -0.18% [-0.76%, +0.40%], p 0.69 |

R3 reproduces H's A5 numbers (H -0.11 / +0.47 / +1.97, pooled +0.93%; R3 -0.36 / +0.43 / +1.74,
pooled +0.97%).

### 5.11 Campaign F: best-check against the R3 winner

Source: `F-SUMMARY.md`, `F.json`, `F-most.json`, `driver/AMENDMENT-F-most-cases.md`,
`r3/runinfo-F.json`. Timed 06:12-06:28, decided 06:30; 31 / 21 / 21; seed 20261202; reference
o4r3d with o4r3d-aa. Timed arms 19 / 15 / 12; the registered set is 21 arms
(`decide_r3.f_arms("o4r3d")`). Dropped as same bytes as o4r3d: o4r3b on uart and cli, O4-r2 on
cli. Aliases: c7b3 → o4 everywhere; o4_nocp and o4_nospec → o4 on packet; o4_legacy, o4_rank,
o4_tasks → o4 on uart and cli; o4r3c → o4_pwhopr on uart and cli; s6b3 → o4_noregions on uart and
cli; o4_chained and o4_isb → o4 on cli.

**VALID**: packet -1.45% [-2.98%, +0.27%] (SE 0.62%); uart +0.35% [-2.59%, +3.43%] (1.19%);
cli +0.88% [-1.81%, +3.35%] (1.02%). No rerun.

#### 5.11.1 Every arm against o4r3d

d > 0: the arm is faster than o4r3d. "slower" marks UB < 0.

| arm (item) | packet | uart | cli | pooled | p |
|---|---|---|---|---|---:|
| o4r2 (O4-r2) | -0.15% (0.78%) [-1.47%, +1.17%] | +0.41% (0.84%) [-1.04%, +1.86%] | same bytes | +0.11% (0.57%) [-0.85%, +1.06%] | 0.4266 |
| o4r3c (A5 + A3) | +0.06% (0.71%) [-1.15%, +1.26%] | alias of o4_pwhopr | alias of o4_pwhopr | +0.10% (0.42%) [-0.60%, +0.80%] | 0.4051 |
| o4_pwhopr (A5) | -0.71% (0.68%) [-1.86%, +0.44%] | -0.13% (0.90%) [-1.68%, +1.42%] | +0.25% (0.63%) [-0.84%, +1.34%] | -0.18% (0.41%) [-0.87%, +0.50%] | 0.6708 |
| o4_rank (A3) | +0.44% (0.74%) [-0.82%, +1.69%] | alias of o4 | alias of o4 (slower) | -0.33% (0.44%) [-1.07%, +0.40%] | 0.7740 |
| o4_tasks (A2a) | -0.28% (0.67%) [-1.42%, +0.86%] | alias of o4 | alias of o4 (slower) | -0.56% (0.42%) [-1.27%, +0.14%] | 0.9065 |
| o4_legacy (A8) | -0.41% (0.52%) [-1.30%, +0.48%] | alias of o4 | alias of o4 (slower) | -0.57% (0.38%) [-1.21%, +0.06%] | 0.9335 |
| o4_nohi (X1) | -0.95% (0.73%) [-2.19%, +0.29%] | +0.22% (0.66%) [-0.92%, +1.37%] | -1.59% (0.78%) [-2.93%, -0.24%] slower | -0.67% (0.41%) [-1.36%, +0.02%] | 0.9446 |
| o4_min2 (A7) | -1.10% (0.59%) [-2.10%, -0.09%] slower | -1.08% (0.91%) [-2.65%, +0.49%] | +0.48% (0.87%) [-1.03%, +1.99%] | -0.71% (0.43%) [-1.43%, +0.01%] | 0.9468 |
| o4r2b (O4-r2b) | +0.47% (0.66%) [-0.64%, +1.59%] | -1.08% (0.82%) [-2.50%, +0.33%] | -1.80% (0.69%) [-3.00%, -0.61%] slower | -0.72% (0.41%) [-1.41%, -0.04%] slower | 0.9583 |
| o4r3b (A5 + X1 + X4) | -0.75% (0.73%) [-1.98%, +0.49%] | same bytes | same bytes | = packet | 0.8443 |
| fl3b3 (C1) | +0.10% (0.71%) [-1.10%, +1.31%] | -1.29% (1.01%) [-3.05%, +0.46%] | -1.26% (0.62%) [-2.33%, -0.19%] slower | -0.78% (0.42%) [-1.49%, -0.07%] slower | 0.9644 |
| fl3b3p (C1p) | -0.10% (0.92%) [-1.66%, +1.46%] | +0.57% (1.01%) [-1.18%, +2.31%] | -2.30% (0.81%) [-3.70%, -0.90%] slower | -0.83% (0.52%) [-1.70%, +0.04%] | 0.9426 |
| o4 (O4-r1; c7b3 is its alias) | -1.55% (0.78%) [-2.87%, -0.22%] slower | +0.73% (1.20%) [-1.33%, +2.80%] | -1.14% (0.61%) [-2.20%, -0.08%] slower | -1.01% (0.45%) [-1.76%, -0.26%] slower | 0.9861 |
| o4_chained (A1) | +1.54% (0.85%) [+0.09%, +2.98%], p 0.0404 | -2.38% (0.61%) [-3.43%, -1.33%] slower | alias of o4 (slower) | -1.09% (0.38%) [-1.73%, -0.45%] slower | 0.9969 |
| o4_nocp (X2) | alias of o4 (slower) | -0.80% (0.61%) [-1.86%, +0.26%] | -1.23% (0.61%) [-2.28%, -0.17%] slower | -1.14% (0.38%) [-1.77%, -0.51%] slower | 0.9981 |
| o4_nospec (X4) | alias of o4 (slower) | -0.79% (0.80%) [-2.17%, +0.58%] | -1.15% (0.69%) [-2.34%, +0.05%] | -1.16% (0.43%) [-1.89%, -0.44%] slower | 0.9953 |
| o4_isb (I1) | -1.84% (0.61%) [-2.88%, -0.79%] slower | -0.03% (0.83%) [-1.47%, +1.41%] | alias of o4 (slower) | -1.18% (0.38%) [-1.82%, -0.53%] slower | 0.9984 |
| s6b3 | -1.05% (0.57%) [-2.01%, -0.09%] slower | alias of o4_noregions | alias of o4_noregions (slower) | -1.28% (0.39%) [-1.94%, -0.62%] slower | 0.9991 |
| o4_noregions (X3) | -1.28% (0.51%) [-2.15%, -0.42%] slower | -0.38% (0.71%) [-1.61%, +0.85%] | -3.14% (0.87%) [-4.64%, -1.64%] slower | -1.38% (0.37%) [-2.00%, -0.75%] slower | 0.9998 |

#### 5.11.2 Verdicts

| family | hypotheses | rank 1 | d | p | threshold | rejected |
|---|---:|---|---:|---:|---:|---|
| strict BEST (PREREG-R3) | 58 | o4_chained packet | +1.54% | 0.0404 | 0.00086 | none |
| BEST-most, pooled | 19 | o4r3c pooled | +0.10% | 0.4051 | 0.00263 | none |
| BEST-most, per workload | 40 | o4_chained packet | +1.54% | 0.0404 | 0.00125 | none |

- Both rules hold, there is no workload-specific win, and the verdict is the same under both
  readings of "majority" for a one-workload arm. Under the registered rules **o4r3d was
  adopted** (`F.json` and `F-most.json` adoption true).
- o4r3c and o4r3d are not distinguishable: +0.10% [-0.60%, +0.80%] (the R3 direct pair gave
  +0.11% [-0.45%, +0.67%]).
- F independently confirms that o4r3d is faster than O4-r1 (O4-r1 -1.01% pooled, shown slower).
- Near miss: A1 on packet, +1.54% [+0.09%, +2.98%], unadjusted p 0.040, not rejected; slower on
  uart and pooled.
- The packet A/A gap (-1.45%) means the averaged reference sits about 0.7 points slower than
  o4r3d alone on packet; it is inside VALID's tolerance.

#### 5.11.3 Direct pairs in F (descriptive, not part of any rule)

Method: `r3/direct_pairs_r3.py`'s method on `*/timing-F`; Y is the arm's own timing, not the
averaged reference. These numbers are in `F-SUMMARY.md`, not in `F.json`.

| pair | packet | uart | cli | pooled [one-sided 95%], p |
|---|---:|---:|---:|---|
| o4r3c vs o4 (O4-r1) | +1.73% (SE 0.94%) | -0.56% (SE 1.17%) | +1.39% (SE 0.73%) | +1.11% [+0.25%, +1.98%], p 0.017 |
| o4r3d vs o4 (O4-r1) | +2.08% (SE 0.85%) | -0.70% (SE 1.38%) | +0.79% (SE 0.64%) | +1.02% [+0.22%, +1.82%], p 0.019 |
| o4r3c vs o4r3d | -0.65% (SE 0.70%) | +0.11% (SE 1.09%) | +0.95% (SE 0.85%) | +0.01% [-0.79%, +0.82%], p 0.49 |
| o4r3c vs o4_pwhopr (A3 on packet) | +0.90% (SE 0.72%) | same bytes | same bytes | +0.90% [-0.33%, +2.13%], p 0.11 |

#### 5.11.4 F re-read with O4-r3 (o4r3c) as the reference (post hoc)

Source: `evidence/r3/adapt_f.py`, `evidence/r3/decide.json`, `evidence/r3/decide.stdout.txt`;
these rows are the O4-r3 rows of `docs/o4-evidence.json` (campaign id `F-r3`).

`adapt_f.py` renames o4_pwhopr's uart and cli runs to o4r3c (same bytes) and uses o4r3c as both
reference and A/A arm, because F has no o4r3c A/A copy. So **VALID is vacuous** (a_w = 0). Block
0 stays burn-in. decide.py registration 2 ran with `--unregistered`. F's alias workloads are not
re-read, so those cells are "not timed". d > 0: the arm is faster than O4-r3.

| arm | packet | uart | cli | pooled |
|---|---|---|---|---|
| o4_rank | +0.54% (0.68%) [-0.62%, +1.70%] | not timed | not timed | = packet |
| o4_chained | +1.78% (0.76%) [+0.49%, +3.07%] | -2.11% (1.05%) [-3.93%, -0.29%] slower | not timed | +0.45% (0.62%) [-0.58%, +1.49%] |
| o4r3d | +0.65% (0.69%) [-0.53%, +1.83%] | -0.11% (1.11%) [-2.02%, +1.80%] | -0.95% (0.86%) [-2.43%, +0.53%] | -0.01% (0.48%) [-0.82%, +0.80%] |
| o4r3d-aa | -0.65% (0.86%) [-2.11%, +0.81%] | +0.86% (1.14%) [-1.11%, +2.84%] | +0.02% (0.75%) [-1.29%, +1.32%] | -0.05% (0.51%) [-0.90%, +0.80%] |
| o4_tasks | -0.17% (0.91%) [-1.72%, +1.37%] | not timed | not timed | = packet |
| o4r2b | +0.46% (0.66%) [-0.65%, +1.58%] | -0.72% (1.13%) [-2.68%, +1.23%] | -1.75% (0.81%) [-3.16%, -0.35%] slower | -0.47% (0.47%) [-1.25%, +0.31%] |
| o4r2 | -0.66% (0.72%) [-1.88%, +0.57%] | -0.29% (1.26%) [-2.47%, +1.89%] | not timed | -0.56% (0.63%) [-1.62%, +0.49%] |
| o4_legacy | -0.60% (0.67%) [-1.75%, +0.54%] | not timed | not timed | = packet |
| o4_nohi | -0.73% (0.49%) [-1.56%, +0.10%] | +0.44% (0.74%) [-0.85%, +1.73%] | -1.56% (0.78%) [-2.91%, -0.21%] slower | -0.63% (0.36%) [-1.24%, -0.03%] slower |
| o4_min2 | -0.96% (0.69%) [-2.13%, +0.21%] | -1.14% (1.29%) [-3.38%, +1.10%] | +0.18% (1.11%) [-1.74%, +2.11%] | -0.73% (0.53%) [-1.62%, +0.16%] |
| o4_pwhopr | -0.90% (0.72%) [-2.13%, +0.33%] | same bytes | same bytes | = packet |
| o4r3b | -0.95% (0.74%) [-2.21%, +0.31%] | not timed | not timed | = packet |
| s6b3 | -0.97% (0.87%) [-2.44%, +0.51%] | not timed | not timed | = packet |
| fl3b3 | +0.03% (0.87%) [-1.44%, +1.51%] | -1.35% (0.85%) [-2.83%, +0.12%] | -1.46% (0.64%) [-2.56%, -0.36%] slower | -1.05% (0.44%) [-1.78%, -0.31%] slower |
| o4_isb | -2.10% (0.88%) [-3.60%, -0.60%] slower | +0.38% (1.05%) [-1.44%, +2.19%] | not timed | -1.07% (0.68%) [-2.21%, +0.06%] |
| o4 (O4-r1) | -1.73% (0.93%) [-3.31%, -0.14%] slower | +0.56% (1.15%) [-1.43%, +2.56%] | -1.39% (0.73%) [-2.66%, -0.13%] slower | -1.11% (0.51%) [-1.97%, -0.25%] slower |
| o4_nocp | not timed | -0.98% (1.09%) [-2.88%, +0.91%] | -1.24% (0.69%) [-2.44%, -0.04%] slower | -1.17% (0.59%) [-2.16%, -0.17%] slower |
| fl3b3p | -0.41% (0.80%) [-1.77%, +0.95%] | +0.25% (1.07%) [-1.61%, +2.10%] | -2.54% (0.73%) [-3.80%, -1.28%] slower | -1.21% (0.48%) [-2.02%, -0.40%] slower |
| o4_nospec | not timed | -0.87% (1.26%) [-3.05%, +1.30%] | -1.47% (0.79%) [-2.83%, -0.10%] slower | -1.30% (0.67%) [-2.43%, -0.16%] slower |
| o4_noregions | -1.53% (0.89%) [-3.04%, -0.02%] slower | -0.18% (1.07%) [-2.03%, +1.67%] | -3.77% (0.77%) [-5.09%, -2.44%] slower | -2.21% (0.51%) [-3.06%, -1.36%] slower |

**BEST holds** in this family of 57: nothing is faster than O4-r3. Rank 1 is o4_chained packet
(+1.78%, p 0.0130, threshold 0.000877).

## 6. Decisions per item

These are the final decisions, as recorded in `scopt/o4.py` (`REVISION = "O4-r3"`,
`REVISIONS = ("O4-r1", "O4-r3")`). "Registered verdict" is what the pre-registered rule said in
the campaign that decided the item. Numbers: §5.

### 6.1 In O4-r3 and new against O4-r1

| item | arm | registered verdicts | final | `scopt/o4.py` | evidence |
|---|---|---|---|---|---|
| A5, priced exhaustive (recursive) regions | o4_pwhopr | H: faster than O4-r1 (cli, pooled); H2: faster than O4-r2b; R3: ADD-confirm (as o4r3a, and inside o4r3c and o4r3d) | **in** | `FIRMWARE_MODES["exhaustive"]` included, measured_best, PROVEN_GAIN; `KNOBS` mode and cost_model | H pooled +0.93% [+0.44%, +1.42%]; H2 +1.01% [+0.51%, +1.52%]; R3 o4r3a +0.97% [+0.50%, +1.43%] |
| A3, net-work ranked chain-static | o4_rank | A2: ADD excluded by Holm (p 0.00883 against 0.00833); R3: part of o4r3c and o4r3d, both pass ADD-confirm | **in** | `KNOBS` chain-static `rank_by_net_work`, `c_target_check` included, measured_best | A2 packet +1.51% [+0.48%, +2.54%]; R3 o4r3c packet +0.76% against o4r3a's -0.36% |

### 6.2 O4-r1 items kept in O4-r3

| item | arm | registered verdicts | final | `scopt/o4.py` | evidence |
|---|---|---|---|---|---|
| Firmware regions (against X3) | o4_noregions | A2: REMOVE rejected | **kept** | `PASSES["firmware-inline"]` included, measured_best | A2 cli -1.65% [-2.50%, -0.80%]; F -1.38% pooled against o4r3d; post hoc -2.21% against O4-r3 |
| Host-inline (against X1) | o4_nohi | A2: REMOVE accepted; in O4-r2, O4-r2b, o4r3b, o4r3d | **kept** (§7.13) | `PASSES["host-inline"]` included, bundle, NEUTRAL | A2 +0.24% [-0.30%, +0.77%]; F o4r3c vs o4r3d +0.10% [-0.60%, +0.80%] |
| SPEC bundle (against X4) | o4_nospec | A2: REMOVE accepted | **kept** (§7.13) | specialize-returns, branch-folding, dead-host-ops included, bundle, NEUTRAL | A2 +0.32% [-0.24%, +0.88%]; c7b3 makes 0 / 9 / 1 variants |
| constant-propagation (against X2) | o4_nocp | A2: REMOVE accepted, moot under X4 | **kept** | included, bundle, marginal HARMFUL (fixture 0.943x) | A2 +0.58% [+0.02%, +1.14%]; 0 / 6 / 1 patches |
| WHOPR planner (against A8 legacy) | o4_legacy | A2: ADD excluded | **kept** | `KNOBS` planner reference "whopr", partitions 4, workers 4 | A2 packet +0.37% [-0.60%, +1.35%]; F -0.57% pooled |
| B3 GENERAL chaining, member skip, trusted contract | – | from O4-r1 (c7b3) | **kept** | chain-returns, chain-static measured_best PROVEN_GAIN; member_slot_skip required | rounds 1 and 2 (`docs/O4.md` "What is in O4") |

### 6.3 Not in O4-r3

| item | arm | registered verdicts | final | `scopt/o4.py` | evidence |
|---|---|---|---|---|---|
| A1, `residual_goto_tb = "chained"` | o4_chained | A2: ADD excluded (Holm) | **out: workload-specific option** | `KNOBS` residual_goto_tb default "auto" | A2 packet +1.82%, uart -0.99%, pooled -0.09%; F packet +1.54% (p 0.040, not rejected), uart -2.38%; post hoc packet +1.78%, uart -2.11% |
| I1, ISB side exit (`isb_boundary`) | o4_isb | A2: FIDELITY included; inside O4-r2, which failed B1 | **out** | `KNOBS` isb_boundary default (absent) | A2 +0.19% [-0.36%, +0.75%]; B1 O4-r2 packet -1.21%; F packet -1.84% (slower), pooled -1.18%; post hoc packet -2.10% |
| A4, full-graph hot-loop host-inline | o4_hotgraph | A screen: excluded, measured slower | **out** | `KNOBS` hot_loop_kinds default "transfer" | A pooled -3.63% [-4.84%, -2.43%] |
| A6, bounded unrestricted host-inline | o4_hostall | H / A2: ADD excluded (Holm and non-inferiority) | **out** | `KNOBS` allow_partial default off | H pooled -4.51% [-5.08%, -3.94%]; H2 -4.88% |
| A7, `min_call_count = 2` | o4_min2 | A screen: to A2 (+1.31%); A2: ADD excluded | **out**; the knob is superseded (exhaustive has no call-count gate) | `KNOBS` min_call_count excluded, superseded | A2 +0.10% [-0.44%, +0.63%]; F -0.71% pooled |
| A2a, hot-loop-tasks | o4_tasks | A2: ADD excluded (Holm) | **out** | `FIRMWARE_MODES["hot-loop-tasks"]` excluded, measured_neutral | A2 packet +0.98% [-0.30%, +2.25%]; F -0.56% pooled; post hoc packet -0.17% |
| hot-loop selection (O4-r1's mode) | – | superseded by A5 | **out** | `FIRMWARE_MODES["hot-loop"]` excluded, superseded | R3 +1.06% for o4r3c; F O4-r1 -1.01% against o4r3d |
| C1, FirmLooper v3 + B3, unpriced | fl3b3 | A: to A2; A2: BEST not shown | **out** | `FIRMWARE_MODES["firmlooper-v3"]` excluded, measured_slower | A2 pooled +0.20%, packet +2.03% (not rejected); F -0.78% pooled (slower) |
| C1p, FirmLooper v3 + B3, priced | fl3b3p | A: not to A2 (d ≤ 0) | **out** | as C1 (arm fl3b3p) | A -0.70%; F -0.83% |
| C2, layout passes (`b3-llvm`) | b3l | A: not to A2; slower than O4 | **out**: layout passes not composable with regions | cluster-hot, llvm-cache-layout, order-cold, hotcold-partition excluded, not_composable | A -2.32% [-3.47%, -1.17%] |
| Unpriced exhaustive with bounded host-inline | w7bd_b3 | H: not faster (slower pooled) | **out** | (`docs/O4.md` "What is not in O4") | H -3.57%; H2 -2.17% |

### 6.4 Compositions

| composition | parts | outcome |
|---|---|---|
| O4-r1 | c7b3 | adopted at the start (identity); superseded by O4-r3 |
| O4-r2 | A5 + I1 + X1 + X4 | failed B1 (packet -1.21%, LB -2.87%); never adopted |
| O4-r2b | I1 + X1 + X4 | passed B1-retry and B2; failed H2 (A5 faster); never adopted |
| o4r3a | A5 | passed ADD-confirm in R3 (+0.97%) |
| o4r3b | A5 + X1 + X4 | passed ADD-confirm in R3 (+0.69%), outside the tie band |
| o4r3d | A5 + A3 + X1 + X4 | registered R3 winner; adopted by F under the registered rules; not shipped (§7.13) |
| **o4r3c = O4-r3** | A5 + A3 | passed ADD-confirm in R3 with the highest d (+1.06%); ties o4r3d in F; **shipped** |

O4-r2 and O4-r2b were never adopted, so the packaged revisions go from O4-r1 to O4-r3
(`pipelines/O4-r3.toml` header; commit `56c4c80`).

## 7. Rule changes and deviations

Every change from the written design is listed here, with when it was made relative to the
data.

1. **Student-t reference (decide.py registration 2).** Made at 01:56, before any O4 timing,
   on the lead's decision, because the registered normal reference gave a 20.3% false BEST
   rate in a null 15-arm screen; the t reference gives 5.2% (§3.4; `driver/PREREG.md`).
2. **Same-bytes aliases between timed arms** (registration 2). o4_noregions has the s6b3
   anchor's bytes on uart and cli. Registration 1 would have timed those bytes twice and
   compared them with themselves. Now one arm is timed, the other reads its estimate, and an
   alias scope is not a second Holm hypothesis (`driver/PREREG.md` change 2).
3. **Combined A2** (registration 2), a deliberate deviation from design §3.5. Instead of one
   plan per candidate at seed 20261126 + k, one plan per workload holds o4, o4-aa and every
   candidate whose bytes differ, at seed 20261130. The candidates share one reference, so
   their estimates are correlated; Holm stays valid. About 2.5x less wall time
   (`driver/PREREG.md` change 3).
4. **H crash rule.** After attempt 1 failed, a rule was set before any H estimate: a driver
   crash voids the attempt on all three workloads and the campaign reruns whole with the same
   plans and seed. The rerun is VALID attempt 1, not a seed + 100 rerun (`H-SUMMARY.md`).
   PREREG-R3 later wrote the same rule down for R3 and F.
5. **P4b trigger.** The design lands hot-loop-tasks only if A7 is *accepted* or C1p's pooled d
   is above 0. Neither held when it landed: A7 had only *survived the screen* (and was later
   excluded in A2), and C1p's d was -0.70%. The code landed (`b35cdb6`) and o4_tasks was timed
   in A2 on instruction (`A2-SUMMARY.md` "P4b, for the record"; `packet/VERIFY.md` "P4b").
6. **A5 absorbed into O4-r2.** decide.py's registered composition was I1 + X1 + X4. Under
   design §3.7 BEST, a faster challenger must be absorbed, so O4-r2 added A5. Composing A5 with
   I1 (which modifies the hot-loop base A5 replaces) was the lead's call (`decisions.json`
   `composition`, `absorbed_from_best`).
7. **O4 preflight name refusal.** `--pipeline O4-r2.toml` was refused before any pass (a
   pipeline named O4-rN must equal a packaged revision). O4-r2 was therefore built from the
   copy `o4r2.toml`, which differs only in name and description; O4-r2b was built the same way
   from `o4r2b.toml`, and the R3 variants were named outside the pattern (`o4r3*.toml`). The
   name does not reach cache.sc (VERIFY.md "P5"; `B-SUMMARY.md` "O4-r2b").
8. **B1 used ADD-confirm,** not the REMOVE that the registered composition (no add-on) implied,
   because A5 is a speed claim and ADD-confirm is the stricter rule. Fixed in `p6/B1-rule.md`
   before any B plan or timing; the B1 plans carry the amended composition and A5's H pooled d
   (0.009299589) so the retry rule could name it. REMOVE would also have failed.
9. **B naming.** The registered decide.py decides B1 only for an arm named `o4r2` and fixes
   B2's reference to `o4r2` / `o4r2-aa`. So in B1-retry, B2 and H2 the arm **`o4r2` is
   O4-r2b**, and the first O4-r2 build is timed as **`o4r2a`**. Each plan records this
   (`x_o4r2_is`, `x_arm_map`). H2 carries `x_campaign: "B2"` (label "H2") because decide.py fixes
   H's reference to o4; its seed is 20261128 + 100 = 20261228. G0 for the retry was read from
   `p6/gates-B1-retry.json` (`B-SUMMARY.md` "Naming").
10. **Adoption of O4-r2b required H2 as well.** The step's rule was that O4-r2(b) is adopted
    only if B1, B2 and the H rerun all pass; decide.py's `adopt_o4r2: true` covers only
    B1-retry and B2. H2 failed, so O4 stayed at O4-r1 (`B-SUMMARY.md` "Outcome" 6).
11. **R3, a new registration.** The registered B1 retry was used up, so R3 is a second
    pre-registration (`driver/PREREG-R3.md`, 05:42), written after the B data and before any
    R3 data. It adds `decide_r3.py` (ADD-confirm of four variants against O4-r1, the choice
    rule with its tie-break, and F's arm set from each arm's latest pooled UB95 against O4-r1
    above log 0.99). Interpretation choices recorded there: G0 is a precondition of
    ADD-confirm; the Holm family counts distinct pooled estimates; `--pgo-launch` stays on the
    o4r3b and o4r3d lines (inert without host-inline). Minor deviations in the run:
    `--settle-seconds 30` passed on the command line, and the F plans made with an absolute
    `--r3` path (`R3-SUMMARY.md` "Deviations").
12. **The F amendment, BEST-most.** Written by the lead at about 06:27 at the user's
    instruction, while F was still timing (packet at 482 of 558 runs, the amendment's count;
    F's packet plan has 589 runs, 19 arms by 31 blocks, §3.2; uart and cli done). No F
    estimate had been computed or read, and decide_r3.py had not been run on F. It replaces
    strict BEST for adoption and for O4's standing contract: an optimization enters O4 when
    the pooled test against O4 rejects at 0.05 and it is not shown slower on most of its
    differing workloads; a one-workload gain is a documented option and is not absorbed.
    `driver/best_most.py` applies it to F.json without computing any new estimate (11 tests).
    R3's decision is unaffected. The user's words:

    > "i think you took the instructions too literally. We can't have a perfect configuration,
    > that always win. Since its workload dependent. Win in most cases is fine"
    > (`driver/AMENDMENT-F-most-cases.md`)

13. **O4-r3 = o4r3c, not the registered o4r3d.** The registered R3 tie-break picked o4r3d
    (by 0.0042 points of d) for its deterministic build, and F adopted o4r3d under both rules.
    Late in the run (by 06:40), the user restated the goal:

    > "the key is to have all our optimizations in recursive manner thats it"
    > (quoted in `rec/DESIGN.md` §1.1 and `rec/map-regions.md`)

    The lead then shipped o4r3c, which is o4r3d with host-inline and the SPEC bundle kept: O4
    holds every optimization, neither costs anything measurable (F: o4r3c vs o4r3d +0.10%
    [-0.60%, +0.80%]), and o4r3c had the highest pooled d in R3 (+1.06%). This departs from the
    registered choice; its cost is that O4-r3's build depends on the gdb helper-PGO replay
    (§8.4). Sources: `docs/O4.md` "What is in O4"; commit `56c4c80`; `pipelines/O4-r3.toml`.
14. **Post hoc F re-reference.** To give O4-r3 its own evidence rows, F's observations were
    re-read with o4r3c as reference (`evidence/r3/adapt_f.py`, `decide.py --unregistered`). It
    is post hoc: F was registered with o4r3d as reference, there is no o4r3c A/A copy (VALID is
    vacuous), and F's alias workloads are not re-read (§5.11.4).
15. **Round-2 seed of the O4-r1 evidence** (made at 00:01, before registration 2). Round 2's
    observations were read with decide.py registration 1 (`75e7b12e…`) `--unregistered`, with
    c7b3 as both reference and A/A arm (VALID vacuous) and no burn-in block, because round 2
    pre-registered none (`docs/o4-evidence.json` `spec`; `evidence/r1-seed/`).

The original rule, in the user's words (2026-11-25, `design/O4-design.md` §0):

> "we should have one config that include all of our good things let's call it O4. Do that!
> and O4 remains the best. Note it down as well. Whatever the best speed optimizations are
> there MUST BE included in O4~!"

On WHOPR the user added: "even without profile-guided firmlooper should help identify hot
regions."

## 8. Findings

### 8.1 Region selection

- **Priced exhaustive selection (A5) is the fastest selection measured,** and it is the
  recursive one. Against O4-r1 it was faster in H (+0.93% pooled) and R3 (+0.97%), and against
  O4-r2b in H2 (+1.01%) and B2 (+1.56%, not shown under Holm). Its gain is mostly on cli (+1.97%
  in H, +1.74% in R3). Its regions: 53 / 27 / 14 (O4-r1: 48 / 19 / 7), members 7,052 / 1,228 /
  1,470 (5,967 / 614 / 820), nested inlines 360 / 43 / 32 (289 / 26 / 19). It refuses 140 /
  175 / 194 roots as not_profitable and 3 / 10 / 14 as unpatchable_native_exit. On cli it
  refuses O4-r1's 0x83d06 root (279 members) and admits 8 roots O4-r1 lacks. Sources:
  `H-SUMMARY.md`, `R3-SUMMARY.md`, `B-SUMMARY.md`, `{packet,uart,cli}/VERIFY.md`.
- **Unpriced exhaustive selection was never timed on its own.** What is measured is that
  priced exhaustive selection (A5) beats hot-loop (above). Each timed arm with unpriced
  exhaustive selection also changed host-inline, so its result does not isolate the
  selection:
  - w7 also copies unbounded helper graphs. The design lists its s6/w7 0.8513 / 0.6921 /
    0.9109 under "Unbounded trusted helper graphs (H2)" (`design/O4-design.md` §1.3). The same
    pipeline with the graphs bounded is faster: w7/w7bd 1.2576 / 1.4003 / 1.3149
    (`design/inventory.md` row H3, round 2's bounded WHOPR timing).
  - w7bd_b3 also carries bounded unrestricted host-inline. It measured -3.57% pooled in H and
    -2.17% in H2; A6, that host-inline alone on O4-r1, measured -4.51% in H (`H-SUMMARY.md`,
    `B-SUMMARY.md`).
  - The inventory labels exhaustive selection (F2) "NEUTRAL on packet and cli", and on uart
    "the cause is confounded with host-inline growth (H3)" (`design/inventory.md` row F2).
- **Regions matter on top of B3 now.** Rounds 1 and 2 found the regions' marginal effect on
  top of B3 within noise (mean about +1.3%). Here removing them (X3) is slower: A2 cli -1.65%,
  F -1.38% pooled against o4r3d, -2.21% post hoc against O4-r3.
- **A1 is workload dependent.** Chained residual pricing helps packet (A2 +1.82%, F +1.54%,
  post hoc +1.78%) and hurts uart (A2 -0.99%, F -2.38%, post hoc -2.11%). On packet it admits
  the CRC roots (0x4a70, 2,317,210 calls), which fl3b3 also admits unpriced; the recursion map
  attributes fl3b3's packet signal (+2.03% in A2) to the same roots. Its regions are entered
  (A1 gate: "entered, possibly slower").
- **A3 is a near-miss on its own and a gain in composition.** A2 packet +1.51% [+0.48%, +2.54%]
  missed Holm by 0.0005 (p 0.00883 against 0.00833). It changes bytes only on packet, where the
  256-unit chain-static cap binds (27 exits refused unranked, 16 ranked). In R3 o4r3c's packet
  estimate is +0.76% against o4r3a's -0.36%.
- **A7's screen signal did not replicate** (A +1.31% with LB +0.19%; A2 +0.10%). The design
  named this risk and its mitigations: Holm against the winner's curse, and head-to-heads after
  the screen (`design/O4-design.md` §4 risk 11).
- **A8's predicted swap did not happen on O4.** On cli, probe2 predicted root 0x83794 replaced
  by 0x83788; on O4 both have 1,326 calls, hot-loop orders ties by callsite_id, and the
  callsite ids differ between the probe's base and O4 (the host-inline stage changes them).
  So the legacy planner gives O4's roots and bytes. On packet legacy gives 48 regions (+0x82066,
  -0x828da), not about 50 (`cli/VERIFY.md` §4; `packet/VERIFY.md` "Problems" 2).
- **I1 alone is neutral, in composition it costs packet.** A2 +0.19%. O4-r2 (with I1) lost
  packet in B1 (-1.21%) while A5 + X1 + X4 without I1 (o4r3b) did not (packet LB -1.85% in R3);
  R3 names I1 the likely cause, as a cross-campaign hypothesis. F shows o4_isb slower on packet
  (-1.84%), and post hoc -2.10% against O4-r3.
- **FirmLooper adds nothing once regions are priced.** C1p (priced FirmLooper) admits only 3 /
  3 / 2 regions and is not faster (A -0.70%, F -0.83%). hot-loop-tasks adds one 14-call memset
  region on packet only. The recursion map found that every FirmLooper-only root had 0 training
  calls; FirmLooper's real signal is the CRC roots that pricing refuses (`rec/map-regions.md`
  §0).

### 8.2 Host-inline

- **Nearly inert in O4.** It applies 1 / 0 / 0 sites (`helper_shl_cc` on packet). Almost every
  hot helper call goes to `helper_lookup_tb_ptr` or `helper_udiv`, which the selected-root
  lowering refuses (`rec/map-hostinline.md` §4). Removing it (X1) was neutral (A2 +0.24%) and
  restoring it with SPEC on top of A5 + A3 measured +0.10% (F).
- **Its packet site count is not stable across builds.** The gdb counted replay of
  `helper_shl_cc` gave 8,372 (o4), 8,514 (c7b3 rebuild), 2,344-2,438 (six builds started
  together at 00:50), 3,464 (o4_nospec) and 8,651-8,969 (others); round 2 recorded 9,433 and
  o4r3c 8,186. The threshold is `min_site_calls = 1000`. Every build still applied the site, but
  a count below 1,000 would make `-O4` drop it and publish o4_nohi's bytes
  (`packet/VERIFY.md` "Problems" 1; `rec/map-hostinline.md` §4).
- **Without it, uart and cli change layout only.** o4_nohi differs from o4 by 202,146 bytes on
  uart and 157,176 on cli, yet every receipt, root, member count and profit is identical. The
  host-inline native prefix stage runs even with 0 sites and changes the units' `source_hash`,
  and with it the callsite ids, region ids, region order and member placement. On uart and cli,
  X1 times a relayout (`uart/VERIFY.md` "Findings" 3; `cli/VERIFY.md` §4).
- **Copying helper graphs is slower.** A4 (full graphs, 32,768 B cap) -3.63% in A; A6 (bounded
  unrestricted) -4.51% in H and -4.88% in H2; round 2's unbounded graphs measured c7b3/w7b3
  0.7235 / 0.6180 / 0.7813 (`design/O4-design.md` §1.3). w7b3 also uses unpriced exhaustive
  selection; A4 and A6 change only host-inline on O4-r1. The copies remove no nested call
  (`nested_calls_eliminated: 0`; `rec/map-hostinline.md` §0).

### 8.3 The SPEC bundle

c7b3 makes 0 / 9 / 1 SPEC variants and 0 / 6 / 1 constant-propagation patches. Removing the
bundle (X4) or constant-propagation (X2) was neutral in A2 (+0.32%, +0.58%). On packet both
removals have O4-r1's bytes.

### 8.4 Build and launch reliability

- **Helper-PGO gdb flake.** The counted replay failed twice in this campaign, both on uart:
  the P4b spot check of O4-r1 (`Catchpoint 1 (syscall 'exit_group' [231])`, then
  `count.gdb:11: Error in sourced command file: Couldn't get registers: No such process.`)
  and the R3 check build of o4r3a (`Couldn't get registers: No such process.`). Each passed on
  one retry (`uart/VERIFY.md` "P4b" and "R3"; `r3/build.log`). Before this campaign the
  replay had already failed intermittently twice (`design/O4-design.md` §1.2).
- **Code-buffer EEXIST.** "allocate supercache code buffer: File exists" (QEMU `map_cache`,
  fixed-address `MAP_FIXED_NOREPLACE` at 0x7fff6c000000) hit 1 of about 3,400 timed launches in
  A and H plus their staging runs. It is a latent launch failure for every sealed cache, O4
  included; it fails loudly with exit 1 and never gives wrong output (`H-SUMMARY.md`). It did
  not recur in A2, B1-H2 or R3.
- **`--pgo-launch` is inert without host-inline,** and the pipeline name does not reach
  cache.sc (O4-r1 = c7b3, o4r3a = o4_pwhopr, O4-r3 = o4r3c).

### 8.5 Measurement

- **The driver's A/A on uart flagged bias while VALID held.** In A (0.9907x [0.9491, 0.9993])
  and A2 (0.9911x [0.9803, 0.9994]) the driver's median-ratio A/A flagged `harness_bias`; in H2
  it reported `above_one` (1.0091x [1.0001, 1.0186]). decide.py's pre-registered VALID (HL,
  burn-in dropped, 99%) held every time, and every estimate uses the pooled reference.
- **cli's burn-in block was disturbed in every campaign but B1,** because cli's staging is short
  and its first blocks overlap the other workloads' staging and warm replays. Runs above 1.3x
  their median in block 0: A 17 of 22 (A/A ratio 0.6830), H 6 of 10, A2 8 of 16, B1-retry 2,
  B2 10 (max 8.05x), H2 4, R3 2, F 19 (max 17.8x). decide.py drops block 0 as pre-registered.
  In A, H and A2, cli's timed blocks also overlapped that staging.
- **A/A block log-ratio SD over the timed blocks** (packet / uart / cli): A 4.68 / 6.22 / 2.78%;
  H 4.05 / 4.82 / 3.93%; A2 4.38 / 4.79 / 3.21%; B1 6.09 / 4.29 / 3.24%; B1-retry 7.72 / 4.43 /
  3.51%; B2 4.36 / 4.95 / 4.45%; H2 4.54 / 4.22 / 4.00%; R3 5.49 / 4.95 / 3.57%; F 3.45 / 4.83 /
  4.34% (round 2's basis: 3.66 / 7.00 / 3.12%). Sources: the summaries; `r3/runinfo-F.json`.
- **The normal reference was miscalibrated.** It would have reported a false "faster than O4"
  in about 1 of 5 null screens (§3.4).

### 8.6 What is and is not recursive (the recursion investigation)

Sources: `rec/DESIGN.md`, `rec/map-regions.md`, `rec/map-b3.md`, `rec/map-hostinline.md`,
`rec/map-others.md`, `rec/map-firmbolt.md`, `rec/no-qemu-routes.md`;
`docs/o4-recursive-critique.md`. Read-only design work: nothing in the plan has been built or
timed. In the campaign the design's critique step was stopped before it ran. A critique was
written afterwards (2026-11-26, `docs/o4-recursive-critique.md`, summarized in
`docs/o4-recursive.md` §8). A later read-only analysis, `rec/no-qemu-routes.md`, contests part
of that critique (the last two bullets). It first appeared at 08:01 and changed again at
08:17; its 08:17 version (44,484 B, sha256 `eb2f2c0a…`) is final and archived as
`docs/o4-campaign/recursive/no-qemu-routes.md`. `docs/o4-recursive.md` is the full summary.

- **Only region formation is recursive.** `build_firmware_region` expands nested calls
  transitively over (uid, inline-context) with no depth, member or growth bound; only a
  visited set and a recursion guard stop it. O4-r3 reaches depth 5 / 5 / 5 with 360 / 43 / 32
  nested calls. Each root's whole closure is admitted or refused as one unit (no nested gate,
  no flat fallback; 3 packet roots lost this way), and chainability is judged per region, so
  one B3-grown member prices every residual goto_tb of the region at a full dispatch (the CRC
  root is priced at -22.2 ms).
- **B3, SPEC and host-inline do not reach inside regions.** Regions are planned after B3 and
  materialized at link time; each member copies the **pre-B3** bytes of its source, and QEMU's
  skip flags refuse to arm B3 slots in member copies. Host-inline reaches members only because
  it is the pre-B3 native prefix, so members inherit its splices. No pass runs on member
  copies, and the pass manager runs each pass once (no fixpoint).
- **B3's work on region-entry sources is dead code.** `lookup_entry` returns the region before
  the public TB, so while trusted regions are loaded a region-entry source's public code, with
  its B3 guards, never runs. Of chain-static's guarded units, 44 of 256 / 23 of 50 / 13 of 43
  are region-entry sources; 44 of 53 / 23 of 27 / 13 of 14 entry sources are B3-grown. On packet
  44 capped slots are dead while 16 profitable plans were refused by the same cap. The
  critique (its §2.3) and `rec/no-qemu-routes.md` (its §0) add that every B3 slot on an entry
  source guards the root call, which the region already turns into an internal call edge, so
  copying B3 into entry members would revive nothing; the cost is chain-static's budget.
- **The U7 premise was wrong.** U7 ("B3 slots bypass the lookup that enters regions": about 53
  region entries per packet run against 41,700 for p7) rests on `region_entries`, which counts
  lookup misses only. A B3 GENERAL slot whose target is a region's entry PC already chains into
  the region (o4r3c packet: 2,817 lookup entries, 56 trusted-destination chain installs). The
  real gap runs the other way: regions contain no B3. (`map-others.md` and `map-firmbolt.md`
  still state the old premise; `map-b3.md`, `map-regions.md` and `DESIGN.md` §1.4 correct it.)
- **The 256-unit cap.** `MAX_UNITS = 256` in `chain_returns.py`, enforced by the pass and again
  by the manager. It binds only on packet chain-static (272 eligible plans with ranking: 256
  kept, 16 refused). The sc6 arrays have room for about 2,000 slots against 303 today. Listing
  chain-static twice would lift the cap with no code change (not built; the critique checked it
  against the code, its §3.5).
- **Host-inline is one level deep** in O4's mode; the full-graph modes only copy. The gdb replay
  could be replaced by the steady profile's exact TB-entry counts (ratio 1.000 on uart and
  0.878-1.000 on packet for the hot non-lookup sites). The designed but unbuilt C1/C2 partial
  evaluation of the MSR BASEPRI / hflags pair is the "recursive" host-side option.
- **FirmBOLT analogue.** FirmBOLT `-O4` is a transitive inliner (R1) that copies already
  optimized bodies (R2), folds context in each copy (R3), prices each nested candidate (R4) and
  runs late passes on the result (R5). scopt has R1 only.
- **The staged plan** (`rec/DESIGN.md` §3): stage 1 config only (o4rec1 = o4r3c + A1 + a second
  chain-static entry; siblings o4rec1a without A1, o4rec1x with the neutral cleanup passes;
  and a perf attribution); stage 2 scopt-only pricing and selection (nested gate and
  best-of-three plans for exhaustive, B3-aware per-member pricing, the cap lift in code,
  single-instruction BL discovery, the FirmLooper union, gdb-free host-inline, safety budgets);
  stage 3 B3 inside member copies (linker plus one QEMU member flag); stage 4 closure
  extensions and intra-region layout; stage 5 context specialization on members and helper
  partial evaluation; stage 6 items that need evidence first. The prediction for o4rec1 against
  o4r3c is packet about +1 to +2%, uart about -1 to -2.5%, cli the same bytes: probably a
  packet-specific option.
- **How much B3 member copies lose is contested.** QEMU skips 799 / 178 / 153 B3 slots in
  member copies (sidecar `skipped_b3_slots`). The critique counts a slot as covered only
  when one of the member's internal edges goes to the slot's own target: 601 / 117 / 114
  covered, 198 / 53 / 37 left (its §2.3, §6 item 3). `rec/no-qemu-routes.md` counts a slot
  as covered when the member's `goto_ptr` exit already has an internal edge: 770 / 162 / 147
  covered, 29 / 16 / 6 left, all in root-continuation members, carrying at most 0.661M /
  0.227M / 0.0068M steady lookups, at most 4.0 / 1.4 / 0.04 ms per training run (its §2,
  reproduced in its §5.1).
- **Stage 3 needs no QEMU change (resolved).** scopt only rewrites the supercache, and the
  user stated that principle: "scopt has nothing to do with QEMU. it works on supercache not
  QEMU". DESIGN and the critique (its §2.2) had proposed one new QEMU member flag
  (`SC7_MEMBER_B3_COPY`). `rec/no-qemu-routes.md` says no QEMU
  change is needed (its §0, kept after its own check in §5.0): root-continuation expansion
  (4a) plus a priced, guarded return expansion makes the residual exits internal, and a twin
  source record lets today's loader arm B3's own slots. Its own check (its §5.2, §5.3) found
  that 4a needs a stronger poll rule, because a caller loop that does not re-run the call
  unit becomes a poll-free internal cycle, and that the twin record is correct only under
  sealed or exact-PC loads (a plain load can activate it and corrupt the arena), so the twin
  is a fallback. Nothing has been built to settle either point.

## 9. Open issues

1. **No registered campaign has O4-r3 as its reference.** Its standing rests on R3 (registered,
   against O4-r1), F's registered row against o4r3d (a tie), and the post hoc F re-read with a
   vacuous VALID. A campaign with o4r3c and an o4r3c A/A copy as the reference would close
   this.
2. **Recursion beyond call expansion is designed, not built** (§8.6). In the campaign the
   design's critique step was stopped before it ran. A critique was written afterwards
   (2026-11-26, `docs/o4-recursive-critique.md`, summarized in `docs/o4-recursive.md` §8),
   and nothing in the plan has been built. A later read-only analysis
   (`rec/no-qemu-routes.md`, archived as `docs/o4-campaign/recursive/no-qemu-routes.md`)
   shows that no stage needs a QEMU change and contests the critique's sizing of stage 3
   (§8.6). The first
   arm, o4rec1, has exact TOML in `rec/DESIGN.md` §4.1; its siblings o4rec1a and o4rec1x are
   specified in §3.1 and §4.3. None is built. The perf attribution that would size stages 3-5
   has not been run.
3. **Host-inline's gdb counted replay** is non-deterministic (2,344-9,433 counts on packet
   against a threshold of 1,000) and occasionally fails. Whether the `-O4` stage gate would
   refuse a build whose site count fell below the threshold was not tested. O4-r3 keeps
   host-inline, so its build depends on the replay.
4. **The code-buffer EEXIST launch failure** in QEMU's `map_cache` (about 1 in 3,400 launches)
   was recorded, not fixed, in this work.
5. **Workload-specific options are recorded, not resolved.** A1 helps packet and hurts uart;
   B3-aware pricing (stage 2b) is the proposed fix. Why I1 costs packet in composition is a
   hypothesis.
6. **The 256-unit cap** still binds on packet (16 ranked plans refused).
7. **Generalization.** The evidence comes from three FirmBOLT `-O4` images with one training
   input each, and the profit constants were never swept (`design/O4-design.md` §4 risk 12).

**Statements elsewhere that these records supersede.** They predate the campaign's results or
were written from the predictions. `scopt/o4.py` is outside this documentation step; the lead
can correct both files.

| statement | where | what the records show | source |
|---|---|---|---|
| the WHOPR planner's pricing order "changes admission (legacy: packet +2 roots, cli 1 swapped)" | `docs/O4.md` "What is in O4", WHOPR planner row; `scopt/o4.py:195-198` | that is probe2's prediction. On O4, legacy gives packet 48 regions (+0x82066, -0x828da), and o4_legacy has o4's bytes on uart and cli (§8.1) | `packet/VERIFY.md` "Problems" 2; `cli/VERIFY.md` §4; `uart/VERIFY.md` P2 table |
| lifting the 256-unit cap is "unmeasured, code change"; "raising it is a code change (B2)"; 27 exits refused | `docs/O4.md` "What is not in O4"; `scopt/o4.py:224-226` | listing chain-static twice lifts it with no code change (not built). Under O4-r3's ranking 16 plans are refused, not 27 (§8.6) | `rec/DESIGN.md` row 22, §4.1; `rec/map-b3.md` §5.2; `docs/o4-recursive-critique.md` §3.5 |
| the regions' marginal effect inside O4 is "NEUTRAL on top of B3" | `docs/O4.md` "What is in O4", regions row; `scopt/o4.py` `PASSES["firmware-inline"]` (`marginal="NEUTRAL"`) | removing the regions (X3) is slower now: A2 cli -1.65%, F -1.38% pooled against o4r3d, -2.21% post hoc against O4-r3 (§8.1) | `A2-SUMMARY.md`; `F-SUMMARY.md`; `evidence/r3/decide.json` |
| O4-r3 is "faster on all three measured workloads" | `docs/O4.md` lines 7-8 | R3's estimates are positive on all three, but R3's rule tests the pooled estimate (with per-workload non-inferiority). Per workload only cli's LB95 is above 0: packet +0.76% (LB -0.38%), uart +0.43% (LB -0.28%). In F's direct pair (descriptive) O4-r3 against O4-r1 is uart -0.56%, and post hoc O4-r1 (`o4`) is uart +0.56% against O4-r3 (§1, §5.11) | `R3-SUMMARY.md`; `F-SUMMARY.md`; `evidence/r3/decide.json` |
| unpriced exhaustive selection "regressed (s6/w7 0.85 / 0.69 / 0.91)" | `docs/O4.md` "What is in O4"; `scopt/o4.py:141-142`, `:180-182` | s6/w7 also copies unbounded helper graphs; unpriced exhaustive selection was never timed on its own (§8.1) | `design/O4-design.md` §1.3; `design/inventory.md` rows F2, H3 |

## 10. Where the raw records are

### 10.1 In the repo

- `docs/O4.md` (the O4 note), `scopt/o4.py` (decisions), `docs/o4-evidence.json` (rows pinned by
  sha256, written by `scripts/o4_evidence.py`), `pipelines/O4-r1.toml`, `pipelines/O4-r3.toml`,
  `pipelines/O4.toml`.
- `docs/o4-recursive.md` (the recursion investigation summarized) and
  `docs/o4-recursive-critique.md` (the critique written afterwards; it has no local original).
- `docs/o4-campaign/`, the archive of the campaign's text records:

| archive path | local source |
|---|---|
| `docs/o4-campaign/{A,H,A2,B,R3,F}-SUMMARY.md` | `/data/scfix-o4/*-SUMMARY.md` |
| `docs/o4-campaign/data/` (screen, H, A2, B1, B1-retry, B2, B, H2, R3, F, F-most, decisions JSON) | `/data/scfix-o4/*.json` |
| `docs/o4-campaign/design/O4-design.md` | `/data/scfix-o4/design/O4-design.md` |
| `docs/o4-campaign/pipelines/` (every arm and composition TOML) | `/data/scfix-o4/design/pipelines/` |
| `docs/o4-campaign/prereg/` (PREREG.md, PREREG-R3.md, AMENDMENT-F-most-cases.md, B1-rule.md, DECIDE.sha256, DECIDE_R3.sha256) | `/data/scfix-o4/driver/`, `/data/scfix-o4/p6/B1-rule.md` |
| `docs/o4-campaign/recursive/` (DESIGN.md, the five maps and `no-qemu-routes.md`) | `/data/scfix-o4/rec/` |
| `docs/o4-campaign/tools/` (decide.py, decide_r3.py, best_most.py, make_plan.py, compare_rtos.py, as `.txt`) | `/data/scfix-o4/driver/` |
| `docs/o4-campaign/verify/{packet,uart,cli}-VERIFY.md` | `/data/scfix-o4/{packet,uart,cli}/VERIFY.md` |

### 10.2 On local disk (`/data/scfix-o4/`, not in the repo)

| path | what |
|---|---|
| `TIMING.md` | this file |
| `design/` | `O4-design.md` (and `.v1.md`, `.json`), `inventory.md`, `feasibility.md`, `pipelines/`, `arms.json`, `commands.txt`, `gen_arms.py`, `probe/`, `probe2/` |
| `driver/` | `decide.py`, `decide_r3.py`, `best_most.py`, `compare_rtos.py`, `make_plan.py`, `make_gates.py`, `validate_plans.py`, `validate_r3.py`, the tests, `PREREG.md`, `PREREG-R3.md`, `AMENDMENT-F-most-cases.md`, the sha256 files, `prereg/` (registration 1), `sim/` |
| `plans/<C>/` | the plans of every campaign, with `validation.json` |
| `<w>/timing-<C>/` and `<w>/timing-<C>.log` | driver output: `plan.json`, `observations.jsonl`, `host.json`, `identities.json`, per-run logs; `timing-H.attempt1` keeps H's failed attempt |
| `<w>/VERIFY.md`, `<w>/arms-built.json`, `<w>/receipts.json` | build gates, receipts, regions replays, P4b, P5, R3 sections |
| `<w>-w/` | private inputs, builds (`opt/<arm>/`: `command.txt`, `optimize.json`, logs, store), launches, gate runs |
| `gates/` | G0 files (`all.json`, per workload) |
| `screen.json`, `H.json`, `A2.json`, `B1.json`, `B1-retry.json`, `B2.json`, `B.json`, `H2.json`, `R3.json`, `F.json`, `F-most.json` (with `*.stdout.txt`) | decide outputs |
| `decisions.json`, `p5/` | P5 decisions and the O4-r2 build |
| `p4b/` | hot-loop-tasks build, gates, spot check |
| `p6/` | campaign B: B1 rule, O4-r2b build, plan makers, tables, `final_o4_vs.json`, run info |
| `r3/` | R3 builds, validity, plan makers, tables, direct pairs, F criterion, `runinfo-F.json` |
| `hygiene/<C>/` | per-campaign `before.json`, `after.json`, `diag.json`, `launch.log`, `start.marker` (`H/attempt1/` for H's failed attempt) |
| `evidence/r1-seed/` | O4-r1 identity and the round-2 observations seeded into the evidence |
| `evidence/r3/` | O4-r3 identity (`identity.json`), `adapt_f.py`, the post hoc `decide.json` |
| `rec/` | the recursion investigation: the five maps and `DESIGN.md` (06:40-07:03), and `no-qemu-routes.md` (08:01-08:17, final at 08:17; archived) |

Earlier rounds: `/data/scfix-campaign/TIMING.md` (round 1) and `/data/scfix-r2/TIMING.md`
(round 2, including "Bounded WHOPR timing").
