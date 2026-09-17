# TIMER (NUCLEO-F103RB)

Firmware: TIM3 runs as a time base with the update interrupt enabled (IRQ 29); `HAL_TIM_PeriodElapsedCallback` toggles LD2 about once a second. The main loop samples `TIM3->CNT` every 10 ms. Covers the paper's Timer test — read the counter value and execute a callback after an interrupt.

Fully rehosted — no board needed. The model under test is TIM3, generated below. RCC, FLASH and GPIOA are the same models as `../GPIO_with_GPIO_INT` and are included in `generated_models/`.

All commands run from the FastDyn repo root.

## Setup

```bash
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/TIMER/generated_models/*.c boardrunner/boardrunner_sdk/model/
mkdir -p hardware_log
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/TIMER/hardware_log/io.trace hardware_log/io.log
```

## Run

Terminal 1 — LD2 is emulated by the GPIOA model and reported on a FIFO:

```bash
cat /tmp/gpioa_out      # PA5=1 / PA5=0 alternating about once a second
```

Terminal 2, ~25 s (the hardware trace is 25 s; matching the length keeps the verifier's pattern mining aligned), then Ctrl-C:

```bash
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/TIMER/timer_config.toml
```

Expected in `io.log`:

```bash
grep -c "Interrupt Taken.*Vector = 0x0000002D" io.log   # TIM3 update IRQ (29 + 16), ~1 per second
grep -c "0x40010810" io.log                             # LED writes, one per IRQ
```

## TIM3 (counter + interrupt callback)

```bash
fastdyn generate -hw hardware_log/io.log -b STM32F103xx -p TIM3 -p NVIC -mname TIM3 -ms TIM3 -o ./fastdyn_work_tim3
fastdyn llm -d fastdyn_work_tim3 -o boardrunner/boardrunner_sdk/model/tim3_model.c --compile --model gpt-5.4 --reasoning-effort medium
```

Run, then:

```bash
fastdyn verifier -hw hardware_log/io.log -em io.log -b STM32F103xx \
    -p TIM3 -mname TIM3 -d TIM3:boardrunner/boardrunner_sdk/model/tim3_model.c -o ./fastdyn_work

# on mismatch, then re-run + re-verify:
fastdyn llm -d fastdyn_work -o boardrunner/boardrunner_sdk/model/tim3_model.c \
    --compile --model gpt-5.4 --reasoning-effort medium --stateless
```

Expected: `Both hardware log and emulation log matched!`. In our run the first synthesis was functionally correct (one IRQ per update event, LED toggling) and needed one correction round for the `SR` read pattern.

Symbols must be `tim3_init/_read/_write`; if the LLM picked another prefix, rename and `cmake --build boardrunner/boardrunner_sdk/build -j`.

## Hardware trace / passthrough (board required)

`timer_config.toml`: every passthrough handler on, every elder handler off, `remaining_space` passthrough on. Every MMIO access is forwarded to the real MCU over ST-Link; the physical LD2 toggles about once a second.

```bash
tests/idle_firmwares/flash_scripts/flash.sh tests/idle_firmwares/prebuilt/idlefirmware_f103.axf
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/TIMER/timer_config.toml
# ~25 s, Ctrl-C
cp io.log boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/TIMER/hardware_log/io.trace
grep -c "Interrupt Taken.*Vector = 0x0000002D" io.log   # > 0
```
