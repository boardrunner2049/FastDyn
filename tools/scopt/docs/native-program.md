# Native whole-program optimization

FastDyn's cache image remains the executable program. Captured instruction spans, recovery rows, patch provenance, helper bindings, translation keys, link slots and per-image environment layouts accompany that image. The initial target is Cortex-M3 Thumb firmware running under an x86-64 QEMU host binary. Profiles rank candidates. Variant admission uses a checked predecessor fact or a value guard on the physical incoming path.

## Reproduce the measured fixtures

Use an x86-64 Ubuntu build environment with Python 3.12, the dependencies from the host-inline source lock and the editable scopt installation. Run at the FastDyn repository root. The driver builds the pinned runtime, recreates the RBIT capture, runs existing warm/identity/B3/B4/splice arms, and generates a separate executable specialization fixture. The small specialization fixture uses an explicit 64 KiB global code-growth limit. Every newly published arm receives static validation and a two-input held-out execution check.

```bash
source scopt-env/bin/activate
export LLVM_PROJECT_SOURCE_ARCHIVE=/path/to/llvm-project-20.1.2.src.tar.xz
export SCOPT_NATIVE_BUILD_ROOT=/path/to/scopt-native-build
tools/scopt/scripts/reproduce_program.sh /tmp/scopt-program-run
```

Set `SCOPT_RUNTIME_BUILD_EVIDENCE` to a previous bootstrap's `build-evidence.json` to reuse its QEMU and plugin after checking their hashes, dependency versions and runtime source lock. Captures, layout, profiles and validation run again in the new directory. A previously built locked bridge can be selected through `SCOPT_HOST_INLINE_BRIDGE`. On the Apple Silicon development host, Rosetta's x86 runner disagrees with the arithmetic auxiliary-flag oracle. Set `SCOPT_QEMU_USER_ORACLE=/usr/bin/qemu-x86_64` to run a separately identified x86 TCG V18 differential test. This setting changes the self-test engine. Firmware timing still uses the recorded system QEMU and FastDyn plugin. Inspect the oracle's engine, binary hash and version in its JSON record.

To use an existing qualified state-forward or ordinary-edge fixture, run these commands with its corresponding EnvLayout, v6-capable QEMU, plugin and native bridges. The composed command publishes the splice, reimports **all** derived SCM chunks, runs specialization and cleanup on that generation, and selects legal placement using effective unit sizes. It checks for a real v6 link and variant and a moved TB before launching the original-input V10 comparison.

```bash
export SCOPT_FASTDYN_SRC=$PWD FASTDYN_ROOT=$PWD
export SCOPT_LLVM_LAYOUT_BRIDGE=$SCOPT_HOST_INLINE_BRIDGE
python tools/scopt/scripts/bootstrap_global_specialization.py --fixture state-forward \
  --qemu "$PWD/third_party/common/qemu/build/qemu-system-arm" \
  --plugin "$PWD/build/libfastdyn.so" --work-dir /tmp/state-forward
python tools/scopt/scripts/compose_program.py /tmp/state-forward /tmp/state-composed
python tools/scopt/scripts/benchmark_passes.py /tmp/state-forward \
  tools/scopt/pipelines/global-region-llvm.toml /tmp/state-pass-cost \
  --post-splice /tmp/state-composed --params /tmp/state-composed/params.json --pairs 5
```

The normal optimizer entry point accepts the same snapshot and standard pass switches. The native stage is materialized, validated and reimported internally. Its artifact ID, original and derived snapshot IDs, and pass ledger appear under `native_stage` in the JSON response.

```bash
w=/tmp/state-forward
snapshot=$(python -c 'import json,sys;print(json.load(open(sys.argv[1]))["snapshot"])' "$w/baseline.json")
export SCOPT_ENVLAYOUT_PATH="$w/envlayouts"
scopt optimize "$snapshot" --pipeline native-program --store /tmp/scopt-native-store \
  --runtime-identity "$w/runtime-identity.v2.json" --elf "$w/guest.elf" --prep "$w/prep" \
  --profile "$w/profile/merged.profile.json" --params /tmp/state-composed/params.json --json
```

For a single pass use `--only host-inline-splice`. For a full-pipeline ablation use, for example, `--disable region-state-forward`. The splice pass can admit two independent integer callsites in one TB with `max_sites_per_unit=2`. Translated helper frames and guest control-flow entry contracts have separate admission and validation rules.

For the x86 TCG differential checker add `--selftest /path/to/current-hostsym-qemu.json` to both commands after generating the witness with `python tools/scopt/scripts/hostsym_qemu_oracle.py --out /path/to/current-hostsym-qemu.json`. Changing the proof kernel invalidates the witness. With `--fixture global-transitive`, the modified writer A, preserving B, and consumer C occupy different TBs. A guarded B-to-C private entry checks the assumed value before the optimized successor uses it. Its evaluation parameters set `min_pass_share` to 0.45 and `max_code_growth_bytes` to 65536 under `specialize-edges`.

On the bundled FreeRTOS example, `configs/minimal.toml` changes the LED-flash task argument to 3 and prints the value before ending at scheduler startup. `--pipeline local-scalar` checks ordinary entries under that public entry contract. `--pipeline native-program` also runs this cleanup on the generation consumed by whole-program planning. A pass that yields an unchanged cache image is marked explicitly by the prefix benchmark.

## Pass ledger

