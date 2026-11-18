"""Symbolic state of ``hostsym`` (B3/B4 design §9.2). WP19.

Terms
-----
A term is a Python ``int`` (a concrete value, always masked to its width)
or a tuple ``(op, *args)``. Terms are built only through the constructors
below, which fold constants and apply a few algebraic identities, so the
original and the candidate side of a comparison produce *syntactically
equal* terms for equal computations. Term equality is plain ``==``.

Widths are implicit: every term stands for a value of the width it was
built with; a 64-bit register holding a 32-bit result is ``("zx", 4, t)``.

Leaves::

    ("ENV",)                 the env pointer (``rbp`` at entry)
    ("FRAME",)               the frame pointer (``rsp`` at entry)
    ("in", reg)              a host register at entry (never observable)
    ("env0", off, w)         env bytes [off, off+w) at entry (unless P fixes them)
    ("b0", leaf, b) / ("hv", epoch, old)  one byte at entry / after a havoc
    ("frame0", off, w)       frame bytes at entry
    ("own", rel)             an address inside the unit's own [TB struct, end):
                             struct pointers and pool/code addresses compare
                             modulo relocation (C' ~ C, design §9.2)
    ("hmem", ver, addr, w)   a non-env host memory read (version bumps at every
                             host store, call and LDST)
    ("gld", k, oi, addr, seq)  the value of guest load LDST k
    ("ret", seq) / ("clob", ...)  call results and havocked registers/flags

Memory (:class:`ByteMem`) is byte-addressed: each written byte remembers the
write it came from, so partial overlaps are exact (no "envmix" guess).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Union

__all__ = [
    "Term", "ENV", "FRAME", "mask", "is_const", "lo", "zx", "sx", "ins", "binop", "unop",
    "byte_of", "cat", "ByteMem", "State", "Event", "mentions",
]

Term = Union[int, tuple]
ENV: tuple = ("ENV",)
FRAME: tuple = ("FRAME",)


def mask(w: int) -> int:
    return (1 << (8 * w)) - 1


def is_const(t: Any) -> bool:
    return isinstance(t, int)


def _key(t: Term) -> tuple[int, int]:
    return (1, 0) if isinstance(t, int) else (0, hash(t))


def lo(w: int, t: Term) -> Term:
    """The low ``w`` bytes of ``t``."""
    if isinstance(t, int):
        return t & mask(w)
    if w == 8:
        return t
    op = t[0]
    if op == "zx" and t[1] >= w:
        return t[2] if t[1] == w else lo(w, t[2])
    if op == "ins" and t[1] >= w:
        return t[3] if t[1] == w else lo(w, t[3])
    if op == "lo" and t[1] >= w:
        return ("lo", w, t[2])
    if op == "sx" and t[1] >= w:  # sign extension from >= w bytes keeps the low w bytes
        return t[3] if t[1] == w else lo(w, t[3])
    return ("lo", w, t)


def zx(w: int, t: Term) -> Term:
    """``t`` (a ``w``-byte value) zero-extended to 64 bits."""
    if isinstance(t, int):
        return t & mask(w)
    if w == 8:
        return t
    if t[0] == "zx" and t[1] <= w:
        return t
    return ("zx", w, t)


def sx(wf: int, wt: int, t: Term) -> Term:
    """Sign-extend the ``wf``-byte value ``t`` to ``wt`` bytes."""
    if isinstance(t, int):
        v = t & mask(wf)
        if v >> (8 * wf - 1):
            v -= 1 << (8 * wf)
        return v & mask(wt)
    if wf == wt:
        return t
    return ("sx", wf, wt, t)


def ins(w: int, old: Term, t: Term) -> Term:
    """64-bit register ``old`` with its low ``w`` (1 or 2) bytes replaced by ``t``."""
    if isinstance(old, int) and isinstance(t, int):
        return (old & ~mask(w) & mask(8)) | (t & mask(w))
    return ("ins", w, old, t)


_COMM = frozenset({"add", "and", "or", "xor", "mul"})


def binop(name: str, w: int, a: Term, b: Term) -> Term:
    """``w``-byte ``a <name> b`` with folding (add sub and or xor shl shr sar mul)."""
    m = mask(w)
    if isinstance(a, int) and isinstance(b, int):
        if name == "add":
            return (a + b) & m
        if name == "sub":
            return (a - b) & m
        if name == "and":
            return a & b
        if name == "or":
            return a | b
        if name == "xor":
            return a ^ b
        if name == "mul":
            return (a * b) & m
        if name == "shl":
            return (a << b) & m
        if name == "shr":
            return (a & m) >> b
        if name == "sar":
            v = a & m
            if v >> (8 * w - 1):
                v -= 1 << (8 * w)
            return (v >> b) & m
        raise ValueError(name)
    if name in ("add", "or", "xor", "sub", "shl", "shr", "sar") and b == 0:
        return a
    if name in ("add", "or", "xor") and a == 0:
        return b
    if name == "and" and (a == 0 or b == 0):
        return 0
    if name == "and" and (a == m):
        return b
    if name == "and" and (b == m):
        return a
    if name in ("and", "or") and a == b:
        return a
    if name in ("xor", "sub") and a == b:
        return 0
    if name in _COMM and _key(a) > _key(b):
        a, b = b, a
    return (name, w, a, b)


def unop(name: str, w: int, a: Term) -> Term:
    m = mask(w)
    if isinstance(a, int):
        if name == "not":
            return ~a & m
        if name == "neg":
            return (-a) & m
        raise ValueError(name)
    if a[0] == name and a[1] == w:
        return a[2]
    return (name, w, a)


def byte_of(t: Term, i: int) -> Term:
    if isinstance(t, int):
        return (t >> (8 * i)) & 0xFF
    return ("byte", t, i)


def cat(parts: Iterable[Term]) -> Term:
    """Little-endian concatenation of byte terms."""
    ps = list(parts)
    if all(isinstance(p, int) for p in ps):
        v = 0
        for i, p in enumerate(ps):
            v |= p << (8 * i)
        return v
    return ("cat", tuple(ps))


def mentions(t: Term, leaf: tuple) -> bool:
    if isinstance(t, int):
        return False
    if t == leaf:
        return True
    return any(isinstance(x, tuple) and mentions(x, leaf) for x in t[1:])


# ---------------------------------------------------------------------------
# byte memory
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _W:
    """One write: ``term`` of ``width`` bytes stored at ``off``."""

    term: Term
    off: int
    width: int


class ByteMem:
    """Byte-addressed symbolic memory (env or frame).

    Never-written bytes read as ``("b0", leaf, b)`` (or the precondition's
    concrete byte). A havoc (a non-pure call, an LDST slow path) maps every
    byte it does not ``keep`` to ``("hv", epoch, old_byte)``: the new value
    is an unknown function of the old one, so two sides whose byte differed
    before still differ after (a deleted store never becomes invisible),
    and two sides that agreed still agree (both run the same call on an
    env that the comparison at the call event showed equal).
    """

    __slots__ = ("bytes", "havocs", "pre", "leaf")

    def __init__(self, leaf: str, pre: Mapping[int, int] | None = None):
        self.bytes: dict[int, tuple[_W, int]] = {}
        self.havocs: tuple[tuple[Any, Callable[[int], bool] | None], ...] = ()
        self.pre: Mapping[int, int] = pre or {}  # byte offset -> concrete byte (precondition P)
        self.leaf = leaf

    def copy(self) -> "ByteMem":
        m = ByteMem(self.leaf, self.pre)
        m.bytes = dict(self.bytes)
        m.havocs = self.havocs
        return m

    def write(self, off: int, w: int, t: Term) -> None:
        wr = _W(t, off, w)
        for i in range(w):
            self.bytes[off + i] = (wr, i)

    def havoc(self, epoch: Any, keep: Callable[[int], bool] | None = None) -> None:
        """Every byte except those ``keep`` accepts becomes ``("hv", epoch, old)``."""
        for b, (wr, i) in list(self.bytes.items()):
            if keep is None or not keep(b):
                self.bytes[b] = (_W(("hv", epoch, self._wbyte(wr, i)), b, 1), 0)
        self.havocs = self.havocs + ((epoch, keep),)

    def _wbyte(self, wr: _W, i: int) -> Term:
        t = wr.term
        if isinstance(t, tuple) and t[0] == self.leaf + "0" and t[2] == wr.width:
            return ("b0", self.leaf, t[1] + i)  # a stored initial value is the initial byte
        if wr.width == 1 and i == 0:
            return t
        return byte_of(t, i)

    def _init(self, b: int) -> Term:
        v: Term = self.pre[b] if b in self.pre else ("b0", self.leaf, b)
        for epoch, keep in self.havocs:
            if keep is None or not keep(b):
                v = ("hv", epoch, v)
        return v

    def byte(self, b: int) -> Term:
        """Canonical term of byte ``b`` (used by the snapshot comparison)."""
        s = self.bytes.get(b)
        if s is not None:
            return self._wbyte(*s)
        return self._init(b)

    def read(self, off: int, w: int) -> Term:
        srcs = [self.bytes.get(off + i) for i in range(w)]
        s0 = srcs[0]
        if s0 is not None and s0[1] == 0 and s0[0].off == off and \
                all(s is not None and s[0] is s0[0] and s[1] == i for i, s in enumerate(srcs)):
            wr = s0[0]
            return wr.term if wr.width == w else lo(w, wr.term)
        if all(s is None for s in srcs) and not any(
                keep is None or not keep(off + i) for _, keep in self.havocs for i in range(w)):
            if all((off + i) in self.pre for i in range(w)):
                return cat(self.pre[off + i] for i in range(w))
            if not any((off + i) in self.pre for i in range(w)):
                return (self.leaf + "0", off, w)
        return cat(self.byte(off + i) for i in range(w))

    def written(self) -> set[int]:
        return set(self.bytes)


# ---------------------------------------------------------------------------
# path state
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Event:
    """One observable event of a path.

    ``kind``: ``call`` | ``guest`` | ``host_store`` | ``exit``. ``data`` holds
    the non-env payload terms; ``env``/``frame`` are frozen memories (compared
    under the observation contract for ``cls``/``exit_kind``, and a call event's
    ``reads`` bytes in addition); ``pc`` is the host offset of the instruction
    (diagnostics only, never compared).
    """

    kind: str
    cls: str
    data: tuple
    env: ByteMem | None = None
    frame: ByteMem | None = None
    exit_kind: str | None = None
    pc: int = -1
    #: call events: the env bytes the helper may read (a byte predicate, compared at the
    #: event in addition to the bytes the observation contract makes observable there)
    reads: Callable[[int], bool] | None = None


@dataclass
class State:
    regs: dict[str, Term]
    flags: dict[str, Term]
    env: ByteMem
    frame: ByteMem
    hver: int = 0
    events: list[Event] = field(default_factory=list)
    cond: list[tuple[Term, bool, int]] = field(default_factory=list)  # (cond, outcome, pc)
    known: dict[Term, bool] = field(default_factory=dict)
    stub_writes: list[int] = field(default_factory=list)  # pcs of stores/events inside a guard stub
    steps: int = 0
    unsupported: tuple[str, int] | None = None
    abs_refs: list[tuple[int, Term, str, Term | None]] = field(default_factory=list)
    # (pc, effective address, r|w, base pointer)
    end: tuple | None = None
    assumes: list[tuple] = field(default_factory=list)  # abstraction assumptions (not in the
    # path signature), e.g. ("logreg_spin_exit", S)

    @classmethod
    def initial(cls, pre: Mapping[int, int] | None = None) -> "State":
        regs: dict[str, Term] = {r: ("in", r) for r in (
            "rax", "rbx", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11", "r12", "r13",
            "r14", "r15")}
        regs["rbp"] = ENV
        regs["rsp"] = FRAME
        flags: dict[str, Term] = {f: ("in", f) for f in ("ZF", "SF", "CF", "OF", "PF", "AF")}
        return cls(regs, flags, ByteMem("env", pre), ByteMem("frame"))

    def fork(self) -> "State":
        s = State(dict(self.regs), dict(self.flags), self.env.copy(), self.frame.copy(),
                  self.hver, list(self.events), list(self.cond), dict(self.known),
                  list(self.stub_writes), self.steps, self.unsupported, list(self.abs_refs),
                  self.end, list(self.assumes))
        return s

    @property
    def seq(self) -> int:
        return len(self.events)

    def signature(self, drop: Callable[[int], bool] | None = None) -> tuple:
        return tuple((c, o) for c, o, pc in self.cond if drop is None or not drop(pc))
