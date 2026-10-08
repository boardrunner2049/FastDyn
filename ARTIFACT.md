# BoardRunner — Artifact Evaluation road map (CCS '26)

Artifact for *BoardRunner: Automatic Firmware Rehosting Using High-Fidelity Compositional Device Models*. This file is the entry point for evaluators: what is where, and how to run each experiment of the paper. Every experiment is a published, self-contained Docker image run as `docker run --rm` with a host directory mounted at `/results`; nothing needs to be built, and this repository is only needed to read sources, configurations and the shipped measurements. Per-experiment READMEs next to each experiment give the details.

Archive (complete source snapshot, this file, the paper and the appendix): https://doi.org/10.6084/m9.figshare.33941068 · Images: https://hub.docker.com/u/pseclab · Maintained sources: https://github.com/PSecLab/FastDyn (may lag the archive), with https://github.com/boardrunner2049/libhw and the patched QEMU https://github.com/boardrunner2049/qemu (branch `fastdyn`), which the image builds fetch at pinned commits.

## Requirements

Docker Engine ≥ 20 on an x86-64 Linux host, ~25 GB free disk for all images, no hardware. Optional hardware experiments need an STM32F429I-DISC1 (Figure 8c), a NUCLEO-F103RB (Table 6 Passthrough column; a Bosch BME280 breakout additionally for its SPI and I2C firmwares), or a NUCLEO-H753ZI / STM32F769I-EVAL (Table 8 model baselines), reached through the board's on-board ST-Link over USB.

## Images

| Image | Experiment | Digest | Download |
|---|---|---|---|
| `pseclab/fastdyn-slice:1.1.2` | Figure 8a/8b, FastDyn side; renders Figure 8 | `sha256:1a7527906fe1c410d00f6349beb6319ad25949016d1565ae6eeb5730c7df55c1` | 0.75 GB |
| `pseclab/halucinator-slice:1.0.1` | Figure 8a/8b, HALucinator baseline | `sha256:c9206ba5877e65d76fec89afcf2a75f3a299345fbc958c49ff6cd31cf8e8792e` | 3.4 GB |
| `pseclab/hitl-slice:1.0.1` | Figure 8c (board required) | `sha256:d0cc1cecd2d3869844b47e660bac0bca0bc311b8e624091563bb662f26477566` | 1.8 GB |
| `pseclab/halucinator-macro:1.0.1` | Figure 9, HALucinator | `sha256:f9a6a33bbadfc3527443f89450f68533f1242c1fa9736eca79ba3978ad8b20b8` | 3.4 GB |
| `pseclab/halucinator-fd-macro:1.0.1` | Figure 9, HALucinator-FD | `sha256:a49c6323d59f2f007fb8ccd53832ec2f84727402b5a6f8c4a73ab784900518a4` | 2.1 GB |
| `pseclab/boardrunner:1.0.0` | Tables 5/6/7 (NUCLEO-F103RB) | `sha256:d2baa5868bf76285989c83fb5280a70692713e203b0e66787a55dde6236d86c9` | 1.4 GB |
| `pseclab/fastdyn-fuzzer:1.0.0` | Table 8 (real-world firmware, fuzzing) | `sha256:a52d885127c2e5191e5a21103ca94a976a264fdf2b4a03aaf54736157e37be2a` | 0.6 GB |

`docker pull pseclab/<name>:<tag>` (or `pseclab/<name>@<digest>` for the exact image). Every image stops cleanly on Ctrl-C.

## Basic test (1 minute)

```bash
docker pull pseclab/fastdyn-slice:1.1.2
docker run --rm pseclab/fastdyn-slice:1.1.2 modifier 1
```
Prints one `[BENCH][slice_modifier] elapsed_ns=...` line (~100 µs): patched QEMU, FastDyn plugin and firmware work on your host.

## Experiments

If a number is far from the expected one, re-run that command: the timings are wall-clock measurements inside a container that shares the host with everything else on it (busy host, Docker Desktop VM, CPU frequency scaling, first run after a pull), and the block counts depend on scripted inputs arriving while the firmware polls for them.

