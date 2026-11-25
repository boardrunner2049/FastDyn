# FreeRTOS task and object analysis

FirmLooper reads Thumb code, ELF symbols, mapping symbols and section flags. A profile is optional. To inspect the full selection on an ELF, run the following in the scopt Python environment.

```sh
python - /path/to/firmware.elf <<'PY'
import json, sys
from scopt.analysis.task_loops import discover_task_loops
from scopt.decode.guest_symbols import load_guest_image
result = discover_task_loops(load_guest_image(sys.argv[1]), include_terminal=True)
print(json.dumps(result.report(), indent=2))
PY
```

### Task registrations and handles

The analysis indexes direct calls and tail transfers to FreeRTOS creation APIs and follows unconditional Thumb entry veneers. At each task registration it traces R0 through moves, immediate halves, address formation, word loads, stack slots, direct wrappers and branches whose incoming values agree. An odd pointer that resolves to a function entry identifies a possible task. The receipt keeps the registered pointer, resolved body and wrapper call sites. Conflicting paths, unknown writes, mutable global loads and indirect callers can leave the entry unresolved. Straight-line scalar callbacks can preserve a caller's stack slot when their memory accesses can be tracked at the actual argument addresses.

The object receipt covers recognized queue, semaphore, mutex, timer and event-group APIs. A creation identity denotes a static site that may allocate repeatedly. A local handle can reach a named use through registers, memory or wrapper arguments. `global_storage` records the address of the mutable word read by a use. `handle_stores` records observed static creation-to-store flow. Other possible writers, interrupt users, waiter sets and scheduling state remain unknown. These observations do not authorize lock removal.

### Selecting steady-state work

A selected natural loop has a header dominating its backedges. The analysis excludes initialization before that header. `persistent_strong` means there are no modeled exits. With `include_terminal=True`, `persistent_with_terminal_exit` also admits exits where every modeled cleanup path reaches a recognized process termination, assertion failure or self-deletion. Calling `vTaskDelete` with an unknown handle cannot establish self-deletion. A normal return, arbitrary hook, unknown transfer or cleanup cycle cannot establish termination. Under analysis revision 2 with `task_end` (FirmLooper v3, `discover_task_loops(..., revision=2, task_end=True)`), an unpredicated return from a task entry recovered from a FreeRTOS creation site is a terminal, because FreeRTOS starts each task with LR set to `prvTaskExitError`. Explicit entries do not get it, and cleanup cycles, one-shot loops and inner loops still fail.

Direct and indirect calls may block. A returning `BLX` continues at its fallthrough. Resolved Thumb switch tables require an unsigned immediate index guard, an aligned PC-relative `TBB` or `TBH`, mapped table bytes, complete intra-function instruction targets and no alternate known entry after the comparison. Analysis revision 2 also resolves GCC's Thumb-2 word table `adr base,table; ldr.w pc,[base,idx,lsl #2]` under the same guards, plus `base != idx`, a 4-aligned table and words that all have the Thumb bit set. The same revision follows an unsymbolized BOLT `b.w` entry patch outside the Thumb code ranges to the moved function (see [FirmLooper v3](firmware-inline-policies.md#firmlooper-v3)). Only FirmLooper v3 uses revision 2; `rtos_storage`, the analysis CLIs, `firmlooper` and `firmlooper-v2` stay on revision 1. Other shapes remain unresolved. Ordinary loops and exceptional/unknown edges keep their original runtime behavior.

For each cycle, the receipt records its header, classification, instruction PCs, direct calls, exits and terminal cleanup PCs. Revision 2 also records the terminal kinds of `persistent_with_terminal_exit` cycles and, for rejected cycles, either `nested_in_selected_loop` or the reasons each exit blocks selection. Call PCs are candidates for the region builder, which independently checks captured code and supported control flow. An ELF analysis alone cannot establish that a selected site executed.

### Attribute recovered services to FreeRTOS submodules

Use the symbolized reference build's DWARF compilation units to attribute kernel code to the task, queue/semaphore/mutex, timer, list, event, stream/message, co-routine, heap or port module. An application source file that happens to share a kernel filename is excluded. For a nonstandard directory layout pass `--kernel-dir` using the path as recorded in DWARF. The target can also be the symbolized ELF itself. For example:

```sh
python -m scopt.analysis.rtos_opportunities packet.stripped.elf \
  --reference packet.debug.elf --tb-profile packet.tbprof > modules.json
```

A TB profile is optional. Supply a diagnostic recorded on that exact target with FirmBOLT's TB profiler. The tool sums executions of TBs whose start equals the recovered function entry across contexts. A tail transfer or loop back to that address also counts. The reported numbers mean entry-address TB executions. They do not estimate CPU cycles, unique invocations or time in the module. A zero can mean that the function was inlined, omitted by the linker, missing from debug information, or unexercised. The function list includes the matching method and target PC for each attributable row. Cross-build names inherit the limits of the signature matcher.

### Find RTOS objects that can own permanent storage

Given a function the application declares to run at most once per boot, the storage report lists direct object creations, their arguments and the destination variables for recovered handles. For a stripped guest, supply a companion with the same load image so function and data addresses agree.

```sh
python -m scopt.analysis.rtos_storage packet.stripped.elf \
    --reference packet.debug.elf --once-function main_compute
```

For the packet firmware, the report identifies `xFreeQ` (8 one-byte items), `xWorkQ` (9 one-byte items), `xResultQ` (2 items of 68 bytes), `xMonitorQ` (4 one-byte items), the mutex and the heartbeat timer. Each has its own source constructor. A matching kernel entry can implement several public macros, e.g. a stream buffer with or without callbacks. The source determines which constructor to rewrite.

A creation inside a control-flow cycle, ambiguous handle storage, missing queue dimensions, or an unresolved branch can leave a site unsuitable for direct conversion. A candidate still requires the application's once-entry and ownership contract, the matching source, and agreement to the changed allocation and failure behavior. This analysis handles calls in the named routine. An arbitrary helper, factory or allocator in the rest of the image can have a different lifetime. [Build-assisted static-object conversion](https://github.com/boardrunner2049/firmbolt/blob/treb-wip/tools/rtos-specialize/static_objects.py)
