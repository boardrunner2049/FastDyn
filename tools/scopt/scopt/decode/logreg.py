"""The FastDyn coverage ``LOG_REG`` block: one exact, fully explained host form (close-out).

``coverage = true`` (FastDyn ``core/core.c:848-865``) registers an inline
``QEMU_PLUGIN_INLINE_LOG_REG`` on guest instruction 0 of every TB. QEMU's
``gen_inline_log_reg_cb`` / ``log_reg()`` (``target/arm/tcg/translate.c:233-279``)
emits, with a consumer index (the spinning form)::

    wait:  idx  = ld16u [&index]
           next = ext16u(idx + 4)
           cons = ld16u [&tracer_consumed_index]
           brcond EQ next, cons -> wait          # spin while the ring is full
           p    = buffer + (intptr)idx           # add r64,r64
           st32 [p] = cpu_R[15]                  # the coverage event
           st16 [&index] = idx + 4

At the TB's first ``insn_start`` TCG has no live temporaries, so the register
allocator always produces the same 65 bytes (``rbx r12 r13 r14``; design
``design/closeout-logreg.md`` §A.1)::

    +0x00 48 8d 1d d1d1d1d1   lea rbx,[rip+d1]          I := IDX   (MOVI_CONST)  <- spin target
    +0x07 44 0f b7 23         movzx r12d,word [rbx]      X := idx
    +0x0b 45 8d 6c 24 04      lea r13d,[r12+4]           N := X + 4
    +0x10 45 0f b7 ed         movzx r13d,r13w            N := ext16u(N)
    +0x14 4c 8d 35 d2d2d2d2   lea r14,[rip+d2]           C := CONS  (MOVI_CONST)
    +0x1b 45 0f b7 36         movzx r14d,word [r14]      C := consumed
    +0x1f 45 3b ee            cmp r13d,r14d
    +0x22 74 dc               je S                       spin       (LABEL_JCC)
    +0x24 4d 63 ec            movsxd r13,r12d            P := sext(X)
    +0x27 4c 8d 35 d3d3d3d3   lea r14,[rip+d3]           B := BUF   (MOVI_CONST)
    +0x2e 4d 03 ee            add r13,r14                P := BUF + X
    +0x31 44 8b 75 vv         mov r14d,[rbp+vv]          V := env.regs[15] (vv from the EnvLayout)
    +0x35 45 89 75 00         mov [r13],r14d             ring store (the coverage event)
    +0x39 41 83 c4 04         add r12d,4
    +0x3d 66 44 89 23         mov word [rbx],r12w        index store

:func:`match_logreg` is the **one** definition of "the block", shared by
HostOps (``scopt.analysis.hostops``) and ``hostsym``
(``scopt.validate_sym.program``). It is pure (no store, no analysis cache)
and fails closed: a block is matched only when every rule holds, else a
:class:`NoMatch` names the first rule that failed and the caller treats the
unit exactly as before (pinned / cyclic / unsupported).

Rules (design §A.2):

1. ``position`` — the block starts at ``insn0_start``, lies inside guest
   instruction 0's host range and overlaps no other instruction range, LDST
   fast/slow range, pool or guard stub.
2. ``bytes`` — every byte outside the three rel32 holes and the ``regs[15]``
   displacement equals the canonical form.
3. ``metadata`` — the three leas each carry exactly one ``MOVI_CONST`` site
   whose value is the rip-resolved target, the ``je`` carries a ``LABEL_JCC``
   site whose target is the block start, no other site lies inside the block
   and no OpMark lies inside it.
4. ``entry`` — no branch site outside the block targets any byte of it.
5. ``pointers`` — IDX and CONS are importer-bound ``abs.plugin_data`` of the
   same FastDyn module; BUF is a bound pointer into a nameless anonymous
   mapping (``abs.other`` with an ``anon@`` anchor: the tracer thread's malloc
   arena) and **nothing else**; the three are distinct and IDX/CONS lie
   outside the ring extent. Any other ring-base class is refused with the
   stable detail prefix :data:`RING_BASE_REFUSED`. In particular ``abs.heap``
   is refused (closeout2): ``[heap]`` holds the vCPU on the gate binary (env at
   ``0x5555594951c0``, ``reports/B4-gate.md``) and gate runs pass no
   ``env_abs``, so neither this rule nor V12's ``_abs_violations`` could tell a
   ring pointer from an env pointer there. ``abs.guest_ram``, ``abs.qemu_data``,
   ``abs.plugin_data``, ``arena.*``, ``prologue.*``, ``const``, ``ext.*`` and
   every named or file-backed mapping are refused as before.
6. ``field`` — the value load is an exact 4-byte ``regs[15]`` access under the
   bound EnvLayout (disp8 form).
7. ``triple`` — when the caller passes ``expect``, the block's
   ``(IDX, CONS, BUF)`` must equal it (one coverage ring per process). The
   snapshot-wide form of this rule is :func:`snapshot_triples`, enforced by
   ``validate_b3`` (V15) and reported by the HostOps census.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "LOGREG_LEN", "RING_EXTENT", "LOGREG_MODULES", "LogSite", "LogMeta", "LogBlock", "NoMatch",
    "match_logreg", "meta_of_unit", "snapshot_triples", "CANON", "RIP_HOLES", "FIELD_HOLE",
    "SPIN_JCC", "HEAD_END", "RING_STORE", "IDX_STORE", "VALUE_LOAD", "INSNS", "RULES",
    "looks_like_logreg", "BRANCH_SITE_KINDS", "RING_BASE_CLASS", "RING_BASE_ANCHOR",
    "RING_BASE_REFUSED",
]

LOGREG_LEN = 0x41
#: largest byte range a 4-byte store at a 16-bit index can reach: [BUF, BUF + 0xffff + 4)
RING_EXTENT = 0x10003
#: the plugin module whose .bss holds the ring index and the consumer index
LOGREG_MODULES = frozenset({"libfastdyn.so"})
#: the only importer binding a ring base (BUF) may have: a pointer into a nameless anonymous
#: mapping (``classify.HostContext.data_value``: not a RAMBLOCK, not ``[heap]``, not a
#: module's .bss, no path). On the gate binary every real block's BUF is
#: ``anon@0x7fff68000000+0xb70``, the tracer thread's malloc arena (FastDyn core.c:979).
RING_BASE_CLASS = "abs.other"
RING_BASE_ANCHOR = "anon@"
#: stable ``NoMatch.detail`` prefix of rule 5 for a ring base of any other class (fail
#: closed): ``abs.heap`` may alias env (``[heap]`` holds the vCPU on the gate binary),
#: ``abs.guest_ram`` is guest RAM, and every other class is not the tracer's ring arena
RING_BASE_REFUSED = "ring_base_class_refused"

SPIN_JCC = 0x22
HEAD_END = 0x24
VALUE_LOAD = 0x31
RING_STORE = 0x35
IDX_STORE = 0x3D
#: (hole offset, end of the lea it belongs to, role) — rel32 rip displacements
RIP_HOLES = ((0x03, 0x07, "idx"), (0x17, 0x1B, "cons"), (0x2A, 0x2E, "buf"))
#: the disp8 of ``mov r14d,[rbp+disp8]`` (the regs[15] offset from the EnvLayout)
FIELD_HOLE = 0x34

#: the 15 instructions: (offset, size, text)
INSNS = (
    (0x00, 7, "lea rbx,[rip+d1]"), (0x07, 4, "movzx r12d,word [rbx]"),
    (0x0B, 5, "lea r13d,[r12+4]"), (0x10, 4, "movzx r13d,r13w"),
    (0x14, 7, "lea r14,[rip+d2]"), (0x1B, 4, "movzx r14d,word [r14]"),
    (0x1F, 3, "cmp r13d,r14d"), (0x22, 2, "je S"), (0x24, 3, "movsxd r13,r12d"),
    (0x27, 7, "lea r14,[rip+d3]"), (0x2E, 3, "add r13,r14"),
    (0x31, 4, "mov r14d,[rbp+regs15]"), (0x35, 4, "mov [r13],r14d"),
    (0x39, 4, "add r12d,4"), (0x3D, 4, "mov word [rbx],r12w"),
)

CANON = bytes.fromhex(
    "488d1d00000000"  # +0x00 lea rbx,[rip+d1]
    "440fb723"        # +0x07 movzx r12d,word [rbx]
    "458d6c2404"      # +0x0b lea r13d,[r12+4]
    "450fb7ed"        # +0x10 movzx r13d,r13w
    "4c8d3500000000"  # +0x14 lea r14,[rip+d2]
    "450fb736"        # +0x1b movzx r14d,word [r14]
    "453bee"          # +0x1f cmp r13d,r14d
    "74dc"            # +0x22 je S (rel8 -0x24)
    "4d63ec"          # +0x24 movsxd r13,r12d
    "4c8d3500000000"  # +0x27 lea r14,[rip+d3]
    "4d03ee"          # +0x2e add r13,r14
    "448b7500"        # +0x31 mov r14d,[rbp+disp8]
    "45897500"        # +0x35 mov [r13],r14d
    "4183c404"        # +0x39 add r12d,4
    "66448923"        # +0x3d mov word [rbx],r12w
)
assert len(CANON) == LOGREG_LEN
_HOLE_BYTES = frozenset(b for h, _e, _r in RIP_HOLES for b in range(h, h + 4)) | {FIELD_HOLE}

#: the expected sites inside the block: offset -> (kind, field offset, width)
_SITES = {0x00: ("MOVI_CONST", 0x03, 4), 0x14: ("MOVI_CONST", 0x17, 4),
          0x27: ("MOVI_CONST", 0x2A, 4), SPIN_JCC: ("LABEL_JCC", 0x23, 1)}
BRANCH_SITE_KINDS = frozenset({"LABEL_JMP", "LABEL_JCC", "LDST_FAST_JCC", "LDST_RET_JMP",
                               "OTHER_BRANCH"})
RULES = ("layout", "position", "bytes", "field", "metadata", "entry", "pointers", "triple")


@dataclass(frozen=True, slots=True)
class LogSite:
    """One ``.scm`` Site, neutral: kind name, absolute captured value, field position."""

    off: int
    kind: str
    value: int
    field_off: int
    width: int


@dataclass(frozen=True)
class LogMeta:
    """The Tier-0 metadata the matcher reads (built from a captured TBUnit).

    ``site_base`` is the absolute address of ``code[0]`` at capture: branch
    sites are compared as ``value - site_base`` so that a relocated copy of the
    unit (C', R' as placed) matches with the same metadata.
    """

    site_base: int
    insn0_start: int
    insn_rows: tuple[tuple[int, int, int], ...]  # (host_start, host_end, index)
    sites: tuple[LogSite, ...]
    marks: tuple[int, ...]  # OpMark host offsets (any kind)
    busy: tuple[tuple[int, int, str], ...]  # ranges the block may not overlap
    pointers: tuple[tuple[int, str, str], ...]  # (bound value, importer class, anchor)
    value_field: tuple[int, int] | None  # (env offset, size) of regs[15]; None: unbound

    def pointer(self, v: int) -> tuple[str, str] | None:
        for val, cls, anchor in self.pointers:
            if val == v:
                return cls, anchor
        return None

    def shifted(self, mu: Any) -> "LogMeta":
        """The metadata moved through an offset map ``mu`` (``program.remap``)."""
        return LogMeta(
            self.site_base, mu(self.insn0_start),
            tuple((mu(a), mu(b), i) for a, b, i in self.insn_rows),
            tuple(LogSite(mu(s.off), s.kind,
                          (self.site_base + mu(s.value - self.site_base))
                          if s.kind in BRANCH_SITE_KINDS else s.value,
                          mu(s.field_off), s.width) for s in self.sites),
            tuple(mu(m) for m in self.marks),
            tuple((mu(a), mu(b), w) for a, b, w in self.busy),
            self.pointers, self.value_field)


@dataclass(frozen=True)
class NoMatch:
    rule: str
    detail: str = ""

    def __bool__(self) -> bool:  # a NoMatch is falsy, a LogBlock truthy
        return False


@dataclass(frozen=True)
class LogBlock:
    start: int
    idx_abs: int
    cons_abs: int
    ring_abs: int
    value_off: int  # env offset of regs[15]
    canon: str  # sha256 of the bytes with the holes replaced by the resolved targets
    module: str
    ring_class: str  # importer class of BUF (always RING_BASE_CLASS)
    end: int = field(init=False)
    spin_jcc: int = field(init=False)
    head_end: int = field(init=False)
    value_load: int = field(init=False)
    ring_store: int = field(init=False)
    idx_store: int = field(init=False)

    def __post_init__(self) -> None:
        s = self.start
        for k, v in (("end", s + LOGREG_LEN), ("spin_jcc", s + SPIN_JCC),
                     ("head_end", s + HEAD_END), ("value_load", s + VALUE_LOAD),
                     ("ring_store", s + RING_STORE), ("idx_store", s + IDX_STORE)):
            object.__setattr__(self, k, v)

    ring_extent: int = RING_EXTENT
    value_field: str = "regs[15]"
    regs = (("I", "rbx"), ("X", "r12"), ("N", "r13"), ("C", "r14"), ("P", "r13"), ("B", "r14"),
            ("V", "r14"))

    @property
    def triple(self) -> tuple[int, int, int]:
        return (self.idx_abs, self.cons_abs, self.ring_abs)

    def contains(self, off: int) -> bool:
        return self.start <= off < self.end

    def overlaps(self, a: int, b: int) -> bool:
        return a < self.end and self.start < b

    def to_json(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "spin_jcc": self.spin_jcc,
                "ring_store": self.ring_store, "idx_store": self.idx_store,
                "idx_abs": hex(self.idx_abs), "cons_abs": hex(self.cons_abs),
                "ring_abs": hex(self.ring_abs), "ring_extent": self.ring_extent,
                "value_field": self.value_field, "value_off": self.value_off,
                "module": self.module, "ring_class": self.ring_class, "canon": self.canon}


def _rel32(code: bytes, off: int) -> int:
    return int.from_bytes(code[off:off + 4], "little", signed=True)


def _module_of(anchor: str) -> str:
    return anchor.split("+", 1)[0] if "+" in anchor else ""


def match_logreg(code: bytes, base: int, meta: LogMeta | None, *,
                 expect: tuple[int, int, int] | None = None) -> LogBlock | NoMatch:
    """The LOG_REG block at ``meta.insn0_start`` of ``code`` (placed at ``base``), or the
    first rule it fails (see the module doc)."""
    if meta is None:
        return NoMatch("layout", "no metadata")
    vf = meta.value_field
    if vf is None or vf[1] != 4 or not 0 <= vf[0] <= 0x7F:
        return NoMatch("layout", f"regs[15] not bound as a 4-byte disp8 field: {vf}")
    s = meta.insn0_start
    e = s + LOGREG_LEN
    code = bytes(code)
    # 1. position --------------------------------------------------------------------
    if s < 0 or e > len(code):
        return NoMatch("position", "block outside the code")
    rows0 = [(a, b) for a, b, i in meta.insn_rows if i == 0]
    if not any(a == s and b >= e for a, b in rows0):
        return NoMatch("position", "not at the start of guest insn 0 / insn 0 too short")
    for a, b, i in meta.insn_rows:
        if i != 0 and a < b and a < e and s < b:
            return NoMatch("position", f"overlaps insn {i}")
    for a, b, what in meta.busy:
        if a < e and s < b:
            return NoMatch("position", f"overlaps {what} [{a:#x},{b:#x})")
    # 2. bytes -----------------------------------------------------------------------
    blk = code[s:e]
    for k in range(LOGREG_LEN):
        if k not in _HOLE_BYTES and blk[k] != CANON[k]:
            return NoMatch("bytes", f"byte +{k:#x} is {blk[k]:#04x}, want {CANON[k]:#04x}")
    # 6. field -----------------------------------------------------------------------
    if blk[FIELD_HOLE] != vf[0]:
        return NoMatch("field", f"value load disp {blk[FIELD_HOLE]:#x} is not regs[15] "
                                f"({vf[0]:#x})")
    # 3. metadata --------------------------------------------------------------------
    targets = {role: base + s + end + _rel32(blk, h) for h, end, role in RIP_HOLES}
    inside = [x for x in meta.sites if s <= x.off < e or s <= x.field_off < e]
    by_off: dict[int, list[LogSite]] = {}
    for x in inside:
        by_off.setdefault(x.off - s, []).append(x)
    if set(by_off) != set(_SITES):
        return NoMatch("metadata", f"sites at {sorted(hex(k) for k in by_off)}")
    for rel, (kind, fo, w) in _SITES.items():
        xs = by_off[rel]
        if len(xs) != 1 or xs[0].kind != kind or xs[0].field_off != s + fo or xs[0].width != w:
            got = [(x.kind, x.field_off, x.width) for x in xs]
            return NoMatch("metadata", f"site at +{rel:#x}: {got}")
    for (h, _end, role), rel in zip(RIP_HOLES, (0x00, 0x14, 0x27)):
        if by_off[rel][0].value != targets[role]:
            return NoMatch("metadata", f"MOVI_CONST +{rel:#x} value {by_off[rel][0].value:#x} != "
                                       f"decoded {targets[role]:#x}")
    if by_off[SPIN_JCC][0].value - meta.site_base != s:
        return NoMatch("metadata", "spin LABEL_JCC does not target the block start")
    if any(s <= m < e for m in meta.marks):
        return NoMatch("metadata", "an OpMark lies inside the block")
    # 4. entry -----------------------------------------------------------------------
    for x in meta.sites:
        if s <= x.off < e or x.kind not in BRANCH_SITE_KINDS:
            continue
        t = x.value - meta.site_base
        if s <= t < e:
            return NoMatch("entry", f"{x.kind} at {x.off:#x} targets +{t - s:#x}")
    # 5. pointers --------------------------------------------------------------------
    idx, cons, buf = targets["idx"], targets["cons"], targets["buf"]
    pi, pc, pb = meta.pointer(idx), meta.pointer(cons), meta.pointer(buf)
    if pi is None or pc is None or pb is None:
        return NoMatch("pointers", "a block pointer is not importer-bound")
    if pi[0] != "abs.plugin_data" or pc[0] != "abs.plugin_data":
        return NoMatch("pointers", f"IDX/CONS classes {pi[0]}/{pc[0]}")
    mod = _module_of(pi[1])
    if mod != _module_of(pc[1]) or mod not in LOGREG_MODULES:
        return NoMatch("pointers", f"IDX/CONS modules {pi[1]} / {pc[1]}")
    # fail closed: only a nameless anonymous mapping; never abs.heap (may hold env), never
    # guest RAM, a module image/.bss, the arena or the prologue
    if not (pb[0] == RING_BASE_CLASS and pb[1].startswith(RING_BASE_ANCHOR)):
        return NoMatch("pointers", f"{RING_BASE_REFUSED}: BUF class {pb[0]} anchor {pb[1]} "
                                   f"(only {RING_BASE_CLASS} {RING_BASE_ANCHOR}...; a ring base "
                                   "that may alias env or guest RAM is never accepted)")
    if len({idx, cons, buf}) != 3 or abs(idx - cons) < 2 or \
            any(buf <= p < buf + RING_EXTENT or p <= buf < p + 2 for p in (idx, cons)):
        return NoMatch("pointers", "IDX/CONS/BUF overlap")
    # 7. triple ----------------------------------------------------------------------
    if expect is not None and (idx, cons, buf) != tuple(expect):
        return NoMatch("triple", f"({idx:#x},{cons:#x},{buf:#x}) != expected")
    canon = bytearray(blk)
    parts = []
    for h, _end, role in RIP_HOLES:
        canon[h:h + 4] = b"\0\0\0\0"
        parts.append(targets[role].to_bytes(8, "little"))
    digest = hashlib.sha256(bytes(canon) + b"".join(parts)).hexdigest()
    return LogBlock(s, idx, cons, buf, vf[0], "sha256:" + digest, mod, pb[0])


def looks_like_logreg(meta: LogMeta | None) -> bool:
    """A unit worth counting in ``logreg_unmatched``: a backward LABEL site (a loop)."""
    if meta is None:
        return False
    return any(x.kind in ("LABEL_JCC", "LABEL_JMP") and x.value - meta.site_base <= x.off
               for x in meta.sites)


def meta_of_unit(u: Any, layout: Any, code_address: int) -> LogMeta | None:
    """:class:`LogMeta` of a captured :class:`~scopt.model.unit.TBUnit` (None: no metadata)."""
    if getattr(u, "meta_status", "ok") != "ok":
        return None
    from ..formats.enums import OpMarkKind, SiteKind

    base = code_address + u.tc_offset
    sites = tuple(LogSite(int(x.insn_off), SiteKind(x.kind).name, int(x.value),
                          int(x.field_off), int(x.width)) for x in u.sites)
    marks = tuple(sorted(int(m.host_off) for m in u.opmarks))
    lmarks = sorted((m for m in u.opmarks if m.kind in (OpMarkKind.QEMU_LD, OpMarkKind.QEMU_ST)),
                    key=lambda m: m.host_off)
    by_ord = {l.op_ordinal: l for l in u.ldst}
    busy: list[tuple[int, int, str]] = []
    for ordn, m in enumerate(lmarks):
        l = by_ord.get(ordn)
        if l is not None:
            busy.append((int(m.host_off), int(l.raddr_off), "ldst_fast"))
    for l in u.ldst:
        busy.append((int(l.slow_start), int(l.slow_end), "ldst_slow"))
    if u.pool_start is not None:
        busy.append((int(u.pool_start), len(u.code), "pool"))
    stub = getattr(u, "stub_start", None)
    if stub is not None:
        busy.append((int(stub), max(len(u.code), int(stub) + 1), "guard_stub"))
    ptrs = []
    for p in u.bindings.unchecked_host_pointers:
        if 0 <= p.site < len(u.sites):
            ptrs.append((int(u.sites[p.site].value), str(p.cls), str(p.anchor)))
    vf = None
    if layout is not None and layout.has_field("regs[15]"):
        f = layout.field("regs[15]")
        vf = (int(f.off), int(f.size))
    rows = tuple(sorted((int(i.host_start), int(i.host_end), int(i.index)) for i in u.insns))
    return LogMeta(base, int(u.insn0_start), rows, sites, marks, tuple(busy), tuple(ptrs), vf)


def snapshot_triples(units: Iterable[Any], layout: Any, code_address: int
                     ) -> dict[tuple[int, int, int], list[str]]:
    """Rule 7 over a snapshot: matched ``(IDX, CONS, BUF)`` triple -> unit ids."""
    out: dict[tuple[int, int, int], list[str]] = {}
    for u in units:
        m = meta_of_unit(u, layout, code_address)
        if m is None:
            continue
        lb = match_logreg(u.code, code_address + u.tc_offset, m)
        if lb:
            out.setdefault(lb.triple, []).append(u.uid)
    return out


def blocks_by_uid(units: Iterable[Any], layout: Any, code_address: int
                  ) -> Mapping[str, LogBlock]:
    """uid -> matched block (rules 1-6) of every unit that has one."""
    out: dict[str, LogBlock] = {}
    for u in units:
        m = meta_of_unit(u, layout, code_address)
        if m is None:
            continue
        lb = match_logreg(u.code, code_address + u.tc_offset, m)
        if lb:
            out[u.uid] = lb
    return out


def encode_block(s_abs: int, idx: int, cons: int, buf: int, value_off: int) -> bytes:
    """The canonical 65 bytes placed at absolute address ``s_abs`` (synthetic units, tests)."""
    b = bytearray(CANON)
    for (h, end, _role), tgt in zip(RIP_HOLES, (idx, cons, buf)):
        b[h:h + 4] = (tgt - (s_abs + end)).to_bytes(4, "little", signed=True)
    b[FIELD_HOLE] = value_off
    return bytes(b)


def block_sites(s: int, s_abs: int, idx: int, cons: int, buf: int) -> tuple[LogSite, ...]:
    """The four Tier-0 sites of a block at unit offset ``s`` (synthetic units, tests)."""
    return (LogSite(s, "MOVI_CONST", idx, s + 0x03, 4),
            LogSite(s + 0x14, "MOVI_CONST", cons, s + 0x17, 4),
            LogSite(s + SPIN_JCC, "LABEL_JCC", s_abs, s + 0x23, 1),
            LogSite(s + 0x27, "MOVI_CONST", buf, s + 0x2A, 4))


__all__ += ["blocks_by_uid", "encode_block", "block_sites"]
