# Pass framework (WP9)

Design: `design/B1-design.md` §5.2-5.6 and §5.9. This page records how WP9
filled in the points the design leaves open. The module docstrings have the
details.

| Piece | Module |
|---|---|
| copy-on-write `WorkingSet` (layout facet + annotations) | `scopt/model/snapshot.py` |
| `WorkingSetView` (SnapshotView), `TBUnitView` (UnitView) | `scopt/model/view.py` |
| `AnalysisRegistry`, discovery, built-in `annotations@1` | `scopt/analysis/registry.py` |
| `AnalysisStore`, `AnalysisCtx`, invalidation, sqlite cache | `scopt/analysis/store.py` |
| `ProposalBuilder`, `Edit`, `Read`, `FactRef` | `scopt/proposal.py` |
| `PassManager`, `Candidate`, pipelines, pass registry | `scopt/manager.py` |
| ledger rows (`ledger.jsonl`) | `scopt/ledger.py` |
| `identity@1`, `move-units@1` (test-only PLACE) | `scopt/passes/{identity,move_units}.py` |
| pipelines | `pipelines/{identity,b2-layout}.toml` |
| B2 analyses and passes (WP11): `hotness@1`, `locality@1`, `hotcold-partition@1`, `cluster-hot@1`, `order-cold@1` | `scopt/analysis/{hotness,locality}.py`, `scopt/passes/{hotcold,cluster,order_cold}.py`; see `docs/b2-layout.md` |

## Conventions other WPs rely on

- **Registering analyses.** A module in `scopt/analysis/` defines
  `ANALYSES = (AnalysisSpec(...), ...)`, or (without `ANALYSES`) a single
  module-level `SPEC`; `default_registry()` imports it. WP6's
  `scopt/analysis/profile.py` is found through its `SPEC` (`profile.counts@1`).
- **Registering passes.** A module in `scopt/passes/` defines
  `PASSES = (PassClass, ...)`; `default_pass_registry()` imports it.
- **Facets.** `code`, `meta`, `layout` and `annotations`. The design lists
  `code`, `meta` and `layout`; ANNOTATE edits change only the `annotations`
  facet, so they do not invalidate code/layout analyses. `dep_units` hashes
  say which facet they are: `sha256:` is the unit content hash (code+meta),
  `layout:` the WorkingSet layout hash, `annot:` the annotation hash.
- **Analysis inputs.** A compute function reads only through its
  `AnalysisCtx` and only what its spec declares: `inputs` ⊆ {`code`, `meta`,
  `layout`, `annotations`, `profile`, `profile:<phase>`, `profile:*`, `bundle`}, and other
  analyses only if listed in `requires`. Otherwise `UndeclaredInput`. A pass
  may read through `ctx.analysis()` only the analyses its descriptor lists in
  `requires`.
- **Values.** `result_hash` digests the value: plain data, dataclasses, enums,
  mappings, sets, or objects (via `content_digest()` or `__dict__`/`__slots__`).
- **Certified facts.** A compute function calls `ctx.certify()`. Passes cite
  facts with `ctx.fact(...)` (attached to every later edit) or `cites=[...]`.
