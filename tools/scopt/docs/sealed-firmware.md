# Sealed whole-firmware SuperCache

Sealed mode is an explicit user-trust runtime contract for immutable Cortex-M
firmware.  It is not a semantic strengthening of ordinary SuperCache.  The
producer translates every Thumb halfword in every executable ELF `PT_LOAD`
range, then runs the training input to append ordinary multi-instruction hot
TBs.  Replay prefers the hot duplicate and retains the one-instruction record
as exhaustive fallback coverage.

The loader uses an exact-PC direct index.  A missing translation key is fatal;
sealed replay never compiles a replacement TB or appends to the cache.  It
omits the guest-byte hash check and QEMU in-TB fault-recovery contract while
retaining the normal per-TB interrupt poll by default.  The producer encodes
`can_do_io=true` in captured multi-instruction TBs, so replay does not mutate
every executable page and an access that required rewind is not recoverable.
Target-specific TB-start operations such as ARM IT-state updates remain.
The user must verify immutable firmware and acceptable device and fault
behavior externally.

`QEMU_SC_SEALED_SKIP_IRQ=1` is the separate unsafe interrupt-poll ablation.  It
requires `QEMU_SC_SEALED=1` and replaces the verified 11-byte entry poll with
two architectural NOPs without moving the native entry address.  Use it only
when the firmware contract explicitly excludes interrupt delivery.

## Produce and verify

```sh
PYTHONPATH=tools/scopt scopt-env/bin/python \
  tools/scopt/scripts/prepare_sealed_firmware.py \
  --config compute.toml --out sealed-prep \
  --qemu third_party/common/qemu/build/qemu-system-arm \
  --input final-1.bin --trust-user-no-recovery

PYTHONPATH=tools/scopt scopt-env/bin/python \
  tools/scopt/scripts/verify_sealed_firmware.py \
  --elf firmware.elf --trace sealed-prep.sealed.trace \
  --cache sealed-prep/cache/fw.sc \
  --qemu third_party/common/qemu/build/qemu-system-arm
```

Replay uses the generated FastDyn configuration with `QEMU_SC_SEALED=1`.
`fastdyn compile-launch` can compile that configuration to a shell launcher so
Python is outside the timed path:

```sh
fastdyn compile-launch -c sealed-prep/run.toml -o sealed-launch --no-build-fmu
QEMU_SC_SEALED=1 sealed-launch/run.sh
```

The coverage manifest binds the ELF, QEMU, trace and cache hashes.  It records
the executable ranges, expected halfword entries, verified fallback entries,
total cache records and multi-instruction hot records.  Cache preparation and
launch compilation are setup costs and must not be included in warm runtime.

## Compute result (2026-11-23)

The real FreeRTOS compute firmware had two executable ranges, `0x0-0x5db0`
and `0x10000-0x1c1c8`, containing 36,796 halfword entry positions.  The
verified cache contained all 36,796 exhaustive fallback entries, 1,293
multi-instruction hot records and 38,252 records total.  Replay produced the
same sixteen reported registers and left the cache SHA-256 unchanged.

Six alternating Python-free process blocks were pinned to CPU 2.  Each cell is
only the firmware `[BENCH][fw]` window; cache production, verification, launch
compilation and process setup are excluded.

| block | ordinary warm firmware time | sealed firmware time |
| ---: | ---: | ---: |
| 1 | 1.077002 s | 1.043170 s |
| 2 | 1.095366 s | 1.040172 s |
| 3 | 1.118117 s | 1.046239 s |
| 4 | 1.069556 s | 1.030507 s |
| 5 | 1.131855 s | 1.063000 s |
| 6 | 1.050605 s | 1.040905 s |
| median | **1.086184 s** | **1.042037 s** |

The median paired speedup is **1.0455x**; the paired-bootstrap 95% percentile
interval is **1.0209-1.0667x**.  All six pairs favored sealed replay.  The
cache SHA-256 was `6eb1d216...c41e769` before and after the campaign.

The earlier ~1.44 s sealed regression was a runtime bug, not an inherent cache
cost.  `scopt_sealed_pretranslate()` fetched two producer environment variables
with `getenv()` on every execution-loop iteration.  Producer/control runs found
those variables quickly; replay did two full unsuccessful environment scans.
Caching the configuration on the first call removed that hot-loop work.  A
separate same-artifact diagnostic found no guest-page or byte-hash rejection.

A first version that replayed only one-instruction coverage records took
1.881734 s.  That independent regression came from fragmenting hot translations
into one TB per guest instruction.  Retaining ordinary hot TBs reduced dynamic
chain installs from 10,299 to about 1,340 in diagnostic runs.

## Configuration 7: sealed WHOPR

`sealed-whopr-7` implements the whole-program structure without treating the
exhaustive one-instruction coverage layer as observed control flow.  Its LGEN
stage summarizes verified hot TBs, WPA constructs the firmware call graph and
SCCs and creates balanced partitions, and parallel LTRANS jobs construct the
call-site-specific private regions.  Recursive edges and three native exits
that could not be patched remain ordinary side exits.

The FreeRTOS compute artifact embedded 169 regions, 3,494 copied TB members,
3,994 internal transfers and 1,071,165 private bytes.  Maximum acyclic inline
depth was four.  Export, reimport, source identity, directory, internal-edge,
loader-capability and final-byte validation all passed.

Six alternating compiled-launch process pairs on CPU 2 measured a trusted
sealed baseline median of **1.083271 s** and configuration 7 median of
**1.063933 s**.  The median paired speedup was **1.0187x**, bootstrap 95%
interval **1.0046-1.0292x**, with all six pairs favoring configuration 7.
In a same-artifact control, enabled regions beat `no-regions` by **1.0321x**
`[1.0096, 1.0654]`; the difference between that mechanism result and the net
gain is container/activation overhead.  All runs reproduced the sixteen
reported registers and neither cache changed.  Preparation, optimization,
deployment, launch compilation, and process setup are outside `[BENCH][fw]`.

Implementation, commands, raw rows and limitations are in
[`sealed-whopr-7.md`](sealed-whopr-7.md).

## Remaining limitations

- “Complete” is exact for halfword entries in the declared executable ranges
  and the recorded base translation context.  Training adds encountered IT or
  other contexts.  An unseen context fails closed; it is not silently assumed
  impossible.
- Direct and already-chained transfers avoid JIT and generic hash lookup.
  Indirect transfers and residual exits still consult the exact-PC direct
  index.  Eliminating those lookups requires a separately validated whole-CFG
  private region; coverage alone cannot turn an indirect target into a known
  edge.
- The mode intentionally does not support QEMU fault recovery, icount, or guest
  code mutation.  Interrupt polling remains enabled unless the explicit unsafe
  `QEMU_SC_SEALED_SKIP_IRQ=1` option is used.  Use ordinary SuperCache when the
  omitted contracts matter.
- The cache is sparse and has a large logical size because it retains the
  existing executable arena layout.  Allocated disk bytes are much smaller;
  compacting this representation is independent of runtime semantics.
