# End-to-end workflow: prepare, optimize, validate, run, report

This page covers the end-to-end workflow. It strings the per-milestone docs together in
the order a new workload goes through them. The commands are the ones the B5 evaluation
ran (their logs are part of the maintainer's evaluation data, not in the repository).
For a first run on your own machine, with your own QEMU build, start with
`docs/getting-started.md`; it uses the same commands. Paths are
shortened with the variables below.

```sh
F=$FASTDYN_ROOT                       # workspace for runs (not the repository)
PY=python                             # the venv with scopt installed; `scopt` = `$PY -m scopt`
                                      # run from tools/scopt
Q=<QEMU tree>/build/qemu-system-arm   # a build of the QEMU scopt branch (docs/qemu.md); the
                                      # evaluation used the gate binary, sha256 56d8bfb3...0430d90
CFG=<base FastDyn TOML>               # supercache on, supercache_flat = false, icount shift 0
W=<work dir>                          # e.g. $F/<workload>, a fresh directory per workload
```

**Runtime.** B1/B2 used the WP-QINT binary (QEMU `scopt` branch up to commit
`a0681a6ac3`). B3, B4 and B5 use the frozen gate binary: WP-QINT plus the `.sc` v6 loader
(commit `934890ed51`). The B0 binary (the FastDyn QEMU without the scopt commits) is only
a reference. A cache recorded by one binary is never valid for another, because the
runtime signature hashes the executable; scopt's per-executable data (EnvLayout, helper
effect table, v6 capability) has to be set up for each build (`docs/qemu.md`).

Every run goes through the WP8 harness:
- a fresh run dir;
- `setarch x86_64 -R`;
- a timeout;
- a scrubbed environment;
- `supercache_flat = false`.

## 1. Prepare (preparation runs; never on the production vCPU path)

| step | command | output | doc |
|---|---|---|---|
| capture-on cold run (+ warm appends per input) | `scopt prepare -c $CFG --out $W/prep --qemu $Q --inputs train-1.bin --inputs train-2.bin --inputs train-3.bin` | `prep/cache/*.sc`, `prep/capture.scm`, `prep/oracle/` | `docs/b1-gate.md`, `docs/scm-capture-notes.md` |
| ExecutionBundle v1 (identity bundle) | `scopt bundle identity -c $W/prep/run.toml --manifest $W/prep/cache/<name>.sc.fastdyn.json --out $W/bundle.json` | `bundle.json` | `docs/contracts.md` |
| RuntimeIdentity | `scopt bundle runtime-identity --run-dir $W/prep/oracle --sc ... --scm $W/prep/capture.scm --out $W/runtime_identity.json` | `runtime_identity.json` | |
| ImportCache | `scopt import $W/prep --bundle $W/bundle.json --runtime-identity $W/runtime_identity.json --store $W/store` | `store/snapshots/<id>/` (immutable; an incompatible input is rejected with exit 2) | `docs/b1-gate.md` |
| B3/B4 admission (RuntimeIdentity v2, V20) | `scopt admission identity --snapshot $W/store/snapshots/<id> --config $CFG --out $W/runtime_identity.v2.json` | `runtime_identity.v2.json` | `docs/b3-run-harness.md` |
| ProfileRegions (exact, software-only) | `scopt profile run -c $CFG --snapshot $W/store/snapshots/<id> --qemu $Q --phase steady@0x2990 --inputs train-{1,2,3}.bin --out $W/prof` | `prof/merged.profile.json` (Profile v1) | `docs/qprof-producer.md` |
| graph / summaries (optional inspection) | `scopt graph export`, `scopt return-edges summaries`, `scopt b4 census` | `graph.json`, CalleeSummary v1 rows, D1/D2/D3 counts | `docs/return-edges.md`, `docs/b4.md` |

