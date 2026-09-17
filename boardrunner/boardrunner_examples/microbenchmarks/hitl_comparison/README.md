# HITL Microbenchmark: Avatar2 vs FastDyn Passthrough

Per-op cost comparison for the paper's Figure 8(c), HITL execution. Both stacks emulate
the *same* firmware and forward *every* MMIO access to a real STM32F429I-DISC1
via ST-Link/V2; the two paths differ only in the host software between QEMU
and the probe:

- **Avatar2**: `QEMU trap → POSIX msg-queue → Python → OpenOCDTarget → GDB
  → openocd → SWD → chip`
- **FastDyn passthrough**: `QEMU trap → C dispatch → hw_session → LibHW
  → libusb → SWD → chip`

Two directions are measured:
- **reads**  — N loads from `GPIOA_IDR`  (`0x40020010`, read-only, safe)
- **writes** — N stores to `GPIOA_BSRR` (`0x40020018`, write-only, atomic
  bit set/reset; writing `0` is a valid no-op transaction)

Both addresses live in STM32's vendor peripheral region (`0x40000000-
0x60000000`), which both stacks forward to the physical chip. On the
reference host below, FastDyn passthrough is ~**20×** faster than Avatar2
in both directions.

## Contents

```
hitl_comparison/
├── README.md                                    (this file)
├── patches/
│   ├── fastdyn_hw_session_no_halt_check.patch   for FastDyn plugin
│   └── avatar_qemu_gcc13_softfloat.patch        for avatar-qemu (GCC 13+)
├── firmware/
│   ├── firmware_src/                            microbench.c, startup, Makefile
│   │   ├── pre_built/                           committed: read/write × N=100/1k/100k
│   │   └── build/                               fresh `make` writes here (gitignored)
│   ├── idle_firmware/microbench_idle.axf        quiet-CPU firmware for flash
│   └── scripts/flash.sh
├── avatar2/scripts/bench_avatar2.py             Avatar2 harness
├── fastdyn_passthrough/
│   ├── scripts/bench_fastdyn.sh                 FastDyn harness
│   └── scripts/dev_config.json                  passthrough MMIO range → stlink
├── run_all.sh                                   orchestrator (both benches)
├── plot.py                                      renders per-direction bar chart
└── results/                                     CSV and PNGs land here; our N=1000 measurements are
                                                 _{avatar2,fastdyn}_per_iter_{reads,writes}.csv and combined.csv
```

## Hardware

- STM32F429I-DISC1 board (Cortex-M4, 2 MB flash, 192 KB SRAM)
- Onboard ST-Link/V2-1 (USB VID:PID `0483:374b`)
- USB cable from the board's ST-Link port to the host
- User account with write access to the ST-Link USB device (via a udev
  rule or `plugdev` group membership; if `openocd` in the smoke test
  below fails with permission errors, that is your issue)

## Host prerequisites (tested on Ubuntu 22.04 and 24.04)

```bash
sudo apt install -y \
    build-essential meson ninja-build git pkg-config libglib2.0-dev \
    libpixman-1-dev libusb-1.0-0-dev libaio-dev libssl-dev \
    gcc-arm-none-eabi arm-none-eabi-nm arm-none-eabi-objdump \
    openocd python3-venv python3-dev
```

Additionally, **Ubuntu 24.04 only**: apt ships `libaio.so.1t64` (64-bit
`time_t`) instead of the legacy `libaio.so.1` soname that older QEMU
builds link against. See "Libaio shim" (step 9) below.

## One-time build

The default paths in the harness scripts assume this layout:

```
$WORK/
├── FastDyn/          this repo (already cloned; contains hitl_comparison/)
├── avatar2/          sibling avatar2 clone
└── libhw/            sibling libhw clone
```

Replace `$WORK` with any directory (e.g. `~/hitl`) and stay consistent
below. All paths in the scripts can also be overridden via env vars (see
the header of each bench script for the full set), e.g. `AVATAR_QEMU=...`
if avatar-qemu lives somewhere other than the sibling layout.

### 1. Apply the FastDyn plugin patch

The stock FastDyn `hw_session_read()` calls `hw_board_halted()` on every
MMIO to serve pending IRQs. That is a *second* USB round-trip per access,
which roughly doubles the per-op cost for benchmarks that never halt. The
patch comments the halt query out on the read/write happy paths; the
retry/error branch and the poll thread are unchanged.

```bash
cd $WORK/FastDyn
git apply boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison/patches/fastdyn_hw_session_no_halt_check.patch
```

### 2. Build FastDyn with libhw + device models enabled

Needs the FastDyn-fork QEMU (`$WORK/qemu`, built as in the top-level README) and LibHW (step 3) as siblings of `$WORK/FastDyn`:

```bash
cd $WORK/FastDyn
make LIBHW=true DEV=true BOARD_RUNNER=true
# → produces $WORK/FastDyn/build/libfastdyn.so (the Makefile reconfigures from scratch;
#   qemu_path=../qemu and libhw_path=../libhw are its defaults)
```

### 3. Build LibHW

```bash
git clone https://github.com/PSecLab/libhw $WORK/libhw   # or place your existing checkout here
cd $WORK/libhw && make
# → produces $WORK/libhw/out/libhw.so and $WORK/libhw/out/hw_test
```

### 4. Clone Avatar2 and its avatar-qemu submodule

```bash
cd $WORK
git clone https://github.com/avatartwo/avatar2.git
cd avatar2
git submodule update --init targets/src/avatar-qemu
```

### 5. Apply the avatar-qemu GCC 13 patch (only on GCC ≥ 13)

Older QEMU's `fpu/softfloat.c` declares two forward-decl'd functions as
`int` while the definitions return `FloatRelation`. GCC 13 rejects the
mismatch. Two-line fix:

```bash
cd $WORK/avatar2/targets/src/avatar-qemu
git apply $WORK/FastDyn/boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison/patches/avatar_qemu_gcc13_softfloat.patch
```

If your GCC is older than 13, applying it is harmless. Verify GCC with
`gcc --version`.

### 6. Build avatar-qemu (arm-softmmu only)

On Ubuntu 24.04, add `--disable-bpf` — its libbpf removed the
`bpf_program__set_socket_filter` symbol older QEMU links against.

```bash
cd $WORK/avatar2/targets/src/avatar-qemu
mkdir -p build && cd build
../configure --target-list=arm-softmmu --disable-werror --disable-docs \
    $([ -f /lib/x86_64-linux-gnu/libaio.so.1t64 ] && echo --disable-bpf)
