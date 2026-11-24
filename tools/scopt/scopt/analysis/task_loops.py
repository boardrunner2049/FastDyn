"""Recognize task loops from Thumb control flow and FreeRTOS creation sites.

Calls may block; a persistent loop describes the normal-return graph and is
not a guarantee of future execution. These facts affect selection only.
"""
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass

from capstone import arm as A

from ..decode.guest_symbols import GuestImage
from .rtos_cfg import predicated
from .rtos_instances import Program, TERMINATE, discover_instances, value_states


@dataclass
class TaskLoops:
    call_pcs: frozenset[int]
    instruction_pcs: frozenset[int]
    registrations: list[dict]
    tasks: list[dict]
    objects: list[dict]
    uses: list[dict]
    handle_stores: list[dict]

    def report(self) -> dict:
        return {"discovery": "thumb_argument_flow_or_explicit_entry",
                "loop_rule": "dominating header with closed or classified terminal exits",
                "call_model": "direct and indirect calls may block; normal return resumes at fallthrough",
                "registrations": self.registrations, "tasks": self.tasks,
                "objects": self.objects, "handle_uses": self.uses, "handle_stores": self.handle_stores,
                "selected_call_sites": len(self.call_pcs),
                "selected_instructions": len(self.instruction_pcs)}


def _components(edges, predecessors):
    """Iterative Kosaraju pass, including very large task bodies."""
    order, seen = [], set()
    for root in edges:
        todo = [(root, False)]
        while todo:
            pc, finish = todo.pop()
            if finish:
                order.append(pc)
            elif pc not in seen:
                seen.add(pc)
                todo.append((pc, True))
                todo.extend((v, False) for v in edges[pc] if v in edges and v not in seen)
    seen.clear()
    for root in reversed(order):
        if root in seen:
            continue
        comp, todo = set(), [root]
        seen.add(root)
        while todo:
            pc = todo.pop()
            comp.add(pc)
            for pred in predecessors[pc]:
                if pred not in seen:
                    seen.add(pred)
                    todo.append(pred)
        yield comp


def _natural_loops(edges, pred, entry):
    """Immediate dominators in reverse postorder, then predecessor closure of backedges."""
    if entry not in edges:
        return {}
    seen, post = set(), []
    todo = [(entry, False)]
    while todo:
        pc, finish = todo.pop()
        if finish:
            post.append(pc)
        elif pc not in seen:
            seen.add(pc)
            todo.append((pc, True))
            todo.extend((v, False) for v in edges[pc] if v in edges and v not in seen)
    order = list(reversed(post))
    rank = {pc: i for i, pc in enumerate(order)}
    idom = {entry: entry}

    def intersect(a, b):
        while a != b:
            if rank[a] > rank[b]:
                a = idom[a]
            else:
                b = idom[b]
        return a

    changed = True
    while changed:
        changed = False
        for pc in order[1:]:
            parents = (v for v in pred[pc] if v in idom)
            parent = next(parents)
            for other in parents:
                parent = intersect(parent, other)
            if idom.get(pc) != parent:
                idom[pc] = parent
                changed = True

    headers = defaultdict(set)
    for tail, dsts in edges.items():
        for header in dsts:
            if header in rank and rank[header] <= rank[tail] and intersect(tail, header) == header:
                headers[header].add(tail)
    loops = {}
    for header, tails in headers.items():
        body = {header}
        todo = list(tails)
        while todo:
            pc = todo.pop()
            if pc not in body:
                body.add(pc)
                todo.extend(pred[pc] - body)
        loops[header] = body
    return loops


