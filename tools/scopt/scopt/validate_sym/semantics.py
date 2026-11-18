"""x86-64 instruction semantics of ``hostsym`` (B3/B4 design §9.2). WP19.

**Concrete** (exact, checked natively by V18 :mod:`.selftest`) for::

    mov movabs movzx movsx movsxd lea add sub and or xor not neg inc dec
    test cmp shl/shr/sar (imm, 32/64-bit) setcc cmovcc nop

with the flags ZF SF CF OF PF (AF is never modelled: it is a term). Every
other mnemonic is an **uninterpreted deterministic function** of all its
inputs (explicit and implicit registers, immediates, memory values, flags
read): each output (register, memory, flag) is the term
``("uf", sig, inputs, slot)``. That is sound for syntactically equal
instructions on equal inputs (§9.2). Control flow (jmp/jcc/call/ret/int3)
is handled by :mod:`.paths`.

Memory operands are delegated to a ``mem`` object (``read(st, ci, op, w)``,
``write(st, ci, op, w, value)``) supplied by the path engine; it returns
:class:`Unsupported` for operands it cannot model.
"""

from __future__ import annotations

from typing import Any

import capstone
from capstone import x86 as X

from .state import State, Term, binop, ins, is_const, lo, mask, sx, unop, zx

__all__ = ["Unsupported", "reg_info", "execute", "cond_of", "cc_of", "bnot", "CONCRETE",
           "FLAG_NAMES", "eval_cond", "apply_cc"]

FLAG_NAMES = ("ZF", "SF", "CF", "OF", "PF", "AF")
CONCRETE = frozenset({"mov", "movabs", "movzx", "movsx", "movsxd", "lea", "add", "sub", "and",
                      "or", "xor", "not", "neg", "inc", "dec", "test", "cmp", "shl", "shr",
                      "sar", "nop"})


class Unsupported(Exception):
    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


_FAM: dict[str, tuple[str, int]] = {}
for _b, _names in {"rax": "rax eax ax al", "rbx": "rbx ebx bx bl", "rcx": "rcx ecx cx cl",
                   "rdx": "rdx edx dx dl", "rsi": "rsi esi si sil", "rdi": "rdi edi di dil",
                   "rbp": "rbp ebp bp bpl", "rsp": "rsp esp sp spl"}.items():
    for _n in _names.split():
        _FAM[_n] = (_b, 8 if _n.startswith("r") else 4 if _n.startswith("e") else
                    2 if len(_n) == 2 and _n[1] in "xip" else 1)
for _k in range(8, 16):
    for _suf, _w in (("", 8), ("d", 4), ("w", 2), ("b", 1)):
        _FAM[f"r{_k}{_suf}"] = (f"r{_k}", _w)


def reg_info(name: str) -> tuple[str, int]:
    """``"r12d"`` -> ``("r12", 4)``; raises :class:`Unsupported` for ah/bh/xmm/segment regs."""
    f = _FAM.get(name)
    if f is None:
        raise Unsupported(f"register:{name}")
    return f


# ---------------------------------------------------------------------------
# registers
# ---------------------------------------------------------------------------

def rd_reg(st: State, name: str) -> Term:
    fam, w = reg_info(name)
    return lo(w, st.regs[fam])


def wr_reg(st: State, name: str, v: Term) -> None:
    fam, w = reg_info(name)
    if fam in ("rbp", "rsp"):
        raise Unsupported(f"write:{fam}")
    if w == 8:
        st.regs[fam] = v
    elif w == 4:
        st.regs[fam] = zx(4, v)
    else:
        st.regs[fam] = ins(w, st.regs[fam], v)


# ---------------------------------------------------------------------------
# flags and conditions
# ---------------------------------------------------------------------------

def _parity(v: int) -> int:
    return 1 - (bin(v & 0xFF).count("1") & 1)


def _msb(v: int, w: int) -> int:
    return (v >> (8 * w - 1)) & 1


def _set_result_flags(st: State, w: int, r: Term) -> None:
    if is_const(r):
        st.flags["ZF"] = int(r == 0)
        st.flags["SF"] = _msb(r, w)
        st.flags["PF"] = _parity(r)
    else:
        st.flags["ZF"] = ("Z", w, r)
        st.flags["SF"] = ("S", w, r)
        st.flags["PF"] = ("P", r)


