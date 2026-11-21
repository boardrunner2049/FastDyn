# Trebuchet

Trebuchet is the end-to-end pipeline that takes an unoptimized (`-O0`) firmware image
through every optimization stage this project has, and measures each stage:

```
 -O0 firmware ──► FirmBOLT ──► FastDyn ──► warm SuperCache ──► scopt
   (guest ELF)    guest-binary   emulator    persistent          offline optimizer
                  optimizer                  translation cache    for the cache
```

It answers one question: how much faster does the firmware run under FastDyn after
each stage, and where does that leave it against stock QEMU? One driver,
`trebuchet.sh`, builds the firmware, runs every stage and times five configurations:

| # | arm | emulator | guest image | SuperCache |
|---|---|---|---|---|
| 1 | `stock-O0` | stock QEMU | `-O0` | none |
| 2 | `fd-O0` | FastDyn | `-O0` | off |
| 3 | `fd-fb` | FastDyn | FirmBOLT-optimized | off |
| 4 | `fd-fb-warm` | FastDyn | FirmBOLT-optimized | ordinary warm cache |
| 5 | `fd-fb-scopt` | FastDyn | FirmBOLT-optimized | warm cache selected by scopt |

Two more arms give context and cost little: `stock-fb` (the FirmBOLT image on stock
QEMU) and `fd-O0-warm` (the `-O0` image with an ordinary warm cache).

## Result (2026-11-21)

FreeRTOS compute firmware at `-O0`, Cortex-M3. Medians over 24 blocks on the three
final inputs; every one of the 168 timed launches passed its result check. Raw samples:
`results/2026-11-21-composed/campaign.json`; candidate ranking:
`results/2026-11-21-composed/selection.json`.

| # | configuration | guest work | whole process |
|---|---|---:|---:|
| 1 | stock QEMU 8.2.2, `-O0` image | 1174 ms | 1208 ms |
| 2 | FastDyn, `-O0` image, cache off | 1442 ms | 1681 ms |
| 3 | FastDyn, FirmBOLT image, cache off | 1411 ms | 1656 ms |
| 4 | FastDyn, FirmBOLT image, warm SuperCache | 1361 ms | 1600 ms |
| 5 | FastDyn, FirmBOLT image, scopt-selected warm SuperCache | 1177 ms | 1417 ms |

Speedup of each step on guest work (median of within-block ratios, 95 % bootstrap
interval over blocks):

| step | speedup |
|---|---:|
| 1 → 2: stock QEMU to FastDyn | 0.815× [0.806, 0.822] |
| 2 → 3: FirmBOLT | 1.019× [1.012, 1.023] |
| 3 → 4: warm SuperCache | 1.040× [1.033, 1.046] |
| 4 → 5: scopt | 1.154× [1.149, 1.162] |
| 2 → 5: the whole pipeline on FastDyn | 1.224× [1.218, 1.229] |
| 1 → 5: against stock QEMU | 0.994× [0.990, 1.004] |

Reading it:

- The pipeline makes FastDyn 22 % faster on this firmware. FastDyn with nothing enabled
  starts 18 % behind stock QEMU here; after the pipeline, guest work is level with stock
  (the interval includes 1). Whole-process time is still 15 % behind stock.
- scopt is the largest step. The cache its selection deployed is `sfb+b3`: the
  icount-entry store-forwarding fix and return/static chaining in one cache. On the
  ranking input it was 1.179× faster than ordinary warm, against 1.080× for `sfb` alone
  and 1.052× for `b3` alone. `sfb` is what scopt classifies as a fix of baseline TCG
  code rather than an optimizer pass.
- FirmBOLT gains 2 % here, far less than on call-heavy firmware. The hottest function
  of this firmware contains a `switch`; GCC lowers an `-O0` switch on Cortex-M3 to
  `adr` + `ldr.w pc, [table, index, lsl #2]` with the address table inline, FirmBOLT does
  not recognise that as a jump table, and the function is left in place with nothing
  inlined into it.
