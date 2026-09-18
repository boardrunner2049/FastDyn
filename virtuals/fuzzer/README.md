# FastDyn Fuzzing Implementation

This is the directory for the current fuzzing implementation, which will be updated as we test the fuzzer on more interesting/complicated firmwares, and add/update the backends.

## Overview

To start, this is the guide for manually setting up the fuzzer to run.

fuzz.c is the core of the fuzzer, which is meant to act as a generic interface between the fuzzing backend and the model/firmware that is acting as the fuzzing harness. Currently, we support libAFL and a modified version of AFLNet as the backends for this fuzzer, which can be compiled in in the Makefile depending on the usecase. libAFL is good for producing a generic, single chunk per iteration, and is easier to get going on a new target. AFLNet is better for stateful protocols with a trace of messages, but requires a bit more work for a new protocol, which involves adding in support for that protocol into the modified AFLNet. We have currently added Ethernet and Modbus support in AFLNet which can be used as a reference along with the existing protocols

To use the fuzzers, the path to the built fuzzing backend must be included in the relevant environment variables along with other FastDyn requirements, with the following as a reference at the time of writing:
```sh
export LD_LIBRARY_PATH=/path/to/FastDyn/build:/path/to/FastDyn/device_models/postmartem/verifier:/path/to/FastDyn/virtuals/fuzzer/fastdyn_fuzz_lib/target/release
export PATH=/path/to/qemu/build:$PATH
export PATH=$PATH:/path/to/aflnet
```

## Reproduce the coverage targets

The target-specific reproduction inputs live in
[`reproducibility/`](reproducibility/): saved models, TOMLs, and AFLNet seed
corpora.  The launchers below compile each model directly into the new result
directory, so do not add a target to `boardrunner_sdk/CMakeLists.txt`.  Build
the normal FastDyn/VIO variant once for the board and QEMU runs:

```sh
cd FastDyn
ninja -C build
cmake -S boardrunner/boardrunner_sdk -B boardrunner/boardrunner_sdk/build
cmake --build boardrunner/boardrunner_sdk/build
```

Build the AFLNet variant for the fuzzing runs as described in the AFLNet build
section below. The scripts also find sibling `qemu/build` and `aflnet`
directories automatically.

### Hardware preparation for passthrough runs

Only the MQTT, lwIP, and SD-card **board** launchers use ST-LINK passthrough.
Before any of those runs, flash an idle image to the real MCU—not the demo
firmware.  QEMU executes the demo firmware; the attached MCU must remain idle
so the two CPUs do not contend for the same peripherals.  One flashed F769
idle image covers both the lwIP and SD-card runs.

| Board run | Attached board | Idle image included in this tree | Optional source directory |
| --- | --- | --- | --- |
| MQTT | NUCLEO-H753ZI | `tests/idle_firmwares/prebuilt/idlefirmware_h7.axf` | `tests/idle_firmwares/NucleoH753zi/` |
| lwIP and SD-card | STM32F769I-EVAL | `tests/idle_firmwares/prebuilt/idleloop_firmware_f4_f7.axf` | `tests/idle_firmwares/STM32F769i_eval/` |

If building an image, follow the toolchain setup in
[`tests/idle_firmwares/README.md`](../../tests/idle_firmwares/README.md),
configure the selected board's `Makefile` paths, and build its
`build/idlefirmware.axf`. Flash it with STM32CubeProgrammer or OpenOCD using
the configuration for the **actual attached board**. Do not run
`tests/idle_firmwares/flash_scripts/flash.sh` unchanged: it currently selects
an STM32F469 Discovery OpenOCD configuration and is not a safe generic
flasher for the H753 or F769. Reconnect/reset the board after a successful
flash, then confirm that both the ST-LINK debug probe and its VCP are usable by
the current user. The launchers auto-detect a sole VCP, or accept
`--board-port /dev/ttyACM<N>`.

