# Operational application campaigns

Trebuchet builds candidate registries for prepared original and FirmBOLT images, selects one complete deployment, and measures the frozen choice on reserved scenarios. Read each application's qualification record before creating a performance protocol. The historical fixtures, upstream ARM images that only boot, and native simulator diagnostics are excluded from performance summaries.

| Family | Preparation and operational checks |
|---|---|
| ArduPilot and PX4 | [Existing firmware ports and FMI mission qualification](flight/README.md) |
| ESCape32 | [Pinned ESC and bootloader builds and board qualification](escape32/README.md) |
| grblHAL | [ARM release build, published drawing and upstream simulator](grblhal/) |
| Pye on MicroPython | [Terminal editor qualification](pye/) |

## Runtime

The existing ArduCopter and PX4 ports both completed a cacheless waypoint mission with independent FMI checks. Pye completed a release-image ordinary replay and a separate replay of the restricted guest rewrite. Each result records the exact image and inventory. [Flight receipts](flight/evidence/) · [Pye qualification](../results/real-applications/pye-qualification/) · [Two-image preparation](../results/real-applications/pye-joint-preparation/)

Build FastDyn and its pinned QEMU according to [scopt setup](../../scopt/docs/getting-started.md). Build the Cortex-M FirmBOLT frontend and patched backend from the pinned checkout. The native inliner requires a separate [LLVM 20.1.2 bridge](../../scopt/native/README.md). Activate the scopt Python environment or set `TREBUCHET_PYTHON` to its interpreter. Set `SCOPT_RUNTIME_QEMU`, `SCOPT_RUNTIME_QEMU_SHA256`, `SCOPT_ENVLAYOUT_PATH`, `SCOPT_HELPER_EFFECTS_PATH` and `SCOPT_V6_EXES` for the exact binary. Configure the normal platform, clocks, devices and icount policy from the application manifest.

On the timing runner, first check the exact per-process operation used by scopt.

```sh
setarch x86_64 -R python3 -c 'from pathlib import Path; assert int(Path("/proc/self/personality").read_text(), 16) & 0x40000'
```

Run the original application's documented job and its oracle before capture. A successful release build, an ELF with validated symbols, and a matching output from a host-native simulator are separate milestones. Keep profiles and cache inventories separate per image. Record the training, ranking, confirmation, promotion and final scenarios before any timing. Include the inventory construction policy, device boundary, runtime settings, upstream provenance, oracle, tolerance rationale and useful work in the contract. A fully warm final comparison must declare whether final paths contributed to its inventory.

## Materializing candidates

After an operational admission, prepare the ordinary snapshot, manifest, runtime admission, and exact training profile for each supported image with the existing scopt prepare, bundle, import, admission and profile commands. Bind inputs by content. Keep the original release image, relocation-preserving release rebuild and any exploratory compiler configurations distinct. The manifest for materialization has the following fields.

| Field | Meaning |
|---|---|
| `application`, `scenario`, `configuration` | Names of the real application, scenario contract and pinned configuration |
| `admission`, `contract` | Absolute paths to the operational admission JSON and scenario contract. Admission includes the application name, `status: admitted`, reproduction and oracle evidence. |
| `objective` | `name`, `unit`, `direction` (`minimize` or `maximize`), `useful_work` and `boundaries` |
| `images` | Map keyed by `original` and optionally `firmbolt`. Each supplies absolute paths for `firmware`, `config`, `runtime` (patched QEMU executable), `snapshot` (ordinary imported store directory), `profile` (exact training profile), `manifest` (capture's FastDyn manifest), `prep` (scopt preparation), and `admission_runtime` (runtime identity v2 for transformation). |
| `budget` | `candidate_limit` and `offline_seconds` for the complete application/configuration, including both images |
| `recipes`, `params` | Optional recipe list and stage-specific parameters. The four required controls are `warm`, `b3`, `sfb`, and `sfb+b3`. |

The `+` operator materializes and reimports each published generation before its successor. Shared prefixes run once per image in one invocation. The registry records commands, stdout receipts, stderr, pass outcomes and input/output snapshot IDs. The original ordinary capture remains bound through the entire chain. Missing or rejected generations cannot enter selection. An application with an unsupported FirmBOLT image can still compare its qualified original-image deployments. Record the reason for excluding that image.

Available additional stages include `static`, `returns`, `general` (return and static chaining), `dominant`, `b4`, `b4-coalesce`, `b4-absorb`, `sfb-hot`, `ph`, `b3-ph`, `native`, `pc` and `slide`. For example, `sfb+b4+b3` has two intermediate imports. `sfb+b3-ph` performs semantic growth before placing final-sized blocks in its B3 generation. To test selective SFB, set `params.sfb-hot.entry_coverage` to a percentage and optionally bound `max_growth_bytes` and `max_moved_units`. General optimize stages accept scopt's per-pass JSON parameter map. The registry controls generate each stage's explicit pass policy. Adding an entry to a plan requests an experiment and does not establish target or runtime compatibility.

```sh
tools/trebuchet/trebuchet.sh candidates materialization.json new-registry
tools/trebuchet/trebuchet.sh select new-registry/registry.json protocol.json selection
tools/trebuchet/trebuchet.sh final new-registry/registry.json protocol.json selection final
```

See [joint selection and application observer protocol](../lib/joint_selection.md). For final evaluation the protocol adds `family`, `final_seed` and `pairs.final`. The latter must be at least 12 and divisible by the number of order permutations among distinct deployments. Every block measures the original ordinary warm reference, frozen incumbent and selected deployment. Byte-identical deployments share an arm. The final report keeps individual observations, failures, absolute measurements and paired within-scenario percentile bootstrap intervals (2,000 resamples). Intervals across randomized block samples are conditional on this host and these scenarios.

For a complete suite, freeze a JSON corpus with a shared `budget` and `applications` list. Each admitted row records its `application`, `family`, positive `weight`, `status` of `admitted`, and pinned `registry` and `protocol` objects with `path` and `sha256`. For a blocked row specify `status` of `blocked` and the observed `reason`. Run all selections before any final evaluation. Each suite run requires a fresh output directory.

```sh
tools/trebuchet/trebuchet.sh dataset frozen-corpus.json new-campaign
tools/trebuchet/trebuchet.sh dataset_report new-campaign/dataset.json
```

The table weights scenarios and applications as declared, then computes one geometric mean across the complete admitted family set. A blocked suite emits no numeric aggregate. Fully warm comparisons require zero appended translations. The Pye training-frozen protocol deploys a fresh copy of a training-only snapshot for each trial and records late translations alongside the application oracle. See the [Pye campaign](pye/campaign.md) and its [frozen policy](pye/frozen-policy.json).
