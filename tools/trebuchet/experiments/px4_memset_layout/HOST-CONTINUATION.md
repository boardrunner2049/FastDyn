# PX4 host continuation

Fixed-address replay is available on this host: `setarch x86_64 -R true`
exits zero. The preserved original and FirmBOLT profile runs also exited zero
under that prefix and loaded their requested v5 caches at `0x7fff6c000000`.
They are not timing-ready, however: the runs appended 7,280 and 7,537
translation records respectively.

The configurable-phase admission fix was then exercised on both preserved
PX4 snapshots. It produced nonidentity artifacts with consumed profiles:

- original guest: 5,059 hot units accepted and all 47,005 units moved;
- FirmBOLT guest: 5,021 hot units accepted and 48,135 of 48,137 units moved.

Both artifacts passed V1-V8, including two-sided reimport and recovery checks.
This establishes activation and static validity, not runtime performance: the
profiles came from the unsealed runs above, and neither candidate has yet passed
an independent zero-append replay.

The correct continuation is **seal, profile, optimize, seal again**. Reimport
the expanded cache produced by the preserved warm run; profile that new
snapshot first so the profile is bound to its new SnapshotId; then optimize
and independently replay the active artifact. Do not reuse the old profile on
the expanded snapshot and do not time an identity artifact.

Run the following from the FastDyn repository root. The original-guest values
are shown first:

```bash
export PYTHONPATH="$PWD/tools/scopt"
PY="$PWD/scopt-env/bin/python"
QEMU="$PWD/third_party/common/qemu/build/qemu-system-arm"
W=/data/trebuchet-clean/work/scopt-px4-original
OUT=/data/trebuchet-results/px4-original-sealed
SC="$W/unpacked-prof/rep-0/cache/px4.sc"
SCM="$W/unpacked-prep/capture.scm"
MANIFEST="$W/unpacked-prof/rep-0/cache/px4.sc.fastdyn.json"
BUNDLE="$W/unpacked-bundle.json"
RUNTIME="$W/unpacked-runtime_identity.json"
CONFIG="$W/unpacked-prep/run.toml"
PARAMS="$W/b2-boot-params.json"
mkdir -p "$OUT"

"$PY" -m scopt.cli import --sc "$SC" --scm "$SCM" \
  --manifest "$MANIFEST" --bundle "$BUNDLE" \
  --runtime-identity "$RUNTIME" --store "$OUT/import" \
  --no-progress --json > "$OUT/import.json"
SNAP=$(find "$OUT/import/snapshots" -mindepth 1 -maxdepth 1 -type d -print -quit)

"$PY" -m scopt.cli profile run -c "$CONFIG" --snapshot "$SNAP" \
  --qemu "$QEMU" --out "$OUT/seal-baseline" --json \
  > "$OUT/seal-baseline.json"
jq -e '.verdict == "PASS" and ([.runs[].appended] | all(. == 0))' \
  "$OUT/seal-baseline.json"

"$PY" -m scopt.cli optimize "$SNAP" --pipeline b2-layout \
  --profile "$OUT/seal-baseline/merged.profile.json" --params "$PARAMS" \
  --store "$OUT/layout" --qemu "$QEMU" --json > "$OUT/optimize.json"
ARTIFACT=$(jq -er '.artifact_dir' "$OUT/optimize.json")
jq -e '([.pipeline[] | select(.outcome == "candidate" and (.affected | length > 0))] | length > 0) and (.input_snapshot_id != .output_snapshot_id)' \
  "$ARTIFACT/cache.sc.scopt.json"

"$PY" -m scopt.cli profile run -c "$CONFIG" --snapshot "$ARTIFACT" \
  --artifact --qemu "$QEMU" --out "$OUT/seal-candidate" --json \
  > "$OUT/seal-candidate.json"
jq -e '.verdict == "PASS" and ([.runs[].appended] | all(. == 0))' \
  "$OUT/seal-candidate.json"
```

For the FirmBOLT guest, use the same commands with these values:

```bash
W=/data/trebuchet-clean/work/scopt-px4-combined
OUT=/data/trebuchet-results/px4-firmbolt-sealed
SC="$W/prof/rep-0/cache/px4.sc"
SCM="$W/prep/capture.scm"
MANIFEST="$W/prof/rep-0/cache/px4.sc.fastdyn.json"
BUNDLE="$W/bundle.json"
RUNTIME="$W/runtime_identity.json"
CONFIG="$W/prep/run.toml"
PARAMS="$W/b2-boot-params.json"
```

If the baseline sealing replay still appends records, its isolated run cache
is the next import input. Repeat the import and baseline replay before
optimizing; retain every iteration and its `run.json`. A nonzero append count
is an unavailable timing result, not a regression.

The former Pye combined campaign cannot be resumed here. Its documented path,
`/workspace/trebuchet-work/empirical/runtime`, is absent, including its raw
six-warmup/two-scored-block records and boot-bound caches.
