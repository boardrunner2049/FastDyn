# Fully rehosted SDK firmware (NUCLEO-F103RB)

Seven STM32CubeF1 SDK workloads, one per peripheral class in the paper's Table 7, rehosted with **no hardware in the loop**: every peripheral the firmware touches is served by a BoardRunner-generated model, and the Cortex-M core (NVIC, SysTick, SCB) by QEMU. Each example ships everything needed to run it and to regenerate its models:

```
<example>/
├── firmwares/*.elf          firmware binary
├── hardware_log/io.trace    MMIO trace captured on the board (passthrough)
├── generated_models/*.c     the verified models (plus SPI/I2C slave models)
├── <example>_config.toml    FastDyn platform config, in rehosted state
└── README.md                run / generate / verify commands
```

| Example | Paper test (Table 7) | Models generated | Included |
|---|---|---|---|
| `GPIO_with_GPIO_INT` | GPIO: read/set/clear a pin; callback after pin interrupt | GPIOA, GPIOC, EXTI | RCC, FLASH, AFIO |
| `UART` | receive / transmit a byte | USART2 | RCC, FLASH, GPIOA |
| `SPI` | receive / transmit a byte (BME280) | SPI2 (+GPIOB chip select) | BME280 slave, RCC, FLASH, GPIOA |
| `I2C` | read / write a byte to a slave (BME280) | I2C1, GPIOB | BME280 slave, RCC, FLASH, GPIOA, AFIO |
| `ADC_with_DMA` | read a conversion; transfer register → RAM | ADC1, DMA1 | RCC, FLASH, GPIOA |
| `TIMER` | read counter; callback after interrupt | TIM3 | RCC, FLASH, GPIOA |
| `PWM` | configure PWM as an autonomous peripheral | TIM3 | RCC, FLASH, GPIOA |

RCC, FLASH, GPIOA and AFIO are generated once (in `GPIO_with_GPIO_INT`) and reused; the copies in every `generated_models/` are identical and were verified against each example's trace.

## Prerequisites

- FastDyn built (`build/libfastdyn.so`) and the `fastdyn` CLI on `PATH` (`source setup.sh`), run from the FastDyn repo root.
- `socat` (UART, SPI/I2C are self-contained; the GPIO examples use plain named pipes).
- To **regenerate** models: an OpenAI key in `~/.fastdyn.env` (`OPENAI_API_KEY=...`). Running the shipped models needs no key.
- To **recapture** a trace: a NUCLEO-F103RB, the idle firmware (`tests/idle_firmwares/`), and for SPI/I2C a BME280 breakout — see the "Hardware trace / passthrough" section of each README. No hardware is needed otherwise.

## Notes for evaluators

- The model `.so` files are built from `boardrunner/boardrunner_sdk/model/*.c`; each README's Setup step copies that example's models there. Copy one example at a time — shared names (`gpioa_model.c`, …) are identical, but `tim3_model.c` differs between `TIMER` and `PWM`.
- LLM synthesis is not deterministic; regenerating a model may take a different number of correction rounds than reported in the README. The verifier verdict (`matched` / `All peripherals passing`) is the acceptance criterion.
- Hardware traces contain one artifact of the HITL setup: after an interrupt is served, the board re-enters the ISR once more with the flag already cleared (a second, empty ISR entry per event). The emulated models raise one interrupt per event; the verifier reports this as a warning, not a mismatch.
