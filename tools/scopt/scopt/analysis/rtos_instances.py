"""Static FreeRTOS creation/use sites and Thumb argument flow.

Facts describe a code location, never the number of dynamic allocations or a
scheduler invariant. Locally forwarded handles and global storage addresses
are reported separately; a load from mutable storage has no proven pointee.
"""
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import lru_cache

from capstone import CS_AC_WRITE
from capstone import arm as A

from ..decode.thumb_sem import _md
from .rtos_cfg import (decode_function, direct_target, jump_tables, predicated, reachable,
                       resolve_function, resolve_veneer)


_TASK_CREATE = {"xTaskCreate", "xTaskCreateStatic", "xTaskCreateAffinitySet",
                "xTaskCreateStaticAffinitySet"}
_OBJECT_CREATE = {
    "xQueueGenericCreate": "queue", "xQueueGenericCreateStatic": "queue",
    "xQueueCreate": "queue", "xQueueCreateStatic": "queue",
    "xQueueCreateMutex": "mutex", "xQueueCreateMutexStatic": "mutex",
    "xSemaphoreCreateMutex": "mutex", "xSemaphoreCreateRecursiveMutex": "mutex",
    "xQueueCreateCountingSemaphore": "semaphore", "xQueueCreateCountingSemaphoreStatic": "semaphore",
    "xSemaphoreCreateBinary": "semaphore", "xSemaphoreCreateCounting": "semaphore",
    "xTimerCreate": "timer", "xTimerCreateStatic": "timer",
    "xEventGroupCreate": "event_group", "xEventGroupCreateStatic": "event_group",
}
_USES = {"xQueueReceive", "xQueuePeek", "xQueueGenericSend", "xQueueSend",
         "xSemaphoreTake", "xQueueSemaphoreTake", "xQueueTakeMutexRecursive",
         "xQueueGiveMutexRecursive", "xTimerGenericCommand", "xTimerGenericCommandFromTask",
         "xEventGroupWaitBits", "xEventGroupSetBits", "xEventGroupClearBits"}
_INSTANCE_APIS = _TASK_CREATE | _OBJECT_CREATE.keys() | _USES
TERMINATE = {"vTaskDelete", "abort", "_exit", "exit", "__assert_func",
             "__assert_fail", "__stack_chk_fail", "k_panic"}
_ARGS = (A.ARM_REG_R0, A.ARM_REG_R1, A.ARM_REG_R2, A.ARM_REG_R3)
_CALLER_SAVED = (*_ARGS, A.ARM_REG_R12, A.ARM_REG_LR)


@dataclass(frozen=True)
class TaskInstance:
    creation_pc: int
    entry: int
    caller_context: tuple[int, ...] = ()


@dataclass
class RtosInstances:
    task_instances: list[TaskInstance]
    registrations: list[dict]
    objects: list[dict]
    uses: list[dict]
    handle_stores: list[dict]


@dataclass(frozen=True)
class _Arg:
    index: int


@dataclass(frozen=True)
class _Stack:
    offset: int


@dataclass(frozen=True)
class _Handle:
    creation_pc: int


@dataclass(frozen=True)
class _Global:
    address: int


def _offset(value, disp):
    if isinstance(value, int):
        return (value + disp) & 0xffffffff
    if isinstance(value, _Stack):
        return _Stack(value.offset + disp)
    return None


def _address(insn, mem, state):
    if mem.index or insn.writeback:
        return None
    base = (insn.address + 4) & ~3 if mem.base == A.ARM_REG_PC else state.get(mem.base)
    return _offset(base, mem.disp)


def _operand(op, state):
    if op.type == A.ARM_OP_IMM:
        return op.imm
    if op.type == A.ARM_OP_REG and not op.shift.value:
        return state.get(op.reg)
    return None


def _join(a, b):
    return {k: v for k, v in a.items() if b.get(k) == v}


def _memory_key(address):
    if isinstance(address, int):
        return ('global', address)
    if isinstance(address, _Stack):
        return ('stack', address.offset)
    return None


def _store(state, key, value, width=4):
    # Unknown alias/width kills all tracked memory. A partial write kills
    # every overlapping word. Only exact complete word stores generate facts.
    for k in tuple(state):
        if isinstance(k, tuple) and (key is None or
                (key[0] == k[0] and k[1] < key[1] + width and key[1] < k[1] + 4)):
            del state[k]
    if key is not None and width == 4 and value is not None:
        state[key] = value


