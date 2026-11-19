"""``hostsym`` trace equivalence, guarded-chain rule and fact certification (§9.2, V12-V14). WP19.

* :func:`check_equiv` (V12): for every candidate path feasible under ``P``
  there is an original path with the same sequence of symbolic branch
  outcomes and vice versa, and the traces are equal event for event: the
  non-env payload terms exactly, the env snapshot per byte for the fields
  the observation contract makes observable at that event (fail closed: no
  contract = every byte), the frame at calls. Before exploring, the LDST
  blocks must be identical (``edit_inside_ldst_block``) and the search rows,
  icount and cflags unchanged (``recovery_rows_changed``). After exploring,
  every absolute host pointer the candidate dereferences must lie outside
  env/CPUState (``host_abs_aliases_env``).
* :func:`check_guarded_chain` (V12 on R' + V13 entailment): as above, except
  that a candidate path ending in ``Exit(guarded_slot, n)`` matches the
  original path ending in ``Call(lookup_tb_ptr); Exit(goto_ptr)`` iff (a) its
  path condition entails every declared guard atom, (b) its env equals the
  original env at the lookup call with ``can_do_io = 1`` applied **to the
  original side only**, compared as a direct-chain exit (so ``can_do_io`` is
  compared only when ``tb_start_cb_possible``; then a candidate that does not
  show ``can_do_io == 1`` is a mismatch), and (c) the guard stub has no
  call, store or LDST. Guard-stub branches are not part of the path
  signature.
* :func:`certify_fact` (V14): ``env[field]`` is the same constant on every
  feasible path that reaches the slot (or the lookup exit of an ungrown R).

Verdicts: ``equivalent``; ``mismatch`` (reason ``validation_mismatch`` or a
more specific failed code: ``edit_inside_ldst_block``,
``edit_inside_log_block`` (a coverage LOG_REG block gained, lost or changed;
close-out), ``recovery_rows_changed``, ``host_abs_aliases_env``,
``guard_contract_violation``, ``fact_derivation_mismatch``) — a candidate bug,
reported as *failed*; ``inconclusive`` (``validation_inconclusive``:
unsupported instruction/path or the 256-path budget) — *skipped_unknown*.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .paths import MAX_PATHS, Exploration, block_insns, explore
from .program import GSlot, Program
from .state import ByteMem, Event, State, Term, binop

__all__ = ["Verdict", "check_equiv", "check_guarded_chain", "certify_fact", "FactCert",
           "compare_traces", "ldst_identical", "sync_of", "ALLOWED_ABS_CLASSES", "term_str",
           "log_blocks_identical", "abs_class", "LOGREG_RING_CLASS"]

ALLOWED_ABS_CLASSES = frozenset({"abs.guest_ram", "abs.plugin_data", "abs.qemu_data",
                                 "abs.logreg_ring"})
#: the ring base of a matched coverage LOG_REG block (close-out): allowed only as the
#: block's own ring store, and only when its extent misses env (numeric, given ``env_abs``)
LOGREG_RING_CLASS = "abs.logreg_ring"
_DEFAULT_CDIO = -4  # neg.can_do_io (G21)


@dataclass
class Verdict:
    status: str  # equivalent | mismatch | inconclusive
    reason: str | None = None
    detail: str = ""
    o_paths: int = 0
    n_paths: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "equivalent"

    @property
    def rejected(self) -> bool:
        return self.status != "equivalent"

    def to_json(self) -> dict[str, Any]:
        d = {"status": self.status, "reason": self.reason, "detail": self.detail,
             "o_paths": self.o_paths, "n_paths": self.n_paths}
        if self.data:
            d["data"] = self.data
        return d


def _mismatch(reason: str, detail: str, **kw: Any) -> Verdict:
    return Verdict("mismatch", reason, detail, **kw)


def _inconclusive(detail: str, **kw: Any) -> Verdict:
    return Verdict("inconclusive", "validation_inconclusive", detail, **kw)


def term_str(t: Term, limit: int = 160) -> str:
    s = str(t)
    return s if len(s) <= limit else s[:limit] + "..."


# ---------------------------------------------------------------------------
# observation contract
# ---------------------------------------------------------------------------

_CALL_SYNC = {"lookup": "S3", "global": "S4", "plugin": "S4", "noreturn": "S4", "unknown": "S4",
              "raising": "S6", "pure": "P"}


def sync_of(ev: Event) -> tuple[str | None, str | None]:
    """(sync event, exit kind) at which ``ev``'s env snapshot is compared."""
    if ev.kind == "call":
        return _CALL_SYNC.get(ev.cls, "S4"), None
    if ev.kind == "guest":
        return "S5", None
    if ev.kind == "exit":
        return "S2", ev.exit_kind
    return None, None


