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

## Run (Docker, no hardware)

Everything for Tables 5/6/7 on this board is in one self-contained image, `pseclab/boardrunner:1.0.0` (`pseclab/boardrunner@sha256:d2baa5868bf76285989c83fb5280a70692713e203b0e66787a55dde6236d86c9`): patched QEMU, the FastDyn plugin with device models, passthrough and the basic-block counter, the `fastdyn` CLI, the VIO SDK, and every firmware, generated model, hardware trace and replay tape of this directory.

```bash
docker pull pseclab/boardrunner:1.0.0
mkdir -p results && cd results
docker run --rm -v "$PWD:/results" pseclab/boardrunner:1.0.0 example all       # the 7 rehosted workloads (Table 7), ~2 min
docker run --rm -v "$PWD:/results" pseclab/boardrunner:1.0.0 table6 all        # Table 6: 5 firmwares x {null, replay, boardrunner}, ~5 min
docker run --rm -v "$PWD:/results" pseclab/boardrunner:1.0.0 verify uart       # trace verification (TV) of a boardrunner run
```

`example <name>` runs one workload (`GPIO_with_GPIO_INT UART SPI I2C ADC_with_DMA TIMER PWM`) for 10 s (third argument changes it) in a fresh QEMU, drives its input where it has one (GPIO: 10 button presses on the emulated PC13; UART: types 10 characters into the emulated USART2), and prints the unique-basic-block count and the result of the workload's functional check (Table 7: the GPIO callback ran once per edge, the typed characters were echoed, the BME280 chip id was read over I2C, the SPI read-back matched, DMA transfers completed, TIM3 update interrupts fired, TIM3->CNT advances). The I/O log of each run is written to `results/examples/<name>.log`.

`table6 <fw> <mode>` (`fw`: `gpio uart i2c spi adc`; `mode`: `null replay boardrunner`) measures one cell of Table 6 and prints the block count and the functional check; `table6 all` runs the 15 hardware-free cells and prints the table (`results/table6.md`, block lists and I/O logs in `results/table6/`). `verify <fw>` runs VIOMapper's Verifier on the boardrunner run's I/O log against the recorded hardware trace (TV; expected `matched` / `All peripherals passing`, warnings are fine).

Expected: BoardRunner and Replay execute the same block set for every firmware and Null a strict subset (counts below); BoardRunner passes every functional check; Null fails all of them (no peripheral responds); Replay fails the input-driven ones (GPIO, UART: it reproduces the recording instead of responding to the presses/characters of this run), which is the paper's FC ✗ for Replay. See `table6/README.md` for the numbers and why they differ slightly from the paper's. If a cell is far off or its FC fails unexpectedly, re-run it: the scripted inputs are sent a fixed 3 s after QEMU starts, and on a heavily loaded host (Docker Desktop VM, other containers running) the firmware may not yet be polling when they arrive. `table6 <fw> <mode> 20` widens the window.

## Hardware (optional): NUCLEO-F103RB

Nothing above needs a board. With a NUCLEO-F103RB on USB you can additionally reproduce the Passthrough column of Table 6, re-record the replay tapes, and re-capture any example's hardware trace. **SPI and I2C additionally need a Bosch BME280 breakout wired to the board** (wiring tables in `Nucleo-F103RB/SPI/README.md` and `Nucleo-F103RB/I2C/README.md`); the other five workloads (GPIO, UART, ADC+DMA, TIMER, PWM) need only the board. The board's ST-Link (USB `0483:374b`) must be openable by your user (udev rule or `plugdev` group).

The board runs an idle `while(1)` firmware while FastDyn executes the real firmware in QEMU and forwards every MMIO access to the chip, so the two CPUs do not contend for the peripherals. Flash it once, then run any passthrough mode; for example the UART firmware, whose banner appears on the board's virtual COM port and whose LD2 turns on after 10 characters:

```bash
lsusb | grep -i st-link
docker run --rm -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' \
    pseclab/boardrunner:1.0.0 check                          # "Cortex-M3 r1p1 processor detected"
docker run --rm -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' \
    pseclab/boardrunner:1.0.0 flash                          # idle firmware, once: "** Verified OK **"
docker run --rm -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' \
    --device=/dev/ttyACM0 -v "$PWD:/results" pseclab/boardrunner:1.0.0 table6 uart passthrough
```

`table6 <fw> passthrough` prints the block count (expected: identical to the boardrunner and replay cells) and the functional check observed on the real peripheral; `table6 <fw> record` additionally leaves an icount-stamped tape in `results/table6/<fw>_record.trace` that replaces `table6/tapes/<fw>/record.trace`. `--device=/dev/ttyACM0` is only needed for `uart` (the driver types into the board's VCP). GPIO passthrough asks you to press the blue user button during the run.

Each example's own README (`Nucleo-F103RB/<example>/README.md`, section "Hardware trace / passthrough") documents the same flow for re-capturing that example's `hardware_log/io.trace` with the `fastdyn` CLI, plus the wiring for SPI/I2C; the generation/verification commands there regenerate the models from a trace (OpenAI API key required, `OPENAI_API_KEY`; not needed for anything else).

## Without Docker


- FastDyn built (`build/libfastdyn.so`) and the `fastdyn` CLI on `PATH` (`source setup.sh`), run from the FastDyn repo root.
- `socat` (UART, SPI/I2C are self-contained; the GPIO examples use plain named pipes).
- To **regenerate** models: an OpenAI key in `~/.fastdyn.env` (`OPENAI_API_KEY=...`). Running the shipped models needs no key.
- To **recapture** a trace: a NUCLEO-F103RB, the idle firmware (`tests/idle_firmwares/`), and for SPI/I2C a BME280 breakout — see the "Hardware trace / passthrough" section of each README. No hardware is needed otherwise.

Notes:

- The model `.so` files are built from `boardrunner/boardrunner_sdk/model/*.c`; each README's Setup step copies that example's models there. Copy one example at a time — shared names (`gpioa_model.c`, …) are identical, but `tim3_model.c` differs between `TIMER` and `PWM`.
- LLM synthesis is not deterministic; regenerating a model may take a different number of correction rounds than reported in the README. The verifier verdict (`matched` / `All peripherals passing`) is the acceptance criterion.
- Hardware traces contain one artifact of the HITL setup: after an interrupt is served, the board re-enters the ISR once more with the flag already cleared (a second, empty ISR entry per event). The emulated models raise one interrupt per event; the verifier reports this as a warning, not a mismatch.
