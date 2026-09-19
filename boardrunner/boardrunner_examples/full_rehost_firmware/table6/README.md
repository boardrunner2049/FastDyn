# Table 6 — BoardRunner vs naive baselines (NUCLEO-F103RB)

Unique basic blocks executed by each SDK firmware under four peripheral-modeling modes, plus whether the firmware passes its functional check (`FC`) and whether the model output matches the recorded hardware trace (`TV`). Run it with the `pseclab/boardrunner` image, see `../README.md`; `configs/` holds the per-mode platform configs, `tapes/` the recordings replay consumes, and `results/` the block lists behind the numbers below.

## How the count is taken, and why it differs from the paper

`coverage = true` in the platform config makes the FastDyn plugin log the address of every translated block it executes; the tracer thread inserts novel addresses into a set that is written to `<work_dir>/bbl.txt` on exit. The number in the table is the number of lines in that file.

The firmware binaries shipped here were rebuilt from the SDK examples after the paper's measurements with different compilation flags, so the absolute block counts differ from the paper's (GPIO +27, ADC+DMA −8, Null cells ±3; the others are identical). What the table shows reproduces exactly on the shipped binaries: Passthrough, Replay and BoardRunner execute the same block set, Null a strict subset, and only BoardRunner passes both the functional check and trace verification. `results/<fw>/<mode>.txt` holds the block lists so the sets can be compared, not just the counts.

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

Shipped binaries (this artifact; `table6 all` in the image reproduces the Null/Replay/BoardRunner columns, `table6 <fw> passthrough` the first one on a board):

| Firmware   | Passthrough | Null | Replay | BoardRunner |
| ---------- | ----------- | ---- | ------ | ----------- |
| GPIO (cbk) | 218         | 101  | 218    | 218         |
| UART       | 415         | 352  | 415    | 415         |
| I2C        | 438         | 304  | 438    | 438         |
| SPI        | 291         | 262  | 291    | 291         |
| ADC+DMA    | 370         | 268  | 370    | 370         |
| **FC**     | ✓           | ✗    | ✗      | ✓           |
| **TV**     | ✓           | ✗    | ✓      | ✓           |

The block sets, not just the counts, are identical across Passthrough, Replay and BoardRunner (`diff` the `.txt` files). The GPIO passthrough count depends on how many times the button is pressed during the run (the ISR and callback blocks are only reached on a press); the Replay and BoardRunner columns use the same recorded presses / the same 10 scripted presses.

FC is decided per firmware by its Table 7 operation (details in `../README.md`): Null fails all five (no peripheral responds); Replay fails GPIO and UART, the two input-driven firmwares, because it reproduces the recording instead of answering this run's presses/characters — the input-less firmwares (I2C, SPI, ADC+DMA) replay their recording and pass. TV is `verify <fw>` in the image (VIOMapper's Verifier against the recorded hardware trace).

## Modes and hardware needs

| Mode        | Board?  | What it is                                                                                         |
| ----------- | ------- | -------------------------------------------------------------------------------------------------- |
| passthrough | **yes** | every MMIO access forwarded to the chip; the reference measurement                                 |
| null        | no      | target peripheral returns 0 on read and drops writes (`classic` backend);                          |
| replay      | no      | every access served from `tapes/<fw>/record.trace`, an icount-stamped recording taken on the board |
| boardrunner | no      | the generated models (`../Nucleo-F103RB/<example>/generated_models`)                               |

Three of the four columns need no hardware. Passthrough (and `record`, which produces the tapes) needs a NUCLEO-F103RB running the idle firmware, and a BME280 breakout for SPI and I2C — wiring and flashing are in the examples' "Hardware trace / passthrough" sections.

## Commands

`docker run --rm -v "$PWD:/results" pseclab/boardrunner:1.0.0 table6 <fw> <mode> [secs]` with `<fw>` in `gpio uart i2c spi adc` and `<mode>` in `null replay boardrunner` (no hardware) or `passthrough record` (board on USB, see `../README.md`); `table6 all` runs the 15 hardware-free cells and prints the table. Each cell is a fresh QEMU run of `secs` (default 10) seconds; the counts saturate within the first second. Block lists go to `results/table6/<fw>_<mode>.txt`, I/O logs next to them.

## Re-recording a tape (hardware)

`table6 <fw> record` runs the firmware on the board in record mode and leaves the icount-stamped trace in `results/table6/<fw>_record.trace`; copy it over `tapes/<fw>/record.trace` (and rebuild the image, or bind-mount the directory) to replay it.

## Files

```
table6/
├── run_table6.sh                                        the same measurement without Docker (fastdyn CLI on PATH, repo root)
├── configs/<fw>/{passthrough,null,record,replay}.toml   (boardrunner = the example's own config)
├── tapes/<fw>/record.trace                              recording consumed by replay
└── results/<fw>/<mode>.txt                              our measurements (one block address per line)
```

`<fw>` ∈ `gpio`, `uart`, `i2c`, `spi`, `adc`.