def _arith_flags(st: State, name: str, w: int, a: Term, b: Term, r: Term) -> None:
    """Flags of add/sub (cmp) with operands ``a``, ``b`` and result ``r``."""
    _set_result_flags(st, w, r)
    st.flags["AF"] = ("A", name, w, a, b)
    if is_const(a) and is_const(b):
        if name == "add":
            st.flags["CF"] = int(a + b > mask(w))
            st.flags["OF"] = int(_msb(a, w) == _msb(b, w) and _msb(r, w) != _msb(a, w))
        else:
            st.flags["CF"] = int(a < b)
            st.flags["OF"] = int(_msb(a, w) != _msb(b, w) and _msb(r, w) != _msb(a, w))
        st.flags["AF"] = int((((a ^ b ^ r) >> 4) & 1))
    elif b == 0:
        st.flags["CF"] = 0
        st.flags["OF"] = 0
    else:
        st.flags["CF"] = ("C", name, w, a, b)
        st.flags["OF"] = ("O", name, w, a, b)


def _logic_flags(st: State, w: int, r: Term, ci: Any) -> None:
    _set_result_flags(st, w, r)
    st.flags["CF"] = 0
    st.flags["OF"] = 0
    st.flags["AF"] = ("undef", "AF", ci.mnemonic, w, r)


def bnot(x: Term) -> Term:
    if is_const(x):
        return 1 - x
    if x[0] == "bnot":
        return x[1]
    return ("bnot", x)


def _bor(x: Term, y: Term) -> Term:
    if is_const(x):
        return 1 if x else y
    if is_const(y):
        return 1 if y else x
    if x == y:
        return x
    return ("bor",) + tuple(sorted((x, y), key=hash))


def _bne(x: Term, y: Term) -> Term:
    if is_const(x) and is_const(y):
        return int(x != y)
    if is_const(x):
        return bnot(y) if x else y
    if is_const(y):
        return bnot(x) if y else x
    if x == y:
        return 0
    return ("bne",) + tuple(sorted((x, y), key=hash))


_CC_POS = {"e": "e", "z": "e", "ne": "e", "nz": "e", "l": "l", "nge": "l", "ge": "l", "nl": "l",
           "le": "le", "ng": "le", "g": "le", "nle": "le", "b": "b", "c": "b", "nae": "b",
           "ae": "b", "nb": "b", "nc": "b", "be": "be", "na": "be", "a": "be", "nbe": "be",
           "s": "s", "ns": "s", "o": "o", "no": "o", "p": "p", "pe": "p", "np": "p", "po": "p"}
_CC_NEG = {"ne", "nz", "ge", "nl", "g", "nle", "ae", "nb", "nc", "a", "nbe", "ns", "no", "np",
           "po"}


def cc_of(mnemonic: str, prefix: str) -> tuple[str, bool]:
    """``("jne", "j")`` -> ``("e", False)``: the positive condition and its polarity."""
    s = mnemonic[len(prefix):]
    if s not in _CC_POS:
        raise Unsupported(f"cc:{mnemonic}")
    return _CC_POS[s], s not in _CC_NEG


def eval_cond(st: State, cc: str) -> Term:
    """Truth term (0/1 int or a boolean term) of the positive condition ``cc``."""
    f = st.flags
    if cc == "e":
        return f["ZF"]
    if cc == "l":
        return _bne(f["SF"], f["OF"])
    if cc == "le":
        return _bor(f["ZF"], _bne(f["SF"], f["OF"]))
    if cc == "b":
        return f["CF"]
    if cc == "be":
        return _bor(f["CF"], f["ZF"])
    if cc == "s":
        return f["SF"]
    if cc == "o":
        return f["OF"]
    if cc == "p":
        return f["PF"]
    raise Unsupported(f"cc:{cc}")  # pragma: no cover


def cond_of(st: State, mnemonic: str, prefix: str) -> Term:
    """Truth term of a jcc/setcc/cmovcc condition (polarity applied)."""
    cc, pos = cc_of(mnemonic, prefix)
    t = eval_cond(st, cc)
    return t if pos else bnot(t)


def apply_cc(st: State, t: Term) -> Term:
    """Use what the path already decided about ``t`` (its condition list)."""
    if is_const(t):
        return t
    if t in st.known:
        return int(st.known[t])
    if t[0] == "bnot" and t[1] in st.known:
        return int(not st.known[t[1]])
    return t


# ---------------------------------------------------------------------------
# operands
# ---------------------------------------------------------------------------

def _rd_op(st: State, ci: Any, op: Any, mem: Any, w: int | None = None) -> Term:
    if op.type == X.X86_OP_REG:
        return rd_reg(st, ci.reg_name(op.reg))
    if op.type == X.X86_OP_IMM:
        return op.imm & mask(w or op.size or 8)
    if op.type == X.X86_OP_MEM:
        return mem.read(st, ci, op, w or op.size)
    raise Unsupported("operand")


def _wr_op(st: State, ci: Any, op: Any, v: Term, mem: Any) -> None:
    if op.type == X.X86_OP_REG:
        wr_reg(st, ci.reg_name(op.reg), v)
    elif op.type == X.X86_OP_MEM:
        mem.write(st, ci, op, op.size, v)
    else:
        raise Unsupported("operand")


