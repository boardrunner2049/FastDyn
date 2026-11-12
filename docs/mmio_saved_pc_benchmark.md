# MMIO saved-PC cleanup benchmark

The implementation makes this bounded claim: **FastDyn obtains the same saved
PC value with less synchronous work on the vCPU.** The value is not a new
instruction-exact PC facility.

## PC contract

`saved_pc` is the saved PC-register value that the existing FastDyn
register-reading path would return at the beginning of the callback. QEMU reads
it from `first_cpu` on every v2 exporter invocation. A valid zero is represented
by `QEMU_PLUGIN_MMIO_SAVED_PC_VALID`; zero is not an unavailable sentinel.

The direct target hooks preserve stored AArch32 R15, the selected AArch64 PC
register, x86 EIP/RIP GDB-width normalization without `CS.base`, and RISC-V
maximum-XLEN GDB-width normalization.

## Revisions and commands

- FastDyn baseline: `442d1932605eb722274c9a830f54771350a9c56a`.
- Pinned QEMU: `99b54836ded8dc3271eba52deee7453efc0a13b9`.
- Patched source: the same revisions plus the four ordered QEMU patches,
  ending in `patches/qemu-fastdyn-mmio-pc.patch`, and the FastDyn callback
  changes in this working tree/PR.
- Terminal QEMU patch SHA-256:
  `3a6e7c5937a89cba11f1dc99fc7fa0d82ee5381d09b351d0cf03fa3d783e9390`.

Build the maintained target matrix and FastDyn from the same header:

```bash
source ./setup.sh --build-qemu \
  --qemu-targets arm-softmmu,aarch64-softmmu,x86_64-softmmu,riscv32-softmmu,riscv64-softmmu
ninja -C build
```

The baseline plugin was built from the reviewed FastDyn revision's legacy
`dev.c` against the updated QEMU header (the legacy QEMU entry point remains
available). The patched plugin was `build/libfastdyn.so`. Run the paired test:

```bash
FASTDYN_MMIO_PC_BASELINE_PLUGIN=/path/to/legacy/libfastdyn.so \
FASTDYN_MMIO_PC_PATCHED_PLUGIN="$PWD/build/libfastdyn.so" \
FASTDYN_MMIO_PC_ARTIFACTS=/tmp/mmio-pc-results \
  tests/integration/run_fastdyn_mmio_pc.sh
```

The runner disables automatic MMIO substitution, uses the minimal native echo
model, alternates order across five pairs, and checks the firmware result and
ordinary model read/write counts. The timed boundary is the existing
`machine0.qemu_execution` whole-QEMU phase. The fixed million-access loop comes
after setup accesses, so translation is warm; whole-QEMU startup remains in
both measurements. Logging stays enabled and identical.

## Result

Measured on the development host on 2026-11-11:

| Build | Median seconds | Min–max seconds | MMIO accesses/s |
| - | -: | -: | -: |
| Legacy PC path | 0.999059 | 0.966757–1.029570 | 1,000,955 |
| Scalar saved-PC path | 0.894460 | 0.891636–1.026275 | 1,118,007 |

Each run completed 1,000,012 reads and one write and printed the same PASS
result. Median speedup was **1.1169×** (11.69%). No speedup threshold is used as
a test gate.

The ordinary FastDyn v2 callbacks contain no `core_get_pc()` call. Their QEMU
dispatch reads a scalar target hook directly, so the normal path does not
allocate/free a `GByteArray` or call the GDB register reader. The allocating
`core_get_pc()` implementation remains available to unrelated runtime clients.

`tests/integration/run_fastdyn_mmio_pc_equivalence.sh` is the test-only
allocation probe. At callback entry it compares the v2 scalar with the existing
plugin/GDB register read and has passed for Cortex-M, AArch32, AArch64,
x86-16/32/64, RV32, and RV64. Its x86-16 fixture runs with a nonzero `CS.base`.
The Cortex-M fixture requires a valid zero PC, and a stopped-CPU qtest access
confirms the preserved `first_cpu` selection for a non-vCPU caller. The same
probe rejects wrong-version, undersized, and missing-callback descriptors and
confirms that a superseded legacy exporter is not invoked.
