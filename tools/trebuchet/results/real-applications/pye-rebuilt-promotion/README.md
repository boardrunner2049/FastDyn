# Reserved promotion edit on rebuilt deployments

The current original-image SFB→B3 recipe did not pass the predeclared promotion gate against historical host-cache SFB→B3 on the canonical FirmBOLT guest. The operator corrected the malformed shebang of the upstream packaging script. Each arm won six of the 12 pairs, with a one-sided balanced-order p-value of 0.7165. The 1% practical improvement threshold and the 0.05 significance threshold were both missed.

| Metric | Historical host cache, FirmBOLT guest | Current host cache, original guest | Median paired gain [95% bootstrap interval] |
| --- | ---: | ---: | ---: |
| Editing latency | 16.787 ms | 18.041 ms | 0.999× [0.926, 1.022] |
| QEMU lifetime | 56.383 ms | 56.625 ms | 0.997× [0.968, 1.034] |
| Launch wall time | 233.653 ms | 232.944 ms | 1.004× [0.999, 1.008] |
| Whole process-tree CPU | 220.854 ms | 219.976 ms | 1.004× [0.997, 1.010] |
| Initial records per fresh deployment | 3,511 | 3,513 | |
| Added translations per launch, median [range] | 54 [51, 58] | 53 [50, 57] | |

Absolute values are medians of each arm. The gain and interval use within-pair ratios. Each rebuilt deployment used an independently captured and profiled training-only inventory on one host. All 24 scored runs passed the five cache identity checks, exact initial/final append accounting, and complete UART and returned-document validation. The two frozen recipe choices came from earlier independent experiments. This comparison used the single reserved promotion document, with one additional warmup pair. It performed no ranking or final evaluation.

The historical selector qualified six of seven old host-cache recipes on the newer repaired FirmBOLT guest. This experiment compared rebuilt deployment identities and does not establish an end-to-end historical toolchain result. The earlier strict promotion decision remains unchanged.

[Frozen prior identities](policy.json) · [Rebuilt arms](arms.json) · [Paired results](comparison.json) · [Verified per-arm costs and translations](verified-trials.json) · [Complete hosted case declarations, matching ROMFS inputs, keys and expected documents](inputs.tgz) · [Raw trial proofs and UART outputs](receipts.tgz)
