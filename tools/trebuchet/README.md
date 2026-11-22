# Trebuchet

Use the [real application workflow](applications/README.md) for admission, candidate materialization,
joint image selection, reserved final evaluation and reporting. The historical FreeRTOS dataset
below is retained for implementation regressions and archival reproducibility. Its executable
stages require the explicit `fixture` subcommand.

## Historical FreeRTOS development campaign


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

## Dataset

Results are always reported as the five numbers for every category of this dataset.
All five are FreeRTOS applications for the MPS2 Cortex-M3 demo, built at `-O0`, with
three training, three held-out and three final inputs each.

| category | application | what dominates |
|---|---|---|
| `compute` | packet-processing pipeline (`main_compute.c`) | parsing, CRC, key-value store, a small VM; three tasks and queues |
| `fwbench` | protocol encoder and parser over a queue (`main_fwbench.c`, `fw_proto.c`) | many small cross-file calls, queue and mutex traffic |
| `kernbench` | kernel services (`main_kernbench.c`) | stream buffer, notifications, event group, semaphores |
| `libbench` | C library (`main_libbench.c`) | formatting, string and memory routines, sorting with a callback |
| `algobench` | compute kernels (`main_algobench.c`) | list walk, matrix product, state machine, CRC in one translation unit |

`compute` reads a generated frame stream from its input bank; the other four read a seed
and an iteration count (`firmware/workloads/gen_params.py`). The profile splits differ in
seed, and `final` runs longer than `train`.

## Result (2026-11-21)

Medians over 24 blocks per category on the final inputs; all 840 timed launches passed
their result checks. Raw samples and candidate rankings: `results/2026-11-21-dataset/`.

Guest work (ms, median)

| category | 1 stock QEMU | 2 FastDyn | 3 +FirmBOLT | 4 +warm cache | 5 +scopt cache | cache deployed in 5 |
|---|---:|---:|---:|---:|---:|---|
| `compute` | 1175 | 1435 | 1413 | 1372 | 1184 | `sfb+b3` |
| `fwbench` | 1100 | 1470 | 1120 | 1077 | 1038 | `b3` |
| `kernbench` | 1141 | 1758 | 1470 | 1426 | 1420 | `ordinary warm` |
| `libbench` | 1005 | 1076 | 747 | 723 | 585 | `sfb+b3` |
| `algobench` | 982 | 1064 | 1178 | 1159 | 894 | `sfb+b3` |

Speedup of each step, guest (median of within-block ratios)

| category | 2→3 | 3→4 | 4→5 | 2→5 | 1→5 |
|---|---:|---:|---:|---:|---:|
| `compute` | 1.013× | 1.030× | 1.159× | 1.208× | 0.989× |
| `fwbench` | 1.313× | 1.040× | 1.035× | 1.412× | 1.059× |
| `kernbench` | 1.194× | 1.032× | 1.007× | 1.231× | 0.801× |
| `libbench` | 1.437× | 1.031× | 1.237× | 1.848× | 1.716× |
| `algobench` | 0.900× | 1.016× | 1.298× | 1.188× | 1.102× |
| **geometric mean** | **1.155×** | **1.030×** | **1.142×** | **1.358×** | **1.097×** |

Reading it:

- **The pipeline makes FastDyn 1.36× faster on average** (geometric mean of 2 → 5), from
  1.19× on `algobench` to 1.85× on `libbench`.
- **Against stock QEMU the result is 1.10× on average**, but uneven: `libbench` is 1.72×
  faster than stock, `fwbench` and `algobench` 1.06× and 1.10×, `compute` level, and
  `kernbench` still 20 % slower. FastDyn with nothing enabled is slower than stock in
  every category (by 8 % to 35 %).
- **FirmBOLT helps call-heavy code and hurts one category.** It gains 1.31× on `fwbench`,
  1.19× on `kernbench` and 1.44× on `libbench`, little on `compute`, and makes
  `algobench` 10 % slower. In `algobench` (and `compute`) the hottest function contains
  an `-O0` `switch`, which FirmBOLT cannot relocate; it stays in place while its callees
  move to the new region, so calls that were inside one guest page now cross pages
  (cross-page transitions on a training input: 6.2 M before, 23.0 M after).
- **The scopt step depends on the category**: 1.30× on `algobench`, 1.24× on `libbench`,
  1.16× on `compute`, 1.04× on `fwbench`, none on `kernbench`. The selection deployed
  `sfb+b3` three times and `b3` once. On `kernbench` no candidate passed confirmation
  (the best, `b3`, won 9 of 12 pairs on one input), so arm 5 is the ordinary warm cache
  there; its 1.007× is two runs of the same cache and shows the noise level. The
  store-forwarding fix alone is slower than ordinary warm on `fwbench` (0.985×) and
  `kernbench` (0.971×).
- **A warm cache gains 2 % to 4 %** inside the measured window in every category.

Whole process (ms, median)

| category | 1 stock QEMU | 2 FastDyn | 3 +FirmBOLT | 4 +warm cache | 5 +scopt cache | cache deployed in 5 |
|---|---:|---:|---:|---:|---:|---|
| `compute` | 1210 | 1674 | 1652 | 1612 | 1426 | `sfb+b3` |
| `fwbench` | 1116 | 1694 | 1346 | 1303 | 1269 | `b3` |
| `kernbench` | 1172 | 1987 | 1700 | 1662 | 1655 | `ordinary warm` |
| `libbench` | 1042 | 1302 | 973 | 951 | 811 | `sfb+b3` |
| `algobench` | 1013 | 1291 | 1405 | 1385 | 1121 | `sfb+b3` |

Speedup of each step, process (median of within-block ratios)

