# PWM (NUCLEO-F103RB)

Firmware: TIM3 channel 1 generates a PWM signal on PA6 (D12); the main loop samples `TIM3->CNT` every 10 ms. Covers the paper's PWM test — configure PWM as an autonomous peripheral.

Fully rehosted — no board needed. The model under test is TIM3, generated below. RCC, FLASH and GPIOA are the same models as `../GPIO_with_GPIO_INT` and are included in `generated_models/`.

All commands run from the FastDyn repo root.

## Setup

```bash
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/PWM/generated_models/*.c boardrunner/boardrunner_sdk/model/
mkdir -p hardware_log
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/PWM/hardware_log/io.trace hardware_log/io.log
```

## Run

No input is needed. ~8 s, then Ctrl-C:

```bash
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/PWM/pwm_config.toml
```

Expected in `io.log`: the counter advances on every 10 ms sample:

```bash
grep -c "Read.*address = 0x40000424" io.log                                             # TIM3 CNT reads, > 500
grep "Read.*address = 0x40000424" io.log | grep -oE "value = 0x[0-9a-f]+" | head -5     # increasing, ~0x2f per sample
```

## TIM3 (PWM)

```bash
fastdyn generate -hw hardware_log/io.log -b STM32F103xx -p TIM3 -p GPIOA -mname TIM3 -ms TIM3 -o ./fastdyn_work_tim3
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

Expected: `Both hardware log and emulation log matched!` (a "number of data accesses" warning on CNT is expected — it is a free-running counter, so the sampled values are data, not state).

Symbols must be `tim3_init/_read/_write`; if the LLM picked another prefix, rename and `cmake --build boardrunner/boardrunner_sdk/build -j`.

## Hardware trace / passthrough (board required)

`pwm_config.toml`: every passthrough handler on, every elder handler off, `remaining_space` passthrough on. Every MMIO access is forwarded to the real MCU over ST-Link; the PWM waveform is present on PA6 (D12) and can be seen with a scope or logic analyzer, nothing else is observable.

```bash
tests/idle_firmwares/flash_scripts/flash.sh tests/idle_firmwares/prebuilt/idlefirmware_f103.axf
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/PWM/pwm_config.toml
# ~8 s, Ctrl-C
cp io.log boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/PWM/hardware_log/io.trace
grep -c "Read.*address = 0x40000424" io.log   # > 500
```
