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

The steps in short (each is explained below; none may be skipped):

1. install the Ubuntu packages (§1);
2. `git clone -b scopt https://github.com/boardrunner2049/FastDyn.git` and
   `git submodule update --init third_party/common/qemu` (§2);
3. build QEMU and the FastDyn plugin with `setup.sh` (§3);
4. create a Python 3.12 venv and `pip install -e 'tools/scopt[test]'` (`[test]` brings
   pytest) (§4);
5. bind scopt to your QEMU (§5): `scopt envlayout produce`, **`scopt helper-effects
   rebind`** (without it every helper call is fail-closed and B3/B4 find little to do),
   and the `export` lines (`SCOPT_ENVLAYOUT_PATH`, `SCOPT_HELPER_EFFECTS_PATH`,
   `SCOPT_V6_EXES`, `SCOPT_RUNTIME_QEMU`, `SCOPT_RUNTIME_QEMU_SHA256`, `FASTDYN_ROOT`);
6. run the tests (§6);
7. write the firmware's FastDyn TOML (§7), then prepare, optimize, validate and run
   (§8-§11); `docs/workflow.md` has the full workflow.

## 1. Prerequisites

Ubuntu 24.04 (x86-64 host; the optimizer reads x86-64 host code).

```sh
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake git ninja-build pkg-config python3-venv \
  libglib2.0-dev libcjson-dev libpixman-1-dev libfdt-dev zlib1g-dev flex bison \
  libstlink-dev libusb-1.0-0-dev \
  python3.12 python3.12-venv gdb util-linux
```

- **Python 3.12** for scopt (`requires-python >= 3.12`; Ubuntu 24.04's `python3` is 3.12).
- **meson** comes from pip, not apt: QEMU's `configure` needs meson >= 1.5, which
  FastDyn's `requirements.txt` installs into `fastdyn-env/`. Ubuntu 24.04's apt meson
  (1.3) is too old, so it is not in the list.
- **Disk space**: about 12 GB free for the checkout (see §3; most of it is FastDyn
  submodules scopt does not use), plus a few GB for the scopt work directory.
- **Network access** during the build: `setup.sh` fetches submodules and Python
  packages, and QEMU's `configure` downloads its softfloat test subprojects.
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

If your checkout has no `tools/scopt/docs/getting-started.md`, or `scopt helper-effects
--help` (after §4) lists no `rebind` command, you cloned an older state of the `scopt`
branch than this page describes; fetch it again.

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
It took about 3 minutes on 16 cores. Naming `--qemu-root` is optional on a fresh
checkout. It matters when an already built `../qemu` sits next to the checkout: without
it, setup.sh prefers that older build, and it has neither the capture/profile hooks nor
the v6 loader.

setup.sh is FastDyn's general setup, so it does more than scopt needs:

- it installs all of FastDyn's Python requirements into `fastdyn-env/` (vtk, opencv,
  matplotlib, MAVProxy, ...), including the meson QEMU needs;
- it initializes the FastDyn submodules `third_party/common/cmsis-svd-data`,
  `third_party/courbet_deps/SITL_Models` and `third_party/courbet_deps/mavlink_headers`.
  They dominate the disk use: SITL_Models is about 750 MB of the download, and
  cmsis-svd-data downloads only about 34 MB but checks out to about 5.8 GB. A checkout
  after setup.sh was about 11 GB in total. scopt uses none of these submodules.
  `--skip-submodules` leaves them out (it should save about 7 GB; the scopt path was
  checked with them present, not with this flag);
- it clones the OptiFuzz dependency (banquo) into `../banquo`, **outside the
  checkout**; add `--skip-optifuzz` to leave it out;
- it creates `third_party/common/qemu/ws/` with Courbet RAM backing files (one of
  512 MiB); add `--skip-qemu-workspace` to leave them out. scopt does not use them:
  every scopt run gets its own RAM files;