- A warm cache gains 4 % inside the measured window: a 1.4 s steady-state window spends
  little time translating.

`python3 lib/report.py results/2026-11-21-composed/campaign.json` prints these tables.

The first campaign of the same day (`results/2026-11-21/`) had no combined candidate:
its selection deployed `sfb` alone, arm 5 was 1270 ms and the pipeline 1.131×. A
separate four-arm comparison of ordinary warm, `sfb`, `b3` and `sfb+b3` on the final
inputs is in `tools/scopt/docs/review-2026-11-21/`.

A run of `trebuchet.sh all 4` from an empty work directory (fresh FreeRTOS fetch,
12 blocks, before the combined candidate existed) rebuilt byte-identical loadable images
and reproduced the first campaign: 1172, 1440, 1410, 1362 and 1273 ms for arms 1 to 5.

## Requirements

- This checkout built as `tools/scopt/docs/getting-started.md` §1 to §4 describes: the
  pinned QEMU (`third_party/common/qemu/build/qemu-system-arm`), `build/libfastdyn.so`,
  `fastdyn-env/`, and scopt installed in a Python 3.12 environment whose `scopt` command
  is on `PATH`.
- [FirmBOLT](https://github.com/boardrunner2049/firmbolt) at `aa7db77` or later (the
  `-O0` image does not link with an earlier revision), with its `llvm-bolt` built.
- `arm-none-eabi-gcc` (Arm GNU Toolchain 13.3.Rel1 was used).
- A stock `qemu-system-arm` with the `mps2-an385` board (Ubuntu 24.04 packages 8.2.2).
- `pkg-config` with glib-2.0 development files, `git`, network access for the FreeRTOS
  fetch, a tmpfs at `/dev/shm`.

## Run

```sh
export PATH=/path/to/scopt-env/bin:$PATH        # scopt
export TREBUCHET_WORK=$HOME/trebuchet-work      # a new directory outside the checkout
export FIRMBOLT_REPO=/path/to/firmbolt
export FIRMBOLT_BOLT=/path/to/llvm-bolt         # the FirmBOLT-enabled build
export TREBUCHET_CPUS=8-11                      # optional: CPUs for the timed runs

tools/trebuchet/trebuchet.sh all                # every stage, then the report
```

`all` takes about 45 minutes on a 32-core host, 15 of them timed. Nothing else should
load the timing CPUs during `select` and `bench`. The stages can be run one at a time,
in this order:

| stage | what it does | output under `$TREBUCHET_WORK` |
|---|---|---|
| `firmware` | fetches the pinned FreeRTOS demo, copies the compute application over it, generates the nine inputs | `FreeRTOS/`, `inputs/` |
| `build` | builds the image at `-O0`, linked with `--emit-relocs` | `fw/O0/RTOSDemo.out` |
| `firmbolt` | profiles the image on the three training inputs inside FastDyn's machine, then runs FirmBOLT's emulator-performance mode | `fbprof/`, `fw/O0-fb/RTOSDemo.out` |
| `bind` | produces scopt's per-executable data for this QEMU build | `scopt-data/` |
| `prepare` | for both images: captures the cache, imports it, takes the exact profile, builds six candidate caches, records reference transcripts | `cfg/`, `scopt/O0/`, `scopt/O0-fb/` |
| `compose` | (part of `prepare`) builds the `sfb+b3` cache from existing `sfb` and profile data | `scopt/*/out/sfb-b3/` |
| `select` | ranks the candidates of the FirmBOLT image and confirms the winner (`scopt tune-warm`) | `scopt/O0-fb/tune.json` |
| `bench [REPS]` | the timed campaign, `REPS` blocks per final input (default 8) | `results/campaign.json` |
| `report [FILE]` | medians and paired speedups | standard output |

