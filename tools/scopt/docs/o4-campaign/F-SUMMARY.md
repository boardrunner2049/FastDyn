# Campaign F: result

Timed 2026-11-26 06:12-06:28 EST (`hygiene/F/launch.log:1-6`, `r3/runinfo-F.json`); decided at 06:30 EST by this step. The registered decider `driver/decide_r3.py` (sha256 `e6554ab034b85d021df575b2eb401c1cf288e26614e37a66c9477c517409969c`, the digest in `driver/DECIDE_R3.sha256` and `driver/PREREG-R3.md:5`) was run unchanged on F. It imports the registered `driver/decide.py` (`0b7247476a39abade599000de4e2555c7edb1167b57d0989f4bac54cbaf1a438`, `driver/DECIDE.sha256`), and neither file was changed. The amendment's BEST-most (`driver/AMENDMENT-F-most-cases.md`, sha256 `01b64b25…`, as in `driver/AMENDMENT-F.sha256`) was then applied by `driver/best_most.py`. That script only reads F.json, and it computes no estimate of its own. Its two Holm step-downs use decide.py's `holm` (`driver/decide.py:350-362`).

**Verdict: o4r3d (A5 + A3 + X1 + X4) is adopted as the new O4, under both the strict rule and the amendment.**
- VALID passed on all three workloads, with no rerun and nothing provisional.
- The strict BEST of PREREG-R3.md holds: none of the 58 hypotheses is rejected.
- BEST-most holds: none of the 19 pooled hypotheses and none of the 40 per-workload hypotheses is rejected, and there is no workload-specific win.
- No arm is faster than o4r3d under either rule.

Sources:
- `F.stdout.txt:1-6`
- `F.json:1339-1342` (`.F.best`) and `F.json:3180-3188` (`.adoption`)
- `F-most.json:1542-1545` (`.best_most`) and `F-most.json:1552-1564` (`.adoption`)

## Outcome

1. **VALID: ok on every workload** (`F.stdout.txt:2-4`, `F.json:125-170`).
   - The 99% interval of a_w = HL log(T(o4r3d) / T(o4r3d-aa)) contains 0 on packet, uart and cli.
   - `rerun_required: []`, `provisional: false`, no `reference_bias` workloads and no warnings (`F.json:171-174`).
   - **No rerun was needed, so none was run.**
   - Packet's A/A gap is the largest: −1.45%, 99% [−2.98%, +0.27%]. The averaged reference (½(log T(ref) + log T(aa)), `driver/decide.py:473-475`) therefore sits about 0.7 points slower than o4r3d alone on packet. This is inside VALID's tolerance. The descriptive direct pairs below show its size.
2. **Strict BEST (PREREG-R3.md:145-148; `decide_r3.f_rule`, `driver/decide_r3.py:382-387`): holds.**
   - One Holm family of 58 hypotheses (every arm × timed scope, plus pooled when an arm differs on 2 or more workloads; alias scopes take their target's hypothesis).
   - Nothing is rejected, so `faster_than_W` is `[]` (`F.json:1339-1342`).
   - The smallest p is o4_chained on packet: p 0.0404, against a rank-1 threshold of 0.00086.
3. **BEST-most (AMENDMENT-F-most-cases.md:17-29): holds** (`F-most.json:1542-1545`).
   - The pooled family has 19 hypotheses: the 20 F arms other than W and W-aa, with c7b3 taking o4's pooled hypothesis (decide.py's `pooled_alias_of`). Its smallest p is o4r3c's: p 0.4051, against a threshold of 0.00263.
   - The per-workload family has 40 hypotheses (every arm × workload where the bytes differ, minus alias workloads). Its smallest p is o4_chained on packet: p 0.0404, against a threshold of 0.00125.
   - Nothing is rejected in either family, so no arm blocks and there is no workload-specific win (`workload_specific_wins: []`).
   - The verdict is the same under both readings of "majority" for an arm that differs on one workload only (o4r3b), because nothing is rejected (`holds_if_one_of_one_is_not_a_majority: true`).
4. **Adoption** (PREREG-R3.md:150-154; AMENDMENT-F-most-cases.md:27-29). W = o4r3d meets every condition:
   - It passed ADD-confirm in R3 (`R3.stdout.txt:8`), and R3 is final (`R3.stdout.txt:9`).
   - Strict BEST holds and BEST-most holds.
   - F is final.
   - **adopted = true under both rules** (`F.json:3187`, `F-most.json:1560`).
   - Under the amendment's clause 4, the docs say "strict BEST (PREREG-R3.md) also holds" (`F-most.json:1563`), and they have no workload-specific win to name.
5. **o4r3c vs o4r3d (the registered row; o4r3c = o4r3d with host-inline and the SPEC bundle restored, `design/pipelines/o4r3d.toml:25-34`, `PREREG-R3.md:43-44`):**
   - Pooled **+0.10%** (SE 0.42%, df 59.8), one-sided 95% [−0.60%, +0.80%], p 0.4051.
   - Per workload: packet +0.06% (p 0.469), uart −0.13% (p 0.557, alias of o4_pwhopr), cli +0.25% (p 0.348, alias of o4_pwhopr).
   - Holm rank 10 of 58 in the strict family and rank 1 of 19 in the BEST-most pooled family. It is not rejected in either.
   - **The two are not distinguishable.** R3's descriptive direct pair gave the same answer (+0.11% [−0.45%, +0.67%], `R3-SUMMARY.md:105`).
