# Implementation plan for B1 and B2 (work packages for parallel worktrees)

This plan implements `B1-design.md` (cited as §n). Each work package (WP) is self-contained, with its own files, inputs and outputs, acceptance test and dependencies. Engineers work in separate git worktrees.

## Ground rules (apply to every WP)

### Repositories and worktrees

- **Python.** WP0 creates the `scopt` repo at `tools/scopt` with `git init`. Each WP works in `git worktree add ../wt-scopt-<wp> -b <wp>` and merges to `main` after review.
- **QEMU.**
  - Create worktrees from `src/qemu` with `git -C src/qemu worktree add ../wt/qemu-<wp> -b <wp> 12f5e688` (a worktree per QEMU work package).
  - First apply `patches/qemu-fastdyn-plugin-icount.patch` (QEMU commit `00c19f7ee0`). The compat patch exists in `src/qemu` only as uncommitted working-tree changes; commit it as the first commit of each branch.
  - Then apply patch `qemu-sc-base` (now QEMU commit `2dfce24b2a`) from WP0.
  - Build in the worktree's own `build/` directory with the same configure options as `src/qemu/build`. Read them from `build/meson-info/intro-buildoptions.json`: optimization=2, debug=true.
- **Do not touch `src/qemu`.** Its sources and build are not modified until WP-QINT, and that WP needs explicit maintainer approval first. Each QEMU WP delivers a patch file. (The patches are now the commits of the QEMU `scopt` branch, see `docs/qemu.md`.)
- **Do not touch `src/fastdyn`.** Its sources are never modified. The FastDyn shim (WP8) lives in scopt.

### Runtime rules (from the spec, the maintainer's decisions and the review)

- QEMU is always started through `setarch x86_64 -R`.
- `supercache_flat=false`.
- Each run gets a fresh run directory with its own cache, work and RAM directories, and RAM files are reset.
- Profiles are software-only: no perf, no sudo.
- **Never run `gdb` without `-nx`, and never with cwd inside `src/qemu`.** Its `.gdbinit` starts QEMU.

### Shared contracts

The shared contracts are frozen by WP0. Changing any of them needs a design-doc edit and a review from every dependent WP:
- the binary layouts of `.scm` v1 (§2.4-2.5) and `.qprof` v1 (§3.5);
- the id definitions (§4.1);
- the CacheRegion v1 and Profile v1 JSON schemas (§3.8, §4.5);
- the pass API (§5.2-5.5).

### Testing

- Tests use `pytest`. Each WP adds tests under `tests/` in scopt; QEMU WPs add smoke scripts under `scopt/tests/qemu/`.
- Tests read real fixtures from the maintainer's evaluation data (read-only; not in the repository) and from `tests/data/`, which holds small extracted slices.

---

## Dependency graph (→ = "needed by")

```
WP0 ─┬→ WP1 ─┬→ WP2 ─┐
     │       ├→ WP8  │
     ├→ WP4 ─┼───────┼→ WP7 ─┬→ WP10 ─┬→ WP14 (B1 gate) ─→ WP15 (B2 gate)
     ├→ WP6 ─┼───────┼───────┼→ WP11 ─┘       ↑
     ├→ WP9 ─┴───────┴───────┘                │
     ├→ WP3 ─┐                                │
     ├→ WP5 ─┴→ WP-QINT ──────────────────────┤
     ├→ WP12 ─────────────────────────────────┤
     └→ WP13 ─────────────────────────────────┘
```

**Parallel start after WP0** (one or two days):
- WP1, WP3, WP4, WP5, WP6, WP9, WP12 and WP13 can all start immediately.
- WP2 and WP8 start once WP1's reader API lands (stubbed in WP0, so they can start against the stub).

**Critical path:** WP0 → WP3 → WP-QINT → WP7 (real-data acceptance) → WP10 → WP14 → WP15.

---

## WP0: scaffolding and frozen contracts (single engineer, first)