For an OpenOCD installation with the standard target scripts, the following
are the corresponding one-time flash commands (run from `FastDyn`). Replace a
generic `interface/stlink.cfg` plus `target/...` pair with a board-specific
OpenOCD board config if your installation provides one:

```sh
# NUCLEO-H753ZI, used by the MQTT board run
openocd -f interface/stlink.cfg -f target/stm32h7x.cfg \
  -c 'program tests/idle_firmwares/prebuilt/idlefirmware_h7.axf verify reset exit'

# STM32F769I-EVAL, used by both lwIP and SD-card board runs
openocd -f interface/stlink.cfg -f target/stm32f7x.cfg \
  -c 'program tests/idle_firmwares/prebuilt/idleloop_firmware_f4_f7.axf verify reset exit'
```

The reproduced Modbus board launcher is PTY/Elder-only and needs no board or
idle image.  If separately following the original F103 passthrough-capture
guide, first flash `tests/idle_firmwares/prebuilt/idleloop_firmware_f1.axf`
and follow its documented reset sequence.  AFLNet and QEMU-only runs never
need a physical board.

For the host-side portions, install `mosquitto`, `mosquitto-clients`, and
`socat` for MQTT; `iproute2`, `iputils-ping`, `netcat`, and `curl` for lwIP;
and `dosfstools` (`mkfs.fat`) for SD-card. The launchers check these commands
before starting. The lwIP `tap0` commands and MQTT broker behavior are given
in the target notes below.

Run these from `FastDyn`.  Every fuzzing output directory must be new.

```sh
# Stateful fuzzing: no board is required.
./virtuals/fuzzer/run_aflnet_coverage.sh lwip   /absolute/results/lwip-aflnet
./virtuals/fuzzer/run_aflnet_coverage.sh mqtt   /absolute/results/mqtt-aflnet
./virtuals/fuzzer/run_aflnet_coverage.sh modbus /absolute/results/modbus-aflnet

# Non-fuzzing Elder/passthrough baselines.
# MQTT needs the H753 board and a local Mosquitto installation.
./virtuals/fuzzer/run_board_coverage.sh mqtt --board-port /dev/ttyACM0
# lwIP and SD-card need the F769 board; lwIP also needs configured tap0.
./virtuals/fuzzer/run_board_coverage.sh lwip   --board-port /dev/ttyACM0
./virtuals/fuzzer/run_board_coverage.sh sdcard --board-port /dev/ttyACM0
# Modbus is an Elder/PTTY run and needs no physical board.
./virtuals/fuzzer/run_board_coverage.sh modbus

# Cold QEMU-only baselines: no models, board, or passthrough.
./virtuals/fuzzer/run_qemu_coverage.sh all
```

`--board-port` can be omitted when exactly one ST-LINK VCP is connected.  The
lwIP `tap0` setup and the MQTT broker details are given in their short target
notes below.  Each launcher writes and prints a `bbl.txt`; its final `SIGINT`
is the script's intentional wall-clock cutoff, not an unexpected fuzzer
timeout.

The following are reference unique-basic-block counts for the checked-in
models and corpora. AFLNet entries show the measured default-budget result
followed by a modest longer-campaign planning target. Non-fuzzing values are
the measured baseline runs. Small timing variation is normal for the fuzzing
and hardware-assisted rows; the QEMU-only values are the measured cold-start
baselines.

| Launcher | Target | Reference BBL count |
| --- | --- | ---: |
| `run_aflnet_coverage.sh` | lwIP | 2,587 measured; ~2,600 longer-run target |
|  | MQTT | 902 measured; ~920 longer-run target |
|  | Modbus | 611 measured; ~620 longer-run target |
| `run_board_coverage.sh` | MQTT | 869 |
|  | lwIP | 1,814 |
|  | SD-card | 1,393 |
|  | Modbus | 521 |
| `run_qemu_coverage.sh` | MQTT / lwIP / SD-card / Modbus | 196 / 124 / 103 / 123 |

