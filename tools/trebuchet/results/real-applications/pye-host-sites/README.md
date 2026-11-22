# Physical calls and NOPs in the Pye training edit

The original training ELF has SHA-256 prefix `e5b58cf9`. The FirmBOLT training ELF is `caab33a7`, the earlier narrow image with identical load segments to the later metadata-corrected canonical ELF `d38a3d05`. The raw receipts retain the actual image identities. [Load-segment comparison](../../../applications/pye/qualification/firmbolt-canonical/load-equivalence.json)

Both profiles cover reset through the editor’s return on the same queued UART task, `update-package-version`. The reached `steady` marker is Reset_Handler at 0x20d50 and starts at dispatch zero. It includes interpreter startup and UART polling. The separate marker-bounded guest trace identifies the narrower operational edit window. The original-image fully warm gate in this joint experiment failed with three appended translations. The per-image profiles were collected in separate diagnostic runs and passed their producer checks. [Joint preparation](../pye-joint-preparation/campaign.json) · [Operational-window attribution](../../../applications/pye/qualification/editor-trace/attribution.json)

| Training measure | Original guest | FirmBOLT guest |
|---|---:|---:|
| Imported ordinary TBs, all physical spans checked | 3,855 | 3,837 |
| Profile dispatches, including profile-only units | 1,805,044 | 1,822,393 |
| Additional profile-only units | 4 | 2 |
| Physical host CALL sites | 8,417 | 8,393 |
| CALL sites with a proven per-dispatch upper bound | 8,417 | 8,393 |
| Sum of execution bounds across that subset | 2,165,697 | 2,192,359 |
| Exact unflagged in-domain P3 lookup-hit events | 203,713 | 216,872 |
| Physical NOP instructions | 12,405 | 12,379 |
| Static NOP bytes | 15,967 | 15,933 |
| NOP sites with a proven per-dispatch upper bound | 3,551 | 3,537 |
| Sum of byte-execution bounds across that subset | 4,410,414 | 4,414,721 |
| Sites with NOPs proved mandatory for recorded unflagged slot exits | 2,309 | 2,313 |
| Minimum NOP instruction executions at those sites | 1,196,386 | 1,195,682 |
| Minimum NOP byte-executions at those sites | 2,299,579 | 2,299,504 |
| Sites where both ends of the exit-derived execution interval coincide | 2,238 | 2,241 |

A byte-execution charges the encoded length of a visited instruction once. The subset sums omit sites whose count is unknown and the lower-bound and upper-bound subsets overlap. P3 runs inside the native lookup helper. Its counted lookup events give a minimum on calls across the full in-domain observation. These counters establish execution volume. Elapsed cost and I-cache traffic require separate measurements.

The largest proved contribution is a three-byte NOP at guest TB PC 0xe40, host offset 96. Its interval is 279,567–279,571 visits on the original image and 279,567–279,576 on FirmBOLT. The following jump has an aligned mutable rel32 field at offset 100. Deleting those three bytes without compensating layout would unalign the atomic field. At guest PC 0xde4 the alternate exit illustrates why TB counts mislead. The original entered the TB 279,580 times, while the NOP before slot 1 has only 4,169 guaranteed visits. Each row in the compressed census carries its physical identity, encoded bytes, evidence class and applicable interval.

The original exposes nine captured unsigned-division helper CALL sites and FirmBOLT exposes twelve. Removing each CALL from the verified full host CFG shows which observed chain exits require that CALL. Summing those proven visits and the independent per-unit upper bounds gives 14,680–14,683 original and 14,680–14,681 FirmBOLT calls. In the original, guest PC 0xe8fe contributes 8,525–8,528, 0xe748 contributes exactly 4,186 and 0xe894 contributes exactly 1,725. These three sites occupy distinct TBs. The selective one- and two-site register-only recipes disable faulting helpers. The broader native stage has a separate UDIV path whose eligibility requires a matching helper identity and semantics. The identical archived QEMU ELF bounds its unsigned-division function at 107 bytes and 35 linearly decoded instructions. The lookup function occupies 572 bytes and 142 instructions. Neither footprint gives instructions retired per call. These files contain ordinary code. Private NOPs, weighted private encoding refusals and post-splice growth remain unmeasured.

An independent original-only campaign recorded 3,834 ordinary units and eight profile-only additions. The training replay passed the zero-append gate. The subsequent ranking reference on a different document appended six units, so ranking stopped. Its offline training census bounds UDIV at 14,679–14,682 executions, detects 8,386 physical CALL sites and proves at least 1,218,612 NOP executions on observed exits. This separate capture is excluded from the two-image table above. [Original-only candidate receipts](../pye-original-known-inventory/README.md) · [Original-only raw census](original-only-census.json.gz)

## Reproduction

Download the three evidence parts and checksum inventory for Actions run 44981162436, verify the archive checksums and extract sparsely using the Pye instructions. The original artifacts in this workspace are under `/workspace/trebuchet-work/runner/44981162436/`. The following commands use that extraction. Off-host verification compares cache and search bytes, SCM hashes, complete per-unit physical regions, event maps, exits and recovery. Original live-host symbol and relocation annotations differed on 1,591 original and 1,590 FirmBOLT units. The read-only analyzer bound named QEMU helper footprints to the exported executable digest, captured runtime identity, memory map and unique sized symbol. It emits no transformed artifact. [Export procedure](../../../applications/pye/campaign.md) · [Census method](../../../lib/site_census.md)

```sh
offline=/workspace/trebuchet-work/runner/44981162436/offline
export SCOPT_ENVLAYOUT_PATH="$offline/campaign/envlayout"
export SCOPT_HELPER_EFFECTS_PATH="$offline/campaign/helper-effects"
for img in original firmbolt; do
    run="$offline/campaign/$img"
    elf="$offline/product/micropython/ports/qemu/build-MPS2_AN385/firmware.elf"
    if [ "$img" = firmbolt ]; then elf="$offline/campaign/firmbolt-guest/firmware.elf"; fi
    scopt-env/bin/python tools/trebuchet/lib/site_census.py \
        --archived-ordinary --archived-qemu "$offline/qemu-system-arm" \
        --snapshot "$run"/store/snapshots/* --profile "$run/profile/merged.profile.json" \
        --elf "$elf" --application pye --scenario update-package-version --phase steady \
        --out "$img-census.json"
done
```

To reproduce the separate original-only census, use the export under `/workspace/trebuchet-work/runner/pye-campaign/attempt-1/offline` with `img=original` and the same command.

[Original guest raw census](original-census.json.gz) · [FirmBOLT guest raw census](firmbolt-census.json.gz)