6. **o4r3c vs O4-r1 (o4).** F has no registered row for this pair, because F's reference is o4r3d. Three views:
   - **Registered, campaign R3** (`R3.stdout.txt:7`): pooled **+1.06%**, LB95 +0.61%, p 0.00007. Per workload: packet +0.76%, uart +0.43%, cli +1.74%.
   - **F, difference of the two registered rows against o4r3d** (arithmetic, no p): pooled +0.10 − (−1.01) = **+1.11%**. Per workload: packet +0.06 − (−1.55) = +1.61%; uart −0.13 − (+0.73) = −0.86%; cli +0.25 − (−1.14) = +1.39%.
   - **F, descriptive direct pair** (see "Direct pairs" below; not part of any rule): pooled **+1.11%** (SE 0.51%), one-sided 95% [+0.25%, +1.98%], p 0.017. Per workload: packet +1.73%, uart −0.56%, cli +1.39%.
   - **All three views agree: o4r3c is about 1.1% faster than O4-r1 pooled. It is faster on packet and cli, and within noise on uart.**
7. **o4r3d vs O4-r1, re-measured in F.**
   - O4-r1's registered row against o4r3d is pooled **−1.01%** (SE 0.45%), one-sided 95% [−1.76%, −0.26%].
   - So O4-r1 is shown slower than o4r3d pooled, on packet (−1.55%, UB −0.22%) and on cli (−1.14%, UB −0.08%). Uart is +0.73% with UB +2.80%: not shown either way.
   - c7b3, o4_nocp (packet) and o4_nospec (packet) are aliases of o4 and read the same numbers.
   - **F independently confirms R3's finding that o4r3d is faster than O4-r1** (R3: +0.76% pooled, `R3.stdout.txt:8`).
8. **Fastest pooled arm.** No arm is faster than o4r3d under either rule.
   - By point estimate, the highest pooled d against o4r3d is **o4r2** (O4-r2: A5 + I1 + X1 + X4) at **+0.11%** (0.00106), p 0.427, one-sided 95% [−0.85%, +1.06%]. Its pool covers packet and uart only, because its cli bytes equal o4r3d's.
   - Next is **o4r3c** at **+0.10%** (0.00101) over all three workloads, p 0.405.
   - Both are within one SE of 0. **The registered fastest, o4r3d, stands.**
   - Every other arm has a negative pooled d against o4r3d. Ten are shown slower pooled (UB95 < 0): o4r2b, fl3b3, o4 and its alias c7b3, o4_chained, o4_nocp, o4_nospec, o4_isb, s6b3 and o4_noregions.
9. **Near-miss worth recording: o4_chained (A1) on packet**, +1.54% with one-sided 95% [+0.09%, +2.98%] and unadjusted p 0.040.
   - It is not rejected: the threshold is 0.00086 in the strict family and 0.00125 in the per-workload family. So it is not a workload-specific win under the amendment.
   - It is shown slower on uart (−2.38%, UB −1.33%) and on cli as an alias of o4 (−1.14%), and pooled (−1.09%, UB −0.45%).
   - Under the amendment's standing contract (`AMENDMENT-F-most-cases.md:31-37`) it is not an O4 candidate.

## Campaign F run

| | packet | uart | cli |
|---|---|---|---|
| plan (registered) | `plans/F/packet.json` (`129f925c…`) | `plans/F/uart.json` (`8bb0c1ff…`) | `plans/F/cli.json` (`2abcfae5…`) |
| driver copy | `packet/timing-F/plan.json` (`cb8d2d66…`; adds `aa_control_resolved` and the `o4r3d-aa` arm, otherwise equal) | `uart/timing-F/plan.json` (`17f93373…`) | `cli/timing-F/plan.json` (`4919e228…`) |
| CPU, seed | 10, 20261202 | 20, 20261202 | 12, 20261202 |
| blocks (burn-in + timed) | 31 (1 + 30) | 21 (1 + 20) | 21 (1 + 20) |
| timed arms incl. A/A | 19 | 15 | 12 |
| observations | 589 (1 case) | 630 (2 cases) | 504 (2 cases) |
| window (block 0 end to last block end) | 06:17:12-06:28:06 | 06:16:13-06:24:13 | 06:14:23-06:15:35 |
| slow runs (`r3/runinfo-F.json`) | none | 2 timed (o4 help ×1.314 in block 3; o4_nospec help ×1.309 in block 14) | 19, all in burn-in block 0 (max ×17.8, o4 help) |

- Every plan is bound to the R3 decision: `x_r3_decision.sha256 = f5449dac…`, the sha256 of `R3.json`.
- Every plan names the winner: `x_winner = o4r3d`.
- decide_r3.py checked that the timed, same-bytes and alias arms equal `f_arms("o4r3d")` (`driver/decide_r3.py:207-247`).
- Same bytes as o4r3d, so those workloads are dropped: o4r3b on uart and cli, and o4r2 on cli.
- Aliases: c7b3 → o4 on every workload; o4_nocp and o4_nospec → o4 on packet; o4_legacy, o4_rank and o4_tasks → o4 on uart and cli; o4r3c → o4_pwhopr on uart and cli; s6b3 → o4_noregions on uart and cli; o4_chained and o4_isb → o4 on cli.
- Hygiene (`hygiene/F/after.json`): the QEMU binary is unchanged (aacca05, `951775b0…`), and fd-int is unchanged (b35cdb6, clean). The find-newer audit of the read-only trees is empty.

## VALID (reference o4r3d vs A/A o4r3d-aa; decide.py's two-sided 99% bootstrap interval)