class _Obs:
    """Byte-level observability under a contract (cached per field)."""

    def __init__(self, contract: Any, layout: Any):
        self.c = contract
        self.lay = layout
        self._field: dict[int, str] = {}
        self._cache: dict[tuple[str, str, str | None], bool] = {}

    def field_of(self, b: int) -> str:
        f = self._field.get(b)
        if f is None:
            ref = None if self.lay is None else self.lay.classify(b, 1)
            f = "?" if ref is None else ref.name
            self._field[b] = f
        return f

    def observable(self, b: int, sync: str, exit_kind: str | None) -> bool:
        if self.c is None:
            return True
        f = self.field_of(b)
        k = (f, sync, exit_kind)
        v = self._cache.get(k)
        if v is None:
            v = True if f == "?" else bool(self.c.observable(f, sync, exit_kind=exit_kind))
            self._cache[k] = v
        return v


def _env_diff(a: ByteMem, b: ByteMem, obs: _Obs, sync: str, exit_kind: str | None,
              *, all_bytes: bool = False, extra: Any = None) -> list[tuple[int, str]]:
    """Differing bytes: those observable at ``sync`` under the contract, plus every byte
    ``extra`` accepts (a call's helper read set)."""
    out = []
    for x in sorted(a.written() | b.written()):
        if not all_bytes and not obs.observable(x, sync, exit_kind) and \
                not (extra is not None and extra(x)):
            continue
        if a.byte(x) != b.byte(x):
            out.append((x, obs.field_of(x)))
    return out


def _ev_label(e: Event) -> str:
    return f"{e.kind}:{e.cls}@{e.pc:#x}"


def compare_traces(o: Sequence[Event], n: Sequence[Event], obs: _Obs) -> str | None:
    """None when equal, else a description of the first difference."""
    if len(o) != len(n):
        return (f"trace length {len(o)} != {len(n)}: "
                f"O {[_ev_label(e) for e in o]} N {[_ev_label(e) for e in n]}")
    for i, (a, b) in enumerate(zip(o, n)):
        d = _event_diff(a, b, obs)
        if d:
            return f"event {i} ({_ev_label(a)} vs {_ev_label(b)}): {d}"
    return None


def _event_diff(a: Event, b: Event, obs: _Obs) -> str | None:
    if a.kind != b.kind or a.cls != b.cls or a.exit_kind != b.exit_kind:
        return f"kind {a.kind}/{a.cls}/{a.exit_kind} != {b.kind}/{b.cls}/{b.exit_kind}"
    if a.data != b.data:
        for j, (x, y) in enumerate(zip(a.data, b.data)):
            if x != y:
                return f"payload[{j}] {term_str(x)} != {term_str(y)}"
        return "payload length"
    sync, ek = sync_of(a)
    if sync is not None and a.env is not None and b.env is not None:
        # a call: the bytes its helper may read are compared too (helper effect table);
        # both sides call the same target (payload equal), so they carry the same set
        d = _env_diff(a.env, b.env, obs, sync, ek, extra=a.reads)
        if d:
            return "env " + ", ".join(f"{f}@{x:#x}" for x, f in d[:6])
    if a.frame is not None and b.frame is not None:
        d = _env_diff(a.frame, b.frame, obs, "S4", None, all_bytes=True)
        if d:
            return "frame " + ", ".join(f"{x:#x}" for x, _ in d[:6])
    return None


# ---------------------------------------------------------------------------
# structural pre-checks
# ---------------------------------------------------------------------------

