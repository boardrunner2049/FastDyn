# decide.py, registration 2

Date: 2026-11-26, 01:56 EST. Made by the campaign-preparation step, before any O4 timing.

| | sha256 |
|---|---|
| Registration 1 (superseded) | `75e7b12e4e2cb252bc359208cf2c469c9b384dfca3f034f72c187fb5b0e84b52` |
| **Registration 2 (this one)** | **`0b7247476a39abade599000de4e2555c7edb1167b57d0989f4bac54cbaf1a438`** |

- `DECIDE.sha256` now holds registration 2. Every plan carries it as `x_decide_sha256`, and
  `make_plan.py` refuses to write a plan when decide.py differs from it.
- Registration 1 is archived byte for byte in `prereg/decide.registration1.py` and
  `prereg/DECIDE.registration1.sha256`.
- The lead decided to switch to the Student-t reference before this step began. This step
  carried it out.

## No O4 timing data existed

The check was run at 01:54 EST, after the plans were written and before this file:

```
find /data/scfix-o4 -name observations.jsonl
```

It lists only the six round-2 copies in
`evidence/r1-seed/{timing-1,timing-bounded}/{packet,uart,cli}/observations.jsonl`. They were
written on 2026-11-25 at 23:59, and each has the same sha256 as its original under
`/data/scfix-r2/<w>/{timing-1,timing-bounded}/`.

- No driver output directory exists: `/data/scfix-o4/{packet,uart,cli}/timing-*` matches
  nothing.
- No plan existed before this step. `/data/scfix-o4/plans/` was created at 01:50 by
  `make_plan.py A`.
- `validate_plans.py` ran decide.py on synthetic null observations. It wrote them only in a
  scratch directory outside `/data/scfix-o4`. They are not timing data.

No O4 timing data existed when this registration was made.

## Change 1: Student-t reference (the lead's decision)

### Why

decide.py reads HL / SE_boot (Hodges-Lehmann over the bootstrap SE). At 10-20 blocks that
statistic has heavier tails than a normal. The infra step showed this in `sim/fwer_sim.py`,
`sim/fwer_t.py` and `sim/calib_z.py`.

`sim/fwer_reg2.py` re-ran the same null data with this decide.py's own functions: the same
`random.Random(42)` stream, the same bootstrap seeds and 2,000 rounds. Its output is in
`sim/fwer_reg2.out`. The model is a null 15-arm campaign A with o4 and o4-aa, 20/10/10 blocks
after burn-in, and round 2's pair SDs.

| null 15-arm campaign A | normal (reg. 1) | Student-t (reg. 2) | nominal |
|---|---:|---:|---:|
| BEST false "faster than O4", 400 sims (the recorded runs) | 0.203 | **0.052** | ≤ 0.05 |
| the same, 2,000 sims | 0.188 | **0.051** | ≤ 0.05 |
| screen: null item has UB99_w < 0 on some workload, 400 / 2,000 sims | 0.0515 / 0.0551 | 0.0237 / 0.0263 | ≤ 0.0297 |
| single one-sided 5% tests, packet / uart / cli, 2,000 sims | 0.060 / 0.066 / 0.062 | 0.052 / 0.050 / 0.047 | 0.05 |

The first 400 simulations reproduce the recorded `fwer_sim.out` (0.203 and 0.0515) and
`fwer_t.out` (0.052) exactly.

### What changed

| quantity | registration 1 | registration 2 |
|---|---|---|
| p_w | Φ(−d_w / SE_w) | P(T_df > d_w / SE_w), df_w = n_w − 1 |
| LB_w, UB_w | d_w ∓ 1.645 SE_w | d_w ∓ t(0.95, df_w) SE_w |
| UB99_w (screen) | d_w + 2.326 SE_w | d_w + t(0.99, df_w) SE_w |
| pooled p, LB, UB | normal | t with Welch-Satterthwaite df = (Σ 1/SE_w²)² / Σ (1 / (SE_w⁴ df_w)) |
| minimum blocks | 1 after burn-in | 2 after burn-in, so that df ≥ 1 |

