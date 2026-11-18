# Host-inline baseline fixture

This Cortex-M3 workload performs 65,536 iterations of a 32-bit bit reversal and addition. QEMU translates the guest `rbit` instruction to its existing `helper_rbit`. FastDyn prints the final register and stops at the benchmark end marker. `oracle.py` computes the expected register independently. `inputs.json` fixes two training seeds and two held-out seeds. Preparation and profiling use the training seeds. The held-out pair is used for the independent output check and warm artifact validation.

## Reproduce

Use an x86-64 Linux FastDyn checkout containing these fixture sources. The instructions were reviewed against `72c398a`. This source lock starts from `4108598`, which includes the independent cache parsers. It pins QEMU at `934890ed5119ef70da69276991d921573f23ed4f`, with the capture, profile and loader patches already integrated. The bootstrap checks for changes outside `tools/scopt` relative to the recorded FastDyn commit.

Install Python 3.12 and the Ubuntu 24.04 packages and versions listed in `source-lock.json`. The lock includes GDB for DWARF extraction and `binutils-arm-none-eabi` for the firmware. Run from the FastDyn checkout root. Choose a fresh output directory.

```sh
python3.12 -m venv scopt-env
scopt-env/bin/pip install -e 'tools/scopt[test]'
scopt-env/bin/python tools/scopt/scripts/bootstrap_host_inline.py \
  --source-lock tools/scopt/fixtures/host-inline/source-lock.json \
  --work-dir /tmp/scopt-host-inline-baseline
```

The script uses FastDyn's source build in the checkout, retaining QEMU's `-O2 -g` configuration. It verifies the executable and plugin hashes after qualification. The EnvLayout is extracted with GDB for that executable. Keep the executable, plugin, and run files in place for later artifact tests. If the executable changes, run the bootstrap again in a fresh directory.

## Evidence

- `bootstrap.log` records subprocess commands and output. The run directories also contain the guest command, output, and launcher evidence.
- `build-evidence.json` records the actual revision, package versions, build options, and native image hashes. ELF build IDs appear in `bootstrap.log`.
- `no-cache-*`, `cold`, and `warm` record the expected firmware behavior and cache reuse. The warm run deploys the cold cache with the warm directory’s own manifest and proves zero appended TBs.
- `prep`, `envlayouts`, `bundle.json`, `runtime-identity.json`, and `runtime-identity.v2.json` hold the captured cache and its bound metadata. `profile` contains per-input `.qprof` files and the merged profile.
- `store` holds the immutable snapshot and identity artifact. `reimport` holds the round-trip snapshot. `v10/dynamic.json` records both held-out pairs, each with an independent expected-output check and zero appended TBs.
- `baseline.json` is written only after successful qualification. It points to the artifacts and records the oracle inputs, results expected, and runtime binding.
