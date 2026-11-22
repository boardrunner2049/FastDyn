# Real-application results

The release-image editor and flight missions have executed on ARM guest firmware. Each result below identifies its guest, cache state and application oracle. The archived synthetic benchmarks have no weight in these comparisons.

| Comparison | Verified outcome |
|---|---|
| Pye guest relocation on stock QEMU, cacheless training edit | All 24 measured pairs passed the document oracle. Median paired operational speedup was 1.012× (95% interval 0.989–1.023×). [Absolute times, CPU costs and raw pairs](pye-stock-training/) |
| Pye ordinary and transformed cache preparation | The original-image two-sweep diagnostic materialized ordinary, B3, SFB and SFB→B3. The held-out ranking warmup added six TBs and stopped the comparison. A separate joint-image preparation preserved a successful zero-append FirmBOLT training replay and a failed original zero-append check. [Original registry](pye-original-known-inventory/) · [Joint preparation](pye-joint-preparation/) |
| ArduCopter and PX4 on their existing FastDyn ports | Each passed a cold mission, a cache population mission and eight cache replays. The eighth replay added 692 and 1,395 blocks respectively. Both applications reproduced the mission using a sealed, hashed initial inventory. [Raw-checked mission and cache receipts](../../applications/flight/evidence/hosted-qualification.json) |
| grblHAL and ESCape32 | grblHAL completed the drawing on the upstream simulator. The ARM controller session still needs board I/O validation. Both ESCape32 release inputs stopped at the STM32G071 PLL-ready poll. [grblHAL](../../applications/grblhal/) · [ESCape32](../../applications/escape32/) |

The [work-package ledger](work-packages.md) maps all 18 sections to implementation, validation, remaining gaps and measurements. The [host site census](pye-host-sites/) gives physical execution bounds for individual calls and padding. [Provenance](provenance.json) records the pinned components and hashes. The [application adapters](../../applications/README.md) contain the reproduction commands.
