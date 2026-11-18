# Fixtures (WP12)

The fixtures live outside this repo, in `/data/extra_copy/fastdyn/fixtures/`. In the same way as the qualified `freertos-mps2` fixture, they are not under git. This repo carries the static consistency tests (`tests/unit/test_wp12_fixtures.py`) and the dynamic smoke test (`tests/qemu/wp12_fixtures_smoke.sh QEMU OUT`).

| fixture | for | status / doc |
|---|---|---|
| `fixtures/fault/` | F-B2-3 fault attribution. Variants: (a) unaligned LDR in a hot TB, (b) SDIV/0 helper route, (c) BusFault, (d) MMIO io-recompile | expected values and faulting-insn ELF addresses in `fixtures/EXPECTED.md` §1; qualified (5 images × icount/no-icount × nocache/cold/warm) |
| `fixtures/freertos-mps2/` | B0 real workload, blinky | qualified earlier, reused. 99.96% idle loop, so unsuitable for layout PGO |
| `fixtures/freertos-mps2-compute/` | F-B2-7 real workload: FreeRTOS packet-processing pipeline, 0% idle-loop TB share in the window | `fixtures/freertos-mps2-compute/QUALIFICATION.md` |
| `fixtures/inputs/{train,heldout,final}/freertos-mps2-compute/` | 3 distinct input streams per split | `fixtures/inputs/README.md` |
| `fixtures/b3/` (WP24) | B3/B4 fixtures of design §9.5: one image with phases F-B3-1…14, 17, 20…24 (+1p, 3b, 6t/6r/6h, 8p) and F-B4-1…7, 9, 10 selected by the input word; F-B3-16(a,b,d,e) stale configs/images; F-B3-24 `-O0`/`-Os` images; the F-B3-15 mps2 alias image with a direct-QEMU runner; gdb `-nx` diag scenarios (F-B3-12/13 incl. the PAUSED stop) and the O3 host-hit counter | `fixtures/b3/README.md`; qualified 90/90 + alias on `wt/qemu-int` (`runs/wp24-fix/qual-final`, current image after review 3) |

## Contracts the run harness (WP8) and the arms (WP13) must keep

1. **Input bank.** The compute TOML declares `[[Memory.input]]`: `id = "input"`, 1 MiB at 0x30000000, `memory_file = "input.bin"`.
   - Before each run, write the selected input file into that bank file, zero-padded to the bank size. Reset every *other* file-backed bank as usual.
   - Bank contents are not part of the FastDyn fingerprint (`qemu_target.py:886-944`), so one artifact and fingerprint serves all inputs.
   - This was proved by warm-running a train-1 cache with heldout-1, final-1 and train-2:
     - only K(B)\K(A) records were appended;
     - no invalidation occurred;
     - the manifest was byte-identical.
2. **Relative run paths.** All fixture templates use `ram0.ram`, `input.bin`, `qmp.sock` and `cache/<name>.sc`, and FastDyn runs with `cwd = run dir`. So the rendered TOML, and therefore the fingerprint, is identical in every run directory. A fresh run directory with a copied cache is a valid warm run, with no shim needed. §4.8's line-level rewrite of `memory_file`, `qmp_socket` and `supercache` still works. A WP8 run dir that keeps relative values gets the same property.
3. **Completion.** Every fixture ends at a `benchmark_end` virtual that is first reached after fixed work:
   - fault images: after the last variant;
   - compute: after all frames of all passes.

   Guest-visible results are `printreg` and `debug_log` lines. Normalize as `fixtures/lib/fdrun.py` does: mask the BENCH host time; in no-icount mode, also mask the virtual times.
4. **QEMU binary.** Everything was qualified on the B0 binary, and the smoke test also passed on `wt/qemu-wp0` (base patch). Once WP-QINT exists, re-run `fault/qualify.sh` and `freertos-mps2-compute/qualify.sh` with `FIXTURE_QEMU=wt/qemu-int/build/qemu-system-arm`.

## B3/B4 fixture contracts (WP24, `fixtures/b3/`)