**Files:**
- `tools/scopt/` with `pyproject.toml` (deps: capstone==5.0.3, pyelftools, jsonschema, click; installed with `pip install -e` into a Python 3.12 virtual environment).
- Core modules: `scopt/canon.py` (canonical JSON, sha256 ids, `fnv1a64`, `record_checksum_fold`), `scopt/ids.py`, `scopt/formats/enums.py` (site kinds, encodings, opmark kinds, TB flags, chunk types, qprof edge kinds: exactly §2.4 and §3.5).
- JSON schemas `scopt/formats/schemas/{cache_region.v1,profile.v1,execution_bundle.v1,artifact_sidecar.v1}.json`.
- `scopt/model/unit.py` dataclasses (§5.2) and `scopt/passes/base.py` types (§5.4), as interfaces only.
- `tests/synth/builders.py`, with signatures only (`build_sc(units, header=...)`, `build_scm(...)`, `build_qprof(...)`).
- patch `qemu-sc-base` (QEMU, ≈80 lines; now QEMU commit `2dfce24b2a`):
  - `include/tcg/supercache.h` and `tcg/supercache.c`: accessors `tcg_supercache_buffer()` and `tcg_supercache_loaded_max_end()`.
  - `tcg/tcg.c`: `tcg_target_tb_ret_addr()`.
  - Empty stub files `include/tcg/sc-capture.h`, `tcg/sc-capture.c`, `accel/tcg/sc-profile.h` and `accel/tcg/sc-profile.c`, registered in their `meson.build`. They provide `scc_configure_from_env()` and `scp_configure_from_env()` as no-ops plus `extern bool scc_active, scc_in_tb, scp_active` (all false).
  - The two configure calls at the end of `tcg_supercache_configure` (supercache.c:316-334). Mutual exclusion is checked in `scp_configure_from_env`.
  - This gives WP3 and WP5 disjoint edit sets.

**Inputs:** design §2.4-2.5, §3.5, §4.1, §5.

**Outputs:**
- the importable package skeleton;
- golden test vectors for FNV and the id functions: `tests/unit/test_ids.py`, with tb_uid computed by hand for one record of minimal.sc;
- the base patch, which builds.

**Acceptance:**
- `pytest tests/unit/test_ids.py` passes.
- The QEMU worktree with base patch builds, and a B0 warm run with the base binary in a fresh run directory works. The first run is cold, because the exe changed and therefore the signature.

**Dependencies:** none.

---

## WP1: `.sc` v5 reader and writer, search-data codec, identity writer

**Files:**
- `scopt/formats/sc_v5.py`: `ScHeader`, `ScRecord`, `read_sc(path) -> ScFile`, `write_sc(path, header, units, *, sparse=True)`, `sparse_copy(src, dst)`, `sha256_sparse(path)`. The reader mirrors `load_records`: `count=min(...)`, committed flag, checksum, bounds. It reports skipped records with reasons.
- `scopt/decode/search.py`: `decode_search(buf, icount, pcrel=True) -> rows`, `encode_search(rows)`, and `unwind(rows, host_pc_off) -> (insn, words)` with GETPC_ADJ=2.
- `tests/unit/test_sc_v5.py` and `tests/unit/test_search.py`.

**Inputs:** design §0 G1, §2.7 steps 1-3, §4.6 publish rules, §5.8 V1/V9; `tools/scopt/independent/scparse.py`.

**Outputs:**
- the parsed model;
- `scopt sc dump` and `scopt sc identity-copy IN OUT` (read → write).

**Acceptance (T-B1-2, static half of V9):**
- On the maintainer's evaluation data (not in the repository): 163 valid records; every field agrees with `scparse.py` (run as a subprocess and compared); `write(read(x))` has a sha256 equal to x.
- `unwind` tests use hand-built rows.
- Corruption cases give `record_checksum`, `header_checksum`, `version` and `truncated_records`.

**Dependencies:** WP0.

---

## WP2: x86 decoder and meta-less reference classifier

