# B3/B4 run harness: v6 proof, diagnostics, oracles O1-O6, negative controls, arms (WP26)

Design: `design/B3-B4-design.md` §5.1, §5.7, §5.8, §9.3-§9.5, §10.1 and the WP26 implementation
note at its end. Plan: `design/IMPLEMENTATION-PLAN-B3B4.md` `## WP26`.

Nothing here is timed. Every QEMU run uses `setarch x86_64 -R`, `timeout` and a fresh
directory; gdb is always `gdb -nx -batch` with cwd = FastDyn's own cwd (never a QEMU tree).

## Modules

| Module | Content |
|---|---|
| `scopt/run/proof.py` | post-run proof; v6 files add checks `v6_header` (still version 6 with the deployed feature flags; a v5-only loader truncates a v6 file and writes a fresh v5 header, F-B3-18) and `ext` (bytes `[0x100, 0x10000)` unchanged); `deployed_state(..., frontend="none")` for direct QEMU runs (no manifest) |
| `scopt/run/deploy.py` | refusal codes for test-only artifacts and v6 consistency (below); `identity_reasons` (pure, pinned for F-B3-16(a)/(e)); `deploy_direct` / `launch_direct` / `prepare_direct` (no FastDyn: the mps2 alias run of F-B3-15; `prepare_direct` is the capture-on cold run for `scopt import --sc … --scm … --firmware …` without a manifest or bundle) |
| `scopt/run/policy.py` | test-only tags: negative controls (`x_negative_control`, marker `NEGATIVE_CONTROL.json`) and test policies (`x_test_only`, only `allow_ram_resident`); `publish_refusal`, `deploy_refusals`, `waive` / `tag_sidecar` for WP21's admission |
| `scopt/run/negative.py` | `package_negative`: link a mutant plan (+ optional byte mutation, `reseal`), validate, bypass V12-V17, write a tagged directory outside any store |
| `scopt/run/diag.py` | `prepare_diag`; `diag_exec(exec_trace=False)` for `-d int` only (O2a); `stats_env` / `read_v6_stats` (`QEMU_SC_V6_STATS`); `gdb_hits` (hardware breakpoints, at most 4 per gdb run); `artifact_probe_addrs` (C', S, F of a v6 file); `diag_twin` (m-CE7 TLB counter, coverage ring) |
| `scopt/run/oracles.py` | comparators O1, O2a, O2b, O3, O4, O5, O6, `oracle_rows`, and the driver `dynamic_oracles` |
| `scopt/run/arms.py` | `B3B4_ARMS` (§10.1), `FIXTURE_ARMS` (§9.4), `ablation_arms`, `b3b4_arm_specs`; preflight refusals |
| `scopt/formats/artifact.py` | `publish` refuses `negative_control` / `test_only_artifact` directories, and a `validation.json` that is not `accepted` (`validation_failed`) or that the sidecar's `validation_sha256` does not name (`validation_mismatch`) |
| `scopt/commands/run_b3.py` | `scopt publish`, `scopt direct {deploy,launch}` |

## Commands

```
scopt validate ART --dynamic -c BASE.toml --out DIR --oracles O1,O2a,O2b,O3,O5,O6 \
      [--baseline WARM] [--inputs IN.bin --fixture-input NAME] \
      [--fixture-expected fixtures/b3/expected.py] [--mode icount|noicount] \
      [--o3 '{"stats": {"links_armed": ">0"}, "hits": {"variant 0 C'"'"'": ">0"}}'] \
      [--hit-target LABEL] [--negative-control] [--test-policy allow_ram_resident]
scopt validate ART --dynamic -c BASE.toml --out DIR          # unchanged: V10
scopt publish ART_DIR --store STORE                          # verified copy; refuses negative controls
scopt direct deploy ALIAS.sc --run-dir D --qemu Q \
      --argv '@QEMU@ -M mps2-an385 -cpu cortex-m3 -kernel alias.elf ... -accel tcg,supercache=@SUPERCACHE@'
scopt direct launch --run-dir D [--v6-stats]
scopt diag probes ART_DIR|cache.sc                           # C'.tc.ptr, S, F addresses
scopt diag hits --run-dir RD --out D (--addr 0x.. | --probe LABEL | --all-probes)
scopt diag twin --run-dir RD --out D [--cov] [--d-int] [--allowed-calls N]
scopt diag exec --run-dir RD --out D --no-exec               # -d int only (O2a)
scopt diag stats FILE                                        # qemu-sc-v6-stats@1
```

## Oracles

| Oracle | Input | pass | Notes |
|---|---|---|---|
| O1 | two `run.json` | proofs PASS, equal exit code, `output_norm_sha256`, `guest_norm` | with `--fixture-expected`: the fixture's per-mode oracle too; in noicount it replaces the byte comparison (timer phases) |
| O2a | two `-d int` logs (diag re-runs) | exception lines identical | order of exception kinds/vectors only (G31) |
| O2b | two guest logs | icount: every SysTick/fault/IRQ record field equal; noicount: counts consistent, records complete, fault positions (pc, cfsr, ipsr, bfar, mmfar, iter) and IRQ positions (pc, iter) equal (SysTick positions and handler counters `0x56xx` are host-time dependent and not compared); both modes: end counters `0x5f02`-`0x5f04` present in both logs | fixture named invariants added when given |
| O3 | stats file, gdb hits | every requirement met | unmet: `vacuous`; not collected, no requirement, or hits from a gdb run whose QEMU did not exit 0: `not_run` (never 0) |
| O4 | O1/O2a/O2b of a negative control vs the reference, plus both `run.json` | the control's proof is PASS (launch verdict PASS, or `INVALID_exit_code` for a QEMU abort), the reference is PASS, and at least one O1/O2a/O2b `fail` records a concrete difference (O1 `differences`, O2a `first_difference`, O2b with a sound reference log) | a control that did not provably run (`INVALID_artifact_not_used`, a v6 control on `qint`, stray capture files) is `not_run`, never detected; equal outputs of a proved control: `fail` (not detected) |
| O5 | proof, stats | reported counts | `fail` only for a failed proof or loader invariant violations |
| O6 | coverage files or the LOG_REG ring (diag twin `--cov`), C' hits | equal and C' hits > 0 | this FastDyn build writes no coverage file; with coverage off there is no ring (`not_run`) |

`dynamic_oracles` verdict: `fail` when an oracle fails or is `vacuous` (or a deploy is
refused); otherwise `incomplete` when any requested oracle is `not_run` (or missing) in any case
(for example O3 with no stats file because a v6 artifact ran on a v5-only binary, or O6 with
coverage off); `pass` only when every requested oracle ran everywhere. The ledger row carries
the same verdict, and `scopt validate --dynamic --oracles` exits 0 only for `pass`. Ask only
for the oracles a configuration can run (e.g. no O3 stats requirement on `qint`).

With `--negative-control` (`allow_negative_control=True`) the driver runs in **O4 mode**: each
case adds O4 and the verdict is O4's alone: `pass` = the mutant was detected in every case
(exit 0, "mutant detected"), `fail` = not detected in some case or a deploy was refused (exit
1, "mutant NOT detected"), `incomplete` = the control was not proved to run (exit 1). The
control's O1/O2a/O2b failures are its detections, not failures. The ledger reason starts with
`negative control O4 (pass = mutant detected)`.

A `.sc` v6 artifact on a QEMU binary that is not v6-capable (`scopt.validate.v6_capable_exes`:
`runs/wp22/gate-binary.json`, `$SCOPT_V6_EXES`) is refused before any launch
(`OracleRefused("v6_loader_required")`, CLI exit 2): on such a binary every v6 run would be
`INVALID_artifact_not_used`.

## `scopt publish` (verified copy)

Nothing is copied unless, in this order: the source is not a negative control or test-policy
artifact; it has `cache.sc` and a schema-valid sidecar without `x_uncertified`; its own
`MANIFEST.sha256` verifies (`manifest_verify`); the sidecar's `validation_sha256` is the sha256 of
`validation.json` (`validation_mismatch`); `validation.json` is `accepted`, has no failing row
(`validation_failed`) and names the sidecar's input/output SnapshotIds (`validation_mismatch`);
the output SnapshotId recomputed from the `cache.sc` bytes as the v5/v6 loader sees them equals
that id (`output_snapshot_mismatch`); and the staged copy hashes to the source manifest
(`copy_mismatch`), so the published ArtifactId is the source's.

## Deploy refusal codes added by WP26

| Code | When |
|---|---|
| `negative_control_artifact` | the artifact is a negative control and `allow_negative_control` was not given |
| `test_only_artifact:<policy>` | built under a test policy the caller did not admit (`test_policies`) |
| `unknown_test_policy:<policy>` | the tag names a policy that does not exist (or is malformed) |
| `loader_requirement_mismatch` | header version and sidecar `loader_requirement` disagree |
| `ext_artifact_id_mismatch` / `ext_artifact_id_missing` | a v6 file's ext `artifact_id8` differs from (or has no) sidecar `x_ext_artifact_id8` |
| `direct_argv_missing`, `direct_qemu_missing` | direct deploy without `@SUPERCACHE@` or without a QEMU binary |

Pinned F-B3-16 codes (existing codes, `tests/unit/test_wp26_harness.py`): (a) changed patch plan
and (e) coverage-enabled configuration are both refused with `bundle_mismatch` first, then
`config_mismatch`. (a) differs in `instrumentation_hash`, `patch_bindings_hash`,
`patch_plan_hash`; (e) additionally in `contract_id` (on the fixture configs the plan hash also
differs because every config prints its own BENCH name; the isolated coverage change differs in
`contract_id` only).

## Negative controls

```python
from scopt.run.negative import package_negative
r = package_negative(snapshot, mutant_plan, "runs/<wp>/negative", label="drop-g_bp",
                     mutant="drop g_bp", detector="F-B3-13(d) O1",
                     mutate=lambda link: ...,            # optional byte mutation
                     runtime=v6_identity, v6_exes=[...])
# r.path: deploy with allow_negative_control=True (or scopt validate --negative-control)
```

The directory is written outside any store, carries `x_negative_control` and
`NEGATIVE_CONTROL.json`, keeps the full `validation.json` (the bypassed failures included;
verdict `rejected`), and is refused by `scopt publish`, `formats.artifact.publish`, deploy (by
default; also when given as the bare `<dir>/cache.sc`, judged by the directory's marker file and
the file's own `<file>.scopt.json` sidecar) and campaign preflight. Known limit: the tags
live next to the file, not in the `.sc` bytes, so a control's `cache.sc` copied to another
directory deploys as an untagged bare `.sc` (`identity.checked = false`); gate reports name
artifacts by directory, never by a bare `.sc`. A row outside the bypass set that fails refuses the mutant
(`unbypassed:<code>`). A byte mutant skips the re-import (V8/V19 absent).

## Smoke on the current binary (not acceptance)

`runs/wp26/smoke/smoke.py` (run from the worktree with `PYTHONPATH` set to it) exercises the
paths on `wt/qemu-int` with recorded WP24 caches: deploy+launch of F-B3-1 warm (PASS, fixture
oracle clean), gdb hit counts on the TBs at Vc and f (1000 each), the diag twin (3 counted
calls after `benchmark_start`: the fixture's teardown `MPU_CTRL = 0` after the loop is one
flush that counts 3 calls; the smoke accepted it with `allowed_calls=3`. Since WP26 review 2
`clean` is always the m-CE7 rule, 0 calls and all 20 counter breakpoints resolved, so such a
twin is `within_allowance` but not `clean`; `--allowed-calls` is diagnostic only and no design
note approves it for gate use), a `-d int` only run, `dynamic_oracles` (O1/O2a/O2b pass, O3 `not_run` without
v6 stats, O5 reported, O6 `not_run` with coverage off and `pass` on the F-B3-23 coverage
config; with the review fix such a run is `incomplete`, not `pass`), the direct mps2 alias run (PASS, output = `expected.norm`), and the non-FastDyn import
path (`prepare_direct` + `scopt import`: 17/17 units `ok_relocatable`, no bundle).

## Integration on the gate binary (WP26 integration stage)

`tests/qemu/wp26_integ.py` (see `tests/qemu/README.md`); evidence in `runs/wp26/integ/final/`.
Facts that a user of the harness needs:

- **FastDyn takes the QEMU from the TOML's `qemu_path`.** `deploy_new(..., qemu=Q)` exports
  `FASTDYN_QEMU`, but a rendered fixture TOML that names a binary wins. A run on another binary
  needs the template rendered for that binary (same arm; only `qemu_path` differs). Otherwise the
  run silently uses the binary named in the TOML. The first integration attempt fell into this,
  and its `qint` runs ran on the gate binary. Check `run.json` `qemu_sha256`.
- **The `.sc` runtime_signature is bound to the exe.** `qint`'s v5 loader also rejects a v5
  cache made on the gate binary (`header`: `runtime_signature` changed). So an F-B3-18 INVALID
  on `qint` proves something only together with a control whose signature matches `qint`.
- **`scopt validate --dynamic --oracles` and the baseline.** Without `--baseline` the reference
  is `<artifact store>/snapshots/<input id>`. An artifact packaged into its own store (as
  `package_b3` does in the tests) needs `--baseline <snapshot dir>`. Otherwise the reference
  deploy is refused (`artifact_unreadable`) and the verdict is `fail`.
- **A guest debugger on the gdbstub** (`-S -gdb`) makes QEMU print
  `qemu-system-arm: warning: icount sleep disabled and no active timers` once. That line is in
  `guest_norm`, so O1 against a launcher run differs on it. O2b ignores it, because it only reads
  `Reg` pairs. A watchpoint run appends records to the cache (4 to 12 here). The proof still
  passes.
- **SysTick CVR is 0 in every record of the WP24 fixtures** (frozen `b4-3`, `7`, `20`, `6h`
  oracles, and every gate run here). In icount the O2b positions therefore rest on the stacked
  PC, xPSR, registers and iteration counter, not on CVR. (iteration, PC) still pins the dynamic
  instruction. What CVR would add, drift between the interrupt and the handler's first load, is
  not observed.