For a portable package-local entry point, use the equivalent commands in
[`reproducibility/README.md`](reproducibility/README.md).

## libAFL

### Build

To build the libAFL implementation, cd to the root folder of the project, and run the docker script with the following command, with elevated permissions if necessary:
```sh
./virtuals/fuzzer/fastdyn_fuzz_lib/run_docker.sh 
```

Once in the docker container, build the release version with the following command
```sh
cargo build --release
```
If you wish to compile with a different mode, make sure that the relevant path is updated in LD_LIBRARY_PATH

Then, in the Makefile, make sure to set the relevant flag
```Makefile
LIBFUZZ		 ?= true
```

to use the libAFL backend, along with enabling
```toml
coverage = true
```
in the relevant .toml configuration file

## AFLNet

As mentioned, we have a custom implementation of AFLNet. This implementation can be accessed at https://anonymous.4open.science/r/aflnet/README.md

### Build

To build the AFLNet implementation, cd into the root of the downloaded AFLNet backend used, and build with
```sh
make clean libaflnet.a
```

Then, in the Makefile, make sure to set the relevant flag
```Makefile
AFLNET 		 ?= true
```


### FastDyn stateful targets

The reproducible target assets are deliberately self-contained under
[`reproducibility/`](reproducibility/): models, TOML templates, saved corpora,
and package-local launchers live there.  The compatibility launchers in this
directory resolve every target-specific asset from that bundle and compile the
needed model directly for the new run.  Consequently, target reproduction does
not require adding a model target to `boardrunner_sdk/CMakeLists.txt`.

The FastDyn bridge sends one AFLNet request as one `fuzz_get_data()` message,
collects every `fuzz_set_data()` response before the next sync, and restores the
firmware snapshot after the complete testcase. A seed file is therefore a raw,
concatenated sequence of protocol messages; it does not contain an execution
trace or schema encoding.

These checked-in target setups use the modeled device boundary and require no
hardware or passthrough fallback:

| Target | AFLNet `-P` selector | AFLNet-only config | Initial corpus |
| --- | --- | --- | --- |
| lwIP Ethernet server | `ETHERNET` | `reproducibility/configs/aflnet/lwip.toml` | `reproducibility/seeds/lwip/` |
| MQTT client over USART3 | `MQTT_CLIENT` | `reproducibility/configs/aflnet/mqtt.toml` | `reproducibility/seeds/mqtt/` |
| FreeMODBUS RTU slave | `MODBUS` | `reproducibility/configs/aflnet/modbus.toml` | `reproducibility/seeds/modbus/` |

`ETHERNET` splits raw Ethernet frames using ARP or IPv4 lengths and extracts
ARP, TCP, UDP, ICMP, and other IPv4 response states. `MQTT_CLIENT` has a
varint-aware MQTT splitter and treats emitted client packet types as states;
this is distinct from ordinary `MQTT`, where a network server emits broker
responses. `MODBUS` splits CRC-bearing RTU request frames and derives states
from normal, exception, malformed, and bad-CRC responses.

The launchers load these saved raw corpus files directly; they do not generate
seeds while starting a campaign. lwIP keeps its baseline and complementary
`all`, `cgi`, `rto`, `ack`, and `reassembly` phase corpora under its directory,
as well as the retained TCP-state, edge, and timer subsets used to maintain the
broad corpus.

Build one backend at a time.  The launchers compile and link their bundled
models on demand, so no target-specific CMake build targets are needed:

```sh
(cd aflnet && make libaflnet.a)
cd FastDyn
meson setup build-aflnet --reconfigure \
  -Denable_aflnet=true -Denable_libfuzz=false \
  -Daflnet_path=../aflnet
ninja -C build-aflnet
cd ..
cmake -S FastDyn/boardrunner/boardrunner_sdk \
  -B FastDyn/boardrunner/boardrunner_sdk/build-aflnet
cmake --build FastDyn/boardrunner/boardrunner_sdk/build-aflnet
```