**Files:**
- `scopt/decode/x86.py`: `sweep(code: bytes, addr: int, end: int) -> list[Insn]` and `extract_refs(insns, arena, maps) -> list[DecodedRef]`. Extracted references cover rel8 and rel32 branches, `[rip+disp]` operands, movabs, and imm32/disp values that fall in the arena or in a MAPS range.
- `scopt/decode/symbols.py`: MAPS parser, ELF symtab via pyelftools, `symbolize(addr) -> (module, symbol, off)`.
- `scopt/decode/thumb.py`: guest Thumb length check.
- `scopt/importer/metaless.py`: classifies minimal.sc with heuristics (pool start = lowest RIP memory operand; PIE base 0x555555554000 from `nm build/qemu-system-arm`), for bring-up and for regression comparison only.

**Inputs:** design §2.7 steps 4-6; review A2; `src/qemu/build/qemu-system-arm`; `src/fastdyn/build/libfastdyn.so`.

**Outputs:** `scopt inspect metaless minimal.sc`, which prints class tallies.

**Acceptance:**
- The tallies on minimal.sc reproduce G17 exactly: 394 / 3 / 3 / 316 / 3 / 316 (+0:105, +1:50, +3:161) / 336 / 52 / 0.
- The 11 helper names match symbols.
- 0 `(bad)` instructions.

**Dependencies:** WP0; WP1 reader.

---

## WP3: QEMU capture Tier 0 (`scc`)

**Files** (QEMU worktree on top of compat + base):
- `tcg/sc-capture.c` (≈700 lines) and `include/tcg/sc-capture.h`;
- hooks H2-H17 in `tcg/tcg.c` (H3, H4, H5, H13, H14, H15, H16a), `tcg/i386/tcg-target.c.inc` (H6-H12), `tcg/supercache.c` (H2, H17), `target/arm/tcg/translate.c` (H16);
- `scc_configure_from_env` body (H1).

**Output:**
- patch `qemu-sc-capture` (now QEMU commit `409e90ca08`);
- `scopt/tests/qemu/scm_smoke.py`, a standalone script that validates the FileHeader checksum, chunk trailers, seq order and END. It must not depend on WP4.

**Inputs:** design §2 in full; construction rules R1-R6 (§2.2).

**Review checklist:**
- R1-R6 hold.
- Every hook is gated on `scc_in_tb` or `scc_active`.
- No `malloc`, `g_*`, `tcg_malloc`, `FILE*` or `mmap` appears in `sc-capture.c`. Enforce with `nm` on the object: no references to `malloc`, `g_malloc` or `fopen`.
- The fd is moved to ≥1000.
- A tag still pending at H15 sets `capture_error`.

**Acceptance (in the worktree build, on minimal through a fresh run dir):**
- `scm_smoke.py` passes.
- The committed TB count equals the `.sc` `record_count`.
- Every TB chunk's 104 record bytes equal the `.sc` record at its slot.
- `code_fnv64` matches.
- **T-B1-1 locally:** three capture-off and two capture-on cold runs in one recreated run dir, all with the worktree binary. Whole-file sha256 equality, or the documented per-key mode.
- Different binaries are never compared byte for byte, because helper addresses shift whenever QEMU text changes. Byte identity is only ever checked between capture-on and capture-off runs of the **same** binary.

**Dependencies:** WP0 (base patch and enums).

---

## WP4: `.scm` v1 reader and writer, synthetic builders

**Files:**
- `scopt/formats/scm_v1.py`: `read_scm(path) -> ScmFile` (header, run_info, helpers, tbs by slot, maps, ramblocks, end, `incomplete`, `rejected_chunks`), `write_scm(path, ScmFile)`, and `merge_scm(files) -> dict[slot, TbChunk]` (byte-equal overlap or `snapshot_merge_conflict`);
- `scopt meta dump --jsonl`;
- `tests/synth/builders.py`, implemented for `.sc` and `.scm`, with corruption helpers: flip a byte, drop a site, truncate a chunk, swap a run.