### E1 — Figure 8a/8b: function bypass and host operation (15 min) — `boardrunner/boardrunner_examples/microbenchmarks/firmware_slicing/README.md`

```bash
docker pull pseclab/fastdyn-slice:1.1.2
docker pull pseclab/halucinator-slice:1.0.1
mkdir -p results && cd results
docker run --rm -v "$PWD:/results" pseclab/fastdyn-slice:1.1.2 both 1000          # FastDyn, ~1 min
docker run --rm -v "$PWD:/results" pseclab/halucinator-slice:1.0.1 pcupdate 10   # HALucinator, ~7 min
docker run --rm -v "$PWD:/results" pseclab/halucinator-slice:1.0.1 counter 10    # HALucinator, ~2 min
docker run --rm -v "$PWD:/results" pseclab/fastdyn-slice:1.1.2 plot              # -> fig8.pdf
```
Expected per 1000-op run: FastDyn ≈100 µs for both benches (paper 102.11 / 100.89 µs, ±15% across hosts); HALucinator ≈40 s (`pcupdate`) and ≈10 s (`counter`) (paper 40.35 / 10.14 s). `100` instead of `10` reproduces the paper's sample size (~1.5 h).

### E2 — Figure 9: end-to-end macrobenchmarks (30 min) — `boardrunner/boardrunner_examples/macrobench/README.md`

```bash
docker pull pseclab/halucinator-fd-macro:1.0.1
docker pull pseclab/halucinator-macro:1.0.1
mkdir -p results && cd results
docker run --rm -v "$PWD:/results" pseclab/halucinator-fd-macro:1.0.1 all 10   # ~3 min
docker run --rm -v "$PWD:/results" pseclab/halucinator-macro:1.0.1 all 10      # ~25 min
docker run --rm -v "$PWD:/results" pseclab/halucinator-macro:1.0.1 summary
docker run --rm -v "$PWD:/results" pseclab/halucinator-macro:1.0.1 plot        # -> macrobench.pdf
```
Expected (`summary`): HALucinator ≈1.55 / 10.63 / 10.48 / 122.1 s and HALucinator-FD ≈1.70 / 2.18 / 1.54 / 3.18 s for STM32 UART / Zephyr K64F / Zephyr H103 / Zephyr filesystem, speedups ≈0.91× / 4.87× / 6.82× / 38.37×, "average speedup across 4 workloads: 12.74×" (±10% absolute, ±5% on the ratios).

### E3 — Table 6: BoardRunner vs naive baselines (10 min) — `boardrunner/boardrunner_examples/full_rehost_firmware/table6/README.md`

