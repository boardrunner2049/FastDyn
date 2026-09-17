# I2C (NUCLEO-F103RB + BME280)

Firmware: I2C1 master (100 kHz, remapped to PB8/PB9) talking to a BME280 at `0x76`. It reads the chip ID and calibration blocks, configures the sensor, then loops reading the 8-byte measurement block and toggling LD2 each cycle; any I2C error makes LD2 blink forever. Covers the paper's I2C test — read or write a byte to a slave.

Fully rehosted — no board and no sensor needed. Models under test: I2C1 (generated) and GPIOB (generated; bus-recovery bit-bang on PB8/PB9 before the pins go to alternate function). The BME280 slave is included in `generated_models/bme280_i2c_slave.c`; RCC, FLASH, GPIOA and AFIO are the same models as `../GPIO_with_GPIO_INT`.

All commands run from the FastDyn repo root.

## Setup

```bash
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/I2C/generated_models/*.c boardrunner/boardrunner_sdk/model/
mkdir -p hardware_log
cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/I2C/hardware_log/io.trace hardware_log/io.log
```

## Run

Terminal 1 — LD2 is emulated by the GPIOA model and reported on a FIFO:

```bash
cat /tmp/gpioa_out      # PA5=1 / PA5=0 alternating, one toggle per measurement cycle
```

Terminal 2 (~10 s, then Ctrl-C):

```bash
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/I2C/i2c_config.toml
```

Expected in `io.log`:

```bash
grep -c "address = 0x40005410" io.log                                                   # I2C1 DR, hundreds
grep "Read.*address = 0x40005410" io.log | grep -oE "value = 0x[0-9a-f]+" | sed -n 2p   # 0x60 (chip ID)
```

A failed transaction shows instead as LD2 toggling every few ms (`fail_blink_forever`) with only a handful of DR accesses.

## I2C1 + GPIOB (read / write a byte to a slave)

```bash
fastdyn generate -hw hardware_log/io.log -b STM32F103xx -p I2C1 -p GPIOB -p AFIO -mname I2C1 -ms I2C1 -o ./fastdyn_work_i2c1
fastdyn llm -d fastdyn_work_i2c1 -o boardrunner/boardrunner_sdk/model/i2c1_model.c --compile --model gpt-5.4 --reasoning-effort medium

fastdyn generate -hw hardware_log/io.log -b STM32F103xx -p GPIOB -mname GPIOB -ms GPIOB -o ./fastdyn_work_gpiob
fastdyn llm -d fastdyn_work_gpiob -o boardrunner/boardrunner_sdk/model/gpiob_model.c --compile --model gpt-5.4 --reasoning-effort medium
```

Run, then:

```bash
fastdyn verifier -hw hardware_log/io.log -em io.log -b STM32F103xx \
    -p I2C1 -mname I2C1 \
    -d I2C1:boardrunner/boardrunner_sdk/model/i2c1_model.c \
    -d BME280:boardrunner/boardrunner_sdk/model/bme280_i2c_slave.c -o ./fastdyn_work

fastdyn verifier -hw hardware_log/io.log -em io.log -b STM32F103xx \
    -p GPIOB -mname GPIOB -d GPIOB:boardrunner/boardrunner_sdk/model/gpiob_model.c -o ./fastdyn_work

# on mismatch, then re-run + re-verify:
fastdyn llm -d fastdyn_work \
    -o boardrunner/boardrunner_sdk/model/i2c1_model.c \
    -o boardrunner/boardrunner_sdk/model/bme280_i2c_slave.c \
    --compile --model gpt-5.4 --reasoning-effort medium --stateless
```

Expected: `Both hardware log and emulation log matched!` for both. I2C1 is the most iteration-hungry model in this set: the STM32F1 master-receiver flag sequence (ADDR → RXNE|BTF → STOP handling for the 1-byte and N-byte `Mem_Read` paths) took several correction rounds in our run; GPIOB passed first time.

Symbols must be `<device>_init/_read/_write` for `i2c1` and `gpiob`, and `bme280_send / bme280_receive / bme280_event` for the slave (`device = "bme280"` in the TOML); if the LLM picked another prefix, rename and `cmake --build boardrunner/boardrunner_sdk/build -j`.

## Hardware trace / passthrough (board + BME280 required)

Wiring — Adafruit BME280 breakout in I2C mode:

| BME280 | Nucleo | Note |
|---|---|---|
| VIN | 3V3 (CN6 pin 4) | |
| GND | GND (CN6 pin 6) | |
| SCK / SCL | D15 = PB8 (CN5, top pin) | |
| SDI / SDA | D14 = PB9 (CN5, second pin) | |
| SDO | GND | address 0x76 |
| CS | unconnected | high = I2C mode |

`i2c_config.toml`: every passthrough handler on, every elder handler off, `remaining_space` passthrough on. Every MMIO access is forwarded to the real MCU over ST-Link; with the sensor wired correctly the physical LD2 blinks briefly at boot, then toggles slowly once per measurement cycle; a wiring error shows as fast continuous blinking.

```bash
tests/idle_firmwares/flash_scripts/flash.sh tests/idle_firmwares/prebuilt/idlefirmware_f103.axf
rm -f io.log
fastdyn run -c boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/I2C/i2c_config.toml
# ~10 s, Ctrl-C
cp io.log boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/I2C/hardware_log/io.trace
grep -c "address = 0x40005410" io.log   # > 20
grep -c "address = 0x40010004" io.log   # 2 (AFIO MAPR remap)
```
