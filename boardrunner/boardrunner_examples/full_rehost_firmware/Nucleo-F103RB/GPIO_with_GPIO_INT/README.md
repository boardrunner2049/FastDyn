# GPIO + GPIO_INT (NUCLEO-F103RB)

Firmware: PC13 (user button) is an EXTI15_10 interrupt; the ISR reads PC13 and drives LD2 (PA5). Covers the paper's GPIO tests — read, set, clear a pin (GPIOA, GPIOC) and execute a callback after a pin interrupt (EXTI).

Fully rehosted — no board needed. Models under test are generated below: GPIOA, GPIOC, EXTI. RCC, FLASH and AFIO are included in `generated_models/`. The only hardware coupling is GPIOC → EXTI (pin level → edge → IRQ 40; line-to-port mapping in AFIO), so those share trace context.

All commands run from the FastDyn repo root.

## Setup

```bash
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/GPIO_with_GPIO_INT/generated_models/*.c boardrunner/boardrunner_sdk/model/
mkdir -p hardware_log
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/GPIO_with_GPIO_INT/hardware_log/io.trace hardware_log/io.log
```

## Run

Used after each generation step below. Terminal 1:

```bash
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/GPIO_with_GPIO_INT/gpio.toml
```

Terminal 2 — button (FIFO path is the `#define` at the top of `gpioc_model.c`); Terminal 3 — LED:

```bash
for i in $(seq 15); do printf '0' > /tmp/gpioc_pc13; sleep 0.25; printf '1' > /tmp/gpioc_pc13; sleep 0.25; done
cat /tmp/gpioa_out      # PA5=1 on press, PA5=0 on release
```

Ctrl-C terminal 1. Each edge shows `Interrupt Taken: Vector = 0x00000038` followed by a GPIOA BSRR write (`0x20` / `0x200000`) in `io.log`.

## 1. GPIOA (set / clear a pin)

```bash
fastdyn generate -hw hardware_log/io.log -b STM32F103xx -p GPIOA -mname GPIOA -ms GPIOA -o ./fastdyn_work_gpioa
fastdyn llm -d fastdyn_work_gpioa -o boardrunner/boardrunner_sdk/model/gpioa_model.c --compile --model gpt-5.4 --reasoning-effort medium
```

Run, then:

```bash
fastdyn verifier -hw hardware_log/io.log -em io.log -b STM32F103xx \
    -p GPIOA -mname GPIOA -d GPIOA:boardrunner/boardrunner_sdk/model/gpioa_model.c -o ./fastdyn_work

# on mismatch, then re-run + re-verify:
fastdyn llm -d fastdyn_work -o boardrunner/boardrunner_sdk/model/gpioa_model.c \
    --compile --model gpt-5.4 --reasoning-effort medium --stateless
```

## 2. GPIOC + EXTI (read a pin, callback after interrupt)

```bash
fastdyn generate -hw hardware_log/io.log -b STM32F103xx -p GPIOC -p EXTI -p AFIO -mname GPIOC -ms GPIOC -o ./fastdyn_work_gpioc
fastdyn llm -d fastdyn_work_gpioc -o boardrunner/boardrunner_sdk/model/gpioc_model.c --compile --model gpt-5.4 --reasoning-effort medium

fastdyn generate -hw hardware_log/io.log -b STM32F103xx -p EXTI -p GPIOC -p AFIO -p NVIC -mname EXTI -ms EXTI -o ./fastdyn_work_exti
fastdyn llm -d fastdyn_work_exti -o boardrunner/boardrunner_sdk/model/exti_model.c --compile --model gpt-5.4 --reasoning-effort medium
```

Run, then:

```bash
fastdyn verifier -hw hardware_log/io.log -em io.log -b STM32F103xx \
    -p GPIOC -p EXTI -mname GPIOC -mname EXTI \
    -d GPIOC:boardrunner/boardrunner_sdk/model/gpioc_model.c \
    -d EXTI:boardrunner/boardrunner_sdk/model/exti_model.c -o ./fastdyn_work

# on mismatch, then re-run + re-verify:
fastdyn llm -d fastdyn_work \
    -o boardrunner/boardrunner_sdk/model/gpioc_model.c \
    -o boardrunner/boardrunner_sdk/model/exti_model.c \
    --compile --model gpt-5.4 --reasoning-effort medium --stateless
```

Expected: `All peripherals passing`. Two EXTI warnings on `PR` are expected — the hardware trace contains a second, empty ISR entry per edge (`PR` reads `0`) that the emulated EXTI does not reproduce.

Symbols must be `<device>_init/_read/_write` matching the TOML device name (`gpioa`, `gpioc`, `exti`); if the LLM picked another prefix, rename and `cmake --build boardrunner/boardrunner_sdk/build -j`.

## Hardware trace / passthrough (board required)

`gpio.toml`: every passthrough handler on, every elder handler off, `remaining_space` passthrough on. Every MMIO access is forwarded to the real MCU over ST-Link, so the behavior is observable on the board itself: pressing the blue user button (B1) lights the physical LD2, releasing it turns LD2 off.

```bash
tests/idle_firmwares/flash_scripts/flash.sh tests/idle_firmwares/prebuilt/idlefirmware_f103.axf
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/GPIO_with_GPIO_INT/gpio.toml
# press the blue button a few times, Ctrl-C
cp io.log boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/GPIO_with_GPIO_INT/hardware_log/io.trace
grep -c "Vector = 0x00000038" io.log   # > 0, one per press/release
```
