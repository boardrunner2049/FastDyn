"""Benefit/cost model for firmware-inline regions (``firmware-dispatch-v1``).

A firmware region replaces dispatcher work of the public sealed code with
private edges.  This module prices one *planned* region with the exact
training profile, per run of the training input, so a pass can refuse roots
whose region costs more dispatch work than it removes.  It never adds or
changes region content: the only effect of the model is a refusal.

Flow.  Profile transition probabilities are pushed through the region's
member graph.  For a member ``m`` whose source unit ``u`` executed
``execs(u)`` times, an exit ``x`` mapped to a profile edge kind (``goto_tb``
slot ``s`` -> ``chain<s>``; ``goto_ptr`` -> ``lookup_hit``/``lookup_miss``;
``exit_null``; ``exit_requested``) is taken with probability
``count(u, kind) / execs(u)``.  Flagged edges count: the exit was taken even
when an interrupt was delivered before the next entry.  The flow ``v`` of the
members solves ``v = s + P^T v`` strongly connected component by component in
topological order (``s`` is the caller's executions at the region entry); a
closed component without leak takes the observed executions, and ``v`` never
exceeds ``execs(u)``.  Every inline context has its own flow, so a callee
shared by several roots, or repeated in nested contexts, is not double
counted.

Savings (what the region removes compared with public sealed code), per
dynamic event:

* internal ``goto_tb`` edge: one poll, when the region skips member polls
  (trusted entry offset > 0);
* internal ``goto_ptr`` edge (a branch patched to a direct ``jmp``): one
  ``helper_lookup_tb_ptr`` plus a poll; a guarded edge (every return, and
  every lookup edge of a safe region) pays the guard back;
* an internal lookup that missed in training: a full dispatcher iteration;
* region entry: one poll (trusted entry at +15).

Costs added, per dynamic event:

* residual ``goto_tb`` exit (over *all* member exits without an internal
  edge, including the never-expanded caller and continuation exits): a full
  dispatcher iteration when the region's exits are not chainable
  (:func:`scopt.link.region_exits_chainable` false, or forced with
  ``residual_goto_tb='unchained'``), else nothing -- QEMU chains them like the
  public exit.  In a composed sealed+B3 pipeline ``auto`` prices a trusted
  region unchained when a member source is B3-grown, whatever the QEMU
  member-skip capability: deliberately conservative, because the
  internal-edge credit assumes lookups B3 already chains and residual
  ``goto_ptr`` exits on B3-guarded tails lose the guard in the pre-B3 member
  copies.  An explicit ``chained`` overrides it.  A trusted exit into a
  trusted region entry (its own caller, or another planned root) is priced as
  a dispatch: exact for QEMU before counters revision 4, conservative from
  revision 4 on (that QEMU chains such an exit to the destination entry's +0,
  which costs one poll);
* residual ``goto_ptr``/``exit_null``/``exit_requested``: nothing (the same
  as public code; trusted members never take their poll exit);
* entry: when chaining into a trusted entry is refused (``entry_chain``
  ``unchained``: non-sealed ``CF_PCREL`` captures), one dispatch per chained
  in-edge of the caller;
* footprint: ``byte_ns`` per byte of member code and descriptors, once per
  run.

A root is admitted when ``saved >= (1 + margin) * added``, ``net > 0`` and
``net / calls >= min_net_per_call_ns``.  See docs/firmware-inline-policies.md
for the default constants and how they were derived.
"""

from __future__ import annotations

import math
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from types import SimpleNamespace
from typing import Any

from ..formats.enums import ExitKind
from ..formats.region_v1 import unit_exits

__all__ = ["MODEL", "COST_PARAMS", "COST_MODES", "CHAIN_MODES", "DispatchCosts", "ProfitIndex",
           "RegionEstimate", "ProfitModel", "NestedProfitExpansion", "estimate_region",
           "admission", "resolve_residual_goto_tb", "resolve_entry_chain", "nested_gain"]

