# Global edge specialization

`global-specialization` computes a capture-derived program graph and a serial
transformation plan. Unit nodes keep the full translation key and a content
hash. Native host code, capture metadata, and the bound helper-effect table
supply local summaries. The full host CFG includes load/store slow paths.
Missing metadata and unresolved transfers keep a conservative effect and
possible reentry. A profile can rank targets and provision guards. The
availability and frequency of a profile edge cannot prove a value or exclude
an unprofiled target.

The byte-transfer domain represents constants, preserved bytes, and
unknown bytes. The SCC fixed point retains equal known bits at merges.
For partial fields the ledger reports fixed-width signed and unsigned bounds
implied by the mask. These bounds are advisory and cannot authorize a
rewrite or a private entry. All partial-mode
general entries start unknown. Complete mode starts at verified coverage
roots and the residual domain, and unresolved reachable transfers retain
possible reentry. General entries keep their baseline behavior in both modes.
Only a proved direct transfer can carry exact facts into a general entry.
The planning report includes separate contexts for proposed private entries.

The transformation admits the existing guarded off-page static lookup tail.
A source-local fact can authorize a specialized continuation after V14
rechecks it on the emitted slot. A bounded three-TB specialization can also
use a modifier-defined `r0 = 0` from A when B preserves the bytes to its
lookup tail. B adds a checked `r0 == 0` guard before C's private slot.
The original lookup remains available when the key or value guard fails.
The general C entry retains its original instructions. This guarded case
does not require that every entry of B came from A. The native validator
checks all admitted paths and the reimported artifact retains the
correspondence and recovery metadata. This implementation has one
environment value predicate and does not infer arbitrary multi-hop closure.

The planner assigns all decisions for a predecessor to one serial partition.
Choose `partitions` from 1 through 64. Plan and summary digests include the
source generation, dependency hashes, coverage selection, and growth
budget. Partition count affects recorded pipeline provenance. It does not
alter the content of the global decisions or the emitted cache when the
same decisions fit.

## Reproduce the guarded fixture

Run from `tools/scopt` inside the matching Ubuntu container. This example
uses Bash and a fresh absolute work directory.

```sh
python="$PWD/../../scopt-env/bin/python"
qemu="$PWD/../../third_party/common/qemu/build/qemu-system-arm"
plugin="$PWD/../../build/libfastdyn.so"
work=/opt/whopr-transitive-run
"$python" scripts/bootstrap_global_specialization.py \
  --fixture global-transitive --work-dir "$work" --qemu "$qemu" --plugin "$plugin"
export FASTDYN_ROOT="$(realpath ../..)" SCOPT_FASTDYN_SRC="$(realpath ../..)"
export SCOPT_ENVLAYOUT_PATH="$work/envlayouts"
hash="$(sha256sum "$qemu" | cut -d' ' -f1)"
export SCOPT_V6_EXES="$hash"
mkdir "$work/effects"
export SCOPT_HELPER_EFFECTS_PATH="$work/effects"
"$python" -m scopt helper-effects rebind \
  --from-exe-sha 56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90 \
  --exe "$qemu" --out "$work/effects/$hash.json"
"$python" scripts/hostsym_qemu_oracle.py --out "$work/hostsym-qemu.json"
cat > "$work/params.json" <<'JSON'
{"specialize-edges":{"partitions":1,"max_code_growth_bytes":65536,"min_pass_share":0.45}}
JSON
"$python" -m scopt optimize "$work"/store/snapshots/* \
  --pipeline global-specialization --store "$work/optimized" \
  --profile "$work/profile/merged.profile.json" --runtime-identity "$work/runtime-identity.v2.json" \
  --prep "$work/prep" --elf "$work/guest.elf" --v6-exe "$hash" \
  --selftest "$work/hostsym-qemu.json" --params "$work/params.json"
"$python" -m scopt validate "$work"/optimized/artifacts/* --dynamic \
  -c "$work/host-inline.toml" --qemu "$qemu" --baseline "$work"/store/snapshots/* \
  --out "$work/optimized-v10" --inputs "$work/heldout-0.bin" --inputs "$work/heldout-1.bin"
```

Omit the QEMU oracle script and `--selftest` on a native x86-64 host that
passes V18. The independent oracle script requires Ubuntu's `qemu-user`
package. It records executable identity and command, and uses the existing
V18 test programs with separate generated-code addresses for each form.
The VirtualApple execution host failed the V18 AF checks, including a
preserving MOV. The separately labeled QEMU user-mode results leave that
hardware result visible.

The fixture-specific profitability threshold accounts for 4,095 qualifying
writer-edge observations out of 8,192 lookup observations. This threshold
has no role in the proof. The alternate predecessor enters the same B
translation key with a nonzero r0 and follows C's general entry. Held-out
oracles are `0x1000` for the modifier path and `0x3000` for the fallback.
Use `coverage_mode: "complete"` and `coverage_evidence` in the pass parameters
only with a matching producer record. An explicit complete request fails
if the record is absent or stale. `auto` records the reason for partial
admission. No code is deleted on the basis of graph reachability.

## Observer-aware store elimination in private variants

`region-state-dse` runs after the branch and scalar passes in the region
pipelines. It runs the existing byte-level `syncdead` solver on the effective
private CFG and the general CFG under the same executable-bound helper
effects. It removes a plain D1 store only if it can be observed in the
general body and every path from the store in the private body overwrites
the relevant bytes before an observer. A helper's possible write is not a
definite overwrite. A read, a memory or MMIO event, a possible helper fault,
or a dispatcher exit can retain the original store. The pass refuses edits
when the full and atomic CFGs differ. The general TB, lookup fallbacks and
guest recovery metadata stay in their existing forms. V12 checks behavior
under the separately verified entry facts, and V15 checks placement and the
store-to-NOP template.

For the dedicated fixture, use the preceding commands with
`--fixture region-dse` in a fresh work directory, pipeline
`global-region`, and the two output arms shown below. The conditional
overwrite assigns r3=7 for the admitted modified path. On the same-key
general path, the original r3=5 can reach the accumulator. Independent
held-out results are `0xe0000` and `0xa0000` after 131,072 iterations.
Keep the full optimization arguments and use only the indicated ablation
to compare the effect of this pass.

```sh
for arm in dse-on dse-off; do
    dirs="$work/$arm"
    mkdir "$dirs"
    flags=()
    if [[ $arm == dse-off ]]; then flags=(--disable region-state-dse); fi
    "$python" -m scopt optimize "$work"/store/snapshots/* \
      --pipeline global-region --store "$dirs" --profile "$work/profile/merged.profile.json" \
      --runtime-identity "$work/runtime-identity.v2.json" --prep "$work/prep" \
      --elf "$work/guest.elf" --v6-exe "$hash" --selftest "$work/hostsym-qemu.json" \
      --params "$work/params.json" "${flags[@]}" --json > "$dirs/result.json"
    "$python" -m scopt validate "$dirs"/artifacts/* --dynamic \
      -c "$work/host-inline.toml" --qemu "$qemu" --baseline "$work"/store/snapshots/* \
      --out "$dirs/v10" --inputs "$work/heldout-0.bin" --inputs "$work/heldout-1.bin"
done
"$python" scripts/measure_program.py "$work" "$work/dse-timing" \
  --arm dse-on="$work/dse-on" --arm dse-off="$work/dse-off"
```
