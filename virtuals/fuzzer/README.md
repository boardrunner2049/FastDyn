# FastDyn Fuzzing Implementation

This is the directory for the current fuzzing implementation, which will be updated as we test the fuzzer on more interesting/complicated firmwares, and add/update the backends.

## Overview

To start, this is the guide for manually setting up the fuzzer to run.

fuzz.c is the core of the fuzzer, which is meant to act as a generic interface between the fuzzing backend and the model/firmware that is acting as the fuzzing harness. Currently, we support libAFL and a modified version of AFLNet as the backends for this fuzzer, which can be compiled in in the Makefile depending on the usecase. libAFL is good for producing a generic, single chunk per iteration, and is easier to get going on a new target. AFLNet is better for stateful protocols with a trace of messages, but requires a bit more work for a new protocol, which involves adding in support for that protocol into the modified AFLNet. We have currently added Ethernet and Modbus support in AFLNet which can be used as a reference along with the existing protocols

To use the fuzzers, the path to the built fuzzing backend must be included in the relevant environment variables along with other FastDyn requirements, with the following as a reference at the time of writing:
```sh
export LD_LIBRARY_PATH=/path/to/FastDyn/build:/path/to/FastDyn/virtuals/fuzzer/fastdyn_fuzz_lib/target/release
export PATH=/path/to/qemu/build:$PATH
export PATH=$PATH:/path/to/aflnet
```

## Reproducing the paper's coverage results

The fuzzing and coverage experiments of the BoardRunner paper (Table 8: the
lwIP, MQTT, SD-card and Modbus targets, with their models, configurations,
AFLNet seed corpora, launchers and reference basic-block counts) are in the
[BoardRunner](https://github.com/boardrunner2049/BoardRunner) repository, under
`evaluation/fuzzing/`; its `README.md` and `EVALUATION.md` describe the runs.

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


### Stateful targets

The FastDyn bridge sends one AFLNet request as one `fuzz_get_data()` message,
collects every `fuzz_set_data()` response before the next sync, and restores the
firmware snapshot after the complete testcase. A seed file is therefore a raw,
concatenated sequence of protocol messages; it does not contain an execution
trace or schema encoding.

The modified AFLNet adds three protocol selectors (`-P`), which FastDyn reads
from `FASTDYN_AFLNET_PROTOCOL`. Initial seeds come from `FASTDYN_AFLNET_IN_DIR`
(default `aflnet-in/` in the work directory); an existing output directory
(`FASTDYN_AFLNET_OUT_DIR`, default `aflnet-out/`) resumes the campaign instead.
`ETHERNET` splits raw Ethernet frames using ARP or IPv4 lengths and extracts
ARP, TCP, UDP, ICMP, and other IPv4 response states. `MQTT_CLIENT` has a
varint-aware MQTT splitter and treats emitted client packet types as states;
this is distinct from ordinary `MQTT`, where a network server emits broker
responses. `MODBUS` splits CRC-bearing RTU request frames and derives states
from normal, exception, malformed, and bad-CRC responses.

Build one backend at a time. The AFLNet variant of FastDyn:

```sh
(cd aflnet && make libaflnet.a)
cd FastDyn
meson setup build-aflnet --reconfigure \
  -Denable_aflnet=true -Denable_libfuzz=false \
  -Daflnet_path=../aflnet
ninja -C build-aflnet
```

A snapshot covers the firmware's writable segments. FastDyn asks which ones to
include; unattended runs include all of them, and `FASTDYN_SNAPSHOT_SEGMENTS`
(`all`, `none`, or an index expression such as `0,2-3`) selects them without
asking.

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

`state` and `snap` points may also specify `resume`. This is useful for a
state-only trampoline located immediately before the real fuzz boundary: the
state is restored at the trampoline and execution is redirected to `snap`, so
the schema injects its fields before the target executes an input-consuming
instruction.

A `state` or `snap` point may additionally specify `return_address`. This
sets the target link register before its optional `resume` redirect. It lets a
schema call a small target accessor and return directly into a normal target
method without embedding an instance pointer in the schema.

For the short setup path before the first snapshot, `flow.setup` accepts fixed
`at`/`register`/`value` writes. It is appropriate for returning a fixed status
and skipping an unavailable target-side call; it is not fuzz input. For
example, a write to register 15 redirects execution after the call while the
other writes supply that call's result.

```toml
[Machine]
coverage = true
fuzzing = true
fuzzing_schema = "path/to/schema.json"
```

```json
{
  "flow": {
    "state": { "at": "0x08001000", "resume": "0x08002000" },
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

Without options, the first four types copy their next `size` input bytes
unchanged. Direct fields write those bytes to their resolved location; streams
emit them one at a time. Their distinction is retained in the loaded schema for type-aware
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

`int` and `uint` can instead use `options.values`, a nonempty list of at most
255 representable integers. It consumes one fuzz-input byte and selects one
list entry by modulo, which is useful for a curated set of protocol message
IDs without allowing arbitrary dispatcher values:

```json
{ "name": "message_id", "type": "uint", "size": 3,
  "options": { "values": [22, 77] } }
```

`int`, `uint`, and `float` also support `options.range`, mutually exclusive
with `constant` and `values`:

```json
{ "name": "temperature", "type": "float", "size": 4,
  "options": { "range": [0, 50] } }
```

This reserves the usual `size` input bytes and interprets them as an unsigned
little-endian integer `x` for every numeric type. It maps the full source range
linearly into the inclusive output range using
`min + x / (2^(8 * size) - 1) * (max - min)`. Integer outputs round to the
nearest integer; floats use ordinary IEEE-754 encoding. Thus four zero bytes
produce float32 `0.0`, four `FF` bytes produce `50.0`, and `00 00 00 80`
produces approximately `25.0`.

Integer widths may be 1–8 bytes; integral bounds must fit the field and the
existing exact JSON-number limit of ±9007199254740991. Float widths are 2, 4,
or 8 bytes, with finite bounds inside the format's numeric limits. Decimal
bounds may round slightly on encoding. Equal bounds still consume `size`
bytes, and short inputs are zero-padded before normalization. See the
[schema reference](schema/README.md#range-on-numeric-types) for the full rules.

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
