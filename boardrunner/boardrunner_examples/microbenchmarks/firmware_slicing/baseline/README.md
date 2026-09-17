# HALucinator slicing microbenchmark (baseline for Figure 8a/8b)

Runs the same `slice_probe` firmware inside the `halucinator` Docker container with two intercept handlers, N=1000 dispatches per run:

- `PcUpdate` (`halucinator.bp_handlers.sentinel_log.PcUpdate`) — modifier analog: `return True, None` (HALucinator sets PC=LR, the emulated function is skipped)
- `CounterVirt` (`halucinator.bp_handlers.sentinel_log.CounterVirt`) — virtual-callback analog: `counter++; return False, None` (GDB single-steps the BKPT, the emulated function runs)

Reports per-iteration `SENTINEL_ELAPSED` and per-op cost. Our measurements are in `results/` (`pc_update_benchmark.txt`, `counter_update_benchmark.txt`; 100 and 10 iterations); `../scripts/plot_results.py` reads them directly.

## Prereqs (one-time)

The `halucinator` container is the one the macrobench uses: bring it up with `boardrunner/boardrunner_examples/macrobench/setup_containers.sh` (see `macrobench/REPRODUCE.md` for the image). Then, from this directory, copy the firmware sources, the configs and the handler into it and build the firmware at N=1000:

```bash
docker exec halucinator mkdir -p /root/microbench/build
docker cp ../firmware/microbench.c  halucinator:/root/microbench/
docker cp ../firmware/startup.s     halucinator:/root/microbench/
docker cp ../firmware/stm32_flash.ld halucinator:/root/microbench/
docker cp halucinator_configs/microbench_slice_addrs.yaml           halucinator:/root/microbench/
docker cp halucinator_configs/microbench_slice_memory.yaml          halucinator:/root/microbench/
docker cp halucinator_configs/microbench_slice_pcupdate_config.yaml halucinator:/root/microbench/
docker cp halucinator_configs/microbench_slice_counter_config.yaml  halucinator:/root/microbench/
docker cp halucinator_configs/sentinel_log.py halucinator:/root/halucinator/src/halucinator/bp_handlers/sentinel_log.py

docker exec halucinator bash -c "cd /root/microbench && \
    arm-none-eabi-gcc -mcpu=cortex-m4 -mthumb -mfloat-abi=soft \
        -Wall -O0 -g -ffreestanding -nostdlib -fno-common -fno-builtin \
        -DBENCH_SLICE -DN_INNER=1000 -DN_OUTER=1 \
        -T stm32_flash.ld -nostdlib -Wl,--gc-sections -Wl,-Map=build/output.map \
        startup.s microbench.c -o build/microbench_slice.axf && \
    arm-none-eabi-objcopy -O binary build/microbench_slice.axf build/microbench_slice.bin"
```

(Same flags as `../firmware/Makefile`; the container must have `arm-none-eabi-gcc`, which the macrobench image ships.)

## Run

```bash
scripts/run_pcupdate_benchmark.sh          # PcUpdate,    ITERS=10 (default)
scripts/run_counter_benchmark.sh           # CounterVirt, ITERS=10
ITERS=100 scripts/run_pcupdate_benchmark.sh
```

Each prints one `iter k  <elapsed>s  <per-op> ms/op` line per iteration and writes `results/slice_pcupdate_N1000.csv` / `results/slice_counter_N1000.csv` (`iter,elapsed_s,per_op_ms`). A PcUpdate iteration takes ~40 s, a CounterVirt iteration ~10 s.
