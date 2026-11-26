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
   *Follow-up (counters revision 4):* QEMU also chains a trusted exit into another
   trusted region's entry. A region entry's `tc.ptr` skips the poll (+15), so the
   chain enters at the entry member's first byte (+0): the destination's IRQ/exit-request
   poll and its `can_do_io` store run on every chained transfer, as when chaining into an
   ordinary TB. Requirements: the region's entry prefix is proved at load (the poll exits
   to the member's own stub, which returns the entry descriptor with
   `TB_EXIT_REQUESTED`, and the store writes 1; a region that fails stays loadable and is
   never such a destination), and the same sealed or `incoming-chain` rule as
   ordinary-to-trusted chains. `QEMU_SC_V7_EXPERIMENT=no-trusted-to-trusted-chain`
   restores the refusal on the same bytes. Pricing is unchanged: the profit model still
   charges such an exit a dispatch, which is conservative now. Timed in
   [round 2](#round-2-follow-ups-2026-11-25), it has no measurable time effect: every
   pair with and without it on the same bytes is within noise, including those where it
   removes almost all residual exits.
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
  residual exits on UART and CLI enter another trusted region, which QEMU `a1bdec6`
  does not chain (counters revision 4 does; within noise in
  [round 2](#round-2-follow-ups-2026-11-25)).
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
  Of the 74 composed regions in these artifacts, all but one (on packet) kept the shared
  descriptor. Member skip flags now give them member descriptors on a QEMU that honours
  the flags; [round 2](#round-2-follow-ups-2026-11-25) timed that within noise.
- `sealed-firmlooper-v2` selects no call on the packet and CLI `-O4` images: every
  in-loop call is an RTOS boundary or the task loop is not closed. On UART it is
  within noise of configuration 6. On packet and CLI, a benchmark task that ends by
  returning from its entry never has a closed loop (CLI console, packet `prvRxTask`);
  GCC word jump tables (packet `prvRxTask`) are unknown exits; and BOLT entry patches
  in `.bolt.org.text` have unknown ownership, so the hot packet call to `prvCrc16Byte`
  through the patch at 0x331c is public. Separately, `Reset_Handler` is seeded as an
  interrupt, so everything reachable from `main` is classified public.
  `sealed-firmlooper-v3` addresses all four on analysis revision 2 and selects 15
  regions on packet (including the four `prvRxTask` CRC calls), 7 on UART and 5 on
  CLI; see [FirmLooper v3](firmware-inline-policies.md#firmlooper-v3).
  [Round 2](#round-2-follow-ups-2026-11-25) timed it within noise of v2 on all three
  images.

Plans, observations and per-arm counters are in
`/data/scfix-campaign/{packet,uart,cli}/timing-1`.

### Round 2: follow-ups (2026-11-25)

The follow-ups were captured again on QEMU `aacca05` (counters revision 4) and timed
with the same driver: 20 paired blocks on packet, 10 on UART and CLI, ratios as above.
The A/A bands are [0.9501, 1.0526] (packet), [0.9700, 1.0309] (UART) and
[0.9802, 1.0202] (CLI). Packet's A/A interval excludes 1 (1.0322 [1.0035, 1.0526])
because the configuration 6 reference ran slow, so every configuration-6-referenced
packet ratio is inflated, by about 1.3-4.1% against its byte-identical twins. The new
binary shows no measurable slowdown: time(round-1 configuration 6 on `a1bdec6`) /
time(configuration 6 on `aacca05`) is 0.9885, 1.0099 and 1.0299, all within noise.

- **Trusted-to-trusted chains** (time with `no-trusted-to-trusted-chain` / time without,
  same bytes): all nine pairs are within noise. Where there is trusted-to-trusted
  traffic, the chains remove almost all residual exits. With `sealed-pgo-7`,
  `cost_model = "off"` and `nested = "public"` they fall from 38,101 to 17 on packet
  (0.9962 [0.9782, 1.0205]) and from 194,859 to 19 per UART commands run
  (0.9981 [0.9755, 1.0154]). The largest point estimate, CLI `sealed-pgo-7`, is
  1.0354 [0.9996, 1.0768] (8 of 10 blocks), and its interval overlaps the band. On
  packet the default `sealed-pgo-7` and `sealed-pgo-7-b3` make no trusted-to-trusted
  attempt.
- **`--pgo-phase auto`** chose `launch_virtual:benchmark_start` on all three workloads
  and trained `sealed-pgo-7` on the same phase as round 1, so no time change is
  attributable to it. Its premise does not hold on packet, where `-O4` left
  `vComputeWindowStart` executed at the marker's address (`not_chosen: same_address`).
  On UART and CLI round 1 named the steady marker explicitly, and its auto build, which
  fell back to `whole_run_unreached`, was byte-identical to the steady-trained one.
- **Member skip flags** (`sealed-pgo-7-b3` without / with `--v7-member-skip-exe`): regions
  with member descriptors go from 1 of 48 to 48 of 48 (packet), 0 of 19 to 19 of 19
  (UART) and 0 of 7 to 7 of 7 (CLI), and residual exits from 10 to 1, from 7,673 to 2
  (UART commands) and from 10,488 to 3 (CLI commands). The time ratios are 1.0131,
  1.0274 and 0.9769, all within noise. With the flags, the composed artifact against
  configuration 6 is 1.1843 (packet; 1.164 against a byte-identical twin), 1.1616 and
  1.2415, above the floor, and against 6 + B3 it is 1.0167, 0.9901 and 1.0148, within
  noise.
- **`sealed-firmlooper-v3`** selects 15, 7 and 5 regions where v2 selects 0, 5 and 0,
  activates 8, 4 and 2 of them, and makes no trusted-to-trusted attempt. time(v2) /
  time(v3) is 1.0621 [1.0405, 1.0773] on packet: 20 of 20 blocks, but within noise,
  because the interval crosses the band edge 1.0526. v2 is byte-identical to
  configuration 6 there and unbiased against its A/A copy. It is 1.0428 on UART and
  1.0331 on CLI, also within noise. The packet estimate cannot come through the
  `prvRxTask` -> `prvCrc16Byte` chain the design expected, and its mechanism is not
  diagnosed. Every point estimate has v3 slower than `sealed-pgo-7`:
  time(`sealed-pgo-7`) / time(v3) is 0.9580 and 0.9505 (within noise) and 0.8736 on CLI
  (below the floor).
- **`sealed-whopr-7`** is a regression: 0.8513, 0.6921 and 0.9109 against configuration
  6, all below the floor (on packet against the slow reference; against its
  byte-identical twins it is 0.826 and 0.831). Keeping `helper_lookup_tb_ptr` out of
  its host-inline prefix removes the regression
  ([`sealed-whopr-7-bounded`](sealed-whopr-7.md#bounded-helper-graphs-sealed-whopr-7-bounded)).
  Timed as a shipped pipeline in a later paired run, `sealed-whopr-7-bounded` is
  1.2576, 1.4003 and 1.3149 times as fast as `sealed-whopr-7`, above the floor, and
  within noise of `sealed-pgo-7` (0.9685, 0.9522 and 1.0230; on UART round 2's 0.9163
  for the same bytes was below the floor).

Plans, observations and per-arm counters are in
`/data/scfix-r2/{packet,uart,cli}/timing-1`, and the full report is
`/data/scfix-r2/TIMING.md`.

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
- Chained transfers between regions poll (counters revision 4), but two poll-free
  paths remain inside the trusted contract (`interrupts="disabled"`): cycles of
  internal edges within one region, and a `goto_ptr` lookup that enters a region entry
  at +15.
- The cache is sparse and has a large logical size because it retains the
  existing executable arena layout.  Allocated disk bytes are much smaller;
  compacting this representation is independent of runtime semantics.
