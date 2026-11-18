"""``exit-facts@1``: the proposer of return-edge facts per R unit (B3/B4 design §3.2). WP20.

A fact ``F = (R, E, field, value, def_site)`` says: on every execution of R
that reaches the lookup tail of exit ``E``, ``env[field] == value`` (32-bit),
``field`` in ``r0..r12, NF, ZF, CF, VF``. This module derives it from host
code only — forward **must-constant** dataflow over HostOps on the atomic
``hcfg@1`` view (an LDST block is one segment; its slow path is a softmmu
helper that does not write TCG globals). Profiles never create facts.

Lattice per env field (meet over the atomic predecessors)::

    const(v, defs)   every path stored v (defs = the defining stores (host_off, insn))
    killed(barrier)  a constant was stored, then a barrier ran on some path
    nonconst(off)    some path stored a non-constant (or partial) value
    partial          some paths stored a constant, others did not store at all
    (absent)         R never stores the field (its value is the predecessor's)

Kills (§3.2 rule 2, A's algorithm): a call whose flags lack
``TCG_CALL_NO_WRITE_GLOBALS`` (flags & 3 == 0: FastDyn virtuals are flags-0
calls), a call whose helper has no row in the helper effect table or whose row may
write a TCG global (:mod:`.helper_effects`, fail closed), any plugin callback
(unnamed helper; even a NO_RWG plugin callback can change guest registers through
``qemu_plugin_set_register``), a host store
through an unknown pointer, and an unclassified ``rbp`` write. Host register
constants are tracked so that ``mov [rbp+d], reg`` with a known ``reg`` is a
constant store; a call clobbers the caller-saved registers.

Result per lookup-tail exit (``ExitFactSet``): ``facts`` (const at the tail
``mov rdi, rbp``), ``refused`` (``fact_not_constant`` /
``fact_killed_by_helper`` / ``fact_not_local_to_return_tb`` for fields that R
stores but that are not must-constant there), ``barriers`` (every kill on a
path to the tail). The certifier (WP19 ``hostsym``, V14) must derive the same
set; a disagreement is ``failed:fact_derivation_mismatch`` (§3.2), checked by
:func:`compare_with_certifier`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..decode.thumb_sem import ENV_OF_FIELD, FACT_FIELDS, FIELD_OF_ENV
from .api import AnalysisSpec, ScopeRef
from .helper_effects import BoundEffects, call_class, effects_for_hostops
from .hcfg import HCfg, build_hcfg
from .hostops import OpKind, UnitHostOps, reg_family

__all__ = ["FactRow", "Refusal", "Barrier", "ExitFactSet", "UnitExitFacts", "propose_exit_facts",
           "find_lookup_tails", "LookupTail", "compare_with_certifier", "EXIT_FACTS", "ANALYSES",
           "FACT_ENV_FIELDS"]

FACT_ENV_FIELDS = frozenset(ENV_OF_FIELD[f] for f in FACT_FIELDS)
_CALLER_SAVED = frozenset({"rax", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11"})
_M32 = 0xffffffff
_M64 = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class LookupTail:
    """The 11-byte ``mov rdi,rbp; call [lookup_tb_ptr]; jmp rax`` tail (G29)."""

    copy_idx: int  # op index of mov rdi, rbp (T)
    call_idx: int
    goto_idx: int
    off: int  # host offset T
    end: int  # T + 11
    insn: int | None

    @property
    def length(self) -> int:
        return self.end - self.off


@dataclass(frozen=True, slots=True)
class FactRow:
    field: str  # guest name r0..r12 / NF ZF CF VF
    value: int
    defs: tuple[tuple[int, int | None], ...]  # (def_host_off, def_insn), sorted

    @property
    def def_host_off(self) -> int:
        return self.defs[-1][0]

    @property
    def def_insn(self) -> int | None:
        return self.defs[-1][1]

    def to_json(self) -> dict[str, Any]:
        return {"field": self.field, "value": f"0x{self.value:x}",
                "defs": [{"host_off": o, "insn": i} for o, i in self.defs]}


@dataclass(frozen=True, slots=True)
class Refusal:
    field: str
    reason: str
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        d = {"field": self.field, "reason": self.reason}
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass(frozen=True, slots=True)
class Barrier:
    host_off: int
    kind: str  # call_global | plugin_cb | unclassified_store
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"host_off": self.host_off, "kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class ExitFactSet:
    tail: LookupTail
    facts: tuple[FactRow, ...]
    refused: tuple[Refusal, ...]
    barriers: tuple[Barrier, ...]

    def fact(self, field: str) -> FactRow | None:
        return next((f for f in self.facts if f.field == field), None)

    def to_json(self) -> dict[str, Any]:
        return {"tail_off": self.tail.off, "tail_len": self.tail.length, "insn": self.tail.insn,
                "facts": [f.to_json() for f in self.facts],
                "refused": [r.to_json() for r in self.refused],
                "barriers": [b.to_json() for b in self.barriers]}


@dataclass(frozen=True)
class UnitExitFacts:
    uid: str
    status: str  # ok | unavailable
    reasons: tuple[str, ...]
    exits: tuple[ExitFactSet, ...] = ()
    stored_fields: tuple[str, ...] = ()  # fact fields R stores anywhere (guest names)

    def by_goto(self, goto_off: int) -> ExitFactSet | None:
        return next((e for e in self.exits if e.tail.end - 2 == goto_off or
                     e.tail.goto_idx == goto_off), None)

    def to_json(self) -> dict[str, Any]:
        return {"uid": self.uid, "status": self.status, "reasons": list(self.reasons),
                "stored_fields": list(self.stored_fields),
                "exits": [e.to_json() for e in self.exits]}

    def content_digest(self) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(self.to_json(), sort_keys=True)
                                          .encode()).hexdigest()


# ---------------------------------------------------------------------------
# tails
# ---------------------------------------------------------------------------

def find_lookup_tails(h: UnitHostOps) -> list[tuple[int, LookupTail | None, str | None]]:
    """For every GOTO_PTR op: ``(goto op index, tail or None, reason)``; reason is
    ``exit_not_goto_ptr`` when the 11-byte shape is absent and
    ``tail_is_branch_target`` when a captured label lands inside ``(T, T+11)``."""
    ops = h.ops
    targets = {o.target for o in ops if o.target is not None}
    out: list[tuple[int, LookupTail | None, str | None]] = []
    for i, o in enumerate(ops):
        if o.kind != OpKind.GOTO_PTR:
            continue
        ok = (i >= 2 and ops[i - 1].kind == OpKind.CALL and
              ops[i - 1].helper_name == "lookup_tb_ptr" and ops[i - 2].kind == OpKind.COPY and
              ops[i - 2].dst_reg == "rdi" and ops[i - 2].src_reg == "rbp" and
              o.text == "jmp rax")
        if not ok:
            out.append((i, None, "exit_not_goto_ptr"))
            continue
        t = LookupTail(i - 2, i - 1, i, ops[i - 2].off, o.end, ops[i - 2].insn)
        if t.length != 11:
            out.append((i, None, "exit_not_goto_ptr"))
            continue
        if any(t.off < x < t.end for x in targets):
            out.append((i, t, "tail_is_branch_target"))
            continue
        out.append((i, t, None))
    return out


# ---------------------------------------------------------------------------
# dataflow
# ---------------------------------------------------------------------------

# env lattice values: ("c", v, defs) | ("k", Barrier) | ("n", off) | ("p",)
_State = tuple[dict[str, int], dict[str, tuple]]


def _meet(states: Sequence[_State]) -> _State:
    if len(states) == 1:
        r, e = states[0]
        return dict(r), dict(e)
    regs = dict(states[0][0])
    for r, _ in states[1:]:
        for k in list(regs):
            if r.get(k) != regs[k]:
                del regs[k]
    keys = set().union(*(e.keys() for _, e in states))
    env: dict[str, tuple] = {}
    for k in keys:
        vals = [e.get(k) for _, e in states]
        ks = [v for v in vals if v is not None and v[0] == "k"]
        if ks:
            env[k] = ks[0]
            continue
        ns = [v for v in vals if v is not None and v[0] == "n"]
        if ns:
            env[k] = ns[0]
            continue
        cs = [v for v in vals if v is not None and v[0] == "c"]
        if len(cs) < len(vals) or any(v is not None and v[0] == "p" for v in vals):
            env[k] = ("p",)
            continue
        if len({c[1] for c in cs}) != 1:
            env[k] = ("n", None)
            continue
        defs = tuple(sorted(set().union(*(c[2] for c in cs)), key=lambda d: (d[0], d[1] or -1)))
        env[k] = ("c", cs[0][1], defs)
    return regs, env


def _kill_all(env: dict[str, tuple], b: Barrier, barriers: list[Barrier]) -> None:
    # every fact field may have been written by the callee (stored or not before)
    for f in FACT_ENV_FIELDS:
        env[f] = ("k", b)
    if b not in barriers:
        barriers.append(b)


def _reg_width(name: str | None) -> tuple[str, int] | None:
    return reg_family(name) if name else None


def _transfer(o: Any, regs: dict[str, int], env: dict[str, tuple],
              barriers: list[Barrier], fx: BoundEffects | None = None) -> None:
    k = o.kind
    # ---- env effects --------------------------------------------------------------------
    if k == OpKind.CALL:
        cc = call_class(o.helper_name, o.helper_flags, fx)
        if cc == "plugin":
            _kill_all(env, Barrier(o.off, "plugin_cb", f"helper {o.helper_id}"), barriers)
        elif cc != "ldst_slow" and (
                not ((o.helper_flags or 0) & 0x3) or fx is None or
                not fx.keeps_globals(o.helper_name, o.helper_flags)):
            # flags-0 helper, or a helper whose row does not rule out a global write
            _kill_all(env, Barrier(o.off, "call_global",
                                   f"{o.helper_name}:flags={o.helper_flags}"), barriers)
        for r in _CALLER_SAVED:
            regs.pop(r, None)
        return
    if k == OpKind.HOST_ABS_STORE and o.abs_addr is None:
        _kill_all(env, Barrier(o.off, "unclassified_store", o.text), barriers)
    for a in o.env:
        if "w" not in a.rw:
            continue
        if not a.classified:
            _kill_all(env, Barrier(o.off, "unclassified_store", o.text), barriers)
            continue
        if a.field not in FACT_ENV_FIELDS:
            continue
        val = None
        if a.exact and a.width == 4:
            if k == OpKind.ENV_STORE_IMM and o.imm is not None:
                val = o.imm & _M32
            elif k == OpKind.ENV_STORE_REG and o.src_reg:
                fam = reg_family(o.src_reg)
                if fam and fam[0] in regs:
                    val = regs[fam[0]] & _M32
        env[a.field] = ("c", val, ((o.off, o.insn),)) if val is not None else ("n", o.off)
    # ---- register effects -----------------------------------------------------------------
    written = set(o.regs_written) | set(o.regs_partial)
    new: tuple[str, int] | None = None
    dst = _reg_width(o.dst_reg)
    if dst is not None and dst[0] not in o.regs_partial and dst[1] in (4, 8):
        mask = _M32 if dst[1] == 4 else _M64
        if k in (OpKind.CONST, OpKind.POOL_LOAD) and o.imm is not None:
            new = (dst[0], o.imm & mask)
        elif k == OpKind.COPY and o.src_reg:
            sf = reg_family(o.src_reg)
            if sf and sf[0] in regs:
                new = (dst[0], regs[sf[0]] & mask)
        elif k == OpKind.ENV_LOAD and not o.alu and o.env and dst[1] == 4:
            a = o.env[0]
            st = env.get(a.field)
            if a.exact and a.width == 4 and st is not None and st[0] == "c" and \
                    o.text.startswith("mov "):
                new = (dst[0], st[1] & _M32)
    for r in written:
        regs.pop(r, None)
    if new is not None:
        regs[new[0]] = new[1]


def _dataflow(h: UnitHostOps, g: HCfg, stop: set[int], fx: BoundEffects | None = None
              ) -> tuple[dict[int, _State], dict[int, list[Barrier]]]:
    """In-states of every reachable op (atomic view), in index order (the graph is a DAG
    whose edges go forward; ``g.cyclic`` units are refused by the caller)."""
    ins: dict[int, _State] = {}
    outs: dict[int, _State] = {}
    barriers_at: dict[int, list[Barrier]] = {}
    order = sorted(g.reachable_atomic)
    for i in order:
        preds = [p for p in g.predecessors(i) if p in outs]
        if i == g.entry:
            st: _State = ({}, {})
        elif not preds:
            continue
        else:
            st = _meet([outs[p] for p in preds])
            bl: list[Barrier] = []
            for p in preds:
                for b in barriers_at.get(p, ()):
                    if b not in bl:
                        bl.append(b)
            barriers_at[i] = bl
        ins[i] = st
        if i in stop:
            continue
        regs, env = dict(st[0]), dict(st[1])
        bl = list(barriers_at.get(i, []))
        _transfer(h.ops[i], regs, env, bl, fx)
        barriers_at[i] = bl
        outs[i] = (regs, env)
    return ins, barriers_at


def propose_exit_facts(h: UnitHostOps, g: HCfg | None = None, *,
                       effects: BoundEffects | None = None) -> UnitExitFacts:
    """Facts at every admissible lookup tail of ``h`` (see the module docstring).
    ``effects`` defaults to the helper effect table bound to ``h``'s EnvLayout."""
    fx = effects if effects is not None else effects_for_hostops(h)
    if not h.ok:
        return UnitExitFacts(h.uid, "unavailable", tuple(h.reasons) or ("hostops_pinned",))
    g = g if g is not None else build_hcfg(h)
    if not g.ok or g.cyclic:
        return UnitExitFacts(h.uid, "unavailable",
                             tuple(g.reasons) or ("hcfg_cyclic",))
    tails = [(gi, t, r) for gi, t, r in find_lookup_tails(h) if t is not None and r is None]
    stored = sorted({FIELD_OF_ENV[a.field] for o in h.ops for a in o.env
                     if "w" in a.rw and a.field in FACT_ENV_FIELDS},
                    key=FACT_FIELDS.index)
    if not tails:
        return UnitExitFacts(h.uid, "ok", (), (), tuple(stored))
    ins, bars = _dataflow(h, g, {t.copy_idx for _, t, _ in tails}, fx)
    out: list[ExitFactSet] = []
    for _gi, t, _r in tails:
        st = ins.get(t.copy_idx)
        if st is None:  # unreachable tail
            continue
        _regs, env = st
        facts: list[FactRow] = []
        refused: list[Refusal] = []
        for f in FACT_FIELDS:
            v = env.get(ENV_OF_FIELD[f])
            if v is None:
                continue
            if v[0] == "c":
                facts.append(FactRow(f, v[1], v[2]))
            elif v[0] == "k":
                if f in stored:
                    refused.append(Refusal(f, "fact_killed_by_helper",
                                           f"{v[1].kind}@{v[1].host_off:#x} {v[1].detail}"))
            elif v[0] == "n":
                refused.append(Refusal(f, "fact_not_constant",
                                       "" if v[1] is None else f"store@{v[1]:#x}"))
            elif v[0] == "p":
                refused.append(Refusal(f, "fact_not_local_to_return_tb",
                                       "stored on some paths of R only"))
        out.append(ExitFactSet(t, tuple(facts), tuple(refused),
                               tuple(bars.get(t.copy_idx, ()))))
    return UnitExitFacts(h.uid, "ok", (), tuple(out), tuple(stored))


def compare_with_certifier(proposed: ExitFactSet,
                           certify: Callable[[str, int], bool | None]) -> list[dict[str, Any]]:
    """Run the certifier (WP19 ``certify_fact``-like callable ``(field, value) -> True/False/None``)
    on every proposed fact. ``False`` is ``failed:fact_derivation_mismatch``; ``None`` is
    ``validation_inconclusive``. Returns one row per fact."""
    rows = []
    for f in proposed.facts:
        r = certify(f.field, f.value)
        rows.append({"field": f.field, "value": f"0x{f.value:x}",
                     "certified": r is True,
                     "reason": None if r is True else
                     ("validation_inconclusive" if r is None else "fact_derivation_mismatch")})
    return rows


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def _compute(ctx: Any, scope: ScopeRef) -> UnitExitFacts:
    h = ctx.analysis("hostops", scope).value
    g = ctx.analysis("hcfg", scope).value
    return propose_exit_facts(h, g)


EXIT_FACTS = AnalysisSpec(name="exit-facts", version=1, scope="unit",
                          requires=(("hostops", 1), ("hcfg", 1)), inputs=frozenset(),
                          compute=_compute, result_schema=UnitExitFacts)
ANALYSES = (EXIT_FACTS,)