def _norm_insn(p: Program, ci: Any) -> tuple:
    """Instruction identity modulo relocation: rel/RIP targets as normalised addresses."""
    import capstone
    from capstone import x86 as X

    branch = ci.group(capstone.CS_GRP_JUMP) or ci.group(capstone.CS_GRP_CALL)
    parts: list[Any] = [ci.mnemonic]
    for o in ci.operands:
        if o.type == X.X86_OP_IMM and branch:
            parts.append(("tgt", p.norm_addr(o.imm)))
        elif o.type == X.X86_OP_MEM and o.mem.base == X.X86_REG_RIP:
            parts.append(("rip", p.norm_addr(ci.address + ci.size + o.mem.disp), o.size))
        elif o.type == X.X86_OP_IMM:
            parts.append(("imm", o.imm, o.size))
        elif o.type == X.X86_OP_REG:
            parts.append(("reg", ci.reg_name(o.reg)))
        else:
            m = o.mem
            parts.append(("mem", m.base, m.index, m.scale, m.disp, m.segment, o.size))
    return tuple(parts)


def ldst_identical(o: Program, n: Program, mu=None) -> str | None:
    """None when every LDST block of ``o`` is present and identical in ``n``."""
    mu = mu or (lambda x: x)
    if len(o.ldst) != len(n.ldst):
        return f"{len(o.ldst)} LDST blocks != {len(n.ldst)}"
    for off, b in o.ldst.items():
        nb = n.ldst.get(mu(off))
        if nb is None or (nb.k, nb.oi, nb.is_ld) != (b.k, b.oi, b.is_ld):
            return f"LDST {b.k} missing or retyped"
        for (s, e), (s2, e2) in (((b.fast_start, b.raddr), (nb.fast_start, nb.raddr)),
                                 ((b.slow_start, b.slow_end), (nb.slow_start, nb.slow_end))):
            if e - s != e2 - s2:
                return f"LDST {b.k} block size changed"
            if o.code[s:e] == n.code[s2:e2] and o.base == n.base:
                continue
            io = block_insns(o, s, e)
            inn = block_insns(n, s2, e2)
            if io is None or inn is None or [_norm_insn(o, c) for c in io] != \
                    [_norm_insn(n, c) for c in inn]:
                return f"LDST {b.k} bytes differ in [{s2:#x},{e2:#x})"
    return None


def log_blocks_identical(o: Program, n: Program, mu=None) -> str | None:
    """None when the matched coverage LOG_REG blocks of ``o`` and ``n`` correspond 1:1 at
    mapped offsets with equal canonical bytes (holes resolved), pointer triple and
    ``regs[15]`` offset (close-out): a candidate may not gain, lose or change a block."""
    mu = mu or (lambda x: x)
    ob, nb = o.logblocks, n.logblocks
    if {mu(x) for x in ob} != set(nb):
        return (f"LOG_REG blocks at {sorted(hex(x) for x in ob)} (original) vs "
                f"{sorted(hex(x) for x in nb)} (candidate)")
    for x, lb in ob.items():
        nl = nb[mu(x)]
        if (lb.canon, lb.triple, lb.value_off) != (nl.canon, nl.triple, nl.value_off):
            return f"LOG_REG block at {x:#x} differs (bytes, pointers or value field)"
    return None


def _structural(o: Program, n: Program, mu=None) -> Verdict | None:
    if o.search != n.search or o.icount != n.icount or o.cflags != n.cflags:
        return _mismatch("recovery_rows_changed",
                         "search rows, icount or cflags differ (rows must be byte-identical)")
    d = ldst_identical(o, n, mu)
    if d:
        return _mismatch("edit_inside_ldst_block", d)
    d = log_blocks_identical(o, n, mu)
    if d:
        return _mismatch("edit_inside_log_block", d)
    return None


def abs_class(p: Program, v: Any) -> str | None:
    """Importer class of absolute pointer ``v`` in ``p``; the ring base of a block matched on
    ``p``'s own bytes is ``abs.logreg_ring``."""
    if not isinstance(v, int):
        return None
    for lb in p.logblocks.values():
        if v == lb.ring_abs:
            return LOGREG_RING_CLASS
    return p.abs_classes.get(v)