`run_aflnet_coverage.sh` selects the protocol, model, and BBL-only timeout
values for every target. It requires a new output directory. MQTT and Modbus
start one campaign; lwIP runs six complementary clean-start phases that append
to one `bbl.txt` file. That preserves setup and teardown coverage together with
normal HTTP, CGI, TCP timer/retransmit, response-acknowledgement, and IPv4
reassembly lifecycle paths.

The launcher is non-interactive: it selects every writable firmware segment
for the snapshot through `FASTDYN_SNAPSHOT_SEGMENTS=all`, so a first run cannot
spend its fuzzing timeout at BoardRunner's range-selection prompt. Override it
with an explicit index expression (for example `FASTDYN_SNAPSHOT_SEGMENTS=0,2-3`)
only when deliberately narrowing snapshot state.

```sh
cd FastDyn
./virtuals/fuzzer/run_aflnet_coverage.sh lwip /absolute/results/lwip
wc -l /absolute/results/lwip/bbl.txt
./virtuals/fuzzer/run_aflnet_coverage.sh mqtt /absolute/results/mqtt
./virtuals/fuzzer/run_aflnet_coverage.sh modbus /absolute/results/modbus
```

MQTT runs for 25 seconds and Modbus for 100 seconds by default. The launcher
then sends QEMU an expected `SIGINT`, prints that this is its own wall-clock
cutoff rather than an AFLNet testcase timeout, and reports the final BBL count.
Set `FASTDYN_AFLNET_RUN_SECONDS` to use another positive duration; do not wrap
these two commands in a second `timeout` command. If an older wrapper does
interrupt the launcher, it reports any BBL file QEMU has already persisted
before exiting.

The lwIP campaign defaults to 55 seconds for the baseline phase, 100 seconds
for the broad corpus, and 60 seconds for each focused phase. It exits nonzero
when the final union is not over 2,500 BBLs. To extend a run without mixing
reports, choose a new output directory and set
`FASTDYN_AFLNET_MAIN_SECONDS`, `FASTDYN_AFLNET_PHASE_SECONDS`, or
`FASTDYN_AFLNET_BASELINE_SECONDS`. The timer/retransmit phase automatically
uses a 30-second guest tick while all other phases use the 1-second tick.

The script accepts `FASTDYN_BOARDRUNNER` when BoardRunner is not activated in
`PATH`. It automatically finds sibling `qemu/build` and `aflnet` directories.
It selects `ETHERNET`, `MQTT_CLIENT`, and `MODBUS` respectively. Set
`FASTDYN_AFLNET_CORPUS` to an alternate seed directory to replace MQTT or
Modbus's saved corpus, or lwIP's baseline phase corpus. The MQTT
model enters fuzz input mode when `FASTDYN_AFLNET_PROTOCOL=MQTT_CLIENT`; it
uses `fastdyn_work/model-snap.bin` when available but does not require that
older baseline for an AFLNet campaign.

### Non-fuzzing Modbus RTU coverage

`run_board_coverage.sh modbus` reproduces the Nucleo-F103RB FreeMODBUS Elder
workflow without AFLNet or a physical board. It builds the unchanged original
BoardRunner USART2 model, creates `/tmp/usart1_pty` and `/tmp/host_modbus`,
then runs the documented host bridge against the model. Its generated config
re-pends USART2 after the firmware consumes `DR`; this run-only virtual lets
the original model's PTY FIFO progress through an RTU frame without changing
the model or firmware. The bridge's `core` profile exercises FC01/02/03/04/05/06/0F
and deliberately leaves out FC16, the application-state sequence, and the
intentional exception request. This reports approximately 521 basic blocks in
the verified run.

```sh
cd FastDyn
./virtuals/fuzzer/run_board_coverage.sh modbus
```

