# Generic fuzzing schema

This directory implements FastDyn's JSON-driven generic fuzzing schema. A
machine TOML configuration supplies its path through the `fuzzing_schema`
setting. The schema is parsed before execution rules are installed; malformed
or unsupported input fails schema loading rather than being partially applied.

An example demonstrating as many use cases as possible can be seen in `SCHEMA.json`

The schema has four independent top-level sections:

```json
{
  "flow": { "...": "..." },
  "fields": [ "..." ],
  "streams": [ "..." ],
  "hooks": [ "..." ]
}
```

Only these keys are accepted, and each may occur at most once. At least one of
`fields` or `streams` is required. Unknown keys and duplicate properties are
rejected.

## Directory map

| File | Responsibility |
| --- | --- |
| `schema.h` | Shared data structures and the public boundary between the schema modules and the generic fuzzer. `TypeHandler` is the extension point for new types. |
| `schema.c` | Reads JSON, parses top-level fields, owns loaded schema state, validates cross-section references, and starts the per-input injection lifecycle. |
| `expression.c` | Recursive-descent parser for field and stream destination expressions. It reads target registers and memory through the fuzz API. |
| `fields.c` | Takes the fuzzer input, assigns fixed-field input ranges, materializes field values once per iteration, and writes normal fields to guest registers or memory. |
| `types.c` | Type-handler registry and implementations for `int`, `uint`, `float`, `data`, `length`, and `checksum`. |
| `streams.c` | Parses streams, resolves their ordered field-name lists, assigns their shared input suffix, emits configurable chunks per hook visit, and keeps emitted-byte history for derived types. |
| `hooks.c` | Parses and installs injection hooks, binds hook names to top-level fields or streams, and dispatches hook callbacks. |
| `flow.c` | Parses and installs the optional state/snapshot/synchronization loop. |
| `meson.build` | Adds this directory's implementation files to the FastDyn build. |

## Lifecycle and input layout

At the first snapshot event in an iteration, the fuzzer input is acquired.
Direct fields (those with `location`) reserve their expected input bytes in
array order. Fields that are not bound to a hook are injected at that snapshot;
hooked fields are injected at their hook instead. A direct field is written at
most once per iteration.

Streams use the remaining, unreserved suffix. All streams share that suffix in
their actual hook-execution order. An input shorter than a requested field or
stream range is zero-padded. Constants and computed values reserve no input
unless their own type explicitly says otherwise.

## `flow`

`flow` is optional. When present it must contain `snap` and `sync`, and may
also contain `state`:

```json
"flow": {
  "state": { "at": "0x08001000" },
  "snap":  { "at": "0x08002000" },
  "sync": [
    { "at": "0x08002100", "resume": "0x08002000" },
    { "at": "0x08002120", "resume": "0x08002000" }
  ]
}
```

`at` accepts an unsigned JSON number or a decimal/`0x`-prefixed string.
`state` captures a longer-lived initialization state. `snap` captures the
per-input state. `sync` is a non-empty array; every entry ends processing for
the current input and writes its required `resume` address to the program
counter before the next iteration.

Flow and hook addresses must all be distinct.

## Top-level `fields`

Each top-level field requires `name`, `type`, and `size`; `location` and
`options` are optional:

```json
{
  "name": "field_name",
  "location": "r2 + 4",
  "type": "uint",
  "size": 2,
  "options": { "...": "..." }
}
```

`name` is unique among top-level fields and streams. `size` is a non-negative
integer number of output bytes. A field with `location` is a direct field:
the location is evaluated while the snapshot is active and must resolve to a
register destination (`reg(N)`) or memory address. A direct field with no hook
is injected at `flow.snap`; without `flow`, it uses the generic callback's
existing snapshot lifecycle.

A field without `location` is stream-only and must appear in the `fields`
list of at least one stream. It has the exact same type-handler behavior and
per-iteration materialization as a direct field; only its delivery mechanism
is different.

### Location expressions

The location grammar is intentionally an address-expression grammar. Apart
from `reg(N)`, every successful expression becomes a memory address:

```text
location   := reg(N) | expression
expression := term { ('+' | '-') term }
term       := primary { ('*' | '/') primary }
primary    := number | rN | '[' expression ']' | uBITS '[' expression ']'
            | '(' expression ')'
```

Numbers may be decimal or `0x` hexadecimal. Whitespace is ignored.

