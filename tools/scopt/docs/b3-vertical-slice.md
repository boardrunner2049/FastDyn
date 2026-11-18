# B3 first vertical slice (WP27)

Design: `docs/design/B3-B4-design.md` §12 "First vertical slice" and the WP27 implementation note at
its end. Plan: `docs/design/IMPLEMENTATION-PLAN-B3B4.md` `## WP27`. Driver: `tests/qemu/wp27_slice0.py`
(see `tests/qemu/README.md`). Evidence: the maintainer's evaluation data (not in the repository) (`verdict.json`, `wp27-slice0.json`).

The two-TB example is WP24 phase F-B3-1: R = `f_b3_1` (`adds r0,r0,#1; bx lr`, FastDyn
modifier `r0 <- 0` on the `bx lr`), C = the one-instruction TB `[cbz r0, Lz]` at `vc_b3_1`.

## Slice 0a (static, current binary)

Everything is the normal pipeline: `scopt return-edges summaries --certify` and
`scopt optimize --pipeline b3 --b3`. The only special input is `--v6-exe <qint sha256>`:
the output is a `.sc` v6 artifact, and V7 (`v6_capable_runtime`) refuses a v6 artifact
for a runtime that cannot load it. `qint` cannot, so without the override the full
pipeline is rejected by V7 alone (every other static row passes); with it the artifact
is published into an evidence store that is never deployed.

## Slice 0b (dynamic, current binary, experimental, never published)

`experiments/wp27_inplace.py`:

```python
from experiments.wp27_inplace import build_inplace_copy, patches_from_variant
desc = build_inplace_copy(snapshot, snapshot_dir, r_vaddr=0x10002, vc=0x2f4,
                          patches=patches_from_variant(variant_diff_row),
                          out_dir=".../copy", layout=envlayout)
```

* R gets one GENERAL guarded slot (canonical guard `guard-v1`), **registered** in R's v5
  record with the `exit_tb(R|n)` reset, so the stock main loop links it after its own
  lookup (as WP16's v5 probe). NativeLinker v2 builds it (`B3Plan(register_slots=True)`);
  the records and bodies are written with the v5 writer and the ext table is dropped.
* The slice-0a C' patches are written **in place into the general C**.
* Label `experimental_unsound_in_place_specialization` in `EXPERIMENTAL_UNSOUND.json`.
  The directory has no sidecar or manifest, so `scopt publish` and every store refuse it.
  It is sound for F-B3-1 only (C has one predecessor, the modifier makes r0 = 0 at
  every entry of C, no handler touches r0).

### The ordering twin

The plan's m-CE7 twin counts every `tlb_flush*`/`tcg_commit*` call after `benchmark_start`
and is clean only at 0. Every `fixtures/b3` phase writes `MPU_CTRL = 0` in `main`'s
teardown after the measured loop (WP26 note 10), one flush counted as 3 calls, so the
literal rule fails on every run of these fixtures, the warm reference included.
`window_twin_script` keeps the same counters but lets them stop and print the hit counts
of hardware breakpoints on C and R's stub at each flush. The run is safe for the v5
copy when every flush happened after the last execution of C and R's stub
(`flushes_before_last_probe_hit == 0`).

## Slice 1 (gate binary, integration stage)

Driver: `tests/qemu/wp27_slice1.py` (see `tests/qemu/README.md`). Evidence:
the maintainer's evaluation data (not in the repository) (`wp27-slice1.json`, `verdict.json`). Every run uses the frozen gate
binary `qint-v6-gate-1` (exe sha256 `56d8bfb3…`); the driver refuses to start if the tag,
the `qint-v6` HEAD or the exe changed. Nothing is timed.

The slice-1 artifact is the normal pipeline with the chain passes off (the `b3-spec` arm):

```
scopt optimize <F-B3-1 base> --pipeline b3 --b3 --disable chain-returns --disable chain-static \
    --runtime-identity <gate RuntimeIdentity v2> --profile … --prep … --elf … --store S
scopt publish S/artifacts/<id> --store P
scopt validate P/artifacts/<id> --dynamic --oracles O1,O2a,O2b,O3,O5 -c <b3-1 toml> \
    --baseline <base snapshot> --inputs fixtures/b3/inputs/1.bin --fixture-input 1 \
    --fixture-expected fixtures/b3/expected.py --qemu <gate exe> \
    --o3 '{"stats": {"links_armed": "==1"}, "hits": {"variant 0 C'"'"'": ">=995"}}'
```

It is a `.sc` v6 file with one variant (C' of `vc_b3_1`, the §3.5 CP/BF/DHO bytes), one
SPEC slot on `f_b3_1` and one link. On the gate binary the link arms once; the stub runs
1000 times, its fallback F once (the first iteration: a link arms only after both R and C
are activated, §5.5) and C' 999 times.

**O4.** No pass proposes a wrong fact, so the negative controls are built by hand with
`scopt.link.B3Plan` and packaged by `scopt.run.negative.package_negative` (never
publishable): `falsified_fact_link` (the variant cites `r0 == 1` and is folded for it: the
`jne` becomes `jmp`, so `cbz r0` is never taken) and `fold_flip` (the same bytes citing the
true certified fact). Both are rejected statically (V14 and V19 `stale_fact`; V12) and
both change O1 (z 1, nz 999 against z 1000, nz 0). The same hand-built plan with the
manager's patches and FactRef publishes with every static row and gives the manager's
variant bytes, so the mutants differ from a sound artifact only in the fold (and fact).

**X-REHY (coverage).** Under `coverage = true` FastDyn puts an inline LOG_REG ring store
(with a spin loop) into every TB. The B3 toolchain fails closed there: the proposer has no
fact for R (`exit_not_goto_ptr`), and a guard on R is rejected (V13 `guard_template`:
R's HostOps pinned by `unexplained_host_store`; hostsym inconclusive on the backward
branch). The X-REHY question is about the runtime, so the slice-1 variant is placed into
the coverage C as a test-only experiment (WP26 packager, the failing V12/V13/V14/V19
rows bypassed and recorded). Result: O1/O2a/O2b equal, links armed 1, C' 999 hits, and
FastDyn's LOG_REG ring at `benchmark_end` equal to warm's (same index, same buffer hash):
C' logs every execution through C's baked pointers, so no per-variant rehydration is
needed (§5.5's assumption holds).
