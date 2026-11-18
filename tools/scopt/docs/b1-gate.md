# B1 gate (WP14)

The B1 completion gate (design §6) is T-B1-0 … T-B1-9 on the minimal fixture, plus
T-B1-0/1/3/5 on FreeRTOS MPS2 CM3: the blinky fixture and the WP12 compute variant.
Every run uses the integrated WP-QINT binary. `docs/qemu.md` describes that binary (QEMU branch `scopt` up to commit `a0681a6ac3`).

## How to run it

```sh
PY=python
cd <scopt worktree>
# T-B1-0: re-qualification plus the disabled-hook cost (not gated; see the flag rule below)
$PY tests/qemu/t_b1_0.py --out runs/wp14/t-b1-0 --workload all --pairs 30 --seed 14
# T-B1-1..9: the CLI end to end, per workload
for w in minimal freertos compute; do
  $PY tests/qemu/b1_gate.py --workload $w --out runs/wp14/gate/$w --t-b1-0 runs/wp14/t-b1-0/t-b1-0.json
done
# re-check the evidence from the files
B1_GATE_ROOT=runs/wp14/gate B1_GATE_DIR=runs/wp14/gate/minimal \
  $PY -m pytest tests/acceptance/test_b1_gate.py tests/acceptance/test_b1_9_rejections.py
```

`b1_gate.py` runs each step as a `python -m scopt …` subprocess. Each command and its
exit code go to `OUT/cmds.jsonl`, and its output to `OUT/logs/`. The checks then read
the files the commands wrote. The driver writes `OUT/gate.json` with:
- each test's verdict (`pass`, `fail` or `not_run`), its checks and its evidence paths;
- whether the test is gated for this workload;
- the identity artifact's ledger stages.

## The chain (§8)

| step | command | output |
|---|---|---|
| T-B1-1 | `scopt qualify-capture -c BASE --qemu Q [--input F]` | `t-b1-1/qualification.json` |
| prepare | `scopt prepare -c BASE --out prep [--inputs F]` | `prep/cache/*.sc`, `prep/capture.scm`, `prep/oracle/` |
| bundle | `scopt bundle identity`, `scopt bundle runtime-identity --run-dir prep/oracle --scm prep/capture.scm` | `bundle.json`, `runtime_identity.json` |
| T-B1-2 | in-process checks, `scparse.py`, `scopt sc identity-copy --require-identity` | `t-b1-2/` |
| import (T-B1-3, T-B1-8) | `scopt import prep --bundle --runtime-identity --store store` | `store/snapshots/<id>/` |
| T-B1-4 | `scopt graph export/import --check`; a 100-record truncated snapshot imported with the full capture | `graph/`, `trunc/`, `store-trunc/` |
| T-B1-6/7 | `scopt profile run --snapshot S --phase steady@0x08003972 --watch …`; `scopt deploy` + `scopt diag exec --nochain` (minimal only) | `prof/`, `t-b1-6/`, `prof-trunc/` |
| T-B1-5 | `scopt optimize S --pipeline identity`, re-import of the derived `.scm`, `scopt validate A`, `scopt profile run --snapshot A --artifact` (isolation), `scopt validate A --dynamic`, `scopt run --arms warm,identity --reps 5`, `scopt diag exec` | `store/artifacts/<id>/`, `v10/`, `timing/`, `diag-identity/` |
| T-B1-4 pair | the first `Linking TBs` line of `diag-identity`, then `scopt graph export --unit A --unit B` | `graph/pair.json` |
| ledger | `scopt report OUT` | `report/report.{md,json}` |
| T-B1-9 | `pytest tests/acceptance/test_b1_9_rejections.py` with `B1_GATE_DIR=OUT` (minimal) | `t-b1-9/junit.xml` |

## Notes

- **T-B1-0 flag rule (B1FIX).** The disabled-hook overhead is `flagged` only when
  the whole 95% CI is above 1%. It is `unresolved` when the CI contains 1% and either
  the median is above 1% or the CI is wider than 5 points; otherwise `not_flagged`.
  Part A also fails when the minimal cold `.sc` has a different host code size
  (`tc_size` per tb_uid) from the B0 golden, not only a different tb_uid set.
- **QEMU default (B1FIX).** A run directory made without `qemu` uses the integrated
  runtime (`scopt.run.runtime.runtime_qemu()`, sha256-checked) and records
  `qemu_selection` and `qemu_sha256` in `rundir.json`. `qemu=launcher.FASTDYN_DEFAULT_QEMU`
  opts out (FastDyn's own choice, the B0 build). `SCOPT_RUNTIME_QEMU` still checks
  the binary: against `SCOPT_RUNTIME_QEMU_SHA256` when set, else against the recorded
  sha256.
- **Ledger.** The importer writes no ledger. `optimize` re-opens the snapshot, which
  re-runs the WP7 cross-check, and writes the `inspected` rows: one per input unit,
  with the status as verdict and the pin reasons (once per optimize invocation, into
  the first candidate's ledger, through a shared `validate.Inspection`), plus one
  artifact row per candidate. The artifact row's verdict is `pass` (every unit
  `ok_relocatable`), `partial` (some pinned or `no_meta`) or `fail` (no unit), and
  its `counts` field holds `{units, ok_relocatable, pinned, no_meta}`.
  - The `inspected`, `compiled` and `accepted` rows carry the candidate id.
  - `validated_dynamic` and `measured` carry the artifact id, which is the hash of a
    MANIFEST that covers the ledger.
  - `scopt report` joins the two ids through the sidecar's `x_candidate_id`.
- **Workloads with an input bank.** `prepare`, `qualify-capture --input`,
  `validate --dynamic --inputs`, `run --inputs` and `profile run --inputs` write the
  input into the `input` RAM bank. `diag exec` replays the inputs the source run
  recorded, and refuses an input larger than its bank (`input_too_large`) before
  writing anything.
- **The `-d exec,nochain` oracle runs on minimal only.** On the FreeRTOS window its
  log passed 12 GB before the run was stopped.