- **Profiles.** Profile views are WP6 `scopt.formats.profile_v1.ProfileView`
  objects (`ProfileView.open(profile, target)`). For a phase the manager uses
  `view.profile.phase_id(name)` (absent → `missing_profile`),
  `view.profile.phase_reached(pid)` (→ `profile_phase_not_reached`) and the
  match rate: `view.coverage` when `view.coverage_phase` is that phase and the
  view's target has the same unit set as the snapshot being optimized,
  otherwise the same ratio recomputed (share of the profile's entries in that
  phase that land on the snapshot's units). A `ProfileView.not_collected`
  view has no phases. Other view shapes (`phase_reached(phase)`/
  `match_rate(phase)`, or phase rows) are accepted too; see
  `scopt.analysis.store.profile_phase_info`. `PassContext.profile(phase)`
  returns the view (phase None: the first view carrying a profile).
  In an analysis, `ctx.profile(phase)` needs `profile:<phase>`; the inputs
  `profile` or `profile:*` allow any phase and `ctx.profile(None)` /
  `ctx.profile_view()`. The read records the profile_id (or a
  `no_profile:<phase|*>` marker, so attaching a profile later makes the result
  stale). `ctx.snapshot` is a snapshot facade over the ctx: `units()` without
  a content input returns UnitId-only handles (the unit set is covered by the
  snapshot_id).
- **Frozen edit args.** Edit args and annotation values are deep-frozen when
  proposed/applied (dict → MappingProxyType, list → tuple, set → frozenset);
  annotation hashes use the thawed JSON form.
- **Builder keywords.** Every B2 edit method also takes keyword-only
  `reads=` and `cites=` (the frozen positional signatures are unchanged).
- **Ledger.** Pass rows use stage `optimized` (added to the §5.9 list) with
  verdict = the `Outcome` value.

## Reason codes the manager emits

`Reason.*` from `scopt/passes/base.py`, plus `scopt.manager.R`:
`pass_not_registered`, `pass_version_unavailable`, `invalid_params`,
`ablation_disabled`, `domain_not_supported`, `region_scope_not_enabled`,
`profile_phase_not_reached`, `pinned`, `unknown_unit`, `bad_pass_result`,
`edits_with_no_change`, `edit_capability`, `edit_precondition`.

| Situation | Outcome | Reason |
|---|---|---|
| descriptor declares a kind the run does not enable (all B3/B4 kinds) | SKIPPED_UNSUPPORTED | `capability_not_enabled` |
| pass calls a B3/B4 builder method | FAILED | `capability_not_enabled` |
| pass calls a B2 method outside its capabilities | FAILED | `edit_capability` |
| uncaught exception | FAILED | `exception`; detail = `sha256:<traceback> <Type>: <msg>` |
| `BudgetExceeded`, run > `max_wall_s`, edits touch > `max_units` | BUDGET_EXHAUSTED | `budget_exhausted` |
| required phase: no profile / phase absent | SKIPPED_UNKNOWN | `missing_profile` |
| phase not reached / match below `min_profile_match` (default 0.95) | SKIPPED_UNKNOWN | `profile_phase_not_reached` / `profile_incompatible` |
| a `reads` entry is no longer current | FAILED | `stale_edit` |
| a cited fact is no longer current (or not certified) | FAILED | `stale_fact` |
| layout edit on a unit that is not `ok_relocatable` / pinned | FAILED | `not_relocatable` / `pinned` |
| unit laid out twice in a pass, or re-laid out without `overrides={"layout"}` | FAILED | `conflict` |

In every case except CANDIDATE the pass's edits are discarded and the
pipeline continues. Ledger `evidence_ref` is the edit digest for applied
edits, `sha256:<traceback>` when the pass raised (including a re-wrapped
EditCapabilityError), else null.

## Known limits (for WP10 and later)

- **PLACE is not range-checked here.** The manager checks alignment,
  relocatability, pins and conflicts, but not whether a PLACE target overlaps
  another unit or lies outside the arena. NativeLinker (WP10) and V1 must
  reject that.
- **Cited facts and `preserves`.** A fact cited from an earlier generation is
  checked against the dep_units it was computed with; if an intervening pass
  changed those units it is reported `stale_fact` even when that pass
  declared the analysis in `preserves` (preserved results are re-stamped in
  the store, but the cited object keeps its old dependencies). Re-read the
  fact in the current generation to cite it. Conservative by design.
- **stale_edit sources.** Reads recorded through the ctx are taken in the same
  generation the edits are checked against, so within one manager run
  `stale_edit` comes only from explicit `reads=` or from a non-deterministic
  analysis.
- **sqlite cache trust.** `analyses.sqlite` stores pickled results; loading
  it unpickles. Use only cache files this tool wrote locally; never load one
  from an untrusted source.
- **Named pipelines.** `load_pipeline("<name>")` reads
  `$SCOPT_PIPELINES_DIR/<name>.toml`, else `<repo>/pipelines/<name>.toml`
  (outside the package; a non-editable install must set the variable). A
  bare name never resolves to a file in the cwd.
- **CacheSnapshot.** WP7's `scopt.model.cache_snapshot.CacheSnapshot` exposes
  `capture_level` (1 for a bound capture); for a base snapshot without it the
  manager infers 1 when any unit has meta_status `ok`, else 0.
  `identity@1` declares `requires_capture_level=0` so it always runs.
- **Analysis scopes.** `annotations@1` is snapshot-scoped (every unit's
  annotation facet), so any ANNOTATE edit drops it; WP7's `refs@1` and
  `recovery@1` are unit-scoped on the content facet and survive layout and
  annotation edits; `layout@1` reads every unit's layout facet.

## Real-data check

`tests/acceptance/test_wp9_real.py` (markers `real`, `slow`) runs the manager
and the store on WP7 `CacheSnapshot`s imported from the real minimal capture
(163 TBs) and from the FreeRTOS capture (> 1000 TBs): identity pipeline
NO_CHANGE with an empty edit log, `b2-layout` without a profile changing
nothing, the real analyses through the store, ANNOTATE+PLACE invalidating
exactly `annotations`, `layout` and a layout dependent, and stale-edit /
capability failures. It skips when the `runs/` inputs are absent.
