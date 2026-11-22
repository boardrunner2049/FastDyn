# Incremental Pye training measurements

All arms execute the original MicroPython release image and the queued package-version edit. Each worker captures its own ordinary inventory and exact profile. Each trial deploys an immutable arm into a fresh private run directory, validates the cache identity and checks every byte of the returned document. Append counts include all translations after deployment. [Frozen cases and contrasts](training-contrasts.json)

Each group predeclares its generation order, immediate-parent comparisons and resource limits. Most groups allow 3,200 offline seconds. The supplemental LLVM layout group allows 1,700. Each standard generation also has a 650-second limit. A missing parent excludes its children. Identical deployment identities share one measurement arm. The worker retains generation refusals and excludes an arm after a failed launch. Failure of ordinary execution stops the worker.

One discard block precedes 12 scored blocks. Six randomly generated permutations and their reverses give each pair of distinct arms six launches in either order. The runner binds the process tree to one CPU, serializes trials and freezes launch order before measuring. The primary interval starts at UART release and ends when Pye returns. The report gives medians and paired bootstrap intervals for editing, QEMU, process-tree CPU and process wall time. Each interval describes an exploratory training comparison. The experiment makes no promotion decision.

The independent audit checks the worker’s exact policy commit, launch sequence, cache proofs, immutable UART receipts, hashes of the full output and transcript, raw clock observations, alias decisions and recomputed bootstrap intervals. It also reports median FastDyn pre-QEMU, RAM-reset and bank-staging times when the launch exposes them.

```sh
# WORK has a prepared native MPS2 QEMU, release plugin and Pye product.
taskset -c "$CPU" scopt-env/bin/python tools/trebuchet/applications/pye/training_contrasts.py \
    chains-sfb "$WORK" "$RESULTS"
scopt-env/bin/python tools/trebuchet/applications/pye/audit_training_contrasts.py \
    "$RESULTS" --commit "$WORKER_COMMIT"
```

The `integrated` group uses the QEMU direct-slot patch in addition to the native MPS2 PSRAM correction. Its ordinary arm and transformed arms share that QEMU and its freshly collected snapshot and profile. The compactor reimports generated ordinary SCM metadata. The continuity candidate requires an extra instrumented run to check the link-arming, variant-exit and invariant counters. Timed trials leave those counters disabled. Subtracting entry bailouts from recorded variant exits supplies a conservative lower bound on executions reaching the eliminated reload. The training edge count bounds the possible benefit from above.