| Form | Meaning |
| --- | --- |
| `reg(3)` | Write the field value to register 3. This is the only register destination form. |
| `r3` | Read register 3 and use its current value as a memory address. |
| `r3 + 4` | Write at the memory address held in `r3`, plus four. |
| `[r3 + 4]` | Read a little-endian 32-bit pointer at `r3 + 4` and use that pointer as the next address value. |
| `u16[r3 + 4]` | Read a little-endian unsigned 16-bit value at `r3 + 4` and use it as the next address value. |
| `[[r3 + 4] + 8]` | Follow two levels of pointers. |

`uBITS[...]` supports 1 through 64 bits. Reads round up to a whole number of
bytes and clear unused high bits. Ordinary `[...]` is a 32-bit pointer read.
Expression nesting is limited to 64 levels. Stream destinations use the same
grammar, but are evaluated at every stream hook visit so `r1`, for example,
can track the receive buffer selected by the current UART read call.

## Types

Every type has a handler with four conceptual operations: parse its options,
resolve references after the whole schema has loaded, report its expected input
size, and generate its output bytes. The current registry is:

| Type | Output | Expected fuzz bytes |
| --- | --- | --- |
| `int` | Signed integer bytes. | `size`, or 0 with `constant`. |
| `uint` | Unsigned integer bytes. | `size`, or 0 with `constant`. |
| `float` | Floating-point bytes. | `size`, or 0 with `constant`. |
| `data` | Arbitrary byte array. | `size`, or 0 with `constant`. |
| `length` | Computed or conditionally fuzzed byte count. | 0, or `size + 1` when `fuzzable` is true. |
| `checksum` | CRC-16/MODBUS or CRC-16/MCRF4XX. | 0. |

Without options, `int`, `uint`, `float`, and `data` copy their next `size`
fuzz-input bytes unchanged. The distinction is retained for schema analysis;
the unconstrained writer does not convert numeric byte order or normalize host
values.

### `constant` on raw types

`int`, `uint`, `float`, and `data` accept exactly one raw-type option:

```json
"options": { "constant": value }
```

The constant produces fixed output and consumes no fuzz input.

| Type | `constant` value | Encoding and restrictions |
| --- | --- | --- |
| `uint` | Non-negative integer JSON number. | Must fit `size`; encoded little-endian and zero-extended. |
| `int` | Integer JSON number. | Must fit signed `size`; encoded little-endian and sign-extended. |
| `float` | JSON number. | `size` must be 2, 4, or 8; encoded as little-endian IEEE-754 binary16, binary32, or binary64. |
| `data` | Array of byte numbers. | The array must contain exactly `size` integers from 0 through 255. |

For example, a fixed Marvelmind prefix can be expressed as:

```json
{ "name": "header", "type": "data", "size": 2,
  "options": { "constant": [255, 71] } }
```

### `length`

`length` requires `fields` and `byte_order`:

```json
{
  "name": "payload_size",
  "type": "length",
  "size": 2,
  "options": {
    "fields": ["payload"],
    "byte_order": "little",
    "fuzzable": true
  }
}
```

- `fields` is a non-empty ordered array of names. The output size of every
  referenced field or finite stream is summed. All field definitions are
  top-level, so this works the same for direct fields and stream-only fields.
- `byte_order` is required and is either `"little"` or `"big"`.
- `fuzzable` is optional and defaults to `false`.

When non-fuzzable, the generated value is always the computed count. When
fuzzable, the field consumes `size + 1` input bytes. Its first byte is a
selector: a value below 128 writes the following `size` fuzz bytes unchanged;
a value of 128 or higher writes the computed count. The full `size + 1` bytes
are consumed in either case so later input offsets remain stable.

An unbounded raw stream cannot be used as a `length` target because it has no
fixed total size.

### `checksum`

The supported algorithms are `"crc16-modbus"` and `"crc16-mcrf4xx"`.
`crc16-mcrf4xx` is the MAVLink CRC accumulator (initial value `0xffff`, no
final XOR). For MAVLink frames, include the dialect-specific `CRC_EXTRA` in
the covered bytes separately when constructing the complete frame checksum.

For example, CRC-16/MODBUS can be specified as:

```json
{
  "name": "frame_crc",
  "type": "checksum",
  "size": 2,
  "options": {
    "algorithm": "crc16-modbus",
    "over": ["uart_rx"],
    "byte_order": "little"
  }
}
```