def _task(program, func, include_terminal):
    insns = program.decode(func)
    edges, pred, tables = program.cfg(func, insns)
    terminal = set()
    if any(program.api.get(pc) in TERMINATE for pc in edges):
        states = value_states(program, func, insns, edges)
        for pc in edges:
            name = program.api.get(pc)
            if (name in TERMINATE and not predicated(insns[pc]) and
                    (name != "vTaskDelete" or states[pc].get(A.ARM_REG_R0) == 0)):
                terminal.add(pc)

    # Remove a spurious normal-return path from an established noreturn call.
    if terminal:
        todo, live = [func.start], {}
        while todo:
            pc = todo.pop()
            if pc in edges and pc not in live:
                live[pc] = () if pc in terminal else edges[pc]
                todo.extend(live[pc])
        edges = live
        pred = defaultdict(set)
        for pc, dsts in edges.items():
            for dst in dsts:
                if dst in edges:
                    pred[dst].add(pc)

    # Least fixed point: an exit's reachable cleanup must terminate on *every*
    # modeled path. An unrelated loop, unknown exit or ordinary return fails.
    leads_to_terminal = set(terminal) & edges.keys()
    queue = deque(leads_to_terminal)
    while queue:
        dst = queue.popleft()
        for pc in pred[dst] - leads_to_terminal:
            if all(n in leads_to_terminal for n in edges[pc]):
                leads_to_terminal.add(pc)
                queue.append(pc)

    selected, calls, cycles = set(), set(), []
    for header, body in sorted(_natural_loops(edges, pred, func.start).items()):
        exits = {dst for pc in body for dst in edges[pc] if dst not in body}
        if not exits:
            cls = "persistent_strong"
        elif exits <= leads_to_terminal:
            cls = "persistent_with_terminal_exit"
        else:
            cls = None
        admitted = cls == "persistent_strong" or (include_terminal and cls is not None)
        sites = sorted(pc for pc in body if insns[pc].id == A.ARM_INS_BL)
        if admitted:
            selected.update(body)
            calls.update(sites)
        cleanup, todo = set(), list(exits & leads_to_terminal)
        while todo:
            pc = todo.pop()
            if pc not in cleanup:
                cleanup.add(pc)
                todo.extend(edges[pc])
        cycles.append({
            "header": hex(header), "entries": [hex(header)],
            "classification": cls, "status": "selected" if admitted else "cycle_has_exit",
            "instructions": len(body), "instruction_pcs": list(map(hex, sorted(body))),
            "direct_call_pcs": list(map(hex, sites)), "terminal_pcs": list(map(hex, sorted(cleanup))),
            "outgoing": [hex(dst) for dst in sorted(exits - {None})]
                        + (["unknown_or_termination"] if None in exits else []),
        })
    for comp in _components(edges, pred):
        entries = sorted(pc for pc in comp if pc == func.start or pred[pc] - comp)
        if len(entries) > 1:
            cycles.append({"status": "multiple_entry_cycle", "classification": None,
                           "entries": list(map(hex, entries)), "instructions": len(comp)})
    return selected, calls, {"name": func.name, "entry": hex(func.start),
                             "reachable_instructions": len(edges),
                             "status": "selected" if selected else "no_closed_task_loop",
                             "cycles": cycles, "jump_tables": {
                                 hex(k): list(map(hex, v)) for k, v in tables.items() if k in edges}}


def discover_task_loops(image: GuestImage, task_entries: Iterable[str | int] = (), *,
                        include_terminal: bool = False) -> TaskLoops:
    program = Program(image)
    instances = discover_instances(program)
    roots = {t.entry for t in instances.task_instances}
    registrations = instances.registrations
    for entry in task_entries:
        if entry in program.names:
            matches = {program.body(pc) for pc in program.names[entry]}
        else:
            try:
                pc = int(entry, 0) if isinstance(entry, str) else entry
                matches = {program.body(pc)}
            except (TypeError, ValueError):
                matches = set()
        matches.discard(None)
        row = {"explicit_entry": entry, "status": "unknown_or_ambiguous_task_entry"}
        if len(matches) == 1:
            root = next(iter(matches))
            roots.add(root)
            row.update(status="resolved", entry=hex(root))
        registrations.append(row)
    all_insns, all_calls, tasks = set(), set(), []
    for root in sorted(roots):
        insns, calls, report = _task(program, program.funcs[root], include_terminal)
        all_insns.update(insns)
        all_calls.update(calls)
        tasks.append(report)
    return TaskLoops(frozenset(all_calls), frozenset(all_insns), registrations, tasks,
                     instances.objects, instances.uses, instances.handle_stores)