**Inputs:** design §2.4-2.5.

**Outputs:** the reader used by WP7 and WP10, and the builders used by every Python WP.

**Acceptance:**
- Round trip `write(read(x)) == x` on synthetic files.
- Prefix semantics: a bad trailer drops the chunk and its suffix; a missing END gives `incomplete`.
- Merge-conflict case.
- Once WP3 produces a real `.scm`, it parses with 0 rejected chunks. Add it to `tests/data/` as a slice.

**Dependencies:** WP0.

---

## WP5: QEMU profile mode (`scp`)

**Files** (QEMU worktree on compat + base; disjoint from WP3):
- `accel/tcg/sc-profile.c` (≈450 lines) and `accel/tcg/sc-profile.h`;
- hooks P1-P6 in `accel/tcg/cpu-exec.c` (983-986, 455-456, 379-410, ≈827, 733, 517);
- `scp_configure_from_env` with the phase parser;
- P7 dump at atexit.

**Output:** patch `qemu-sc-profile` (now QEMU commit `a0681a6ac3`) and `scopt/tests/qemu/qprof_smoke.py` (header, rows, trailer).

**Inputs:** design §3.1-3.5.

**Acceptance (worktree build, minimal, warm copy of a cache produced by the same worktree binary):**
- The `.qprof` is valid, with `clean_exit=1` and no overflow.
- The flow-conservation identities of §3.4 hold.
- `total_dispatches` equals the number of `Trace` lines from a direct `-d exec,nochain` run of the same binary, within the T-B1-1a determinism mode.
- With `QEMU_SC_PROFILE` unset, the warm run appends 0 records and gives the same normalized output as before.
- With both `QEMU_SC_CAPTURE` and `QEMU_SC_PROFILE` set, QEMU exits with the mutual-exclusion error.
- A debug build passes the `tcg_tb_lookup(GETPC()) == cur` assert.

**Dependencies:** WP0.

---

## WP6: `.qprof` ingest, Profile v1, ProfileView

**Files:**
- `scopt/formats/qprof_v1.py`;
- `scopt/formats/profile_v1.py` with `ingest(qprof, staged_sc, snapshot) -> Profile`, `ProfileView.open(profile, snapshot_or_artifact)`, `merge(profiles)`, `check_flow(profile)`, and the status rules of §3.7;
- `scopt/analysis/profile.py` (`profile.counts.v1`).

**Inputs:** design §3.5-3.8; the WP4 builders for snapshots; a synthetic `.qprof` (builder added here).

**Outputs:** `profile.json` writer and reader, with a canonical `profile_id`.

**Acceptance (synthetic):**
- exact, lower_bound and every unknown reason;
- TRANSIENT rows do not affect completeness;
- PROFILE_ONLY rows are never merged;
- each compat reject case (`bundle`, `contract`, `runtime`, `coverage`);
- merge rules;
- broken flow conservation is detected.

Once WP5 output exists, a real minimal profile ingests with `complete=true`.

**Dependencies:** WP0 (WP4 builders for the snapshot side; stub until WP4 lands).

---

## WP7: importer, cross-check, recovery oracle, snapshot store, graph

**Files:**
- `scopt/importer/{join,crosscheck,classify,recovery,import_cache}.py`;
- `scopt/model/snapshot.py` (`CacheSnapshot`, store layout §4.3);
- `scopt/graph.py` (§4.4);
- `scopt/analysis/{refs,layout,cfg}.py`;
- `scopt import` and `scopt graph export|import` CLI.

**Inputs:** WP1 reader, WP2 decoder and symbolizer, WP4 reader; design §2.7, §4.3-4.5.

**Outputs:** `store/snapshots/<id>/{cache.sc, *.scm, manifest, snapshot.json, units.jsonl, code/, evidence.jsonl}` and `graph.json`.

