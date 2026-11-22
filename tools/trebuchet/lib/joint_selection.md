# Joint deployment selection

Run the selector with the scopt interpreter after an application has an admitted
operational baseline, per-image ordinary snapshots, exact profiles and a completed
candidate registry.

```sh
scopt-env/bin/python tools/trebuchet/lib/joint_selection.py registry.json protocol.json new-output-dir
```

Each trial deploys the selected image's firmware, config and proof-checked cache with
its pinned QEMU. Stage memory-bank inputs before launch. To drive a native serial
device, supply a pinned UART peer and per-case stage. The launcher starts the peer
before QEMU, collects and hashes its output, and requires a successful application
receipt. The observer verifies application output against the expected result.
A live external plant requires its own attachment.

## Protocol

Write the protocol and pin the application observations and replay files before
ranking. Digests use the same `sha256:<hex>` format as scopt. File references below
are objects with absolute `path` and `sha256` fields.

| Field | Meaning |
|---|---|
| `application`, `configuration`, `objective` | Copy the admitted registry values. The objective includes `name`, `unit`, `direction`, `useful_work` and `boundaries`. |
| `registry_sha256` | Hash of the completed registry JSON file. |
| `measurement` | `operational_latency_ns` or `process_cpu_ns` has unit `ns` and direction `minimize`. `throughput_per_second` has unit `work_units/s` and direction `maximize`. Define the work unit in the objective. |
| `secondary_measurement` | `guest_window_ns` uses one guest marker interval. `qemu_lifetime_ns` uses the complete QEMU execution span in the FastDyn timing trace. |
| `fallback_identity` | Exact deployment identity of `original/warm`. |
| `incumbent_id`, `incumbent_identity` | ID and frozen deployment identity of the incumbent. An optional pinned `incumbent_registry` with `incumbent_image` can reference a prior build with the same application, configuration, objective and input contract. |
| `images` | Object keyed by image variant. Each binding records independent `firmware_sha256`, `config_sha256`, canonical `profile_id` and a pinned `observer` Python program. An external incumbent uses the same binding fields in `incumbent_image`. |
| `candidate_limit` | Upper bound on the registry size, within the application materialization budget. |
| `seed`, `warmup_pairs`, `pairs` | Fixed seed, at least one discarded warmup pair per comparison and scenario, and even pair counts of at least 12 for each of `ranking`, `confirmation` and `promotion`. The separate final evaluation uses `final_seed` and at least 12 final blocks, balancing every permutation of its distinct arms. |
| `alpha`, `minimum_gain` | One-sided significance threshold up to 0.05, and minimum practical paired speedup greater than 1. |
| `timeout_seconds` | Positive timeout for each launch or observation extraction. |
| `scenarios` | Lists for `training`, `ranking`, `confirmation`, `promotion` and `final`. Each entry has a unique `id`, positive `weight`, a `bank_inputs` map from bank names to pinned replay files, and a pinned `expected` reference file. Preserve the inputs' upstream or operational provenance in the admission record. |

The same scenario across images uses one common, independently validated expected
application result. Its JSON has a positive `completed_work`, an `observations`
object of named application outputs, and optional absolute `tolerances` for declared
numeric fields. Use exact outputs where practical and record the rationale for each
tolerance in the scenario contract. The observer is bound separately to each image,
allowing symbols or output layouts to differ. The invocation is
`python OBSERVER RUN_DIRECTORY/run.json FIRMWARE_ELF`. It returns JSON containing
`completed_work`, `observations` and, for latency or throughput, a positive
`operational_elapsed_ns` measured between the predeclared boundaries. CPU cost comes
from the launcher's host user and system time and requires equal validated progress.
The run must also have one valid secondary interval, a successful cache proof, zero
appended blocks, the pinned replay and a normal exit. Raw trial records also include
RAM reset, bank staging and FastDyn process-tree CPU cost. For the QEMU lifetime
protocol, the pre-QEMU interval runs from FastDyn's total start to its QEMU execution
start. Final results contain per-arm medians for these costs.

Ranking records paired observations against the same original-image ordinary-warm
deployment. The score uses the weighted geometric mean of absolute per-scenario
objective medians. A candidate must beat the common reference in ranking. The best
candidate is frozen before confirmation; ties retain the first according to the seeded
order. Confirmation and promotion independently require the practical paired gain and
exact balanced-order randomization test to pass for every declared scenario. A rejected
candidate observation is preserved in the output. Failure of a reference observation
or a change of deployment identity invalidates the campaign. Failure to confirm
selects the exact ordinary-warm fallback. The resulting selection is compared with
the frozen incumbent. The `promoted` decision refers to that separate comparison.
If materialization of the historical control fails, a protocol frozen with
`promotion_enabled: false` and no incumbent bindings can still measure the
selection against ordinary warm. It records an unavailable promotion and the
aggregate report withholds incumbent gains for the affected family and corpus.

The output contains the frozen protocol and registry, raw measured and discarded
warmup observations in launch order, each run's proof, the frozen ranking winner,
and the selection and promotion receipt. Final scenarios remain reserved for a
separate evaluation of the frozen decision. Selector unit tests use an in-process
mock trial and do not produce application performance evidence.

## State continuity census

Use a training snapshot that matches its exact profile and a bound DWARF layout.
The diagnostic records per-edge store/reload pairs for exact-offset, matching-width
D1 state fields. It separates observed chain-slot traffic from possible dynamic
access counts, records helpers, faultable accesses, branch-path coverage and register
clobbers, and preserves the possibility of independent entry. IRQ, exception and
longjmp-tagged chain traffic is reported separately. The optional registry binding
ties the report to an admitted application. A report without that binding is an
unattributed diagnostic.

```sh
scopt-env/bin/python tools/trebuchet/lib/state_continuity.py "$SNAPSHOT" "$TRAINING_PROFILE" \
  --phase steady --registry "$REGISTRY" --image original --out state-continuity.json
```

An unchained dispatch profile can record the exact number of exits through a slot.
It cannot establish that those exits hit a linked target in a measured deployment.
The per-site-pair upper bound allows either site to be bypassed on the internal
host CFG. The census supplies no value-transport or private-entry proof and emits
no code.
