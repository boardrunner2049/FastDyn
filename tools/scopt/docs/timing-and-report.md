# Timing arms and the report (WP13)

Code: `scopt/run/arms.py` (`scopt run`), `scopt/report/{collect,render,stats}.py`
(`scopt report`). Design: §4.8, §5.9, §5.11, §7 F-B2-7; spec §5, §6, §8.

## `scopt run`

```
scopt run -c BASE.toml --arms nocache,empty,warm,identity,layout \
          [--warm SC|DIR] --artifact identity=A1 --artifact layout=A2 \
          [--ablation cluster-hot=A3 ...] [--nocache-config NOCACHE.toml] \
          [--set train|heldout|final|fixed] [--inputs F ... | --input-set DIR] \
          [--seed-input TRAIN.bin] [--input-bank input] \
          [--qemu QEMU] --reps 30 [--seed N] --out W/timing
scopt run --campaign campaigns/X.toml --out W/timing     # frozen campaign file
```

| arm | what runs | notes |
|---|---|---|
| `nocache` | the base TOML with `[Machine].supercache` commented out, or `--nocache-config` | refused if the normalized config differs from the base in anything but `supercache`/`supercache_flat` |
| `empty` | the base TOML in a fresh run dir, nothing deployed (a cold run) | the cache it writes is reported (records, sizes) |
| `warm` | the unoptimized warm cache (primary reference), deployed through the WP8 oracle | without `--warm`, one cold run with the campaign QEMU records it first (`frozen/warm.sc`) |
| `identity`, `layout`, any other name | an artifact dir or `.sc` from `--artifact NAME=PATH`, deployed through the WP8 oracle | an arm named in `--arms` without an artifact becomes a `not_run` row |
| `layout-no-<pass>` | `--ablation PASS=PATH` (an artifact optimized with `--disable PASS`) | compared with `layout` (its parent) as well as with `warm` |
| `semantic`, `combined` | never run without an explicit artifact | otherwise reported `unsupported` (`capability_not_enabled`); supplied artifacts use canonical `.sc` v7 |

How a campaign is run:

- **Fresh run directories.** Every run gets its own WP8 run directory, `OUT/runs/<arm>/r<rep>[-i<input>]/`. Deploy arms go through `scopt deploy`: the oracle, a sparse copy, and `--check` on each deploy arm's first run. They are launched through `scopt.run.launcher.launch`, which applies `setarch -R`, the timeout, the environment scrub, the RAM reset and the §4.8 proof.
- **Inputs.** Each input is written into the RAM bank with `id = "input"` after the RAM reset. This is the fixture contract in `docs/fixtures.md`; the input bank's contents are not in the fingerprint.
  - `--input-set DIR` takes every input listed in a WP12 split directory's `MANIFEST.json` (for example `fixtures/inputs/heldout/freertos-mps2-compute`) and verifies each file's sha256 and size (`input_manifest_mismatch` otherwise). The split label comes from the manifest; a different `--set` is refused (`split_mismatch`). `--inputs` and `--input-set` are exclusive.
  - Explicit `--inputs` without `--set` are labelled `unlabelled`; `fixed` means a fixed-input workload (no inputs).
  - The `warm = auto` seed run uses `--seed-input` (default: the first timed input). When timing held-out or final inputs, give a train input, so the warm cache is not built on an input it is then measured on. The report states which input seeded the warm cache and whether it is also timed.
- **QEMU identity.** With `--qemu`, every run's QEMU (read from the run's log or oracle) must have the campaign binary's sha256, else the run is not a sample (`qemu_mismatch:<sha>`): a `qemu_path` in the TOML wins over `FASTDYN_QEMU` inside FastDyn and would otherwise switch binaries silently. `campaign.json` lists the distinct sha256 values seen (`qemu_seen`), and the report flags valid samples from more than one binary (`mixed_qemu`). B1/B2 campaigns use the WP-QINT binary the WP-QINT QEMU build; a B0-built cache deployed under it fails the post-run proof (the runtime signature differs), so let `warm = auto` record the warm cache with that binary.
- **Order.** Each repetition shuffles the inputs, and each (repetition, input) pair shuffles the arms, all with `random.Random(seed)`. The schedule is written to `campaign.json` before the first run. Runs with the same (repetition, input) are a *pair*.
- **Sample validity.** A run counts as a sample only if all of these hold: exit 0; no timeout; no stray `.scm`/`.qprof` files; BENCH printed; `PASS` for deploy arms, and `NOT_PROVED` with no cache file for `nocache`. An `INVALID_artifact_not_used` run is never a sample. Invalid runs are kept in `runs.jsonl` with their reason.
- **Refused configurations.** These are refused before any run: `enable_gdb`, `stop_on_start`, `launch_gdb`, `log_options` containing `nochain`, and `supercache_flat = true`.
- **Freezing.** Before the first run the campaign records the sha256 of every arm source, of the base and nocache TOMLs, of every input (and the seed input) and of QEMU. A campaign file's `sha256` that does not match is refused (`freeze_violation`). All of these hashes are recomputed after the last run; any change makes the campaign status `freeze_violation` (exit 1) and is listed per file in `freeze_check`.
- **Outputs.** `campaign.json`, `runs.jsonl` (one summary row per run) and `ledger.jsonl`. The ledger gets one stage-`measured` row per run and one per deploy arm. The artifact id is the artifact's `MANIFEST.sha256` id; a bare `.sc` gets `sc:<arm>:sha256:<hex>`, because it is not a published artifact.
- **Refused before any timing run (exit 2).** In addition to the configurations above: inputs or a seed input requested while the base (or nocache) TOML has no file-backed `[Memory]` bank with id `--input-bank` (`input_bank_missing`), an input larger than that bank (`input_too_large`), an unreadable TOML, a `[Machine].qemu_path` pinned to a binary other than `--qemu` (`qemu_path_conflict`: FastDyn would ignore `FASTDYN_QEMU`), and a `warm = auto` seed run whose QEMU sha256 is not `--qemu`'s (`warm_seed_qemu_mismatch`). Once `campaign.json` exists it is never left `running`: a refusal after preflight sets `status: refused`, a crash `status: error`, both with an `error` object.
- **Exit status.** 0 when every run is a sample; 3 when some runs are not samples; 1 on a freeze violation; 2 when the campaign is refused.