- n_w is the number of timed blocks, excluding burn-in block 0. For campaign A that gives
  df 19 / 9 / 9; for A2, H and B1 it gives 39 / 79 / 39.
- Each estimate row now also records `df`, `t95` and `t99`.
- The schema is `scfix-o4.decide/2`. The constants record `reference_distribution` and
  `bound_quantiles` in place of `z95` and `z99`.

These are unchanged:

- d_w (Hodges-Lehmann) and SE_w (SD of 20,000 block-bootstrap HL values, seed
  `f"{seed}:{w}:{X}"`, floored at 1e-9);
- inverse-variance pooling of d and SE;
- Holm at α 0.05, with the same families;
- the thresholds log 0.98 and log 0.99;
- the groups and composition;
- B1 and B2;
- VALID's two-sided 99% bootstrap percentile interval and its rerun at seed + 100.

### Implementation

The t functions are pure Python; decide.py imports nothing outside the standard library.

- `t_sf` uses the regularized incomplete beta, computed by a modified-Lentz continued
  fraction to 1e-15.
- `t_ppf` bisects on `t_sf` to 1e-12 relative.

They were checked against scipy 1.11.4 (`/usr/bin/python3`, used only by a throwaway check
script). The grid was integer df 2-200 plus 12 fractional (Welch-like) df, 252 t values
from −40 to 40, and 21 p values from 0.0005 to 0.9995.

- `t_sf` and `t_cdf`: max absolute error 5.1e-14.
- `t_ppf`: max absolute error **1.55e-8**, at p 0.99 and df 2. That error is scipy's own.
  The closed form at df 2, (2p − 1) / √(2p(1 − p)), gives 6.9645567342833. decide.py is
  9.3e-14 from it and scipy is 1.55e-8 from it.
- Overall: max absolute error 1.55e-8, inside the required 1e-6.

The unit tests check df 1, 2 and 4 against their closed forms, and the tabulated quantiles
t(0.95/0.99, 9) and t(0.95/0.99, 19) against scipy's values.

## Change 2: same-bytes aliases between timed arms

On uart and cli, `o4_noregions` (X3) has exactly the round-2 s6b3 anchor's cache.sc:
`a8f1020e…` on uart and `a3b91907…` on cli. Registration 1 dropped only arms whose bytes
equal the reference's, so these bytes would have been timed twice and compared with
themselves.

**make_plan.py.** Within a workload, a set of non-reference arms with one cache.sc is timed
once.

- The arm kept is chosen in this order: a decision item (add-on, removal or fidelity), then a
  challenger, then an anchor, then the campaign's arm order. Here it keeps `o4_noregions`.
- The others are recorded as `x_same_cache_bytes.aliases`: `{"s6b3": {"uart":
  "o4_noregions", "cli": "o4_noregions"}}`.
- Comparisons are rewritten onto the kept arm. `[s6b3, b3l]` becomes `[o4_noregions, b3l]`
  on uart and cli.
- Comparisons of identical bytes are dropped (`[s6b3, o4_noregions]`). So are repeats:
  `[s6b3, o4]` is already `[o4, o4_noregions]`.

**decide.py.** On an alias workload, X's row is the kept arm's estimate: the same numbers
and the same bootstrap seed. s6b3's anchor comparison on uart and cli is therefore read off
o4_noregions.

- In BEST, an alias scope is not a second hypothesis. It takes the kept arm's Holm result,
  and its rows carry `alias_of`.
- A pooled scope is an alias only if every workload of X is an alias of one arm Y, and Y's
  own row covers exactly those workloads.
- s6b3's pooled scope stays its own hypothesis, because packet's s6b3 (`25d8d8e6…`) is
  timed separately.

decide.py refuses three cases: an alias arm that is also timed on that workload, an alias
whose target is not timed there, and runs that disagree on the aliases.

## Change 3 (a deliberate deviation from O4-design §3.5): campaign A2 in combined mode