def lea_addr(st: State, ci: Any, op: Any) -> Term:
    """The effective address term of memory operand ``op`` (no RIP)."""
    mo = op.mem
    if mo.segment != 0:
        raise Unsupported("segment")
    a: Term = 0
    if mo.base != 0:
        a = st.regs[reg_info(ci.reg_name(mo.base))[0]]
    if mo.index != 0:
        iv = st.regs[reg_info(ci.reg_name(mo.index))[0]]
        if mo.scale != 1:
            iv = binop("mul", 8, iv, mo.scale)
        a = binop("add", 8, a, iv)
    return binop("add", 8, a, mo.disp & mask(8))


# ---------------------------------------------------------------------------
# instructions
# ---------------------------------------------------------------------------

def execute(st: State, ci: Any, mem: Any, *, allow_lock: bool = False) -> None:
    """Execute one non-control instruction on ``st`` (raises :class:`Unsupported`)."""
    m = ci.mnemonic
    ops = ci.operands
    if m == "nop" or m.startswith("nop"):
        return
    if ci.prefix[0] == 0xF0 or m.startswith("lock "):
        if not allow_lock:  # lock (rep/repne string forms are refused by mnemonic)
            raise Unsupported(f"prefix:{m}")
        m = m.removeprefix("lock ")
    if m in ("mov", "movabs"):
        a, b = ops
        v = _rd_op(st, ci, b, mem, a.size)
        _wr_op(st, ci, a, v, mem)
        return
    if m in ("movzx", "movsx", "movsxd"):
        a, b = ops
        v = _rd_op(st, ci, b, mem)
        v = zx(b.size, v) if m == "movzx" else sx(b.size, a.size, v)
        _wr_op(st, ci, a, lo(a.size, v), mem)
        return
    if m == "lea":
        a, b = ops
        if b.mem.base == X.X86_REG_RIP:
            v = mem.rip_addr(st, ci, b)
        else:
            v = lea_addr(st, ci, b)
        _wr_op(st, ci, a, lo(a.size, v), mem)
        return
    if m in ("add", "sub", "and", "or", "xor", "test", "cmp"):
        a, b = ops
        w = a.size
        if m == "xor" and a.type == b.type == X.X86_OP_REG and a.reg == b.reg:
            x = y = 0
        else:
            x = _rd_op(st, ci, a, mem, w)
            y = _rd_op(st, ci, b, mem, w)
            if b.type == X.X86_OP_IMM:
                y = (b.imm & mask(8)) & mask(w)  # sign-extended imm, truncated to w
        name = {"test": "and", "cmp": "sub"}.get(m, m)
        r = binop(name, w, x, y)
        if name in ("add", "sub"):
            _arith_flags(st, name, w, x, y, r)
        else:
            _logic_flags(st, w, r, ci)
        if m not in ("test", "cmp"):
            _wr_op(st, ci, a, r, mem)
        return
    if m in ("inc", "dec"):
        (a,) = ops
        w = a.size
        x = _rd_op(st, ci, a, mem, w)
        name = "add" if m == "inc" else "sub"
        r = binop(name, w, x, 1)
        cf = st.flags["CF"]
        _arith_flags(st, name, w, x, 1, r)
        st.flags["CF"] = cf
        _wr_op(st, ci, a, r, mem)
        return
    if m in ("not", "neg"):
        (a,) = ops
        w = a.size
        x = _rd_op(st, ci, a, mem, w)
        r = unop(m, w, x)
        if m == "neg":
            _arith_flags(st, "sub", w, 0, x, r)
            if not is_const(x):
                st.flags["CF"] = ("C", "neg", w, x)
                st.flags["OF"] = ("O", "neg", w, x)
        _wr_op(st, ci, a, r, mem)
        return
    if m in ("shl", "shr", "sar") and len(ops) == 2 and ops[1].type == X.X86_OP_IMM and \
            ops[0].size in (4, 8):
        a, b = ops
        w = a.size
        c = b.imm & (0x3F if w == 8 else 0x1F)
        if c == 0:
            return  # result and flags unchanged
        x = _rd_op(st, ci, a, mem, w)
        r = binop(m, w, x, c)
        _set_result_flags(st, w, r)
        if is_const(x):
            if m == "shl":
                cf = (x >> (8 * w - c)) & 1
            elif m == "shr":
                cf = (x >> (c - 1)) & 1
            else:
                cf = (sx(w, 8, x) >> (c - 1)) & 1 if c - 1 < 64 else _msb(x, w)
            st.flags["CF"] = cf
            if c == 1:
                st.flags["OF"] = (_msb(r, w) ^ cf) if m == "shl" else (_msb(x, w) if m == "shr"
                                                                        else 0)
            else:
                st.flags["OF"] = ("undef", "OF", m, w, x, c)
        else:
            st.flags["CF"] = ("C", m, w, x, c)
            st.flags["OF"] = ("O", m, w, x, c) if c == 1 else ("undef", "OF", m, w, x, c)
        st.flags["AF"] = ("undef", "AF", m, w, x, c)
        _wr_op(st, ci, a, r, mem)
        return
    if m.startswith("set") and len(ops) == 1 and ops[0].size == 1:
        t = apply_cc(st, cond_of(st, m, "set"))
        _wr_op(st, ci, ops[0], t if is_const(t) else ("b2i", t), mem)
        return
    if m.startswith("cmov") and len(ops) == 2 and ops[0].type == X.X86_OP_REG:
        a, b = ops
        w = a.size
        t = apply_cc(st, cond_of(st, m, "cmov"))
        x = _rd_op(st, ci, a, mem, w)
        y = _rd_op(st, ci, b, mem, w)
        if is_const(t):
            r = y if t else x
        elif x == y:
            r = x
        else:
            r = ("ite", t, y, x)
        _wr_op(st, ci, a, r, mem)  # 32-bit cmov zero-extends even when false
        return
    _uninterpreted(st, ci, mem)


