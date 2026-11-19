# LLVM cache-directed layout

Compare the current Pettis–Hansen order with LLVM 20.1.2 on the same snapshot
and exact steady-state profile. Build the [native bridge](../native/README.md)
and set `SCOPT_LLVM_LAYOUT_BRIDGE` to its executable path.

```sh
scopt optimize "$SNAPSHOT" --pipeline b2-layout --profile "$PROFILE" --store "$BASE_STORE"
scopt optimize "$SNAPSHOT" --pipeline b2-llvm-layout --profile "$PROFILE" --store "$LLVM_STORE"
scopt optimize "$SNAPSHOT" --pipeline b2-llvm-layout --disable llvm-cache-layout \
  --profile "$PROFILE" --store "$DISABLED_STORE"
```

The two pipelines share hot/cold classification, section policy, and the
checked native linker. LLVM orders only movable hot TBs with exact counts.
The request uses aligned arena footprints and captured offsets from native
code entry. The TB header displacement is constant, so using each code
entry as the model's origin preserves source-to-target distances in a
packed run. Pinned TBs and gaps can change final distances.

An exact unflagged `chain0` or `chain1` observation uses the captured
`goto_tb` opcode offset for that slot. A `lookup_hit` uses the captured
`goto_ptr` opcode offset if the source has exactly one such site. The adapter
drops ambiguous or missing sites and retains each distinguishable offset
when two exits lead to the same destination. One missing edge does not
discard an otherwise usable graph. A graph with no positive eligible edge,
a missing bridge, or an invalid bridge response uses the existing
Pettis–Hansen order. The pass reports the LLVM or fallback outcome, and the
normal B2 sections keep unknown execution counts separate from measured
zeroes.

Cache-Directed Sort models locality of transfers that the TBs already execute.
LLVM ExtTSP rewards fallthrough and fixes the first block of a function CFG.
The cache graph consists of independently entered TBs whose terminal transfers
remain in place, so this pipeline selects Cache-Directed Sort. ExtTSP is
applicable to a future region that can materialize and validate actual
fallthroughs.
[LLVM CodeLayout implementation](https://github.com/llvm/llvm-project/blob/llvmorg-20.1.2/llvm/lib/Transforms/Utils/CodeLayout.cpp).

Use `scopt validate --dynamic` to reload each artifact and check behavior and
zero cache appends, then compare fixed or held-out samples through the regular
interleaved harness.

```sh
scopt run -c "$CFG" --qemu "$QEMU" --warm "$SNAPSHOT" \
  --arms warm,pettis-hansen,layout --artifact pettis-hansen="$BASE_ARTIFACT" \
  --artifact layout="$LLVM_ARTIFACT" --reference pettis-hansen \
  --ablation llvm-cache-layout="$DISABLED_ARTIFACT" --reps 10 --seed 1 --out "$OUT"
```

The locality sidecar reports estimated placement metrics from the same
profile. Compare code footprints independently of packing; a layout-only
edit does not remove instructions.

## Placement after B3

`global-region-llvm` runs the same semantic passes as `global-region` and
then classifies and orders the source units. Both accept an imported v5
snapshot with previously certified integer splices. When reimporting a splice
artifact, pass every `cache*.scm` capture using a separate `--scm` option.
Use the same attached training profile and runtime identity for both pipelines.

The order uses emitted guard sizes. A pre-rewrite lookup count on a guarded
source can represent a guarded transfer or a fallback, so the layout graph
omits that ambiguous count. The V2 linker checks the composed placement,
emits fixed-up TBs, then places variants and builds the extension table at the
final offsets. An invalid combined placement rejects the candidate. Reimport
checks relocated fixups, recovery coordinates, and any retained splice proof.