The final QEMU `SIGINT` is the launcher's expected wall-clock cutoff. Pass a
new work directory or a longer budget when needed:

```sh
./virtuals/fuzzer/run_board_coverage.sh modbus \
  --duration 25 --work-dir /absolute/results/f103-modbus-elder
```

### Hardware-assisted, non-fuzzing MQTT coverage

`run_board_coverage.sh` is separate from the AFLNet launcher. It restores the
documented NUCLEO-H753ZI MQTT elder-model flow with coverage enabled but no
fuzzing: the saved USART3 model connects through a PTY and local Mosquitto,
while all other peripherals use the attached board through ST-LINK passthrough.
It publishes `on`, `off`, `on`, `off` to `stm32/cmd`, subscribes to
`stm32/sensor`, and reports `bbl.txt` after its own expected timeout.

```sh
cd FastDyn
./virtuals/fuzzer/run_board_coverage.sh mqtt
```

The script detects a unique ST-LINK VCP through `/dev/serial/by-id`. If more
than one is available, require an explicit board selection:

```sh
./virtuals/fuzzer/run_board_coverage.sh mqtt \
  --board-port /dev/ttyACM1 \
  --work-dir /absolute/results/h753-mqtt-board
```

The VCP identifies the board, but MQTT elder mode uses the model PTY
(`/tmp/usart1_pty`) rather than that serial device; BoardRunner's ST-LINK
backend performs hardware passthrough. Install `mosquitto`,
`mosquitto-clients`, `socat`, a C compiler, and GLib development headers.
Use `--duration` (minimum 15 seconds), `--broker-host`, or `--broker-port` to
adapt the local environment.

### Hardware-assisted, non-fuzzing lwIP coverage

The same launcher also runs the STM32F769I-EVAL lwIP HTTP-server demonstration
without fuzzing. It builds the supplied TAP-capable Ethernet Elder model for
the run and sends Ethernet traffic through `tap0`; all non-Ethernet
peripherals remain on ST-LINK passthrough. The fixed, non-mutating traffic
sequence is one ICMP echo, a TCP connection to port 80, `GET /index.html`,
`GET /STM32F7xxLED.html`, and a missing-page request to cover the HTTP 404
path. It does not invoke `/leds.cgi`.

Create `tap0` once using the example's documented network:

```sh
sudo ip tuntap add dev tap0 mode tap user "$(whoami)"
sudo ip addr add 192.168.8.1/24 dev tap0
sudo ip link set dev tap0 up
```

Then run:

```sh
cd FastDyn
./virtuals/fuzzer/run_board_coverage.sh lwip
```

If more than one ST-LINK VCP is attached, select the F769 explicitly. The
default 65-second run produces approximately 1,800 blocks (1,814 in the
verified run); a small timing variation is normal.

```sh
./virtuals/fuzzer/run_board_coverage.sh lwip \
  --board-port /dev/ttyACM1 \
  --work-dir /absolute/results/f769-lwip-board
```

### Hardware-assisted, non-fuzzing SD-card coverage

The STM32F769I-EVAL FatFs demo is also available through the same launcher. It
creates a fresh isolated 64 MiB FAT32 image, builds the saved SDMMC1 Elder
model, and routes all non-SDMMC1 peripherals to ST-LINK passthrough. The
normal demonstration creates, writes, and reads `STM32.TXT`; the launcher
verifies that its expected contents were written to the image before reporting
coverage. It does not stimulate SD-card insertion/removal interrupts.

```sh
cd FastDyn
./virtuals/fuzzer/run_board_coverage.sh sdcard \
  --board-port /dev/ttyACM1 \
  --work-dir /absolute/results/f769-sdcard-board
```

The default 45-second run reached 1,393 blocks in the verified baseline run.
The work directory retains the card image and logs; the temporary `sdcard.img`
link required by the supplied model is removed automatically when the run ends.

