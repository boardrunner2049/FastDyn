# F-B2-3 fault attribution (WP15B)

Design §7 F-B2-3: on the fault fixture (`fixtures/fault`, see `fixtures/EXPECTED.md` §1),
the printed fault reports must be identical across nocache, warm, identity and an
artifact whose **faulting units** are moved by ±64 MiB; the stacked PC must equal the ELF
address of the faulting instruction; in (d) the one-instruction CF_NOIRQ TB must appear
with the same key in both arms; static V6 must agree for the moved units.

## How to run it

```sh
PY=/data/extra_copy/fastdyn/tools-env/bin/python
cd <scopt worktree>
$PY tests/qemu/b2_fault.py --out runs/wp15b/fault --modes noicount,icount --reps 3
$PY tests/qemu/b2_fault.py --out runs/wp15b/fault --render        # OUT/tables.md
B2_FAULT_DIR=runs/wp15b/fault $PY -m pytest tests/acceptance/test_b2_fault_attribution.py
```

Per (mode, image) the driver writes `OUT/<mode>/<image>/` with `cmds.jsonl`, `logs/`,
`fault.json` (every check with its detail and evidence), and `OUT/b2-fault.json`. Images:
`a`, `b`, `c`, `d` (one variant each) and `fixture` (all four in one image). The gate
verdict covers mode `noicount`; `icount` is informational, because design §9 excludes
CF_USE_ICOUNT units from the F-B2 gates until they are qualified.

## Chain

| step | command / check |
|---|---|
| config | `fault-<image>.toml.in` rendered as `fixtures/lib/fdrun.py` does (integrated QEMU, `supercache = "cache/fault-<image>.sc"`, `supercache_flat = false`, icount line only in mode icount) |
| prepare, bundle, import | `scopt prepare`, `bundle identity`, `bundle runtime-identity`, `import --store` |
| faulting units | every unit whose guest range `[pc, pc+guest_size)` contains the ELF address (pyelftools symbol table) of `a_fault_ldr`, `b_fault_sdiv`, `c_fault_ldrb` or `d_mmio_ldr`; (c) and (d) also require the one-insn CF_NOIRQ unit at that address |
| `identity` | `optimize --pipeline identity` (V9 sha256 equality) |
| `plus64` | `tests/data/pipelines/test-move-units.toml`, `deltas` +64 MiB for the faulting units |
| `minus64-direct` | the same with −64 MiB from the original placement. Infeasible: every tb_offset is far below 64 MiB, so the target is negative (below `ROUND_UP(prologue_end, 64)`); `move-units` fails `edit_precondition` and the published candidate moves 0 units. Recorded, not run as an arm |
| `minus64c` | `plus128` (+128 MiB), its derived `.scm` re-imported into `store-chain/` (prep manifest, bundle, RuntimeIdentity), then −64 MiB from that snapshot: NativeLinker applies d = −64 MiB. Its `cache.sc` must be byte-identical to `plus64` |
| `minusmax` | the largest feasible negative move of the original placement: faulting units packed (slot order) from `ROUND_UP(prologue_end, 64)`; the units that occupied that range go to `ROUND_UP(max_end, 64)` onwards (`minusmax_moves`) |
| static checks | V1-V8 pass (V9 pass for identity, skip otherwise), `scopt validate A`, the validation.json V6 rows, and a per-moved-unit V6 recomputation on the artifact bytes: every recovery point unwinds to the original `(insn, words)` and the faulting insn is a recovery point |
| runs | `scopt run --arms nocache,warm,identity,plus64,minus64c,minusmax --reps N` (correctness only; BENCH times are not analysed). Printed `Reg` lines equal across all runs and equal `expected/<image>.norm`; stacked PC == ELF address; deploy arms proof PASS, appended 0 |
| diag | `scopt diag exec` (`-d exec,int`) on rep 0 of every deploy arm. Per `Taking exception` (except 8, exception return): the last `Trace` line before it (the TB entered from the dispatcher last; the faulting loops self-chain, so this is the faulting TB) must be a faulting unit, with the same key in every arm, at the unit's placement in that arm's cache. `cpu_io_recompile` rewinds equal across arms. (c)/(d): the CF_NOIRQ TB key with the same count in every arm, at its moved address; (d) 64 rewinds to `d_mmio_ldr` |
| negative control | single-variant fault images: a copy of the plus64 `cache.sc` whose faulting TB's search row for the faulting insn names `pc − 2`. Static V6 must flag it and the run (proof PASS, appended 0) must print a different stacked PC, which shows the dynamic check is not vacuous |
