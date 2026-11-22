# Host register continuity on the Pye training pair

The original and narrow FirmBOLT training profiles each recorded 279,567 unflagged chain-slot-0 transitions from guest 0xe40 to 0xde4 in `qstr_find_strn`. The captured producer writes guest r5 from host r13d at offset 79. From there to the mutable jump at 99 it updates the I/O flag and guest PC while preserving r13d and the stored r5. The successor reloads r5 into ebx at offset 22 after the ordinary icount check. The prefix uses ebx as scratch and preserves r13d.

The diagnostic emits a private-entry replacement `mov ebx,r13d` (44 89 eb) for `mov ebx,[rbp+20]` (8b 5d 14). It keeps the predecessor store for IRQ, fault and dispatcher observations. The public entry still reloads from memory even when its incoming r13 is stale. The early icount exit precedes the replacement on both paths. The replacement removes one CPU-state read per successful private traversal with identical encoded length. The profile counts observed exits and supplies an upper bound for eligible traversals. It does not distinguish installed jumps from dispatch fallback.

The x86-64 execution harness uses the physically captured suffix and prefix and runs 42 comparisons per image, covering normal and early exits, general-register results, flags, CPU state and independent entry. The recorded SHA-256 values bind the original public unit, the same-address private prototype and its training capture. The output JSON records the two physical instruction encodings and the exact immutable input hashes.

The follow-up adds a direct-slot class to the SC6 loader. A private link can arm only at ordinary dispatch, after QEMU resolves the exact live source, ordinary chain slot, general destination TB and guest PC. The general destination owns the incoming link on QEMU’s normal invalidation list. The loader retains the private TB’s recovery registration, TLB and debug revocations, reset code and generic fallback. Dispatch, interrupt and other predecessor entries continue to use the public code.

The deployable packager reopens its input store strictly, checks the guest ELF, eligible host CFG and capture against the exact training edge, relocates the private clone, and checks both SC6 readers, public bytes, extension binding, runtime identity and deployment sidecar. Both images passed using fresh local imports of the archived ordinary caches and physical captures. The package also emits the public SCMs and a private variant record. Re-import independently reconstructs the relocated body plus its one declared patch and checks ordinary origin, recovery and relocation metadata across all TBs. The relocated endpoints’ complete region identities exactly matched the original hosted imports. Other units remained at their original offsets. Each clone reserves 368 arena bytes before any inter-record alignment, comprising a 192-byte TB structure, 160 bytes of host code and a 16-byte guest-instruction recovery map. It adds no instructions to the taken private path and uses the already live r13d until the replacement at offset 22. Both successor copies overwrite r13d at offset 28. The loader scans the new direct link at ordinary dispatch before patching the existing jump. These are training-only packages outside the frozen candidate registry. [Original package](original-package.json) · [FirmBOLT package](firmbolt-package.json)

The independent full-body harness executes the actual public and linked private 160-byte instruction streams. It first checks the complete reviewed instruction encoding, all three private return addresses and the reset jump encodings. All 125 comparisons per image agreed on the full CPU-state buffer, modified host registers, flags and normalized TB return index, covering both normal successors and the initial icount bailout. Independent public entry also passed with a stale incoming register. This proof exercises the two compiled TBs independently of the emulator’s dispatch decisions. [Original linked-body checks](original-linked-body.json) · [FirmBOLT linked-body checks](firmbolt-linked-body.json)

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

Build the deployable candidate in the source capture environment, then run the complete linked-body audit with the same Python environment:

```sh
python tools/trebuchet/lib/package_continuity.py "$SNAPSHOT" "$PROFILE" --guest-elf "$FIRMWARE" --out "$NEW_DIR"
python tools/trebuchet/lib/check_pye_private_body.py "$NEW_DIR/cache.sc"
```