_FLAG_BITS = {"CF": "CF", "PF": "PF", "AF": "AF", "ZF": "ZF", "SF": "SF", "OF": "OF"}


def _eflags(ci: Any) -> tuple[list[str], list[str]]:
    ef = ci.eflags
    rd: list[str] = []
    wr: list[str] = []
    for nm in dir(X):
        if not nm.startswith("X86_EFLAGS_"):
            continue
        parts = nm[len("X86_EFLAGS_"):].split("_", 1)
        if len(parts) != 2 or parts[1] not in _FLAG_BITS:
            continue
        if ef & getattr(X, nm):
            (rd if parts[0] == "TEST" else wr).append(parts[1])
    return sorted(set(rd)), sorted(set(wr))


_EF_CACHE: dict[int, tuple[list[str], list[str]]] = {}


def _uninterpreted(st: State, ci: Any, mem: Any) -> None:
    m = ci.mnemonic
    ops = ci.operands
    if m in ("push", "pop", "pushfq", "popfq", "leave", "enter", "cpuid", "rdtsc", "syscall",
             "xchg", "cmpxchg", "xadd", "hlt", "ud2", "lfence", "mfence", "sfence") or \
            m.startswith("rep") or m.startswith("lods") or m.startswith("stos") or \
            m.startswith("movs") and m not in ("movsx", "movsxd"):
        raise Unsupported(f"mnemonic:{m}")
    try:
        rr, rw = ci.regs_access()
    except capstone.CsError as e:  # pragma: no cover
        raise Unsupported(f"regs_access:{m}") from e
    explicit = {ci.reg_name(o.reg) for o in ops if o.type == X.X86_OP_REG}
    imp_r = sorted(n for n in (ci.reg_name(r) for r in rr)
                   if n not in explicit and n not in ("rflags", "eflags", "flags"))
    imp_w = sorted(n for n in (ci.reg_name(r) for r in rw)
                   if n not in explicit and n not in ("rflags", "eflags", "flags"))
    key = ci.eflags
    ef = _EF_CACHE.get(key)
    if ef is None:
        ef = _EF_CACHE[key] = _eflags(ci)
    f_rd, f_wr = ef
    inputs: list[Term] = []
    sig: list[Any] = [m]
    for o in ops:
        acc = getattr(o, "access", 0)
        sig.append((o.type, o.size))
        if o.type == X.X86_OP_IMM:
            inputs.append(o.imm & mask(8))
        elif (acc & capstone.CS_AC_READ) or acc == 0 or m.startswith("cmov") or \
                m in ("bsr", "bsf"):  # conditional destinations depend on their old value
            inputs.append(_rd_op(st, ci, o, mem))
        else:
            inputs.append(None)
    for n in imp_r:
        inputs.append(rd_reg(st, n))
    for f in f_rd:
        inputs.append(st.flags[f])
    sig_t = tuple(sig)
    in_t = tuple(inputs)
    j = 0
    for o in ops:
        acc = getattr(o, "access", 0)
        if o.type != X.X86_OP_IMM and (acc & capstone.CS_AC_WRITE):
            _wr_op(st, ci, o, ("uf", sig_t, in_t, j, o.size), mem)
        j += 1
    for n in imp_w:
        fam, w = reg_info(n)
        wr_reg(st, n, ("uf", sig_t, in_t, n, w))
    for f in f_wr:
        st.flags[f] = ("uf", sig_t, in_t, f)
