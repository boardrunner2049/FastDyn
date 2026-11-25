# QEMU for scopt

scopt works on the persistent TCG cache ("supercache", `.sc`) of FastDyn's QEMU fork.
It needs three things from QEMU that the stock FastDyn fork does not have: a capture of
the relocation metadata of every cached translation block (`.scm`), an exact software
profile of a warm run (`.qprof`), and the canonical `.sc` v7 loader used by every newly
published artifact. They live on the `treb-wip` branch of `https://github.com/boardrunner2049/qemu.git`,
which is what this repository's `third_party/common/qemu` submodule is pinned to.

Build steps are in `docs/getting-started.md` §3. This page explains the branch, what
each part is for, and how scopt's data is tied to one QEMU executable.

## The branch

Base: `12f5e68807` "tcg: add persistent supercache support" (branch `fastdyn`), the
FastDyn QEMU with the supercache backend (`tcg/supercache.c`, `.sc` v5). On top of it:

The version names in the next table describe historical commits. They are accepted
ancestry, not alternative current publication formats; current capture and publication
use canonical v7.

| commit | title | what it adds | used by |
|---|---|---|---|
| `00c19f7ee0` | fastdyn: plugin icount compat patch | FastDyn's `patches/qemu-fastdyn-plugin-icount.patch` as a commit: virtual-time plugin API (`qemu_plugin_set_budget_exact_stop`, virtual-time budget timers), QOM/QMP additions | FastDyn itself (all runs) |
| `2dfce24b2a` | tcg/supercache: base hooks for scc capture and scp profile | small accessors (`tcg_supercache_buffer`, `tcg_supercache_loaded_max_end`, `tcg_target_tb_ret_addr`) and empty capture/profile modules wired into the build, so the next two commits touch disjoint files | the two commits below |
| `409e90ca08` | tcg/sc-capture: Tier 0 preparation capture | `QEMU_SC_CAPTURE=/abs/path.scm`: for every TB committed to the `.sc`, record its relocation sites (helper calls, branches, label jumps, exits, pool entries, slow paths), guest exits, code/guest hashes, host maps and RAM blocks, in the chunked `.scm` v1 format; observe only | `scopt prepare`, `scopt import` |
| `a0681a6ac3` | accel/tcg: supercache exact software profile | `QEMU_SC_PROFILE=/abs/run.qprof` (+ `QEMU_SC_PROFILE_PHASES=name@0xVADDR,...`): run a warm cache with chaining disabled at run time only, count every TB entry and the edge into it, write `.qprof` v1 at exit | `scopt profile run` |
| `934890ed51` | tcg/supercache: .sc v6 loader | load and arm the `.sc` v6 extension table (variants, guarded slots, SPEC links); disarm on TLB flush, breakpoints, single-step and plugin changes; `QEMU_SC_V6_STATS=/abs/file` counters. v5 files take the unchanged path; this binary still writes v5 | B3 artifacts, the v6 dynamic oracles |
| `ff8ec2f` through `2de4376` | firmware-region v7 loader | validate, load, invalidate and index safe/trusted private firmware regions | `firmware-inline` artifacts |
| `bd331f8` | tcg: make v7 the canonical supercache format | write and load public-only and semantic-extension artifacts under the single top-level v7 format | every newly captured or published cache |
| `af4905c`, `86f583a` | sealed whole-firmware replay | exhaustive immutable-ROM coverage, fail-closed replay, optional interrupt-poll bypass, and cached hot-loop configuration | trusted sealed caches |

The `treb-wip` runtime also supports `QEMU_SC_SEALED=1` for a verified
whole-firmware cache.  Production uses `QEMU_SCOPT_SEALED_RANGES` and
`QEMU_SCOPT_SEALED_TRACE`; replay uses a direct exact-PC index and fails on a
missing key instead of invoking the JIT.  This opt-in contract removes guest
hash validation and in-QEMU fault recovery.  It retains per-TB interrupt polls
by default; `QEMU_SC_SEALED_SKIP_IRQ=1` is a separate unsafe opt-out.  Full
commands and limitations are in [`sealed-firmware.md`](sealed-firmware.md).

