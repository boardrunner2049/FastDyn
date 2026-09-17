# SPI (NUCLEO-F103RB + BME280)

Firmware: SPI2 master, polling. Writes the BME280 `config` register (`0xF5` ← `0x1C`), reads it back, and turns LD2 on if the sensor returned `0x1C`. Chip select is PB12 driven as a GPIO. Covers the paper's SPI test — transmit and receive a byte.

Fully rehosted — no board and no sensor needed. The model under test is SPI2 (SPI2 registers + GPIOB, since the CS toggle on PB12 is part of the transaction); the BME280 slave model is included in `generated_models/bme280_slave.c`, as are RCC, FLASH and GPIOA (same models as `../GPIO_with_GPIO_INT`).

All commands run from the FastDyn repo root.

## Setup

```bash
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/SPI/generated_models/*.c boardrunner/boardrunner_sdk/model/
mkdir -p hardware_log
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/SPI/hardware_log/io.trace hardware_log/io.log
```

## Run

Terminal 1 — LD2 is emulated by the GPIOA model and reported on a FIFO:

```bash
cat /tmp/gpioa_out      # expect a single PA5=1 once the read-back matched
```

Terminal 2:

```bash
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/SPI/spi_config.toml
```

The transaction completes within a second; Ctrl-C afterwards. Expected in `io.log`:

```bash
grep "Read.*address = 0x4000380C" io.log | grep -oE "value = 0x[0-9a-f]+"   # ff ff ff ff 1c 1c
grep -c "0x40010810.*value = 0x00000020" io.log                             # 1  (LED on once, never off)
```

## SPI2 (transmit / receive a byte)

```bash
fastdyn generate -hw hardware_log/io.log -b STM32F103xx -p SPI2 -p GPIOB -mname SPI2 -ms SPI2 -ms GPIOB -o ./fastdyn_work_spi2
fastdyn llm -d fastdyn_work_spi2 -o boardrunner/boardrunner_sdk/model/spi2_model.c --compile --model gpt-5.4 --reasoning-effort medium
```

Run, then:

```bash
fastdyn verifier -hw hardware_log/io.log -em io.log -b STM32F103xx \
    -p SPI2 -p GPIOB -mname SPI2 \
    -d SPI2:boardrunner/boardrunner_sdk/model/spi2_model.c \
    -d BME280:boardrunner/boardrunner_sdk/model/bme280_slave.c -o ./fastdyn_work

# on mismatch, then re-run + re-verify:
fastdyn llm -d fastdyn_work \
    -o boardrunner/boardrunner_sdk/model/spi2_model.c \
    -o boardrunner/boardrunner_sdk/model/bme280_slave.c \
    --compile --model gpt-5.4 --reasoning-effort medium --stateless
```

Expected: `All peripherals passing`. In our run the master passed first time; one correction round adjusted the slave's idle MISO value (`0xff`, as the real sensor drives it).

Symbols must be `spi2_init/_read/_write` for the master and `slave_spi_transfer` / `slave_spi_set_cs` for the slave; if the LLM picked another prefix, rename and `cmake --build boardrunner/boardrunner_sdk/build -j`.

## Hardware trace / passthrough (board + BME280 required)

Wiring — BME280 breakout in SPI mode:

| BME280 | MCU pin | Nucleo header |
|---|---|---|
| VCC | 3V3 | CN6 pin 4 |
| GND | GND | CN6 pin 6 |
| SCK / SCL | PB13 (SPI2_SCK) | CN10 pin 30 |
| SDO | PB14 (SPI2_MISO) | CN10 pin 28 |
| SDI / SDA | PB15 (SPI2_MOSI) | CN10 pin 26 |
| CSB | PB12 (CS) | CN10 pin 16 |

`spi_config.toml`: every passthrough handler on, every elder handler off, `remaining_space` passthrough on. Every MMIO access is forwarded to the real MCU over ST-Link; with the sensor wired correctly the physical LD2 turns solid on, otherwise it stays off.

```bash
tests/idle_firmwares/flash_scripts/flash.sh tests/idle_firmwares/prebuilt/idlefirmware_f103.axf
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/SPI/spi_config.toml
# LD2 on within a few seconds, Ctrl-C
cp io.log boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/SPI/hardware_log/io.trace
grep "Read.*address = 0x4000380C" io.log | grep -oE "value = 0x[0-9a-f]+" | tail -1   # 0x1c
```
