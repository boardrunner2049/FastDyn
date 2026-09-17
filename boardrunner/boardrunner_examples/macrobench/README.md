# macrobench

End-to-end wall-time comparison of unmodified **HALucinator** (Avatar2
+ GDB backend) vs **FastDyn-Py** (FastDyn plugin with embedded Python
interpreter), on four firmware workloads drawn from HALucinator's
public test corpus.

Feeds Figure 9 in the paper.

**For artifact-eval reviewers reproducing the paper's numbers,
follow [REPRODUCE.md](REPRODUCE.md).** This file describes the
harness for developers extending it.

## Prereqs

- Docker (rootless or root-mode). On Ubuntu 24.04+, rootless requires
  `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`.
- Python 3.10+ on the host with `tomllib` (stdlib in 3.11+; install
  `tomli` for 3.10 or use `python3 -c 'import tomllib'` to check).
- `matplotlib` on the host (for figure regeneration).
- Two Docker containers, brought up once:

```bash
./setup_containers.sh
```

Creates `halucinator` (from `halucinator:latest`) and `fastdyn-py`
(from `pseclab/fastdyn-py:1.3.0`) as `sleep infinity` daemons.
Each benchmark run is a `docker exec` — no repeated container startup
cost.

## Run one workload once

```bash
python3 runner.py --fw stm32_uart_it --backend fastdyn-py
python3 runner.py --fw stm32_uart_it --backend halucinator
```

Prints wall time in seconds (or `TIMEOUT`). Add `--csv` to emit
`fw,backend,wall_s`. Add `--dump-emu-log <path>` to persist the
emulator's stdout+stderr for diagnostics (used to extract dispatch
counts, see below).

## Full sweep

```bash
./sweep.sh 10                    # 10 measured iterations per (fw, backend)
python3 summarize.py             # markdown table + geomean speedup
python3 make_macro_figure.py     # regenerates the paper figure
```

`sweep.sh` writes one CSV per firmware to `results/`, each column is
`iter,firmware,backend,wall_s`, with `backend` ∈ {`halucinator`,
`fastdyn-py`}. Each iteration launches a fresh `docker exec` and thus
a fresh QEMU process — no TCG / interpreter / plugin state carries
across iterations, so no warm-up discard is performed.

## Extending

`firmwares.toml` defines the workloads. Two shapes:

- `mode = "uart"` — firmware needs scripted input. Runner starts
  `peers/bench_uart_peer.py` inside the container alongside the
  emulator. Peer subscribes on the same ZMQ topics
  (`Peripheral.UARTPublisher.*`) that HALucinator's own
  `hal_dev_uart` uses, so it's a drop-in substitute for automated
  timing. Peer sends scripted prompts/responses, watches UART TX for
  the `success` marker, prints timing to stderr.

- `mode = "stdout"` — firmware terminates on a marker string appearing
  on the emulator's own stdout (e.g., `zephyr_fs`, which prints
  `mount /lfs 1` many times before finishing). Runner watches emu
  stdout directly.

To add a workload:

1. Add a `[name]` block to `firmwares.toml` with `emu_cmd`, `emu_cwd`,
   `mode`, `timeout`, `success` marker, and (for uart-mode) the
   scripted `steps`.
2. Run once untimed to confirm the pipeline works and find the actual
   terminal marker string.
3. Add to sweep.sh's default firmware list if you want it in the paper
   sweep.

## Files

```
runner.py                one (firmware, backend) run → wall time
sweep.sh                 loops runner over N iters × all firmwares
summarize.py             results/*.csv → markdown table + geomean
make_macro_figure.py     results/*.csv + stats/*.yaml → macrobench.pdf
firmwares.toml           per-firmware spec
peers/bench_uart_peer.py peer that replaces hal_dev_uart for timing
setup_containers.sh      one-time Docker bring-up
results/                 raw per-firmware CSVs (committed)
stats/                   per-firmware halucinator stats.yaml with
                         intercept dispatch counts (committed)
figure/macrobench.pdf    the rendered Figure 9
```

## Output format

`results/<firmware>.csv`:

```
iter,firmware,backend,wall_s
1,stm32_uart_it,halucinator,1.5595
1,stm32_uart_it,fastdyn-py,1.7033
...
```

`summarize.py` prints a markdown table:

```
| firmware       | HALucinator (s) | FastDyn-Py (s)  | speedup |
| stm32_uart_it  |     1.55 ± 0.05 |     1.70 ± 0.06 |   0.91× |
| zephyr_k64f    |    10.63 ± 0.03 |     2.18 ± 0.03 |   4.87× |
| zephyr_h103    |    10.48 ± 0.01 |     1.54 ± 0.01 |   6.82× |
| zephyr_fs      |   122.12 ± 0.10 |     3.18 ± 0.02 |  38.37× |

geomean speedup across 4 workloads: 5.84×
```

## Intercept counts

`stats/<firmware>.yaml` records the per-breakpoint dispatch counts
(HALucinator's own `hal_stats` counters), used to annotate workload
complexity in Figure 9. Regenerate by re-running each firmware and
copying the emitted `tmp/HALucinator/stats.yaml` (or `tmp/<name>/stats.yaml`
if the firmware uses `-n <name>`).

The paper reports these dispatch counts as evidence that the
`stm32_uart_it` slowdown is a startup-dominated microbenchmark
(8 total dispatches) whereas `zephyr_fs` amortizes the fixed
plugin cost over 1,177 dispatches, yielding the 38.37× speedup.

## Naming

For clarity across scripts, docs, and paper prose:

| CLI / container / CSV | Human-facing label |
|---|---|
| `halucinator` | HALucinator (unmodified, Avatar2 + GDB backend) |
| `fastdyn-py`  | FastDyn-Py (FastDyn plugin with embedded Python interpreter) |