| workload | attempt | blocks | a_w = HL log(o4r3d / o4r3d-aa) | SE | 99% interval | biased |
|---|---:|---:|---:|---:|---|---|
| packet | 1 | 30 | −1.45% | 0.62% | [−2.98%, +0.27%] | no |
| uart | 1 | 20 | +0.35% | 1.19% | [−2.59%, +3.43%] | no |
| cli | 1 | 20 | +0.88% | 1.02% | [−1.81%, +3.35%] | no |

## Every arm against the reference o4r3d (decide_r3.py / decide.py estimates, from F.json `.estimates` and `.F.best.rows`)

The estimates follow the registered method:
- d is the Hodges-Lehmann estimate over the timed blocks of ½(log T(o4r3d) + log T(o4r3d-aa)) − log T(arm). Positive means the arm is faster than o4r3d.
- SE is from a 20,000-round block bootstrap.
- The reference distribution is Student-t with df = n − 1.
- Pooled estimates use inverse-variance weights with Welch df, over the workloads where the arm's bytes differ from o4r3d's.
- LB95 and UB95 are one-sided 95% bounds. p is P(T > d/SE), the one-sided p for "arm faster than o4r3d".

The Holm columns give rank and threshold in three families:
- the strict BEST family of 58;
- the BEST-most pooled family of 19, on pooled rows;
- the BEST-most per-workload family of 40, on workload rows.

An alias row is its target's hypothesis and carries the target's numbers and ranks. Verdict is decide.py's: `faster_than_W` if rejected, `slower_than_W` if UB95 < 0, otherwise `not_shown`. Rows are sorted by pooled d.

### Compact view (d against o4r3d; "= W" means the same bytes as o4r3d; ↓ means UB95 < 0, shown slower)

| arm (item) | pooled d | pooled LB95 / UB95 | p | packet | uart | cli |
|---|---:|---|---:|---:|---:|---:|
| **o4r3d (W)** | 0 (reference) | | | 0 | 0 | 0 |
| o4r2 (O4-r2) | +0.11% | -0.85% / +1.06% | 0.4266 | -0.15% | +0.41% | = W |
| o4r3c (o4r3c) | +0.10% | -0.60% / +0.80% | 0.4051 | +0.06% | -0.13% (=o4_pwhopr) | +0.25% (=o4_pwhopr) |
| o4_pwhopr (A5) | -0.18% | -0.87% / +0.50% | 0.6708 | -0.71% | -0.13% | +0.25% |
| o4_rank (A3) | -0.33% | -1.07% / +0.40% | 0.7740 | +0.44% | +0.73% (=o4) | -1.14% (=o4) ↓ |
| o4_tasks (A2a) | -0.56% | -1.27% / +0.14% | 0.9065 | -0.28% | +0.73% (=o4) | -1.14% (=o4) ↓ |
| o4_legacy (A8) | -0.57% | -1.21% / +0.06% | 0.9335 | -0.41% | +0.73% (=o4) | -1.14% (=o4) ↓ |
| o4_nohi (X1) | -0.67% | -1.36% / +0.02% | 0.9446 | -0.95% | +0.22% | -1.59% ↓ |
| o4_min2 (A7) | -0.71% | -1.43% / +0.01% | 0.9468 | -1.10% ↓ | -1.08% | +0.48% |
| o4r2b (o4r2b) | -0.72% | -1.41% / -0.04% | 0.9583 | +0.47% | -1.08% | -1.80% ↓ |
| o4r3b (o4r3b) | -0.75% | -1.98% / +0.49% | 0.8443 | -0.75% | = W | = W |
| fl3b3 (C1) | -0.78% | -1.49% / -0.07% | 0.9644 | +0.10% | -1.29% | -1.26% ↓ |
| fl3b3p (C1p) | -0.83% | -1.70% / +0.04% | 0.9426 | -0.10% | +0.57% | -2.30% ↓ |
| c7b3 (c7b3) | -1.01% | -1.76% / -0.26% | 0.9861 | -1.55% (=o4) ↓ | +0.73% (=o4) | -1.14% (=o4) ↓ |
| o4 (O4-r1) | -1.01% | -1.76% / -0.26% | 0.9861 | -1.55% ↓ | +0.73% | -1.14% ↓ |
| o4_chained (A1) | -1.09% | -1.73% / -0.45% | 0.9969 | +1.54% | -2.38% ↓ | -1.14% (=o4) ↓ |
| o4_nocp (X2) | -1.14% | -1.77% / -0.51% | 0.9981 | -1.55% (=o4) ↓ | -0.80% | -1.23% ↓ |
| o4_nospec (X4) | -1.16% | -1.89% / -0.44% | 0.9953 | -1.55% (=o4) ↓ | -0.79% | -1.15% |
| o4_isb (I1) | -1.18% | -1.82% / -0.53% | 0.9984 | -1.84% ↓ | -0.03% | -1.14% (=o4) ↓ |
| s6b3 (s6b3) | -1.28% | -1.94% / -0.62% | 0.9991 | -1.05% ↓ | -0.38% (=o4_noregions) | -3.14% (=o4_noregions) ↓ |
| o4_noregions (X3) | -1.38% | -2.00% / -0.75% | 0.9998 | -1.28% ↓ | -0.38% | -3.14% ↓ |

### Full table

