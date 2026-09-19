# Function bypass and host operation microbenchmarks (Figure 8a, 8b)

Per-operation cost of FastDyn's inline modifier (function bypass, Figure 8a) and virtual instruction (host operation, Figure 8b) versus HALucinator's breakpoint handlers, on a Cortex-M4 firmware that performs the operation N=1000 times per run. No hardware, no checkout and no build needed: both sides are published as self-contained Docker images.

| Image | Contents | Pin |
|---|---|---|
| `pseclab/fastdyn-slice:1.1.2` | patched QEMU + FastDyn plugin + firmware; also renders Figure 8 | `pseclab/fastdyn-slice@sha256:1a7527906fe1c410d00f6349beb6319ad25949016d1565ae6eeb5730c7df55c1` (0.75 GB) |
| `pseclab/halucinator-slice:1.0.1` | unmodified upstream HALucinator (`halucinator/halucinator` @ `5403321`) + the same firmware and the `PcUpdate`/`CounterVirt` handler | `pseclab/halucinator-slice@sha256:c9206ba5877e65d76fec89afcf2a75f3a299345fbc958c49ff6cd31cf8e8792e` (3.4 GB) |

## Run

```bash
docker pull pseclab/fastdyn-slice:1.1.2
docker pull pseclab/halucinator-slice:1.0.1
mkdir -p results
docker run --rm -v "$PWD/results:/results" pseclab/fastdyn-slice:1.1.2 both 1000          # FastDyn, ~1 min
docker run --rm -v "$PWD/results:/results" pseclab/halucinator-slice:1.0.1 pcupdate 10   # HALucinator, ~7 min
docker run --rm -v "$PWD/results:/results" pseclab/halucinator-slice:1.0.1 counter 10    # HALucinator, ~2 min
docker run --rm -v "$PWD/results:/results" pseclab/fastdyn-slice:1.1.2 plot              # -> results/fig8.pdf
```

Every iteration is a fresh QEMU process. `both 1000` prints, per bench, mean ± sd per 1000-op run and ns per operation; the HALucinator runs print one `iter k <seconds> (<ms/op>)` line each. Outputs in `results/`: `{modifier,virtual}_run.log` (one `elapsed_ns=` line per run), `{pcupdate,counter}_run.csv` (`iter,elapsed_s,per_op_ms`), `fig8.pdf`. `plot` uses whatever is in `results/` and the shipped measurements for the rest, and prints which; panel 8c (HITL) comes from the shipped hardware measurements unless you ran `../hitl_comparison/` (board required) into the same `results/`.

Expected per 1000-op run: FastDyn ~100 µs for both benches (paper: 102.11 µs and 100.89 µs; ±15% across hosts); HALucinator ~40 s (`pcupdate`) and ~10 s (`counter`) (paper: 40.35 s and 10.14 s). Use `100` instead of `10` for the paper's sample size (~70 min + ~17 min).

Other commands: `modifier N`, `virtual N` (one bench only); without `-v ... /results` the lines are only printed.

## Shipped measurements and figure

`results/{modifier,virtual}_benchmark.txt` (FastDyn, 100 runs each) and `baseline/results/{pc_update,counter_update}_benchmark.txt` (HALucinator, 100 runs each) are the data behind the paper's figure; `python3 scripts/plot_results.py -o fig8.pdf` renders it from them (needs matplotlib), `--fresh ./results` substitutes your own runs, `--<name> <file>` overrides one input.

## How it works

`firmware/microbench.c` calls `slice_probe()` N times between `bench_start()` and `bench_done()`. FastDyn (`modifier_benchmarking/`, `virtuals_benchmarking/`) attaches `benchmark_start`/`benchmark_end` virtual instructions to the two markers (they stamp `CLOCK_MONOTONIC` in the plugin) and either patches `slice_probe` with the modifier `r15 <- r14` (return immediately) or attaches the `bench_tick` virtual instruction to it. HALucinator (`baseline/`) intercepts the same three symbols with `baseline/halucinator_configs/sentinel_log.py`: `PcUpdate` returns `True` (PC=LR, the function is skipped), `CounterVirt` increments a counter and returns `False` (the function runs); the elapsed time is measured inside the handler, so container start-up is not part of it.

Rebuild the images from this checkout: `scripts/Dockerfile` (context = FastDyn repo root, ~15 min: `docker build -f $M/scripts/Dockerfile -t fastdyn-slice .` with `M=boardrunner/boardrunner_examples/microbenchmarks/firmware_slicing`) and `baseline/Dockerfile` (context = this directory: `docker build -f baseline/Dockerfile -t halucinator-slice .`). Both take `--build-arg N_INNER=<n>` to change the loop bound; firmware sources and a prebuilt binary are in `firmware/`.