def _transfer(image, insn, before, create_pc, leaf=()):
    st = before.copy()
    ops = insn.operands
    _, written = insn.regs_access()
    for reg in written:
        st.pop(reg, None)
    name = insn.mnemonic.split('.')[0]
    dst = ops[0].reg if ops and ops[0].type == A.ARM_OP_REG else None
    value = None
    if insn.id in (A.ARM_INS_BL, A.ARM_INS_BLX):
        for r in _CALLER_SAVED:
            st.pop(r, None)
        if leaf:
            # Replay only verified straight-line scalar leaves at the actual
            # argument/stack addresses. Unknown stores still kill all aliases.
            memory = before
            for effect in leaf:
                memory = _transfer(image, effect, memory, False)
            for key in tuple(st):
                if isinstance(key, tuple):
                    del st[key]
            st.update((key, val) for key, val in memory.items() if isinstance(key, tuple))
        else:
            _store(st, None, None)
        if create_pc:
            st[A.ARM_REG_R0] = _Handle(insn.address)
    elif insn.id in (A.ARM_INS_MOV, A.ARM_INS_MOVW) and len(ops) == 2:
        value = _operand(ops[1], before)
    elif insn.id == A.ARM_INS_MOVT and len(ops) == 2 and isinstance(before.get(dst), int):
        value = (before[dst] & 0xffff) | (ops[1].imm << 16)
    elif insn.id == A.ARM_INS_ADR and len(ops) == 2:
        value = (((insn.address + 4) & ~3) + ops[1].imm) & 0xffffffff
    elif insn.id in (A.ARM_INS_ADD, A.ARM_INS_SUB, A.ARM_INS_ADDW, A.ARM_INS_SUBW) and len(ops) in (2, 3):
        base = before.get(dst) if len(ops) == 2 else _operand(ops[1], before)
        if len(ops) == 3 and ops[1].type == A.ARM_OP_REG and ops[1].reg == A.ARM_REG_PC:
            base = (insn.address + 4) & ~3
        inc = _operand(ops[-1], before)
        if isinstance(inc, int):
            value = _offset(base, inc if insn.id in (A.ARM_INS_ADD, A.ARM_INS_ADDW) else -inc)
    elif insn.id == A.ARM_INS_LDR and len(ops) == 2 and ops[1].type == A.ARM_OP_MEM:
        addr = _address(insn, ops[1].mem, before)
        key = _memory_key(addr)
        if key in before:
            value = before[key]
        elif isinstance(addr, int):
            if any(lo <= addr and addr + 4 <= hi for lo, hi in image.readonly_ranges):
                raw = image.read(addr, 4)
                if raw is not None:
                    value = int.from_bytes(raw, 'little')
            else:
                value = _Global(addr)
    elif name in ('str', 'strb', 'strh') and len(ops) == 2 and ops[1].type == A.ARM_OP_MEM:
        _store(st, _memory_key(_address(insn, ops[1].mem, before)), before.get(dst),
               {'str': 4, 'strb': 1, 'strh': 2}[name])
    elif insn.id in (A.ARM_INS_PUSH, A.ARM_INS_POP):
        sp = before.get(A.ARM_REG_SP)
        if isinstance(sp, _Stack):
            n = 4 * len(ops)
            new = sp.offset - n if insn.id == A.ARM_INS_PUSH else sp.offset + n
            for idx, op in enumerate(ops):
                key = ('stack', (new if insn.id == A.ARM_INS_PUSH else sp.offset) + idx * 4)
                if insn.id == A.ARM_INS_PUSH:
                    _store(st, key, before.get(op.reg))
                elif key in before:
                    st[op.reg] = before[key]
            st[A.ARM_REG_SP] = _Stack(new)
        elif insn.id == A.ARM_INS_PUSH:
            _store(st, None, None)
    elif (name.startswith(('str', 'stm', 'vst')) or
          any(op.type == A.ARM_OP_MEM and op.access & CS_AC_WRITE for op in ops)):
        _store(st, None, None)
    if dst is not None and value is not None:
        st[dst] = value
    return _join(before, st) if predicated(insn) else st


