# Firmware inlining policies

The `firmware-inline` pass takes a `mode` that chooses which captured guest calls become private region roots. All policies use the same Thumb BL discovery, context matching, region builder, and linker.

| Mode | Region roots | Calls inside an admitted callee |
| --- | --- | --- |
| `exhaustive` | Every discovered call is submitted to the planner. | Expand captured acyclic calls recursively. |
| `hot-loop` | Calls in observed transfer cycles with exact counts meeting `min_call_count` in the selected profile phase. | Leave nested calls public. |
| `firmlooper` | Captured calls whose precise guest BL instruction belongs to a persistent loop in a recognized task entry. No profile or count threshold is required. | Expand captured acyclic calls recursively, including other eligible calls reached after they return. |

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

Pass this file with `--params params.json`. For each task the receipt lists reachable cycles, entries, outgoing transfers, and selected call PCs. A selected cycle has one entry and no known normal exit or unresolved transfer in the intraprocedural graph. Direct calls contribute a fallthrough on return. This models loops that persist when their callees return normally. A call before the loop, a loop with an exit, and code outside recognized tasks do not create roots.

The firmware pass reports selected roots that were actually emitted in `embedded`. Its `private_guarded_returns` count covers return guards embedded inside these executable regions. The B3 public-slot count concerns separately registered slots. An unexpected guest return target or context uses the original lookup after guest return effects complete. Trusted private returns require a bound EnvLayout and an eligible lookup tail. If either is unavailable, the receipt records a side exit. Trusted interrupt and instruction-count settings still apply.

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

Return a tuple of captured call-site IDs in `PolicySelection.roots`. A policy may also return per-site refusal reasons in `refusals`, per-site JSON evidence in `site_evidence`, and policy-level JSON evidence in `evidence`. Unselected IDs without an explicit reason appear as `excluded_by_policy`. Unknown, duplicated, and contradictory nominations fail compilation. Built-in names are reserved. A custom name must resolve to exactly one installed entry point.