make -j$(nproc)
# → produces $WORK/avatar2/targets/src/avatar-qemu/build/qemu-system-arm
```

### 7. Python venv with Avatar2

```bash
cd $WORK/FastDyn/boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install "setuptools<81"   # avatar2 imports pkg_resources
.venv/bin/pip install pygdbmi matplotlib
.venv/bin/pip install -e $WORK/avatar2
# Sanity check (-P keeps the local avatar2/ scripts directory off sys.path,
# where it would shadow the installed package):
.venv/bin/python -P -c "from avatar2 import Avatar, OpenOCDTarget, QemuTarget; print('ok')"
```

### 8. Build the firmware variants (optional; pre-built binaries included)

```bash
cd $WORK/FastDyn/boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison/firmware/firmware_src
make all
# → build/microbench_hitl_read_N100.axf     (100 GPIOA_IDR loads)
#   build/microbench_hitl_read_N1000.axf    (1k)
#   build/microbench_hitl_read_N100000.axf  (100k; ~17 min at Avatar2 speed)
#   build/microbench_hitl_write_N100.axf    (100 GPIOA_BSRR stores)
#   build/microbench_hitl_write_N1000.axf   (1k)
#   build/microbench_hitl_write_N100000.axf (100k)
```

Symbol addresses (`bench_start`, `bench_done`, `Reset_Handler`) are
resolved by the harness scripts via `nm`, so rebuilding with any N does
not require touching the scripts. Direction is auto-detected from the
firmware filename (`*_read_*` / `*_write_*`).

### 9. Libaio shim (Ubuntu 24.04 only)

```bash
cd $WORK/FastDyn/boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison
mkdir -p libaio_shim
ln -sf /lib/x86_64-linux-gnu/libaio.so.1t64 libaio_shim/libaio.so.1
```

`bench_avatar2.py` auto-detects this directory (env var `LIBAIO_SHIM`
overrides). Harmless on distros where `libaio.so.1` is available natively.

## Verify hardware is reachable

Before running the benchmarks, confirm openocd can talk to the board:

```bash
lsusb | grep 0483:374b
# Bus 001 Device XXX: ID 0483:374b STMicroelectronics ST-LINK/V2.1

openocd -f /usr/share/openocd/scripts/board/stm32f429discovery.cfg \
        -c "init; targets; shutdown"
# ...
# Info : [stm32f4x.cpu] Cortex-M4 r0p1 processor detected
# ...
```

If that succeeds, the probe is wired up correctly.

## Flash the idle firmware (one-time)

FastDyn's passthrough model talks to the physical CPU register file via
SWD; a firmware writing to arbitrary SRAM in the background could perturb
timing. Flash the trivial `while(1)` firmware once:

```bash
cd $WORK/FastDyn/boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison
firmware/scripts/flash.sh
```

## Run the benchmarks

**All-in-one** (runs both stacks × both directions, writes `results/combined.csv`):

```bash
cd $WORK/FastDyn/boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison
source .venv/bin/activate
./run_all.sh                       # sanity: ITERS=1, ~35 s, one sample per tool and direction
N=1000 ITERS=100 ./run_all.sh      # the paper's 100 samples per cell, ~55 min (Avatar2 ~13 s and
                                   # FastDyn ~3 s per sample, x2 directions)
# Firmware sizes supported by the shipped Makefile: 100, 1000, 100000.
```

**Avatar2 alone** (single direction):

```bash
.venv/bin/python avatar2/scripts/bench_avatar2.py \
    --firmware firmware/firmware_src/pre_built/microbench_hitl_read_N1000.axf \
    --n 1000 --iters 1 \
    --out results/avatar2.csv