| category | 2→3 | 3→4 | 4→5 | 2→5 | 1→5 |
|---|---:|---:|---:|---:|---:|
| `compute` | 1.012× | 1.025× | 1.132× | 1.170× | 0.846× |
| `fwbench` | 1.260× | 1.031× | 1.026× | 1.335× | 0.880× |
| `kernbench` | 1.169× | 1.025× | 1.004× | 1.195× | 0.704× |
| `libbench` | 1.336× | 1.024× | 1.174× | 1.611× | 1.288× |
| `algobench` | 0.917× | 1.013× | 1.236× | 1.148× | 0.903× |
| **geometric mean** | **1.128×** | **1.024×** | **1.111×** | **1.281×** | **0.906×** |

`trebuchet.sh fixture dataset_report` prints these tables from a work directory;
`python3 lib/dataset_report.py results/2026-11-21-dataset compute fwbench kernbench libbench algobench`
prints them from the stored samples.

Earlier single-firmware campaigns on `compute` are kept in `results/2026-11-21/` (before
the combined `sfb+b3` candidate existed: pipeline 1.131×) and
`results/2026-11-21-composed/` (with it: 1.224×).

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

tools/trebuchet/trebuchet.sh fixture dataset            # all five categories, then the tables
TREBUCHET_WORKLOAD=fwbench tools/trebuchet/trebuchet.sh fixture all    # one category
```

`dataset` takes about two and a half hours on a 32-core host: preparation of the five
categories runs side by side, then selection and the timed campaign run one category at
a time. `all` for one category takes about 45 minutes, 15 of them timed. Nothing else should
load the timing CPUs during `select` and `bench`. The stages can be run one at a time,
in this order:

`TREBUCHET_WORKLOAD` (default `compute`) names the category of a single-category stage.
`firmware` and `bind` are shared; everything else is under `$TREBUCHET_WORK/<category>/`.

| stage | what it does | output under `$TREBUCHET_WORK/<category>` |
|---|---|---|
| `firmware` | fetches the pinned FreeRTOS demo, copies the five applications over it, generates nine inputs per category | `../FreeRTOS/`, `../inputs/` |
| `build` | builds the category's image at `-O0` (`-DFW_WORKLOAD=N`), linked with `--emit-relocs` | `fw/O0/RTOSDemo.out` |
| `firmbolt` | profiles the image on the three training inputs inside FastDyn's machine, then runs FirmBOLT's emulator-performance mode | `fbprof/`, `fw/O0-fb/RTOSDemo.out` |
| `bind` | produces scopt's per-executable data for this QEMU build | `../scopt-data/` |
| `prepare` | for both images: captures the cache, imports it, takes the exact profile, builds six candidate caches, records reference transcripts | `cfg/`, `scopt/O0/`, `scopt/O0-fb/` |
| `compose` | (part of `prepare`) builds the `sfb+b3` cache from existing `sfb` and profile data | `scopt/*/out/sfb-b3/` |
| `select` | ranks the candidates of the FirmBOLT image and confirms the winner (`scopt tune-warm`) | `scopt/O0-fb/tune.json` |
| `bench [REPS]` | the timed campaign, `REPS` blocks per final input (default 8) | `results/campaign.json` |
| `report [FILE]` | medians and paired speedups of one category | standard output |
| `dataset [REPS]` | every stage for every category, then `dataset_report` | all of the above, `../dataset.json` |
| `dataset_report` | the five numbers per category, step speedups, geometric means | standard output |

Other settings: `ARM_PREFIX` (default `arm-none-eabi-`), `STOCK_QEMU` (default
`/usr/bin/qemu-system-arm`), `TREBUCHET_QEMU` (default: the checkout's QEMU build),
`SCOPT_RAM_ROOT` (default `/dev/shm/trebuchet-ram`). scopt's own variables
(`FASTDYN_ROOT`, `SCOPT_ENVLAYOUT_PATH`, ...) are set by the driver and point into the
work directory.

To start over, make the work directory writable first (scopt stores snapshots
read-only): `chmod -R u+w "$TREBUCHET_WORK" && rm -rf "$TREBUCHET_WORK"`.

## How each stage works

### Firmware and inputs

`firmware/compute/` is the packet-processing application for the FreeRTOS MPS2 Cortex-M3
demo (FreeRTOS `48d4714c71da`): `main_compute.c` plus small edits to `main.c`,
`FreeRTOSConfig.h` and, in `gcc/` (the demo's `build/gcc`), `startup_gcc.c`, the
`Makefile` and the linker script. It reads its
input stream from RAM at `0x21000000` and marks its measured window with six small
marker functions (`vComputeWindowStart`, `vComputeReport1..4`, `vComputeDone`).

- `firmware/workloads/` holds the other four applications. `fw_main.c` reads their seed
  and iteration count from the same input address, opens the measured window, and
  reports the checksum and iteration count through the same marker functions.
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

- One RTOS, one board, one optimization level, one host. The five categories are small
  synthetic applications, not field firmware.
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

### Campaign checkpoints

Each new campaign records a run ID, workload, planned input order, arms, and repetition
count in `results/campaign.json` before warm-up. Checkpoints are replaced atomically
after each complete block. Only a completed campaign with one checked launch per
planned block and arm, the correct input, distinct positions, and positive timing
values can produce a performance report. A terminated process can leave a running
checkpoint. A detected launch or preparation error leaves the campaign failed.
`report.py CAMPAIGN.json --diagnostic` can display available timed blocks after a
failure and exits with status 2. Archived version 1 campaigns use their published
eight-repetition, three-input, seven-arm plan.

The dataset driver assigns a dataset run ID before preparation, records it in each
campaign, and stops on preparation, selection, or benchmark failure. The dataset
report checks the workload and dataset identities before aggregation. An interrupted
or failed dataset has no suite geometric mean.
