# Firmware-inline evaluation

`firmware-inline@1` is active and behavior-correct on the bounded FreeRTOS
`RTOSDemo.axf` boot workload, but its exhaustive policy is a measured regression
for that workload.  It remains opt-in.

The exact run used FastDyn `dedef1a`, QEMU `3927bf2`, QEMU executable SHA-256
`e34cdf7c...a284ba1`, and firmware SHA-256 `53ca19e3...4f0a04`.  The v7 loader
accepted artifact id `98d429cdc7827bce`; replay appended zero translations and
the v7 export/reimport/load proof passed.

## Activation and size

The pass discovered 30 verified Thumb call sites, emitted 27 regions at maximum
acyclic expansion depth 5, and reported four pending sites (one missing callee
capture and three missing continuation captures).  The artifact has 164 private
members and 131 internal edges.  During the measured run, 18 region entries
instantiated 118 member descriptors, registered only the 18 composite entries,
and admitted 95 internal edges.  Exact member recovery used the copied search
streams 119 times.

Private native code is 71,064 bytes versus 63,524 bytes for the copied source
TB bodies.  The 7,540-byte difference is return-context guards and ordinary
lookup fallbacks.  Total private-region arena footprint is 102,100 bytes.

## Native before and after

At guest call site `0x08003982 -> 0x0800398e`, the public caller exits through
the ordinary TB path:

```text
7fff6c0149a3: e9 00000000  jmp 7fff6c0149a8
7fff6c0149a8: lea rax,[rip-0xef]
7fff6c0149af: jmp 7fff6c000018
```

The private copy directly enters the copied callee:

```text
7fff6c027b73: e9 18000000  jmp 7fff6c027b90
```

The copied callee keeps guest return effects, checks full return context and PC,
then enters the copied continuation.  A mismatch uses the original lookup path
without replaying the return effects:

```text
7fff6c027c21: cmp dword ptr [rbp+0x3c],0x8003986
7fff6c027c2b: je  7fff6c027c36
7fff6c027c37: jmp 7fff6c027c4c        # embedded continuation
7fff6c027c41: mov rdi,rbp
7fff6c027c44: call qword ptr [rip-0x7a]
7fff6c027c4a: jmp rax                 # fallback
```

Run `scripts/inspect_firmware_inline.py` to reproduce complete public/private
disassembly and size accounting from an artifact.

## Timing result and diagnosis

Twelve randomized paired process blocks were pinned to CPU 0.  The measured
window is the firmware's bounded boot-to-scheduler interval:

| Candidate | Median speed versus ordinary warm | Paired bootstrap 95% interval |
| --- | ---: | ---: |
| current scopt | 0.991x | 0.979-1.017x |
| firmware-inline | 0.747x | 0.733-0.761x |

Against current scopt directly, firmware-inline measured 0.746x
`[0.739, 0.756]`.  This is a regression, not an inconclusive result.

The mechanism did run.  Composite registration reduced QEMU's host-PC tree
insertions from 118 member TBs to 18 entry TBs.  A live, fail-closed FastDyn
predicate then proved 116 of the 118 translation-callback replays inert; only
two instrumented members replayed and produced two callbacks.  Relative to the
correct composite implementation before that predicate, median candidate time
fell from about 327 us to 265 us and relative performance improved from 0.586x
to 0.747x.  This removes the specific redundant post-JIT callback work, but not
the regression.

The exhaustive implementation still validates and constructs all 118 member
descriptors before entering their 18 regions and adds 7,540 native bytes of
guards/fallbacks.  On this roughly 0.2 ms workload, those one-time costs
dominate; ordinary QEMU already chains many public TB edges, so removing the
remaining dispatches does not repay the setup.  A source-dependency cache was
also tested: it removed 40 repeated hashes but regressed candidate time from
about 265 us to 308 us (0.634x versus ordinary warm), so it was rejected and
removed.  This result does not establish a steady-state regression because the
firmware stops at scheduler entry and has no repeated steady measurement
window.

The machine-readable raw samples and identities are in
`fixtures/firmware-inline/measurement.json`.

## Reproduction skeleton

```sh
QEMU=$PWD/third_party/common/qemu/build/qemu-system-arm
QEMU_SHA=$(sha256sum "$QEMU" | cut -d' ' -f1)
PYTHONPATH=tools/scopt python3 -m scopt prepare -c "$CONFIG" \
  --out "$WORK/prep" --qemu "$QEMU" --json
# Generate bundle/runtime identity, import with the exact firmware ELF, and
# produce EnvLayout for this exact QEMU as described in docs/getting-started.md.
SCOPT_ENVLAYOUT_PATH="$WORK/envlayouts" PYTHONPATH=tools/scopt \
  python3 -m scopt optimize "$SNAPSHOT" --pipeline firmware-inline \
    --prep "$WORK/prep" --elf "$FIRMWARE" --runtime-identity "$RUNTIME" \
    --v7-exe "$QEMU_SHA" --qemu "$QEMU" --warm-baseline "$SNAPSHOT" \
    --store "$WORK/store" --json
PYTHONPATH=tools/scopt python3 -m scopt rundir launch "$RUN_DIR" \
  --v7-stats --json
taskset -c 0 env PYTHONPATH=tools/scopt \
  python3 tools/scopt/scripts/measure_firmware_inline.py \
    --config "$CONFIG" --qemu "$QEMU" --warm "$SNAPSHOT" \
    --current "$CURRENT_ARTIFACT" --firmware-inline "$V7_ARTIFACT" \
    --out "$WORK/timing" --pairs 12 --seed 98912
```

## Remaining limitations

The current policy is exhaustive only; it does not yet provide lazy descriptor
construction or a conditional selection policy.  The real-application result
is boot-only, not steady-state.  Structural loader mutation checks, local semantic
fixtures, export/reimport identity, exact output replay, and code-dependency
tracking are covered; destructive runtime tests for every MMIO, interrupt,
fault, callback, and invalidation combination remain bounded rather than a
proof over all firmware behavior.
