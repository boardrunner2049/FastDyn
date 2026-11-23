# Sealed whole-firmware SuperCache

Sealed mode is an explicit user-trust runtime contract for immutable Cortex-M
firmware.  It is not a semantic strengthening of ordinary SuperCache.  The
producer translates every Thumb halfword in every executable ELF `PT_LOAD`
range, then runs the training input to append ordinary multi-instruction hot
TBs.  Replay prefers the hot duplicate and retains the one-instruction record
as exhaustive fallback coverage.

The loader uses an exact-PC direct index.  A missing translation key is fatal;
sealed replay never compiles a replacement TB or appends to the cache.  The
loader also omits the generic per-TB interrupt poll, guest-byte hash check and
QEMU in-TB fault-recovery contract.  It preserves target-specific TB-start
operations such as ARM IT-state updates.  For multi-instruction hot TBs it
forces `can_do_io=true`, so a fault or MMIO access that required rewind is not
recoverable.  The user must verify immutable firmware and acceptable device,
fault and interrupt behavior externally.

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
verified cache contained all 36,796 exhaustive fallback entries, 1,314
multi-instruction hot records and 38,297 records total.  Replay produced the
same sixteen reported registers and left the cache SHA-256 unchanged.

Three alternating Python-free process blocks measured:

| block | ordinary warm firmware time | sealed firmware time |
| ---: | ---: | ---: |
| 1 | 1.380841 s | 1.375849 s |
| 2 | 1.474994 s | 1.312220 s |
| 3 | 1.394446 s | 1.414818 s |
| median | **1.394446 s** | **1.375849 s** |

The median ratio is 1.0135x in favor of sealed mode.  Three blocks are a smoke
measurement, not enough evidence for a default performance claim.  A first
version that replayed only one-instruction coverage records took 1.881734 s.
The regression was caused by fragmenting hot translations into one TB per
guest instruction; retaining ordinary hot TBs reduced dynamic chain installs
from 10,299 to 1,342 in diagnostic runs.

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
- The mode intentionally does not support QEMU fault recovery, icount, guest
  code mutation, or per-TB interrupt polling.  Use ordinary SuperCache when
  those contracts matter.
- The cache is sparse and has a large logical size because it retains the
  existing executable arena layout.  Allocated disk bytes are much smaller;
  compacting this representation is independent of runtime semantics.