MODEL = "firmware-dispatch-v1"
COST_MODES = ("auto", "profile", "off")
CHAIN_MODES = ("auto", "chained", "unchained")
#: Pass parameter -> :class:`DispatchCosts` field.
COST_PARAMS = {"cost_poll_ns": "poll_ns", "cost_lookup_ns": "lookup_ns",
               "cost_guard_ns": "guard_ns", "cost_dispatch_ns": "dispatch_ns",
               "cost_byte_ns": "byte_ns", "cost_margin": "margin",
               "cost_min_net_per_call_ns": "min_net_per_call_ns"}
_GS_LIMIT = 512
_EPS = 1e-12


class ProfitError(ValueError):
    pass


@dataclass(frozen=True)
class DispatchCosts:
    """Nanosecond prices of dispatcher events (defaults: docs/firmware-inline-policies.md)."""

    poll_ns: float = 0.4
    lookup_ns: float = 7.0
    guard_ns: float = 1.0
    dispatch_ns: float = 40.0
    byte_ns: float = 0.25
    margin: float = 0.25
    min_net_per_call_ns: float = 1.0

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "DispatchCosts":
        values = {}
        for name, attr in COST_PARAMS.items():
            if name not in params:
                continue
            value = params[name]
            if (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ProfitError(f"{name} must be a finite number >= 0")
            values[attr] = float(value)
        return cls(**values)

    def to_json(self) -> dict[str, float]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _exit_key(exit_: Any) -> str | None:
    if exit_.kind == ExitKind.GOTO_TB and exit_.slot in (0, 1):
        return f"chain{exit_.slot}"
    if exit_.kind == ExitKind.GOTO_PTR:
        return "lookup"
    if exit_.kind == ExitKind.EXIT_NULL:
        return "exit_null"
    if exit_.kind == ExitKind.EXIT_REQUESTED:
        return "exit_requested"
    return None


class ProfitIndex:
    """Per-unit exit counts and executions of one profile phase."""

    def __init__(self, rows: Iterable[Mapping[str, Any]],
                 executions: Callable[[str], int | None]):
        self.by_kind: dict[str, Counter[str]] = defaultdict(Counter)
        self.out: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
        self.inn: dict[str, Counter[str]] = defaultdict(Counter)
        for e in rows:
            src, dst, kind = e.get("src"), e.get("dst"), e["kind"]
            if src is None or kind == "first" or e["count"] <= 0:
                continue
            self.by_kind[src][kind] += e["count"]
            self.out[src][kind, dst] += e["count"]
            self.inn[dst][kind] += e["count"]
        self._executions = executions
        self._execs: dict[str, int | None] = {}

    @classmethod
    def build(cls, profile: Any, phase: str) -> "ProfitIndex":
        def executions(uid: str) -> int | None:
            count = profile.count(uid, phase, "executions")
            return count.value if count.exact else None
        return cls(profile.edges(phase), executions)

    def execs(self, uid: str) -> int | None:
        if uid not in self._execs:
            self._execs[uid] = self._executions(uid)
        return self._execs[uid]

    def count(self, uid: str, key: str) -> int:
        kinds = self.by_kind.get(uid)
        if not kinds:
            return 0
        if key == "lookup":
            return kinds["lookup_hit"] + kinds["lookup_miss"]
        return kinds[key]

    def chain_into(self, uid: str, key: str, targets: frozenset[str]) -> int:
        """Count of ``key`` (a chain kind) exits of ``uid`` that entered ``targets``."""
        out = self.out.get(uid)
        return sum(n for (kind, dst), n in out.items() if kind == key and dst in targets) \
            if out else 0


@dataclass(frozen=True)
class RegionEstimate:
    status: str  # exact | ambiguous | unknown
    entries: float
    calls: float
    terms: Mapping[str, float]
    bytes: int
    saved_ns: float
    added_ns: float
    footprint_ns: float
    residual_goto_tb: str
    entry_chain: str
    reasons: tuple[str, ...] = ()
    member_net: tuple[float, ...] = field(default=(), compare=False)
    edge_saved: tuple[tuple[int, float, float], ...] = field(default=(), compare=False)
    member_flow: tuple[float, ...] = field(default=(), compare=False)

    @property
    def net_ns(self) -> float:
        return self.saved_ns - self.added_ns - self.footprint_ns

    @property
    def net_ns_per_call(self) -> float:
        return self.net_ns / max(self.calls, 1.0)

    def to_json(self) -> dict[str, Any]:
        r = lambda x: round(x, 3)  # noqa: E731
        return {"model": MODEL, "status": self.status, "entries": r(self.entries),
                "calls": r(self.calls), "terms": {k: r(v) for k, v in sorted(self.terms.items())},
                "bytes": self.bytes, "saved_ns": r(self.saved_ns), "added_ns": r(self.added_ns),
                "footprint_ns": r(self.footprint_ns), "net_ns": r(self.net_ns),
                "net_ns_per_call": r(self.net_ns_per_call),
                "residual_goto_tb": self.residual_goto_tb, "entry_chain": self.entry_chain,
                "reasons": list(self.reasons)}


def resolve_residual_goto_tb(param: str, plan: Any,
                             b3_grown: Iterable[str] = ()) -> str:
    """``chained``/``unchained`` for the region's residual ``goto_tb`` exits.

    ``auto``: safe regions keep their public slots (chained); trusted regions
    chain exactly when :func:`scopt.link.region_exits_chainable` holds for
    ``b3_grown``, the units whose exits earlier B3 passes of the same
    pipeline guard (grown with registered slots; empty when firmware-inline
    runs alone).  This is deliberately conservative and does not depend on
    the QEMU member-skip capability: a region priced chained is always linked
    with member descriptors, but a grown member source prices unchained even
    when the linker can skip its B3 slots (see the module docstring).
    """
    if param in ("chained", "unchained"):
        return param
    if param != "auto":
        raise ProfitError(f"residual_goto_tb must be one of {CHAIN_MODES}")
    if not plan.trusted:
        return "chained"
    from ..link import region_exits_chainable

    return "chained" if region_exits_chainable(plan, b3_grown) else "unchained"


def resolve_entry_chain(param: str, plan: Any, unit_of: Callable[[str], Any]) -> str:
    """``auto``: QEMU refuses chains into a trusted entry unless the loader is
    sealed; a ``CF_PCREL`` entry key marks a non-sealed capture."""
    if param in ("chained", "unchained"):
        return param
    if param != "auto":
        raise ProfitError(f"entry_chain must be one of {CHAIN_MODES}")
    if not plan.trusted:
        return "chained"
    from ..decode.search import is_pcrel

    entry = unit_of(plan.members[plan.entry_member].source_uid)
    return "unchained" if is_pcrel(entry.key.cflags) else "chained"


def _sccs(n: int, succ: list[list[int]]) -> list[list[int]]:
    """Tarjan without recursion; components in topological order."""
    index = [-1] * n
    low = [0] * n
    on = [False] * n
    stack: list[int] = []
    out: list[list[int]] = []
    counter = 0
    for root in range(n):
        if index[root] >= 0:
            continue
        work = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on[v] = True
            recurse = False
            while i < len(succ[v]):
                w = succ[v][i]
                i += 1
                if index[w] < 0:
                    work.append((v, i))
                    work.append((w, 0))
                    recurse = True
                    break
                if on[w]:
                    low[v] = min(low[v], index[w])
            if recurse:
                continue
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on[w] = False
                    comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    out.reverse()
    return out


def _solve_component(comp: list[int], inner: Mapping[tuple[int, int], float],
                     b: Mapping[int, float]) -> list[float] | None:
    """Solve ``v = b + A v`` on one component (``inner[(src, dst)]`` = p).
    None when singular (a loop closed inside the region) or not converged."""
    k = len(comp)
    pos = {m: i for i, m in enumerate(comp)}
    if k <= _GS_LIMIT:
        a = [[0.0] * (k + 1) for _ in range(k)]
        for i, m in enumerate(comp):
            a[i][i] = 1.0
            a[i][k] = b[m]
        for (src, dst), p in inner.items():
            a[pos[dst]][pos[src]] -= p
        for col in range(k):
            piv = max(range(col, k), key=lambda r: abs(a[r][col]))
            if abs(a[piv][col]) < _EPS:
                return None
            a[col], a[piv] = a[piv], a[col]
            for r in range(k):
                if r != col and a[r][col] != 0.0:
                    f = a[r][col] / a[col][col]
                    row, prow = a[r], a[col]
                    for c in range(col, k + 1):
                        row[c] -= f * prow[c]
        return [a[i][k] / a[i][i] for i in range(k)]
    preds: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for (src, dst), p in inner.items():
        preds[pos[dst]].append((pos[src], p))
    v = [b[m] for m in comp]
    for _ in range(20000):
        delta = 0.0
        for i in range(k):
            new = b[comp[i]] + sum(p * v[j] for j, p in preds[i])
            delta = max(delta, abs(new - v[i]) / max(1.0, abs(new)))
            v[i] = new
        if delta < _EPS:
            return v
    return None


def estimate_region(plan: Any, unit_of: Callable[[str], Any], index: ProfitIndex,
                    costs: DispatchCosts, *, residual_goto_tb: str, entry_chain: str,
                    tb_size: int = 0, exits_of: Callable[[str], tuple[Any, ...]] | None = None,
                    trusted_entries: Iterable[str] = (),
                    captured: Mapping[str, float] | None = None) -> RegionEstimate:
    """Price ``plan`` (after trusted sanitization) on the training profile.

    ``residual_goto_tb``/``entry_chain`` are resolved modes (``chained`` or
    ``unchained``).  ``trusted_entries`` are the caller UIDs of other trusted
    regions planned in the same artifact; a trusted exit into one of them
    (or into this region's own entry) is never chained.  ``captured[uid]``
    is the flow of ``uid`` that regions admitted earlier already execute as
    private members: those executions never reach this region's entry (its
    public lookup), so they are subtracted from the entry's executions.
    """
    exits_of = exits_of or (lambda uid: unit_exits(unit_of(uid)))
    n = len(plan.members)
    reasons: list[str] = []
    status = "exact"
    poll = costs.poll_ns if plan.trusted and plan.trusted_entry_offset > 0 else 0.0
    entry_uid = plan.members[plan.entry_member].source_uid
    entries_into = frozenset(trusted_entries) | {entry_uid} if plan.trusted else frozenset()
    execs: list[int] = []
    for m in plan.members:
        e = index.execs(m.source_uid)
        if e is None:
            status = "unknown"
            reasons.append(f"executions_unknown:{m.source_uid}")
            e = 0
        execs.append(e)
    internal: dict[tuple[int, str], Any] = {}
    for i, edge in enumerate(plan.edges):
        if edge.target_member is not None:
            internal[edge.source_member, edge.exit_id] = (i, edge)
    # Per member: [(exit, key, count, internal edge or None)].
    rows: list[list[tuple[Any, str | None, float, Any]]] = []
    for mi, m in enumerate(plan.members):
        exits = exits_of(m.source_uid)
        groups: dict[str | None, list[Any]] = defaultdict(list)
        for x in exits:
            groups[_exit_key(x)].append(x)
        member_rows = []
        for key, xs in groups.items():
            total = 0 if key is None else index.count(m.source_uid, key)
            edged = [x for x in xs if (mi, x.id) in internal]
            if key is None:
                if edged:
                    status = "unknown" if status == "unknown" else "ambiguous"
                    reasons.append(f"unpriced_exit:{m.source_uid}/{edged[0].id}")
                for x in xs:
                    member_rows.append((x, key, 0.0, None))
                continue
            if len(xs) > 1 and edged:
                # Several exits share one profile kind: the split is unknown.
                # Credit no internal saving (and no flow) through them.
                if status == "exact":
                    status = "ambiguous"
                reasons.append(f"ambiguous_exit_kind:{m.source_uid}/{key}")
                residual = [x for x in xs if (mi, x.id) not in internal]
                for x in xs:
                    member_rows.append((x, key, float(total) if residual and x is residual[0]
                                        else 0.0,
                                        None if (mi, x.id) not in internal else
                                        ("ambiguous", internal[mi, x.id])))
                continue
            for x in xs:
                share = float(total) if len(xs) == 1 else 0.0
                member_rows.append((x, key, share, internal.get((mi, x.id))))
        rows.append(member_rows)
    # Markov flow over the member graph.
    prob: dict[tuple[int, int], float] = defaultdict(float)
    for mi, member_rows in enumerate(rows):
        if execs[mi] <= 0:
            continue
        for x, key, count, edge in member_rows:
            if edge is None or edge[0] == "ambiguous" or key == "exit_requested":
                continue
            prob[mi, edge[1].target_member] += count / execs[mi]
    succ: list[list[int]] = [[] for _ in range(n)]
    for (src, dst) in sorted(prob):
        succ[src].append(dst)
    source = [0.0] * n
    source[plan.entry_member] = max(
        float(execs[plan.entry_member]) - (captured or {}).get(entry_uid, 0.0), 0.0)
    inflow = list(source)
    v = [0.0] * n
    for comp in _sccs(n, succ):
        members = set(comp)
        inner = {(s, d): p for (s, d), p in prob.items() if s in members and d in members}
        sol = _solve_component(comp, inner, {m: inflow[m] for m in comp})
        if sol is None:
            # Singular: a loop closed inside the region with no leak.  Its
            # members take their observed executions (clamped below anyway).
            if len(comp) > _GS_LIMIT:
                status = "unknown"
                reasons.append("flow_not_converged")
            sol = [float(execs[m]) for m in comp]
        for m, value in zip(comp, sol):
            v[m] = min(max(value, 0.0), float(execs[m]))
        for m in comp:
            for d in succ[m]:
                if d not in members:
                    inflow[d] += v[m] * prob[m, d]
    terms: Counter[str] = Counter()
    saved = [0.0] * n
    added = [0.0] * n
    edge_saved: list[tuple[int, float, float]] = []
    calls = 0.0
    for mi, member_rows in enumerate(rows):
        rate = v[mi] / execs[mi] if execs[mi] > 0 else 0.0
        uid = plan.members[mi].source_uid
        for x, key, count, edge in member_rows:
            if key == "exit_requested":
                continue  # an aborted entry, never a transfer; trusted members skip it
            flow = rate * count
            if edge is not None and edge[0] != "ambiguous":
                ei, e = edge
                if mi == plan.entry_member and x.id == plan.callsite.exit_id:
                    calls += flow
                if key.startswith("chain"):
                    ns = poll
                    terms["internal_chain"] += flow
                elif key == "lookup":
                    kinds = index.by_kind.get(uid, Counter())
                    hits = kinds["lookup_hit"]
                    total = hits + kinds["lookup_miss"]
                    hit = flow * (hits / total) if total else 0.0
                    miss = flow - hit
                    guarded = e.guarded or e.kind == "return" or not plan.trusted
                    ns_hit = costs.lookup_ns + poll - (costs.guard_ns if guarded else 0.0)
                    ns_miss = costs.dispatch_ns + poll - (costs.guard_ns if guarded else 0.0)
                    terms["guarded_return" if e.kind == "return" else "internal_lookup"] += hit
                    terms["internal_dispatch"] += miss
                    ns = (hit * ns_hit + miss * ns_miss) / flow if flow else 0.0
                else:
                    ns = costs.dispatch_ns + poll
                    terms["internal_dispatch"] += flow
                saved[mi] += flow * ns
                edge_saved.append((ei, flow, flow * ns))
                continue
            if key is not None and key.startswith("chain"):
                blocked = rate * index.chain_into(uid, key, entries_into) if entries_into else 0.0
                blocked = min(blocked, flow)
                unchained = flow if residual_goto_tb == "unchained" else blocked
                terms["residual_goto_tb_unchained"] += unchained
                terms["residual_goto_tb_chained"] += flow - unchained
                added[mi] += unchained * costs.dispatch_ns
            elif key is not None:
                terms["residual_dynamic"] += flow
    entries = v[plan.entry_member]
    terms["entry_polls"] += entries if poll else 0.0
    saved[plan.entry_member] += entries * poll
    if entry_chain == "unchained":
        chained_in = float(sum(n for kind, n in index.inn.get(entry_uid, Counter()).items()
                               if kind.startswith("chain")))
        terms["entry_unchained"] += chained_in
        added[plan.entry_member] += chained_in * costs.dispatch_ns
    descriptors_each = not plan.trusted or residual_goto_tb == "chained"
    size_of = [unit_of(m.source_uid).tc_size + (tb_size if descriptors_each or
                                                 mi == plan.entry_member else 0)
               for mi, m in enumerate(plan.members)]
    footprint = [costs.byte_ns * s for s in size_of]
    return RegionEstimate(
        status=status, entries=entries, calls=calls, terms=dict(terms), bytes=sum(size_of),
        saved_ns=sum(saved), added_ns=sum(added), footprint_ns=sum(footprint),
        residual_goto_tb=residual_goto_tb, entry_chain=entry_chain,
        reasons=tuple(dict.fromkeys(reasons)),
        member_net=tuple(s - a - f for s, a, f in zip(saved, added, footprint)),
        edge_saved=tuple(edge_saved), member_flow=tuple(v))


def admission(est: RegionEstimate, costs: DispatchCosts) -> str | None:
    """None when the root is admitted, else the refusal reason."""
    if est.status == "unknown":
        return "profit_estimate_unknown"
    if (est.saved_ns >= (1.0 + costs.margin) * est.added_ns and est.net_ns > 0
            and est.net_ns_per_call >= costs.min_net_per_call_ns):
        return None
    return "not_profitable"


@dataclass(frozen=True)
class ProfitModel:
    """One validated cost model of a firmware-inline run."""

    index: ProfitIndex
    costs: DispatchCosts
    residual_goto_tb: str = "auto"
    entry_chain: str = "auto"
    phase: str = "steady"
    #: Units whose exits B3 guards (grown before firmware-inline in composed
    #: sealed+B3 pipelines).
    b3_grown: frozenset[str] = frozenset()

    def estimate(self, plan: Any, snapshot: Any, *, exits_of=None,
                 trusted_entries: Iterable[str] = (),
                 captured: Mapping[str, float] | None = None) -> RegionEstimate:
        return estimate_region(
            plan, snapshot.unit, self.index, self.costs,
            residual_goto_tb=resolve_residual_goto_tb(self.residual_goto_tb, plan,
                                                      self.b3_grown),
            entry_chain=resolve_entry_chain(self.entry_chain, plan, snapshot.unit),
            tb_size=snapshot.header.tb_size, exits_of=exits_of,
            trusted_entries=trusted_entries, captured=captured)

    def to_json(self) -> dict[str, Any]:
        return {"model": MODEL, "phase": self.phase, "constants": self.costs.to_json(),
                "residual_goto_tb": self.residual_goto_tb, "entry_chain": self.entry_chain}


def nested_gain(probe: RegionEstimate, plan: Any, call_exit: Any, costs: DispatchCosts) -> float:
    """Marginal gain (ns per run) of inlining one nested call.

    ``probe`` prices the nested call as a one-level region ``plan`` (caller,
    callee closure, continuation).  The gain is what the callee members and
    the call edge save net of their residual exits and footprint, plus the
    dispatch the call's own exit would cost if it stayed a residual
    ``goto_tb`` of an unchainable region.  The caller and the continuation
    are left out: the outer region owns them, and the whole-region gate
    prices everything that follows the call.
    """
    gain = sum(net for net, m in zip(probe.member_net, plan.members) if m.inline_context)
    calls = 0.0
    for ei, flow, ns in probe.edge_saved:
        edge = plan.edges[ei]
        if edge.source_member == plan.entry_member and edge.exit_id == call_exit.id:
            gain += ns
            calls += flow
    if (call_exit.kind == ExitKind.GOTO_TB and probe.residual_goto_tb == "unchained"):
        gain += calls * costs.dispatch_ns
    return gain


class NestedProfitExpansion:
    """``PolicySelection.expansion`` callback: inline a nested call only when
    :func:`nested_gain` is positive.  Branches and roots always expand; the
    existing recursion guard of :func:`build_firmware_region` and the
    whole-region profit gate still apply.  ``decisions`` records every
    priced nested call for the receipt."""

    def __init__(self, model: ProfitModel, snapshot: Any, image: Any, calls: tuple[Any, ...],
                 *, trusted: bool):
        self.model = model
        self.snapshot = snapshot
        self.image = image
        self.calls = calls
        self.trusted = trusted
        self.decisions: dict[str, dict[str, Any]] = {}
        self._by_site = {(c.source_uid, c.insn_vaddr): c for c in calls}
        self._lock = threading.Lock()
        self._shared: Any = None

    def _planning(self):
        with self._lock:
            if self._shared is None:
                from .firmware_calls import firmware_compatibility_index

                units = tuple(self.snapshot.units())
                self._shared = SimpleNamespace(
                    units=units, by_uid={u.uid: u for u in units},
                    compatibility=firmware_compatibility_index(units),
                    calls=SimpleNamespace(calls=self.calls))
            return self._shared

    def __call__(self, site: Any, context: tuple[str, ...]):
        from .rtos_boundaries import ExpansionAction

        if site.kind != "call":
            return ExpansionAction.INLINE
        call = self._by_site.get((site.source_uid, site.source_pc))
        if call is None:
            return "nested_call_unpriced"
        row = self.decisions.get(call.callsite_id)
        if row is None:
            row = self._decide(call)
            self.decisions[call.callsite_id] = row
        return ExpansionAction.INLINE if row["action"] == "inline" else row["reason"]

    def _decide(self, call: Any) -> dict[str, Any]:
        from .firmware_regions import build_firmware_region

        shared = self._planning()
        row: dict[str, Any] = {"callsite_id": call.callsite_id, "source_uid": call.source_uid,
                               "insn_vaddr": hex(call.insn_vaddr),
                               "target_vaddr": hex(call.target_vaddr)}
        probe = build_firmware_region(self.snapshot, self.image, shared.calls, call,
                                      units=shared.units, by_uid=shared.by_uid,
                                      compatibility=shared.compatibility, expand_nested=False)
        if not probe.complete:
            return {**row, "action": "public", "reason": "nested_probe_incomplete"}
        if self.trusted:
            from ..link import TRUSTED_ENTRY_OFFSET

            probe = replace(probe, trusted=True, trusted_entry_offset=TRUSTED_ENTRY_OFFSET)
        est = self.model.estimate(probe, self.snapshot)
        call_exit = next((x for x in unit_exits(self.snapshot.unit(call.source_uid))
                          if x.id == call.exit_id), None)
        if est.status == "unknown" or call_exit is None:
            return {**row, "action": "public", "reason": "nested_profit_unknown"}
        gain = nested_gain(est, probe, call_exit, self.model.costs)
        calls = sum(flow for ei, flow, _ in est.edge_saved
                    if probe.edges[ei].source_member == probe.entry_member
                    and probe.edges[ei].exit_id == call.exit_id)
        row.update(calls=round(calls, 3), gain_ns=round(gain, 3),
                   residual_goto_tb=est.residual_goto_tb)
        if gain > 0 and calls > 0:
            return {**row, "action": "inline", "reason": None}
        return {**row, "action": "public", "reason": "nested_not_profitable"}

    def report(self) -> list[dict[str, Any]]:
        return [self.decisions[k] for k in sorted(self.decisions)]