O4-design §3.5 planned one head-to-head plan per surviving candidate, `[o4, X]` at seed
20261126 + k. Registration 2 instead writes one plan per workload. It contains o4, o4-aa
and every A2 candidate whose bytes differ from o4 on that workload.

- Blocks are 41 / 81 / 41: 40 / 80 / 40 plus burn-in.
- The seed is **20261130**, distinct from A (…26), B1 (…27), H (…28) and B2 (…29). A VALID
  rerun uses seed + 100.
- The plan's `x_candidates` is the screen's `a2_order`. A workload where every candidate has
  o4's bytes gets no plan.

**What stays as designed:**

- Each `[o4, X]` pair keeps its blocks and the same pooled o4 / o4-aa reference, so
  per-pair power is as designed.
- ADD's Holm family is unchanged: the add-ons decided in A2 and H.
- BEST's A2 family is unchanged: every A2 arm and scope.
- Every candidate is still decided only on the workloads where its bytes differ.

**What differs:**

- The candidates share one run's o4 / o4-aa blocks, so their estimates are correlated
  through the reference. Holm's step-down is valid under any dependence.
- A biased VALID result reruns the whole workload's combined plan.
- A block holds more arms, so it spans more time. If all 13 A-screen candidates survived, a
  block would hold up to 13 arms with these builds (packet and uart; 11 on cli), instead of
  3. The noise basis in §3.3 already comes from round 2's 17-arm blocks.

**Gain:** about 2.5× less wall time than one plan per candidate.

**Code.**

- decide.py accepts an A2 run with `x_candidates` (a combined unit) or `x_candidate`
  (registration 1's form, still accepted).
- The screen now reports `a2_mode: "combined"` and `a2_seed: 20261130`, and its
  `a2_order` entries no longer carry per-candidate seeds.
- `make_plan.py A2` writes `plans/A2/<w>.json`. `--per-candidate` still writes the
  original per-candidate plans; P4b's `o4_tasks` would need them as its own A2 unit.

## Files at registration

| file | sha256 |
|---|---|
| decide.py | `0b7247476a39abade599000de4e2555c7edb1167b57d0989f4bac54cbaf1a438` |
| make_plan.py | `048da1778f3b26e3457c221ffdfb854c2513f7ea012adc716dcfea225f4c0e05` |
| compare_rtos.py (unchanged) | `f7046c86be09c37d7b6b12665cfc0d4b427696aedd1ff956dea319de277d7736` |
| o4lib.py (unchanged) | `50b49ef0a052795d4aa47f2d5c9f7599528ce6e5ca02a4561cc72aa6e59afdb0` |
| test_decide.py | `ccf6572f86344a96a4cbc0f8d8b886c704409bd69083753d945814e60e627383` |
| test_make_plan.py | `d6af72095b0ca6e0b9048697fed216705b1f19bf1d08386b73bee6d769404bf0` |
| test_driver.py (unchanged) | `57b3c35dcadbcf350dba37db64a1498724f9c0f26185a0266b6aecc160ec2f81` |
| make_gates.py (new: the G0 files `/data/scfix-o4/gates/{packet,uart,cli,all}.json`) | `1b735d59f1f9adea2bf693565f64f6d621946fb38e3f816e2c2712280bcbebd8` |
| validate_plans.py (new: pre-flight without QEMU) | `a2d420e7e529b0ec111f1001115e3c2b5ea5864ab75b31ca37bd19aa369a9890` |
| sim/fwer_reg2.py / .out | `4c2b9958…0d49d34` / `dd69331e…5134cc` |

**Tests.** test_decide.py, test_driver.py and test_make_plan.py: **60 passed**. Before this
step there were 39 tests, all passing.

- The new tests cover:
  - the t closed forms and quantiles;
  - df = n − 1 in estimates and Welch df in pooling;
  - a null calibration;
  - aliases in decide.py and make_plan.py;
  - combined A2;
  - B2's alias of c7b3 to o4.
- The round-2 replay now checks |d / SE| < t(0.99, df) with df 18 / 8 / 8.
