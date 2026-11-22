# Writing a pass (pass-writer guide)

Assignment §4 "Interface for pass writers" asks for immutable unit views, shared
analyses, and transforms that submit edits instead of writing cache files. In
scopt that contract is:

```python
class MyPass:
    descriptor: ClassVar[PassDescriptor]
    def run(self, unit_or_snapshot, ctx: PassContext, edits: ProposalBuilder) -> PassResult: ...
```

| spec name (§4) | scopt type | module |
|---|---|---|
| UnitView / SnapshotView | `TBUnitView`, `WorkingSetView` (read-only; `units()`, `uids()`, `is_relocatable`, `is_pinned`, refs, recovery, guest origins) | `scopt/model/unit.py` (frozen interfaces), `scopt/model/view.py` |
| PassContext + AnalysisStore | `ctx.params`, `ctx.analysis(name, scope)`, `ctx.fact(...)`, `ctx.profile(phase)`, `ctx.budget()` | `scopt/passes/base.py`, `scopt/analysis/store.py` |
| ProposalBuilder | `annotate`, `pin`, `layout_order`, `place` (B2); `clone`, `redirect_exit`, `add_entry`, `pure_rewrite`, `attach_fact` (B3/B4, typed payloads) | `scopt/proposal.py`, payload types in `scopt/passes/base.py` |
| PassResult | `Outcome` = `no_change`, `candidate`, `skipped_unsupported`, `skipped_unknown`, `budget_exhausted`, `failed`; plus a stable `reason`, `affected` ids, `guest_origins`, `stats` | `scopt/passes/base.py` |
| Descriptor | name/version, `HostCache` domain, `unit_scope`, `requires` (analysis, version), `requires_profile_phase`, `capabilities` (edit kinds), `budgets`, `preserves`/`invalidates`, `params_schema`, `overrides` | `PassDescriptor` in `scopt/passes/base.py` |
| Candidate acceptance | `PassManager` (checks, invalidation, ledger) then `ValidateAndPackage` (NativeLinker, V1-V9; `package_b3`/`package_b4` add V10-V23 incl. hostsym) | `scopt/manager.py`, `scopt/manager_b3.py`, `scopt/validate.py` |

The manager rules (reason codes, what is discarded, stale facts) are in
`docs/pass-framework.md`. The B3/B4 additions (typed payloads, slot arbitration,
templates per pass) are in `docs/b3-passes.md` and `docs/b4.md`.

`requires_profile_phase` is the pass's default admission phase. A pass that declares
`phase: str` in `params_schema` may select another phase; admission, match checking,
and the profile receipt all use the effective parameter value.

## Worked example 1: a minimal pass (runnable)

`tests/unit/test_pass_writer_example.py` holds this pass and runs it through
`PassManager` on a synthetic snapshot:

```python
from scopt.passes.base import Budgets, EditKind, Outcome, PassDescriptor, PassResult

class TagLargeUnits:
    """Annotate every unit whose host code is at least `min_bytes` long."""
    descriptor = PassDescriptor(
        name="tag-large-units", version=1, unit_scope="snapshot",
        requires_capture_level=0,                     # needs no .scm metadata
        capabilities=frozenset({EditKind.ANNOTATE}),  # the only edit kind it may propose
        budgets=Budgets(max_wall_s=5.0),
        preserves=frozenset({"*"}),                   # annotations change no code/layout
        params_schema={"min_bytes": int})

    def run(self, snap, ctx, edits) -> PassResult:
        limit = ctx.params.get("min_bytes", 64)
        big = [u.uid for u in snap.units() if u.tc_size >= limit]
        if not big:
            return PassResult(Outcome.NO_CHANGE, "no_large_units")
        for uid in big:
            edits.annotate(uid, "size_class", "large")
        return PassResult(Outcome.CANDIDATE, "tagged", affected=tuple(sorted(big)),
                          stats={"tagged": float(len(big))})
```

The test checks three behaviours:
- the manager records the outcome, the edits and one ledger row (`stage = optimized`);
- `disable=("tag-large-units",)` (CLI `--disable`) gives `skipped_unsupported` / `ablation_disabled` with no edits;
- a variant that also calls `edits.layout_order(...)` without declaring `LAYOUT_ORDER` fails with `edit_capability`, and all its edits are discarded.

Run it with `python -m pytest tests/unit/test_pass_writer_example.py`.

## Worked example 2: the design's §5.10 example (profile-driven)

`docs/design/B1-design.md` §5.10 gives `hotcold-partition` as the pass-writer example. It
requires `profile.counts@1` and `refs@1`, needs the `steady` phase at
`min_profile_match = 0.95`, and annotates each unit `hot`, `warm`, `unknown`,
`boot_only` or `cold_zero`. `tests/unit/test_pass_framework.py`
("The §5.10 pass-writer example runs unchanged against this framework") copies it
verbatim and runs it, followed by a small LAYOUT_ORDER pass, `order-by-section`.
The production version is `scopt/passes/hotcold.py`.

## Shipping a pass

1. **Put the class in `scopt/passes/<name>.py` and export `PASSES = (MyPass,)`.**
   `default_pass_registry()` finds it. Analyses go in `scopt/analysis/<name>.py` with
   `ANALYSES = (...)` or `SPEC`.
2. **Add a pipeline TOML in `pipelines/`.** It lists the passes and their params, and
   `allowed_capabilities` must cover every edit kind the passes declare (see
   `pipelines/identity.toml`, `b2-layout.toml`, `b3.toml`).
3. **Run it:**
   `scopt optimize SNAP --pipeline <name> --store STORE [--profile P] [--disable PASS | --only PASS]`.
   - Exit 0 means the artifact was published.
   - Exit 2 means the candidate was rejected. The reason is in `STORE/rejections/`, and the baseline is untouched.
4. **Test it.**
   - Add unit tests under `tests/unit/`.
   - For layout passes, include a link and validate case like `tests/unit/test_wp10_link.py`.
   - For B3/B4 passes, include a mutant that must be rejected (`tests/mutation/`, `scopt b4 mutants`).

Rules a pass must keep (they are enforced, not just conventions):
- A pass never mutates the snapshot or writes cache bytes. It only proposes edits.
- It reads only the analyses its descriptor `requires`.
- It cites certified facts (`ctx.fact`); a fact that is stale at apply time fails the pass with `stale_fact`.
- Profiles choose priority and placement only. A profile is never a correctness fact: B3 facts come from captured semantics or the patch plan, and V14 certifies them.
- `unsupported` and `unknown` are reported as `skipped_*`, never as `failed`. `failed` means a bug in the candidate (spec C6).

Note on the frozen B5 campaign: `check` hashes the `scopt/` and `pipelines/` git trees.
Adding a pass there changes those ids, so `campaigns/b5-freertos.toml` would no longer
check. Develop new passes on a branch; a new campaign would need a new, disclosed freeze.
