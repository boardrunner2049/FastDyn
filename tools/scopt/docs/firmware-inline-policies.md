# Firmware inlining policies

The `firmware-inline` pass takes a `mode` that chooses which captured guest calls become private region roots. All policies use the same Thumb BL discovery, context matching, region builder, and linker.

| Mode | Region roots | Calls inside an admitted callee |
| --- | --- | --- |
| `exhaustive` | Every discovered call is submitted to the planner. | Expand captured acyclic calls recursively. |
| `hot-loop` | Calls in observed transfer cycles with exact counts meeting `min_call_count` in the selected profile phase, kept only when the [profit model](#profit-model) admits the planned region. | Inline a nested call when its marginal profit is positive (`nested="profit"`, the default with the cost model); recursive, so a newly inlined callee's calls are priced next. `nested="public"` or `cost_model="off"` leaves nested calls public. |
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

FirmLooper v2 distinguishes the caller from the destination when applying the FreeRTOS name convention. A recovered registration can identify a file-local application worker such as `vQueueReceiveWhenSuspendedTask`. Its loop may nominate calls even though its name begins with `vQueue`. This exception requires unambiguous ELF local function ownership in a named C compilation unit outside the recognized kernel sources. Explicit task hints, global aliases, stripped functions and kernel source files keep the ordinary name boundary. Scheduler and exception visibility take precedence. A file-local helper owned by a recognized kernel source stays shared, even when its name has no API prefix. This applies to its interior and direct entry veneers. Every callee still goes through the ordinary destination check.

The object receipt reads the type argument of generic constructors from the Thumb binary. Dynamic queues and both stream-buffer constructors use R2. Static generic queues pass their fifth word at the call-site SP. A missing value or unknown type leaves the family unresolved. On 34 symbolized Cortex-M3 FreeRTOS Common/Minimal images, the analysis distinguished 10 streams and nine message buffers previously reported as generic stream buffers. Seven generic queue sites gained a concrete type. Known handle-use records rose from 290 to 404, including ISR and stream-buffer APIs, while 202 task registrations remained resolved. Two registered application workers in the dynamic-priority demo gained nine loop call nominations. All nine destinations were shared or public kernel routines. These are discovery results and do not establish an execution speedup.

To reproduce the static receipt for one linked application from the FastDyn checkout, set `ELF` to its ARM firmware image and run:

```sh
PYTHONPATH=tools/scopt python - "$ELF" <<'PYTHON'
import json
import sys
from scopt.analysis.task_loops import discover_task_loops
from scopt.decode.guest_symbols import load_guest_image

loops = discover_task_loops(load_guest_image(sys.argv[1]), include_terminal=True)
print(json.dumps(loops.report(), indent=2))
PYTHON
```

For two independent ablations, pass `{"firmware-inline":{"policy_options":{"boundaries":false}}}` to retain recursive expansion across RTOS boundaries, or set `include_terminal` to `false` to keep only strong persistent loops. An empty task discovery produces no firmware roots.

The firmware pass reports selected roots that were actually emitted in `embedded`. Its `private_guarded_returns` count covers return guards embedded inside these executable regions. The B3 public-slot count concerns separately registered slots. An unexpected guest return target or context uses the original lookup after guest return effects complete. Trusted private returns require a Cortex-M3 capture, a bound EnvLayout and an eligible lookup tail. Other CPU models preserve the original return lookup. If a requirement is unavailable, the receipt records a side exit. Trusted interrupt and instruction-count settings still apply.

## Profit model

`firmware-inline` can price each planned region with the exact training profile
(`scopt/analysis/firmware_profit.py`, model `firmware-dispatch-v1`). It runs after the
trusted edge checks, on the plan that would be emitted, and can only refuse a root:
it never adds or changes region content. A refused call site keeps its public TBs,
lookup, chaining and return path.

Probabilities from the profile (`count(unit, exit kind) / executions(unit)`, flagged
edges included) are pushed through the member graph, one strongly connected component
at a time, so every inline context has its own flow. Per dynamic event the region
**saves** a poll on every internal `goto_tb` edge and at the region entry (trusted
regions enter members at +15), a `helper_lookup_tb_ptr` plus a poll on every internal
`goto_ptr` edge (less a guard on guarded edges and returns), and a full dispatcher
iteration on an internal edge that missed the lookup in training. It **adds** a full
dispatcher iteration on every residual `goto_tb` exit (all member exits without an
internal edge, including the caller's and the root continuation's) when the region's
exits are unchainable, and on a trusted exit into a trusted region entry (exact for
QEMU before counters revision 4; conservative from revision 4 on, where QEMU chains it
to the destination entry's +0 for the price of one poll); a dispatch per chained
in-edge of the caller when chaining into the trusted entry is refused (non-sealed
`CF_PCREL` captures); and `cost_byte_ns` per byte
of member code and descriptors. `goto_ptr`, `exit_null` and `exit_requested` residual
exits cost what they cost in public code. A root is admitted when
`saved >= (1 + cost_margin) * added`, `net > 0` and `net / calls >= cost_min_net_per_call_ns`.
When several roots overlap, regions whose members contain another root's caller are
priced first, and the later root is credited only with the executions those regions
leave public; a root entered only through an earlier region is refused.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `cost_model` | `auto` | `auto`: on for `hot-loop` (and policies whose descriptor sets `cost_model_default`), `profile`: on for any mode (optimize then collects training edges for it), `off`. |
| `residual_goto_tb` | `auto` | `auto`: safe regions keep public slots (chained); trusted regions chain unless a member source was grown by an earlier B3 pass (`region_exits_chainable`). That B3 rule is deliberately conservative and ignores `member_slot_skip`: the pre-B3 member copies drop B3's guards, and the internal-edge credit assumes lookups B3 already chains. `chained`/`unchained` override. |
| `member_slot_skip` | `auto` | Composed (B3) artifacts only: whether trusted members may skip the goto_tb slots B3 registered (v7 member flags). `auto`: on when the target RuntimeIdentity exe is allowlisted (`--v7-member-skip-exe`, `SCOPT_V7_MEMBER_SKIP_EXES`); `on`: fail the pass unless it is; `off`: never. Recorded in the receipt (`policy.member_slot_skip`) and the pass stat `member_slot_skip`. Pricing does not read it. |
| `entry_chain` | `auto` | `auto`: unchained for a trusted region whose entry key is `CF_PCREL`. |
| `cost_poll_ns` | 0.4 | Skipped 15-byte IRQ/`can_do_io` prefix. |
| `cost_lookup_ns` | 7.0 | One `helper_lookup_tb_ptr` hit (H4 measured about 6.4 ns). |
| `cost_guard_ns` | 1.0 | One private return guard. |
| `cost_dispatch_ns` | 40.0 | One return to `cpu_exec_loop` and back (see below). |
| `cost_byte_ns` | 0.25 | Per run, per byte of private code and descriptor. |
| `cost_margin` | 0.25 | Required saved/added surplus. |
| `cost_min_net_per_call_ns` | 1.0 | Minimum net gain per root call. |

`cost_dispatch_ns` is calibrated on the archived packet run (trebuchet
`2026-11-25-seven-o4`, config 7 against config 6): the per-byte CRC-CCITT root
`0x4a70 -> 0x331c` (2,317,210 training calls, one unchained continuation exit per call)
saves 30.4 ns per call in the model (22 skipped polls, two patched lookups, one
guarded return, the entry poll), and config 7 lost about 8.5 ns per residual exit
(per-block median; 15.7 ns with per-arm medians), so one dispatcher trip costs about
39-46 ns, inside the 20-50 ns expected for the path. With these defaults the CRC root
is refused at -9.6 ns per call when exits are unchainable and admitted at +30.4 ns per
call when they chain; a lookup-exit root such as `0x82aee -> 0x81afc` is admitted
either way (`tests/unit/data/firmware_profit_packet.json` carries both plans). All
constants are pass parameters, and the receipt's `policy.cost_model` records the
values used.

Receipts: every embedded row carries `profit` (status, entries, calls, dynamic
`terms`, bytes, `saved_ns`, `added_ns`, `footprint_ns`, `net_ns`, `net_ns_per_call`,
resolved `residual_goto_tb`/`entry_chain`), `expansion_depth` and
`nested_inlined_calls`; refused roots are `residual` rows with reason
`not_profitable` (or `profit_estimate_unknown` when an execution count is not exact)
and the same `profit` object. `policy.nested_decisions` lists each priced nested call
(`gain_ns`, `action`), and `policy.nested_fallbacks` names roots for which the
one-level region won. Stats add `cost_model_enabled`, `profit_net_ns`,
`profit_refused` and `nested_inlined_calls`. When nothing is embedded because roots
were refused, the outcome reason is `no_profitable_call`.

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

Use `--params` with `{"firmware-inline":{"mode":"listed-sites","policy_options":{"call_pcs":["0x8001234"]}}}` on the same pipeline. A policy receives the imported snapshot, firmware image, discovered calls, pass parameters, and optional profile. Set `requires_edges=True` on its descriptor to request the exact phase profile and opt into the CLI's automatic PGO workflow. `expand_nested=True` requests transitive expansion in admitted regions. `cost_model_default=True` prices the policy's regions unless `cost_model="off"`; the model is passed to the policy as `PolicyInput.profit`, and `scopt.analysis.firmware_profit.NestedProfitExpansion` is a ready-made profit-gated `expansion` callback (return it with `PolicySelection(expand_nested=True)`).

Return a tuple of captured call-site IDs in `PolicySelection.roots`. An optional `PolicySelection.expansion(site, inline_context)` callback returns an `ExpansionAction` from `scopt.analysis.rtos_boundaries`, or a nonempty string that becomes the side exit's residual reason. The default allows every otherwise eligible transfer. A policy may also return per-site refusal reasons in `refusals`, per-site JSON evidence in `site_evidence`, and policy-level JSON evidence in `evidence`. Unselected IDs without an explicit reason appear as `excluded_by_policy`. Unknown, duplicated, and contradictory nominations fail compilation. Built-in names are reserved. A custom name must resolve to exactly one installed entry point.

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

The matcher covers named Thumb functions in file-backed executable sections, including tasks, OS services and application callers. Keep the reference's symbol table and ARM mapping symbols. A matching load-image companion recovers names, function extents and code/data markers at their original addresses. On changed builds, the matcher requires a unique reference and target byte sequence of at least 12 bytes, and independent entry evidence from the target's symbol table, reset/exception vectors, Thumb entry point or a reachable direct call. Every external direct `BL` in a changed-build signature must still reach the independently matched destination, including a byte-identical call at a different address. When the displacement differs, the candidate must also retain a contiguous fixed anchor of at least 12 bytes and every varying field must still decode as a direct call. Ambiguous bodies, unresolved calls and overlapping spans produce refusals. Unknown indirect targets stay unknown.

A changed-build match identifies a **covered span**. The true implementation may continue beyond its end. The receipt records the reference and target addresses, method, span size and extent evidence for each match. The policy consumes these hints; native edits retain their existing capture and control-flow checks. Inferring a queue identity, constant argument, scheduler state or lack of ISR access requires separate evidence.

### Evaluated coverage

The FreeRTOS+CLI UART application (FreeRTOS kernel `89592801dd8b`, Cortex-M3) was stripped with `arm-none-eabi-strip --strip-all`. The target's withheld symbol table was consulted only after inference to score address/name and extent agreement.

| Reference → stripped target | Accepted names at correct entries | Spans with exact original size | Task entries recovered |
|---|---:|---:|---:|
| Same `-Os` build | 237 / 237 | 237 | 4 |
| Ordinary `-Os` → independently linked `-Os` zero-wait kernel | 28 / 238 | 28 | 0 |
| Ordinary `-Os` → `-O0` | 4 / 266 | 2 | 0 |

Normalized direct calls accounted for one of the 28 cross-link matches. The two size mismatches on the `-O0` target were matching prefixes in `prvPortStartFirstTask` and `Default_Handler`, each two bytes short of the symbol size. All four recovered names and start addresses agreed with the withheld symbols. Cross-build matches alone did not recover the UART task registrations in these two comparisons.

A separate sealed capture of the stripped `-O0` guest produced zero regions with ordinary symbol discovery. Supplying its matching `-O0` companion admitted 22 private regions. Each input activated seven regions during the work window. Six randomized process blocks checked 16 output words on each of 24 runs and verified the loaded cache identity. The median time for 6,000 mixed commands went from 731.2 to 709.0 ms, and the 4,000-line help mix went from 345.2 to 322.1 ms. The paired baseline/candidate ratio across both windows was **1.036× (95% bootstrap interval 1.014–1.080)**. This measures enabling FirmLooper on that stripped guest. Matching runs offline and is excluded from the timed window.