- it builds with `make -j$(nproc)` and has no option for the job count. `nproc`
  follows the CPU affinity, so
  `taskset -c 0-7 bash -c 'source ./setup.sh --build-qemu --qemu-root third_party/common/qemu'`
  limits it to 8 jobs.

The build log prints `ninja: error: Could not initialize jobserver` and
`fatal: No names found, cannot describe anything.` (the submodule is a shallow clone);
both are harmless, the build goes on.

**The icount patch.** setup.sh applies `patches/qemu-fastdyn-plugin-icount.patch` only
when `git apply --reverse --check` says it is not already there. On this branch the
QEMU submodule already contains it (commit `00c19f7ee0`), so setup prints "FastDyn QEMU
compatibility patch is already applied". Do not apply it a second time: a plain
`git apply` of that patch fails on this QEMU.

### By hand

```sh
python3 -m venv fastdyn-env                 # the fastdyn command (what setup.sh does)
fastdyn-env/bin/pip install -r requirements.txt     # includes meson >= 1.5 for QEMU
fastdyn-env/bin/pip install -e src
. fastdyn-env/bin/activate                  # QEMU's configure finds meson here
cd third_party/common/qemu
git apply --reverse --check ../../../patches/qemu-fastdyn-plugin-icount.patch   # must succeed
mkdir -p build && cd build
../configure --target-list=arm-softmmu --enable-plugins
make -j8 qemu-system-arm                    # only this target; see docs/qemu.md
cd ../../../..
make DEV=true                               # build/libfastdyn.so
```