def _abs_violations(p: Program, ex: Exploration, env_abs: tuple[int, int] | None) -> list[str]:
    """§7.5 / V12: every absolute host pointer dereferenced outside an LDST block lies
    outside ``[env - sizeof(CPUState), env + sizeof(CPUARMState))``.

    The env address of the process is known only when ``env_abs`` is given; then
    the effective address is checked against it. Otherwise the pointer (the base
    register's value) must be one the importer bound to a class that cannot be
    the vCPU (``abs.guest_ram``, ``abs.plugin_data``, ``abs.qemu_data``, ``ext.*``);
    an unbound or own-unit pointer fails closed.
    """
    out = []
    rings = {lb.ring_abs: lb for lb in p.logblocks.values()}
    for st in ex.paths:
        for pc, ea, rw, bp in st.abs_refs:
            lb = rings.get(ea) if isinstance(ea, int) else None
            if lb is None and isinstance(bp, int):
                lb = rings.get(bp)
            if lb is not None:  # abs.logreg_ring: only the block's own ring store
                if rw != "w" or pc != lb.ring_store or ea != lb.ring_abs:
                    out.append(f"{rw}@{pc:#x} {term_str(ea)}: {LOGREG_RING_CLASS} reference "
                               "that is not the ring store of a matched LOG_REG block")
                elif env_abs is not None and \
                        lb.ring_abs < env_abs[1] and env_abs[0] < lb.ring_abs + lb.ring_extent:
                    out.append(f"w@{pc:#x} ring [{lb.ring_abs:#x},+{lb.ring_extent:#x}) "
                               f"intersects env [{env_abs[0]:#x},{env_abs[1]:#x})")
                continue
            if env_abs is not None and isinstance(ea, int):
                lo_, hi_ = env_abs
                if lo_ <= ea < hi_:
                    out.append(f"{rw}@{pc:#x} {ea:#x} inside env [{lo_:#x},{hi_:#x})")
                continue
            if isinstance(bp, tuple) and bp and bp[0] == "own":
                out.append(f"{rw}@{pc:#x} own-unit pointer {term_str(bp)}")
                continue
            cls = p.abs_classes.get(bp) if isinstance(bp, int) else None
            if cls is None and isinstance(ea, int):
                cls = p.abs_classes.get(ea)
            if cls not in ALLOWED_ABS_CLASSES and not (cls or "").startswith("ext."):
                out.append(f"{rw}@{pc:#x} {term_str(ea)} (pointer {term_str(bp)}) class {cls}")
    return sorted(set(out))


# ---------------------------------------------------------------------------
# V12: trace equivalence
# ---------------------------------------------------------------------------

def _group(ex: Exploration, drop=None) -> dict[tuple, list[State]]:
    g: dict[tuple, list[State]] = {}
    for st in ex.paths:
        g.setdefault(st.signature(drop), []).append(st)
    return g


def _complete(st: State) -> bool:
    return st.unsupported is None


def check_equiv(o: Program, n: Program, pre: Mapping[tuple[int, int], int] | None = None,
                mu=None, contract: Any = None, *, layout: Any = None,
                env_abs: tuple[int, int] | None = None, max_paths: int = MAX_PATHS,
                effects: Any = None) -> Verdict:
    """V12 for a candidate ``n`` of ``o`` under precondition ``pre`` (env (off, w) -> value).
    ``effects`` (a bound helper effect table) defaults to the one bound to ``layout``."""
    v = _structural(o, n, mu)
    if v is not None:
        return v
    eo = explore(o, pre, layout=layout, max_paths=max_paths, effects=effects)
    en = explore(n, pre, layout=layout, max_paths=max_paths, effects=effects)
    return _compare(o, n, eo, en, contract, layout, env_abs)


