# Amendment to the O4 adoption rule: "wins in most cases"

Written by the lead at 2026-11-26 ~06:27 EST, at the user's instruction. Campaign F's timing was still running then: packet was at 482 of 558 runs, while uart and cli had finished their runs. No F estimate had been computed or read by the lead, and decide_r3.py had not been run on F.

## The user's words (2026-11-26)

> "i think you took the instructions too literally. We can't have a perfect configuration, that always win. Since its workload dependent. Win in most cases is fine"

## What changes

The strict BEST rule asked that no arm be faster than O4 on any single workload or pooled. It is replaced for adoption, and for O4's standing contract, by the rule below. The registered tools stay as they are: decide.py (`0b724747…`) and decide_r3.py (`e6554ab0…`) still compute every estimate. The new rule reads their output. It does not change how any estimate is computed.

### BEST-most (adoption check against reference W)

The inputs are decide_r3.py's per-arm estimates against W. They are already in F.json: per-workload and pooled d, SE, one-sided p for "arm faster than W", and Student-t df.

1. **Pooled family.**
   - The hypotheses are one per arm: "arm faster than W, pooled over the workloads where its bytes differ."
   - Holm at 0.05 is applied across all F arms except W and W-aa.
   - An alias takes its target's result.
2. **Per-workload family.**
   - The hypotheses are one per arm and timed workload: "arm faster than W on w."
   - A separate Holm at 0.05 is applied across all of them.
3. **The rule.**
   - **Holds** if no arm is rejected in (1), and no arm is rejected on a majority of the workloads where its bytes differ from W in (2). A majority is at least 2 of 3, or 2 of 2.
   - **Workload-specific wins.** An arm that is faster than W on a single workload only is a workload-specific win. It is recorded with its numbers in TIMING.md, docs/O4.md and docs/o4-evidence.json, and it does not block adoption.
4. **Adoption.**
   - W is adopted as the new O4 if W passed ADD-confirm in R3 and BEST-most holds in F, with VALID ok or reference_bias recorded.
   - If the strict BEST of PREREG-R3.md also holds, the docs say so. If it does not, the docs name each workload-specific win.

### Including an optimization in O4 (the standing contract, replacing "faster on every workload")

An optimization goes into O4 when two things hold:
- O4 with it is faster overall: the pooled one-sided test against O4 rejects at 0.05.
- It is not slower on most workloads: it is not shown slower (UB95_w < 0) on a majority of the workloads where it changes bytes.

A gain on one workload, with losses or no effect elsewhere, is a workload-specific option. It is documented, but O4 does not include it. A config that beats O4 on one workload only does not have to be absorbed.

## Effect on R3

None. R3's ADD-confirm and winner choice (o4r3d) were registered before R3 data and stand as decided. Its per-workload non-inferiority guard (LB95_w > log 0.98) is stricter than the new contract, and all four variants passed it.