### QEMU-only, no-model coverage baseline

`run_qemu_coverage.sh` is the third reproducibility launcher. It generates a
minimal TOML for MQTT, lwIP, SD-card, and Modbus, then runs each firmware in
QEMU and records its unique basic blocks. The TOML contains the firmware,
Cortex-M CPU, coverage setting, and required RAM mapping—no peripheral models
or handlers. FastDyn is loaded solely as the coverage collector.

```sh
cd FastDyn
./virtuals/fuzzer/run_qemu_coverage.sh
```

The default is a one-second, launcher-owned wall-clock ceiling per image and
writes one `bbl.txt` per target plus `summary.tsv` beneath a new
`qemu-runs/qemu-coverage-*` directory.  A timeout is expected: it is the
launcher's final `SIGINT`, not an AFLNet testcase timeout.  To inspect one
firmware or adjust the common budget:

```sh
./virtuals/fuzzer/run_qemu_coverage.sh modbus --duration 20 \
  --work-dir /absolute/results/modbus-qemu-only
```

This is a cold-start comparison baseline, not an application exercise.  With
no device emulation or incoming traffic, its count should not be compared
directly to the modeled, hardware-assisted, or fuzzing coverage numbers.

## Generic Usage

The generic fuzzer obtains one input for each iteration and writes contiguous
parts of that input to the fields in a JSON schema. It is configured entirely
from the target TOML and does not need a target-specific injection callback.

Currently this supports libAFL

### Fuzzing Virtuals

The generic loop uses three lifecycle points:

- `fuzz_state_point` some firmware may have a long startup or a difficult to
  reach fuzzing target. In these cases, the state point is meant to be a
  more complete version of the snapshot that allows a fresh run to be started
  at a chosen point. It simply needs a previous run to have reached that point
  for the snapshot to be taken, subsequent runs can then begin there, skipping
  initialization. May not be necessary on all targets.
- `fuzz_snap_point` captures the per-input snapshot on its first visit. On
  every visit it restores the snapshot as necessary, obtains the next input,
  and invokes the generic schema writer.
- `fuzz_sync_point` marks the end of processing for one input. It records
  coverage, requests the next input, and restores the snapshot for the next
  iteration.

Choose a snap point immediately before the code that consumes the fuzzed data,
and one or more sync points after that code has completed. Put those points in
the schema `flow` object. Each `sync` array entry's `resume` redirects execution
for the next iteration; the redirected instruction must be safe to skip.
Function epilogues are a common choice when the redirect updates `r15` before
the epilogue executes.

```toml
[Machine]
coverage = true
fuzzing = true
fuzzing_schema = "path/to/schema.json"
```

```json
{
  "flow": {
    "state": { "at": "0x08001000" },
    "snap":  { "at": "0x08002000" },
    "sync":  [{ "at": "0x08002080", "resume": "0x08002000" }]
  },
  "fields": []
}
```

The state point is normally reached once. The snap and sync points then form
the persistent loop: **snap → inject → target code → sync → snap**. A schema
without `flow` remains compatible with the existing TOML virtuals and
modifiers, which is useful while migrating a target.

### Schema

`fuzzing_schema` names a JSON object. Its independent top-level sections are
`flow`, `fields`, `streams`, and `hooks`. `flow` owns the state/snap/sync
lifecycle; `hooks` owns non-snap injection events. Each field requires `name`,
`type`, and `size`; `location` is optional. A locationless field is delivered
by a stream. Types may also define an optional `options` object. Each type
reports how many fuzz-input bytes it expects. Direct fields reserve those bytes
in array order; stream-only fields reserve from the shared stream suffix when
first emitted. For `int`,
`uint`, `float`, and `data`, the expectation is `size` bytes.

```json
{
  "fields": [
    {
      "name": "message",
      "location": "r2",
      "type": "data",
      "size": 291
    },
    {
      "name": "mode",
      "location": "reg(0)",
      "type": "uint",
      "size": 4
    }
  ]
}
```

