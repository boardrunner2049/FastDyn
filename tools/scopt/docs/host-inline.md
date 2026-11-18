# Native helper inlining

`host-inline@1` is an offline, standalone SuperCache v5 pass. It ranks verified
TCG helper calls, proposes `INLINE_CALL` edits, and publishes offset-stable
native islands through the normal scopt linker and validation path. QEMU runs
the resulting v5 cache through its existing loader.

The reviewed FastDyn revision was `72c398a748a2a6220e6965a17b8614f9f57258d6`.
Development started at `4108598b0942477159446ce01b33216d972f6de9` after the
branch was rebuilt and the independent v5/v6 parsers were checked in. The
runtime uses QEMU `934890ed5119ef70da69276991d921573f23ed4f` with the upstream
SuperCache, capture and profile hooks. The fixture source lock records the
ordered QEMU history, compiler, linker, dependencies, and setup patch hash.

## Reproduce the baseline and optimize

Use x86-64 Linux with Python 3.12 and the build packages in
[`source-lock.json`](../fixtures/host-inline/source-lock.json). The fixture
[README](../fixtures/host-inline/README.md) describes the guest workload, input
splits, and independent oracle. From the FastDyn checkout, install scopt and
build the [pinned native bridge](../native/README.md).

```sh
python3.12 -m venv scopt-env
scopt-env/bin/pip install -e 'tools/scopt[test]'
source scopt-env/bin/activate

W=/absolute/path/to/fresh-baseline
N=/absolute/path/to/llvm-bolt-build
python tools/scopt/scripts/bootstrap_host_inline.py \
  --source-lock tools/scopt/fixtures/host-inline/source-lock.json --work-dir "$W"
export SCOPT_ENVLAYOUT_PATH="$W/envlayouts"
export SCOPT_HOST_INLINE_BRIDGE="$N/build/bin/scopt-native"
export FASTDYN_ROOT="$PWD" SCOPT_FASTDYN_SRC="$PWD"
QEMU="$PWD/third_party/common/qemu/build/qemu-system-arm"
SNAP=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot"])' "$W/baseline.json")

scopt host-calls "$SNAP" --profile "$W/profile/merged.profile.json" --out "$W/host-calls.json"
scopt optimize "$SNAP" --pipeline host-inline \
  --profile "$W/profile/merged.profile.json" --store "$W/inline-store"
```

The bootstrap writes the `.sc`, `.scm`, raw `.qprof`, merged profile, exact
EnvLayout, runtime identities, and identity artifact. `bootstrap.log` records
commands and outputs. `baseline.json` names the verified snapshot, identity
artifact, and profile. `build-evidence.json` binds the executable and plugin
digests to the build. Regenerate the captures, layouts, and profiles after a
runtime rebuild. The captured executable, plugin, and their load bindings must
still match at optimization and publication.

```sh
ARTIFACT=/absolute/path/to/published-inline-artifact
scopt validate "$ARTIFACT" --snapshot "$SNAP"
scopt validate "$ARTIFACT" --dynamic -c "$W/host-inline.toml" --baseline "$SNAP" \
  --qemu "$QEMU" --inputs "$W/heldout-0.bin" --inputs "$W/heldout-1.bin" \
  --out "$W/inline-v10"
scopt optimize "$SNAP" --pipeline host-inline --disable host-inline \
  --store "$W/ablation-store"
```

The pipeline supports `--only` and `--disable`. Parameters include bridge
path, profile phase, callee byte and instruction limits, per-unit and global
site and growth limits, and a wall-time limit. The default profile phase is
`steady`; counts used for ranking are **TB entries**, a hotness proxy. With no
profile the policy uses a smaller static size limit. The pass ledger records
each refusal, estimated hotness, and emitted growth. Native islands use the
standalone pipeline. B3, guard-stub, and layout combinations have explicit
refusals.

## Supported runtime and calls

The host ABI is x86-64 Linux SysV with the audited QEMU TCG spill frame at
`934890e`. Guest layout and recovery come from the capture and its matching
EnvLayout. The pass checks each machine CALL against its captured helper, CALL
opmark, guest origin, and direct displacement or immutable captured pool slot.
It verifies the module digest, mapped device and inode, load bias, ELF symbol
range, and compiled bytes. The independent bridge and eligibility revisions
are part of the analysis input; ELF build IDs are supplementary evidence.

The initial policy admits pure QEMU helpers with integer arguments and return
values in registers. The leaf body uses supported integer instructions and
ordinary returns, with any direct branch staying within the complete function.
The checker compares exact bytes for non-branch instructions, including their
register widths and flags. It checks branch conditions, reachability, origins,
and return relocation locations. Register-only loops may preserve
nontermination through the same CFG correspondence. Unsupported effects, stack
or memory access, external transfers, calls, traps, and changes to captured
recovery dependencies receive per-site refusals. FastDyn virtual and plugin
calls are inventoried and image-bound, but their ABI is outside this initial
policy.

