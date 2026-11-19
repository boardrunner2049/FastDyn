# Fall-through host inlining

`host-inline-splice@1` is a separate offline SuperCache v5 pass. Use the
[host-inline bootstrap](host-inline.md) to produce the baseline snapshot,
training profile, and matching QEMU and EnvLayout. The two pipelines share
callee discovery, exact image binding, the pinned BOLT bridge, and the
existing warm execution harness.

```sh
scopt optimize "$SNAP" --pipeline host-inline-splice \
  --profile "$W/profile/merged.profile.json" --store "$W/splice-store"
scopt validate "$SPLICE_ARTIFACT" --snapshot "$SNAP"
scopt validate "$SPLICE_ARTIFACT" --dynamic -c "$W/host-inline.toml" \
  --baseline "$SNAP" --qemu "$QEMU" --inputs "$W/heldout-0.bin" \
  --inputs "$W/heldout-1.bin" --out "$W/splice-v10"
```

## The `helper_rbit` rewrite

The hot Cortex-M3 translation block loaded the input into `edi` and called
through its constant pool. The compiled helper immediately copied `edi` into
`eax`, and the caller already consumed the result directly from `eax`.

```text
Original                      Spliced
mov edi, [rbp]                mov eax, [rbp]
call [rip + helper_slot]      bswap eax
                              ... bit-reversal arithmetic ...
mov ebx, [rbp + 8]            mov ebx, [rbp + 8]
add ebx, eax                  add ebx, eax
```

The spliced block executes the native helper instructions by fall-through.
The splice drops the native entry marker, first register copy, and return.
It retains the arithmetic and final register writes. Seven bytes of alignment
padding near the continuation preserve the patch-field and constant-pool
alignment. The two splices add 144 bytes of machine code versus 164 bytes for
the island variant. Each spliced call removes both island transfers.

## Admission and validation

The first pattern is a straight-line x86-64 SysV leaf taking one 32-bit integer
in `edi` and returning a 32-bit integer in `eax`. An adjacent single load must
supply the argument, and the helper must copy it into `eax` before its next
use. The proof checks that the inlined body overwrites any changed `edi`
state before it can be read or observed. It preserves architecturally defined
register and flag effects. For example, AF remains architecturally undefined
after a final XOR. Existing native-helper restrictions and stack assumptions
are described in [host-inline](host-inline.md).

The pipeline defaults to one splice per TB. Set the pass parameter
`max_sites_per_unit` to 2 to allow two disjoint integer splices bound to the
same original TB. Each splice must fit wholly inside one captured guest
instruction. The pair uses one cumulative offset map for code and capture.
If the pair requires branch widening or contains a framed callback, the
second site is refused and the accepted first site is retained.

The checker refuses incoming transfers or data references into replaced
bytes, missing guest or callee provenance, recovery
dependencies, and unsupported displacements. Integer splices can widen up
to eight captured short Jcc or JMP instructions whose internal targets no
longer fit in rel8. Each widening uses the same near-branch condition and
target plus NOP padding, adding eight aligned bytes. The checker excludes
branches with unsupported encodings or ambiguous sites and references into a
widened instruction's interior or padding. Physical offset changes also
update captured sites, jump reset offsets, literal-pool users, slow-path
returns, and the guest recovery search table.

Derived SCM v4 records carry the original binding and splice evidence. V24
checks the exact linked bytes, original helper identity, and physical offset
reconstruction. Reimport independently checks the code and metadata before
it exposes a transformed TB. V10 checks guest output and cache reuse under
warm execution. The original island pipeline retains its existing format and
validation path.

## Reproduce the two-RBIT canary

The checked-in [two-RBIT fixture](../fixtures/host-inline-two-rbit/) adds a
second RBIT on guest register r3. The guest SRAM store between the RBITs
makes each native helper receive an adjacent load from its own CPU-state
field. The same arithmetic oracle checks r0 for both configurations.

From the FastDyn root with the scopt Python environment active, build the
[pinned bridge](../native/README.md) and set `SCOPT_HOST_INLINE_BRIDGE` to the
resulting executable. Use a fresh work directory.

