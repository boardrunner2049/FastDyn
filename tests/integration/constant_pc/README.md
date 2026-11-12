# Constant-PC chaining fixture

This fixture exercises the ordinary FastDyn TOML modifier path for ARM32 A32
and Thumb.  The modifier is in the middle of the frontend's original block;
it must skip both the intercepted instruction and the original suffix.  A
finite forward/backward loop checks the iteration count and checksum.

Prerequisites are the ARM embedded binutils (`arm-none-eabi-as` and
`arm-none-eabi-ld`), the patched QEMU ARM system binary, `build/libfastdyn.so`,
and the FastDyn Python environment.  The release matrix used for this fixture
is built from the repository root with:

```bash
source ./setup.sh --build-qemu \
  --qemu-targets arm-softmmu,aarch64-softmmu,x86_64-softmmu,riscv32-softmmu,riscv64-softmmu
```

Run correctness and structural checks:

```bash
tests/integration/run_fastdyn_constant_pc.sh
```

Override paths with `FASTDYN_QEMU_BIN`, `FASTDYN_PLUGIN_LIB`, and
`FASTDYN_BIN`.  The runner performs optimized and
`FASTDYN_CONST_PC_CHAIN=0` legacy runs, dumps final TCG operations, and uses a
short execution trace to require QEMU's `Linking TBs` event.

The deterministic-instruction-count validation uses the same runner and build:

```bash
FASTDYN_QEMU_ICOUNT=shift=0,align=off,sleep=off \
  tests/integration/run_fastdyn_constant_pc.sh
```

For QEMU slot assertions, configure a separate patched tree with
`--target-list=arm-softmmu --enable-plugins --enable-debug-tcg --disable-docs`,
build `qemu-system-arm`, and point `FASTDYN_QEMU_BIN` at that binary when
running the same command.  No timing comparison is made with the debug build.

For the bounded paired benchmark (five alternating pairs by default):

```bash
python tests/integration/constant_pc/benchmark.py
```

Set `FASTDYN_CONST_PC_BENCH_PAIRS` (minimum 5),
`FASTDYN_CONST_PC_BENCH_ITERATIONS`, or `FASTDYN_CONST_PC_ARTIFACTS` to retain
the JSON report and individual logs.  The timing boundary is FastDyn's
`machine0.qemu_execution` phase; both modes use the same executable, firmware,
plugin, QEMU model, and modifier.  CI checks correctness and structure, not a
wall-clock threshold.
