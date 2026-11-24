# Firmware inlining policies

The `firmware-inline` pass takes a `mode` that chooses which captured guest calls become private region roots. All policies use the same Thumb BL discovery, context matching, region builder, and linker.

| Mode | Region roots | Calls inside an admitted callee |
| --- | --- | --- |
| `exhaustive` | Every discovered call is submitted to the planner. | Expand captured acyclic calls recursively. |
| `hot-loop` | Calls in observed transfer cycles with exact counts meeting `min_call_count` in the selected profile phase. | Leave nested calls public. |
| `firmlooper` | Captured calls whose precise guest BL instruction belongs to a persistent loop in a recognized task entry. No profile or count threshold is required. | Expand captured acyclic calls recursively, including other eligible calls reached after they return. |
| `firmlooper-v2` | Also admits single-entry natural task loops whose exits have recognized terminal paths. | Recursively embeds application code. Stops cloning at classified RTOS, scheduler and exception boundaries. |

`sealed-pgo-7` still runs both PGO host-helper inlining and the `hot-loop` firmware policy. `sealed-firmlooper` runs the firmware pass alone. Use the following command on a matching sealed capture, setting `SCOPT_ENVLAYOUT_PATH` to the directory containing the layout produced from the runtime executable.

```sh
scopt optimize "$SNAP" --pipeline sealed-firmlooper --elf "$ELF" \
  --runtime-identity "$RUNTIME_JSON" --v7-trusted-exe "$QEMU_SHA" \
  --store "$OUT" --json
```

FirmLooper automatically recognizes supported direct FreeRTOS registrations with a recoverable task pointer. For a different scheduler, supply known task entries as ELF symbols or addresses. The existing Thumb analysis needs function boundaries and code bytes. An unknown entry is reported in the receipt.

```json
{"firmware-inline":{"policy_options":{"task_entries":["worker_task","0x8001234"]}}}
```

Pass this file with `--params params.json`. The receipt lists task registrations, object and handle evidence, loop headers, classifications, exits and selected call PCs. Both static policies admit sealed one-instruction fallback records at independently identified task-loop BLs. They use the exact-PC, physical page and translation context used by the loader. Ambiguous target mappings stay public.

Use `--pipeline sealed-firmlooper-v2` for the RTOS-aware policy. For example, a task that initializes a UART, then loops on a queue and calls `parse_record`, can embed `parse_record` and its ordinary application callees. The initialization call stays outside the roots. A classified shutdown path can leave the loop. An unclassified exit prevents selection under this rule. Blocking calls are modeled as returning along normal control flow, and every branch and guest OS effect remains executable.

The recursive callback receives an `ExpansionSite` with the source unit, guest instruction and destination, plus the enclosing tuple of original call-site IDs. It runs at nominated roots, nested direct calls, and static branch targets (including tail transfers). It can return:

| Action | Current executable transfer |
| --- | --- |
| `INLINE` | Embed the eligible target in the private call context. All capture, linking and return-context requirements apply. |
| `SHARED` | Use the existing transfer to public code, including QEMU's lookup. The loader initializes public descriptors lazily. A fixed offline jump would skip that initialization. Receipts record `rtos_shared_public_lookup`. |
| `PUBLIC` | Preserve the original transfer. Receipts record `rtos_public_boundary`. |

The built-in boundary classification uses FreeRTOS API names, recognized scheduler functions, exception vector entries, handler names and direct call/branch reachability from handlers. FreeRTOS+ application libraries such as `FreeRTOS_CLIProcessCommand` can be inlined. MPU-wrapped kernel APIs and kernel `FromISR` entries stay shared or public. A `FromISR` name alone does not imply all its callees execute in an interrupt. When a discovered handler directly reaches a function, the policy keeps the entire symbol range public, including shared routines also called in task context. A direct branch veneer inherits its destination's classification. Unknown function ownership stays public. Indirect interrupt call targets, stripped or renamed private kernel routines, and dynamically installed vectors can escape static identification. The classification affects duplication only. It gives no authority to remove synchronization, exception checks or guest memory effects.

For two independent ablations, pass `{"firmware-inline":{"policy_options":{"boundaries":false}}}` to retain recursive expansion across RTOS boundaries, or set `include_terminal` to `false` to keep only strong persistent loops. An empty task discovery produces no firmware roots.

The firmware pass reports selected roots that were actually emitted in `embedded`. Its `private_guarded_returns` count covers return guards embedded inside these executable regions. The B3 public-slot count concerns separately registered slots. An unexpected guest return target or context uses the original lookup after guest return effects complete. Trusted private returns require a Cortex-M3 capture, a bound EnvLayout and an eligible lookup tail. Other CPU models preserve the original return lookup. If a requirement is unavailable, the receipt records a side exit. Trusted interrupt and instruction-count settings still apply.

## Install a policy

Install a trusted Python package into the same environment as scopt, with an entry in its `pyproject.toml`:

```toml
[project.entry-points."scopt.firmware_inline_policies"]
listed-sites = "my_policies:LISTED_SITES"
```