**Acceptance:**
- **Synthetic:** each T-B1-9 import-side reason code. The "site removed from capture" case gives `unexplained_reference` and the unit is pinned; the "site removed from bytes" case gives `missing_reference`. Also `code_fnv_mismatch` gives `no_meta`, and a shifted search row gives `recovery_mismatch`.
- Re-import is byte-deterministic.
- **Real** (needs WP3 or WP-QINT output): T-B1-3 (G17 tallies, 100% bound), T-B1-4 (graph round trip, two-TB goto_tb pair, no `dst` on static edges) and T-B1-8.

**Dependencies:** WP1, WP2, WP4 (real acceptance also needs WP3).

---

## WP8: run harness, deploy oracle, identities, post-run proof

**Files:**
- `scopt/run/toml_rewrite.py`: line-level rewrite of `supercache`, `memory_file` and `qmp_socket`, verified with `tomllib`.
- `scopt/run/fdshim/__main__.py`: runs in `fastdyn-env`; stubs `qemu_target.start_execution`, records `qemu_argv.json`, then calls `fastdyn.main.cli`.
- `scopt/run/deploy.py`: oracle, sparse copy, `--check` double run, and the seed-run fallback.
- `scopt/run/launcher.py`: `setarch x86_64 -R`, cwd `src/fastdyn`, `-q`, env scrub of `QEMU_SC_*`, RAM reset, timeouts, normalized outputs.
- `scopt/run/proof.py`: §4.8 proof.
- `scopt/run/diag.py`: direct QEMU from the recorded argv with its own cache copy, plus `-d exec[,nochain]`.
- `scopt/formats/bundle_v1.py`: identity bundle, reader and RuntimeIdentity, including meson build options.
- `scopt qualify-capture`, which orchestrates T-B1-1 once a capture-capable binary is configured via `qemu_path` in the base TOML.

**Inputs:** design §4.2, §4.8, §5.11; FastDyn 0e13b739 (`qemu_target.py:133-216, 1000-1012`; `fastdyn.py:36-79`).

**Outputs:** `run.json` per run (argv, env diff, timings, BENCH, proof verdict, appended count).