Other settings: `ARM_PREFIX` (default `arm-none-eabi-`), `STOCK_QEMU` (default
`/usr/bin/qemu-system-arm`), `TREBUCHET_QEMU` (default: the checkout's QEMU build),
`SCOPT_RAM_ROOT` (default `/dev/shm/trebuchet-ram`). scopt's own variables
(`FASTDYN_ROOT`, `SCOPT_ENVLAYOUT_PATH`, ...) are set by the driver and point into the
work directory.

To start over, make the work directory writable first (scopt stores snapshots
read-only): `chmod -R u+w "$TREBUCHET_WORK" && rm -rf "$TREBUCHET_WORK"`.

## How each stage works

### Firmware and inputs

`firmware/compute/` is a packet-processing application for the FreeRTOS MPS2 Cortex-M3
demo (FreeRTOS `48d4714c71da`): `main_compute.c` plus small edits to `main.c`,
`FreeRTOSConfig.h` and, in `gcc/` (the demo's `build/gcc`), `startup_gcc.c`, the
`Makefile` and the linker script. It reads its
input stream from RAM at `0x21000000` and marks its measured window with six small
marker functions (`vComputeWindowStart`, `vComputeReport1..4`, `vComputeDone`).

- The input address is RAM on the stock `mps2-an385` board (its 16 MiB PSRAM) and a
  declared memory bank under FastDyn, so the same ELF runs on both.
- The linker script puts all code in one `.text` output section and read-only data in
  its own section; FirmBOLT needs both, and `--emit-relocs`.
- `gen_inputs.py` generates nine inputs deterministically: three each for `train`,
  `heldout` and `final`, with different frame mixes. `train` feeds the two profiles,
  `heldout` the cache selection, `final` only the reported numbers.

### FirmBOLT

FirmBOLT's emulator-performance mode needs a block profile of the exact binary. The
profile is taken inside FastDyn's own machine: `lib/qemu-prof.sh` stands in as the QEMU
executable of a cache-less configuration and adds FirmBOLT's profiler plugin to
FastDyn's command line. The three training profiles are concatenated.

The marker functions are excluded from optimization (`--skip-funcs`), because FastDyn's
benchmark hooks are attached to their addresses. FirmBOLT moves the hot code to free
flash at `0x10000`; the FastDyn configuration of each image is generated from that
image's symbols (`lib/gen_toml.py`).

### SuperCache and scopt

`prepare` follows `tools/scopt/docs/workflow.md`, with two choices of its own:

- **The cache is captured over all nine inputs.** A cache captured on the training
  inputs alone is incomplete for the others: 400 to 700 blocks were translated again
  on a held-out input, so the run was not warm and `tune-warm` rejects it. The exact
  profile that the passes read is still taken on the training inputs only.
- **RAM is private and on tmpfs** (`share = false`, `SCOPT_RAM_ROOT`), as in the
  four-way benchmark (`tools/scopt/docs/selected-warm-benchmark`).

Six candidate caches are built per image: `b3` (with `specialize-returns`,
`chain-returns` and `chain-static` enabled), `exit-pc-fold`, `b2-arena-slide`, `b4`
(state-sync elimination), `sfb`, and `sfb+b3`. The last one carries both the
store-forwarding fix and chaining in one cache: the `sfb` artifact is re-imported as a
snapshot and `b3` runs on it, with the ordinary snapshot as its warm baseline
(`trebuchet.sh compose`). `select` hands them to `scopt tune-warm`, which
ranks them against the ordinary warm cache on `heldout-1` (12 pairs each) and confirms
the best one on `heldout-2` and `heldout-3`. Arm 5 deploys what that confirmation
selected; if it had failed, arm 5 would be the ordinary warm cache.

`dominant-return`, `activation-layout` and `irq-entry` change nothing on this firmware
and are not candidates. The native host-inline pipelines need the LLVM 20.1.2 bridge
(`tools/scopt/native`) and are not part of Trebuchet yet.

### Binding scopt to the QEMU build

`bind` runs `scopt envlayout produce` and `scopt helper-effects rebind`. On the QEMU
this branch pins, the rebind refuses: one reviewed source, `include/tcg/tcg.h`, differs
from the tree the shipped table was written for. The difference is two `TCGContext`
fields; no call flag and no helper declaration changed. `lib/rebind_helper_effects.py`
accepts exactly that file with exactly that content and refuses any other difference.

### Timing

`lib/bench.py` runs blocks. A block is one final input and one launch of every arm, in
random order; one warm-up block per input is not recorded.

- **FastDyn arms** go through scopt's run-directory API (`create_run_dir`, `deploy`,
  `launch`), the same path `tune-warm` uses. Guest work is FastDyn's `[BENCH]` window;
  the process time is the wall time of the FastDyn frontend process. A launch passes
  only if the guest's sixteen reported registers equal the reference and, with a cache,
  the run proof passes with no block appended.
- **A failed launch invalidates the campaign.** `bench` records the sample with its
  reasons, marks the file `"valid": false` and exits 1. `report` then names the failed
  launches and exits 2 without a speedup table (`--diagnostic` prints the numbers,
  labeled). A block missing an arm is treated the same way. Nothing is dropped,
  substituted or re-run selectively.
- **The stock arm** has no benchmark hooks. `lib/stock.py` starts stock QEMU stopped
  with a gdbstub, sets one breakpoint at `vComputeWindowStart`, removes it when it is
  hit, and then finds the end by polling the program counter until it is inside
  `vComputeDone`. Polls are sparse until 3 % before the expected end and 2 ms apart
  after that, about 25 per run, each stopping the vCPU for about 0.2 ms. The end is
  taken as the midpoint of the last two polls (±1 ms). A second launch without any
  breakpoint gives the process time.
- **Both arms are checked against the same expected values.** The stock runner reads
  the firmware's statistics structure from guest memory; `lib/result_schema.py` says
  which structure field each reported value is, and the check compares field by field
  (15 of the 16 reported values; the sixteenth mixes in a local that is not in the
  structure). The three timing-dependent counters are not compared. A test derives the
  same mapping from `main_compute.c`.

  A breakpoint must not stay inserted during the window: QEMU single-steps every block
  on the guest page of an inserted breakpoint, which made this firmware run eight times
  slower when the end marker was a breakpoint.
- All arms run with `-icount shift=0`. Both QEMUs execute the same ELF for arms 1 and 2.
- The two timers differ (FastDyn's own marker against a gdbstub stop), so step 1 → 2 is
  less exact than the steps between FastDyn arms.

`lib/report.py` prints the median per arm and, for each step, the median of the
within-block time ratios with a bootstrap interval over blocks. `lib/compare.py` runs the
same kind of campaign over any set of caches for one image (used for the four-arm
comparison of `sfb`, `b3` and `sfb+b3`). `python3 -m pytest tools/trebuchet/tests` checks
the result schema, the oracle and the invalid-campaign rule.

## Limits

- One firmware, one optimization level, one host. FirmBOLT gains much more on firmware
  whose hot path is many small calls (FirmBOLT `docs/emulator-performance.md`).
- The scopt arm has no native host-inline pass, and `sfb+b3` is the only composition
  it tries.
- The warm caches cover every timed input, so the campaign measures fully warm
  execution. It says nothing about the cost of first discovering new paths.
- Whole-process time for stock QEMU ends when the work is done; for FastDyn it also
  covers process exit.

## Another firmware

The driver is written for the compute firmware. For another image, the parts to replace
are the `firmware` and `build` stages, the marker symbols and input bank address in
`lib/gen_toml.py`, the window symbols in `lib/stock.py`, and the result check in
`lib/bench.py`. The image needs a defined end of work, an input that does not go
through the FastDyn configuration, `--emit-relocs`, one `.text` section, and free flash
for FirmBOLT's relocated code.