The current submodule pin is `86f583abc6`. It includes the earlier restore and FastDyn
runtime integration, firmware-region loading, and canonical v7 publication. The pinned
source is ready to build.

The capture and profile modes run only in preparation runs, are mutually exclusive, and
refuse a flat supercache. Nothing on the normal vCPU path changes when their variables
are unset (every hook is behind an `unlikely` flag). scopt sets and clears these
variables itself and scrubs every `QEMU_SC_*` variable from the environment of other
runs.

The maintainer's binaries, for reference (their data ships with scopt):

| name | tree | sha256 | role |
|---|---|---|---|
| WP-QINT | base + first four commits | `8864c72671971cf4…5f41` | B1/B2 evaluation |
| gate | the original v6 loader (`934890ed51`; same tree as the maintainer's tag `qint-v6-gate-1`) | `56d8bfb32c91c970…0d90` | B3, B4, B5 evaluation |

Your own build has a different sha256 even from the same sources; see "Binding" below.

### The icount patch

The FastDyn tree keeps `patches/qemu-fastdyn-plugin-icount.patch` for QEMU checkouts
that do not have it. On this branch it is already commit `00c19f7ee0`. `setup.sh`
detects that with `git apply --reverse --check` and skips it. Applying it by hand
(`git apply`) fails on this branch; that failure is expected.

## Build options

```sh
cd third_party/common/qemu && mkdir -p build && cd build
../configure --target-list=arm-softmmu --enable-plugins
make -j"$(nproc)" qemu-system-arm
```

- **Debug information is required.** `scopt envlayout produce` reads the layout of
  `CPUArchState` and `CPUState` from DWARF with `gdb -nx -batch 'ptype/o …'`, and the
  gdb-based diagnostics need symbols. QEMU's default configuration builds with `-g`
  (the maintainer's builds report `debug=true`, `optimization=2` in
  `build/meson-info/intro-buildoptions.json`). Do not strip the binary and do not
  configure with `--disable-debug-info`.
- `--enable-debug-tcg` is optional: it adds the profile mode's lookup assertion.
- Build only `qemu-system-arm`. A full `ninja` fails to link `qemu-storage-daemon`,
  because the icount commit references `budget_*` symbols that tool does not have.
- Run gdb from outside the QEMU tree, with `-nx`: the tree has a `.gdbinit`.

## Binding: everything is tied to one executable

The supercache runtime signature covers the QEMU executable, so a `.sc` written by one
binary is discarded by another. scopt goes further and binds its own per-executable
data to the sha256 of `qemu-system-arm`:

- **RuntimeIdentity** (`scopt bundle runtime-identity`) records `qemu_sha256`, the plugin
  and the `.sc` header fields. `import`, `profile`, `deploy` and `validate` compare it
  with the files in use and refuse a mismatch.
- **EnvLayout** (`scopt/formats/envlayouts/<sha hex>.json`, plus the directories in
  `SCOPT_ENVLAYOUT_PATH`): env-relative offset of every field reachable from the TCG
  env pointer. B3 guards, the observation contract and HostOps typing read field names
  only through it. Without a layout for the executable, B3/B4 refuse with
  `guard_layout_unbound`.
- **Helper effect table** (`scopt/formats/helper_effects/<sha hex>.json`, plus the
  directories in `SCOPT_HELPER_EFFECTS_PATH`): for each TCG helper, what it may read and
  write in env, whether it touches host memory, whether it may fault. TCG's call flags
  (`TCG_CALL_NO_RWG`, ...) only speak about TCG globals, so they are not enough. A
  helper without a row, or every helper when no table is bound, is "fail closed": it
  may read and write all of env and host memory and fault. Artifacts stay correct, but
  B3/B4 then cannot move or drop state across helper calls.
- **v7 feature capability**: all new artifacts are top-level v7. Public-only artifacts
  require the canonical loader. Semantic-extension admission still uses the compatibility
  names `SCOPT_V6_EXES` / `--v6-exe`; composite firmware regions use
  `SCOPT_V7_COMPOSITE_EXES` / `--v7-exe`, and trusted regions use
  `SCOPT_V7_TRUSTED_EXES` / `--v7-trusted-exe`. These are feature capability sets,
  not competing output versions.

Any rebuild (even of the same sources) changes the sha256 and the runtime signature.
After a rebuild: prepare again (old snapshots and artifacts do not carry over), produce
the EnvLayout, rebind the helper effect table, and update the applicable capability sets.

### Regenerate the EnvLayout

```sh
Q=third_party/common/qemu/build/qemu-system-arm
QSHA=$(sha256sum $Q | cut -d' ' -f1)
scopt envlayout produce --exe $Q --out "$SCOPT_ENVLAYOUT_PATH/$QSHA.json"
scopt envlayout show --exe-sha256 sha256:$QSHA
```

The producer runs gdb from a fresh temporary directory, expands nested structs and
arrays, and checks `offsetof(ArchCPU, env) == sizeof(CPUState)`. The file name must be
the executable's sha256 in hex. The layout's `envlayout_id` is a content hash of the
whole document, and the document records `exe_sha256`, so every build gets its own id,
even from the same sources. What the same sources and compiler do reproduce is the
field list: a fresh build of the `scopt` branch gave the same 2904 fields and offsets as
the gate binary's layout, under a different `envlayout_id`.

### Adopt the helper effect table for a new build

The rows of a table were checked by hand against the helper sources listed in its
`qemu_source` (`files`, with a sha256 of each in `file_sha256`; `commit` names the tree
they were read in). For the shipped gate table that commit is `4671d5ceda`, the
maintainer's commit before the branch was rewritten for publication; it is not on
GitHub, and `934890ed51` has the same tree. If those files are unchanged in your tree, the rows still hold for
your build and you can copy the table:

```sh
scopt helper-effects rebind \
  --from-exe-sha 56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90 \
  --exe $Q [--qemu-src third_party/common/qemu] [--out FILE]
```

- `--qemu-src` defaults to the tree that holds `build/qemu-system-arm`.
- The command hashes each listed file under `--qemu-src` and compares it with
  `file_sha256` (for an older table without hashes it reads the files of
  `qemu_source.commit` with git, which needs that commit in `--qemu-src`). On any
  difference it writes nothing, lists the differing or missing files, and exits 1.
- On a match it writes the same rows bound to the new sha256, with `file_sha256` and
  `rebound_from` recorded and `table_id` recomputed, to `--out` or to
  `<first SCOPT_HELPER_EFFECTS_PATH directory>/<sha hex>.json`. It refuses to overwrite
  a different existing file without `--force`.
- `scopt helper-effects show --exe $Q` prints the table now bound to an executable.

When the sources differ, write a new table: start from a copy, re-read every listed
file (and the source of any helper your snapshots call that has no row; the `.scm`
capture lists every helper with its TCG flags: `scopt meta dump --jsonl --type HELPER
capture.scm`), fix the rows, update `files`/`file_sha256`/`commit`, set `exe_sha256`, and
recompute `table_id` (`HelperEffectTable.build(...).to_json()` in
`scopt/analysis/helper_effects.py`). The format and the rules each row must satisfy are
documented at the top of that module. The design behind the table is
`docs/design/helper-inlining-design.md` (finding F13).

## Running QEMU directly

Everything above runs through scopt, which launches FastDyn under `setarch x86_64 -R`
with a scrubbed environment and a fresh run directory. When you run QEMU or FastDyn
yourself with a supercache, disable ASLR the same way, keep `supercache_flat = false`,
and use icount `shift = 0` for repeatable runs.

### Trusted development replay

Set `QEMU_SC_TRUSTED=1` on a compiled QEMU launch when the caller vouches for
the unchanged cache and matching immutable guest image. This skips public
record checksums, variant/guard hashes, private-code digests, and comparison
of guest bytes at block admission. Normal warm loading still permits
translation and appending on a cache miss. Add `QEMU_SC_SEALED=1` for exact
sealed operation. The region-specific interrupt/recovery contract is encoded
in the compiled artifact and is separate from this input trust setting.

The loader retains its one-time header and runtime-layout compatibility checks,
record commitment and bounds checks, region placement and branch target checks,
and live invalidation checks. `scopt load --trusted` likewise skips the public
record checksum calculation during offline inspection. Trusted PGO training
propagates the trust setting to its diagnostic QEMU processes. Table lookups use ordinary indexing hashes.
Recording fresh translations still writes the standard record format. With
`QEMU_SC_V7_STATS=/absolute/path.json`, verify `user_trusted_input` and compare
`public_record_checksum_checks`, `guest_hash_checks`, and
`artifact_hash_validations` in a separate diagnostic replay.

### v7 dispatcher-return counters

The `QEMU_SC_V7_STATS` file keeps the schema `qemu-sc-v7-stats@1`. Files with
`"counters_revision": 2` also report how often execution came back to the
dispatcher:

- `loop_returns_nonnull`: returns to `cpu_exec_loop` that left a chainable
  `last_tb`. This is counted even when no v7 directory is loaded, so configurations
  with and without regions can be compared.
- `trusted_residual_exits`: the subset of those returns that left a trusted
  region descriptor. It is split by goto_tb slot (`trusted_residual_exits_slot`)
  and by region (`trusted_residual_exit_regions`, with `region`, `region_id8`,
  entry `pc`, `exits` and `benchmark_exits`, listing only regions with exits).
  Exits through `exit_tb(0)` and longjmps have no `last_tb`, so this is a lower
  bound on region-caused dispatcher round trips.
- `trusted_tb_chain_probes`: trusted-membership lookups made by the generic
  chaining path.
- `chain_installs_trusted_source`: installed goto_tb patches whose source is a
  trusted region descriptor.
- `benchmark_*`: the same counters, restricted to the restore benchmark window.

`"counters_revision": 3` (QEMU with chainable trusted residual exits) adds:

- `trusted_residual_exits_chainable` / `trusted_residual_exits_unchainable`: the
  split of `trusted_residual_exits` by whether the exit left through an armed
  residual goto_tb slot of a per-member descriptor. Once a slot is chained it no
  longer returns to the dispatcher, so in the chaining arm these count first uses
  and re-chains rather than traffic.
- `trusted_residual_slots_armed`: slots armed when trusted regions were (re)built;
  0 for legacy shared-descriptor caches, `no-trusted-exit-chain` and
  `QEMU_SC_SEALED_SKIP_IRQ`.
- `chain_allowed_trusted_source`, `chain_skipped_trusted_to_trusted`: chains from a
  trusted source to an ordinary TB, and refusals because the destination is a
  trusted region entry. Every chain attempt now probes both ends, so
  `trusted_tb_chain_probes == 2 * (chain_attempts - chain_skipped_global)`.

The experiment object gains `no_trusted_exit_chain`
(`QEMU_SC_V7_EXPERIMENT=no-trusted-exit-chain`).

`trusted_tb_calls` counts trusted-membership lookups (non-null dispatcher
returns plus chain probes), not region exits. In trusted mode it equals
`loop_returns_nonnull + trusted_tb_chain_probes`.
`scopt.run.v7_stats` holds these definitions. `counter_problems` checks the
invariants, and `scopt rundir launch --v7-stats` and the firmware-inline
ablation diagnostics report or reject any violation. For older files without
`counters_revision`, `legacy_loop_returns` estimates the dispatcher returns as
`trusted_tb_calls - 2 * (chain_attempts - chain_skipped_global)`. This is
still a return count, not a region-exit count.