def _compare(o: Program, n: Program, eo: Exploration, en: Exploration, contract: Any,
             layout: Any, env_abs, *, guarded: bool = False) -> Verdict:
    kw = dict(o_paths=len(eo.paths), n_paths=len(en.paths))
    obs = _Obs(contract, layout)
    viol = _abs_violations(n, en, env_abs)
    if viol:
        return _mismatch("host_abs_aliases_env", "; ".join(viol[:4]), **kw)
    if eo.budget_exhausted or en.budget_exhausted:
        return _inconclusive(f"path budget ({MAX_PATHS}) exhausted", **kw)
    drop = n.in_stub if guarded else None
    go, gn = _group(eo), _group(en, drop)
    unsup = [f"O:{st.unsupported[0]}@{st.unsupported[1]:#x}" for st in eo.paths
             if st.unsupported] + [f"N:{st.unsupported[0]}@{st.unsupported[1]:#x}"
                                   for st in en.paths if st.unsupported]
    for sig, ns in gn.items():
        os_ = go.get(sig)
        if not os_:
            if unsup:
                continue
            return _mismatch("validation_mismatch",
                             f"candidate path {_sig_str(sig)} has no original path", **kw)
        ost = os_[0]
        for nst in ns:
            if not (_complete(ost) and _complete(nst)):
                continue
            if guarded and nst.end and nst.end[1] == "guarded_slot":
                d = _guarded_diff(o, n, ost, nst, obs, contract, layout)
                if d:
                    reason = "guard_contract_violation" if d.startswith("guard:") else \
                        "validation_mismatch"
                    return _mismatch(reason, f"path {_sig_str(sig)}: {d}", **kw)
                continue
            d = compare_traces(ost.events, nst.events, obs)
            if d:
                return _mismatch("validation_mismatch", f"path {_sig_str(sig)}: {d}", **kw)
    for sig in go:
        if sig not in gn and not unsup:
            return _mismatch("validation_mismatch",
                             f"original path {_sig_str(sig)} has no candidate path", **kw)
    if unsup:
        return _inconclusive("unsupported: " + ", ".join(sorted(set(unsup))[:6]), **kw)
    return Verdict("equivalent", None, f"{len(eo.paths)} original / {len(en.paths)} candidate "
                   "paths equal", **kw)


def _sig_str(sig: tuple) -> str:
    return "[" + ", ".join(f"{'T' if v else 'F'}" for _, v in sig) + "]"


# ---------------------------------------------------------------------------
# guarded chain (R') and V13 entailment
# ---------------------------------------------------------------------------

def atom_entailed(st: State, atom: Any, env: ByteMem, stub_conds: set[tuple[Term, bool]]) -> bool:
    v = env.read(atom.off, atom.width)
    k = atom.value & ((1 << (8 * atom.width)) - 1)
    if isinstance(v, int):
        return v == k
    z = ("Z", atom.width, binop("sub", atom.width, v, k))
    return (z, True) in stub_conds


def _guarded_diff(o: Program, n: Program, ost: State, nst: State, obs: _Obs, contract: Any,
                  layout: Any) -> str | None:
    oe = ost.events
    if len(oe) < 2 or oe[-1].kind != "exit" or oe[-1].exit_kind != "goto_ptr" or \
            oe[-2].kind != "call" or oe[-2].cls != "lookup":
        return "original path does not end in Call(lookup_tb_ptr); Exit(goto_ptr)"
    ne = nst.events
    d = compare_traces(oe[:-2], ne[:-1], obs)
    if d:
        return d
    slot: GSlot = n.slots[nst.end[2]]
    if nst.stub_writes:
        return f"guard: stub has a store/call/LDST at {[hex(x) for x in nst.stub_writes[:4]]}"
    stub_conds = {(c, v) for c, v, pc in nst.cond if n.in_stub(pc)}
    exit_env = ne[-1].env
    missing = [a.atom for a in slot.atoms if not atom_entailed(nst, a, exit_env, stub_conds)]
    if missing:
        return f"guard: path condition does not entail {missing} for slot {slot.n}"
    want = oe[-2].env.copy()
    cdio = _DEFAULT_CDIO
    if contract is not None and "neg.can_do_io" in getattr(contract, "offsets", {}):
        cdio = contract.offsets["neg.can_do_io"]
    elif layout is not None and layout.has_field("neg.can_do_io"):
        cdio = layout.offset("neg.can_do_io")
    want.write(cdio, 1, 1)  # the lookup's own can_do_io = true, original side only
    dd = _env_diff(want, exit_env, obs, "S2", "guarded_slot")
    if dd:
        return "env at slot " + ", ".join(f"{f}@{x:#x}" for x, f in dd[:6])
    return None