`size` must be 2. `algorithm`, non-empty `over`, and `byte_order` are required.
`over` is ordered: fields contribute their materialized output bytes and
streams contribute the bytes they have emitted so far in this iteration. A
checksum field normally names its enclosing stream and must appear after the
bytes it covers. `byte_order` is `"little"` or `"big"` for the two generated
CRC bytes.

## `streams`

A stream models an input source consumed at each hook visit. It emits one byte
per visit by default; set `chunk_size` to emit a larger chunk:

```json
{
  "name": "uart_rx",
  "location": "r1",
  "chunk_size": 1,
  "fields": ["header", "payload", "crc"]
}
```

`name` and `location` are required. Every stream must be named by exactly one
`inject` hook. `chunk_size` is optional, defaults to `1`, and must be a
positive integer no greater than `INT_MAX`. A hook writes the next chunk to the
location; after the stream's data is exhausted, the remaining bytes are zero.

An optional `eof_value` changes that exhausted-stream behavior for a register
destination: instead of zero-padding the next hook visit, FastDyn writes the
specified signed-32-bit or unsigned-32-bit value to the entire register. It
does not add that sentinel to the stream's emitted-byte history. This models
byte-at-a-time APIs that report an empty queue with a wider return value:

```json
{ "name": "serial_read", "location": "reg(0)", "eof_value": -1 }
```

The option applies to both raw and finite streams. It is intentionally a
register-return policy; memory streams retain their byte-oriented zero padding.

For memory destinations, the chunk is written contiguously from the resolved
address. For `reg(N)` destinations, up to the first four bytes are packed
little-endian into the 32-bit register; use a memory destination for chunks
larger than four bytes.

Without `fields`, a stream is unbounded and emits successive bytes from the
shared input suffix. With `fields`, it emits the named top-level field
definitions as a finite sequence. `fields` is a non-empty ordered array of
unique field names. Each referenced field is materialized using its ordinary
type handler; the stream only controls the destination, chunking, and
ordering. A finite stream's declared size is the sum of its referenced field
sizes, including constants and computed bytes, so a `length` field can
describe it.

Stream-only fields reserve their expected fuzz bytes the first time they are
needed; constants and computed fields reserve none. A direct field reused by
a stream retains its normal fixed-prefix input range and emits the same
materialized bytes in both places. A named field may be reused by multiple
streams; it is still materialized only once per iteration.

This example produces a framed stream with a fixed prefix, fuzzed payload, and
computed checksum. The fields are defined once, independently of their stream
delivery:

```json
{
  "fields": [
    { "name": "header", "type": "data", "size": 2,
      "options": { "constant": [255, 71] } },
    { "name": "payload", "type": "data", "size": 16 },
    { "name": "payload_size", "type": "length", "size": 1,
      "options": { "fields": ["payload"], "byte_order": "little" } },
    { "name": "crc", "type": "checksum", "size": 2,
      "options": {
        "algorithm": "crc16-modbus", "over": ["uart_rx"],
        "byte_order": "little"
      } }
  ],
  "streams": [
    { "name": "uart_rx", "location": "r1",
      "fields": ["header", "payload_size", "payload", "crc"] }
  ]
}
```

For a real wire format, place generated length fields at their protocol byte
position in the stream's `fields` array.

## `hooks`

Hooks place normal fields or stream bytes at code locations outside the
snapshot point:

```json
"hooks": [
  {
    "at": "0x08002110",
    "fields": ["mode", "uart_rx"]
  }
]
```

`at` has the same numeric syntax as a flow address. `fields` is a non-empty
array of top-level field and/or stream
names; despite its historical name, it may contain streams. A normal field or
stream can appear in exactly one hook. Hook addresses must be unique and may
not overlap flow addresses. Stream-only fields have no guest location and
cannot be hooked directly; the stream is their injection route.

## Adding a type

Add a `TypeHandler` implementation in `types.c` (or move it to a dedicated
module as the registry grows), then register its string name in
`schema_find_type_handler`. Implement option parsing, any cross-reference
finalization, expected-input reporting, generation, and option cleanup. Keep
generation deterministic for a field within an iteration: fields are
materialized once so dependent `length` and `checksum` handlers observe the
same bytes that are injected or emitted.