1. **Phase and input.** A config is a template `b3-<name>.toml.in` plus `inputs/<name>[-suffix].bin` (word 0 = phase id, word 1 = argument) written into the 4 KiB input bank at 0x30000000 (`fdrun.py --input`). One cache per config serves all of its inputs (F-B3-11 train vs held-out). `b3conf.py` is the machine-readable table (phase, image, patches, virtuals, plugins, timer flag, extra artifacts).
2. **Output.** The guest log is printed after the phase as `Reg 0:<tag>`/`Reg 1:<value>` pairs, then `[BENCH][b3-<name>]`. Tags and the O2b record layouts (SysTick: CVR first, stacked PC/xPSR/r0-r3, iteration r4; faults: PC, xPSR, CFSR, BFAR, MMFAR, IPSR, iteration, r0-r3, r12, lr) are in `fixtures/b3/README.md`.
3. **Oracle.** `expected.py compare(text, input, mode)`: icount = the frozen exact `.norm` plus named invariants; no-icount = invariants, plus the icount `.norm` for phases without a timer. A dynamic test of a B3/B4 artifact compares its arm's `guest.norm` with this oracle and with the warm arm. The no-icount invariants are also exported as data, `expected/<input>.inv.json` (`expected.py export-inv`; equalities with their value, run-relative relations and predicates by name). `test_wp24_fixtures.py` keeps them in sync with `expected.py`.
   - **Timer phases in no-icount accept any number of SysTicks, including zero.** Whether a tick lands in a ~0.5 ms phase is host timing (ptimer 10 µs floor, main-loop kick latency), so `SysTick records > 0` and `hits > 0` are icount-only invariants; a no-icount gate (WP28/WP29) must not require ticks, and interrupt coverage of a B3/B4 arm is judged in icount.
   - Thread-side log writes (`b3_res`) run with PRIMASK set, so a SysTick record is never lost to the non-atomic `log_pair` (`end.systicks == SysTick records` holds in every mode).
4. **Patch points.** The B3 patch of F-B3-x is the modifier `r0 <- 0` on `f_<p>_ret` (asm) or on the single return instruction of the C callee (F-B3-24); continuations are `vc_<p>`; callees are at 0x10000+, continuations below 0x10000 (different 1 KiB pages).
5. **Diag.** `diag/gdb_guest.py` and `diag/o3_hits.py` re-run a finished run's printed QEMU command in a fresh dir (never writing into the source run), optionally with a warm cache copied in.
   - `gdb_guest.py` decides pass/fail itself. It checks the scenario's `# EXPECT key=v[,v..]` lines (B3DIAG values) and `# EXPECT-RES tag=value` lines (guest result pairs) together with QEMU exit 0, and writes `checks` and `pass` to `result.json`; the exit code is 0 only on pass.
   - In F-B3-13(d), after the PAUSED stop (in the spin loop, where r0 is dead) gdb breaks at Vc and writes `r0 := 7` at the first Vc stop (iteration 300), right before the `cbz r0` that the B3 fact specializes. The results must be z 999, nz 1.
   - F-B3-16(d)'s changed instruction is `adds r6,#1` (nz accumulator): the correct output is z 1000, nz 1000, and stale b3.elf code would print nz 0.
   - The diag tools rewrite paths under the run's original dir (`run.json` `run_dir`) and its current location, so moved run dirs work. A warm diag run needs a supercache run as `--from-run` (normally the same warm run as `--cache-from`).
   - `o3_hits.py` requires gdb's `exited normally` (QEMU exit 0) and reports, per target, the hit count from that breakpoint's own `info breakpoints` block: 0 when there is no "already hit" line, and `null` with exit 1 when the target has no row. It derives the flash RAMBlock offset from the reset TB record without assuming that record comes first; `--flash-offset` overrides it.
   - `qemu_rerun.py` used as a script reruns QEMU directly, bypassing FastDyn and its fingerprint. `--kernel`, `--cache-file` and `--qemu` swap the image, the deployed cache and the binary. This is the route to F-B3-16(d)'s activation-time guest-hash check (b3-1 warm cache + `b3_16d.elf`); a FastDyn deploy under b3-16d is a fingerprint deletion.
