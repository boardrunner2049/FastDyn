# NativeLinker, ValidateAndPackage, artifacts (WP10)

Design: `docs/design/B1-design.md` §4.6-4.7, §5.6 step 6, §5.7-5.8. The module
docstrings hold the details. This page records how WP10 settled the points
the design leaves open.

| Piece | Module |
|---|---|
| placer, fixups, rollback, derived `.scm` | `scopt/link.py` (`NativeLinker`) |
| V1-V9, packaging, `optimize()`, re-validation, V10 | `scopt/validate.py` (`ValidateAndPackage`) |
| sidecar, MANIFEST, atomic publish, rejections | `scopt/formats/artifact.py` |
| `scopt optimize`, `scopt validate` | `scopt/commands/optimize.py` |
| test-only F-B2-2 pass | `scopt/passes/reverse_layout.py` (`reverse-layout@1`) |
| test pipelines | `tests/data/pipelines/test-{move-units,reverse-layout,reverse-move}.toml` |

## Workflow

```
scopt optimize STORE/snapshots/<hex> --pipeline identity|b2-layout|FILE.toml --store STORE \
      [--profile P.json ...] [--disable PASS] [--only PASS] [--params PARAMS.json]
  -> STORE/artifacts/<hex of ArtifactId>/   (exit 0)
  or STORE/rejections/<hex of candidate_id>.json   (exit 2; nothing published)
scopt validate STORE/artifacts/<hex>                  # re-run V1-V9, verify MANIFEST (read-only)
scopt validate A --dynamic -c BASE.toml [-c ...] --out W [--reps N]   # V10 (WP8 harness)
```

`--params` is a JSON file `{"<pass>": {<param>: <value>}}` laid over the
pipeline's params (for example, `move-units` moves for F-B2-1/F-B2-5).

## Points the design leaves open

- **What stays put.** Pinned units (not `ok_relocatable`, or PIN edits) are
  fixed obstacles. So is any relocatable unit that no layout edit names: B2
  moves only what a pass ordered into a section or PLACEd. Section units are
  laid out by the §5.7 cursor (`ROUND_UP(cursor, 64)`, bumped past
  obstacles), in the order hot, warm, unknown, boot, cold.
- **Rollback scope.** Per-unit failures are `rel32_range`, `imm_range`,
  `fixup_field`, `place_misaligned`, `place_below_prologue`,
  `place_out_of_range` (`end' >= code_size - host_page - 1024`) and
  `place_overlap`. Each failed unit goes back to its original offset when no
  other unit's explicit placement covers that interval. The placer then runs
  once more. Rolled-back units appear in the sidecar `pinned` list with
  reason `failed:<reason>`, and in `x_link.rollbacks`. If the original slot is
  taken (`rollback_slot_taken`), or the second run fails, the candidate is
  rejected with reason `failed:<first reason>`.
- **`abs_plus_delta` imm32** is accepted only while the result stays in
  `[0, 2^31)`, where the zero-extended and sign-extended readings agree.
  `code_address >= 4 GiB` makes this class pinned in practice anyway.
- **Derived captures.** One derived file is written per source capture that
  binds at least one slot on its own: `cache.scm`, then `cache.<k>.scm`. A
  capture whose slots are all bound through an earlier capture of the §2.5.8
  union produces no file. For example, the two FreeRTOS T-B1-1 captures give
  one `cache.scm`. RUN_INFO, HELPER, MAPS, RAMBLOCKS and END are copied
  unchanged. Rewritten TB chunks get:
  - the unit's **output** slot. `write_sc` packs only valid records, so after
    an input record that `load_records` skips (for example `record_checksum`),
    later units move to lower slots. `UnitLink.slot` and the sidecar
    `move_map` use the output slot too; `UnitLink.input_slot` keeps the
    input's;
  - the new record;
  - `code_fnv64` over the new `[tc', end')`;
  - site `value += d` for values inside the unit's own `[tb, end)`;
  - `final_field` re-read from the new bytes.
