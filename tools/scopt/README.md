# scopt

`scopt` is the offline optimizer for FastDyn QEMU supercache (`.sc`) snapshots.

- Design (authoritative): `/data/extra_copy/fastdyn/design/B1-design.md`
- Work breakdown: `/data/extra_copy/fastdyn/design/IMPLEMENTATION-PLAN.md`
- Contracts frozen by WP0, with clarifications and errata: `docs/contracts.md`

## Start here (deliverables)

- **Workflow** (prepare → optimize → validate/publish → run → report): `docs/workflow.md`
- **Pass-writer guide** with a runnable example: `docs/pass-writer.md`, `tests/unit/test_pass_writer_example.py`
- **Schemas:** `scopt/formats/schemas/*.json` and `scopt/formats/layouts.py`. Check them with `scopt contracts check`; the contract table is in `docs/contracts.md`
- **One-command report:** `tests/qemu/b5_campaign.py report` (`docs/b5-campaign.md`, `docs/timing-and-report.md`)
- **Milestone reports and the requirement → evidence map:** `/data/extra_copy/fastdyn/DELIVERABLES.md`

## Setup

```sh
/data/extra_copy/fastdyn/tools-env/bin/pip install -e /data/extra_copy/fastdyn/tools/scopt
/data/extra_copy/fastdyn/tools-env/bin/scopt --help
cd /data/extra_copy/fastdyn/tools/scopt && /data/extra_copy/fastdyn/tools-env/bin/python -m pytest
```

Python 3.12, capstone 5.0.3, pyelftools, jsonschema, click and pytest are all
installed in `tools-env`.

Editable-install caveat: the `scopt` that `tools-env` imports is the one in
`tools/scopt`, i.e. `main`. Inside a WP worktree, run the tests as
`python -m pytest` from the worktree root. pytest puts the worktree first on
`sys.path` (`pythonpath = ["."]`), so the worktree's own copy is the one
imported. Check with `python -c "import scopt; print(scopt.__file__)"`.

## Working on a WP

```sh
git -C /data/extra_copy/fastdyn/tools/scopt worktree add /data/extra_copy/fastdyn/wt/scopt-wpN -b wpn main
```

Commit only on your own branch. A separate step merges branches into `main`.

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

`/data/extra_copy/fastdyn/patches/qemu-sc-base.patch` (WP0) goes on top of the
FastDyn compat commit. The reference worktree is
`/data/extra_copy/fastdyn/wt/qemu-wp0` (branch `wp0`):

- commit 1 is the compat patch;
- commit 2 is the base patch;
- the build is in `build/`, configured with
  `../configure --target-list=arm-softmmu --enable-plugins` using the
  fastdyn-env meson 1.12.1, the same as `src/qemu/build`.

Build only the target you need: `ninja -j12 qemu-system-arm`. A full `ninja`
fails to link `qemu-storage-daemon`, because the compat patch references
`budget_*` symbols that are not in that tool. The pristine tree has the same
failure.

`tests/qemu/base_warm_smoke.sh QEMU OUT_ROOT` runs the minimal fixture cold
and then warm, each in a fresh run directory.