# Direction is inferred from the firmware filename; override with
# --direction {reads,writes} if needed.
```

**FastDyn alone** (single direction):

```bash
FIRMWARE=firmware/firmware_src/pre_built/microbench_hitl_write_N1000.axf \
    N=1000 OUT=results/fastdyn.csv \
    fastdyn_passthrough/scripts/bench_fastdyn.sh
# DIRECTION=reads|writes overrides the filename inference.
```

## Render the plots

```bash
.venv/bin/python plot.py --csv results/combined.csv --out-dir results/
# → results/reads.png   results/writes.png
```

## Expected numbers

Reference host: Ubuntu 24.04, Linux 6.8, Python 3.12, openocd 0.12.0,
libstlink 1.8.0, ST-Link/V2-1 firmware `V2J46M33`, USB 2.0 Full-Speed.
Numbers below are from `./run_all.sh` with default `N=100`.

| stack                                     | direction | per-op    | N=100 total |
|-------------------------------------------|-----------|----------:|------------:|
| **Avatar2** (firmware-in-QEMU + fwd)      | reads     | 10.08 ms  | 1.008 s     |
| **Avatar2** (firmware-in-QEMU + fwd)      | writes    | 10.08 ms  | 1.008 s     |
| **FastDyn passthrough** (halt-check off)  | reads     |  0.506 ms | 0.051 s     |
| **FastDyn passthrough** (halt-check off)  | writes    |  0.506 ms | 0.051 s     |
| **Speedup (both directions)**             |           | **~20×**  |             |

Per-op cost is stable to 3–4 significant figures across N=100 / 1000 /
100000 for both stacks — linear scaling.

Note on variance: absolute per-op numbers depend on your USB stack,
ST-Link firmware version, and kernel scheduler, and may shift ±30–50%
between hosts. The ~20× ratio is stable because both stacks ride the
same USB stack.

## Troubleshooting

**`ImportError: cannot import name 'Avatar' from 'avatar2' (unknown location)`**
A directory named `avatar2` in your current working directory (e.g.
Avatar2's `--output-dir`) is shadowing the installed package. Run the
harness from a different cwd, or `rm` the shadowing directory.

**`qemu-system-arm: error while loading shared libraries: libaio.so.1`**
You're on Ubuntu 24.04 without the shim. Follow step 9 above.

**`qemu-system-arm: This board cannot be used with Cortex-M CPUs`**
avatar-qemu's `configurable` machine only special-cases `cortex-m3` for
the NVIC wrapper. The harness already sets `cpu_model="cortex-m3"`
(M4-compiled firmware is M3-ABI-compatible with soft-float and no DSP),
so if you see this error you are pointing `--avatar-qemu` at the wrong
binary.

**`Could not load plugin ...: libhw.so: cannot open shared object file`**
Set `LIBHW_DIR=/path/to/libhw/out` before invoking `bench_fastdyn.sh`
(or export it) so the runtime linker can find `libhw.so`.

**`Could not find model: '//'` in `bench_fastdyn.sh` output**
`dev_config.json` has JSON-comment-style `//` keys. Only pure JSON is
accepted — remove them.

**`Openocd errored ... couldn't bind tcl to socket on port 6666`**
A previous openocd instance is still running. `pkill -f 'openocd -f '`.

**`RemoteMemoryProtocol.ERROR | Unable to create rx_queue`**
`bench_avatar2.py` pre-seeds the POSIX message queues to avoid this race;
if you still see it, another process holds those queue names. Clear them:
`python3 -c 'import posix_ipc; posix_ipc.unlink_message_queue("/qemu_rx_queue"); posix_ipc.unlink_message_queue("/qemu_tx_queue")'`.

**FastDyn number looks implausible (e.g. 0.2 µs/op)**
The target address is not being forwarded. Two common causes: (1) the
address falls in Cortex-M PPB (`0xE0000000-0xE00FFFFF`, includes DBGMCU
at `0xE0042000`), which QEMU's armv7m model handles internally and cannot
be routed to passthrough — use an address in `0x40000000-0x5FFFFFFF`
instead; (2) `dev_config.json`'s range does not cover the target address.
This artifact's firmware reads from `GPIOA_IDR = 0x40020010`, which is
inside the default passthrough range.

## Reproducibility notes

The measurement quality was verified two ways during development:

1. **Openocd `-d3` on the Avatar2 read run** showed exactly N
   `received packet: m40020010,4` events (one per firmware load),
   proving each read reaches the physical chip.
2. **A one-line `fprintf` probe in FastDyn's `passthrough_read`** showed
   exactly N `PT_READ addr=0x40020010` events with the correct PC.

Analogous evidence holds for writes (openocd `M<addr>,<len>:<val>`
packets and a matching `passthrough_write` trace).

Per-op cost is linear-scaling to 3–4 significant figures across N=100 /
1000 / 100000, in both directions, for both stacks.

## Pinned upstream commits (for exact reproduction)

- FastDyn: this repository, current `main` branch
- Avatar2 Python:  SHA `56a90cfe73` (v1.4.6+33)
- avatar-qemu:     SHA `d774496465`