```bash
docker pull pseclab/boardrunner:1.0.0
mkdir -p results && cd results
docker run --rm -v "$PWD:/results" pseclab/boardrunner:1.0.0 table6 all     # ~6 min
docker run --rm -v "$PWD:/results" pseclab/boardrunner:1.0.0 verify uart    # trace verification; also gpio i2c spi adc
```
Expected: Null ≈101/352/304/262/268, Replay 218/415/438/291/370, BoardRunner 218/415/438/291/370 unique basic blocks for GPIO/UART/I2C/SPI/ADC+DMA; FC `fail` for Null and Replay (Replay cannot answer this run's button presses / typed characters), `pass` for BoardRunner; `verify` prints `matched` / `All peripherals passing`. The firmware binaries were rebuilt after the paper's measurements with different compilation flags, so absolute counts differ from Table 6 (paper: Null 101/352/304/262/265, others 191/415/438/291/378); the relations are exact: Replay and BoardRunner execute the same block set, Null a strict subset, only BoardRunner passes FC and TV.

### E4 — Tables 5/7: fully rehosted SDK workloads (3 min) — `boardrunner/boardrunner_examples/full_rehost_firmware/README.md`

```bash
docker run --rm -v "$PWD:/results" pseclab/boardrunner:1.0.0 example all    # ~2 min
```
Expected: one line per workload (GPIO with interrupt, UART, SPI, I2C, ADC+DMA, TIMER, PWM) with its block count and `FC pass` plus the operation observed. The 63 other workloads of Table 5 (four other boards) are hybrid HITL configurations with firmware, platform configuration and generated models that need their board: `boardrunner/boardrunner_examples/examples/<board>/`.

### E5 — Figure 8c: HITL execution (optional, STM32F429I-DISC1) — `boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison/README.md`

```bash
docker pull pseclab/hitl-slice:1.0.1
lsusb | grep -i st-link
mkdir -p results && cd results
docker run --rm -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' pseclab/hitl-slice:1.0.1 check
docker run --rm -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' pseclab/hitl-slice:1.0.1 flash
docker run --rm -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' -v "$PWD:/results" pseclab/hitl-slice:1.0.1 avatar2 10   # ~4 min
docker run --rm -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' -v "$PWD:/results" pseclab/hitl-slice:1.0.1 fastdyn 10   # ~1 min
docker run --rm -v "$PWD:/results" pseclab/hitl-slice:1.0.1 plot                                                                        # -> fig8.pdf
```
Expected per 1000 forwarded accesses: Avatar2 ≈10.08 s, FastDyn ≈0.506 s, both directions (≈20×; absolute values ±30–50% with the USB stack). The host user must be able to open the ST-Link USB device; `--device-cgroup-rule` is needed by rootful Docker and ignored by rootless Docker.

### E6 — Table 6 Passthrough column (optional, NUCLEO-F103RB) — `boardrunner/boardrunner_examples/full_rehost_firmware/README.md`, section "Hardware"

Only the board is needed for GPIO, UART and ADC+DMA; **SPI and I2C additionally need a BME280 breakout** wired as in `Nucleo-F103RB/{SPI,I2C}/README.md` (without it they run but fail their functional check). Flash the idle firmware once, then run any firmware in passthrough mode:

```bash
docker run --rm -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' pseclab/boardrunner:1.0.0 check   # Cortex-M3 detected
docker run --rm -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' pseclab/boardrunner:1.0.0 flash   # ** Programming Finished **
docker run --rm -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' --device=/dev/ttyACM0 \
    -v "$PWD:/results" pseclab/boardrunner:1.0.0 table6 uart passthrough
```
Expected: the same block count as the Replay/BoardRunner cell and `FC pass` observed on the real peripheral. `table6 gpio|i2c|spi|adc passthrough` likewise (`--device` only for uart; GPIO asks you to press the blue button). Re-capturing an example's hardware trace with the `fastdyn` CLI follows the same flow: each `Nucleo-F103RB/<example>/README.md`, section "Hardware trace / passthrough".

### E7 — Table 8: real-world firmware and fuzzing, software only (10 min) — `virtuals/fuzzer/reproducibility/EVALUATION.md`

```bash
docker pull pseclab/fastdyn-fuzzer:1.0.0
mkdir -p results
docker run --rm -v "$PWD/results:/results" pseclab/fastdyn-fuzzer:1.0.0 ./virtuals/fuzzer/run_qemu_coverage.sh all --work-dir /results/qemu
docker run --rm -v "$PWD/results:/results" pseclab/fastdyn-fuzzer:1.0.0 ./virtuals/fuzzer/run_aflnet_coverage.sh lwip /results/lwip-aflnet
docker run --rm -v "$PWD/results:/results" pseclab/fastdyn-fuzzer:1.0.0 ./virtuals/fuzzer/run_aflnet_coverage.sh mqtt /results/mqtt-aflnet
docker run --rm -v "$PWD/results:/results" pseclab/fastdyn-fuzzer:1.0.0 ./virtuals/fuzzer/run_aflnet_coverage.sh modbus /results/modbus-aflnet
docker run --rm -v "$PWD/results:/results" pseclab/fastdyn-fuzzer:1.0.0 ./virtuals/fuzzer/run_board_coverage.sh modbus --work-dir /results/modbus-elder
```
Each command needs a fresh result directory; a campaign's final SIGINT/timeout is its intended cutoff. Expected unique basic blocks (paper's Table 8 in parentheses): no peripheral MQTT 196 (29), lwIP 124 (170), SD-card 103 (141), Modbus 123 (157) in `results/qemu/summary.tsv`; Modbus on its model 521 (518); fuzzing lwIP ≈2,587 (2,651), MQTT ≈902 (892), Modbus ≈611 (623), each in its directory's `bbl.txt`. Fuzzing exceeds the model baseline on all three; the lwIP campaign exits non-zero below 2,500. Firmware images were rebuilt after the paper's measurements with different build options, which shifts counts by a few blocks (the rebuilt MQTT image no longer exits early in the no-peripheral run); AFLNet counts vary with timing by a few percent.

