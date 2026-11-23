# Runtime-weighted helper-inline sweep

This experiment replaces the original size-only selection with execution-weighted
selection and tests the resulting policies on the sealed FreeRTOS compute firmware.
The input snapshot is `sha256:adc9c820...54d94`; the exact steady profile is
`sha256:812aab8a...639c`. Timings are randomized fresh-process paired blocks on CPU 2.
The scored interval is the firmware `[BENCH][fw]` interval, not compilation or launch.

## Result

The measured sweet spot for the current transplant mechanism is **no helper
transplant**. The most selective candidate (one site, 24 bytes growth) did not
confirm its initial 1.009x point estimate: an independent 24-pair confirmation was
0.996x with a 95% paired-bootstrap interval of [0.961, 1.022]. The exact-artifact
four-site leaf-fallthrough candidate was also neutral at 1.003x [0.971, 1.030].

| policy | sites | net growth | paired speedup | 95% interval | disposition |
| --- | ---: | ---: | ---: | ---: | --- |
| exhaustive graph | 21,977 | 1,179,601,718 B | 0.720x | [0.700, 0.838] | reject |
| frequency-density, 64 KiB | 13 | 64,632 B | 1.008x | [0.979, 1.033] | neutral |
| frequency-density, 256 KiB | 23 | 261,600 B | 0.950x | [0.934, 0.981] | reject |
| frequency-density, 1 MiB | 37 | 1,045,712 B | 0.956x | [0.924, 0.972] | reject |
| frequency, 1 MiB | 32 | 1,048,464 B | 1.002x | [0.984, 1.032] | neutral |
| true leaf fallthrough | 4 | 480 B | 1.003x | [0.971, 1.030] | neutral |
| root wrappers, >=1M proxy | 84 | 1,912 B | 0.979x | [0.953, 1.000] | reject/neutral boundary |
| root wrappers, >=14M proxy | 11 | 248 B | 0.953x | [0.916, 1.039] | neutral, bad point estimate |
| root wrapper, >=28M proxy, confirmation | 1 | 24 B | 0.996x | [0.961, 1.022] | neutral |

`speedup = baseline time / candidate time`; values below one are regressions.
All scored runs produced the same 16-register transcript and retained their sealed
cache hashes. `results.json` records artifact identities and raw-result locations.

## Why frequency did not rescue literal inlining

The user's correction was decisive: bytes are not the objective; eliminated dynamic
work is. However, the available profile counts TB entries, not arbitrary call-site
executions. In particular, `LDST_HELPER_CALL` sites live in out-of-line TLB-miss
paths. A TB may enter 14 million times while its slow helper executes only on misses.
Summing the enclosing TB entries therefore grossly overstates the value of those
sites. Profile-ranked selection now refuses such may-skip sites unless the experiment
explicitly sets `trusted_allow_tb_entry_proxy=true`.

The lowering costs also matter:

* Exhaustive graph transplantation copied roughly 45 KiB of transitive MMU code per
  site and created a 1.18 GiB working-set expansion.
* Root-wrapper lowering avoids that expansion, but an arena-to-PIE call requires
  `movabs` plus an indirect call, and the 16-31-byte wrapper leads into a roughly
  300-instruction slow path. Removing its outer call is negligible, while changing a
  hot TB's placement and call encoding can cost more.
* The four admitted shift leaves really contain no `CALL`, `PUSH`, or `RET` after
  lowering, but their combined TB-entry proxy is only 56,254 per run. That saving is
  below the noise floor of a one-second application run.

The next credible runtime experiment is not more literal copying. It is the existing
same-size partial-evaluation design for the must-execute `v7m_msr` plus
`rebuild_hflags_m32_newel` pair: replace the MSR helper with its proved BASEPRI store
and remove the identity rebuild without moving the TB. See
`docs/design/helper-inlining-design.md`. That is a distinct transformation and is not
claimed as completed here.

## Reproduction

Build a candidate (choose one of the checked-in parameter files):

```sh
PYTHONPATH=tools/scopt scopt-env/bin/scopt optimize \
  /tmp/treb-helper-sweetspot/store-bundle/snapshots/adc9c820fdb4819960b7b3c24a53389337046a2635eaa6489d8a6bd2c0754d94 \
  --pipeline tools/scopt/pipelines/trusted-helper-inline.toml \
  --store /tmp/treb-helper-sweetspot/out-runtime-wrappers-28m \
  --profile /tmp/treb-helper-sweetspot/profile-exact-1.json \
  --params tools/scopt/experiments/helper-inline-sweetspot/params-runtime-wrappers-28m.json \
  --json
```

After deploying the emitted `cache.sc` into a direct launcher, measure without Python
in the scored process:

```sh
PYTHONPATH=tools/scopt scopt-env/bin/python \
  tools/scopt/scripts/measure_sealed_whopr.py \
  --baseline /tmp/treb-whopr7-v28/direct-baseline-exact/run.sh \
  --candidate /tmp/treb-helper-sweetspot/direct-runtime-wrappers-28m/run.sh \
  --out /tmp/treb-helper-sweetspot/paired24-runtime-wrappers-28m-confirm \
  --pairs 24 --cpu 2 --seed 775031
```
