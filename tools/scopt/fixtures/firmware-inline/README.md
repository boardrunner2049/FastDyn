# Firmware-inline fixture

This Cortex-M3 image is the correctness fixture for `firmware-inline@1`.
`boot` calls `init`, `init` contains a loop and a nested call to `leaf`, and
the ordinary guest stack/LR effects remain in the translated code.  Dormant
`cold_entry` and recursive functions support missing-capture and cycle tests.

Build it with the repository toolchain:

```sh
arm-none-eabi-as -mcpu=cortex-m3 -mthumb \
  -o /tmp/firmware-inline.o tools/scopt/fixtures/firmware-inline/guest.S
arm-none-eabi-ld -T tools/scopt/fixtures/firmware-inline/guest.ld \
  -o /tmp/firmware-inline.elf /tmp/firmware-inline.o
arm-none-eabi-objdump -d /tmp/firmware-inline.elf
```

The integration workflow captures this ELF with the same rebuilt QEMU and
FastDyn plugin used for replay, imports the cache and SCM metadata, then runs:

```sh
QEMU_SHA=$(sha256sum "$QEMU" | cut -d' ' -f1)
SCOPT_ENVLAYOUT_PATH="$ENVLAYOUT_DIR" PYTHONPATH=tools/scopt \
  python3 -m scopt optimize "$SNAPSHOT" \
    --pipeline firmware-inline --b3 --runtime-identity "$RUNTIME_IDENTITY" \
    --v7-exe "$QEMU_SHA" --elf /tmp/firmware-inline.elf \
    --store "$STORE" --warm-baseline "$SNAPSHOT" --json
```

The workflow then requires v7 export/reimport identity and a warm replay with
zero appended translations.  This fixture is semantic and activation evidence,
not a real-application performance result.

`regression-ablations.json` records the real FreeRTOS boot workload that exposed
and confirmed the nested source-to-region lookup regression.  It contains the
raw per-process native timings, paired bootstrap intervals, artifact identities,
and diagnostic counters for both the faulty and corrected QEMU implementations.
