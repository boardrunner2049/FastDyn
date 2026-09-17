# ADC + DMA (NUCLEO-F103RB)

Firmware: ADC1 converts continuously; DMA1 channel 1 moves every result from `ADC1->DR` into a RAM buffer and raises the transfer interrupt (IRQ 11), which the firmware services in a loop. Covers the paper's ADC test (read an analog-to-digital conversion) and DMA test (transfer from peripheral MMIO register to RAM).

Fully rehosted — no board needed. Models under test are generated below: ADC1 and DMA1 (coupled: ADC1 issues the DMA request, DMA1 writes RAM and raises the IRQ). RCC, FLASH and GPIOA are the same models as in `../GPIO_with_GPIO_INT` and are included in `generated_models/`.

All commands run from the FastDyn repo root.

## Setup

```bash
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/ADC_with_DMA/generated_models/*.c boardrunner/boardrunner_sdk/model/
mkdir -p hardware_log
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/ADC_with_DMA/hardware_log/io.trace hardware_log/io.log
```

## Run

No input is needed. Let it run for ~10 s, then Ctrl-C:

```bash
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/ADC_with_DMA/adc_with_dma_config.toml
```

Expected in `io.log`: a continuous stream of DMA1 interrupts, each entry reading `DMA1->ISR`, `CCR1` and clearing `IFCR`:

```bash
grep -c "Interrupt Taken.*Vector = 0x0000001B" io.log   # DMA1_Channel1 (IRQ 11 + 16), expect > 1000 for a 10 s run
grep -c "address = 0x40020004" io.log                   # IFCR clears, about the same count
```

## ADC1 + DMA1 (read a conversion, transfer register -> RAM)

```bash
fastdyn generate -hw hardware_log/io.log -b STM32F103xx -p DMA1 -p ADC1 -p NVIC -mname DMA1 -ms DMA1 -o ./fastdyn_work_dma1
fastdyn llm -d fastdyn_work_dma1 -o boardrunner/boardrunner_sdk/model/dma1_model.c --compile --model gpt-5.4 --reasoning-effort medium

fastdyn generate -hw hardware_log/io.log -b STM32F103xx -p ADC1 -p DMA1 -mname ADC1 -ms ADC1 -o ./fastdyn_work_adc1
fastdyn llm -d fastdyn_work_adc1 -o boardrunner/boardrunner_sdk/model/adc1_model.c --compile --model gpt-5.4 --reasoning-effort medium
```

Run, then:

```bash
fastdyn verifier -hw hardware_log/io.log -em io.log -b STM32F103xx \
    -p ADC1 -p DMA1 -mname ADC1 -mname DMA1 \
    -d ADC1:boardrunner/boardrunner_sdk/model/adc1_model.c \
    -d DMA1:boardrunner/boardrunner_sdk/model/dma1_model.c -o ./fastdyn_work

# on mismatch, then re-run + re-verify:
fastdyn llm -d fastdyn_work \
    -o boardrunner/boardrunner_sdk/model/adc1_model.c \
    -o boardrunner/boardrunner_sdk/model/dma1_model.c \
    --compile --model gpt-5.4 --reasoning-effort medium --stateless
```

Expected: `All peripherals passing`. In our run the first synthesis needed one correction round (DMA1 ISR/IFCR handshake).

Symbols must be `<device>_init/_read/_write` matching the TOML device name (`adc1`, `dma1`); if the LLM picked another prefix, rename and `cmake --build boardrunner/boardrunner_sdk/build -j`.

## Hardware trace / passthrough (board required)

`adc_with_dma_config.toml`: every passthrough handler on, every elder handler off, `remaining_space` passthrough on. Every MMIO access is forwarded to the real MCU over ST-Link; the firmware runs its ADC/DMA loop continuously with nothing to observe externally.

```bash
tests/idle_firmwares/flash_scripts/flash.sh tests/idle_firmwares/prebuilt/idlefirmware_f103.axf
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/ADC_with_DMA/adc_with_dma_config.toml
# let it run ~10 s, Ctrl-C
cp io.log boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/ADC_with_DMA/hardware_log/io.trace
grep -c "Interrupt Taken.*Vector = 0x0000001B" io.log   # > 0
```