def value_states(program, func, insns, edges):
    """Must facts at each reachable PC. Conflicting incoming values become unknown."""
    initial = {reg: _Arg(i) for i, reg in enumerate(_ARGS)}
    initial[A.ARM_REG_SP] = _Stack(0)
    states = {func.start: initial}
    pending = deque([func.start] if func.start in edges else [])
    while pending:
        pc = pending.popleft()
        callee = program.calls[func.start].get(pc)
        leaf = program.leaf_body(callee) if callee is not None and pc not in program.api else ()
        after = _transfer(program.image, insns[pc], states[pc], pc in program.objects, leaf)
        for dst in edges[pc]:
            if dst not in edges:
                continue
            old = states.get(dst)
            merged = after if old is None else _join(old, after)
            if old != merged:
                states[dst] = merged
                pending.append(dst)
    return states


class Program:
    """Lightweight cross-reference index; disassembly is retained per active routine."""

    def __init__(self, image):
        self.image = image
        self.md = _md()
        self.ranges = image.code_ranges()
        self.funcs = {}
        self.names = defaultdict(set)
        for f in image.funcs:
            self.funcs.setdefault(f.start, f)
            self.names[f.name].add(f.start)
        api_names = _TASK_CREATE | _OBJECT_CREATE.keys() | _USES | TERMINATE
        apis = {pc: name for name in sorted(api_names) for pc in self.names[name]}
        self.calls = defaultdict(dict)
        self.callers = defaultdict(list)
        self.api = {}
        self.api_entries = {}
        self.terminals = set()
        self.objects = set()
        self.leaf_body = lru_cache(maxsize=128)(self._leaf_body)

        @lru_cache(maxsize=None)
        def target(addr):
            api = resolve_veneer(image, addr, apis, self.ranges, self.md)
            return (api, apis[api]) if api is not None else (
                resolve_veneer(image, addr, self.funcs, self.ranges, self.md), None)

        for f in self.funcs.values():
            insns = self.decode(f)
            for pc, insn in insns.items():
                if insn.id not in (A.ARM_INS_BL, A.ARM_INS_B):
                    continue
                addr = direct_target(insn)
                if addr is None:
                    continue
                if insn.id == A.ARM_INS_B and f.start <= addr < f.end:
                    continue
                callee, api = target(addr)
                if callee is None:
                    continue
                self.calls[f.start][pc] = callee
                self.callers[callee].append((f.start, pc))
                if api is not None:
                    self.api[pc] = api
                    self.api_entries[pc] = callee
                    if api in TERMINATE:
                        self.terminals.add(addr)
                    elif api in _OBJECT_CREATE:
                        self.objects.add(pc)

    def decode(self, func):
        return decode_function(self.image, func, self.ranges, self.md)

    def body(self, pointer):
        return resolve_function(self.image, pointer & ~1, self.funcs, self.ranges, self.md)

    def _leaf_body(self, addr):
        """Straight-line register/word-memory routines whose writes can be replayed."""
        func = self.funcs[addr]
        insns = self.decode(func)
        ops = {A.ARM_INS_PUSH, A.ARM_INS_POP, A.ARM_INS_MOV, A.ARM_INS_MOVT, A.ARM_INS_MOVW,
               A.ARM_INS_ADD, A.ARM_INS_SUB, A.ARM_INS_ADR, A.ARM_INS_ADDW, A.ARM_INS_SUBW,
               A.ARM_INS_LDR, A.ARM_INS_STR, A.ARM_INS_STRH, A.ARM_INS_STRB, A.ARM_INS_NOP}
        pc, body = addr, []
        while pc in insns:
            i = insns[pc]
            if predicated(i):
                break
            if i.id == A.ARM_INS_BX and i.operands[0].reg == A.ARM_REG_LR:
                return tuple(body)
            if i.id == A.ARM_INS_POP and A.ARM_REG_PC in i.regs_access()[1]:
                return tuple(body)
            if (i.id not in ops and i.mnemonic != "nop") or A.ARM_REG_PC in i.regs_access()[1]:
                break
            body.append(i)
            pc += i.size
        return ()

    def cfg(self, func, insns):
        tables = jump_tables(self.image, func, insns)
        return (*reachable(func, insns, self.terminals, tables), tables)