| arm (item) | scope | timed as | d | SE | df | LB95 | UB95 | p | strict Holm rank / thr (58) | BEST-most Holm rank / thr | verdict |
|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|
| o4r2 (O4-r2) | packet | o4r2 | -0.15% | 0.78% | 29.0 | -1.47% | +1.17% | 0.5779 | 17 / 0.00119 | 15 / 0.00192 | not_shown |
| o4r2 (O4-r2) | uart | o4r2 | +0.41% | 0.84% | 19.0 | -1.04% | +1.86% | 0.3157 | 7 / 0.00096 | 7 / 0.00147 | not_shown |
| o4r2 (O4-r2) | cli | same bytes as o4r3d | | | | | | | | | same_bytes |
| o4r2 (O4-r2) | pooled | pooled over packet+uart | +0.11% | 0.57% | 47.1 | -0.85% | +1.06% | 0.4266 | 11 / 0.00104 | 2 / 0.00278 | not_shown |
| o4r3c (o4r3c) | packet | o4r3c | +0.06% | 0.71% | 29.0 | -1.15% | +1.26% | 0.4686 | 13 / 0.00109 | 11 / 0.00167 | not_shown |
| o4r3c (o4r3c) | uart | alias of o4_pwhopr | -0.13% | 0.90% | 19.0 | -1.68% | +1.42% | 0.5574 | 16 / 0.00116 | 14 / 0.00185 | not_shown |
| o4r3c (o4r3c) | cli | alias of o4_pwhopr | +0.25% | 0.63% | 19.0 | -0.84% | +1.34% | 0.3482 | 8 / 0.00098 | 8 / 0.00152 | not_shown |
| o4r3c (o4r3c) | pooled | pooled over packet+uart+cli | +0.10% | 0.42% | 59.8 | -0.60% | +0.80% | 0.4051 | 10 / 0.00102 | 1 / 0.00263 | not_shown |
| o4_pwhopr (A5) | packet | o4_pwhopr | -0.71% | 0.68% | 29.0 | -1.86% | +0.44% | 0.8486 | 25 / 0.00147 | 21 / 0.00250 | not_shown |
| o4_pwhopr (A5) | uart | o4_pwhopr | -0.13% | 0.90% | 19.0 | -1.68% | +1.42% | 0.5574 | 16 / 0.00116 | 14 / 0.00185 | not_shown |
| o4_pwhopr (A5) | cli | o4_pwhopr | +0.25% | 0.63% | 19.0 | -0.84% | +1.34% | 0.3482 | 8 / 0.00098 | 8 / 0.00152 | not_shown |
| o4_pwhopr (A5) | pooled | pooled over packet+uart+cli | -0.18% | 0.41% | 60.9 | -0.87% | +0.50% | 0.6708 | 19 / 0.00125 | 3 / 0.00294 | not_shown |
| o4_rank (A3) | packet | o4_rank | +0.44% | 0.74% | 29.0 | -0.82% | +1.69% | 0.2786 | 4 / 0.00091 | 4 / 0.00135 | not_shown |
| o4_rank (A3) | uart | alias of o4 | +0.73% | 1.20% | 19.0 | -1.33% | +2.80% | 0.2731 | 3 / 0.00089 | 3 / 0.00132 | not_shown |
| o4_rank (A3) | cli | alias of o4 | -1.14% | 0.61% | 19.0 | -2.20% | -0.08% | 0.9611 | 38 / 0.00238 | 28 / 0.00385 | slower_than_W |
| o4_rank (A3) | pooled | pooled over packet+uart+cli | -0.33% | 0.44% | 52.4 | -1.07% | +0.40% | 0.7740 | 21 / 0.00132 | 4 / 0.00313 | not_shown |
| o4_tasks (A2a) | packet | o4_tasks | -0.28% | 0.67% | 29.0 | -1.42% | +0.86% | 0.6615 | 18 / 0.00122 | 16 / 0.00200 | not_shown |
| o4_tasks (A2a) | uart | alias of o4 | +0.73% | 1.20% | 19.0 | -1.33% | +2.80% | 0.2731 | 3 / 0.00089 | 3 / 0.00132 | not_shown |
| o4_tasks (A2a) | cli | alias of o4 | -1.14% | 0.61% | 19.0 | -2.20% | -0.08% | 0.9611 | 38 / 0.00238 | 28 / 0.00385 | slower_than_W |
| o4_tasks (A2a) | pooled | pooled over packet+uart+cli | -0.56% | 0.42% | 54.7 | -1.27% | +0.14% | 0.9065 | 31 / 0.00179 | 6 / 0.00357 | not_shown |
| o4_legacy (A8) | packet | o4_legacy | -0.41% | 0.52% | 29.0 | -1.30% | +0.48% | 0.7812 | 22 / 0.00135 | 18 / 0.00217 | not_shown |
| o4_legacy (A8) | uart | alias of o4 | +0.73% | 1.20% | 19.0 | -1.33% | +2.80% | 0.2731 | 3 / 0.00089 | 3 / 0.00132 | not_shown |
| o4_legacy (A8) | cli | alias of o4 | -1.14% | 0.61% | 19.0 | -2.20% | -0.08% | 0.9611 | 38 / 0.00238 | 28 / 0.00385 | slower_than_W |
| o4_legacy (A8) | pooled | pooled over packet+uart+cli | -0.57% | 0.38% | 57.3 | -1.21% | +0.06% | 0.9335 | 32 / 0.00185 | 7 / 0.00385 | not_shown |
| o4_nohi (X1) | packet | o4_nohi | -0.95% | 0.73% | 29.0 | -2.19% | +0.29% | 0.8984 | 29 / 0.00167 | 25 / 0.00313 | not_shown |
| o4_nohi (X1) | uart | o4_nohi | +0.22% | 0.66% | 19.0 | -0.92% | +1.37% | 0.3689 | 9 / 0.00100 | 9 / 0.00156 | not_shown |
| o4_nohi (X1) | cli | o4_nohi | -1.59% | 0.78% | 19.0 | -2.93% | -0.24% | 0.9725 | 45 / 0.00357 | 34 / 0.00714 | slower_than_W |
| o4_nohi (X1) | pooled | pooled over packet+uart+cli | -0.67% | 0.41% | 62.6 | -1.36% | +0.02% | 0.9446 | 35 / 0.00208 | 9 / 0.00455 | not_shown |
| o4_min2 (A7) | packet | o4_min2 | -1.10% | 0.59% | 29.0 | -2.10% | -0.09% | 0.9627 | 39 / 0.00250 | 29 / 0.00417 | slower_than_W |
| o4_min2 (A7) | uart | o4_min2 | -1.08% | 0.91% | 19.0 | -2.65% | +0.49% | 0.8752 | 26 / 0.00152 | 22 / 0.00263 | not_shown |
| o4_min2 (A7) | cli | o4_min2 | +0.48% | 0.87% | 19.0 | -1.03% | +1.99% | 0.2941 | 6 / 0.00094 | 6 / 0.00143 | not_shown |
| o4_min2 (A7) | pooled | pooled over packet+uart+cli | -0.71% | 0.43% | 64.5 | -1.43% | +0.01% | 0.9468 | 36 / 0.00217 | 10 / 0.00500 | not_shown |
| o4r2b (o4r2b) | packet | o4r2b | +0.47% | 0.66% | 29.0 | -0.64% | +1.59% | 0.2381 | 2 / 0.00088 | 2 / 0.00128 | not_shown |
| o4r2b (o4r2b) | uart | o4r2b | -1.08% | 0.82% | 19.0 | -2.50% | +0.33% | 0.8990 | 30 / 0.00172 | 26 / 0.00333 | not_shown |
| o4r2b (o4r2b) | cli | o4r2b | -1.80% | 0.69% | 19.0 | -3.00% | -0.61% | 0.9913 | 48 / 0.00455 | 36 / 0.01000 | slower_than_W |
| o4r2b (o4r2b) | pooled | pooled over packet+uart+cli | -0.72% | 0.41% | 65.4 | -1.41% | -0.04% | 0.9583 | 37 / 0.00227 | 11 / 0.00556 | slower_than_W |
| o4r3b (o4r3b) | packet | o4r3b | -0.75% | 0.73% | 29.0 | -1.98% | +0.49% | 0.8443 | 24 / 0.00143 | 20 / 0.00238 | not_shown |
| o4r3b (o4r3b) | uart | same bytes as o4r3d | | | | | | | | | same_bytes |
| o4r3b (o4r3b) | cli | same bytes as o4r3d | | | | | | | | | same_bytes |
| o4r3b (o4r3b) | pooled | pooled over packet | -0.75% | 0.73% | 29.0 | -1.98% | +0.49% | 0.8443 | — (single-workload arm: no pooled scope in BEST) | 5 / 0.00333 | not_shown |
| fl3b3 (C1) | packet | fl3b3 | +0.10% | 0.71% | 29.0 | -1.10% | +1.31% | 0.4420 | 12 / 0.00106 | 10 / 0.00161 | not_shown |
| fl3b3 (C1) | uart | fl3b3 | -1.29% | 1.01% | 19.0 | -3.05% | +0.46% | 0.8915 | 27 / 0.00156 | 23 / 0.00278 | not_shown |
| fl3b3 (C1) | cli | fl3b3 | -1.26% | 0.62% | 19.0 | -2.33% | -0.19% | 0.9719 | 44 / 0.00333 | 33 / 0.00625 | slower_than_W |
| fl3b3 (C1) | pooled | pooled over packet+uart+cli | -0.78% | 0.42% | 56.9 | -1.49% | -0.07% | 0.9644 | 41 / 0.00278 | 12 / 0.00625 | slower_than_W |
| fl3b3p (C1p) | packet | fl3b3p | -0.10% | 0.92% | 29.0 | -1.66% | +1.46% | 0.5418 | 15 / 0.00114 | 13 / 0.00179 | not_shown |
| fl3b3p (C1p) | uart | fl3b3p | +0.57% | 1.01% | 19.0 | -1.18% | +2.31% | 0.2910 | 5 / 0.00093 | 5 / 0.00139 | not_shown |
| fl3b3p (C1p) | cli | fl3b3p | -2.30% | 0.81% | 19.0 | -3.70% | -0.90% | 0.9948 | 49 / 0.00500 | 37 / 0.01250 | slower_than_W |
| fl3b3p (C1p) | pooled | pooled over packet+uart+cli | -0.83% | 0.52% | 61.5 | -1.70% | +0.04% | 0.9426 | 33 / 0.00192 | 8 / 0.00417 | not_shown |
| c7b3 (c7b3) | packet | alias of o4 | -1.55% | 0.78% | 29.0 | -2.87% | -0.22% | 0.9713 | 43 / 0.00313 | 32 / 0.00556 | slower_than_W |
| c7b3 (c7b3) | uart | alias of o4 | +0.73% | 1.20% | 19.0 | -1.33% | +2.80% | 0.2731 | 3 / 0.00089 | 3 / 0.00132 | not_shown |
| c7b3 (c7b3) | cli | alias of o4 | -1.14% | 0.61% | 19.0 | -2.20% | -0.08% | 0.9611 | 38 / 0.00238 | 28 / 0.00385 | slower_than_W |
| c7b3 (c7b3) | pooled | pooled over packet+uart+cli (= o4's hypothesis) | -1.01% | 0.45% | 50.8 | -1.76% | -0.26% | 0.9861 | 46 / 0.00385 | 13 / 0.00714 | slower_than_W |
| o4 (O4-r1) | packet | o4 | -1.55% | 0.78% | 29.0 | -2.87% | -0.22% | 0.9713 | 43 / 0.00313 | 32 / 0.00556 | slower_than_W |
| o4 (O4-r1) | uart | o4 | +0.73% | 1.20% | 19.0 | -1.33% | +2.80% | 0.2731 | 3 / 0.00089 | 3 / 0.00132 | not_shown |
| o4 (O4-r1) | cli | o4 | -1.14% | 0.61% | 19.0 | -2.20% | -0.08% | 0.9611 | 38 / 0.00238 | 28 / 0.00385 | slower_than_W |
| o4 (O4-r1) | pooled | pooled over packet+uart+cli | -1.01% | 0.45% | 50.8 | -1.76% | -0.26% | 0.9861 | 46 / 0.00385 | 13 / 0.00714 | slower_than_W |
| o4_chained (A1) | packet | o4_chained | +1.54% | 0.85% | 29.0 | +0.09% | +2.98% | 0.0404 | 1 / 0.00086 | 1 / 0.00125 | not_shown |
| o4_chained (A1) | uart | o4_chained | -2.38% | 0.61% | 19.0 | -3.43% | -1.33% | 0.9996 | 57 / 0.02500 | 40 / 0.05000 | slower_than_W |
| o4_chained (A1) | cli | alias of o4 | -1.14% | 0.61% | 19.0 | -2.20% | -0.08% | 0.9611 | 38 / 0.00238 | 28 / 0.00385 | slower_than_W |
| o4_chained (A1) | pooled | pooled over packet+uart+cli | -1.09% | 0.38% | 55.3 | -1.73% | -0.45% | 0.9969 | 51 / 0.00625 | 15 / 0.01000 | slower_than_W |
| o4_nocp (X2) | packet | alias of o4 | -1.55% | 0.78% | 29.0 | -2.87% | -0.22% | 0.9713 | 43 / 0.00313 | 32 / 0.00556 | slower_than_W |
| o4_nocp (X2) | uart | o4_nocp | -0.80% | 0.61% | 19.0 | -1.86% | +0.26% | 0.8951 | 28 / 0.00161 | 24 / 0.00294 | not_shown |
| o4_nocp (X2) | cli | o4_nocp | -1.23% | 0.61% | 19.0 | -2.28% | -0.17% | 0.9709 | 42 / 0.00294 | 31 / 0.00500 | slower_than_W |
| o4_nocp (X2) | pooled | pooled over packet+uart+cli | -1.14% | 0.38% | 57.7 | -1.77% | -0.51% | 0.9981 | 53 / 0.00833 | 16 / 0.01250 | slower_than_W |
| o4_nospec (X4) | packet | alias of o4 | -1.55% | 0.78% | 29.0 | -2.87% | -0.22% | 0.9713 | 43 / 0.00313 | 32 / 0.00556 | slower_than_W |
| o4_nospec (X4) | uart | o4_nospec | -0.79% | 0.80% | 19.0 | -2.17% | +0.58% | 0.8343 | 23 / 0.00139 | 19 / 0.00227 | not_shown |
| o4_nospec (X4) | cli | o4_nospec | -1.15% | 0.69% | 19.0 | -2.34% | +0.05% | 0.9430 | 34 / 0.00200 | 27 / 0.00357 | not_shown |
| o4_nospec (X4) | pooled | pooled over packet+uart+cli | -1.16% | 0.43% | 62.1 | -1.89% | -0.44% | 0.9953 | 50 / 0.00556 | 14 / 0.00833 | slower_than_W |
| o4_isb (I1) | packet | o4_isb | -1.84% | 0.61% | 29.0 | -2.88% | -0.79% | 0.9972 | 52 / 0.00714 | 38 / 0.01667 | slower_than_W |
| o4_isb (I1) | uart | o4_isb | -0.03% | 0.83% | 19.0 | -1.47% | +1.41% | 0.5161 | 14 / 0.00111 | 12 / 0.00172 | not_shown |
| o4_isb (I1) | cli | alias of o4 | -1.14% | 0.61% | 19.0 | -2.20% | -0.08% | 0.9611 | 38 / 0.00238 | 28 / 0.00385 | slower_than_W |
| o4_isb (I1) | pooled | pooled over packet+uart+cli | -1.18% | 0.38% | 62.9 | -1.82% | -0.53% | 0.9984 | 54 / 0.01000 | 17 / 0.01667 | slower_than_W |
| s6b3 (s6b3) | packet | s6b3 | -1.05% | 0.57% | 29.0 | -2.01% | -0.09% | 0.9637 | 40 / 0.00263 | 30 / 0.00455 | slower_than_W |
| s6b3 (s6b3) | uart | alias of o4_noregions | -0.38% | 0.71% | 19.0 | -1.61% | +0.85% | 0.7012 | 20 / 0.00128 | 17 / 0.00208 | not_shown |
| s6b3 (s6b3) | cli | alias of o4_noregions | -3.14% | 0.87% | 19.0 | -4.64% | -1.64% | 0.9991 | 56 / 0.01667 | 39 / 0.02500 | slower_than_W |
| s6b3 (s6b3) | pooled | pooled over packet+uart+cli | -1.28% | 0.39% | 65.1 | -1.94% | -0.62% | 0.9991 | 55 / 0.01250 | 18 / 0.02500 | slower_than_W |
| o4_noregions (X3) | packet | o4_noregions | -1.28% | 0.51% | 29.0 | -2.15% | -0.42% | 0.9911 | 47 / 0.00417 | 35 / 0.00833 | slower_than_W |
| o4_noregions (X3) | uart | o4_noregions | -0.38% | 0.71% | 19.0 | -1.61% | +0.85% | 0.7012 | 20 / 0.00128 | 17 / 0.00208 | not_shown |
| o4_noregions (X3) | cli | o4_noregions | -3.14% | 0.87% | 19.0 | -4.64% | -1.64% | 0.9991 | 56 / 0.01667 | 39 / 0.02500 | slower_than_W |
| o4_noregions (X3) | pooled | pooled over packet+uart+cli | -1.38% | 0.37% | 63.2 | -2.00% | -0.75% | 0.9998 | 58 / 0.05000 | 19 / 0.05000 | slower_than_W |

### Where each Holm step-down starts (nothing is rejected at rank 1, so nothing is rejected at all)

| family | rank 1 | d | p | threshold |
|---|---|---:|---:|---:|
| strict BEST (58) | o4_chained packet | +1.54% | 0.0404 | 0.00086 |
| BEST-most pooled (19) | o4r3c pooled | +0.10% | 0.4051 | 0.00263 |
| BEST-most per-workload (40) | o4_chained packet | +1.54% | 0.0404 | 0.00125 |

The next pooled ranks are o4r2 (+0.11%, p 0.4266), o4_pwhopr (−0.18%, p 0.6708), o4_rank (−0.33%, p 0.7740) and o4r3b (−0.75%, p 0.8443). The next per-workload ranks are o4r2b packet (+0.47%, p 0.2381), o4 uart (+0.73%, p 0.2731), o4_rank packet (+0.44%, p 0.2786), fl3b3p uart (+0.57%, p 0.2910) and o4_min2 cli (+0.48%, p 0.2941).

## BEST-most: how `driver/best_most.py` reads the amendment

- **Inputs, and the refusal checks** (`driver/best_most.py:74-96`): the script refuses to run unless all of these hold:
  - F.json has schema `scfix-o4.decide-r3/1`, campaign F, the registered decide_r3.py and decide.py digests, and `registered: true`.
  - decide.py's own sha256 equals DECIDE.sha256's.
  - The amendment's sha256 equals AMENDMENT-F.sha256's.
- **No new estimates.** Every d, SE, df, LB95, UB95 and p is copied from F.json's `estimates` (`driver/best_most.py:49, 103-104`, `EST_KEYS`). The tests check that the source has no estimator call and that the pooled rows equal F.json's (`driver/test_best_most.py`).
- **Pooled family** (`driver/best_most.py:112-131`): one hypothesis per arm other than W and W-aa, using F.json's pooled estimate over that arm's differing workloads.
  - o4r3b differs on packet only, so its "pooled" row is decide.py's pool of one: its packet estimate. It is a hypothesis like every other arm, as the amendment says one per arm. (The strict BEST family has no pooled scope for single-workload arms.)
  - An arm whose `pooled_alias_of` is another arm is not a second hypothesis. Here that is c7b3 → o4.
- **Per-workload family** (`driver/best_most.py:133-156`): one hypothesis per arm and workload where the arm's bytes differ from W's. An alias workload takes its target's hypothesis (decide.best's `alias_for`, `driver/decide.py:616-623`).
- **Majority** (`driver/best_most.py:99-100, 158-189`):
  - A majority is n // 2 + 1 of the workloads that differ: 2 of 3 and 2 of 2, as the amendment states.
  - For an arm that differs on one workload, it is 1 of 1. The amendment (line 25) does not name that case.
  - The verdict under the other reading (1 of 1 is never a majority) is reported as `holds_if_one_of_one_is_not_a_majority`. Here both readings hold.
