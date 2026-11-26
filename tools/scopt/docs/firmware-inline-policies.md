# Firmware inlining policies

The `firmware-inline` pass takes a `mode` that chooses which captured guest calls become private region roots. All policies use the same Thumb BL discovery, context matching, region builder, and linker.

| Mode | Region roots | Calls inside an admitted callee |
| --- | --- | --- |
| `exhaustive` | Every discovered call is submitted to the planner. | Expand captured acyclic calls recursively. |
| `hot-loop` | Calls in observed transfer cycles with exact counts meeting `min_call_count` in the selected profile phase, kept only when the [profit model](#profit-model) admits the planned region. | Inline a nested call when its marginal profit is positive (`nested="profit"`, the default with the cost model); recursive, so a newly inlined callee's calls are priced next. `nested="public"` or `cost_model="off"` leaves nested calls public. |
| `firmlooper` | Captured calls whose precise guest BL instruction belongs to a persistent loop in a recognized task entry. No profile or count threshold is required. | Expand captured acyclic calls recursively, including other eligible calls reached after they return. |
| `firmlooper-v2` | Also admits single-entry natural task loops whose exits have recognized terminal paths. | Recursively embeds application code. Stops cloning at classified RTOS, scheduler and exception boundaries. |
| `firmlooper-v3` | As `firmlooper-v2`, on [analysis revision 2](#firmlooper-v3): a return from a FreeRTOS-created task entry is a terminal, BOLT entry patches and GCC word jump tables are followed, and the reset entry is not an interrupt root. | As `firmlooper-v2`. A branch out of a member that ends in ISB stays a side exit (`isb_boundary`). |

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

`--params` merges pass parameters shallowly: a `policy_options` object replaces the pipeline's whole `policy_options`, so list every option you want.

## FirmLooper v3

`--pipeline sealed-firmlooper-v3` (mode `firmlooper-v3`) is FirmLooper v2 on analysis revision 2, plus an ISB side exit and a per-task selection summary. Like v2 it needs no profile, and its cost model stays off unless `cost_model="profile"` is passed. The modes `firmlooper` and `firmlooper-v2` keep analysis revision 1, so their selections, receipts, skip reasons and candidate ids are unchanged. A v3 identity comes from its mode: an override such as `{"boundaries":false}` still runs revision 2 with `task_end`. There is no `sealed-firmlooper-v3-b3` yet. In the composed sc6 + v7 artifact that `sealed-pgo-7-b3` built for packet, regions whose sources B3 edited kept the shared descriptor, so their exits did not chain. The hot packet region below was one of them, and the profile cost model priced it as a loss there (-22.2 ms). FirmLooper's cost model is off, so a `-b3` variant would emit that region unpriced. Composed regions can now get member descriptors ([B3 passes](b3-passes.md#sealed-firmware-regions--b3-configuration-7--b3)): always when B3 only rewrote a member source, and when B3 registered goto_tb slots on one, only through member skip flags for a QEMU allowlisted with `--v7-member-skip-exe` (`b3_registered_slot` otherwise). The profit model still prices a region with a B3-grown member source as unchainable, and no composed FirmLooper v3 artifact has been built or timed.

Revision 2 changes four analysis rules:

- **Task end.** Inside a task entry recovered from a FreeRTOS creation site, an unpredicated `bx lr`, `pop {..., pc}` or post-indexed `ldr pc, [sp], #k` is a terminal of kind `task_return`. FreeRTOS starts every task with LR set to `portTASK_RETURN_ADDRESS` (`prvTaskExitError`), so such a return ends the task. A benchmark task that runs N iterations, signals completion and returns now has a `persistent_with_terminal_exit` loop. Entries given only through `task_entries` do not get this rule. The option `task_end` defaults to true and requires `include_terminal` (also the default). One-shot loops that lead into another loop, cleanup cycles, inner loops and call-free spins such as `b .` are still rejected.
- **BOLT entry patches.** BOLT moves a function out of `.bolt.org.text`, drops its symbol and `$t` mapping, and rewrites the old entry as `b.w <new entry>`. A direct call or branch target outside the Thumb code ranges now resolves to the moved function when all of these hold: it lies in an executable section; it has no symbol, or a symbol of at most 4 bytes starting there; and its first instruction is an unconditional `B` to the exact start of a symbolized function. Task discovery, API recognition and the boundary classification follow the patch. Revision 1 classifies such a target as unknown ownership (public).
- **Word jump tables.** GCC's Thumb-2 switch `cmp idx,#last; bhi default; adr base,table; ldr.w pc,[base,idx,lsl #2]` joins the task CFG. It needs the `TBB`/`TBH` guards plus `base != idx`, a 4-aligned table that starts at a `$d` mapping symbol, and table words that all have the Thumb bit set.
- **Reset is not an interrupt.** The boundary classification no longer seeds the reset entry as an interrupt root. That entry is vector word 1 or the ELF entry when it is not also an exception vector, or a function named `Reset_Handler`, `reset_handler`, `ResetISR` or `Reset_IRQHandler`. Under revision 1, everything reachable from `main` is exception-visible and stays public. Real vector entries are still seeded.

**ISB side exit.** ISB ends its TB with a `goto_tb` to the next instruction. Inside a trusted region that edge would branch past the next member's interrupt poll, so a PendSV that cloned code sets just before the ISB would be taken only at the region exit. v3 keeps every branch out of a member whose last guest instruction is ISB as a side exit with reason `isb_boundary`. The exit is armed and chained like any other residual exit. When chained, it enters an ordinary TB at its entry poll or, on a QEMU with counters revision 4, a trusted region entry at its first byte (+0), so the entry's poll runs; QEMU chains into a region entry only when it verified that poll at load. An exit that is not chained returns to the dispatcher. With `boundaries=false` this is the only expansion rule. Other modes apply the same rule only when asked, with the pass parameter [`isb_boundary = true`](#isb-side-exit-in-any-mode-isb_boundary).

### Why the RTOS boundaries remain

The SHARED, PUBLIC and `rtos_public_caller` boundaries are not required by the trusted region contract; `hot-loop` (`sealed-pgo-7`) clones kernel code and accepts deferred yields. FirmLooper keeps them for these reasons, and only these:

- Kernel APIs, scheduler tasks and exception-visible code are never cloned, so kernel-requested yields and critical sections run in public TBs, which poll for interrupts.
- In v3, an ISB at the end of a cloned member ends the region. `MSR` and `CPS` already end their TB with `exit_tb`, which leaves the region (`observation_boundary`).
- Interrupts pending when a region is entered, or raised while region code runs, are still deferred to the next region exit, as the trusted contract (`interrupts=disabled`) allows.
- A poll-free cycle inside a region that waits on interrupt-updated memory without one of those instructions would never observe the update. FirmLooper does not detect this. It is a property of the trusted region ABI, shared with `hot-loop`.

### FirmBOLT -O4 images

On flattened `-O4` images the task loops mostly call kernel APIs, so v3's main output there is the per-task verdict. A region appears only where a hot task still calls application code. On the packet image, `prvRxTask` stayed unrewritten `-O0` code in `.bolt.org.text` and calls its moved callees through BOLT entry patches. Its switch is a word jump table, and its benchmark loop ends by returning from the task. Receipts from 2026-11-25:

| Image | `sealed-firmlooper-v2` | `sealed-firmlooper-v3` |
| --- | --- | --- |
| packet | `no_eligible_call`, 0 regions | 15 regions: `prvRxTask` calls at 0x49dc, 0x4a02, 0x4a28 and 0x4a70 to `prvCrc16Byte` through the patch at 0x331c (10 members each), `prvEmit` (0x4814) and `memset`; `prvProcTask` to `memcpy`. `prvMonitorTask`, `prvIdleTask` and `prvTimerTask`: `only_rtos_boundary_calls`. |
| uart | 5 regions (`FreeRTOS_CLIProcessCommand`, `vComputeDone`) | The same 5 regions plus 2 `memset` regions. `TzCtrl`: `only_rtos_boundary_calls`. |
| cli | `no_eligible_call`, 0 regions | 5 regions: the console loop's call to `FreeRTOS_CLIProcessCommand` (0x82dd0) and `TzCtrl`'s calls to `prvIsValidCommand` and `prvProcessCommand`. |

None of the three images has an `isb_boundary` exit in its v3 regions. Hot-loop regions contain such edges: with `isb_boundary = true`, the O4 design probe (B3 + `hot-loop`, no host-inline) found 33 on packet (in 15 of 48 regions), 2 on UART and none on CLI.

The round-2 campaign rebuilt v2 and v3 on captures from QEMU `aacca05`, with the same region counts, and timed them (20 paired blocks on packet, 10 on UART and CLI; [sealed firmware](sealed-firmware.md#round-2-follow-ups-2026-11-25)). v3 makes no trusted-to-trusted chain attempt on any image, and only some of its regions activate: 8 of 15, 4 of 7 and 2 of 5. time(v2) / time(v3) is 1.0621 [1.0405, 1.0773] on packet, faster in 20 of 20 blocks but within noise, because the interval crosses the A/A band edge 1.0526. It is 1.0428 on UART and 1.0331 on CLI, also within noise. The packet estimate cannot come through the `prvRxTask` -> `prvCrc16Byte` chain the design expected, and its mechanism is not diagnosed. Every point estimate has v3 slower than `sealed-pgo-7`: time(`sealed-pgo-7`) / time(v3) is 0.9580 (packet) and 0.9505 (UART), within noise, and 0.8736 on CLI, below the floor.

### v3 receipt fields

All are additive, and only v3 writes them.

- `policy.analysis_revision` (2) and `policy.boundary_rules`: `reset_entries`, `patched_entries_resolved` (count) and `isb_boundary`.
- `policy.task_loops`: `analysis_revision`, `task_end`, `cfg_rules` (`jump_tables`: `tbb`, `tbh`, `ldr_pc_word`; `patched_entries`), and the `loop_rule` "dominating header with closed, classified-terminal or task-return exits". Each task row adds `task_end` (false for entries given only through `task_entries`) and `selected_call_pcs`.
- Each cycle adds the following:
  - `terminal_kinds` on a `persistent_with_terminal_exit` cycle: `task_return` or `api:<name>`.
  - status `nested_in_selected_loop` for a rejected cycle inside an admitted loop.
  - `blocking_exits` for every other rejected cycle, mapping each blocking exit (or `unknown`) to its reasons:

    | Reason | The exit or its path |
    | --- | --- |
    | `return_in_cycle` | The cycle itself contains a return (predicated or not). |
    | `unknown_indirect_in_cycle` | The cycle contains an unresolved indirect transfer. |
    | `normal_return` | Reaches an ordinary return (no `task_end` for this task). |
    | `unknown_indirect` | Reaches an unresolved indirect transfer. |
    | `enters_other_cycle` | Enters another cycle: a cleanup loop, a later loop or a spin. |
    | `reenters_cycle` | Comes back into the cycle. |
    | `leaves_function` | Branches out of the function. |

- With `boundaries` on, the residual and embedded rows of every in-loop call carry `boundary`: `{action, rule, function}`, plus `patched_entry` when a BOLT patch was followed and `via` (the handler-to-function path, at most 16 names) for `exception_visible`. The rules, in precedence order, are `unknown_owner`, `exception_visible`, `scheduler_name`, `isr_name`, `kernel_api_name`, `kernel_source` and `application`. An `rtos_public_caller` row also carries `caller_boundary`, the same object for the calling task.
- `policy.selection_summary`: `{verdict, tasks: {name: {entry, status, loop_call_sites, captured, embedded, refused, verdict}}}`. A task verdict is the first that applies: `no_closed_task_loop`, `no_task_loop_calls` (no BL in the selected loops), `task_loop_calls_not_captured`, `embedded`, `planned_not_embedded`, `only_rtos_boundary_calls` (every captured in-loop call was refused at an RTOS boundary), `no_eligible_call`. The image verdict is `embedded` if any task embedded a root, otherwise the first of `no_task_registrations`, `no_closed_task_loop` (every task), `no_task_loop_calls` or `task_loop_calls_not_captured` (no task has a captured in-loop call), `only_rtos_boundary_calls`, `no_eligible_call`.
- When no root was planned and no root was refused as unprofitable, the pass reason is the image verdict instead of `no_eligible_call`. `no_profitable_call` keeps precedence. A planned root that later failed (an incomplete plan, or `unpatchable_native_exit` on the root edge) does not take the verdict.

No stats keys are added.

The firmware pass reports selected roots that were actually emitted in `embedded`. Its `private_guarded_returns` count covers return guards embedded inside these executable regions. The B3 public-slot count concerns separately registered slots. An unexpected guest return target or context uses the original lookup after guest return effects complete. Trusted private returns require a Cortex-M3 capture, a bound EnvLayout and an eligible lookup tail. Other CPU models preserve the original return lookup. If a requirement is unavailable, the receipt records a side exit. Trusted interrupt and instruction-count settings still apply.

## ISB side exit in any mode (`isb_boundary`)

The `firmware-inline` parameter `isb_boundary` (bool) makes the [v3 ISB side exit](#firmlooper-v3) a rule of its own, so it can be chosen, measured and decided separately from FirmLooper.

- **Absent** (every shipped pipeline): each mode keeps its own rule. Only `firmlooper-v3` has one; the other modes, `hot-loop` included, expand a branch out of an ISB-ending member like any other branch. Plans, region ids and params hashes are those of the code before the parameter existed.
- **`true`**: every branch out of a member whose last guest instruction is ISB stays a side exit with reason `isb_boundary`, in the recursive plan and in the one-level (flat) plan alike, so the whole-region gate compares two plans that both keep the exit. Root and nested call decisions still come from the policy, so `policy.nested_decisions` is kept. The nested-gain probes of the profit-gated recursion are built without the rule; that is a pricing approximation of a few nanoseconds per affected call. On `firmlooper-v3` the value is redundant and changes nothing.
- **`false`**: the same as absent, except that `firmlooper-v3` refuses it (`invalid_params`), because v3 always keeps the exit.

Receipts record the rule in force as `policy.isb_boundary` (true for v3 and for `isb_boundary = true`), and, when the parameter turned it on, `policy.isb_sources`: the number of snapshot units that end in ISB. Each cut edge of an embedded region is a `residual` row with reason `isb_boundary`.

Without the rule, a PendSV that cloned kernel code requests just before an ISB (FreeRTOS `portYIELD_WITHIN_API`, for example in `xTaskResumeAll` and `xQueueGenericSend`) is taken at the region exit instead. The trusted contract (`interrupts=disabled`) allows that deferral.

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
were refused, the outcome reason is `no_profitable_call`. Each embedded row's
`residual_exits_chainable` is the priced value (`region_exits_chainable`); whether the
linker actually gave the region member descriptors is in the artifact sidecar
(`x_b3.firmware_region_links`). `policy.member_slot_skip` and the stat
`member_slot_skip` record the member-skip decision.

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

`sealed-firmlooper-v2` and `sealed-firmlooper-v3` can use a local symbolized reference ELF to recover names for RTOS policy selection. Supply `--elf` with the actual target used for the capture. For example, run the standalone diagnostic and the optimizer as follows:

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
