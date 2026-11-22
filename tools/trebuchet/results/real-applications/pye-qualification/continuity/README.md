# Host register continuity on the Pye training pair

The original and narrow FirmBOLT training profiles each recorded 279,567 unflagged chain-slot-0 transitions from guest 0xe40 to 0xde4 in `qstr_find_strn`. The captured producer writes guest r5 from host r13d at offset 79. From there to the mutable jump at 99 it updates the I/O flag and guest PC while preserving r13d and the stored r5. The successor reloads r5 into ebx at offset 22 after the ordinary icount check. The prefix uses ebx as scratch and preserves r13d.

The diagnostic emits a private-entry replacement `mov ebx,r13d` (44 89 eb) for `mov ebx,[rbp+20]` (8b 5d 14). It keeps the predecessor store for IRQ, fault and dispatcher observations. The public entry still reloads from memory even when its incoming r13 is stale. The early icount exit precedes the replacement on both paths. The replacement removes one CPU-state read per successful private traversal with identical encoded length. The profile counts observed exits and supplies an upper bound for eligible traversals. It does not distinguish installed jumps from dispatch fallback.

The x86-64 execution harness uses the physically captured suffix and prefix and runs 42 comparisons per image, covering normal and early exits, general-register results, flags, CPU state and independent entry. The recorded SHA-256 values bind the original public unit, the same-address private prototype and its training capture. The output JSON records the two physical instruction encodings and the exact immutable input hashes.

A deployable transformation still requires a private successor installed exclusively after a validated direct chain. On reset, unchain, invalidation, or dispatcher re-entry, execution must reach the ordinary public target. Existing SPEC guards cover lookup tails. An ordinary mutable slot requires its own binding and relocation proof. This experiment ran outside the frozen recipe registry. No elapsed-time comparison or new guest performance arm was recorded.

Run from the FastDyn root with the exported training campaign:

```sh
export SCOPT_ENVLAYOUT_PATH="$OFFLINE/campaign/envlayout"
export SCOPT_HELPER_EFFECTS_PATH="$OFFLINE/campaign/helper-effects"
python tools/trebuchet/lib/continuity_witness.py "$OFFLINE"/campaign/original/store/snapshots/* \
  "$OFFLINE/campaign/original/profile/merged.profile.json" \
  --guest-elf "$OFFLINE/product/micropython/ports/qemu/build-MPS2_AN385/firmware.elf" \
  --source 'tb:df6512343d9711338d8f856587c3bed1#0' --slot 0 \
  --destination 'tb:20f98f2677e3aad2e4f66e4fe5480c19#0' --out witness.json
```