- **Adoption** (`driver/best_most.py:192-223`): W is adopted when R3's ADD-confirm holds, R3 is final, BEST-most holds and F is not provisional, all as recorded in F.json.
- **Tests:** `driver/test_best_most.py`, 11 passed. They cover:
  - registration and refusals;
  - the null case;
  - a single-workload win that does not block;
  - 2-of-3 and 2-of-2 rejections that block;
  - a pooled rejection that blocks alone;
  - the 1-of-1 reading;
  - aliases in both families;
  - the adoption preconditions;
  - an end-to-end run of decide_r3.py F on synthetic runs, then best_most. In it, an arm 6% faster on cli only fails the strict BEST, is a workload-specific win under BEST-most, and W is adopted.

## Direct pairs in F (descriptive only, not part of any rule)

Method:
- This is `r3/direct_pairs_r3.py`'s method applied to `*/timing-F`, with decide.py's HL, block bootstrap (seed text `"20261202:{w}:{X}-vs-{Y}"`), t bounds and pool.
- d is the HL of log T(Y) − log T(X) over the timed blocks. Positive means X is faster.
- Aliases use their target's timings, and same-bytes workloads are left out.
- Unlike the registered rows, Y here is the arm's own timing, not the ½(ref + A/A) average.
- The script ran from the session scratchpad and wrote nothing under /data/scfix-o4. These numbers are not in F.json.