A campaign TOML (`[campaign]` and `[[arm]]` tables) is documented in
`scopt.run.arms.load_campaign`.

## `scopt report`

```
scopt report W [W2 ...] [--out W/report.md] [--profile P.json ...] [--reference ARM] \
             [--bootstrap 10000] [--seed 0] [--hot-coverage 0.9]
```

`scopt report` always exits 0. Missing, malformed or unsupported inputs become `not_run`/`unsupported` rows or entries under "Collection problems". It writes three files: `report.md`, `report.json` (the full model) and `timing.csv` (raw times).

It reads the following inputs:
- campaigns (`campaign.json` + `runs.jsonl`);
- loose WP8 run directories. The arm comes from the run-dir label `<campaign>/<arm>/<rep>`, else from the path (`<arm>/r<rep>/run.json` or `<arm>/run.json`). Runs without a repetition number are compared unpaired.
- `*ledger.jsonl` files;
- artifact directories (`cache.sc` plus a sidecar, `validation.json` or `MANIFEST.sha256`);
- Profile v1 files.

Report sections:

- **Arms:** one row per spec arm and per extra arm, with status `measured`, `not_measured`, `not_run`, `not_requested` or `unsupported`. `not_requested` means a campaign exists but none of them asked for the arm; `not_run` means the arm was asked for and has no run (for example `no_artifact`), or that no campaign exists at all (`no runs found`).
- **Outcome ledger:** stages are the columns `inspected`, `optimized`, `compiled`, `accepted`, `validated_dynamic` and `measured`. A cell with no ledger row is `not_run`; no stage is ever inferred from another. Per-run rows are aggregated per campaign arm. Unit rows are shown as verdict counts; unit `inspected` rows count once per (unit, snapshot), however many optimize runs repeated them. An artifact's candidate-id rows are joined to its artifact id through the sidecar's `x_candidate_id`; when several artifacts share one candidate id (the same input optimized twice), each keeps its own row and gets the candidate rows of the ledger inside its own directory.
- **Timing:** per arm, the median, IQR, min and max of BENCH (the guest window, i.e. vCPU throughput on fixed work), wall and children CPU time, plus max RSS and appended (fallback) records.
  - The paired speedup `t_ref/t_arm` is reported as the median of per-pair ratios, with a 95% percentile bootstrap CI over pairs (seeded), the geometric mean and the share of pairs that are faster. Verdicts: `faster`/`slower` when the CI excludes 1, otherwise `no_significant_difference`.
  - A pair with a zero, negative, missing or non-finite value on either side gives no ratio: it is listed under `invalid` (index, values, reason such as `arm_zero`) and counted in `n_invalid`; the other pairs are still analysed.
  - Every run is listed raw.
- **Sizes:** for each `.sc`:
  - arena capacity (`code_size`);
  - logical file size;
  - allocated bytes (`st_blocks` × 512);
  - populated native bytes (Σ `end − tc` over valid records);
  - host code bytes and used span.

  When the file is unreadable, not loadable (header problems) or has a truncated record array, every header- or record-derived value is null and the row reads `unknown (<reason>)`; it never shows 0. An empty or unrecognized `validation.json` (for example `{}`) renders as `validation: not_run`.
- **Hot-set locality (model):** profile entries are joined by UnitId to each layout. The section reports the hot units covering 90% of the exact steady entries, their code bytes, and the 64 B lines and 4 KiB pages they touch (code only, and with TB structs). Statuses other than `exact` are counted, never treated as zero. This is labelled as a model.
- **Regressions and anomalies:** listed, never dropped:
  - significant slowdowns of candidate arms;
  - ablations where removing a pass is faster;
  - output mismatches against the reference arm's run in the same pair;
  - invalid runs;
  - valid samples from more than one QEMU binary (`mixed_qemu`);
  - fallback translations;
  - nondeterministic guest output;
  - freeze violations;
  - layouts whose hot-set model is worse than warm.

WP11 additions:

- **Static locality model per artifact (MODEL):** the sidecar's `x_locality` (written by `scopt optimize --profile`): 50/90/99 % coverage rows (units, hot bytes, 64 B lines, 4 KiB pages, span) and hot-edge distance, for the input layout and the candidate layout. See `docs/b2-layout.md`.
- **Ledger with several candidates:** pass rows are scoped to their candidate (one table row per (pass, candidate), plus a "Pass outcomes per candidate" table; see `docs/b2-layout.md`); a cell with several rows lists every verdict with its count; `inspected` unit rows are counted once per (unit, input snapshot); other unit stages are labelled as sums over ledger files, and `units_by_source` (rendered when there is more than one ledger file) keeps them attributable.

  Slowdowns that are not significant, and the expected slowness of the baseline arms, are listed separately.