The supported types are `int`, `uint`, `float`, `data`, `length`, and
`checksum`:

| Type | Meaning |
| --- | --- |
| `int` | Signed integer represented by `size` fuzzed bytes. |
| `uint` | Unsigned integer represented by `size` fuzzed bytes. |
| `float` | Floating-point value represented by `size` fuzzed bytes. |
| `data` | Byte array of `size` fuzzed bytes. |
| `length` | Encodes the combined size of named fields. It is computed by default and can optionally be fuzzed. |
| `checksum` | A computed checksum over named fields and/or bytes already emitted by named streams. |

The first four types copy their next `size` input bytes unchanged. Direct
fields write those bytes to their resolved location; streams emit them one at a
time. Their distinction is retained in the loaded schema for type-aware
analysis; no host-endian conversion or numeric normalization is performed by
the generic writer.

`int`, `uint`, `float`, and `data` also accept `options.constant`. A constant
does not consume fuzz input, which makes it particularly useful for stream
headers and fixed protocol values. Integer constants are JSON numbers encoded
little-endian into `size` bytes; signed constants are sign-extended. Float
constants are JSON numbers encoded as IEEE-754 binary16, binary32, or binary64
when `size` is 2, 4, or 8 respectively. A `data` constant is an exact array of
`size` byte values:

```json
{
  "fields": [
    { "name": "preamble", "type": "uint", "size": 1,
      "options": { "constant": 255 } },
    { "name": "message_id", "type": "uint", "size": 2,
      "options": { "constant": 1 } },
    { "name": "magic", "type": "data", "size": 2,
      "options": { "constant": [255, 71] } }
  ]
}
```

A `length` field requires `fields` and `byte_order` in `options`. `fields` is
an array of names whose declared output sizes are added together. A `length`
field can name defined fields and/or finite streams. An unbounded raw stream
cannot be used because it has no known length. `byte_order` is either
`"little"` or `"big"`. The optional
boolean `fuzzable` defaults to `false`:

```json
{
  "name": "payload_length",
  "location": "r2 + 4",
  "type": "length",
  "size": 2,
  "options": {
    "fields": ["header", "payload"],
    "byte_order": "little",
    "fuzzable": true
  }
}
```

With `fuzzable: false`, `length` consumes no fuzz input and always writes the
computed value. With `fuzzable: true`, it always consumes `size + 1` bytes:
the first byte selects the behavior, and the remaining `size` bytes reserve
the fuzzed value. A selector below `128` writes the fuzzed value; a selector
of `128` or greater discards those reserved bytes and writes the computed
value instead. Reserving the bytes in both cases keeps the input layout stable.

`checksum` supports the standard `crc16-modbus` and `crc16-mcrf4xx`
algorithms. `crc16-mcrf4xx` is MAVLink's CRC accumulator: it starts at
`0xffff` and has no final XOR. It has a fixed `size` of two bytes and consumes
no fuzz input. Its `over` array is an ordered list of field or stream names;
fields contribute their generated bytes and a stream contributes the bytes it
has already emitted in this iteration. `byte_order` controls the two emitted
checksum bytes. MAVLink's dialect-specific `CRC_EXTRA` must be included among
the covered bytes separately when constructing a complete MAVLink frame.

```json
{
  "name": "frame_crc",
  "location": "r3",
  "type": "checksum",
  "size": 2,
  "options": {
    "algorithm": "crc16-modbus",
    "over": ["header", "payload", "uart_rx"],
    "byte_order": "little"
  }
}
```

The checksum is calculated from exactly the ordered bytes named in `over`; it
does not reread mutable guest memory. A stream reference can therefore only
cover bytes emitted before the checksum itself is generated.