| pair | packet | uart | cli | pooled [one-sided 95%], p |
|---|---:|---:|---:|---|
| o4r3c vs o4 (O4-r1) | +1.73% (SE 0.94%) | −0.56% (SE 1.17%) | +1.39% (SE 0.73%) | +1.11% [+0.25%, +1.98%], p 0.017 |
| o4r3d vs o4 (O4-r1) | +2.08% (SE 0.85%) | −0.70% (SE 1.38%) | +0.79% (SE 0.64%) | +1.02% [+0.22%, +1.82%], p 0.019 |
| o4r3c vs o4r3d | −0.65% (SE 0.70%) | +0.11% (SE 1.09%) | +0.95% (SE 0.85%) | +0.01% [−0.79%, +0.82%], p 0.49 |
| o4r3c vs o4_pwhopr (A3 on packet) | +0.90% (SE 0.72%) | same bytes | same bytes | +0.90% [−0.33%, +2.13%], p 0.11 |

- On packet, o4r3c vs o4r3d reads −0.65% here and +0.06% in the registered row. The registered row averages o4r3d with its A/A copy, which ran 1.45% slower on packet (VALID above).
- Either way the pooled difference between o4r3c and o4r3d is about 0.0-0.1%, with an SE near 0.45%.

## What F means for O4 (facts for the lead; this step changes nothing)

