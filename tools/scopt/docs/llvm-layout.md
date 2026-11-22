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

The layout preview links the guarded bodies before constructing the request.
It reads each generated slot's jump offset and each fallback's `jmp rax`
offset from the emitted guard and binds the slot number to its continuation
in the manager's exit plan. With a single original lookup tail, a baseline
lookup to a destination whose within-page offset differs from every guarded
PC must reach the relocated fallback. The graph assigns its full source-profile
count there. Ambiguous tails and possible virtual-address aliases remain
unlocated. A profile of the same transformed generation can attribute actual
lookup-helper hits to the unique emitted `goto_ptr` site.

The `llvm_traffic_*` statistics partition the exact, unflagged edge weight in
the selected kinds of the attached profile view. They include weight outside
the selected nodes, omitted self-loops, unknown sites, proven fallback routes
and ambiguous guarded traffic. `llvm_traffic_total` is the denominator for
this view. An aggregate source profile cannot identify every first installation,
guard failure, or private-copy entry.

An explicit `llvm-cache-layout.estimate_general_reuse = true` experiment models
a GENERAL target with a unique matching within-page offset and continuation
identity. It assumes passing guards and a stable link after first use. It
reserves up to one installation per recorded training replay and gives the
remaining hits to the actual slot jump. `llvm_traffic_estimated_general` and
`llvm_traffic_estimated_install` report these estimates separately. Resets,
interruptions, failed predicates or link invalidation can change both counts.
The ordinary layout mode keeps this traffic unresolved. Timing on reserved
application scenarios and diagnostics on the transformed deployment remain
necessary to evaluate the model.

For kept SPEC variants, source-profile entries and outgoing edges of the
cloned body combine ordinary and private executions. LLVM omits nodes whose
exact count would require splitting these executions. Their weight appears
under `private_source_unlocated` or `private_destination_unlocated`. The
optional `order_private_by_predecessor = true` parameter on `cluster-hot`
or `llvm-cache-layout` places the private suffix in increasing final
predecessor address and slot order. Variants retain their stable identifiers.
The V2 linker rebuilds the extension indices and relocations for that physical
order. All ordinary placement and recovery checks still run. Interleaved
ordinary/private placement and exact private-entry traces remain open.
