# scopt

`scopt` is the offline optimizer for FastDyn QEMU supercache (`.sc`) snapshots. It
imports a cache that FastDyn's QEMU recorded, runs optimization passes over the cached
host code (profile-guided layout, cross-TB return specialization, state-sync
elimination), validates the result, and writes an artifact that FastDyn loads as an
ordinary warm cache.

## Pass defaults

Seven registered passes require explicit opt-in for new optimizations, including custom pipelines:
`local-scalar-cleanup`, `retire-ordinary-exits`, `eliminate-state-sync`, `specialize-returns`,
`chain-returns`, `specialize-edges` and `chain-static`. A skipped stage records
`disabled_by_default` in its pass receipt. For a controlled replay, supply
`--enable PASS` for each of these stages you want to exercise. `--only` narrows
the pipeline and `--params` changes parameter values. Both respect the defaults.

## Start here

- **Getting started** (fresh machine → first optimized artifact): `docs/getting-started.md`
- **QEMU** (the `scopt` QEMU branch, build options, binding to one executable, EnvLayout,
  helper effect table): `docs/qemu.md`
- **Workflow** (prepare → optimize → validate/publish → run → report, timing campaigns):
  `docs/workflow.md`
- **Native whole-program optimization** combines certified helper splices, guarded cross-TB
  specialization, state cleanup and LLVM layout. Select inlining options on one pass, for example
  `--pipeline host-inline=splice,faulting` or `--pipeline host-inline=extreme,4-level`. [Modes and benchmarking guide](docs/native-program.md#choosing-a-host-inline-mode).
- **Pass-writer guide** with a runnable example: `docs/pass-writer.md`,
  `tests/unit/test_pass_writer_example.py`
- **Design documents**: `docs/design/README.md` (B1/B2 design `docs/design/B1-design.md`
  is authoritative for the formats; B3/B4 in `docs/design/B3-B4-design.md`)

Reference:

- Contracts frozen by WP0, with clarifications and errata: `docs/contracts.md`
- Schemas: `scopt/formats/schemas/*.json` and `scopt/formats/layouts.py`. Check them with
  `scopt contracts check`
- One-command report: `tests/qemu/b5_campaign.py report` (`docs/b5-campaign.md`,
  `docs/timing-and-report.md`)
- Per-milestone pages: `docs/b1-gate.md`, `docs/b2-*.md`, `docs/b3-*.md`, `docs/b4.md`,
  `docs/b5-campaign.md`
- Milestone gate and evaluation reports are kept by the maintainer and are not part of
  the repository.

## Paths in these docs

Commands run from `tools/scopt` unless they say otherwise. Several pages describe how the
milestones were evaluated and name places outside the repository:

- `$FASTDYN_ROOT` is the workspace scopt's drivers write to (`runs/...`) and read
  fixtures from (`fixtures/...`). Set it to a directory of your own; tests that need the
  maintainer's recorded runs or fixtures skip when they are absent, with a message that
  names the missing path. Unset, it defaults to the directory that holds `tools/`
  (the FastDyn checkout root); `SCOPT_FASTDYN_SRC` (the FastDyn build scopt launches)
  defaults to that checkout. Both defaults are in `scopt/paths.py`. The few fixed
  maintainer paths scopt itself (outside the tests) looks up under `$FASTDYN_ROOT` are
  listed in `docs/getting-started.md` §5.
- The campaign files in `campaigns/` are frozen records of the maintainer's evaluation
  and name the maintainer's paths.
- "the maintainer's evaluation data", "evaluation report" and "notes" name material that
  is not in the repository.
- "WP-QINT QEMU" and "gate QEMU" are builds of the QEMU `scopt` branch (`docs/qemu.md`).

## Setup

```sh
python3.12 -m venv scopt-env
scopt-env/bin/pip install -e 'tools/scopt[test]'      # from the FastDyn checkout root
scopt-env/bin/scopt --help
cd tools/scopt && ../../scopt-env/bin/python -m pytest tests -m 'not qemu' -q
```

Python 3.12, capstone 5.0.3, pyelftools, jsonschema and click (pytest for the tests).
`docs/getting-started.md` covers QEMU, FastDyn and the environment variables.

Editable-install caveat: the `scopt` that the venv imports is the checkout it was
installed from. In another worktree, run the tests as `python -m pytest` from that
worktree's `tools/scopt`: pytest puts it first on `sys.path` (`pythonpath = ["."]`), so
its own copy is the one imported. Check with
`python -c "import scopt; print(scopt.__file__)"`.

## Working on a change

```sh
git worktree add ../FastDyn-mychange -b mychange      # a separate worktree per change
```

Commit on your own branch and merge it after review.

To keep parallel branches free of merge conflicts:

- **CLI.** Do not edit `scopt/cli.py`. Add a module `scopt/commands/<name>.py`
  that defines `register(group: click.Group)`; `scopt.cli` discovers it
  automatically.
- **Tests.** Add tests under `tests/unit/test_<wp-topic>.py`. If you need
  fixtures, put them in a `conftest.py` in your own test subdirectory, not in
  the shared `tests/conftest.py`.
- **Package `__init__.py` files** are empty on purpose. Do not add re-exports
  to them.
- **Stubs.** WP0 left some modules as stubs: `formats/sc_v5.py` (WP1) and
  `tests/synth/builders.py` (WP4/WP6). Keep their signatures and replace the
  bodies.

## Layout (design §5.1)

```
scopt/
  canon.py ids.py                    # WP0 (frozen)
  formats/enums.py layouts.py schemas.py schemas/*.json   # WP0 (frozen)
  formats/sc_v5.py                   # WP1 (WP0 stub: header/record structs)
  formats/{scm_v1,qprof_v1,profile_v1,bundle_v1,region_v1,artifact}.py  # WP4/6/8/7/10
  model/unit.py                      # WP0 (frozen interfaces); snapshot.py view.py: WP7/WP9
  analysis/api.py                    # WP0 (frozen §5.3 types); store.py etc.: WP9/WP7/WP11
  passes/base.py                     # WP0 (frozen §5.4 types); identity.py etc.: WP9/WP11
  decode/ importer/ run/ report/     # WP2, WP7, WP8/WP13, WP13
  cli.py commands/                   # CLI; commands/<name>.py per WP
tests/
  unit/ synth/ acceptance/ data/ qemu/
```

## QEMU side

scopt needs the QEMU `scopt` branch (`third_party/common/qemu`): FastDyn's supercache
plus the capture, profile and `.sc` v6 loader commits. `docs/qemu.md` describes each
commit, the build options and how scopt's data binds to one QEMU executable.

Build only the target you need: `make qemu-system-arm` (or `ninja qemu-system-arm`). A
full build fails to link `qemu-storage-daemon`, because the FastDyn icount commit
references `budget_*` symbols that are not in that tool.

`tests/qemu/base_warm_smoke.sh QEMU OUT_ROOT` runs the minimal fixture cold
and then warm, each in a fresh run directory.
