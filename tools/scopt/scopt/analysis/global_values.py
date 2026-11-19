"""Conservative byte-level environment summaries and cross-TB exact facts."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from capstone.x86_const import X86_OP_IMM, X86_OP_REG

from .api import AnalysisSpec, ScopeRef
from .program import Program
from .store import AnalysisInput, value_digest
from .exit_facts import find_lookup_tails
from .hconst import FACT_FIELDS, _CALLER_SAVED, _State, _step, decode_one, fact_field_names
from .helper_effects import effects_for_layout, refresh_effects_for_layout
from .hostops import OpKind

ByteValues = tuple[tuple[int, int], ...]
ByteBits = tuple[tuple[int, int, int], ...]  # byte offset, known mask, known-one value
EFFECT_INPUT = "global-helper-effects"


def bind_effect_input(store, layout) -> None:
    effects = refresh_effects_for_layout(layout)
    identity = value_digest((getattr(layout, "envlayout_id", None), effects.table_id))
    store.set_input(EFFECT_INPUT, AnalysisInput(identity, effects))


@dataclass(frozen=True, slots=True)
class KnownBits:
    width: int
    mask: int
    value: int

    @property
    def unsigned_bounds(self) -> tuple[int, int]:
        unknown = ((1 << self.width) - 1) ^ self.mask
        return self.value, self.value | unknown

    @property
    def signed_bounds(self) -> tuple[int, int]:
        sign = 1 << (self.width - 1)
        # Bias two's-complement order by flipping the sign bit. If the sign
        # bit is unknown it must still be unknown in the biased domain.
        biased = KnownBits(self.width, self.mask, self.value ^ (sign & self.mask))
        low, high = biased.unsigned_bounds
        return low - sign, high - sign


def field_bits(bits: ByteBits, start: int, width: int) -> KnownBits:
    """Project a little-endian field to a fixed-width bitvector."""
    if width <= 0:
        raise ValueError("field width must be positive")
    mask = value = 0
    for off, byte_mask, byte_value in bits:
        if start <= off < start + width:
            shift = (off - start) * 8
            mask |= byte_mask << shift
            value |= byte_value << shift
    return KnownBits(width * 8, mask, value)


def _meet_bits(before: ByteBits, incoming: ByteBits) -> ByteBits:
    prior = {off: (mask, val) for off, mask, val in before}
    result = []
    for off, mask, val in incoming:
        old_mask, old_val = prior.get(off, (0, 0))
        agreed = old_mask & mask & ~(old_val ^ val)
        if agreed:
            result.append((off, agreed, val & agreed))
    return tuple(result)



@dataclass(frozen=True, slots=True)
class EnvTransfer:
    preserved: frozenset[int]
    constants: ByteValues = ()

    def apply_bits(self, bits: ByteBits) -> ByteBits:
        output = {off: (mask, val) for off, mask, val in bits if off in self.preserved}
        # A native store's low eight bits are its byte value, even for a
        # negative immediate or an arithmetic result that wrapped.
        output.update((off, (255, val & 255)) for off, val in self.constants)
        return tuple((off, mask, val) for off, (mask, val) in sorted(output.items()))


UNKNOWN = EnvTransfer(frozenset())


@dataclass(frozen=True, slots=True)
class ExitTransfer:
    site_off: int
    kind: str
    effect: EnvTransfer
    must_write: frozenset[int]


@dataclass(frozen=True, slots=True)
class LocalSummary:
    uid: str
    effect_id: str | None
    status: str
    transfers: tuple[ExitTransfer, ...] = ()
    may_read: frozenset[int] = frozenset()
    may_write: frozenset[int] = frozenset()
    reg_uses: frozenset[str] = frozenset()
    reg_defs: frozenset[str] = frozenset()
    flag_uses: frozenset[str] = frozenset()
    flag_defs: frozenset[str] = frozenset()
    calls: tuple[tuple[int, str | None], ...] = ()
    observations: tuple[tuple[int, str], ...] = ()
    opaque_effect: bool = False
    preserved_at_tails: tuple[tuple[int, frozenset[int]], ...] = ()


def byte_footprint(op: Any, fx: Any, tracked: frozenset[int], *, registers_only: bool = False) -> tuple[
        frozenset[int], frozenset[int], frozenset[int], bool]:
    """Read, may-write, definite overwrite, and opaque effect."""
    if op.kind == OpKind.CALL:
        effect = fx.resolved(op.helper_name, op.helper_flags)
        if effect.effect.hostmem_writes:
            return tracked, tracked, frozenset(), True
        return (frozenset(b for b in tracked if effect.reads(b)),
                frozenset(b for b in tracked if effect.writes(b)),
                frozenset(), not effect.rowed)
    if ((op.kind == OpKind.OPAQUE and not registers_only) or op.kind == OpKind.GUEST_ACCESS
            or (op.kind == OpKind.HOST_ABS_STORE and op.log is None)):
        return tracked, tracked, frozenset(), True
    read, written, must = set(), set(), set()
    for acc in op.env:
        bytes_ = tracked.intersection(range(acc.disp, acc.disp + acc.width))
        if "r" in acc.rw:
            read.update(bytes_)
        if "w" in acc.rw:
            written.update(bytes_)
            if op.is_env_store and acc.classified:
                must.update(bytes_)
    return frozenset(read), frozenset(written), frozenset(must), False


def summarize_unit(unit, host, cfg, layout, fx, *, max_steps: int = 100_000) -> LocalSummary:
    if unit.meta_status != "ok" or not host.ok or not cfg.ok or not host.ops or layout is None:
        return LocalSummary(unit.uid, fx.table_id, "opaque_input", opaque_effect=True)
    fields = {}
    for name in FACT_FIELDS:
        env_name = fact_field_names(name)
        if layout.has_field(env_name):
            field = layout.field(env_name)
            if field.size == 4:
                fields[name] = frozenset(range(field.off, field.off + 4))
    tracked = frozenset().union(*fields.values())
    def register_operation(op):
        if op.kind != OpKind.OPAQUE:
            return False
        insn = decode_one(unit.code, op.off, op.size)
        if insn is None or insn.mnemonic not in (
                "add", "sub", "xor", "and", "or", "inc", "dec", "not", "neg", "cmp", "test",
                "shr", "sar", "shl", "rol", "ror", "bswap"):
            return False
        return bool(insn.operands) and all(o.type in (X86_OP_REG, X86_OP_IMM) for o in insn.operands)

    footprints = tuple(byte_footprint(op, fx, tracked, registers_only=register_operation(op))
                       for op in host.ops)
    states = {cfg.entry: (_State(), tracked, frozenset())}
    outputs = {}
    queue = deque([cfg.entry])
    pending = {cfg.entry}
    for _ in range(max_steps):
        if not queue:
            break
        idx = queue.popleft()
        pending.remove(idx)
        state, pres, must = states[idx]
        op = host.ops[idx]
        _, writes, definite, opaque = footprints[idx]
        values, regs, flags = state.d()
        kept = {name: val for name, val in values.items() if not writes & fields[name]}
        if op.kind == OpKind.CALL:
            out = _State.of(kept, {r: v for r, v in regs.items() if r not in _CALLER_SAVED}, {})
        else:
            out, _ = _step(host, idx, unit.code, _State.of(kept, regs, flags), frozenset(fields), fx)
            if opaque:
                out = _State((), out.regs, out.flags)
        transfer = out, pres - writes, must | definite
        outputs[idx] = transfer
        for nxt in cfg.successors(idx, atomic=False):
            update = transfer
            if nxt in states:
                previous, p_old, m_old = states[nxt]
                update = previous.meet(out), p_old & transfer[1], m_old & transfer[2]
            if states.get(nxt) != update:
                states[nxt] = update
                if nxt not in pending:
                    queue.append(nxt)
                    pending.add(nxt)
    if queue:
        return LocalSummary(unit.uid, fx.table_id, "local_summary_budget", opaque_effect=True)
    exits = []
    for idx, kind in sorted(cfg.exits.items()):
        if idx not in outputs:
            continue
        state, preserved, must = outputs[idx]
        constants = tuple(sorted((off, (val >> (8 * i)) & 255) for name, val in state.env
                                 for i, off in enumerate(sorted(fields[name]))))
        exits.append(ExitTransfer(host.ops[idx].off, kind, EnvTransfer(preserved, constants), must))
    ops = [host.ops[i] for i in sorted(states)]
    effects = [footprints[i] for i in sorted(states)]
    may_read = frozenset().union(*(r for r, _, _, _ in effects))
    may_write = frozenset().union(*(w for _, w, _, _ in effects))
    calls = tuple((o.off, o.helper_name) for o in ops if o.kind == OpKind.CALL)
    observations = [(o.off, "guest_memory_or_mmio") for o in ops if o.kind == OpKind.GUEST_ACCESS]
    for o in ops:
        if o.kind == OpKind.CALL:
            fx_row = fx.resolved(o.helper_name, o.helper_flags).effect
            observations.append((o.off, "helper_nonlocal_or_fault" if fx_row.may_fault
                                 or not fx_row.returns else "helper"))
    observations.extend((host.ops[i].off, k) for i, k in sorted(cfg.exits.items()) if i in states)
    tails = []
    indices = {o.off: i for i, o in enumerate(host.ops)}
    for _idx, tail, refusal in find_lookup_tails(host):
        if tail is None or refusal is not None:
            continue
        idx = indices.get(tail.off)
        if idx in states:
            tails.append((tail.off, states[idx][1]))
    return LocalSummary(
        unit.uid, fx.table_id, "ok", tuple(exits), may_read, may_write,
        frozenset().union(*(o.regs_read for o in ops)),
        frozenset().union(*(o.regs_written | o.regs_partial for o in ops)),
        frozenset().union(*(o.flags_read for o in ops)),
        frozenset().union(*(o.flags_written for o in ops)),
        calls, tuple(sorted(observations)), any(o for _, _, _, o in effects), tuple(sorted(tails)),
    )


@dataclass(frozen=True, slots=True)
class FlowArc:
    """An abstract transfer on a proven or assumed edge."""

    source: str
    target: str
    transfer: EnvTransfer


@dataclass(frozen=True, slots=True)
class GlobalValues:
    entries: tuple[tuple[str, ByteValues], ...]
    exhausted: bool
    known_bits: tuple[tuple[str, ByteBits], ...] = ()


def _components(nodes: frozenset[str], outgoing: dict[str, list[FlowArc]]) -> list[tuple[str, ...]]:
    """Iterative Kosaraju order, with SCCs preceding their outgoing targets."""
    incoming: dict[str, list[str]] = defaultdict(list)
    for source, arcs in outgoing.items():
        for arc in arcs:
            incoming[arc.target].append(source)
    seen = set()
    finishing = []
    for root in sorted(nodes):
        if root in seen:
            continue
        stack = [(root, False)]
        while stack:
            node, done = stack.pop()
            if done:
                finishing.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend((arc.target, False) for arc in reversed(outgoing[node]))
    seen.clear()
    components = []
    for root in reversed(finishing):
        if root in seen:
            continue
        members = []
        work = [root]
        seen.add(root)
        while work:
            node = work.pop()
            members.append(node)
            for pred in incoming[node]:
                if pred not in seen:
                    seen.add(pred)
                    work.append(pred)
        components.append(tuple(sorted(members)))
    return components


def solve_values(nodes: Iterable[str], arcs: Iterable[FlowArc], roots: Iterable[str], *,
                 max_steps: int = 1_000_000) -> GlobalValues:
    """Solve a monotone fixed point, including SCCs.

    Conflicts drop only the differing bits. Entry exact values are a
    projection of fully known bytes. No bound on executions is assumed.
    """
    uids = frozenset(nodes)
    states: dict[str, ByteBits] = {root: () for root in roots if root in uids}
    outgoing: dict[str, list[FlowArc]] = defaultdict(list)
    for i, arc in enumerate(arcs):
        if i >= max_steps:
            return GlobalValues(tuple((uid, ()) for uid in sorted(uids)), True)
        if arc.source not in uids or arc.target not in uids:
            continue
        outgoing[arc.source].append(arc)
    steps = 0
    for group in _components(uids, outgoing):
        members = frozenset(group)
        queue = deque(uid for uid in group if uid in states)
        pending = set(queue)
        while queue:
            uid = queue.popleft()
            pending.remove(uid)
            for arc in outgoing[uid]:
                steps += 1
                if steps > max_steps:
                    return GlobalValues(tuple((u, ()) for u in sorted(uids)), True)
                propagated = arc.transfer.apply_bits(states[uid])
                if arc.target in states:
                    propagated = _meet_bits(states[arc.target], propagated)
                if states.get(arc.target) == propagated:
                    continue
                states[arc.target] = propagated
                if arc.target in members and arc.target not in pending:
                    queue.append(arc.target)
                    pending.add(arc.target)
    bits = tuple(sorted(states.items()))
    exact = tuple((uid, tuple((off, val) for off, mask, val in rows if mask == 255))
                  for uid, rows in bits)
    return GlobalValues(exact, False, bits)


def solve_program(program: Program, summaries: dict[str, LocalSummary], *,
                  covered: frozenset[str], roots: frozenset[str],
                  private: tuple[FlowArc, ...] = (), max_steps: int = 1_000_000) -> GlobalValues:
    """Meet over general entries, retaining only checked direct-edge transfers.

    A captured goto_tb can pass environment facts on its direct linked path.
    Its candidates cover possible keys, not just a profiled key. The general
    dispatcher, unknown lookup/recovery and external entry remain unknown.
    """
    units = frozenset(u.uid for u in program.units)
    reachable = program.reachable(roots, covered)
    initial = roots | (units - covered)
    arcs = []
    for edge in program.edges:
        if edge.source not in reachable:
            continue
        if edge.dynamic:
            initial |= covered
        transfer = UNKNOWN
        if (edge.kind == "goto_tb" and edge.slot is not None and not edge.dynamic
                and not edge.fallback):
            summary = summaries.get(edge.source)
            if summary is not None:
                exits = [row.effect for row in summary.transfers
                         if row.kind == f"goto_tb:{edge.slot}"]
                if len(exits) == 1:
                    transfer = exits[0]
        for dest in edge.candidates:
            arcs.append(FlowArc(edge.source, dest, transfer))
    # Private variants are not dispatcher roots. Their only entrances are
    # the planned slots and are checked when the artifact is materialized.
    units |= frozenset(arc.target for arc in private)
    return solve_values(units, (*arcs, *private), initial, max_steps=max_steps)


def _local(ctx: Any, scope: ScopeRef) -> LocalSummary:
    host = ctx.analysis("hostops", scope).value
    cfg = ctx.analysis("hcfg", scope).value
    layout = ctx.analysis("envlayout", ScopeRef("snapshot", ctx.snapshot_id)).value["layout"]
    unit = ctx.unit(scope.id)
    binding = ctx.external_input(EFFECT_INPUT)
    effects = binding.value if binding is not None else effects_for_layout(layout)
    return summarize_unit(unit, host, cfg, layout, effects)


LOCAL_SUMMARY = AnalysisSpec(
    name="local-summary", version=1, scope="unit",
    requires=(("envlayout", 1), ("hostops", 1), ("hcfg", 1)),
    inputs=frozenset({"code", "meta", f"external:{EFFECT_INPUT}"}),
    compute=_local, result_schema=LocalSummary,
)
ANALYSES = (LOCAL_SUMMARY,)