- **V3 and V4 from the decode.** Every reference is compared semantically:
  - offset inside the code;
  - own-struct offset;
  - the same absolute address.

  The "rel32 fits int32" part of V4 is checked by requiring every fixup field
  to decode to its original absolute target, since a wrapped field would
  decode elsewhere. The two-sided part of V3 is the re-import of the written
  `.sc` with the derived capture (V8). It must reproduce, per unit:
  - the classified sites, fixups, status, reasons and origin of the input;
  - no `recovery_mismatch` (V6).
- **V8 `manifest`.** `MANIFEST.sha256` lists `validation.json`, so
  `validation.json` cannot record the manifest's own verification. Its V8
  `manifest` row is `skip`. `publish()` verifies the manifest before the
  rename (a failure rejects the candidate with `V8:manifest_verify`).
  `scopt validate A` verifies it again, together with the sidecar's
  `validation_sha256` and the directory name, which must equal the hex of the
  ArtifactId the manifest gives (`V8 artifact_dir_name`).
- **Re-import order.** The derived captures are re-imported in derive order
  (`cache.scm`, `cache.1.scm`, `cache.2.scm`, ...; `artifact.derived_captures`),
  not in name order, which would put `cache.1.scm` first.
- **Publish race.** When another process publishes the same ArtifactId between
  the existence check and the rename, the rename fails with `ENOTEMPTY` or
  `EEXIST`. `publish()` then returns `exists` if the manifests are identical,
  and raises `artifact_conflict` otherwise. Any other rename error is
  `publish_rename`. It never lets a bare `OSError` escape.
- **V9** applies to every candidate that moves no unit. That includes the
  identity pipeline, `b2-layout` without a profile, and a full rollback. For
  a candidate that moves units it is `skip`. On a canonical input, the output
  sha256 must equal the input's (`status: identity`). On a non-canonical input
  (records skipped, stray bytes, a wrong size), the output must equal an
  independent canonical rewrite of the input (`sc_v5.identity_copy`). The row
  then passes with `status: identity_canonicalized`, and its `dropped` field
  lists `[{slot, reason}]`.
- **V7** requires the input snapshot to carry a bundle and a RuntimeIdentity
  (`identity_missing:*` otherwise), since deploy refuses artifacts without
  them (WP8). The identity documents are copied into the artifact
  byte-for-byte (canonical JSON).
- **Ledger.** The artifact's `ledger.jsonl` holds:
  - `inspected` rows. There is one per input unit, written once per optimize
    invocation through a shared `validate.Inspection`. There is also one
    artifact row, with verdict `pass`, `partial` or `fail` and a structured
    `counts` field;
  - the pass rows;
  - `compiled` rows for each moved unit and for the artifact. A unit that was
    rolled back after a link failure gets `compiled fail <reason>` with
    evidence `rolled_back`;
  - the `accepted` row.

  If publishing fails after `accepted: pass` was written, the rejection's
  ledger gets a later `accepted: fail` row. Its reason is
  `V8:<code>; supersedes accepted:pass@<ts>`, which explicitly supersedes the
  pass row.

  Artifact rows inside the artifact use the **candidate id**, because the
  ArtifactId is the hash of a manifest that covers the ledger. V10 writes its
  `validated_dynamic` row, under the ArtifactId, to `<out>/ledger.jsonl`. The
  artifact itself is immutable.
- **ArtifactId stability.** The id is content-addressed, but `ledger.jsonl`
  (timestamps) and the sidecar pipeline rows (`duration_s`) differ between
  runs. Re-optimizing the same input therefore gives a new ArtifactId with an
  identical `cache.sc`.
- **V10** (`validate_dynamic`) runs each `-c` config (held-out input) `reps`
  times. Each repetition deploys the artifact and the baseline into fresh WP8
  run directories with the deploy oracle; the baseline defaults to the input
  snapshot's store directory. The two arms are launched interleaved. V10
  requires, on both arms:
  - proof PASS;
  - equal exit codes;
  - equal `norm_b0` and `norm_guest` output.

  Appended counts are reported. The QEMU binary defaults to the
  RuntimeIdentity's `qemu_exe_stat.path`.
- **Fixed points of `reverse-layout`.** Mirrored packing can leave a unit at
  its original offset (one unit on minimal). Its `exclude` param, followed by
  `move-units` (`test-reverse-move.toml`), moves every unit.
