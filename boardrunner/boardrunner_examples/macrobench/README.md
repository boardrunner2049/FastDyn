# Macrobench: HALucinator vs HALucinator-FD (Figure 9)

End-to-end wall time of four workloads from HALucinator's public test corpus under unmodified **HALucinator** (Avatar2 + GDB backend) and **HALucinator-FD** (the same HALucinator on the FastDyn backend: FastDyn plugin with an embedded Python interpreter). No hardware. Each backend is a self-contained Docker image; nothing is built on the host.

| Image | Backend | Pin |
|---|---|---|
| `pseclab/halucinator-macro:1.0.1` | HALucinator, upstream `halucinator/halucinator` @ `5403321` unmodified (built on `pseclab/halucinator:5403321`) | `pseclab/halucinator-macro@sha256:f9a6a33bbadfc3527443f89450f68533f1242c1fa9736eca79ba3978ad8b20b8` (3.4 GB) |
| `pseclab/halucinator-fd-macro:1.0.1` | HALucinator-FD (built on `pseclab/halucinator-fd:1.3.0`, `PSecLab/Halucinator_Turbo` @ `c8bed67` with FastDyn @ `82abf50`) | `pseclab/halucinator-fd-macro@sha256:a49c6323d59f2f007fb8ccd53832ec2f84727402b5a6f8c4a73ab784900518a4` (2.1 GB) |

## Run

```bash
docker pull pseclab/halucinator-fd-macro:1.0.1
docker pull pseclab/halucinator-macro:1.0.1
mkdir -p results && cd results
docker run --rm -v "$PWD:/results" pseclab/halucinator-fd-macro:1.0.1 all 10   # ~3 min
docker run --rm -v "$PWD:/results" pseclab/halucinator-macro:1.0.1 all 10      # ~25 min
docker run --rm -v "$PWD:/results" pseclab/halucinator-macro:1.0.1 summary     # table + average speedup
docker run --rm -v "$PWD:/results" pseclab/halucinator-macro:1.0.1 plot        # -> macrobench.pdf
```

`all N` runs the four workloads (`stm32_uart_it`, `zephyr_k64f`, `zephyr_h103`, `zephyr_fs`) N times each and prints one line per run; `<firmware> [N]` runs a subset. Every run is a fresh HALucinator + QEMU process. Before the timed iterations of each firmware, one untimed test run checks that the workload runs in this container (and absorbs the first-use costs of a fresh container: page cache, bytecode); it is not recorded. A run that does not reach its success marker within the per-firmware timeout (`firmwares.toml`, ~3× the expected HALucinator time; `-e TIMEOUT=<s>` overrides) is killed, its output saved to `results/<firmware>_<backend>_iter<i>.log`, and retried once. Both images write rows `iter,firmware,backend,wall_s` into the same `results/<firmware>.csv`, so `summary` and `plot` see both backends.

Expected (`summary`; paper, 10 iterations):

```
| firmware       | HALucinator (s) | HALucinator-FD (s)  | speedup |
| stm32_uart_it  |     1.55 ± 0.05 |     1.70 ± 0.06 |   0.91× |
| zephyr_k64f    |    10.63 ± 0.03 |     2.18 ± 0.03 |   4.87× |
| zephyr_h103    |    10.48 ± 0.01 |     1.54 ± 0.01 |   6.82× |
| zephyr_fs      |   122.12 ± 0.10 |     3.18 ± 0.02 |  38.37× |
average speedup across 4 workloads: 12.74×
```

Absolute times vary ±10% with the host; the speedups reproduce within ±5% and their ordering (`zephyr_fs` >> `zephyr_h103` > `zephyr_k64f` >> `stm32_uart_it`) is stable. `stm32_uart_it` is expected to be *slower* on HALucinator-FD (0.91×): it issues only 8 handler dispatches (`stats/stm32_uart_it.yaml`, HALucinator's own per-breakpoint counters), too few to amortize the plugin's fixed load cost, while `zephyr_fs` issues 1,177 (`stats/zephyr_fs.yaml`). The paper's per-iteration CSVs are in `results/paper/`.

## How it works

Wall time is measured by the UART peer (`peers/bench_uart_peer.py`), a scripted stand-in for HALucinator's `hal_dev_uart` that subscribes to the firmware's UART over HALucinator's own ZMQ topics: from the moment it is listening and the emulator is launched until the firmware's success marker appears on the UART, sending scripted input at the prompts in between (`firmwares.toml`). The peer is started first and reconnects to HALucinator's `ipc://` sockets every 2 ms until the emulator binds them; with libzmq's default 100 ms interval the fast FD backend can print the whole prompt before the peer is subscribed, which shows up as a hung run. `macro_run.py` is the in-container runner and `run.sh` the entrypoint (`all|<fw>...|summary|plot`); `summarize.py` and `make_macro_figure.py` read `results/*.csv`. The Dockerfile builds either image from this directory: `docker build --build-arg BASE=<base image> --build-arg BACKEND=halucinator|halucinator-fd -t <name> .`.
