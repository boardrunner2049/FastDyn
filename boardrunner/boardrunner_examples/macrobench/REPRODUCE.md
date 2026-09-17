# Reproducing Figure 9 (macrobench)

This document lets an external reviewer reproduce the paper's
**HALucinator vs FastDyn-Py** end-to-end speedup numbers (Figure 9)
from a clean machine in ~40 minutes.

## Naming

Two backends are compared in Figure 9:

| Identifier (CLI / container / CSV) | Human-facing label | What it is |
|---|---|---|
| `halucinator`  | HALucinator | Unmodified HALucinator (Avatar2 + GDB backend) |
| `fastdyn-py`   | FastDyn-Py  | FastDyn plugin with embedded Python interpreter |

## Pinned artifacts

All pinned by immutable identifiers. Do not use `:latest` tags — they
will drift.

| Artifact | Pin |
|---|---|
| FastDyn-Py Docker image | `pseclab/fastdyn-py@sha256:2ce475b5d5e7e1830767c0d9f352c5368e03b7c0063a6eafd6a1a5a9a8d772c2` |
| HALucinator Docker image | `halucinator:latest` (upstream HALucinator, unmodified) |
| Halucinator_Turbo source | `PSecLab/Halucinator_Turbo` @ commit `c8bed67` (branch `dev`) |
| FastDyn source (submodule) | `PSecLab/FastDyn` @ commit `82abf50` (branch `halucinator-turbo-optimizations`) |

The FastDyn-Py image is standalone — all source, the compiled
`libfastdyn.so`, and a debug build of `qemu-system-arm` are baked in.
Cloning the source repos is only needed if you want to inspect or
modify the code.

## Host requirements

- Linux, x86_64
- Docker Engine ≥ 20 (rootless or `sudo`-less via `docker` group)
- Python 3.10+ with `matplotlib` (only for regenerating the figure)
- ≈ 8 GB free disk (Docker image ~6.7 GB)
- ≈ 30 min wall-clock for a full 10-iteration sweep

### Ubuntu 24.04 note

Ubuntu 24.04's default AppArmor policy blocks unprivileged user
namespaces, which breaks rootless Docker. If your rootless daemon
fails to start with `rootlesskit: operation not permitted`:

```bash
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
systemctl --user restart docker
```

This is not required for root-mode Docker.

## Bring-up (one-time, ~2 min once image is pulled)

```bash
# 1. Pull the pinned FastDyn-Py image (~6.7 GB download).
docker pull pseclab/fastdyn-py@sha256:2ce475b5d5e7e1830767c0d9f352c5368e03b7c0063a6eafd6a1a5a9a8d772c2

# 2. Pull upstream HALucinator image (for the baseline).
docker pull halucinator:latest       # or build from https://github.com/embedded-sec/halucinator

# 3. Clone this repo (if you haven't already).
git clone https://github.com/PSecLab/FastDyn
cd FastDyn/boardrunner/boardrunner_examples/macrobench

# 4. Bring up the two long-lived benchmark containers.
./setup_containers.sh
# Expected: "creating halucinator ... creating fastdyn-py ... ready."
```

## Run the sweep (~30 min)

```bash
./sweep.sh 10                  # 10 iterations × 2 backends × 4 firmwares
python3 summarize.py           # markdown table + geomean speedup
```

## Regenerate the figure

```bash
pip install --user matplotlib  # if not already installed
python3 make_macro_figure.py
# → boardrunner/boardrunner_examples/macrobench/figure/macrobench.pdf
```

## Expected results

`summarize.py` output on our reference hardware
(Intel Xeon Gold 6248R, Ubuntu 22.04, Docker 24.0):

```
| firmware       | HALucinator (s) | FastDyn-Py (s)  | speedup |
| stm32_uart_it  |     1.55 ± 0.05 |     1.70 ± 0.06 |   0.91× |
| zephyr_k64f    |    10.63 ± 0.03 |     2.18 ± 0.03 |   4.87× |
| zephyr_h103    |    10.48 ± 0.01 |     1.54 ± 0.01 |   6.82× |
| zephyr_fs      |   122.12 ± 0.10 |     3.18 ± 0.02 |  38.37× |

geomean speedup across 4 workloads: 5.84×
```

**Tolerance.** Absolute wall times will vary by ±10 % depending on
host CPU speed and I/O. The **speedup ratios** are the paper's
headline claim and should reproduce within ±5 % on any modern x86_64
host. The relative ordering (`zephyr_fs` >> `zephyr_h103` >
`zephyr_k64f` >> `stm32_uart_it`) is stable across all hardware we
have tested.

## Interpreting the STM32 UART result

FastDyn-Py is **0.91× on stm32_uart_it** (slightly slower than
HALucinator). This is the expected outcome for a startup-dominated
microbenchmark: `stm32_uart_it` issues only **8 handler dispatches**
in its entire lifetime (see `stats/stm32_uart_it.yaml` —
HALucinator's own per-breakpoint counters), while `zephyr_fs` issues
**1,177** (see `stats/zephyr_fs.yaml`). FastDyn-Py's per-op savings
amortize the plugin's fixed loading cost only when the firmware
sustains many per-op dispatches, which is why the speedup grows
monotonically with dispatch count. See Figure 9 in the paper.

## Full result tables (per iteration)

Raw per-iteration CSVs are committed to `results/`:

```
results/stm32_uart_it.csv    # 10 iters × 2 backends
results/zephyr_k64f.csv
results/zephyr_h103.csv
results/zephyr_fs.csv
```

Each row: `iter,firmware,backend,wall_s`, with `backend` ∈
{`halucinator`, `fastdyn-py`}.

## Troubleshooting

**`halucinator: PackageNotFoundError`** — should not occur with the
pinned 1.3.0 image (the entry-point metadata is baked in). If it does,
you may be using an older image. `docker pull` again by digest.

**Peer never signals READY / 10s timeout** — the peer subscribes on
ZMQ ports `5555`/`5556`. If another process on your host uses these
ports, adjust `--rx-port/--tx-port` in `runner.py:run_uart` or set
`DOCKER_HOST` so the containers talk over their own namespaces.

**QEMU exits immediately with `libpython3.NN.so.1.0: cannot open`** —
you built `libfastdyn.so` on the host against a different Python
version than the container's Python. The pinned image ships a plugin
built inside the container against Python 3.10, so this only happens
if `FASTDYN_MOUNT_SRC` is set to a locally-built copy. Unset it to
use the image's baked-in `libfastdyn.so`.

**`samr21_*` firmwares** — pre-existing config issues in HALucinator's
public corpus; not part of Figure 9. Excluded from `sweep.sh`'s
default list.