**Acceptance** (works with today's B0 binary; no QEMU patch needed):
1. Deploy a sparse copy of the maintainer's evaluation data (not in the repository) into a fresh run dir created from `configs/b0-sc.toml` through the oracle, then run warm. The output equals the maintainer's evaluation data (not in the repository), appended = 0, and the proof passes. The `.sc` is not deleted, i.e. the manifest fingerprint matched.
2. `deploy --check` shows equal fingerprints across two scratch dirs.
3. A deliberately wrong manifest gives the frontend deleting the `.sc`, and the proof reports `INVALID_artifact_not_used`.
4. Copying a v6-stamped header gives `INVALID_artifact_not_used`.
5. The identity bundle is stable across two per-run TOMLs, and `bundle_mismatch` is raised after editing a modifiers file in a copy of the config.

**Dependencies:** WP0, WP1.

---

## WP9: pass framework (AnalysisStore, PassManager, ProposalBuilder, ledger)

**Files:**
- `scopt/analysis/{store,registry}.py` (sqlite cache optional behind a flag; the in-memory path comes first);
- `scopt/model/snapshot.py::WorkingSet` (copy-on-write; layout facet only in B2);
- `scopt/model/view.py`, `scopt/proposal.py`, `scopt/manager.py`, `scopt/passes/{base,identity,move_units}.py`, `scopt/ledger.py`;
- `pipelines/identity.toml` and `pipelines/b2-layout.toml`.

**Inputs:** design §5.2-5.6 and §5.9.

**Outputs:** `PassManager.execute() -> Candidate` (an edit log plus the WorkingSet). Linking and packaging are left to WP10.

**Acceptance (pure Python, fake snapshot from the WP4 builders):**
- The identity pass gives NO_CHANGE and an empty edit log.
- An edit outside capabilities raises `EditCapabilityError` and the outcome is FAILED.
- An exception gives FAILED with a traceback hash.
- A budget overrun gives BUDGET_EXHAUSTED.
- A missing profile gives SKIPPED_UNKNOWN.
- B3 edit kinds give `capability_not_enabled`.
- A stale `reads` entry gives `stale_edit`.
- Invalidation drops exactly the transitive dependents.
- Ledger rows are emitted for every pass.

**Dependencies:** WP0 (WP4 builders for fixtures).

---

## WP10: NativeLinker, ValidateAndPackage, artifact publish

**Files:**
- `scopt/link.py`: placer, fixups, slot order preserved, rollback, derived `.scm` via the WP4 writer.
- `scopt/validate.py`: V1-V10. V1 calls `tools/scopt/independent/scparse.py` as the second parser. V10 delegates to WP8.
- `scopt/formats/artifact.py`: sidecar, MANIFEST, atomic publish, rejections.
- `scopt optimize|validate` CLI.

**Inputs:** WP7 snapshot model, WP9 Candidate, WP2 decoder, WP1 writer, WP4 writer; design §4.6-4.7, §5.7-5.8.

**Outputs:** `store/artifacts/<id>/` or `store/rejections/<id>.json`.

**Acceptance:**
- **Synthetic:**
  - moving a unit by ±64·k gives V1-V8 pass;
  - omitting one prologue fixup makes V2 or V3 fail;
  - a rel32 at exactly ±2^31 is accepted or rejected precisely;
  - F-B2-5's forced `0x60000000` placement gives `rel32_range`, then rollback or rejection;
  - duplicate-key LIFO order is preserved.
- **Real (after WP7):**
  - T-B1-5 static half: the identity artifact passes V9 sha256 equality, and the derived `.scm` re-imports to the same SnapshotId;
  - F-B2-1 and F-B2-2 static (V1-V9).

**Dependencies:** WP1, WP2, WP4, WP7, WP9.

---

## WP11: B2 analyses and passes

**Files:**
- `scopt/analysis/{hotness,locality}.py`;
- `scopt/passes/{hotcold,cluster,order_cold}.py`;
- `pipelines/b2-layout.toml`.

**Inputs:** WP6 ProfileView and `profile.counts`, WP9 API, WP7 `refs` and `layout`; design §5.10.

**Outputs:** LAYOUT_ORDER and ANNOTATE edits; locality metrics, labelled as models.

**Acceptance (synthetic snapshot plus profile):**
- Output is deterministic: same inputs give byte-identical edit logs.
- `unknown` units are never classed cold.
- The hot chain order matches a hand-computed Pettis-Hansen result on a 6-unit graph.
- `--disable cluster` produces a different but valid order.
- Pinned units never appear in an order request.

**Dependencies:** WP6, WP9 (WP7 for the real-data run).

---

## WP12: fixtures (fault firmware, FreeRTOS MPS2 qualification)

**Files:**
- `fixtures/fault/{fault.c, startup.s, link.ld, Makefile, fault-*.toml}`. Build with Arm GNU Toolchain 13.3.Rel1 for cortex-m3. There are four variants (a) to (d), as in F-B2-3. The handler stores the stacked PC, xPSR and CFSR/BFAR to r0..r3 and calls `printreg` virtuals, then `benchmark_end`.
- `fixtures/freertos/` holds the build recipe, driving `src/fastdyn/tests/integration/run_freertos_introspection_smoke.sh` into a fixture directory without modifying `src/fastdyn`, plus TOMLs.
- `fixtures/inputs/{train,heldout,final}/` holds the input splits, or documents fixed-input variants if a workload has none.

**Inputs:** spec §8; design §7.

**Outputs:** ELF files plus TOMLs, and `fixtures/EXPECTED.md` with the expected printed values and ELF addresses of the faulting instructions.

**Acceptance:**
- Using the B0 binary and the WP8 launcher (or plain `setarch -R fastdyn run` before WP8 exists), each fault variant prints the expected stacked PC (equal to the ELF address) and CFSR under nocache, cold and warm.
- FreeRTOS MPS2 runs to a defined completion criterion under nocache, cold and warm with equal normalized outputs (B0 qualification for this workload).

**Dependencies:** WP0 for the directory conventions only.

---

## WP13: arms, timing statistics and report

**Files:**
- `scopt/run/arms.py`: arms nocache, empty, warm, identity, layout, plus per-pass ablations; ≥30 interleaved paired repetitions; seeded order.
- `scopt/report/{collect,render}.py`: ledger stages as columns; paired median speedup with bootstrap CI; raw times; arena capacity, logical size, allocated blocks (`stat`), populated native bytes, hot-set model metrics; regressions; `not_run`/`unsupported` rows.
- `scopt report`.

**Inputs:** WP8 `run.json`, WP9 ledger (stub until then).

**Acceptance:**
- On synthetic `run.json` and ledger files, `scopt report` produces a report, including when every stage is missing (it never fails).
- On real B0 runs through WP8 (nocache, warm), it renders the BENCH comparison.

**Dependencies:** WP0 (WP8 and WP9 for the real inputs).

---

## WP-QINT: integrated QEMU binary (needs explicit maintainer approval: modifies `src/qemu`)

**Files:** apply `qemu-sc-base.patch`, `qemu-sc-capture.patch` and `qemu-sc-profile.patch` (in that order; they are conflict-free by construction) to `src/qemu` on top of the compat changes, then rebuild `src/qemu/build/qemu-system-arm`. Record its sha256 and build options.

**Inputs:** WP3 and WP5 patches.

**Acceptance:**
- **T-B1-0:** B0 re-qualification with capture and profile off, plus the disabled-hook cost measurement (≥30 interleaved warm runs vs the old binary with its own cache; the median is reported, >1% is flagged).
- **T-B1-1** run with `scopt qualify-capture`, producing `qualification.json`.
- The old the maintainer's evaluation data (not in the repository) stays untouched as historical evidence. New caches are created per run.

**Dependencies:** WP3, WP5 (WP8 for the harness).

---

## WP14: B1 gate integration

**Files:** `tests/acceptance/test_b1_*.py`, which drives the CLI end to end on minimal: qualify-capture → prepare → bundle → import → graph → profile → optimize identity → validate → deploy and run.

**Acceptance:**
- T-B1-0 … T-B1-9 all pass (design §6) on minimal.
- T-B1-0/1/3/5 pass on FreeRTOS MPS2 once WP12 has qualified it.
- The ledger and report show `inspected`, `compiled`, `accepted` and `validated_dynamic` for the identity artifact.

**Dependencies:** WP-QINT, WP6, WP7, WP8, WP10, WP12, WP13.

---

## WP15: B2 gate integration

**Files:** `tests/acceptance/test_b2_*.py` and `campaigns/b2-freertos.toml` (frozen artifacts, input splits, arms and repetitions).

**Acceptance:**
- F-B2-1 … F-B2-6 pass on minimal and on the fault fixture.
- F-B2-7 is measured with frozen artifacts and held-out and final inputs, and reported with regressions.
- Per-pass enabled/disabled ablations are included.
- No retuning after looking at final inputs. The candidate and profile sha256 values are frozen in the campaign file before timing.

**Dependencies:** WP11, WP14.

---

## Suggested staffing and order

| Day | Work |
|---|---|
| 1-2 | WP0 |
| 2-10, in parallel | Engineer A: WP3 then WP-QINT. Engineer B: WP5, then WP6. Engineer C: WP1, then WP2, then WP7. Engineer D: WP4, then WP9, then WP10. Engineer E: WP8, then WP13. Engineer F: WP12. |
| 10+ | WP11 once WP6 and WP9 are in; then WP14 and WP15. |

**Earliest evidence without any QEMU change** (from the feasibility review): WP1 identity copy with V9 sha256 on minimal.sc; WP2 G17 tallies; WP8 deploying the B0 cache through the oracle into a fresh run dir with a passing proof.