```sh
W=/opt/host-inline-two-rbit-run
QEMU="$PWD/third_party/common/qemu/build/qemu-system-arm"
python tools/scopt/scripts/bootstrap_host_inline.py \
  --source-lock tools/scopt/fixtures/host-inline/source-lock.json \
  --fixture host-inline-two-rbit --work-dir "$W"
export SCOPT_ENVLAYOUT_PATH="$W/envlayouts"
SNAP=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot"])' "$W/baseline.json")
PROFILE="$W/profile/merged.profile.json"

printf '%s\n' '{"host-inline-splice":{"max_sites_per_unit":2}}' > "$W/two-sites.json"
scopt optimize "$SNAP" --pipeline host-inline-splice --profile "$PROFILE" \
  --store "$W/one-site" --json > "$W/one-site.json"
scopt optimize "$SNAP" --pipeline host-inline-splice --profile "$PROFILE" \
  --params "$W/two-sites.json" --store "$W/two-site" --json > "$W/two-site.json"
ONE=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["artifact_dir"])' "$W/one-site.json")
TWO=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["artifact_dir"])' "$W/two-site.json")

for arm in one two; do
  case "$arm" in one) artifact=$ONE ;; two) artifact=$TWO ;; esac
  scopt validate "$artifact" --snapshot "$SNAP" --dynamic -c "$W/host-inline.toml" \
    --baseline "$SNAP" --qemu "$QEMU" --inputs "$W/heldout-0.bin" \
    --inputs "$W/heldout-1.bin" --out "$W/$arm-v10"
done

scopt run -c "$W/prep/run.toml" --qemu "$QEMU" --warm "$SNAP" \
  --arms warm,one-splice,two-splice --artifact "one-splice=$ONE" \
  --artifact "two-splice=$TWO" --reference one-splice --set heldout \
  --inputs "$W/heldout-0.bin" --inputs "$W/heldout-1.bin" \
  --reps 5 --seed 1 --out "$W/campaign"
```

Pass `--reuse-build /path/to/build-evidence.json` to the bootstrap when the
same checkout already has verified source-locked binaries. The bootstrap
checks their digests before capturing.

The optimize JSON files identify the artifacts and record each applied
site. The reference run captured two eligible sites in each of two TBs.
V10 results are in `$W/one-v10/dynamic.json` and `$W/two-v10/dynamic.json`.
The campaign writes raw samples to `$W/campaign/runs.jsonl` and paired
comparisons to `$W/campaign/report.md` and `report.json`.

## Warm timing

The four-arm campaign used the two held-out inputs with six seeded,
interleaved repetitions per arm and input. All 48 runs matched the arithmetic
oracle, passed cache-use checks, and appended zero TBs. Times are warm
firmware medians measured under Rosetta on Apple Silicon.

| Held-out input | Warm baseline | Identity | Island | Splice |
| --- | ---: | ---: | ---: | ---: |
| 0 | 758.17 µs | 751.65 µs | 717.98 µs | 710.60 µs |
| 1 | 723.62 µs | 725.75 µs | 730.77 µs | 683.62 µs |
| Pooled | 745.17 µs | 739.31 µs | 724.69 µs | 685.94 µs |

The median paired splice time was 5.95% lower than warm (MAD 4.07 percentage
points) and 4.51% lower than island (MAD 2.00 percentage points). Individual
pairs ranged from 19.01% faster to 4.29% slower versus warm and from 36.07%
faster to 7.28% slower versus island. Deployment preparation took about 0.475
seconds per run, outside the firmware timing interval. No native physical
x86-64 timing was collected.

```sh
python tools/scopt/scripts/measure_host_inline.py --baseline "$W" \
  --inline-artifact "$ISLAND_ARTIFACT" --inline-v10 "$W/inline-v10/dynamic.json" \
  --splice-artifact "$SPLICE_ARTIFACT" --splice-v10 "$W/splice-v10/dynamic.json" \
  --out "$W/splice-performance" --repetitions 6 --seed 20261118
```