def discover_instances(program):
    """Recover direct registrations, including parameter-forwarding wrappers.

    Each resolved wrapper call context is evidence of a possible task, not a
    claim that the wrapper has no other callers (including indirect callers).
    """
    rows, tasks, objects, uses, stores = [], [], [], [], []
    image = program.image

    @lru_cache(maxsize=32)
    def facts(start):
        f = program.funcs[start]
        insns = program.decode(f)
        edges, _, _ = program.cfg(f, insns)
        return insns, value_states(program, f, insns, edges)

    def origins(start, value):
        pending = [(start, value, ())]
        while pending:
            owner, fact, context = pending.pop()
            if not isinstance(fact, _Arg):
                yield fact, context
                continue
            yield None, context  # other indirect/external callers remain unknown
            for caller, pc in program.callers[owner]:
                if pc in context:
                    continue
                _, states = facts(caller)
                if pc in states:
                    pending.append((caller, states[pc].get(_ARGS[fact.index]), (*context, pc)))

    for owner, calls in program.calls.items():
        if not any(program.api.get(pc) in _INSTANCE_APIS for pc in calls):
            continue
        func = program.funcs[owner]
        insns, states = facts(owner)
        previous = {i.address + i.size: i for i in insns.values()}
        for pc in sorted(calls):
            api = program.api.get(pc)
            if api not in _INSTANCE_APIS:
                continue
            state = states.get(pc)
            row = dict(registrar=func.name, call_pc=hex(pc), api_entry=hex(calls[pc]))
            if api in _TASK_CREATE:
                if state is None:
                    rows.append(dict(row, status="unreachable_or_unresolved_registration"))
                    continue
                for val, ctx in origins(owner, state.get(A.ARM_REG_R0)):
                    entry = program.body(val) if isinstance(val, int) and val & 1 else None
                    evidence = dict(row)
                    if ctx:
                        evidence["wrapper_calls"] = [hex(c) for c in reversed(ctx)]
                    prev = previous.get(pc)
                    if (prev is not None and prev.id == A.ARM_INS_LDR and len(prev.operands) == 2
                            and prev.operands[0].reg == A.ARM_REG_R0
                            and prev.operands[1].type == A.ARM_OP_MEM
                            and prev.operands[1].mem.base == A.ARM_REG_PC and not predicated(prev)
                            and isinstance(val, int)):
                        literal = _address(prev, prev.operands[1].mem, {})
                        raw = image.read(literal, 4) if isinstance(literal, int) else None
                        if raw and int.from_bytes(raw, 'little') == val:
                            evidence["literal"] = hex(literal)
                    if isinstance(val, int):
                        evidence["pointer"] = hex(val)
                    if entry is None:
                        rows.append(dict(evidence, status="unresolved_task_pointer"))
                    else:
                        tasks.append(TaskInstance(pc, entry, ctx))
                        rows.append(dict(evidence, status="resolved", entry=hex(entry)))
            elif state is not None and pc in program.objects:
                kind = _OBJECT_CREATE[api]
                if api == "xQueueGenericCreate":
                    kind = {0: "queue", 1: "mutex", 2: "semaphore", 3: "semaphore",
                            4: "mutex"}.get(state.get(A.ARM_REG_R2), "queue_family")
                elif api == "xQueueGenericCreateStatic":
                    kind = "queue_family"
                objects.append({**row, "kind": kind, "api": api, "identity": hex(pc)})
            elif state is not None:
                for val, ctx in origins(owner, state.get(A.ARM_REG_R0)):
                    use = {**row, "api": api, "status": "unknown_handle"}
                    if ctx:
                        use["wrapper_calls"] = [hex(c) for c in reversed(ctx)]
                    if isinstance(val, _Handle):
                        use.update(status="local_creation_flow", creation_pc=hex(val.creation_pc))
                    elif isinstance(val, _Global):
                        use.update(status="global_storage", storage=hex(val.address))
                    uses.append(use)
        for pc, state in states.items():
            insn = insns.get(pc)
            if insn is None or insn.id != A.ARM_INS_STR or len(insn.operands) != 2:
                continue
            reg, mem = insn.operands
            if mem.type != A.ARM_OP_MEM:
                continue
            val = state.get(reg.reg)
            addr = _address(insn, mem.mem, state)
            if isinstance(addr, int) and isinstance(val, _Handle):
                stores.append(dict(pc=hex(pc), storage=hex(addr), creation_pc=hex(val.creation_pc)))
    return RtosInstances(tasks, rows, objects, uses, stores)
