# Table 6 — BoardRunner vs naive baselines (NUCLEO-F103RB)

Unique basic blocks executed by each SDK firmware under four peripheral-modeling modes, plus whether the firmware passes its functional check (`FC`) and whether the model output matches the recorded hardware trace (`TV`).

**Without hardware** you can reproduce the Null, Replay and BoardRunner columns for all five firmwares from the shipped tapes and models. **With a NUCLEO-F103RB** (and a BME280 for SPI/I2C) you can additionally reproduce the Passthrough column and re-record the tapes. `configs/` holds the per-mode platform configs, `tapes/` the recordings replay consumes, and `results/` the block lists behind every number below, for reference.

## How the count is taken, and why it differs from the paper

`coverage = true` in the platform config makes the FastDyn plugin log the address of every translated block it executes; the tracer thread inserts novel addresses into a set that is written to `<work_dir>/bbl.txt` on exit. Blocks executed inside an interrupt handler are not counted. The number in the table is the number of lines in that file.

The current counter excludes interrupt-handler blocks, which the counter used for the paper included, so the absolute counts below sit a few blocks away from the paper's. What the table shows — Passthrough, Replay and BoardRunner executing the same block set, Null a strict subset, only BoardRunner passing FC and TV — reproduces exactly; `results/<fw>/<mode>.txt` holds the block lists so the sets can be compared, not just the counts.

## Results

Paper (Table 6):

| Firmware   | Passthrough | Null | Replay | BoardRunner |
| ---------- | ----------- | ---- | ------ | ----------- |
| GPIO (cbk) | 191         | 101  | 191    | 191         |
| UART       | 415         | 352  | 415    | 415         |
| I2C        | 438         | 304  | 438    | 438         |
| SPI        | 291         | 262  | 291    | 291         |
| ADC+DMA    | 378         | 265  | 378    | 378         |
| **FC**     | ✓           | ✗    | ✗      | ✓           |
| **TV**     | ✓           | ✗    | ✓      | ✓           |

Current numbers:

| Firmware   | Passthrough | Null | Replay | BoardRunner |
| ---------- | ----------- | ---- | ------ | ----------- |
| GPIO (cbk) | 198         | 99   | 198    | 198         |
| UART       | 413         | 350  | 413    | 413         |
| I2C        | 436         | 302  | 436    | 436         |
| SPI        | 289         | 260  | 289    | 289         |
| ADC+DMA    | 337         | 266  | 337    | 337         |
| **FC**     | ✓           | ✗    | ✗      | ✓           |
| **TV**     | ✓           | ✗    | ✓      | ✓           |

## Modes and hardware needs

| Mode        | Board?  | What it is                                                                                         |
| ----------- | ------- | -------------------------------------------------------------------------------------------------- |
| passthrough | **yes** | every MMIO access forwarded to the chip; the reference measurement                                 |
| null        | no      | target peripheral returns 0 on read and drops writes (`classic` backend);                          |
| replay      | no      | every access served from `tapes/<fw>/record.trace`, an icount-stamped recording taken on the board |
| boardrunner | no      | the generated models (`../Nucleo-F103RB/<example>/generated_models`)                               |

Three of the four columns need no hardware. Passthrough (and `record`, which produces the tapes) needs a NUCLEO-F103RB running the idle firmware, and a BME280 breakout for SPI and I2C — wiring and flashing are in the examples' "Hardware trace / passthrough" sections.

## Setup

From the FastDyn repo root with `fastdyn` on `PATH`. The FastDyn plugin must be built with the fuzzer module enabled — the block counter lives in that module and without it `coverage = true` produces no `bbl.txt`:

```bash
make LIBHW=true LIBGZ=false DEV=true PHY=false FLIGHT_CONTROLLERS=false DEBUG_PRINT=true BOARD_RUNNER=true LIBFUZZ=true
```

`run_table6.sh <fw> <mode> [seconds]` measures one cell: it starts QEMU with a dedicated work dir, drives the firmware's input where it has one, stops after `seconds` (default 10) and prints the block count. For `null` and `boardrunner` it copies the firmware's models (`../Nucleo-F103RB/<example>/generated_models/*.c`) into `boardrunner/boardrunner_sdk/model/` and compiles them; nothing to prepare by hand. Counts saturate within the first second of execution, so run length does not matter beyond that.

```bash
T=boardrunner/boardrunner_examples/full_rehost_firmware/table6
```

## GPIO

```bash
$T/run_table6.sh gpio boardrunner   # 198 — presses the emulated button through the FIFO
$T/run_table6.sh gpio null          # 99
$T/run_table6.sh gpio replay        # 198
$T/run_table6.sh gpio passthrough   # 198 — HARDWARE: board with idle firmware; press the blue button during the run
```

## UART

The BoardRunner run needs the serial PTY pair before it starts (the script types the 10 characters into it):

```bash
socat PTY,link=/tmp/usart2_pty,raw,echo=0 PTY,link=/tmp/host_usart2,raw,echo=0 &
$T/run_table6.sh uart boardrunner   # 413
$T/run_table6.sh uart null          # 350
$T/run_table6.sh uart replay        # 413
$T/run_table6.sh uart passthrough   # 413 — HARDWARE: board with idle firmware; the script sends the characters over /dev/ttyACM0
```

## I2C

```bash
$T/run_table6.sh i2c boardrunner    # 436
$T/run_table6.sh i2c null           # 302
$T/run_table6.sh i2c replay         # 436
$T/run_table6.sh i2c passthrough    # 436 — HARDWARE: board with idle firmware + BME280 on D14/D15 (I2C wiring)
```

## SPI

```bash
$T/run_table6.sh spi boardrunner    # 289
$T/run_table6.sh spi null           # 260
$T/run_table6.sh spi replay         # 289
$T/run_table6.sh spi passthrough    # 289 — HARDWARE: board with idle firmware + BME280 on PB12-PB15 (SPI wiring)
```

## ADC+DMA

```bash
$T/run_table6.sh adc boardrunner    # 337
$T/run_table6.sh adc null           # 266
$T/run_table6.sh adc replay         # 337
$T/run_table6.sh adc passthrough    # 337 — HARDWARE: board with idle firmware
```

## Re-recording a tape (hardware)

`run_table6.sh <fw> record` runs the firmware on the board in record mode and leaves the icount-stamped trace in its work dir; copy it over `tapes/<fw>/record.trace` to replay it.

## Files

```
table6/
├── run_table6.sh
├── configs/<fw>/{passthrough,null,record,replay}.toml   (boardrunner = the example's own config)
├── tapes/<fw>/record.trace                              recording consumed by replay
└── results/<fw>/<mode>.txt                              our measurements (one block address per line)
```

`<fw>` ∈ `gpio`, `uart`, `i2c`, `spi`, `adc`.
