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

There is at most one splice per TB. The splice must fit wholly inside one
captured guest instruction. The checker refuses incoming transfers or data
references into replaced bytes, missing guest or callee provenance, recovery
dependencies, and displacements that no longer fit. Physical offset changes
also update captured sites, jump reset offsets, literal-pool users, slow-path
returns, and the guest recovery search table.

Derived SCM v4 records carry the original binding and splice evidence. V24
checks the exact linked bytes, original helper identity, and physical offset
reconstruction. Reimport independently checks the code and metadata before
it exposes a transformed TB. V10 checks guest output and cache reuse under
warm execution. The original island pipeline retains its existing format and
validation path.

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
