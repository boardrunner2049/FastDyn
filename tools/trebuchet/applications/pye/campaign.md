# Pye fully warm campaign

On a dedicated Linux x86-64 host, install the release ARM compiler and runtime prerequisites, initialize the pinned QEMU submodule, and install this checkout’s FastDyn and scopt environments at the repository root. Export `FIRMBOLT_REPO`, `FIRMBOLT_PYTHON` and `FIRMBOLT_BOLT` for the pinned optimizer. Choose a CPU free of other workloads. New work and result directories have to be on a filesystem supporting sparse caches. The following example uses CPU 2 if it is available and isolated on that machine.

```sh
tools/trebuchet/applications/pye/hosted-campaign.sh /tmp/pye-work /tmp/pye-results 2
```

The wrapper verifies the QEMU board correction, builds the release plugin, fetches and builds the product, and attempts the relocated guest image. The training checker requires execution within the relocated code and a correct complete document. Each qualified image receives an independent config, known-path inventory, checked ordinary snapshot and exact training profile. The seven real sessions all contribute to the cache inventory in two fixed sweeps. Only the training session populates the optimizer profile. Held-out results evaluate editing decisions on paths available during preparation. Frozen preparation on new paths is a separate experiment.

Each role, seed, objective, pair count and budget is fixed in `campaign-policy.json`. The first search admits at most eight deployments, with 3600 seconds for current-recipe materialization. Historical controls receive seven slots and a separate 3600 seconds. These limits cover this one application/configuration. Candidate materialization and interactive timing run sequentially. Every comparison checks the full document, queued UART protocol, runtime and cache identity, normal exit, and zero appended translations. The primary value is elapsed time from release of the queued terminal request to receipt of the editor’s return marker. The runner also reports QEMU lifetime and launch costs.

The pre-change control uses the unmodified scopt code and pipeline definitions extracted from FastDyn commit `16790d6e56c25b820748d275cb80577db205bcbd`. It uses the qualified FirmBOLT image when available and the original otherwise. The reviewed selectable controls are ordinary, B3, PC folding, arena slide, SFB→B3, B4 and SFB. The historical selector’s ranking and confirmation rules are applied through the common UART runner to two independent source-document edits reserved for this purpose. Each candidate’s score is its median paired gain over the fixed image’s warm control. The best score above one advances to the frozen comparison. Confirmation requires a majority of wins, median gain above one and the one-sided balanced-order randomization test at 0.05. An unavailable historical recipe or failed historical measurement leaves promotion unavailable. The remaining current search can still produce an explicitly ordinary-only diagnostic.

The current selector ranks complete deployments against original ordinary warm, using absolute latency. A single winner goes to confirmation. Confirmation and promotion use independent operational jobs, at least 1% median paired gain, and a one-sided significance level of 0.05. An unsuccessful confirmation retains the frozen ordinary deployment. Final evaluation uses a separate document, balanced orders and paired bootstrap intervals. Identical deployments share a measurement arm.

Read `WORK/campaign/campaign.json` for image qualification, failure phase and total cost. The registry, independent historical selection, current selection and final directories retain their own identities and raw observations. The wrapper exports compact evidence on exit, including failed trials. Large immutable caches and prepared images stay under WORK. After a successful timed trial, the runner removes its private cache and RAM copies. A failed ordinary proof blocks the fully warm campaign.

The wrapper exports an offline training bundle per prepared image under `RESULTS/training`, plus the matching QEMU binary once. The bundle contains the sparse ordinary snapshot with its captures, merged and available component profiles, release ELF, cache manifest, base configuration and runtime identities. A failed later stage still exports what exists. Inspect `status.txt` for incomplete preparation and `sizes.txt` for the exact compressed part sizes. Verify and unpack the archive with GNU tar.

```sh
cd "$results/training"
sha256sum -c SHA256SUMS
cat original.tar.gz.part-* | tar -xzSf - -C "$offline"
cat runtime.tar.gz.part-* | tar -xzSf - -C "$offline"
```

A public host without the private optimizer can run the original guest alone. Without `FIRMBOLT_REPO` or a supplied ELF the receipt records the optimized image as unavailable. To use a prequalified guest on that host, append `--firmbolt-elf /path/firmware.elf --firmbolt-sha256 HEX` to the wrapper command. The driver checks the original build’s loadable payload against its frozen qualification hash. Use `--firmbolt-origin-sha256 HEX` when the supplied image has a separately pinned parent payload. The host then checks board allocation and all sixteen core exception vector words, replays an uninstrumented edit, and runs a separate training-only QEMU execution trace to verify entry into relocated code. The trace gives a lower bound on distinct executed TB starts because chaining can skip tracepoints. The trace stays outside timing. The optimized guest still needs its own checked capture, profile and ordinary replay before entering the search.
