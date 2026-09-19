# HALucinator baseline (Figure 8a/8b)

The `pseclab/halucinator-slice` image: unmodified upstream HALucinator (`pseclab/halucinator:5403321`) plus the slice firmware compiled at N=1000, the HALucinator configs (`halucinator_configs/*.yaml`) and the breakpoint handler `halucinator_configs/sentinel_log.py`:

- `PcUpdate` — function bypass: `return True, None` (HALucinator sets PC=LR, the function is skipped)
- `CounterVirt` — host operation: `counter++; return False, None` (the function runs)

The handler prints `SENTINEL_ELAPSED: <s>`, the time between the `bench_start` and `bench_done` breakpoints measured in-process; `run.sh` (the image entrypoint) collects it per iteration into `/results/<bench>_run.csv`. Our measurements: `results/pc_update_benchmark.txt`, `results/counter_update_benchmark.txt` (100 runs each). Run instructions: `../README.md`.