| Family | Existing capability and gap | Implementation and method | Check and measurement |
| --- | --- | --- | --- |
| Capture coverage | Execution-driven capture misses unexecuted Thumb entries | Fixture QEMU patch calls the translation path at every declared halfword and context. The versioned coverage reader checks actual keys, source digests, capsule identity, roots and residual entries | Independent control capture, changing inputs/contexts, partial/complete V10 and live unarm |
| Whole-program decisions | Existing return specialization uses predecessor-local summaries | `analysis/program.py`, `global_values.py` and `passes/specialize_edges.py` use local native summaries, conservative joins, a versioned global plan and serial partitions. This adapts the LGEN/WPA/LTRANS organization | Full-key and value guards, V12–V19 including independent V14 entry proof, conflicting general entry, one versus three partitions |
| Native composition | Splices and v6 guards previously required separate outputs | `splice_layout.py`, `link.py` and derived SCM metadata retain the certified splice while linking a later guard and moving effective units | Exact byte correspondence, reimport, original versus combined V10 and layout ablation |
| Native integer cleanup | The previous region pipeline folds exact constants and removes dead operations | `native_region.py`, `native_integer.py`, and `region_cleanup.py` add bounded native known-bit/range, repeated copy/extension, redundant test and repeated environment-load patterns. The pass uses host register widths, liveness, barriers and full-CFG predecessor information | Local positive and counterexample tests, B3 HostSym and held-out execution. Compare `local-scalar` on ordinary entries or `native-scalar-cleanup` on variants |
| Frame cleanup after recursive inlining | Existing frame expansion emits the inner return-slot release even immediately before an outer LEAVE | `validate_sym/frame_cleanup.py` deletes redundant stack arithmetic and NOPs, then rebinds all retained references. A separate forward checker verifies the observable instruction stream and target identities | Mutated target and stack observer rejected. Compare framed depth two with `compact_frame` true/false |
| Environment state | B4 eliminates local dead syncs | `region-state-forward` uses the guarded variant entry and its current local state to remove an immediate store of the value already present in that exact environment field | V12 and V15, unknown helper and alternate-value tests, full pipeline with pass disabled |
| Private state DSE | Ordinary B4 checks baseline store liveness | `region-state-dse` checks exact private-path dead stores on the effective variant after branch folding. It recomputes the observer-aware store proof and binds the effect table | HostSym and private-variant proof, live/partial-write and faulting-helper controls, public entry and held-out V10 |
| Layout | Existing Pettis–Hansen ordering supplies a baseline | `native/scopt_native.cpp` directly calls LLVM 20.1.2 `computeCacheDirectedLayout`. The adapter supplies site offsets and checks the result before legal placement. B3 uses actual grown sizes | Checked relocation and reimport, FreeRTOS LLVM versus existing ordering, staged combined LLVM versus disabled ordering |

Native scalar and global dataflow algorithms here are scopt code. Upstream LLVM IR, LLVM MIR, and GCC GIMPLE/RTL passes require their respective program representations. The LLVM layout utility is direct upstream reuse. Compact frame removal reduces the emitted body when alignment permits. Same-length rewrites preserve offsets and can leave NOPs in place.

Measured per-pass and leave-one-out results, intervals, raw samples and run qualification are in the [benchmark report](native-program-evaluation/README.md).

## Reading the benchmark data

`benchmark_passes.py` builds a pipeline prefix after every pass and a leave-one-out pipeline for every pass. It checks warm cache admission, firmware output, runtime and input hashes. Separate output stores allow parallel offline preparation with `--jobs` (default two). The timing harness randomizes arms within each input/repetition block. `pass-effects.json` records speedup against the previous prefix and the full pipeline's speedup against its corresponding ablation. `timing/ratios.json` retains the raw paired guest BENCH intervals. The top-level ratios in each comparison are observed timing diagnostics. Comparisons with byte-identical cache images set `attribution.status` to `not_attributable` and omit a credited ratio and interval by recording null values. Different cache bytes make a comparison eligible for attribution. Interpret the observed ratio together with its paired interval and the changed pipeline stages. The first distinct variant can also introduce a guard or a clone, so the cumulative prefix sometimes changes more than one machine instruction. Independent ablations expose that interaction.

The source interval, artifact/pass outcomes, allocated and logical cache sizes, and offline resource use are distinct from the timed vCPU interval. A v6 sparse arena has a large apparent file length even on small fixtures. Results obtained through Rosetta use its host scheduling and code translation. Physical x86 performance requires a separate campaign on that host.

## Scope of the proof

The runtime retains entry checks, guest PC and interrupt accounting, memory access, callbacks and fallback dispatch. Reaching all TBs in a timing run is a warm-cache check. The complete producer establishes only the declared immutable-byte and translation-context domain. Reachability still includes public entries, interrupt roots and conservative unknown transfers. The state pass does not sink an observable store across a fault. General memory PRE, arbitrary native helper frames, physical register allocation, vectorization, and deletion of independently enterable TBs need stronger contracts. A symbolic timeout, refused binding, failed oracle, or unsupported native instruction interface is recorded separately from an applied transformation.

The composed cache benchmarks use partial-mode entry policy. Complete-mode evidence is tied to its producer capture. The coverage fixture separately qualifies and times a guarded specialization in both modes using its coverage-aware runtime. Details and the replay command are in `fixtures/translation-coverage/README.md`.
