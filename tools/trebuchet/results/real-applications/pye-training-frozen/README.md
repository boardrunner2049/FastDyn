# Pye with frozen training-only preparation

The original release image with SFB followed by B3 won a search over eight qualified deployments. Confirmation on a separate edit won 12 of 12 paired blocks, with a median paired gain of 1.382× (one-sided p = 0.00108). The table shows the subsequent 12 paired trials proofreading the upstream editor manual. Each run started from a fresh private copy of the training-only cache, executed the real queued UART edit and returned the entire expected document.

| Held-out final metric | Original ordinary | Original SFB → B3 | Median paired gain [95% bootstrap interval] |
| --- | ---: | ---: | ---: |
| Editing latency | 56.395 ms | 35.803 ms | 1.527× [1.437, 1.579] |
| QEMU lifetime | 220.948 ms | 186.651 ms | 1.185× [1.152, 1.197] |
| Launch wall time | 402.8 ms | 370.6 ms | 1.085× [1.079, 1.098] |
| Whole process-tree CPU | 348.467 ms | 316.628 ms | |
| Late translations per launch, median [range] | 120 [112, 143] | 124.5 [117, 135] | |

Both final arms began with 3,509 cache records. The 216 scored observations passed an independent audit of their complete UART and returned-document bytes, cache prefix and manifest checks, initial record count and exact late-translation delta. The current registry took 569.3 seconds to materialize all eight deployments. The campaign took 1,343.8 seconds, including 538.4 seconds for historical materialization. The registry’s direct footprint lookup reported zero matches on the selected cache. The reimported singleton records and both relocation maps recover the public allocations for all 3,509 training-profile units.

The frozen strict historical rule required all seven recipes to materialize. The historical B4 validator explicitly refused a proposal. The timed result therefore establishes a gain against original ordinary execution on this application and preparation track. Release promotion and a suite gain remain unestablished. The fixed guest for the historical host-cache comparison used the corrected FirmBOLT emitter. The independent supplemental historical control has a separate policy.

[Predeclared frozen policy](policy.json) · [Candidate registry](registry.json) · [Frozen selection](selection.json) · [Final measurements](final.json) · [Verified late translations and CPU](verified-trials.json) · [Raw per-trial outputs and cache proofs](receipts.tgz) · [Archive checksums and hosted identity](hosted-receipt.json)

## Public code footprint from the training profile

The 354 hottest public units accounted for 1,623,250 of 1,803,465 exact entries in the ordinary training profile (90.007%). Six additional profiling-only entries had no ordinary cache record. Holding the 354 unit identities fixed gives the following allocated host-code ranges, including inline pools and guard tails. Each line and page is counted once per layout.

| Layout | Public host-code bytes | 64-byte lines | 4 KiB pages |
| --- | ---: | ---: | ---: |
| Original ordinary | 100,504 | 1,728 | 118 |
| Original SFB | 106,104 | 1,803 | 152 |
| Original SFB → B3 | 116,676 | 1,975 | 172 |

The selected layout reserves 10,572 bytes of appended guard tails across 77 of these units. Those bytes bound the allocated tail space available on the public paths. The profile has no selected-run guard-hit or private-entry counts. The 13 private variants contribute no bytes to this public-only comparison. Actual executed instructions, cache traffic and the held-out edit’s working set remain unmeasured.

To reproduce, extract the raw inputs and run `python public_footprint.py campaign` from their parent directory. The script verifies both snapshot ancestries, all unit identities, exact guest members and keys, slots and moved or retained TB offsets. The ordinary and SFB results also reproduce their independently computed registry footprints. [Calculated ranges and input hashes](public-footprint.json) · [Reproduction script](public_footprint.py) · [Archived reimports, sidecars and profile](public-footprint-inputs.tgz)
