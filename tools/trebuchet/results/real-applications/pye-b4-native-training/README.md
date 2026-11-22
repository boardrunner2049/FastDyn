# Original Pye B4 and guarded UDIV training checks

The previous B4 proposal removed a four-byte store to `neg.can_do_io` at host offset 18. V12 compares the entire affected TB, including a later `movzx ebx,bh` instruction at offset 0x90. On the archived release Pye TB, scopt at `16790d6` returns exactly the recorded inconclusive verdict for the high-byte register. The corrected kernel proves both original and candidate paths equivalent on the same bytes. Changing the source operand from BH to BL in a deliberate mutant changes guest register state at the lookup helper and the corrected kernel rejects it.

The isolated reproduction verifies the archive’s physical region fields and source code hash. One snapshot contains 3,834 units. Reopening it on another machine changed 1,596 path-dependent host annotations. The witness performs an isolated symbolic check. Deployment qualification uses fresh captures and bindings on the host that built and ran QEMU.

Reproduce the single-unit checks using the extracted original training bundle. Execute the same script in each pinned scopt environment.

```sh
python tools/trebuchet/applications/pye/b4_witness.py "$offline" witness.json
```

The supplemental application experiment uses only the published release configuration and `update-package-version` training job. It attempts the regular B4 package and the `host-inline-splice` pipeline with `mode=splice`, `allow_faulting=true` and `udiv_mode=guarded`. The guarded form executes the checked nonzero divide path locally and retains the original call for zero operands. Each changed artifact must reimport with ordinary ancestry and pass a fresh checked training replay with exact full-document output, normal exit and zero appended TBs.

On a prepared Linux host, set `SCOPT_HOST_INLINE_BRIDGE` to the checked LLVM/BOLT 20.1.2 scopt bridge. Build the PSRAM-corrected native runtime and product, then run the bounded qualification. The application runner and scopt Python environment belong to the same checkout.

```sh
app=tools/trebuchet/applications/pye
WARM_BUILD_ONLY=1 "$app/warm-smoke.sh" "$work" "$build_results"
"$app/prepare.sh" fetch "$work/product"
"$app/prepare.sh" build "$work/product"
PATH="$PWD/fastdyn-env/bin:$PATH" taskset -c "$dedicated_cpu" \
    scopt-env/bin/python "$app/qualify_b4_native.py" "$work" "$output"
```

## Hosted results

B4 published and reimported a generation removing 3,421 stores across 2,555 TBs. The 66 D1 and 3,355 D2 stores occupied 13,894 source instruction bytes and were replaced with same-width NOPs. V12 passed for the submitted generation. Publication and reimport took 83.50 seconds. [Summary](training-summary.json)

Native splicing published and reimported seven guarded UDIV sites across seven TBs. V24 verified native correspondence at all seven. Each original six-byte pool-indirect CALL became a 38-byte sequence. The nonzero path performs the verified unsigned divide inline, skips the call, and avoids the helper return. The zero path reaches the original six CALL bytes, moved forward 32 bytes. It preserves the original helper’s ability to return zero or raise on divide by zero. The physical CALL count across these spans remains seven, all on fallback paths. The aggregate emitted growth was 224 bytes. Publication and reimport took 95.30 seconds. [Certificates and complete stage receipts](raw.tgz)

The earlier training census proved 14,679–14,682 dynamic source-site visits across these seven locations by combining completed exit counts, host control flow and TB entries. Its bound for the splice at guest `0xe8fe` was 8,525–8,527. The fresh supplemental profile separately counted 14,681 entries into the seven source TBs, including 8,526 into the hottest TB. For that TB it recorded 8,460 chain-slot-0 exits, 65 chain-slot-1 exits and one requested exit. The emitted target at `0xe8fe` moves its CALL from host offset 90 to 122. Operand values, zero-fallback hits, actual nonzero fast-path hits and hardware instruction/cache counters were unmeasured in the transformed launch. The number of calls avoided depends on the divisor values in the transformed run. [Per-site bounds and footprint](training-summary.json)

All three training replays returned the identical, expected 166-byte document. QEMU exited normally, and the cache proof passed. Preparation reached its eight-sweep cap. The ordinary, B4 and native replays respectively appended four, two and two TBs, so all three failed fully warm qualification. No ranking, final scenario or performance comparison ran. [Raw training data](raw.tgz)