Profiles are taken only on **train** inputs and only on the unoptimized base.
Held-out and final inputs are never used for training. Keep the three sets disjoint:
train inputs for `prepare` and `profile run`, held-out inputs for the dynamic check
(V10), final inputs only for the timed evaluation. (The maintainer's input sets and
their README are in the maintainer's workspace, not in the repository.)

## 2. Optimize (one invocation per pass set; each pass leaves its own ledger row)

| route | command | artifact | doc |
|---|---|---|---|
| identity (no-change pass) | `scopt optimize $W/store/snapshots/<id> --pipeline identity --store $W/out/identity` | `.sc` v5, byte-equal to the input (V9) | `docs/link-validate.md` |
| B2 layout | `scopt optimize ... --pipeline b2-layout --profile $W/prof/merged.profile.json --store $W/out/layout [--disable cluster-hot\|order-cold]` | `.sc` v5 for the unmodified loader | `docs/b2-layout.md` |
| B3 cross-TB | `scopt optimize ... --pipeline b3 --b3 --runtime-identity $W/runtime_identity.v2.json --prep $W/prep --profile ... --selftest hostsym_selftest.json --store $W/out/b3 [--disable PASS\|--only PASS]` | `.sc` v6 (a QEMU with the v6 loader: `SCOPT_V6_EXES`, `--v6-exe`; the evaluation used the gate binary), hostsym-certified (V12/V14/V18) | `docs/b3-passes.md`, `docs/b3-link-v6.md`, `docs/hostsym.md` |
| B4 state sync | `scopt b4 package SNAP --store $W/out/b4 --runtime $W/runtime_identity.v2.json [--classes D1\|D2\|D3]` | `.sc` v5, V12 under the strict call contract | `docs/b4.md` |
| baseline-TCG fix (not an optimizer result) | `scopt sfb package SNAP --store $W/out/sfb` | `.sc` v5 (+16 B per unit) | `docs/b4.md` |

`optimize` exits 0 when the artifact is published and 2 when the candidate is rejected.
The reason goes to `STORE/rejections/<candidate>.json`, and the baseline stays as it was.

`semantic` (B3 + B4 in one artifact) and `combined` (+ layout) have no packager. They are
reported as `skipped_unsupported` (`docs/b5-campaign.md`).

## 3. Validate and publish

| check | command | notes |
|---|---|---|
| static re-validation (V1-V9, MANIFEST) | `scopt validate ARTIFACT --snapshot $W/store/snapshots/<id>` | **move-only artifacts (identity, B2 layout) only.** B3, B4 and `sfb` artifacts change unit bytes and were certified by their own checks (V1-V23, SFB1-SFB9) when packaged; `validate` refuses them with `static_not_applicable`, exit 2, and `scopt publish` re-checks them |
| publish (verified copy) | `scopt publish ARTIFACT --store $W/pubstore` | refuses negative controls, test-policy artifacts and anything that does not verify |
| dynamic V10 on held-out inputs | `scopt validate ARTIFACT --dynamic -c $CFG --qemu $Q --baseline $W/store/snapshots/<id> --inputs heldout-{1,2,3}.bin --out $W/v10/<arm>` | guest output, proof `PASS`, no unexpected fallback |
| B3/B4 dynamic oracles | `scopt validate ARTIFACT --dynamic --oracles O1,O2a,O2b,O3,O5,O6 ...` | fixture oracles (`$FASTDYN_ROOT/fixtures/b3/expected.py`, a maintainer fixture that is not in the repository); O4 via `--negative-control` |

## 4. Run (timing)

Timing goes only through a frozen campaign:
- under `flock $FASTDYN_ROOT/runs/.timing.lock`;
- on at most 4 CPUs;
- with nothing else of ours timed at the same time.

```sh
scopt run --campaign campaigns/X.toml --out $W/timing          # one campaign (docs/timing-and-report.md)
# B5 (the evaluated campaign, frozen; 5 sets, 5,670 runs):
H=e095bf1f0c3728cff65927effe23f713b366dbf8b1c850cdae90f78cd05d262e
$PY tests/qemu/b5_campaign.py check --campaign campaigns/b5-freertos.toml --expect-sha256 $H
$PY tests/qemu/b5_campaign.py run   --campaign campaigns/b5-freertos.toml --expect-sha256 $H --out-root <new root> --cpus 8-11
$PY tests/qemu/b5_campaign.py diag  --campaign campaigns/b5-freertos.toml --expect-sha256 $H --out-root <new root>
```

If `check` fails, stop. Re-freezing would make a new campaign, and it has to be disclosed.

The campaign files in `campaigns/` are frozen records of the maintainer's evaluation:
they name the maintainer's workspace paths and QEMU builds, and their sha256 is part of
the record, so they are not edited. To time on your own machine, write a new campaign
(`docs/timing-and-report.md`).

An untimed single run (no campaign) is `scopt deploy` + `scopt rundir launch`
(`docs/getting-started.md` §11; `--inputs FILE` for an input bank).

Arms, pairing, sample validity and freeze rules are documented in
`docs/timing-and-report.md` and `docs/b5-campaign.md`.

## 5. Report (one command, from the measurements)

```sh
$PY tests/qemu/b5_campaign.py report --campaign campaigns/b5-freertos.toml --out-root <out root>
# equivalent: scopt report <out root> --out <out root>/report/report.md \
#   --profile $W/prof/merged.profile.json --b5-campaign campaigns/b5-freertos.toml
```

`scopt report` always exits 0. A missing or unsupported target, arm or pass becomes a
`not_run` or `skipped_unsupported` row, never a 0. It writes:
- `report.md`;
- `report.json`;
- `timing.csv` (every raw time).

The outcome ledger keeps these stages as separate columns: `inspected`, `optimized`,
`compiled`, `accepted`, `validated_dynamic` and `measured`.

Supplementary, untimed tables (seed sensitivity, leave-one-input-out, drift, load):

```sh
$PY tests/qemu/b5_evaluate.py --out-root <out root> --out <out root>/evaluation/b5-evaluation.json
```

## Milestone drivers (each re-runs one gate end to end)

| milestone | driver | doc / report |
|---|---|---|
| B0 | `$FASTDYN_ROOT/fixtures/{freertos-mps2,freertos-mps2-compute,fault}/qualify.sh` (maintainer fixtures, not in the repository), `tests/qemu/base_warm_smoke.sh` | the maintainer's evaluation report `B0-qualification.md` (not in the repository) |
| B1 | `tests/qemu/t_b1_0.py`, `tests/qemu/b1_gate.py` | `docs/b1-gate.md`, the maintainer's evaluation report `B1-gate.md` (not in the repository) |
| B2 | `tests/qemu/b2_gate.py`, `b2_fault.py`, `b2_campaign.py` | `docs/b2-*.md`, the maintainer's evaluation report `B2-gate.md` (not in the repository) |
| B3 | `tests/qemu/wp28_gate.py`, `wp28_report.py` | `docs/b3-gate.md`, the maintainer's evaluation report `B3-gate.md` (not in the repository) |
| B4 | `tests/qemu/wp29_gate.py`, `wp29_report.py` | `docs/b4.md`, the maintainer's evaluation report `B4-gate.md` (not in the repository) |
| B5 | `tests/qemu/b5_campaign.py`, `b5_evaluate.py` | `docs/b5-campaign.md`, the maintainer's evaluation report `B5-evaluation.md` (not in the repository) |

The gate and evaluation reports, and the map from each requirement to its evidence,
are kept by the maintainer and are not part of the repository.

## Choose a cache for deployment

A successful package contains a candidate that passed the optimizer’s correctness checks. Its artifact ID identifies content and provenance. For offline optimization, the JSON field `artifact_dir` gives the candidate and `selected_artifact` defaults to the input warm snapshot. To promote a candidate on performance, measure it against that original warm snapshot.

```sh
scopt optimize "$SNAPSHOT" --pipeline "$PIPELINE" --store "$STORE" --profile "$PROFILE" \
    --select-with cases.json --selection-out "$RUN/selection" --json
scopt deploy --selection "$RUN/selection/selection.json" -c "$CONFIG" \
    --qemu "$QEMU" --run-dir "$RUN/deployment"
```

For an existing artifact, `scopt select-warm "$SNAPSHOT" "$CANDIDATE" --cases cases.json --out "$RUN/selection" --qemu "$QEMU" --pairs 24` performs the same comparison. Run inside the intended runtime and RAM configuration. Specify each workload’s inputs and an expected normalized guest transcript from a separate reference run. Paths in the following example are relative to the cases file.

```json
{
  "training_inputs": ["training.bin", "screening.bin"],
  "inputs": [
    {"name": "held-out", "config": "benchmark.toml", "bank_inputs": {"input": "final.bin"},
     "expected_guest": "reference/guest.norm"}
  ]
}
```

For a fixed workload without input banks, supply the configuration and reference transcript with `"bank_inputs": {}`. Byte-identical caches retain the original warm source without timing. Changed candidates must pass checked deployment, append zero translation blocks, match the reference transcript, and improve every declared case in balanced randomized pairs. The selection records raw timings and the one-sided randomization test. Freeze a candidate before confirmation and reserve fresh measurements for that decision. A selection supports a claim about the measured host, workload and inputs. Execution times of individual launches vary.
