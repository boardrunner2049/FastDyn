# Maximal certified host inlining

`host-inline-maximal` is the deliberately aggressive scopt host-inlining experiment.
It attempts every captured native call site, including cold sites, recursively expands
each complete supported wrapper chain to its certified terminal, enables audited
faulting helpers, compacts eligible frames, and publishes every proved edit even when
other calls remain unsupported.

It is opt-in and does not change the ordinary `host-inline`, `host-inline-splice`, or
`host-inline-extreme` pipelines.

```sh
scopt optimize "$SNAP" --pipeline host-inline-maximal \
  --profile "$PROFILE" --store "$OUT/maximal" --json
```

The equivalent selector is:

```sh
scopt optimize "$SNAP" --pipeline host-inline=maximal \
  --profile "$PROFILE" --store "$OUT/maximal-selector" --json
```

## What “maximal” means

The mode sets `max_inline_depth` to 2,147,483,647, but emitted depth is not
unbounded. Chain discovery is capture-bound, rejects cycles, validates the complete
chain, and stops at its proved terminal. The large number means “do not truncate a
certified acyclic chain,” not “recursively emit forever.”

The built-in configuration uses the existing audited ceilings:

| Control | Maximal setting | Meaning |
|---|---:|---|
| `max_callee_bytes` | 1,024 | Largest top-level callee considered. |
| `max_cfg_insns` | 128 | Existing leaf control-flow proof limit. |
| `max_sites` | 4,096 | Captured sites considered across the snapshot. |
| `max_sites_per_unit` | 2 | Current linker ceiling for compatible integer splices in one TB. |
| `max_growth_per_unit` | 65,536 bytes | Per-TB emitted-growth ceiling. |
| `max_total_growth` | 65,536 bytes | Whole-artifact emitted-growth ceiling. |
| `max_wall_s` | 60 seconds | Pass wall-time ceiling. |

Two independent calls in one TB are attempted. The linker currently accepts both only
when they are compatible integer splices without conflicting replaced ranges,
relocations, or branch relaxation. A frame plus another splice, two relocated frames,
or a third call remains a recorded refusal. Generalizing this is separate linker and
certificate work, not a parameter change.

`allow_partial=true` is intentional. Strict `extreme` rejects the entire candidate when
even one captured call remains. Maximal mode instead keeps all independently proved
edits and reports every unsupported site. Review `coverage.sites_seen`,
`fully_inlined_sites_applied`, `guarded_sites_applied`, `sites_uncovered`, each site’s
outcome, emitted growth, and the loaded artifact hash before timing it.

## What it does not do

This pass inlines native QEMU helpers and captured FastDyn/plugin callbacks into host
translation blocks. It does **not** turn the guest firmware into one host function.
Firmware calls are guest control flow split across translation blocks; their offline
counterparts are static chaining, return chaining, and guarded edge specialization.
Guest function-level flattening belongs in FirmBOLT.

Currently supported recursive callback chains are deliberately narrow:

- every wrapper is an exact SysV normal-return frame containing one direct call;
- the terminal is a certified register-only leaf or the audited timer form;
- all source functions and mappings match the captured executable; and
- stack phase, return-slot/CET assumptions, relocations, recovery, and external effects
  satisfy the existing validators.

Branching wrappers, multiple nested calls, indirect calls, recursion/cycles, unknown
effects, unsafe stack observation, missing function bounds, and arbitrary QEMU-helper
call graphs are retained or refused. Flattening those requires a new call-DAG
representation and proof, not weakening the eligibility checks.

## Measurement arms

Compare equivalent, separately published artifacts:

1. ordinary warm cache;
2. identity round trip;
3. `host-inline=splice,1-level`;
4. `host-inline=extreme,faulting,4-level` with the desired strict/partial control; and
5. `host-inline=maximal`.

Record removed and residual calls, actual maximum depth reached, affected TBs, emitted
host bytes, cache load/relocation time, and paired warm execution. The existing evidence
already shows why activation is not enough:

- one RBIT splice measured 1.138x [1.089, 1.204] against ordinary warm;
- two RBIT splices measured 1.139x [1.086, 1.219] against warm and 1.156x
  [1.069, 1.191] against one splice, with four sites and 288 added host bytes;
- the combined host-inline prefix was 0.998x and its ablation 1.004x; and
- the only measured depth-two frame comparison, compact versus original, was 1.010x
  [0.974, 1.068], inconclusive.

No checked-in performance result reaches recursion depth three. The local five-frame
activation fixture currently lacks the pinned `scopt-native` bridge, and the local PX4
caches append translations during replay, so neither supplies a valid maximal-mode
timing yet. Maximal mode is therefore an active correctness prototype awaiting valid
performance measurement, not a proposed default.
