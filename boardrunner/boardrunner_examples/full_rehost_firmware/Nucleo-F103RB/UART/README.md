# UART (NUCLEO-F103RB)

Firmware: prints a banner on USART2 (ST-Link VCP), waits for 10 characters, then prints success and turns LD2 on. Covers the paper's UART test — receive and transmit a byte.

Fully rehosted — no board needed. The model under test is USART2, generated below. RCC, FLASH and GPIOA (PA2/PA3 alternate-function setup) are the same models as in `../GPIO_with_GPIO_INT` and are included in `generated_models/`.

All commands run from the FastDyn repo root.

## Setup

```bash
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/UART/generated_models/*.c boardrunner/boardrunner_sdk/model/
mkdir -p hardware_log
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/UART/hardware_log/io.trace hardware_log/io.log
```

## Run

The USART2 model opens `/tmp/usart2_pty`; create it **before** `fastdyn run`. Terminal 1:

```bash
socat PTY,link=/tmp/usart2_pty,raw,echo=0 STDIO
```

Terminal 2:

```bash
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/UART/uart_config.toml
```

Terminal 3 — LD2 is emulated by the GPIOA model and reported on a FIFO:

```bash
cat /tmp/gpioa_out      # prints PA5=1 once the firmware turns the LED on
```

Terminal 1 shows the banner; type 10 characters there. Expected:

```
Target: Enter 10 characters to unlock.
Ready... Go!
HelloWorld
[ DONE ] Received 10 characters.
Status: Success. LED is now Solid ON.
```

Ctrl-C terminal 2. `io.log` has 227 DR writes and 10 DR reads (`grep -c 0x40004404 io.log` → 237).

## USART2 (receive / transmit a byte)

```bash
fastdyn generate -hw hardware_log/io.log -b STM32F103xx -p USART2 -mname USART2 -ms USART2 -o ./fastdyn_work_usart2
fastdyn llm -d fastdyn_work_usart2 -o boardrunner/boardrunner_sdk/model/usart2_model.c --compile --model gpt-5.4 --reasoning-effort medium
```

Run, then:

```bash
fastdyn verifier -hw hardware_log/io.log -em io.log -b STM32F103xx \
    -p USART2 -mname USART2 -d USART2:boardrunner/boardrunner_sdk/model/usart2_model.c -o ./fastdyn_work

# on mismatch, then re-run + re-verify:
fastdyn llm -d fastdyn_work -o boardrunner/boardrunner_sdk/model/usart2_model.c \
    --compile --model gpt-5.4 --reasoning-effort medium --stateless
```

Expected: `Both hardware log and emulation log matched!`. An `SR` entropy warning is expected — the hardware trace was typed by hand, so the firmware saw the line go `IDLE` between characters more often than it does with pasted input.

Symbols must be `usart2_init/_read/_write`; if the LLM picked another prefix, rename and `cmake --build boardrunner/boardrunner_sdk/build -j`.

## Hardware trace / passthrough (board required)

`uart_config.toml`: every passthrough handler on, every elder handler off, `remaining_space` passthrough on. Every MMIO access is forwarded to the real MCU over ST-Link, so the behavior is observable on the board itself: the banner appears on the VCP, and after 10 characters the success message prints and the physical LD2 turns solid on.

```bash
tests/idle_firmwares/flash_scripts/flash.sh tests/idle_firmwares/prebuilt/idlefirmware_f103.axf
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/UART/uart_config.toml
```

Second terminal — the VCP is the board's ST-Link serial port:

```bash
minicom -D /dev/ttyACM0 -b 115200     # type 10 characters after "Ready... Go!"
```

Ctrl-C `fastdyn run`, then:

```bash
cp io.log boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/UART/hardware_log/io.trace
grep -c "0x40004404" io.log   # DR accesses, expect ~237
```
