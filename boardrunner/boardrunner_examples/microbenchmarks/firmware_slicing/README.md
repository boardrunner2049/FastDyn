# Firmware slicing microbenchmark (Figure 8a, 8b)

Per-operation cost of FastDyn's two instrumentation paths on a Cortex-M4 firmware that calls `slice_probe()` N=1000 times per run: an inline **modifier** that skips the probed function (Figure 8a, "function bypass") and a **virtual** callback that counts the call on the host (Figure 8b, "host operation"). HALucinator's equivalent handlers are the baseline (`baseline/`). No hardware involved; FastDyn runs in a self-contained container.

## Run (FastDyn)

```bash
# From the FastDyn repo root:
./boardrunner/boardrunner_examples/microbenchmarks/firmware_slicing/scripts/run_benchmarks.sh 1000
```

Builds the `fastdyn-slice` image on first run (patched QEMU + plugin + firmware, ~15 min), runs both benches 1000 times, writes `results/{modifier,virtual}_<timestamp>.log` and prints min/median/max/mean in ns per op. Expected: ~100 ns/op for both (paper: 102.11 µs and 100.89 µs per 1000-op run).

To rebuild the image with a different firmware loop bound:

```bash
docker build --build-arg N_INNER=1000000 \
    -f boardrunner/boardrunner_examples/microbenchmarks/firmware_slicing/scripts/Dockerfile \
    -t fastdyn-slice:latest .
./boardrunner/boardrunner_examples/microbenchmarks/firmware_slicing/scripts/run_benchmarks.sh 100 1000000
```

## Baseline (HALucinator)

`baseline/README.md` — same firmware inside the `halucinator` container, ~40 s (PcUpdate) and ~10 s (CounterVirt) per run.

## Results and figure

Our measurements: `results/modifier_benchmark.txt`, `results/virtual_benchmark.txt` (FastDyn, 100 runs each), `baseline/results/*.txt` (HALucinator). `scripts/plot_results.py` reads those plus the HITL results in `../hitl_comparison/results/` and renders Figure 8; every bar is computed from the files, pass `--<name> <file>` to point it at fresh logs:

```bash
python3 boardrunner/boardrunner_examples/microbenchmarks/firmware_slicing/scripts/plot_results.py -o results/plot.pdf
python3 boardrunner/boardrunner_examples/microbenchmarks/firmware_slicing/scripts/plot_results.py --help
```