The state relation excludes the dead return-address slot below RSP. On this
runtime, x86-64 TCG reserves an aligned spill frame and emits ordinary calls;
frame spills and stack arguments use nonnegative offsets from RSP. The
checker rejects a caller that exposes its stack pointer or accesses below it,
and rejects a callee that can observe the stack, recover a guest PC, or escape
via an external control transfer. The runtime contract assumes a mapped
thread stack, no asynchronous unwinding through generated code, and no
observer of dead normal or CET shadow-stack slots. A balanced call and return
and the replacement transfers leave the same stack pointers. This contract
covers ordinary execution and does not qualify a debugger or unwinder that
observes the eliminated native frame.

## Artifact and proof

The pass exposes its logical fragments at the WorkingSet generation boundary.
Code, metadata and layout dependencies are invalidated. Analyses that cannot
use the preview report it explicitly. The linker appends the islands after the
original pool and moves the enlarged units as needed. A direct CALL retains
its five-byte footprint; a six-byte pool CALL becomes a near jump and one
padding byte. All original guest-instruction offsets and search bytes are
preserved. The retired call's physical reference and recovery records are
removed. An unused pool slot retains its bytes and offset.

The artifact uses the normal v5 `.sc` with version 3 derived TB metadata in
`.scm`. The new span records preserve exact origin and correspondence data.
Reimport checks the islands, entry and continuation edges, pool retirement,
recoveries, and exact source image. V24 independently checks the final linked
machine bytes and restores the original helper event for HostSym only after
correspondence succeeds. V2–V4 still verify the surrounding original code and
its fixups. V9 applies to identity artifacts. V10 compares held-out guest
results and verifies warm cache use. Re-optimizing an imported artifact
preserves existing islands without applying the same edit twice.

## Measured fixture

Cortex-M3 `RBIT` produced two automatically selected pool-indirect calls to
QEMU's real `helper_rbit`. The pass emitted two 82-byte islands for 164 bytes of
code growth. The hot loop site had a training proxy of 131,070 TB entries and
the one-time site had two. The normal published artifact passed V1–V8 and V24,
and both held-out V10 cases matched the independent oracle and appended zero
TBs. The fixture outputs were `0x0584f8cb` and `0x622aa39a`.

The timing campaign ran six interleaved repetitions per arm and per held-out
input. All 36 runs passed the oracle and warm-cache proof. Times below are
warm firmware execution medians on Apple Silicon using Rosetta for x86-64
Linux.

| Held-out input | Warm baseline | Identity artifact | Inline-only artifact |
| --- | ---: | ---: | ---: |
| 0 | 808.40 µs | 829.02 µs | 792.38 µs |
| 1 | 795.40 µs | 818.27 µs | 787.98 µs |
| Pooled | 795.40 µs | 823.35 µs | 789.10 µs |

The median paired inline time was 3.86% lower than warm baseline. The median
absolute deviation was 2.22 percentage points and individual pairs ranged
from 8.42% faster to 9.79% slower. Compared with identity the paired median
change was −5.90%, with a median absolute deviation of 2.85 percentage
points. Median per-run deployment preparation was about 0.52 seconds and
excluded from firmware time. Offline pass selection and bridge work took about
2.8 seconds on the same host. The benchmark measures this fixture and this
host. Neither the infrastructure nor the results establish a native-x86
speedup or the effects of cross-boundary cleanup.

```sh
python tools/scopt/scripts/measure_host_inline.py --baseline "$W" \
  --inline-artifact "$ARTIFACT" --inline-v10 "$W/inline-v10/dynamic.json" \
  --out "$W/performance" --repetitions 6 --seed 20261118
```

The output contains the frozen campaign, per-run evidence, and the paired
summary in `performance.json`. The two existing V18 HostSym native self-tests
report flags mismatches under Rosetta. V18 belongs to the B3/B4 path. The
standalone native path uses its own V24 check and the ordinary v5 and V10
validation.

Four additional held-out seeds generated with Python `random.Random(20261118)`
passed V10 with zero appends and matched the independent arithmetic oracle.
These runs were kept outside the timing campaign and training profile.

| Seed | Expected output |
| --- | --- |
| `0xe6b9197d` | `0x1a625ca5` |
| `0xa7f8c874` | `0xabd10a06` |
| `0xd4a8142a` | `0x30fc2dbd` |
| `0x34296a83` | `0xf0196e07` |
