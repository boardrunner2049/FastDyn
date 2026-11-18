"""``hconst@1``: host constant lattice of one unit (or variant) under entry facts. WP21.

Design §3.4-3.5: CP, BF and DHO all read this *analysis* (constant lattice per
host instruction of a variant ``C'`` under the facts ``F`` of its predecessor
exit), so the three passes have disjoint effects and can be ablated one by one.

Forward must-constant dataflow over the **atomic** ``hcfg@1`` view (an LDST
block is straight-line; its slow path is not part of the graph), from the
unit entry, with the facts seeded into the env state. ``C'`` has no code
before ``insn0_start`` other than its entry check, which writes only
``neg.*`` fields (G14); a TB-start plugin callback in the prefix is a CALL and
kills the facts like any other callback.

State per program point:

* ``env``: value of each fact field (``r0..r12``, ``NF ZF CF VF``; 32-bit
  TCG globals) that is known;
* ``regs``: 64-bit value of each host GPR family that is known;
* ``flags``: ``ZF SF CF OF PF`` booleans that are known.

Transfer (fail closed: anything not modelled makes its outputs unknown):

* ``mov r32, dword [rbp+d]`` of a fact field (ENV_LOAD, exact, 4 bytes):
  the register gets the field's value — a **foldable load** when the value is
  known and the op lies in an ``insn`` block (CP may rewrite it);
* ENV stores: a whole 32-bit store of a fact field from an immediate or a
  known register defines it; any other store to a fact field kills it;
* kills of every fact field (§3.4 "Fact lifetime"): a call whose TCG flags
  lack ``TCG_CALL_NO_WRITE_GLOBALS`` or whose flags are unknown, a call whose
  helper has no row in the helper effect table or whose row may write a TCG
  global (:mod:`.helper_effects`, fail closed), every plugin
  callback (unnamed helper), a HOST_ABS store (except the two stores of a
  matched coverage LOG_REG block, which write the ring and its index outside
  env; close-out), OPAQUE ops with a memory operand; any exit ends the path;
* a call or a guest access (LDST block) clobbers the caller-saved registers
  and the flags (the slow path is a helper call);
* register ops evaluated concretely: ``mov`` (reg/imm), ``movzx``/``movsx``,
  ``xor and or add sub cmp test`` (reg/imm), ``inc dec neg not``,
  ``shl shr sar`` by an immediate, ``lea`` from a known base, ``setcc``,
  ``cmovcc``; flags of these are computed exactly (ZF SF CF OF PF);
  everything else makes the registers and flags it writes unknown.

Results: ``decided`` (op index -> taken?) for every reachable conditional
jump in an ``insn`` block whose condition is known (not the prefix entry
check, not an LDST fast-path ``jne``); ``foldable`` (op index -> (field,
value, dst register)); ``reachable`` (ops reached when decided branches are
followed only in their decided direction).

The registered analysis ``hconst@1`` (unit scope) is the lattice with **no**
facts; the passes call :func:`build_hconst` with a variant's facts (the
analysis cache key of design §3.5 "hconst@1 <- hostops, hcfg, FactRefs" is
then the variant's FactRefs, recorded in the variant's edits).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import capstone
from capstone import x86 as X

from .api import AnalysisSpec, ScopeRef
from .hcfg import HCfg
from .hostops import OpKind, UnitHostOps, reg_family

__all__ = ["HConst", "build_hconst", "fact_field_names", "FACT_FIELDS", "HCONST", "ANALYSES",
           "jcc_condition", "TCG_CALL_NO_WRITE_GLOBALS", "decode_one"]

TCG_CALL_NO_WRITE_GLOBALS = 0x0002
FACT_FIELDS = tuple(f"r{i}" for i in range(13)) + ("NF", "ZF", "CF", "VF")
_CALLER_SAVED = ("rax", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11")
_FLAGS = ("ZF", "SF", "CF", "OF", "PF")
_M64 = (1 << 64) - 1


def fact_field_names(name: str) -> str:
    """Fact field -> EnvLayout element name (``r3`` -> ``regs[3]``; flags unchanged)."""
    if name.startswith("r") and name[1:].isdigit():
        return f"regs[{int(name[1:])}]"
    return name


_ENV_OF_FACT = {fact_field_names(f): f for f in FACT_FIELDS}


@lru_cache(maxsize=1)
def _md() -> capstone.Cs:
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    return md


def decode_one(code: bytes, off: int, size: int) -> Any:
    """The capstone instruction at ``code[off:off+size]`` (address = off)."""
    for ci in _md().disasm(bytes(code[off:off + size]), off):
        return ci
    return None


@dataclass(frozen=True)
class _State:
    env: tuple[tuple[str, int], ...] = ()
    regs: tuple[tuple[str, int], ...] = ()
    flags: tuple[tuple[str, bool], ...] = ()

    def d(self) -> tuple[dict[str, int], dict[str, int], dict[str, bool]]:
        return dict(self.env), dict(self.regs), dict(self.flags)

    @staticmethod
    def of(env: Mapping[str, int], regs: Mapping[str, int], flags: Mapping[str, bool]) -> "_State":
        return _State(tuple(sorted(env.items())), tuple(sorted(regs.items())),
                      tuple(sorted(flags.items())))

    def meet(self, o: "_State") -> "_State":
        def m(a, b):
            db = dict(b)
            return tuple((k, v) for k, v in a if k in db and db[k] == v)
        return _State(m(self.env, o.env), m(self.regs, o.regs), m(self.flags, o.flags))


@dataclass(frozen=True)
class HConst:
    uid: str
    ok: bool
    reasons: tuple[str, ...]
    facts: tuple[tuple[str, int], ...]  # (field, value) entry facts
    states_in: tuple[_State | None, ...] = ()  # None: not reached
    decided: Mapping[int, bool] = field(default_factory=dict)
    foldable: Mapping[int, tuple[str, int, str]] = field(default_factory=dict)
    reachable: frozenset[int] = frozenset()

    def env_at(self, i: int) -> dict[str, int]:
        s = self.states_in[i] if i < len(self.states_in) else None
        return {} if s is None else dict(s.env)

    def regs_at(self, i: int) -> dict[str, int]:
        s = self.states_in[i] if i < len(self.states_in) else None
        return {} if s is None else dict(s.regs)

    def flags_at(self, i: int) -> dict[str, bool]:
        s = self.states_in[i] if i < len(self.states_in) else None
        return {} if s is None else dict(s.flags)

    @property
    def useful(self) -> bool:
        """At least one decided branch or foldable load (§3.4 ``fact_unused``)."""
        return bool(self.decided) or bool(self.foldable)

    def summary(self) -> dict[str, Any]:
        return {"uid": self.uid, "ok": self.ok, "reasons": list(self.reasons),
                "facts": [[f, hex(v)] for f, v in self.facts],
                "decided": {str(k): v for k, v in sorted(self.decided.items())},
                "foldable": {str(k): [f, hex(v), r] for k, (f, v, r) in
                             sorted(self.foldable.items())},
                "reachable": len(self.reachable)}

    def content_digest(self) -> str:
        import hashlib
        import json

        return "sha256:" + hashlib.sha256(json.dumps(self.summary(), sort_keys=True)
                                          .encode()).hexdigest()


# ---------------------------------------------------------------------------
# condition codes and flag computation
# ---------------------------------------------------------------------------

_CC = {
    "o": lambda f: f["OF"], "no": lambda f: not f["OF"],
    "b": lambda f: f["CF"], "c": lambda f: f["CF"], "nae": lambda f: f["CF"],
    "ae": lambda f: not f["CF"], "nb": lambda f: not f["CF"], "nc": lambda f: not f["CF"],
    "e": lambda f: f["ZF"], "z": lambda f: f["ZF"],
    "ne": lambda f: not f["ZF"], "nz": lambda f: not f["ZF"],
    "be": lambda f: f["CF"] or f["ZF"], "na": lambda f: f["CF"] or f["ZF"],
    "a": lambda f: not f["CF"] and not f["ZF"], "nbe": lambda f: not f["CF"] and not f["ZF"],
    "s": lambda f: f["SF"], "ns": lambda f: not f["SF"],
    "p": lambda f: f["PF"], "pe": lambda f: f["PF"], "np": lambda f: not f["PF"],
    "po": lambda f: not f["PF"],
    "l": lambda f: f["SF"] != f["OF"], "nge": lambda f: f["SF"] != f["OF"],
    "ge": lambda f: f["SF"] == f["OF"], "nl": lambda f: f["SF"] == f["OF"],
    "le": lambda f: f["ZF"] or f["SF"] != f["OF"], "ng": lambda f: f["ZF"] or f["SF"] != f["OF"],
    "g": lambda f: not f["ZF"] and f["SF"] == f["OF"],
    "nle": lambda f: not f["ZF"] and f["SF"] == f["OF"],
}
_CC_NEEDS = {
    "o": ("OF",), "no": ("OF",), "b": ("CF",), "c": ("CF",), "nae": ("CF",), "ae": ("CF",),
    "nb": ("CF",), "nc": ("CF",), "e": ("ZF",), "z": ("ZF",), "ne": ("ZF",), "nz": ("ZF",),
    "be": ("CF", "ZF"), "na": ("CF", "ZF"), "a": ("CF", "ZF"), "nbe": ("CF", "ZF"),
    "s": ("SF",), "ns": ("SF",), "p": ("PF",), "pe": ("PF",), "np": ("PF",), "po": ("PF",),
    "l": ("SF", "OF"), "nge": ("SF", "OF"), "ge": ("SF", "OF"), "nl": ("SF", "OF"),
    "le": ("ZF", "SF", "OF"), "ng": ("ZF", "SF", "OF"), "g": ("ZF", "SF", "OF"),
    "nle": ("ZF", "SF", "OF"),
}


def jcc_condition(mnemonic: str, flags: Mapping[str, bool]) -> bool | None:
    """Value of the condition of ``j<cc>``/``set<cc>``/``cmov<cc>`` under known ``flags``."""
    for pre in ("j", "set", "cmov"):
        if mnemonic.startswith(pre) and mnemonic[len(pre):] in _CC:
            cc = mnemonic[len(pre):]
            if all(k in flags for k in _CC_NEEDS[cc]):
                return bool(_CC[cc](flags))
            return None
    return None


def _parity(v: int) -> bool:
    return bin(v & 0xFF).count("1") % 2 == 0


def _logic_flags(r: int, bits: int) -> dict[str, bool]:
    m = (1 << bits) - 1
    r &= m
    return {"ZF": r == 0, "SF": bool(r >> (bits - 1)), "CF": False, "OF": False,
            "PF": _parity(r)}


def _add_flags(a: int, b: int, bits: int, sub: bool) -> tuple[int, dict[str, bool]]:
    m = (1 << bits) - 1
    a &= m
    b &= m
    if sub:
        r = (a - b) & m
        cf = a < b
        sa, sb, sr = a >> (bits - 1), b >> (bits - 1), r >> (bits - 1)
        of = (sa != sb) and (sr != sa)
    else:
        full = a + b
        r = full & m
        cf = full > m
        sa, sb, sr = a >> (bits - 1), b >> (bits - 1), r >> (bits - 1)
        of = (sa == sb) and (sr != sa)
    return r, {"ZF": r == 0, "SF": bool(sr), "CF": bool(cf), "OF": bool(of), "PF": _parity(r)}


def _sext(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


# ---------------------------------------------------------------------------
# transfer
# ---------------------------------------------------------------------------

class _Eval:
    def __init__(self, env: dict[str, int], regs: dict[str, int], flags: dict[str, bool]):
        self.env, self.regs, self.flags = env, regs, flags

    def get(self, ci: Any, op: Any) -> tuple[int, int] | None:
        """(value, bits) of a reg/imm operand, None when unknown or a memory operand."""
        if op.type == X.X86_OP_IMM:
            return op.imm & _M64, op.size * 8
        if op.type == X.X86_OP_REG:
            n = ci.reg_name(op.reg)
            f = reg_family(n)
            if f is None or n in ("ah", "bh", "ch", "dh"):
                return None
            v = self.regs.get(f[0])
            if v is None:
                return None
            bits = f[1] * 8
            return v & ((1 << bits) - 1), bits
        return None

    def put(self, ci: Any, op: Any, value: int | None) -> None:
        n = ci.reg_name(op.reg)
        f = reg_family(n)
        if f is None or n in ("ah", "bh", "ch", "dh"):
            fam = f[0] if f else None
            if fam:
                self.regs.pop(fam, None)
            return
        fam, w = f
        if value is None:
            self.regs.pop(fam, None)
            return
        if w == 8:
            self.regs[fam] = value & _M64
        elif w == 4:
            self.regs[fam] = value & 0xFFFFFFFF
        else:
            old = self.regs.get(fam)
            if old is None:
                self.regs.pop(fam, None)
            else:
                m = (1 << (8 * w)) - 1
                self.regs[fam] = (old & ~m & _M64) | (value & m)

    def unknown_writes(self, ci: Any, op_meta: Any) -> None:
        for r in op_meta.regs_written | op_meta.regs_partial:
            self.regs.pop(r, None)
        for fl in op_meta.flags_written:
            self.flags.pop(fl, None)
        # capstone flag names include AF/DF; keep only ours
        if op_meta.flags_written:
            for fl in _FLAGS:
                if fl in op_meta.flags_written:
                    self.flags.pop(fl, None)

    def setflags(self, fl: dict[str, bool] | None) -> None:
        self.flags.clear()
        if fl:
            self.flags.update(fl)


def _kill_facts(env: dict[str, int]) -> None:
    env.clear()


def _step(h: UnitHostOps, i: int, code: bytes, st: _State, facts_tracked: frozenset[str],
          fx: Any = None) -> tuple[_State, dict[str, Any]]:
    """Transfer of op ``i``; returns the out-state and per-op notes."""
    o = h.ops[i]
    env, regs, flags = st.d()
    ev = _Eval(env, regs, flags)
    note: dict[str, Any] = {}
    k = o.kind
    if k == OpKind.NOP:
        return st, note
    if k == OpKind.CALL:
        name, fl = o.helper_name, o.helper_flags
        if name is None or fl is None or not (fl & TCG_CALL_NO_WRITE_GLOBALS) or \
                fx is None or not fx.keeps_globals(name, fl):
            _kill_facts(env)
        for r in _CALLER_SAVED:
            regs.pop(r, None)
        flags.clear()
        return _State.of(env, regs, flags), note
    if k in (OpKind.GUEST_ACCESS,):
        for r in _CALLER_SAVED:
            regs.pop(r, None)
        ev.unknown_writes(None, o)
        flags.clear()
        return _State.of(env, regs, flags), note
    if k == OpKind.HOST_ABS_STORE:
        # the two stores of a matched coverage LOG_REG block write the ring and its index,
        # both outside env (design/closeout-logreg.md §A.6): no fact kill. Every other host
        # store (UPDATE_MEM, unmatched) kills every fact.
        if o.log is None or o.abs_addr is None:
            _kill_facts(env)
        ev.unknown_writes(None, o)
        return _State.of(env, regs, flags), note
    if o.is_env_store:
        fld = o.store_field
        f = _ENV_OF_FACT.get(fld or "")
        acc = next((a for a in o.env if "w" in a.rw), None)
        if acc is None or not acc.classified:
            _kill_facts(env)
        elif f is not None and f in facts_tracked:
            val = None
            if acc.exact and acc.width == 4:
                if k == OpKind.ENV_STORE_IMM and o.imm is not None:
                    val = o.imm & 0xFFFFFFFF
                elif k == OpKind.ENV_STORE_REG and o.src_reg:
                    rf = reg_family(o.src_reg)
                    if rf is not None and rf[1] == 4 and rf[0] in regs:
                        val = regs[rf[0]] & 0xFFFFFFFF
            if val is None:
                env.pop(f, None)
            else:
                env[f] = val
        ev.unknown_writes(None, o)
        return _State.of(env, regs, flags), note
    ci = decode_one(code, o.off, o.size)
    if ci is None:
        ev.unknown_writes(None, o)
        return _State.of(env, regs, flags), note
    m = ci.mnemonic
    ops = ci.operands
    if k == OpKind.ENV_LOAD:
        acc = o.env[0] if o.env else None
        f = _ENV_OF_FACT.get(acc.field) if acc is not None else None
        if m == "mov" and len(ops) == 2 and ops[0].type == X.X86_OP_REG and acc is not None \
                and acc.exact and acc.width == 4 and f is not None and f in env:
            rf = reg_family(ci.reg_name(ops[0].reg))
            if rf is not None and rf[1] == 4:
                ev.put(ci, ops[0], env[f])
                note["fold"] = (f, env[f], ci.reg_name(ops[0].reg))
                return _State.of(env, regs, flags), note
        ev.unknown_writes(ci, o)
        return _State.of(env, regs, flags), note
    has_mem = any(x.type == X.X86_OP_MEM for x in ops)
    if has_mem and m != "lea":
        if k == OpKind.OPAQUE and any(
                x.type == X.X86_OP_MEM and (x.access & capstone.CS_AC_WRITE) for x in ops):
            _kill_facts(env)  # an unexplained memory write (never in TCG output we admit)
        ev.unknown_writes(ci, o)
        return _State.of(env, regs, flags), note
    if k in (OpKind.JCC,):
        c = jcc_condition(m, flags)
        if c is not None:
            note["decided"] = c
        return st, note
    if k in (OpKind.JMP, OpKind.GOTO_PTR, OpKind.EXIT_JMP):
        return st, note
    res = _alu(ci, m, ops, ev)
    if not res:
        ev.unknown_writes(ci, o)
    return _State.of(env, regs, flags), note


def _alu(ci: Any, m: str, ops: Any, ev: _Eval) -> bool:
    """Concrete semantics; returns False when not modelled (caller havocs)."""
    flags = ev.flags
    if m in ("nop", "endbr64"):
        return True
    if m == "mov" and len(ops) == 2 and ops[0].type == X.X86_OP_REG:
        s = ev.get(ci, ops[1])
        ev.put(ci, ops[0], None if s is None else s[0])
        return True
    if m in ("movzx", "movsx", "movsxd") and len(ops) == 2 and ops[0].type == X.X86_OP_REG:
        s = ev.get(ci, ops[1])
        if s is None:
            ev.put(ci, ops[0], None)
            return True
        v, bits = s
        dbits = ops[0].size * 8
        if m != "movzx":
            v = _sext(v, bits) & ((1 << dbits) - 1)
        ev.put(ci, ops[0], v)
        return True
    if m == "lea" and len(ops) == 2 and ops[0].type == X.X86_OP_REG:
        mem = ops[1].mem
        if mem.base == 0 or ci.reg_name(mem.base) == "rip":
            ev.put(ci, ops[0], None)
            return True
        b = reg_family(ci.reg_name(mem.base))
        base = ev.regs.get(b[0]) if b else None
        idx = 0
        if mem.index != 0:
            ib = reg_family(ci.reg_name(mem.index))
            iv = ev.regs.get(ib[0]) if ib else None
            idx = None if iv is None else iv * mem.scale
        if base is None or idx is None:
            ev.put(ci, ops[0], None)
        else:
            ev.put(ci, ops[0], (base + idx + mem.disp) & _M64)
        return True
    if m in ("xor", "sub") and len(ops) == 2 and ops[0].type == X.X86_OP_REG and \
            ops[1].type == X.X86_OP_REG and ops[0].reg == ops[1].reg:
        bits = ops[0].size * 8
        ev.put(ci, ops[0], 0)
        ev.setflags(_logic_flags(0, bits) if m == "xor" else _add_flags(0, 0, bits, True)[1])
        return True
    if m in ("xor", "and", "or", "add", "sub", "cmp", "test") and len(ops) == 2 and \
            ops[0].type == X.X86_OP_REG:
        bits = ops[0].size * 8
        a = ev.get(ci, ops[0])
        b = ev.get(ci, ops[1])
        if a is None or b is None:
            if m not in ("cmp", "test"):
                ev.put(ci, ops[0], None)
            ev.setflags(None)
            return True
        av, bv = a[0], b[0] & ((1 << bits) - 1)
        if m in ("xor", "and", "or", "test"):
            r = {"xor": av ^ bv, "and": av & bv, "or": av | bv, "test": av & bv}[m]
            fl = _logic_flags(r, bits)
        else:
            r, fl = _add_flags(av, bv, bits, m in ("sub", "cmp"))
        if m not in ("cmp", "test"):
            ev.put(ci, ops[0], r & ((1 << bits) - 1))
        ev.setflags(fl)
        return True
    if m in ("inc", "dec", "neg", "not") and len(ops) == 1 and ops[0].type == X.X86_OP_REG:
        bits = ops[0].size * 8
        a = ev.get(ci, ops[0])
        if a is None:
            ev.put(ci, ops[0], None)
            if m != "not":
                cf = flags.get("CF")
                ev.setflags(None)
                if m in ("inc", "dec") and cf is not None:
                    flags["CF"] = cf
            return True
        if m == "not":
            ev.put(ci, ops[0], ~a[0] & ((1 << bits) - 1))
            return True
        cf = flags.get("CF")
        if m == "neg":
            r, fl = _add_flags(0, a[0], bits, True)
            fl["CF"] = a[0] != 0
        else:
            r, fl = _add_flags(a[0], 1, bits, m == "dec")
            if cf is None:
                fl.pop("CF")
            else:
                fl["CF"] = cf
        ev.put(ci, ops[0], r)
        ev.setflags(fl)
        return True
    if m in ("shl", "sal", "shr", "sar") and len(ops) == 2 and ops[0].type == X.X86_OP_REG \
            and ops[1].type == X.X86_OP_IMM:
        bits = ops[0].size * 8
        n = ops[1].imm & (63 if bits == 64 else 31)
        a = ev.get(ci, ops[0])
        if n == 0:
            return True
        if a is None:
            ev.put(ci, ops[0], None)
            ev.setflags(None)
            return True
        v = a[0]
        msk = (1 << bits) - 1
        if m in ("shl", "sal"):
            r = (v << n) & msk
            cf = bool((v >> (bits - n)) & 1)
        elif m == "shr":
            r = v >> n
            cf = bool((v >> (n - 1)) & 1)
        else:
            r = (_sext(v, bits) >> n) & msk
            cf = bool((_sext(v, bits) >> (n - 1)) & 1)
        fl = _logic_flags(r, bits)
        fl["CF"] = cf
        if n != 1:
            fl.pop("OF")
        elif m in ("shl", "sal"):
            fl["OF"] = bool(r >> (bits - 1)) != cf
        elif m == "shr":
            fl["OF"] = bool(v >> (bits - 1))
        ev.put(ci, ops[0], r)
        ev.setflags(fl)
        return True
    if m.startswith("set") and len(ops) == 1 and ops[0].type == X.X86_OP_REG:
        c = jcc_condition(m, flags)
        ev.put(ci, ops[0], None if c is None else int(c))
        return True
    if m.startswith("cmov") and len(ops) == 2 and ops[0].type == X.X86_OP_REG:
        c = jcc_condition(m, flags)
        if c is None:
            ev.put(ci, ops[0], None)
        elif c:
            s = ev.get(ci, ops[1])
            ev.put(ci, ops[0], None if s is None else s[0])
        else:
            a = ev.get(ci, ops[0])  # 32-bit cmov zero-extends even when not moving
            ev.put(ci, ops[0], None if a is None else a[0])
        return True
    return False


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def build_hconst(h: UnitHostOps, g: HCfg, facts: Mapping[str, int] | None = None, *,
                 code: bytes | None = None, max_rounds: int = 64, effects: Any = None
                 ) -> HConst:
    """The lattice of unit ``h`` (bytes ``code``, default the unit's) under ``facts``
    (``{"r0": 0, ...}``) at its entry. ``effects`` (a bound helper effect table) defaults
    to the one bound to ``h``'s EnvLayout."""
    from .helper_effects import effects_for_hostops

    fx = effects if effects is not None else effects_for_hostops(h)
    facts = {str(k): int(v) & 0xFFFFFFFF for k, v in (facts or {}).items()}
    bad = sorted(set(facts) - set(FACT_FIELDS))
    if bad:
        raise ValueError(f"not fact fields: {bad}")
    ftuple = tuple(sorted(facts.items()))
    if not h.ok or not g.ok:
        return HConst(h.uid, False, tuple(h.reasons) + tuple(g.reasons), ftuple)
    if code is None:
        raise ValueError("build_hconst needs the unit's code bytes")
    n = len(h.ops)
    tracked = frozenset(FACT_FIELDS)
    ins: list[_State | None] = [None] * n
    if n == 0:
        return HConst(h.uid, True, (), ftuple)
    ins[0] = _State.of(facts, {}, {})
    notes: dict[int, dict[str, Any]] = {}
    work = [0]
    rounds = 0
    seen_count = [0] * n
    while work:
        work.sort()
        i = work.pop(0)
        st = ins[i]
        if st is None:
            continue
        seen_count[i] += 1
        if seen_count[i] > max_rounds:
            return HConst(h.uid, False, ("hconst_did_not_converge",), ftuple)
        out, note = _step(h, i, code, st, tracked, fx)
        notes[i] = note
        succ = g.successors(i, atomic=True)
        o = h.ops[i]
        if "decided" in note and o.kind == OpKind.JCC and o.block == "insn" and \
                o.log is None and "LDST_FAST_JCC" not in o.sites and len(succ) == 2:
            nxt = i + 1
            tgt = next((j for j in succ if j != nxt), None)
            succ = (tgt,) if note["decided"] else (nxt,)
        for j in succ:
            if j is None:
                continue
            new = out if ins[j] is None else ins[j].meet(out)
            if new != ins[j]:
                ins[j] = new
                if j not in work:
                    work.append(j)
        rounds += 1
    reach = frozenset(i for i in range(n) if ins[i] is not None)
    decided: dict[int, bool] = {}
    fold: dict[int, tuple[str, int, str]] = {}
    for i in sorted(reach):
        o = h.ops[i]
        nt = notes.get(i, {})
        if o.log is not None:
            continue  # the LOG_REG block is byte-frozen: never folded or decided
        if "decided" in nt and o.kind == OpKind.JCC and o.block == "insn" and \
                "LDST_FAST_JCC" not in o.sites:
            decided[i] = bool(nt["decided"])
        if "fold" in nt and o.block == "insn":
            fold[i] = nt["fold"]
    return HConst(h.uid, True, (), ftuple, tuple(ins), decided, fold, reach)


def _compute(ctx: Any, scope: ScopeRef) -> HConst:
    h = ctx.analysis("hostops", scope).value
    g = ctx.analysis("hcfg", scope).value
    u = ctx.unit(scope.id)
    return build_hconst(h, g, {}, code=u.code)


HCONST = AnalysisSpec(name="hconst", version=1, scope="unit",
                      requires=(("hostops", 1), ("hcfg", 1)),
                      inputs=frozenset({"code", "meta"}), compute=_compute, result_schema=HConst)
ANALYSES = (HCONST,)
