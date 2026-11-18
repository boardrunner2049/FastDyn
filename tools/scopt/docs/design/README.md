# scopt design documents

These are the design documents scopt was built from, in the order the milestones were
implemented. The code is the final word where they differ; each document records later
changes in implementation notes at its end, and `docs/contracts.md` lists the
clarifications and errata of the frozen contracts.

| document | what it covers | read it when |
|---|---|---|
| [`B1-design.md`](B1-design.md) | Milestones B1/B2. The QEMU capture (`.scm`) and profile (`.qprof`) hooks, Profile v1, the snapshot/unit model, the pass framework (UnitView, PassContext, ProposalBuilder), the native linker for `.sc` v5, validation rows V1-V10, the run harness, and B2 profile-guided layout. | You work on import, the pass API, linking or B2 layout. Start here. |
| [`IMPLEMENTATION-PLAN.md`](IMPLEMENTATION-PLAN.md) | Work packages WP0-WP15 that implemented B1/B2: files, acceptance tests, dependencies. | You want to know which module or test came from which step. |
| [`B3-B4-design.md`](B3-B4-design.md) | Milestones B3/B4. Guarded cross-TB return specialization and chaining (`.sc` v6, the loader rules), EnvLayout, HostOps, the `hostsym` validator, B4 state-sync elimination (D1-D3), validation rows V11-V23, fixtures and the timing model. Implementation notes per work package at the end. | You work on B3/B4 passes, the v6 format or the validator. |
| [`IMPLEMENTATION-PLAN-B3B4.md`](IMPLEMENTATION-PLAN-B3B4.md) | Work packages WP16-WP30 for B3/B4 and the B5 evaluation campaign. | As above, for B3/B4. |
| [`llvm-pass-applicability.md`](llvm-pass-applicability.md) | Survey of LLVM/BOLT passes and what each would mean for an optimizer of TCG host code; shortlist and rejected ideas. | You look for the next pass to write. |
| [`closeout-logreg.md`](closeout-logreg.md) | Gate close-out: the FastDyn coverage `LOG_REG` block as one host event (`HostLog`), the flag-after-helper detector, and the re-gate plan. | You touch `scopt/decode/logreg.py`, syncdead or the coverage handling. |
| [`helper-inlining-design.md`](helper-inlining-design.md) | Proposed `hs-*` pass family (helper-call specialization, e.g. `MSR BASEPRI`). Finding F13 there led to the per-helper effect table (`docs/qemu.md`). | You extend helper handling or the helper effect table. |

## Conventions in these documents

- `src/qemu` is the QEMU tree (in this repository: `third_party/common/qemu`, branch
  `scopt`); `src/fastdyn` (or a `fastdyn/` prefix) is the FastDyn repository root;
  `scopt/` is `tools/scopt/scopt`. QEMU `file:line` citations refer to the tree named in
  each document's header.
- "WP-QINT QEMU" is the integrated build with the capture and profile hooks (QEMU branch
  `scopt` up to commit `a0681a6ac3`); "gate QEMU" is the build of the whole branch
  (`934890ed51`, with the v6 loader). `docs/qemu.md` lists the commits.
- The `qemu-sc-*` patch names are the QEMU commits of the `scopt` branch; the documents
  were written when they were still patch files.
- `fixtures/...` are the test firmware images and their sources, kept in the
  maintainer's workspace (`$FASTDYN_ROOT/fixtures`). The maintainer's evaluation data,
  evaluation reports and notes are cited by name but are not in the repository.
- `tools/scopt/independent/scparse.py` and `sc6parse.py` are the independent `.sc`
  parsers (`tools/scopt/independent/README.md`).
- Status marks such as **V** (verified), **I** (inferred), **U** (unknown),
  MEASURED/MODEL/DERIVED/ESTIMATE are defined at the top of each document.