- **The adopted O4 is o4r3d.** It is O4-r1 plus four changes (`design/pipelines/o4r3d.toml:13-34`):
  - A5: priced exhaustive firmware-inline, `mode = "exhaustive"`, `cost_model = "profile"`. This mode expands nested regions recursively (`expand_nested=True`, `design/O4-design.md:211`).
  - A3: net-work-ranked chain-static.
  - X1: host-inline removed.
  - X4: the SPEC bundle removed (specialize-returns, constant-propagation, branch-folding, dead-host-ops).
- **Restoring host-inline and SPEC is not shown to help.** o4r3c is o4r3d with host-inline and SPEC put back. It measures +0.10% pooled against o4r3d (p 0.41), and +0.01% in the direct pair.
  - Under the amendment's standing contract, an optimization goes into O4 when the pooled test against O4 rejects at 0.05 (`AMENDMENT-F-most-cases.md:33-35`). Neither restoration meets that test in F.
  - o4r3d also has the deterministic build: no gdb helper-PGO replay and no `--pgo-launch` (`design/pipelines/o4r3d.toml:25-30`).
- **No other measured item beats o4r3d.** None of the separately measured items (A1, A2a, A3 alone, A7, A8, I1, X1-X4 on O4-r1, C1, C1p) and none of the compositions (O4-r2, O4-r2b) is faster than o4r3d on any workload or pooled. Nothing is rejected in any of the three families.
- **Recursive levers not yet built or timed** are outside F: B3 slots that enter regions (U7) and B3-aware region pricing (F12), `design/O4-design.md:221`; and lifting the 256-unit B3 cap, `design/O4-design.md:220`.