### E8 — Table 8 model baselines on hardware (optional: NUCLEO-H753ZI for MQTT, STM32F769I-EVAL for lwIP and SD-card) — `virtuals/fuzzer/reproducibility/EVALUATION.md`, section "Run the hardware-assisted baselines"

Flash the board's idle firmware (`idlefirmware_h7.axf` / `idleloop_firmware_f4_f7.axf`, commands in the runbook), set `PORT` to its ST-Link VCP, then e.g. for MQTT:

```bash
PORT=/dev/ttyACM0
docker run --rm --device="$PORT" -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' \
    -v /dev/serial/by-id:/dev/serial/by-id:ro -v "$PWD/results:/results" pseclab/fastdyn-fuzzer:1.0.0 \
    ./virtuals/fuzzer/run_board_coverage.sh mqtt --board-port "$PORT" --work-dir /results/mqtt-board
```
`lwip` and `sdcard` take the same form; lwIP additionally needs a host `tap0` and `--network=host --cap-add=NET_ADMIN --device=/dev/net/tun` (runbook). Expected: MQTT 869 (paper 866), lwIP 1,814 (1,838), SD-card 1,393 (1,416).

## Layout

```
ARTIFACT.md                                   this road map
README.md                                     FastDyn developer README (building from source, other features)
boardrunner/boardrunner_examples/
  microbenchmarks/firmware_slicing/           Figure 8a/8b: images' Dockerfiles, firmware, configs, shipped measurements, plot_results.py
  microbenchmarks/hitl_comparison/            Figure 8c: Dockerfile, firmware, harnesses, shipped measurements
  macrobench/                                 Figure 9: Dockerfile, in-container runner, firmware specs, results/paper/, figure
  full_rehost_firmware/                       Tables 5/6/7 on NUCLEO-F103RB: Dockerfile, 7 rehosted workloads
    Nucleo-F103RB/<example>/                  firmware, hardware trace, generated models, platform config, README
    table6/                                   per-mode configs, replay tapes, block lists
  examples/<board>/                           the 70 SDK workloads of Table 5 (hybrid HITL configurations)
boardrunner/boardrunner_sdk/                  VIO library and generated-model build
src/fastdyn/                                  fastdyn / boardrunner CLI (run, generate, llm, verifier, ...)
core/, device_models/, virtuals/              FastDyn plugin: MMIO dispatch, device-model backends, virtual instructions, fuzzer
virtuals/fuzzer/reproducibility/              Table 8: runbook (EVALUATION.md), Dockerfile of the fuzzer image, models, seeds, configs
docs/                                         FastDyn documentation (BoardRunner_Flow.md: the rehosting workflow end to end)
```

## Reusing the artifact

See the appendix' "Notes on Reusability" and `docs/BoardRunner_Flow.md`. In short: `pseclab/boardrunner` contains the `fastdyn` CLI and the VIO SDK ready to use (`docker run --rm -it --entrypoint bash -v "$PWD:/results" pseclab/boardrunner:1.0.0`); rehosting a new firmware is capture a trace on the board (`fastdyn run` in passthrough mode), `fastdyn generate` + `fastdyn llm` (OpenAI API key) to synthesize a model, `fastdyn verifier` to check it, then `fastdyn run` with the model. Every image rebuilds from its Dockerfile with the FastDyn repository root as build context.
