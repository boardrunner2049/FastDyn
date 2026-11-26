# Round R3: pre-registered rule (decide_r3.py)

Date: 2026-11-26, 05:42 EST. Made by the R3 preparation step, before any R3 timing.

decide_r3.py sha256: `e6554ab034b85d021df575b2eb401c1cf288e26614e37a66c9477c517409969c`

| file | sha256 | role |
|---|---|---|
| `decide_r3.py` | `e6554ab034b85d021df575b2eb401c1cf288e26614e37a66c9477c517409969c` | **the R3 rule (registered here)**; also in `DECIDE_R3.sha256` and in every R3 / F plan as `x_decide_r3_sha256` |
| `decide.py` (unchanged) | `0b7247476a39abade599000de4e2555c7edb1167b57d0989f4bac54cbaf1a438` | registration 2 (PREREG.md); decide_r3.py imports its functions and refuses to run if its sha256 is not this and `DECIDE.sha256`'s |
| `test_decide_r3.py` | `6c128255f609c48cccbc47f85acb3f8cda3247c8bc7423b671e5cd2993f9f6c1` | 18 synthetic tests, all pass |
| `make_plan.py` (unchanged) | `048da1778f3b26e3457c221ffdfb854c2513f7ea012adc716dcfea225f4c0e05` | plan code used by `r3/make_r3_plans.py` |
| `compare_rtos.py` (unchanged) | `f7046c86be09c37d7b6b12665cfc0d4b427696aedd1ff956dea319de277d7736` | the driver |
| `o4lib.py` (unchanged) | `50b49ef0a052795d4aa47f2d5c9f7599528ce6e5ca02a4561cc72aa6e59afdb0` | |
| `validate_plans.py` (unchanged) | `a2d420e7e529b0ec111f1001115e3c2b5ea5864ab75b31ca37bd19aa369a9890` | plan checks reused by the R3 validator |
| `make_gates.py` | `22858e5cd7f21d40ba9551dbcec030426b7d8ef512af4a778867efe02b408533` | BUILT gained o4r3b, o4r3c, o4r3d (was `4de960ae…`, backup in `r3/backup/driver/`) |

## No R3 timing data existed

Checked at 05:41 EST, after the R3 builds and gates and before this file and any R3 plan:

- `ls -d /data/scfix-o4/*/timing-R3* /data/scfix-o4/*/timing-F* /data/scfix-o4/plans/R3 /data/scfix-o4/plans/F`
  matches nothing.
- `find /data/scfix-o4 -name observations.jsonl` lists 30 files: the six round-2 copies under
  `evidence/r1-seed/` (2026-11-25 23:59) and the 24 of campaigns A, H (and H.attempt1), A2, B1,
  B1-retry, B2 and H2 (02:03 to 05:03). None is newer than `r3/start.marker` (05:16).
- `test_decide_r3.py` writes synthetic observations only under pytest's temporary directory.

## What R3 decides

O4-r1 (= c7b3's bytes) is not the fastest measured configuration: o4_pwhopr (A5, firmware-inline
mode "exhaustive" with cost_model "profile") beat it in campaign H (pooled d +0.93%, one-sided
95% [+0.44%, +1.42%]; packet −0.11%, uart +0.47%, cli +1.97%) and beat O4-r2b in H2 (+1.01%) and
B2 (+1.56%, packet +2.06%). O4-r2 (A5 + I1 + X1 + X4) failed B1 on packet (−1.21%, LB95 −2.87%).
R3 times four compositions of A5 with the measured speed items against O4-r1, together, and
picks one under the rule below; campaign F then checks the pick against everything not shown
slower than O4-r1.

