# Constant-PC chaining benchmark

Recorded on 2026-11-12 on an Intel Xeon w5-3435X host (Ubuntu kernel
6.17.0-1033-oem), from FastDyn `e22098f` plus the constant-PC change and pinned
QEMU `99b54836` with the five ordered overlays.  The release QEMU build used
GCC 9.5.0 and the ARM32 Cortex-M4 fixture in
`tests/integration/constant_pc/`.

The command was:

```bash
FASTDYN_CONST_PC_ARTIFACTS=/tmp/fastdyn-const-pc-bench-20261112 \
  fastdyn-env/bin/python tests/integration/constant_pc/benchmark.py
```

Each run completed 5,000,000 redirects and the same final
`iterations * 3` checksum.  Five paired runs alternated legacy and optimized
order.  The timing boundary was FastDyn's whole `machine0.qemu_execution`
phase; the long bounded loop includes the first translation equally in both
modes, with identical firmware, modifier, plugin, QEMU binary, CPU model, and
instrumentation.

| Mode | Samples (seconds) | Median | Min–max spread | Redirects/s |
| --- | --- | ---: | ---: | ---: |
| `FASTDYN_CONST_PC_CHAIN=0` | 0.120803, 0.122967, 0.126778, 0.120344, 0.124101 | 0.122967 | 0.006434 | 40.66 M |
| automatic chain | 0.048544, 0.047947, 0.047834, 0.048634, 0.047360 | 0.047947 | 0.001274 | 104.28 M |

The median ratio was 2.565x in this redirect-dominated microbenchmark.  This is
not a general firmware speedup claim.  The structural run recorded a promoted
slot-1 `goto_tb` with its source-TB `exit_tb`, no surviving nested plugin
cleanup marker, and QEMU's `Linking TBs` event.  The normal exit and runtime
PC-relative alias guard remain in the generated block.

As a representative non-microbenchmark check, `configs/minimal.toml` completed
normally and printed its expected `r0 = 3` and benchmark result.  It contains
zero fixed-PC candidates, so zero were promoted and no benefit is claimed for
that configuration.  Translation diagnostics on the focused fixture reported
one promoted static site (translated twice during setup/retranslation).  The
developer-switch comparison reported `disabled`; conservative tests and code
paths retain the principal fallbacks `no-exit-slot`,
`redirect-already-promoted`, `chaining-disabled-or-cross-page`,
`target-state-or-address`, `callback-overlap`, and `legacy-pc-overlap`.
