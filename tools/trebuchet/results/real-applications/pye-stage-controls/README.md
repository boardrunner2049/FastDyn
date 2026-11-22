# B4 and guarded division on the Pye training edit

Each deployment started a fresh copy of its sealed cache and completed the same package-version edit on the original Pye guest. All 39 launches passed the cache proof and returned the exact 166-byte document. The trial plan balanced all six orders twice, with a separate unscored warmup. No held-out document was used.

| Metric | Ordinary | B4 | Guarded UDIV |
|---|---:|---:|---:|
| Median edit latency (ms) | 10.659 | 11.216 | 10.787 |
| Median paired edit speedup and 95% bootstrap interval | 1 | 0.962× (0.852–1.007) | 1.026× (0.810–1.054) |
| Median QEMU lifetime (ms) | 51.253 | 50.837 | 50.448 |
| Median process wall time (ms) | 233.906 | 233.331 | 233.242 |

Neither edit-latency interval excludes one. Late translations occurred in each arm. These observations cover the training edit with the B4 stores removed or seven guarded UDIV splices installed. They supply no promotion evidence. [Stage semantics](../pye-b4-native-training/) · [Plan](plan.json) · [Paired estimates and late translation counts](summary.json) · [Observations](observations.jsonl) · [Verified archive identity](receipt.json) · [Raw evidence](raw.tgz)