def check_guarded_chain(r: Program, rp: Program, contract: Any = None, *, layout: Any = None,
                        pre: Mapping[tuple[int, int], int] | None = None,
                        env_abs: tuple[int, int] | None = None, effects: Any = None) -> Verdict:
    """V12/V13 on a grown return/static unit ``rp`` (= R') against the original ``r``."""
    if not rp.slots or rp.stub is None:
        return _mismatch("guard_contract_violation", "R' declares no guarded slot / stub")
    v = _structural(r, rp, None)
    if v is not None:
        return v
    eo = explore(r, pre, layout=layout, effects=effects)
    en = explore(rp, pre, layout=layout, effects=effects)
    out = _compare(r, rp, eo, en, contract, layout, env_abs, guarded=True)
    if out.ok:
        reached = {st.end[2] for st in en.paths if st.end and st.end[1] == "guarded_slot"}
        unreached = [s.n for off, s in rp.slots.items() if off not in reached]
        out.data["slots_reached"] = sorted(rp.slots[x].n for x in reached)
        if unreached:
            out.data["slots_unreached"] = unreached
    return out


# ---------------------------------------------------------------------------
# V14: fact certification
# ---------------------------------------------------------------------------

@dataclass
class FactCert:
    field: tuple[int, int]
    status: str  # certified | not_constant | inconclusive | unreached
    value: int | None = None
    paths: int = 0
    detail: str = ""
    cert_hash: str | None = None

    @property
    def certified(self) -> bool:
        return self.status == "certified"

    def to_json(self) -> dict[str, Any]:
        return {"field": list(self.field), "status": self.status, "value": self.value,
                "paths": self.paths, "detail": self.detail, "cert_hash": self.cert_hash}


def certify_fact(r: Program, slot: int | None, fld: tuple[int, int], *, layout: Any = None,
                 pre: Mapping[tuple[int, int], int] | None = None,
                 exit_pc: int | None = None, effects: Any = None) -> FactCert:
    """``env[fld]`` (``(off, width)``) at R's guarded slot ``slot`` (or, for ``None``,
    at the lookup call of every lookup exit) on every feasible path.

    ``exit_pc`` (``slot`` None only) restricts it to the lookup exit whose
    ``jmp reg`` is at that host offset (``T + 9`` for the tail at ``T``): the
    fact of one exit of a unit with several lookup tails."""
    ex = explore(r, pre, layout=layout, effects=effects)
    if not ex.complete:
        why = "budget" if ex.budget_exhausted else ", ".join(
            sorted({f"{w}@{pc:#x}" for w, pc in ex.unsupported})[:4])
        return FactCert(fld, "inconclusive", detail=why)
    vals: list[Any] = []
    for st in ex.paths:
        ev = st.events
        if slot is None:
            if len(ev) >= 2 and ev[-1].exit_kind == "goto_ptr" and ev[-2].kind == "call" and \
                    ev[-2].cls == "lookup" and (exit_pc is None or ev[-1].pc == exit_pc):
                vals.append(ev[-2].env.read(*fld))
        elif st.end and st.end[1] == "guarded_slot" and r.slots[st.end[2]].n == slot:
            env = ev[-1].env
            value = env.read(*fld)
            # A generated value-guard fact must be entailed by actual decoded
            # run-time branches. The declared atom alone conveys no fact.
            conditions = {(term, result) for term, result, pc in st.cond if r.in_stub(pc)}
            for atom in r.slots[st.end[2]].atoms:
                if (atom.atom == "g_r0_zero" and (atom.off, atom.width) == fld
                        and atom_entailed(st, atom, env, conditions)):
                    value = atom.value
            vals.append(value)
    if not vals:
        return FactCert(fld, "unreached", detail="no path reaches the exit")
    if all(isinstance(x, int) for x in vals) and len(set(vals)) == 1:
        key = [r.uid, slot, list(fld), vals[0], len(vals), hashlib.sha256(r.code).hexdigest()]
        if exit_pc is not None:
            key.append(exit_pc)
        h = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        return FactCert(fld, "certified", vals[0], len(vals), "constant on every path",
                        "sha256:" + h)
    return FactCert(fld, "not_constant", None, len(vals),
                    "values: " + ", ".join(sorted({term_str(x, 60) for x in vals})[:4]))
