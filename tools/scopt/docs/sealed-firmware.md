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
`[1.0096, 1.0654]` (medians and interval only; no per-block rows archived); the difference between that mechanism result and the net
gain is container/activation overhead.  All runs reproduced the sixteen
reported registers and neither cache changed.  Preparation, optimization,
deployment, launch compilation, and process setup are outside `[BENCH][fw]`.

Implementation, commands, raw rows and limitations are in
[`sealed-whopr-7.md`](sealed-whopr-7.md).

## Why sealed regressed

A re-analysis of the archived 2026-11 campaigns from raw per-block data found four
causes behind the sealed configurations (6-9) measuring slower than ordinary scopt (5)
or than plain sealed (6). The exhaustive all-helper configuration 7 (0.844x) is a
separate case: executing per-site private helper copies
([sealed-whopr-7.md](sealed-whopr-7.md#current-all-helper-runtime-result)).

1. **No B3 on the sealed baseline.** Configuration 6 is a separate exhaustive,
   icount-off, exact-PC capture without scopt's guarded chaining (`chain-static`,
   `chain-returns`, `specialize-returns`), and configurations 7-9 inherit it, because
   firmware regions could not carry B3. Sealed runs made 20.77M `lookup_tb_ptr`
   lookups against scopt's 4.39M on `-O4` packet, and about 18M more on `-Os`. The sealed/scopt
   time ratio is 1.076-1.166 in 12 of 13 `-Os` panels with every block agreeing. This
   is confounded with the execution contract (scopt runs `-icount shift=0`, sealed runs
   icount off); on `-O0` packet the ratio is 0.976, sealed faster.
   *Change:* `scopt profile run --sealed --trusted` gives B3 a steady profile on the
   sealed capture, so configuration 6 + B3 runs the unchanged `b3` pipeline, and QEMU
   activates the B3 extension on sealed lookups (`no-sealed-sc6` restores the old
   behaviour on the same bytes). The `sealed-*-b3` pipelines compose B3 with the
   trusted regions in one v7 artifact ([B3 passes](b3-passes.md#sealed-captures-configuration-6--b3)).
2. **Unchainable residual exits.** A trusted region had one shared descriptor, QEMU
   cleared `last_tb` after it and never chained from it, so every residual exit
   returned to the dispatcher: about 2.9M per packet run, nearly all from the root
   continuation that the region never expands. The packet PGO arm, whose public code is
   byte-identical to sealed, measured PGO / sealed 1.031x [1.020, 1.053] (6 of 6
   blocks; 1.028x [1.008, 1.050], 10 of 12, pooled with a second campaign).
   *Change:* trusted regions get one descriptor per member, and QEMU chains their
   residual `goto_tb` exits after first use (`no-trusted-exit-chain` restores the old
   behaviour). In composed artifacts a member whose source B3 grew skips the slots B3
   registered (member skip flags) when the target QEMU is allowlisted with
   `--v7-member-skip-exe`; otherwise its region keeps the shared descriptor
   (`b3_registered_slot`; [B3 passes](b3-passes.md#sealed-captures-configuration-6--b3)).
   `loop_returns_nonnull` and `trusted_residual_exits` now count these returns
   ([counters](qemu.md#v7-dispatcher-return-counters)).
3. **No cost model.** `hot-loop` admitted every root above 1,000 training calls, such
   as the per-byte CRC-CCITT callee, whose saving per call (polls, two lookups, a
   guarded return) is smaller than one dispatcher round trip per unchained exit.
   *Change:* the [profit model](firmware-inline-policies.md#profit-model) prices each
   planned region on the training profile and refuses net losses; it is on by default
   for `hot-loop`, and automatic PGO training keeps the steady phase.
4. **Noise and protocol.** Most other reported regressions are within an in-harness
   floor of about ±2% or underpowered: several panels had 3-4 blocks, and 3 blocks
   cannot reach a sign-test p below 0.25. Sealed-pipeline runs drifted -3.2% to +2.7%
   per input between campaigns of the same artifact. The 4-6% identical-artifact
   differences cited earlier came from other setups (Rosetta, stock QEMU) and do not
   set this harness's floor.
   *Change:* the paired drivers (`compare_rtos.py`, hot-loop `measure.py`,
   `measure_sealed_whopr.py`, `measure_firmware_inline_ablations.py` and Trebuchet's
   `compare.py`) now run an automatic A/A arm, refuse fewer than 8 blocks without
   `--allow-few-blocks`, and report the paired block-ratio median with
   a bootstrap interval, sign test and a `within_noise` verdict against the A/A band
   (`scopt.run.paired_stats`).

### Measured effect (2026-11-25)

Packet, UART and CLI (FirmBOLT `-O4` images) were captured again on QEMU `a1bdec6`
and timed with `compare_rtos.py`: 10 paired fresh-process blocks per workload with an
A/A arm. The A/A bands were about ±5-6% on this shared host, and byte-identical copies
differed by up to about 3%, so no effect under about 3% is claimed. Ratios are
time(configuration 6) / time(arm); above 1 means the arm is faster. Every row in the
table is above its A/A floor.

| Arm | packet | UART | CLI |
|---|---:|---:|---:|
| 6 + B3 (`b3` on a sealed steady profile) | 1.151 | 1.189 | 1.225 |
| `sealed-pgo-7` | 1.116 | 1.160 | 1.152 |
| `sealed-pgo-7-b3` (composed) | 1.153 | 1.228 | 1.259 |

- The new `sealed-pgo-7` against the archived artifact on the old binary: 1.110,
  1.175 and 1.121. The binary change alone is neutral (old 6 / new 6: 0.996, 1.006,
  1.001). The old 7-vs-6 regression did not reproduce in this session (old 6 / old 7:
  1.016, 1.002, 1.039, within noise).
- 6 + B3 against configuration 5: 1.124, 1.203 and 1.276, so the sealed baseline
  now beats ordinary scopt. The B3 gain comes from the `chain-returns` and
  `chain-static` slots in the cache bytes: the same bytes with `no-sealed-sc6` run as
  fast (0.981, 0.996, 1.006). Sealed activation is still needed so TLB and MPU
  changes unlink the guarded slots.
- Chainable residual exits (`sealed-pgo-7` against the same bytes with
  `no-trusted-exit-chain`): 1.063, 1.040 and 0.990, within noise. They remove about
  2.96M dispatcher returns per packet run and 1.08M per UART run. Most remaining
  residual exits on UART and CLI enter another trusted region, which QEMU does not
  chain.
- The profit model, recursive nesting and steady training (against the same pipeline
  with `cost_model = "off"`, `nested = "public"` and whole-run training): 1.026, 1.051
  and 1.075, within the A/A band. On the CLI 6,000-command input alone it is 1.088,
  beyond that input's band. That control still has the `exit_null` loop discovery, so
  it is not the archived configuration 7.
- Composition against 6 + B3: 1.013, 1.023 and 1.020, within noise. Composition is
  non-additive because regions and B3 remove the same lookups: 74% of B3's converted
  lookups on UART and 87% on CLI lie on edges the composed regions make internal.
  Unchained residual exits were not the cause: the same counter change was timed as a
  null (time with `no-trusted-exit-chain` / time chained on the same bytes: 0.990 for
  CLI `sealed-pgo-7`, 0.997 for UART `sealed-firmlooper-v2`, both within the A/A band).
  The composed regions of these artifacts kept the shared descriptor. Member skip flags
  now give them member descriptors on a QEMU that honours the flags; that has not been
  timed.
- `sealed-firmlooper-v2` selects no call on the packet and CLI `-O4` images: every
  in-loop call is an RTOS boundary or the task loop is not closed. On UART it is
  within noise of configuration 6.

Plans, observations and per-arm counters are in
`/data/scfix-campaign/{packet,uart,cli}/timing-1`.

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
  code mutation.  Public TBs keep their interrupt poll unless the explicit unsafe
  `QEMU_SC_SEALED_SKIP_IRQ=1` option is used; trusted firmware regions
  (configuration 7) skip it at entry and on internal edges.  Use ordinary SuperCache when the
  omitted contracts matter.
- The cache is sparse and has a large logical size because it retains the
  existing executable arena layout.  Allocated disk bytes are much smaller;
  compacting this representation is independent of runtime semantics.