setup.sh builds the plugin with more options (device models, flight-controller
physics); `make DEV=true` is enough for scopt. Both give `build/libfastdyn.so`.

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
scopt-env/bin/pip install -e 'tools/scopt[test]'   # [test] adds pytest
. scopt-env/bin/activate
scopt --help
scopt contracts check            # schemas and frozen binary layouts: "contracts: ok"
```

`scopt` and `python -m scopt` are the same program. Dependencies: capstone 5.0.3,
pyelftools, jsonschema, click (and pytest for the tests). `scopt-env/` is listed in
the checkout's `.gitignore`.

## 5. Bind scopt to your QEMU

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
export SCOPT_RUNTIME_QEMU=$Q SCOPT_RUNTIME_QEMU_SHA256=$QSHA   # QEMU for commands without --qemu
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
   against your sources (`docs/qemu.md`). The table's `qemu_source.commit`
   (`4671d5ceda`) is the maintainer's commit the rows were reviewed in; it is not on
   GitHub. The public commit `934890ed51` has the same tree, and the rebind compares
   file contents, not commits.

3. **v6 loader.** B3 artifacts use the `.sc` v6 format, which only a QEMU with the v6
   loader reads. scopt treats an executable as v6-capable when its sha256 is in
   `SCOPT_V6_EXES` (comma separated), is given to `scopt optimize --v6-exe SHA`, or is
   the gate binary named by the maintainer's record
   `$FASTDYN_ROOT/runs/wp22/gate-binary.json` (absent on your machine). These sources
   add up; with the exports above, your build is the v6-capable one.

`SCOPT_RUNTIME_QEMU` and `SCOPT_RUNTIME_QEMU_SHA256` name the QEMU that a command
uses when it is given no `--qemu` (the commands below all pass `--qemu "$Q"`). scopt
checks the binary against that sha256. Without them, the only default is the
maintainer's WP-QINT build under `$FASTDYN_ROOT/wt/`, which is absent on your
machine, so such a command is refused with `runtime_qemu_unavailable` rather than run
with an unchecked binary.

The other two locations scopt uses have defaults:

- `FASTDYN_ROOT` is the workspace: the commands below write under `$W`, a directory in
  it, and scopt reads optional maintainer evidence there (`runs/`, `fixtures/`, the
  maintainer's QEMU builds under `wt/`). Unset, it defaults to the directory that holds
  `tools/` (here the checkout root).
- `SCOPT_FASTDYN_SRC` is the FastDyn checkout scopt launches FastDyn from
  (`fastdyn-env/bin/fastdyn`, `build/libfastdyn.so`; FastDyn runs with it as the
  working directory). Unset, it is the checkout that contains this `tools/scopt`;
  set it only to use another FastDyn build.

Put the `export` lines in a file you source in every shell that runs scopt.

## 6. Run the tests

```sh
cd tools/scopt
python -m pytest tests -m 'not qemu' -q -rs
cd ../..
```

The suite uses synthetic snapshots and the files in `tests/data/`, so most of it runs
anywhere; it takes under a minute. Without the maintainer's evidence it gives about
820 passed, about 620 skipped, 0 failed and 0 xfailed. The skips are tests that need
evidence that is not in the repository: tests marked `real`, and tests that re-check
recorded runs. They read the maintainer's workspace under `$FASTDYN_ROOT`
(`runs/...`, `fixtures/...`, the B0 snapshot `runs/b0/cache/minimal.sc`, the WP-QINT
and gate QEMU builds under `wt/`, the B5 plan under `runs/b5`, or `$B5_RUNS`), and
each skips with a message that names the missing path, for example
`.../scopt-work/runs/b0/cache/minimal.sc not present` or
`.../scopt-work/fixtures/b3/b3conf.py not present`.

With the maintainer's workspace as `FASTDYN_ROOT` these tests run. Then a few
strict `xfail` rows record known results of earlier gate runs, and the B5 freeze test
skips with "scopt code changed since the B5 freeze (expected)" when scopt's code
changed after the B5 campaign was frozen.

`-rs` prints every skip reason. A skip is never a failure, and a failure is never an
expected result: report it. Tests marked `qemu` need a built QEMU and the fixtures; they
are the milestone drivers listed at the end of `docs/workflow.md`.

## 7. Choose a firmware and write the base FastDyn TOML

Any Cortex-M firmware that runs to a defined end under FastDyn works. The worked example
is the upstream FreeRTOS MPS2 Cortex-M3 demo, pinned to FreeRTOS
`48d4714c71da903b39baa4615b623525a0c0e2a3` (kernel `89592801dd8b`). It is the demo
directory that `tests/integration/run_freertos_introspection_smoke.sh` builds, but that
script builds the current FreeRTOS head and uses no NVIC modifier or benchmark virtuals;
the pinned build below is the one the addresses in the example TOML belong to. A shallow
fetch of the pinned commit is enough (a full clone of FreeRTOS is large):

```sh
mkdir -p ~/fw/FreeRTOS && cd ~/fw/FreeRTOS
git init -q && git remote add origin https://github.com/FreeRTOS/FreeRTOS.git
git fetch --depth 1 origin 48d4714c71da903b39baa4615b623525a0c0e2a3
git checkout -q FETCH_HEAD
git submodule update --init --depth 1 FreeRTOS/Source FreeRTOS-Plus/Source/FreeRTOS-Plus-Trace
cd -
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

Example `~/fw/fw.toml` for the demo above (replace the addresses with the ones `nm`
printed; `at` is the function address with the Thumb bit cleared). Keep the TOML outside
the checkout, in a directory of its own: FastDyn resolves a relative `supercache` path
next to the TOML.

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
# NVIC_IPR read return 0xff (r3, the ldrb result, at the uxtb that follows).
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

The modifier address is the `uxtb r3, r3` two instructions after `ldrb r3, [r3]` in
`xPortStartScheduler` (`arm-none-eabi-objdump -d "$FW"`: `2a00 ldrb r3, [r3]`,
`2a02 ldr r2, [pc, #188]`, `2a04 uxtb r3, r3`); it only exists for this demo. Check the
TOML once with plain FastDyn, from a scratch directory:

```sh
F=$PWD                                    # the FastDyn checkout
mkdir -p ~/fw/check && cd ~/fw/check
setarch x86_64 -R $F/fastdyn-env/bin/fastdyn run -c ~/fw/fw.toml -o fwrun
cd $F
```