| label | timed arm | pipeline | parts |
|---|---|---|---|
| o4r3a | `o4_pwhopr` (the existing build) | `design/pipelines/o4r3a.toml` (= o4-pwhopr.toml's passes) | A5 |
| o4r3b | `o4r3b` | `design/pipelines/o4r3b.toml` | A5 + X1 + X4 |
| o4r3c | `o4r3c` | `design/pipelines/o4r3c.toml` | A5 + A3 |
| o4r3d | `o4r3d` | `design/pipelines/o4r3d.toml` | A5 + A3 + X1 + X4 |

Each pipeline is the exact union of the measured arm deltas against O4-r1.toml
(`r3/make_r3.py`, checked with tomllib and scopt's `load_pipeline`). Build bytes (cmp,
`/data/scfix-o4/<w>/VERIFY.md` section R3):

| workload | timed in R3 (besides o4, o4-aa) | aliases (bytes equal, timed once) |
|---|---|---|
| packet | o4_pwhopr, o4r3b, o4r3c, o4r3d | none |
| uart | o4_pwhopr, o4r3b | o4r3c → o4_pwhopr, o4r3d → o4r3b |
| cli | o4_pwhopr, o4r3b | o4r3c → o4_pwhopr, o4r3d → o4r3b |

So A3 changes bytes only on packet (as before), and the R3 Holm family has four hypotheses
(o4r3c and o4r3d each have their own packet estimate, so their pooled estimates are their own).

## The rule

All estimates are decide.py's (registration 2), computed by its own functions: per workload
w and arm X, over the timed blocks b ≥ 1, l_b = ½(log T_b(ref) + log T_b(aa)) − log T_b(X);
d_w = Hodges-Lehmann of the l_b; SE_w = SD of 20,000 block-bootstrap HL values (seed
`"{seed}:{w}:{X}"`); Student-t with df_w = n_w − 1; pooled over the workloads where X's bytes
differ from the reference's by inverse variance with Welch-Satterthwaite df; one-sided 95%
bounds LB, UB; p = P(T > d / SE) for "X faster than the reference". Where X's bytes equal a timed
arm Y's on w, X's row there is Y's estimate (an alias).

### Campaign R3

- Arms: o4 (O4-r1; `aa_control` o4, `aa_name` o4-aa) and every variant whose bytes differ from
  o4 on the workload; a variant with another variant's bytes is an alias of it there.
- Blocks 41 / 81 / 41 (40 / 80 / 40 timed + burn-in block 0), seed **20261201**, settle 30 s,
  CPUs packet 10, uart 20, cli 12; the three workloads run concurrently, one driver each.
- **VALID** (decide.py's): per workload, a_w = HL of log(T_b(o4) / T_b(o4-aa)) with its two-sided
  99% bootstrap percentile interval. If it excludes 0 the workload is biased: its plan is rerun
  once at seed + 100 (20261301) as `x_attempt` 2 (`make_r3_plans.py R3 --rerun`); the rerun
  replaces attempt 1 for that workload; if it is biased too, every row of that workload carries
  `reference_bias` and the decision is made with that flag. Until the rerun exists the rows are
  `pending_rerun` and the decision is provisional (not final). (decide.py's per-workload
  selection: if all three reruns are run together for hygiene, a rerun of a workload whose
  attempt 1 was not biased is ignored, with a warning.)
- **ADD-confirm** of variant V against O4-r1 holds iff all of:
  1. **G0**: `gates/all.json` g0 true for V's arm (O4-design 3.7 ADD clause 1, as in B1's
     ADD-confirm). All four arms have g0 true today.
  2. **Holm**: V's pooled one-sided p is rejected by Holm's step-down at family α 0.05 over the
     variants timed, one hypothesis per distinct pooled estimate (a variant whose every workload
     is an alias of one variant Y, timed on exactly those workloads, takes Y's result and is not a
     second hypothesis; not the case here).
  3. **LB_w**: LB95_w > log(0.98) on every workload where V's bytes differ from O4-r1's (alias
     workloads included, with the target's estimate).
- **Choice**: among the variants passing ADD-confirm, the highest pooled d. Within 0.003 of it
  (0.3%, decide.py's GROUP_TIE; d ≥ best d − 0.003), the tie-breaks apply in order: prefer a
  variant with X1 and X4 (deterministic build: no helper PGO, no gdb, no `--pgo-launch`); then
  prefer one without A3 (fewer parts); among what remains, the highest d (an exact tie goes to
  the first of o4r3a, o4r3b, o4r3c, o4r3d). If no variant passes, the winner is "none" and the
  round is **blocked** (O4 stays at r1; F is not run).

### Campaign F (after R3, only with a final winner W)

- Reference W's arm with `aa_name` "⟨W arm⟩-aa" (e.g. o4r3b / o4r3b-aa; o4_pwhopr /
  o4_pwhopr-aa for o4r3a). Blocks 31 / 21 / 21 (30 / 20 / 20 + burn-in), seed **20261202**
  (VALID rerun 20261302), settle 30 s, CPUs and concurrency as R3.
- Arms (`decide_r3.f_arms(W)`): W, O4-r1 (o4), the other three R3 variants, O4-r2 (arm `o4r2`,
  the opt/o4r2 build, labelled "o4r2a" in B2's tables), O4-r2b (arm `o4r2b`), and every other built
  arm or anchor whose latest pooled estimate against O4-r1 has UB95 > log(0.99) = −0.01005.
  Arms whose bytes equal W's on a workload are dropped there; arms with another arm's bytes are
  aliases (make_plan.py's same-bytes handling). decide_r3.py refuses an F run whose timed,
  same-bytes and alias arms are not exactly this set, or whose plan is not bound (`x_r3_decision`
  sha256) to the R3 decision that named W.
- "Latest estimate against O4-r1": the last of campaigns A (screen.json), A2, H, B1 (arm o4r2 =
  O4-r2) and B1-retry (arm o4r2 = O4-r2b) that estimated the arm. B2 and H2 used O4-r2b as the
  reference and give no estimate against O4-r1.

  | arm | campaign | pooled d | UB95 | in F |
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
  | c7b3 | A | same bytes as O4-r1 on every workload | — | yes (an alias of o4) |
  | b3l | A | −2.316% | −1.166% | **no** |
  | w7bd_b3 | H | −3.566% | −2.951% | **no** |
  | o4_hotgraph | A | −3.634% | −2.425% | **no** |
  | o4_hostall | H | −4.509% | −3.938% | **no** |
  | p7 | A | −4.580% | −3.777% | **no** |
  | fl3 | A | −14.697% | −13.713% | **no** |
  | s6 | A | −16.668% | −15.512% | **no** |
  | o4_pinned | — | identity aid (O4-r1 via `--pipeline O4.toml`), never a campaign arm | — | no |

  In F by rule: o4_pwhopr (H: +0.930%, UB +1.425%), o4r2 (B1: +0.486%, UB +1.017%), o4r2b
  (B1-retry: +0.183%, UB +0.681%), o4r3b/c/d (new) and o4. `r3/make_r3_plans.py criterion`
  recomputes this table from the decide outputs and refuses to plan if it differs from
  decide_r3.py's `F_CRITERION`.
- VALID as in R3.
- **BEST** (decide.py's `best`, reference W): every arm and scope (each timed workload, and pooled
  when timed on two or more) gets a one-sided p for "A faster than W"; one Holm family over all of
  them at 0.05; an alias scope takes its target's result and is not a second hypothesis. BEST
  holds iff nothing is rejected.

### Adoption

W is adopted as the new O4 iff W passed ADD-confirm in R3 (final) and BEST holds in F, with VALID
ok or `reference_bias` recorded (no `pending_rerun` in R3 or F). Otherwise O4 stays at r1 and the
outcome is reported with its numbers.

## Interpretation choices (made here, before any data)

1. **G0** is a precondition of ADD-confirm (O4-design 3.7's ADD and B1's ADD-confirm include it;
   the step's statement of the rule did not repeat it). It cannot change the outcome today: every
   R3 arm has g0 true on all three workloads.
2. **VALID** keeps decide.py's per-workload rerun selection (the step says "rerun that campaign").
3. **The Holm family** counts distinct pooled estimates; with today's bytes that is all four
   variants.
4. **Arm names**: o4r3a is timed as the existing `o4_pwhopr` build (o4r3a.toml's uart rebuild has
   its bytes: `rebuild/o4r3a` = `c4eae805…`); F uses the gated build names `o4r2` (O4-r2) and
   `o4r2b` (O4-r2b), so every timed arm is in the gates files under its own name.
5. **`--pgo-launch`** is kept on the o4r3b / o4r3d lines (o4_pwhopr's line with only --pipeline
   and --store changed; scopt does not require dropping it). Without host-inline it is inert: the
   uart build without it (`rebuild/o4r3b-nopgo`) has o4r3b's bytes (`7f93d0d7…`).
6. **F is conditional**: its plans are written only from a final R3 decision with a winner
   (`make_r3_plans.py F --r3 decide-R3.json`), so the F arm set above is registered now and the
   F plans later.

## How to run

```
# R3 (after the load check of O4-design 3.9): one driver per workload, concurrently
source /data/scfix-o4/<w>-w/env.sh && $PY /data/scfix-o4/driver/compare_rtos.py \
  /data/scfix-o4/plans/R3/<w>.json /data/scfix-o4/<w>/timing-R3 > /data/scfix-o4/<w>/timing-R3.log 2>&1
# decide
$PY -B /data/scfix-o4/driver/decide_r3.py R3 packet/timing-R3 uart/timing-R3 cli/timing-R3 \
  --gates gates/all.json --out R3.json
# F (only with a final winner)
$PY -B r3/make_r3_plans.py F --r3 R3.json      # plans/F/<w>.json; then run as above into timing-F
$PY -B driver/decide_r3.py F packet/timing-F uart/timing-F cli/timing-F --r3 R3.json \
  --gates gates/all.json --out F.json
```

A driver that fails with the known launch error "allocate supercache code buffer: File exists"
is not a VALID rerun: that campaign is rerun with the same plans and seed (all three workloads
together), and the failed attempt is kept as `timing-<C>.attemptN`, as campaign H did.

## Tests (`test_decide_r3.py`, 18 passed; with test_decide.py, test_driver.py and test_make_plan.py: 78 passed)

- decide_r3 imports decide.py's registered functions and methods unchanged (`R3Run.estimate is
  decide.Run.estimate`, etc.) and checks decide.py's sha256.
- Refusals: another `x_decide_r3_sha256` or `x_decide_sha256`, another `x_round` or
  `x_campaign`, the wrong seed, blocks, burn-in or settle, an arm set that is not R3's (or F's),
  an F reference that is not W, an F run without `--r3`, with another winner, or bound to another
  R3 decision.
- The choice: none passing (blocked); highest d outside the band; X1+X4 preferred within 0.3%;
  then no A3; the band edge (0.0269 vs 0.030 is outside).
- ADD-confirm's three parts separately (LB_w below log 0.98 on one workload; g0 false or absent;
  Holm step-down at rank 2 with 0.0167 < p < 0.05).
- Aliases end to end: a whole alias is one Holm hypothesis and the identical-d tie goes to the
  variant without A3; a partial alias reads the target's estimate on the aliased workloads; a
  same-bytes-as-O4-r1 workload is left out of LB_w and the pooled estimate.
- VALID: a biased workload is pending_rerun (rerun seed 20261301 listed, decision provisional);
  its biased rerun gives reference_bias and a final decision; the selection equals
  decide.select_runs's on the same runs.
- F: the registered arm set and criterion; BEST holds on null data and W is adopted; one arm 5%
  faster than W is rejected and nothing is adopted; W without ADD-confirm in R3 is not adopted.