For example, put the following in the package's `my_policies.py`:

```python
from scopt.passes.firmware_policy import FirmwarePolicy, PolicySelection


def select_listed(inputs):
    pcs = {int(pc, 0) for pc in inputs.params["policy_options"]["call_pcs"]}
    roots = tuple(call.callsite_id for call in inputs.calls
                  if call.eligible and call.insn_vaddr in pcs)
    return PolicySelection(roots, evidence={"call_pcs": sorted(pcs)})


LISTED_SITES = FirmwarePolicy(select_listed, expand_nested=False)
```

Use `--params` with `{"firmware-inline":{"mode":"listed-sites","policy_options":{"call_pcs":["0x8001234"]}}}` on the same pipeline. A policy receives the imported snapshot, firmware image, discovered calls, pass parameters, and optional profile. Set `requires_edges=True` on its descriptor to request the exact phase profile and opt into the CLI's automatic PGO workflow. `expand_nested=True` requests transitive expansion in admitted regions.

Return a tuple of captured call-site IDs in `PolicySelection.roots`. An optional `PolicySelection.expansion(site, inline_context)` callback returns an `ExpansionAction` from `scopt.analysis.rtos_boundaries`. The default allows every otherwise eligible transfer. A policy may also return per-site refusal reasons in `refusals`, per-site JSON evidence in `site_evidence`, and policy-level JSON evidence in `evidence`. Unselected IDs without an explicit reason appear as `excluded_by_policy`. Unknown, duplicated, and contradictory nominations fail compilation. Built-in names are reserved. A custom name must resolve to exactly one installed entry point.

## FreeRTOS signatures for stripped Thumb firmware

`sealed-firmlooper-v2` can use a local symbolized reference ELF to recover names for RTOS policy selection. Supply `--elf` with the actual target used for the capture. For example, run the standalone diagnostic and the optimizer as follows:

```sh
python -m scopt.analysis.rtos_signatures "$REFERENCE_ELF" "$TARGET_ELF" > recognition.json
jq -n --arg ref "$REFERENCE_ELF" \
  '{"firmware-inline":{"policy_options":{"signature_reference":$ref}}}' > params.json
scopt optimize "$SNAP" --pipeline sealed-firmlooper-v2 --elf "$TARGET_ELF" \
  --runtime-identity "$RUNTIME_JSON" --v7-trusted-exe "$QEMU_SHA" \
  --params params.json --store "$OUT" --json
```

The matcher covers named Thumb functions in file-backed executable sections, including tasks, OS services and application callers. Keep the reference's symbol table and ARM mapping symbols. A matching load-image companion recovers names, function extents and code/data markers at their original addresses. On changed builds, the matcher requires a unique reference and target byte sequence of at least 12 bytes, and independent entry evidence from the target's symbol table, reset/exception vectors, Thumb entry point or a reachable direct call. When only external direct `BL` displacements differ, the candidate must retain a contiguous fixed anchor of at least 12 bytes and every masked field must still decode as a direct call to the independently matched destination. Ambiguous bodies, unresolved calls and overlapping spans produce refusals. Unknown indirect targets stay unknown.

A changed-build match identifies a **covered span**. The true implementation may continue beyond its end. The receipt records the reference and target addresses, method, span size and extent evidence for each match. The policy consumes these hints; native edits retain their existing capture and control-flow checks. Inferring a queue identity, constant argument, scheduler state or lack of ISR access requires separate evidence.

### Evaluated coverage

The FreeRTOS+CLI UART application (FreeRTOS kernel `89592801dd8b`, Cortex-M3) was stripped with `arm-none-eabi-strip --strip-all`. The target's withheld symbol table was consulted only after inference to score address/name and extent agreement.

| Reference → stripped target | Accepted names at correct entries | Spans with exact original size | Task entries recovered |
|---|---:|---:|---:|
| Same `-Os` build | 237 / 237 | 237 | 4 |
| Ordinary `-Os` → independently linked `-Os` zero-wait kernel | 51 / 238 | 51 | 0 |
| Ordinary `-Os` → `-O0` | 5 / 266 | 3 | 0 |

Normalized direct calls accounted for seven of the 51 cross-link matches. The two size mismatches on the `-O0` target were matching prefixes in `prvPortStartFirstTask` and `Default_Handler`, each two bytes short of the symbol size. All five recovered names and start addresses agreed with the withheld symbols. Cross-build matches alone did not recover the UART task registrations in these two comparisons.

A separate sealed capture of the stripped `-O0` guest produced zero regions with ordinary symbol discovery. Supplying its matching `-O0` companion admitted 22 private regions. Each input activated seven regions during the work window. Six randomized process blocks checked 16 output words on each of 24 runs and verified the loaded cache identity. The median time for 6,000 mixed commands went from 731.2 to 709.0 ms, and the 4,000-line help mix went from 345.2 to 322.1 ms. The paired baseline/candidate ratio across both windows was **1.036× (95% bootstrap interval 1.014–1.080)**. This measures enabling FirmLooper on that stripped guest. Matching runs offline and is excluded from the timed window.