It should exit 0 and print a `[BENCH]` line. A plain FastDyn run creates the relative
`memory_file`s (`ram0.ram`, and an input bank's file) in the current directory, which is
why it runs from a scratch directory, and the cache (`cache/fw.sc`) next to the TOML;
delete `~/fw/check` and `~/fw/cache` afterwards. A firmware with an input bank sees an
all-zero input in this check run. scopt's own runs never use these locations: each run
directory gets its own RAM files and cache.

This demo spends almost all of its time in the idle loop, so it is a good smoke test
but a poor optimization target. The evaluated workload is a compute-heavy variant with
an input bank; its sources are kept by the maintainer and are not in the repository.

**A firmware with an input bank.** A firmware that reads its input from memory gets one
more `[Memory]` bank. scopt writes the input file into the bank with id `input` (the
`--input-bank` default) before every run, zero-padded to the bank size. The bank's
contents are not part of FastDyn's TOML fingerprint, so one cache serves every input.
The compute workload declares it like this (base address and size are the firmware's
choice; the `memory_file` must be relative):

```toml
[[Memory.input]]
id = "input"
index = 1
base_address = "0x30000000"
memory_size  = "1M"
memory_type  = "SRAM"
backend      = "file"
memory_file  = "input.bin"
share        = true
prealloc     = false
```

`docs/fixtures.md` lists the rest of the fixture contract (relative run paths, a fixed
end of run).

## 8. Prepare, import, profile

```sh
W=$FASTDYN_ROOT/fw        # a fresh directory per workload
CFG=~/fw/fw.toml
mkdir -p $W

# capture-on cold run: .sc + .scm capture + oracle run dir
scopt prepare -c $CFG --out $W/prep --qemu "$Q"
# a firmware with an input bank: one --inputs per training input, for example
#   scopt prepare -c $CFG --out $W/prep --qemu "$Q" --inputs train-1.bin --inputs train-2.bin
# (without --inputs the cold run sees an empty input bank)
# identity of the configuration and of the runtime
scopt bundle identity -c $W/prep/run.toml --manifest $W/prep/cache/fw.sc.fastdyn.json \
      --out $W/bundle.json
scopt bundle runtime-identity --run-dir $W/prep/oracle --sc $W/prep/cache/fw.sc \
      --scm $W/prep/capture.scm --out $W/runtime_identity.json
# immutable snapshot in a store (exit 2 = rejected, reason printed)
scopt import $W/prep --bundle $W/bundle.json --runtime-identity $W/runtime_identity.json \
      --store $W/store
# (it prints "bundle: 7 fields verified; unverified: memory_map_hash": expected, see below)
SNAP=$(ls -d $W/store/snapshots/*)
# RuntimeIdentity v2 (admission facts), needed by B3 and B4
scopt admission identity --snapshot $SNAP --config $CFG --out $W/runtime_identity.v2.json
# exact software profile on unchained warm copies (train inputs only)
scopt profile run -c $CFG --snapshot $SNAP --qemu "$Q" --out $W/prof
# a firmware with an input bank and a measured window, for example
#   scopt profile run -c $CFG --snapshot $SNAP --qemu "$Q" --phase steady@0x3018 \
#         --inputs train-1.bin --inputs train-2.bin --out $W/prof
```

`prepare` exits 0 (PASS), 1 (FAIL) or 2 (refused, for example a flat cache or a TOML
without a supercache).

`import` re-derives every bundle field it can from the cache's FastDyn manifest and
the native files. `memory_map_hash` needs the TOML's memory banks, which `import` does
not read, so it is reported as unverified. That is expected: the field is part of the
`bundle_id`, and `deploy` recomputes the bundle from the run's TOML and refuses a
mismatch.

`profile run` takes `--phase NAME@0xVADDR` to split the profile at a guest address
(for example the `benchmark_start` address, `0x3018` in the §7 example) and
`--inputs` for each training input. `$W/prof/merged.profile.json` is the profile the passes read.

## 9. Optimize

Each command writes an artifact directory under `STORE/artifacts/<id>/`, or the reason
for a rejection to `STORE/rejections/<candidate>.json`. Use one store per command (as
below), so each store holds the artifacts of one kind.

- `scopt optimize` exits 0 when the artifact is published and 2 when it is rejected.
  With `--json` it prints a JSON document whose `artifact_dir` is the artifact
  directory (`null` when rejected); without it, the directory is printed on the line
  after `artifact <id>`.
- `scopt b4 package` and `scopt sfb package` have no `--json` option: they always
  print JSON (or write it to `--out FILE`). Its `status` is `published`, `no_change` or
  a rejection, and `path` is the artifact directory (or the rejection file). They exit
  0 for `published` or `no_change`, 1 otherwise.

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
A=$(ls -d $W/out/layout/artifacts/*)                # one artifact in this store (§9)
scopt validate $A --snapshot $SNAP                  # static V1-V9: B2 layout artifacts only
P=$W/pub/artifacts/$(basename $A)                   # publish keeps the artifact id
scopt publish  $A --store $W/pub                    # verified copy (every artifact kind)
scopt validate $P --dynamic -c $CFG --qemu "$Q" --baseline $SNAP --out $W/v10/layout

# B3 and B4: no static validate (see the table); publish re-checks them
for k in b3 b4; do
  A=$(ls -d $W/out/$k/artifacts/*)
  scopt publish $A --store $W/pub
  scopt validate $W/pub/artifacts/$(basename $A) --dynamic -c $CFG --qemu "$Q" \
        --baseline $SNAP --out $W/v10/$k
done
```

`$W/pub` now holds several artifacts, so name them by id (as above) rather than with
`ls -d $W/pub/artifacts/*`. `scopt publish --json` prints the published copy's
`artifact_dir`. Add `--inputs heldout-1.bin` (one per input) to the dynamic check for a
firmware with an input bank.

Which check applies to which artifact:

| artifact | packaged with | static `scopt validate` | `scopt publish` | `--dynamic` (V10) |
|---|---|---|---|---|
| B2 layout, identity (`.sc` v5) | V1-V9 | yes | yes | yes |
| B3 (`.sc` v6) | V1-V23 (hostsym-certified) | refused, exit 2 | yes, re-checks certification coverage | yes |
| B4 (`.sc` v5) | V1-V23 (hostsym-certified) | refused, exit 2 | yes, re-checks certification coverage | yes |
| icount-entry fix (`sfb`, `.sc` v5) | SFB1-SFB9 | refused, exit 2 | yes | yes |

Static `scopt validate` re-derives a move-only output: V2 requires every unit to be the
input's bytes, moved. B3, B4 and the icount-entry fix change unit bytes on purpose and
were certified by their own checks when they were packaged, so `validate` refuses them
(`static_not_applicable`, exit 2) instead of reporting V2 failures. `scopt publish`
verifies such an artifact: its manifest, its stored `validation.json` (accepted, no
failing row), the certification coverage of every edited unit, and the output
SnapshotId recomputed from `cache.sc`.

The dynamic check (V10) deploys the artifact and the baseline into fresh run
directories, runs both, and compares guest output and the run proof. Use inputs the
profile did not see (`--inputs`) when the firmware has an input bank.

## 11. Run

```sh
scopt deploy $P -c $CFG --run-dir $W/run1 --qemu "$Q"     # writes the artifact as the run's cache
scopt rundir launch $W/run1                               # setarch -R, timeout, proof -> run.json
```

FastDyn then starts warm from the optimized cache. `launch` resets every RAM bank
before the run, so a file copied into the run directory by hand is lost. Give a
firmware with an input bank its input on the launch:

```sh
scopt rundir launch $W/run2 --inputs heldout-1.bin        # --input-bank ID, default "input"
```

`launch` exits 0 when the run proof passes (verdict `PASS`). With
`--reference-norm FILE` it also compares the normalized output with `FILE` and exits 1
when they differ. Every launch writes the normalized output to `output.norm` in its run
directory. A baseline run is a deploy of the unoptimized snapshot directory itself:

```sh
scopt deploy $SNAP -c $CFG --run-dir $W/run0 --qemu "$Q"     # the baseline: the snapshot
scopt rundir launch $W/run0 --inputs heldout-1.bin
scopt deploy $P -c $CFG --run-dir $W/run3 --qemu "$Q"
scopt rundir launch $W/run3 --inputs heldout-1.bin --reference-norm $W/run0/output.norm
```

Leave out `--inputs` for a firmware without an input bank. A run
directory is used once: deploy into a fresh one for every launch. For timing (paired,
interleaved, frozen campaigns) see `docs/workflow.md` §4 and
`docs/timing-and-report.md`.

## 12. Cleaning up

scopt writes snapshots and artifacts read-only (they are immutable once stored), so
`rm -rf` of a work directory fails on them. Make them writable first:

```sh
chmod -R u+w $W && rm -rf $W
```

## 13. Troubleshooting

| symptom | cause and fix |
|---|---|
| `import` exits 2 with `runtime_mismatch:<field>` or `runtime_identity_id_mismatch` | the RuntimeIdentity was made for another QEMU, plugin or `.sc` header, or edited by hand. Re-run `bundle runtime-identity` on this prep's oracle directory. `runtime_stale:<file>` means a native file (QEMU, plugin, firmware) changed after `prepare`: prepare again. |
| profile ingest fails with `runtime_signature_mismatch` or `code_address_mismatch` | the profiled QEMU is not the one that recorded the snapshot (any rebuild changes the runtime signature). Profile with the same `--qemu` as `prepare`. |
| deploy or launch refuses with `runtime_mismatch` / `runtime_qemu_unavailable` | the QEMU at the recorded path changed or vanished after deploy. Rebuilding QEMU invalidates every snapshot and artifact made with the old binary: start again at step 5. |
| `guard_layout_unbound` (B3/B4 reject, `envlayout show` exits 2) | no EnvLayout for this executable's sha256. Run `scopt envlayout produce` (step 5) and check `SCOPT_ENVLAYOUT_PATH` and the file name. |
| B3/B4 produce almost nothing; helpers show as class `global` | no helper effect table for this executable, so every helper is fail-closed. Run `scopt helper-effects rebind` (step 5); `scopt helper-effects show --exe "$Q"` confirms the binding. |
| `helper-effects rebind` exits 1 (`helper_sources_differ`) | your QEMU's helper sources differ from the reviewed ones. Review the listed files against the table's rows and write a table for your build (`docs/qemu.md`). |
| `v6_runtime_required` (V7), `v6_loader_required`, or `validate --dynamic` exits 2 on a v6 artifact | the target QEMU is not known to have the v6 loader. Add its sha256 to `SCOPT_V6_EXES` (or pass `--v6-exe`). A QEMU without the loader cannot run B3 artifacts at all. |
| `unsupported_flat` | set `supercache_flat = false`. |
| QEMU refuses the supercache / ASLR message | launch under `setarch x86_64 -R` (scopt does this for its own runs). |
| static `scopt validate` exits 2 with `static_not_applicable` | the artifact is a B3, B4 or icount-entry artifact, which the move-only V1-V9 check does not cover (§10). Use `scopt publish` and `scopt validate --dynamic`. |
| `runtime_qemu_unavailable: .../wt/qemu-int/build/qemu-system-arm does not exist` | a command made a run directory without a QEMU. Pass `--qemu "$Q"` (or set `SCOPT_RUNTIME_QEMU` and `SCOPT_RUNTIME_QEMU_SHA256`); the default is the maintainer's WP-QINT build. |
| a run with an input bank behaves as if the input were empty | the input was not given to the run: use `rundir launch --inputs FILE` (§11), `prepare`/`profile run`/`validate --dynamic --inputs FILE`. |
| `rm -rf` of a work directory fails with "Permission denied" | stores are read-only; `chmod -R u+w DIR` first. |
| results differ from run to run | icount is off or `shift` is not 0, or something non-deterministic is in the configuration. |