Field locations are evaluated once, when the schema first reaches the snapshot
point. This intentionally freezes register-derived addresses and pointer
chains for the campaign. Stream locations use the same grammar but are
evaluated at each delivery hook, allowing a stream to target a current output
pointer such as `r1` inside a receive loop.

| Location | Meaning |
| --- | --- |
| `reg(1)` | Fuzz register `r1` itself. This form is valid only as the complete location. |
| `0x20001000` | Fuzz memory at an absolute address. |
| `r2` | Read `r2` and fuzz memory at the address it contains. |
| `r2 + 0x10` | Fuzz memory 16 bytes into the buffer addressed by `r2`. |
| `[r3]` | Read an unsigned 32-bit value from memory at `r3`; fuzz memory at the resulting address. |
| `u16[r3 + 6]` | Read a 16-bit unsigned value from `r3 + 6`; use it as the destination address. |
| `[[r0 + 0x20] + 0x8] + 0x10` | Follow a pointer at `r0 + 0x20`, then a pointer at offset `0x8` in that object, and fuzz 16 bytes into the final object. |

Memory expressions support `+`, `-`, `*`, `/`, parentheses, and nested
dereferences. A bare bracket dereference reads 32 bits; `uN[expression]`
reads an unsigned `N`-bit value. Non-byte-aligned widths are rounded up to a
byte read and masked to `N` bits. For example, if `r4` points to a connection,
the following resolves a three-step object chain before the bytes are written:

```json
{
  "name": "payload",
  "location": "[[[r4 + 0x14] + 0x8] + 0x20]",
  "type": "data",
  "size": 64
}
```

Here the parser reads a pointer from `r4 + 0x14`, follows a second pointer at
offset `0x8`, then follows a third pointer at offset `0x20`. Use parentheses
when they make a more complex arithmetic expression clearer.

#### Hooks and streams

Fields not named by a hook are written at `flow.snap`. A field named by one
`inject` hook is written when that hook executes instead; a normal field is
written only once per iteration even if its instruction is revisited.

Streams model input that is consumed one byte at a time, such as a UART receive
routine. A stream has a destination `location`, while its hook says when to
deliver its next byte. Fixed fields keep their deterministic input prefix; the
stream consumes one byte at a time from the true, unreserved suffix of the
fuzzer input. Multiple stream deliveries share that suffix in execution order.
After the suffix is exhausted, the stream writes a zero byte (an explicit EOF
policy can be added later without changing locations).

A stream without `fields` remains an unbounded raw stream. A stream with
`fields` emits one finite sequence of named field definitions, then zero
padding. This is useful for a single framed message: raw fields consume fuzz
input, while derived fields such as `checksum` consume none and can refer to
the stream's preceding output.

```json
{
  "fields": [
    { "name": "frame", "type": "data", "size": 21 },
    {
      "name": "crc", "type": "checksum", "size": 2,
      "options": {
        "algorithm": "crc16-modbus",
        "over": ["uart_rx"],
        "byte_order": "little"
      }
    }
  ],
  "streams": [
    { "name": "uart_rx", "location": "r1",
      "fields": ["frame", "crc"] }
  ]
}
```

Here `uart_rx` emits 21 fuzzed bytes followed by the CRC-16/MODBUS of those
21 bytes. The stream history is reset at the beginning of every fuzz iteration.

```json
{
  "flow": {
    "snap": { "at": "0x08002000" },
    "sync": [{ "at": "0x08002080", "resume": "0x08002000" }]
  },
  "fields": [
    { "name": "mode", "location": "reg(0)", "type": "uint", "size": 1 }
  ],
  "streams": [
    { "name": "uart_rx", "location": "reg(1)" }
  ],
  "hooks": [
    { "at": "0x08002110", "fields": ["uart_rx"] }
  ]
}
```

In this example `mode` is injected at snap, then every visit to `0x08002110`
places the next stream byte in `r1`. The `fields` name in a hook is retained
for one uniform list: it can contain either ordinary field names or stream
names.
