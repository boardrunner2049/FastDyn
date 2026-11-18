# Getting started: from a fresh machine to a first optimized artifact

This page takes you from a new Ubuntu machine to an optimized supercache (`.sc`)
artifact that FastDyn loads, using a QEMU you built yourself. It then points at the
pages that go deeper:

- `docs/qemu.md`: the QEMU branch, what scopt needs from it, and how artifacts bind
  to one QEMU executable;
- `docs/workflow.md`: the full prepare → optimize → validate → run → report
  workflow, including timing campaigns;
- `docs/pass-writer.md`: writing a new pass;
- `docs/design/README.md`: the design documents.

Paths below are relative to the root of the FastDyn checkout unless they start with
`tools/scopt/`, or a variable such as `$W`.

## 1. Prerequisites

Ubuntu 24.04 (x86-64 host; the optimizer reads x86-64 host code).

```sh
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake git meson ninja-build pkg-config python3-venv \
  libglib2.0-dev libcjson-dev libpixman-1-dev libfdt-dev zlib1g-dev flex bison \
  libstlink-dev libusb-1.0-0-dev \
  python3.12 python3.12-venv gdb util-linux
```

- **Python 3.12** for scopt (`requires-python >= 3.12`; Ubuntu 24.04's `python3` is 3.12).
- **gdb** for `scopt envlayout produce` (reads the QEMU executable's DWARF) and for the
  gdb-based diagnostics. scopt always runs gdb as `gdb -nx` from a temporary directory,
  because the QEMU tree carries a `.gdbinit`; do the same when you run gdb by hand.
- **setarch** (package `util-linux`): every supercache run is launched under
  `setarch x86_64 -R`. The QEMU supercache refuses to run with ASLR enabled.
- **arm-none-eabi-gcc** only if you build the example firmware below (Arm GNU Toolchain
  13.3.Rel1 was used for the qualified images).

## 2. Clone FastDyn and the QEMU submodule

```sh
git clone -b scopt https://github.com/boardrunner2049/FastDyn.git
cd FastDyn
git submodule update --init third_party/common/qemu
git -C third_party/common/qemu log --oneline -1   # 934890ed51 tcg/supercache: .sc v6 loader ...
```

The submodule is pinned to the `scopt` branch of `boardrunner2049/qemu`: the FastDyn
SuperCache base plus the FastDyn icount patch and the three scopt QEMU changes (capture,
profile, v6 loader). `docs/qemu.md` describes each commit.

## 3. Build QEMU and the FastDyn plugin

### With setup.sh

```sh
source ./setup.sh --build-qemu --qemu-root third_party/common/qemu
```

This creates `fastdyn-env/` (the `fastdyn` command), configures QEMU with
`../configure --target-list=arm-softmmu --enable-plugins`, builds
`third_party/common/qemu/build/qemu-system-arm`, and builds `build/libfastdyn.so`.
Naming `--qemu-root` is optional on a fresh checkout. It matters when an already built
`../qemu` sits next to the checkout: without it, setup.sh prefers that older build, and
it has neither the capture/profile hooks nor the v6 loader.

**The icount patch.** setup.sh applies `patches/qemu-fastdyn-plugin-icount.patch` only
when `git apply --reverse --check` says it is not already there. On this branch the
QEMU submodule already contains it (commit `00c19f7ee0`), so setup prints "FastDyn QEMU
compatibility patch is already applied". Do not apply it a second time: a plain
`git apply` of that patch fails on this QEMU.

### By hand

```sh
cd third_party/common/qemu
git apply --reverse --check ../../../patches/qemu-fastdyn-plugin-icount.patch   # must succeed
mkdir -p build && cd build
../configure --target-list=arm-softmmu --enable-plugins
make -j"$(nproc)" qemu-system-arm          # only this target; see docs/qemu.md
cd ../../../..
python3 -m venv fastdyn-env                 # the fastdyn command (what setup.sh does)
fastdyn-env/bin/pip install -r requirements.txt
fastdyn-env/bin/pip install -e src
make DEV=true                               # build/libfastdyn.so
```

Keep the default debug information (`-g`): `scopt envlayout produce` needs the DWARF of
`CPUArchState`/`CPUState`. Do not strip the binary.

Check:

```sh
ls -l third_party/common/qemu/build/qemu-system-arm build/libfastdyn.so
sha256sum third_party/common/qemu/build/qemu-system-arm
```

Write down that sha256. Everything scopt produces for this QEMU is bound to it.

## 4. Install scopt

```sh
python3.12 -m venv scopt-env
scopt-env/bin/pip install -e 'tools/scopt[test]'
. scopt-env/bin/activate
scopt --help
scopt contracts check            # schemas and frozen binary layouts: "contracts: ok"
```

`scopt` and `python -m scopt` are the same program. Dependencies: capstone 5.0.3,
pyelftools, jsonschema, click (and pytest for the tests).

## 5. Run the tests

```sh
cd tools/scopt
python -m pytest tests -m 'not qemu' -q
cd ../..
```

The suite uses synthetic snapshots and the files in `tests/data/`, so most of it runs
anywhere. Expect a number of **skips**, all for evidence that is not in the
repository:

- tests marked `real`, and tests that re-check recorded runs: they read the
  maintainer's workspace (`$FASTDYN_ROOT/runs/...`, `$FASTDYN_ROOT/fixtures/...`, the
  B0 snapshot `runs/b0/cache/minimal.sc`, the WP-QINT and gate QEMU builds) and skip
  with a message such as "minimal snapshot not present", "fixtures/b3 not present" or
  "integrated binary not present";
- the B5 freeze test skips when scopt's code changed after the B5 campaign was frozen
  ("scopt code changed since the B5 freeze (expected)");
- a few `xfail` rows are known, documented limits.

A skip is never a failure. `-rs` prints every skip reason. Tests marked `qemu` need a
built QEMU and the fixtures; they are the milestone drivers listed at the end of
`docs/workflow.md`.

## 6. Bind scopt to your QEMU

scopt only trusts per-executable data for the executable it was produced for, keyed by
the sha256 of `qemu-system-arm`. The package ships data for the two maintainer builds
(gate `56d8bfb3…`, WP-QINT `8864c726…`). Your build has another sha256, so produce your
own.

```sh
Q=$PWD/third_party/common/qemu/build/qemu-system-arm
QSHA=$(sha256sum "$Q" | cut -d' ' -f1)
mkdir -p ~/scopt-data/envlayouts ~/scopt-data/helper_effects
export SCOPT_ENVLAYOUT_PATH=~/scopt-data/envlayouts
export SCOPT_HELPER_EFFECTS_PATH=~/scopt-data/helper_effects
export SCOPT_V6_EXES=$QSHA
export SCOPT_FASTDYN_SRC=$PWD            # FastDyn checkout: fastdyn-env/bin/fastdyn, build/
export FASTDYN_ROOT=~/scopt-work         # your workspace for runs (not the checkout)
mkdir -p "$FASTDYN_ROOT"
```

1. **EnvLayout** (env field offsets, from DWARF, needed by B3 and B4):

   ```sh
   scopt envlayout produce --exe "$Q" --out $SCOPT_ENVLAYOUT_PATH/$QSHA.json
   scopt envlayout show --exe-sha256 sha256:$QSHA      # regs[15], hflags.flags, neg.can_do_io ...
   ```

   The file must be named `<exe sha256 hex>.json`; scopt looks it up by that name.

2. **Helper effect table** (what each TCG helper may read, write, or fault on). Without
   one, every helper call is treated as unknown ("fail closed"): artifacts stay correct,
   but B3/B4 find little to do. Copy the shipped gate table to your executable; the
   command first checks that every QEMU source file the table was reviewed against is
   byte-identical in your tree:

   ```sh
   scopt helper-effects rebind \
     --from-exe-sha 56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90 \
     --exe "$Q"          # --qemu-src defaults to the tree that holds build/; writes
                         # $SCOPT_HELPER_EFFECTS_PATH/$QSHA.json
   scopt helper-effects show --exe "$Q"
   ```

   On an unmodified `scopt` QEMU branch this succeeds. If you changed a helper source it
   fails with exit 1 and lists the files that differ; then the rows have to be reviewed
   against your sources (`docs/qemu.md`).

3. **v6 loader.** B3 artifacts use the `.sc` v6 format, which only a QEMU with the v6
   loader reads. `SCOPT_V6_EXES` (comma separated sha256 values) tells scopt which
   executables have it; `scopt optimize --v6-exe SHA` does the same for one command.

Put the `export` lines in a file you source in every shell that runs scopt.

## 7. Choose a firmware and write the base FastDyn TOML

Any Cortex-M firmware that runs to a defined end under FastDyn works. The worked example
is the upstream FreeRTOS MPS2 Cortex-M3 demo (the same build as
`tests/integration/run_freertos_introspection_smoke.sh`), pinned to FreeRTOS
`48d4714c71da903b39baa4615b623525a0c0e2a3` (kernel `89592801dd8b`):

```sh
git clone https://github.com/FreeRTOS/FreeRTOS.git ~/fw/FreeRTOS
git -C ~/fw/FreeRTOS checkout 48d4714c71da903b39baa4615b623525a0c0e2a3
git -C ~/fw/FreeRTOS submodule update --init FreeRTOS/Source FreeRTOS-Plus/Source/FreeRTOS-Plus-Trace
make -C ~/fw/FreeRTOS/FreeRTOS/Demo/CORTEX_MPS2_QEMU_IAR_GCC/build/gcc -j4
FW=~/fw/FreeRTOS/FreeRTOS/Demo/CORTEX_MPS2_QEMU_IAR_GCC/build/gcc/output/RTOSDemo.out
arm-none-eabi-nm "$FW" | grep -E ' prvQueueReceiveTask$| prvQueueSendTimerCallback$'
```

What the TOML needs for scopt:

| key | value | why |
|---|---|---|
| `[Machine].supercache` | a relative name, e.g. `"cache/fw.sc"` | turns the persistent cache on; scopt rewrites the path into each run directory |
| `[Machine].supercache_flat` | `false` | scopt reads and writes the record (non-flat) format only; flat is refused (`unsupported_flat`) |
| `[Machine].icount` | `{ shift = 0, sleep = false, align = false }` | deterministic guest time, so runs, profiles and outputs repeat exactly |
| `[Machine].qemu_path` | the absolute path of your `qemu-system-arm` | a `qemu_path` in the TOML wins over `--qemu`/`FASTDYN_QEMU`; keep them equal |
| `[CPU.cpu0].plugin_library` | absolute path of `build/libfastdyn.so` | FastDyn is launched from `$SCOPT_FASTDYN_SRC` |
| an end of run | a `benchmark_end` virtual (or another clean exit) | a run counts only when QEMU exits 0; `benchmark_start`/`benchmark_end` also give the `[BENCH]` window that timing uses |
| memory banks | `backend = "file"`, a relative `memory_file` | scopt gives every run its own RAM files |

Do not edit the TOML between runs that share a cache: FastDyn fingerprints its content
and deletes a cache whose configuration changed. Inputs that vary between runs go
into an input RAM bank (`--inputs`, `--input-bank`), not into the TOML.

Example `fw.toml` for the demo above (replace the addresses with the ones `nm` printed;
`at` is the function address with the Thumb bit cleared):

```toml
[Memory]
[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size  = "8M"
memory_type  = "SRAM"
backend      = "file"
memory_file  = "ram0.ram"
share        = true
prealloc     = false

[Machine]
platform = "generic-cortexm"
qemu_path = "/home/me/FastDyn/third_party/common/qemu/build/qemu-system-arm"
supercache = "cache/fw.sc"
supercache_flat = false
icount = { shift = 0, sleep = false, align = false }
display = "none"
serial = "none"
monitor_port = 0
qmp_socket = "qmp.sock"
log_file = "qemu.log"
log_options = "none"
coverage = false
finline = 'None'

[Device]
[Device.Models]

[CPU]
[[CPU.cpu0]]
arch = "arm"
machine = "cortexm"
cpu = "cortex-m3"
binary = "/home/me/fw/FreeRTOS/FreeRTOS/Demo/CORTEX_MPS2_QEMU_IAR_GCC/build/gcc/output/RTOSDemo.out"
plugin_library = "/home/me/FastDyn/build/libfastdyn.so"
init_nsvtor = "0x0"

[CPU.cpu0.plugins.introspection]
enabled = false

# FastDyn's generic Cortex-M SoC implements 4 NVIC priority bits; the MPS2-AN385
# demo expects 8 and fails configASSERT in xPortStartScheduler. Make the probed
# NVIC_IPR read return 0xff (the ldrb result register right after the load).
[[CPU.cpu0.modifiers]]
at = "0x00002a04"
patch = "r3 <- 0xff"

[[CPU.cpu0.virtuals]]            # prvQueueReceiveTask: start of the measured window
at = "0x00003018"
instruction = "benchmark_start"

[[CPU.cpu0.virtuals]]            # prvQueueSendTimerCallback: first timer expiry ends the run
at = "0x00002fd0"
instruction = "benchmark_end"
args = ["fw"]
```

The modifier address is the instruction after `ldrb r3, [r3]` in `xPortStartScheduler`
(`arm-none-eabi-objdump -d "$FW"`); it only exists for this demo. Check the TOML once
with plain FastDyn: `setarch x86_64 -R fastdyn-env/bin/fastdyn run -c fw.toml -o /tmp/fwrun`.

This demo spends almost all of its time in the idle loop, so it is a good smoke test
but a poor optimization target. The evaluated workload is a compute-heavy variant with
an input bank; its sources are kept by the maintainer and are not in the repository.

## 8. Prepare, import, profile

```sh
W=$FASTDYN_ROOT/fw        # a fresh directory per workload
CFG=$PWD/fw.toml
mkdir -p $W

# capture-on cold run: .sc + .scm capture + oracle run dir (add --inputs FILE per input)
scopt prepare -c $CFG --out $W/prep --qemu "$Q"
# identity of the configuration and of the runtime
scopt bundle identity -c $W/prep/run.toml --manifest $W/prep/cache/fw.sc.fastdyn.json \
      --out $W/bundle.json
scopt bundle runtime-identity --run-dir $W/prep/oracle --sc $W/prep/cache/fw.sc \
      --scm $W/prep/capture.scm --out $W/runtime_identity.json
# immutable snapshot in a store (exit 2 = rejected, reason printed)
scopt import $W/prep --bundle $W/bundle.json --runtime-identity $W/runtime_identity.json \
      --store $W/store
SNAP=$(ls -d $W/store/snapshots/*)
# RuntimeIdentity v2 (admission facts), needed by B3 and B4
scopt admission identity --snapshot $SNAP --config $CFG --out $W/runtime_identity.v2.json
# exact software profile on unchained warm copies (train inputs only)
scopt profile run -c $CFG --snapshot $SNAP --qemu "$Q" --out $W/prof
```

`prepare` exits 0 (PASS), 1 (FAIL) or 2 (refused, for example a flat cache or a TOML
without a supercache). `profile run` takes `--phase NAME@0xVADDR` to split the profile
at a guest address (for example the `benchmark_start` address) and `--inputs` for each
training input. `$W/prof/merged.profile.json` is the profile the passes read.

## 9. Optimize

Each command writes an artifact directory under `STORE/artifacts/<id>/` and exits 0, or
exits 2 and writes the reason to `STORE/rejections/<candidate>.json`. `--json` prints
`artifact_dir`.

```sh
# B2: profile-guided hot/cold layout (.sc v5, any loader)
scopt optimize $SNAP --pipeline b2-layout --profile $W/prof/merged.profile.json \
      --store $W/out/layout --json

# B3: cross-TB return specialization, chaining, constant propagation (.sc v6)
scopt optimize $SNAP --pipeline b3 --b3 --runtime-identity $W/runtime_identity.v2.json \
      --prep $W/prep --profile $W/prof/merged.profile.json \
      --selftest $W/hostsym_selftest.json --store $W/out/b3 --json

# B4: state-sync elimination (.sc v5)
scopt b4 package $SNAP --store $W/out/b4 --runtime $W/runtime_identity.v2.json

# icount-entry store forwarding: a fix of baseline TCG code, not an optimizer result (.sc v5)
scopt sfb package $SNAP --store $W/out/sfb
```

`--selftest` names the hostsym self-test document; it is computed and written there the
first time. `--disable PASS` / `--only PASS` run ablations. Pass lists live in
`tools/scopt/pipelines/*.toml`.

## 10. Validate and publish

```sh
A=$(ls -d $W/out/layout/artifacts/*)
scopt validate $A --snapshot $SNAP                  # static V1-V9 (v5 artifacts)
scopt publish  $A --store $W/pub                    # verified copy; v6 artifacts are re-checked here
P=$(ls -d $W/pub/artifacts/*)
scopt validate $P --dynamic -c $CFG --qemu "$Q" --baseline $SNAP --out $W/v10/layout
```

The dynamic check (V10) deploys the artifact and the baseline into fresh run
directories, runs both, and compares guest output and the run proof. Use inputs the
profile did not see (`--inputs`) when the firmware has an input bank.

## 11. Run

```sh
scopt deploy $P -c $CFG --run-dir $W/run1 --qemu "$Q"     # writes the artifact as the run's cache
scopt rundir launch $W/run1                               # setarch -R, timeout, proof -> run.json
```

FastDyn then starts warm from the optimized cache. For timing (paired, interleaved,
frozen campaigns) see `docs/workflow.md` §4 and `docs/timing-and-report.md`.

## 12. Troubleshooting

| symptom | cause and fix |
|---|---|
| `import` exits 2 with `runtime_mismatch:<field>` or `runtime_identity_id_mismatch` | the RuntimeIdentity was made for another QEMU, plugin or `.sc` header, or edited by hand. Re-run `bundle runtime-identity` on this prep's oracle directory. `runtime_stale:<file>` means a native file (QEMU, plugin, firmware) changed after `prepare`: prepare again. |
| profile ingest fails with `runtime_signature_mismatch` or `code_address_mismatch` | the profiled QEMU is not the one that recorded the snapshot (any rebuild changes the runtime signature). Profile with the same `--qemu` as `prepare`. |
| deploy or launch refuses with `runtime_mismatch` / `runtime_qemu_unavailable` | the QEMU at the recorded path changed or vanished after deploy. Rebuilding QEMU invalidates every snapshot and artifact made with the old binary: start again at step 6. |
| `guard_layout_unbound` (B3/B4 reject, `envlayout show` exits 2) | no EnvLayout for this executable's sha256. Run `scopt envlayout produce` (step 6) and check `SCOPT_ENVLAYOUT_PATH` and the file name. |
| B3/B4 produce almost nothing; helpers show as class `global` | no helper effect table for this executable, so every helper is fail-closed. Run `scopt helper-effects rebind` (step 6); `scopt helper-effects show --exe "$Q"` confirms the binding. |
| `helper-effects rebind` exits 1 (`helper_sources_differ`) | your QEMU's helper sources differ from the reviewed ones. Review the listed files against the table's rows and write a table for your build (`docs/qemu.md`). |
| `v6_runtime_required` (V7), `v6_loader_required`, or `validate` exits 2 on a v6 artifact | the target QEMU is not known to have the v6 loader. Add its sha256 to `SCOPT_V6_EXES` (or pass `--v6-exe`). A QEMU without the loader cannot run B3 artifacts at all. |
| `unsupported_flat` | set `supercache_flat = false`. |
| QEMU refuses the supercache / ASLR message | launch under `setarch x86_64 -R` (scopt does this for its own runs). |
| `scopt validate` cannot read a v6 artifact | known gap: static re-validation reads v5 only. `scopt publish` re-checks v6 artifacts. |
| results differ from run to run | icount is off or `shift` is not 0, or something non-deterministic is in the configuration. |