## Files

| file | sha256 | written by this step |
|---|---|---|
| `F.json` | `38980dce09a4c04ec6666095a496ae75da7d37f39c7a1df80a52e9531a3fc91f` | yes: `decide_r3.py F packet/timing-F uart/timing-F cli/timing-F --r3 R3.json --gates gates/all.json --out F.json` |
| `F.stdout.txt` | `0735d116211e7a7e35ad4e7c84ac4fba07522e450eec357583e8ac986457e93e` | yes: that command's console output. The file existed empty, left by the stopped agent at 06:28. |
| `F-most.json` | `ec477d48d5bfc2a2e2cacc5c9e255026bd2579dc5fb0706917ef91fff32f6262` | yes: `best_most.py F.json --out F-most.json` |
| `driver/best_most.py` | `6ff655e283a31d87d457e65896d6e5c5b09c76761bc4c88f61a3cba12c0c12c6` | yes (new) |
| `driver/test_best_most.py` | `2b6742d1e1c06543d124c2680d764e046e720f1116547ae17a4a364588f4cb04` | yes (new) |
| `driver/decide_r3.py` | `e6554ab034b85d021df575b2eb401c1cf288e26614e37a66c9477c517409969c` | no (registered, unchanged) |
| `driver/decide.py` | `0b7247476a39abade599000de4e2555c7edb1167b57d0989f4bac54cbaf1a438` | no (registered, unchanged) |
| `driver/AMENDMENT-F-most-cases.md` | `01b64b25120e4cbd3cd9df72cf713a83c56abca53f45dfc88812932479a71d5f` | no |
| `R3.json` | `f5449daca3236bf992785ba8b88577504b079decdab893bf3b508847cdc974db` | no |
| `gates/all.json` | `bad5bf48b346a7902903500e4804c3a4364e6fb967e54a0aa103eec202d24599` | no |

How these were run:
- Every Python run used `PYTHONDONTWRITEBYTECODE=1 nice -n 19 taskset -c 21-31 /data/trebuchet-clean/FastDyn/scopt-env/bin/python -B`.
- There was no timing, no build and no rerun.
- Nothing was written outside /data/scfix-o4.
- Reproduce with:

```
cd /data/scfix-o4
$PY -B driver/decide_r3.py F packet/timing-F uart/timing-F cli/timing-F --r3 R3.json --gates gates/all.json --out F.json
$PY -B driver/best_most.py F.json --out F-most.json
cd driver && $PY -B -m pytest -q -p no:cacheprovider test_best_most.py
```
