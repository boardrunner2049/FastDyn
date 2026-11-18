"""``hostops@1``: the typed, stable operation view of one TB unit (B3/B4 design §8.1). WP17.

The body of a unit is partitioned and typed **only from Tier-0 emission-time
metadata** (``.scm`` insn rows, OpMarks, Sites, Ldst rows; B1 design §2.3-2.7)
plus the EnvLayout of the QEMU binary (§0 G21). Every byte is explained or
the unit is pinned (fail closed).

Blocks (``HostOp.block``)::

    prefix        [0, insn0_start)         entry check, icount store, can_do_io, TB-start plugin calls
    insn          OpMark/insn_start ranges  typed ops of guest insn ``HostOp.insn``
    ldst_fast     [QEMU_LD/ST OpMark, raddr) the inline TLB check + the guest access of LDST k
    ldst_slow     [slow_start, slow_end)   the out-of-line slow path of LDST k
    exit_stub     from an EXIT_TB/GOTO_TB/GOTO_PTR OpMark to its terminating jmp
    pad           [ldst_end, pool_start)   alignment nops
    (pool)        [pool_start, code_size)  data, not decoded

Op kinds (``OpKind``; the design's list plus the kinds needed to type every
byte — recorded as an implementation note in design/B3-B4-design.md)::

    ENV_LOAD(field)  ENV_STORE_IMM(field, imm)  ENV_STORE_REG(field, reg)  ENV_STORE_OTHER(field)
    ENV_ADDR(field)  CONST  COPY  FLAGS  JCC  JMP  CALL(helper)  GOTO_PTR  EXIT_JMP
    GUEST_ACCESS(k, oi)  TLB_READ  HOST_ABS_STORE(abs)  HOST_ABS_LOAD(abs)  POOL_LOAD
    FRAME_LOAD  FRAME_STORE  NOP  OPAQUE

``field`` is an EnvLayout element name (stable across builds of the same
QEMU), never a displacement. An ``rbp`` operand that the EnvLayout cannot
name pins the unit (``unclassified_env_access``).

Every ENV_STORE gets an ``origin`` that explains it from Tier-0 metadata
(the WP17 acceptance "every ENV_STORE site matches a captured sync mark or
explicit store")::

    icount_entry      prefix: neg.icount_decr.u16.low                (translator.c gen_tb_start)
    cdio_prefix       prefix: neg.can_do_io = imm                     (G13)
    condexec_prefix   prefix: condexec_bits = 0 (TB starts inside an IT block; translate.c:7946-7951)
    global_sync       insn: whole-width store of a TCG global         (temp_sync after the defining op)
    cdio_insn         insn: neg.can_do_io = 0/1                       (G13 / translator_io_start)
    plugin_mem_value  insn with a QEMU_LD/ST OpMark: neg.plugin_mem_value_*  (G22)
    plugin_mem_cbs    insn: neg.plugin_mem_cbs                        (plugin-gen.c)
    explicit_st       insn: whole-element store of another CPUARMState field (store_cpu_field)

Anything else (a store in a stub/slow path, a partial global store, a store
to TLB/icount/CPUState fields inside an insn) is ``unexplained`` and pins the
unit (``unexplained_env_store``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any

import capstone
from capstone import x86 as X

from ..decode.logreg import INSNS as _LOGREG_INSNS
from ..decode.logreg import LogBlock, looks_like_logreg, match_logreg, meta_of_unit
from ..decode.logreg import NoMatch as _NoMatch
from ..formats.envlayout_v1 import EnvLayout, FieldRef
from ..formats.enums import OpMarkKind, SiteKind
from .api import AnalysisSpec, ScopeRef

__all__ = [
    "OpKind", "EnvAccess", "HostOp", "Block", "UnitHostOps", "build_hostops", "TCG_GLOBALS",
    "HOSTOPS", "ANALYSES", "REG_FAMILIES", "FLAG_NAMES", "reg_family", "census",
    "STORE_ORIGINS", "is_plugin_helper",
]


class OpKind(StrEnum):
    ENV_LOAD = "ENV_LOAD"
    ENV_STORE_IMM = "ENV_STORE_IMM"
    ENV_STORE_REG = "ENV_STORE_REG"
    ENV_STORE_OTHER = "ENV_STORE_OTHER"
    ENV_ADDR = "ENV_ADDR"
    CONST = "CONST"
    COPY = "COPY"
    FLAGS = "FLAGS"
    JCC = "JCC"
    JMP = "JMP"
    CALL = "CALL"
    GOTO_PTR = "GOTO_PTR"
    EXIT_JMP = "EXIT_JMP"
    GUEST_ACCESS = "GUEST_ACCESS"
    TLB_READ = "TLB_READ"
    HOST_ABS_STORE = "HOST_ABS_STORE"
    HOST_ABS_LOAD = "HOST_ABS_LOAD"
    POOL_LOAD = "POOL_LOAD"
    FRAME_LOAD = "FRAME_LOAD"
    FRAME_STORE = "FRAME_STORE"
    NOP = "NOP"
    OPAQUE = "OPAQUE"


ENV_STORE_KINDS = frozenset({OpKind.ENV_STORE_IMM, OpKind.ENV_STORE_REG, OpKind.ENV_STORE_OTHER})

# ARM frontend TCG globals (target/arm/tcg/translate.c:68-81): stores to these
# are emitted by the register allocator's temp_sync.
TCG_GLOBALS = frozenset([f"regs[{i}]" for i in range(16)] +
                        ["CF", "VF", "NF", "ZF", "exclusive_addr", "exclusive_val"])

STORE_ORIGINS = ("icount_entry", "cdio_prefix", "condexec_prefix", "global_sync", "cdio_insn", "plugin_mem_value",
                 "plugin_mem_cbs", "explicit_st", "unexplained")

_FAMS: dict[str, tuple[str, int]] = {}
for _b, _names in {"rax": "rax eax ax al ah", "rbx": "rbx ebx bx bl bh", "rcx": "rcx ecx cx cl ch",
                   "rdx": "rdx edx dx dl dh", "rsi": "rsi esi si sil", "rdi": "rdi edi di dil",
                   "rbp": "rbp ebp bp bpl", "rsp": "rsp esp sp spl"}.items():
    for _n in _names.split():
        _FAMS[_n] = (_b, 8 if _n.startswith("r") else 4 if _n.startswith("e") else
                     2 if len(_n) == 2 and _n[1] in "xip" else 1)
for _k in range(8, 16):
    for _suf, _w in (("", 8), ("d", 4), ("w", 2), ("b", 1)):
        _FAMS[f"r{_k}{_suf}"] = (f"r{_k}", _w)
_FAMS["rip"] = ("rip", 8)
REG_FAMILIES = ("rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp",
                "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15")
FLAG_NAMES = ("CF", "PF", "AF", "ZF", "SF", "OF", "DF")


def reg_family(name: str) -> tuple[str, int] | None:
    """``"r12d"`` -> ``("r12", 4)``; None for non-GPRs (xmm, eflags, rip handled apart)."""
    return _FAMS.get(name)


def _eflag_masks() -> tuple[dict[str, int], dict[str, int]]:
    reads: dict[str, int] = {}
    writes: dict[str, int] = {}
    for nm in dir(X):
        if not nm.startswith("X86_EFLAGS_"):
            continue
        v = getattr(X, nm)
        parts = nm[len("X86_EFLAGS_"):].split("_", 1)
        if len(parts) != 2:
            continue
        act, flag = parts
        fl = {"CF": "CF", "PF": "PF", "AF": "AF", "ZF": "ZF", "SF": "SF", "OF": "OF",
              "DF": "DF"}.get(flag)
        if fl is None:
            continue
        if act == "TEST":
            reads[fl] = reads.get(fl, 0) | v
        elif act in ("MODIFY", "RESET", "SET", "UNDEFINED"):
            writes[fl] = writes.get(fl, 0) | v
    return reads, writes


_EF_READ, _EF_WRITE = _eflag_masks()


@lru_cache(maxsize=1)
def _md() -> capstone.Cs:
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    return md


@dataclass(frozen=True, slots=True)
class EnvAccess:
    """One ``rbp``-relative operand. ``rw`` is ``r``, ``w`` or ``rw``."""

    field: str  # element name, or "?" when unclassified
    disp: int
    width: int
    rw: str
    exact: bool
    region: str = "env"

    @property
    def classified(self) -> bool:
        return self.field != "?"


@dataclass(frozen=True, slots=True)
class HostOp:
    off: int
    size: int
    text: str
    kind: OpKind
    block: str
    insn: int | None
    ldst: int | None = None
    exit: str | None = None
    env: tuple[EnvAccess, ...] = ()
    imm: int | None = None
    src_reg: str | None = None
    dst_reg: str | None = None
    regs_read: frozenset[str] = frozenset()
    regs_written: frozenset[str] = frozenset()
    regs_partial: frozenset[str] = frozenset()
    flags_read: frozenset[str] = frozenset()
    flags_written: frozenset[str] = frozenset()
    sites: tuple[str, ...] = ()
    site_auxs: tuple[int, ...] = ()  # aux of each site, aligned with ``sites``
    target: int | None = None  # unit-relative branch target (intra), else None
    target_abs: int | None = None
    helper_id: int | None = None
    helper_name: str | None = None
    helper_flags: int | None = None
    abs_addr: int | None = None
    mem_base: str | None = None
    mem_index: str | None = None
    oi: int | None = None
    is_ld: bool | None = None
    origin: str | None = None
    alu: bool = False
    log: int | None = None  # index of the matched LOG_REG block this op belongs to (close-out)
    spin: bool = False  # the block's ring-full spin ``je`` (hcfg: full-view edge only)

    @property
    def end(self) -> int:
        return self.off + self.size

    @property
    def is_env_store(self) -> bool:
        return self.kind in ENV_STORE_KINDS

    @property
    def store_field(self) -> str | None:
        for a in self.env:
            if "w" in a.rw:
                return a.field
        return None

    def site_aux(self, kind: str) -> int | None:
        for k, a in zip(self.sites, self.site_auxs):
            if k == kind:
                return a
        return None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"off": self.off, "size": self.size, "kind": str(self.kind),
                             "block": self.block, "insn": self.insn, "text": self.text}
        if self.sites:
            d["sites"] = [f"{k}/{a}" for k, a in zip(self.sites, self.site_auxs)]
        for k in ("ldst", "exit", "imm", "src_reg", "dst_reg", "target", "helper_id",
                  "helper_name", "helper_flags", "abs_addr", "oi", "is_ld", "origin"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.env:
            d["env"] = [{"field": a.field, "disp": a.disp, "width": a.width, "rw": a.rw,
                         "exact": a.exact} for a in self.env]
        # written only when set: the digest of every unit without a LOG_REG block is unchanged
        if self.log is not None:
            d["log"] = self.log
        if self.spin:
            d["spin"] = True
        return d


@dataclass(frozen=True, slots=True)
class Block:
    kind: str
    start: int
    end: int
    aux: Any = None


@dataclass(frozen=True)
class UnitHostOps:
    uid: str
    status: str  # "ok" | "pinned"
    reasons: tuple[str, ...]
    ops: tuple[HostOp, ...]
    blocks: tuple[Block, ...]
    insn0_start: int
    body_end: int
    ldst_end: int
    pool_start: int | None
    code_size: int
    icount: int
    cflags: int
    envlayout_id: str | None
    unclassified: tuple[int, ...] = ()
    store_origins: Mapping[str, int] = field(default_factory=dict)
    prefix_plugin_calls: tuple[int, ...] = ()
    log_blocks: tuple[LogBlock, ...] = ()  # matched LOG_REG blocks (scopt.decode.logreg)
    logreg_nomatch: str | None = None  # rule of a loop-shaped unit the matcher refused

    def log_block_at(self, off: int) -> LogBlock | None:
        """The matched LOG_REG block containing host offset ``off``."""
        for lb in self.log_blocks:
            if lb.contains(off):
                return lb
        return None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def op_at(self, off: int) -> HostOp | None:
        m = getattr(self, "_by_off", None)
        if m is None:
            m = {o.off: o for o in self.ops}
            object.__setattr__(self, "_by_off", m)
        return m.get(off)

    def index_of(self, off: int) -> int:
        for i, o in enumerate(self.ops):
            if o.off == off:
                return i
        raise KeyError(off)

    def env_stores(self) -> list[HostOp]:
        return [o for o in self.ops if o.is_env_store]

    def content_digest(self) -> str:
        import hashlib
        import json

        h = hashlib.sha256()
        h.update(json.dumps([self.uid, self.status, list(self.reasons), self.envlayout_id],
                            sort_keys=True).encode())
        for o in self.ops:
            h.update(json.dumps(o.to_json(), sort_keys=True).encode())
        return "sha256:" + h.hexdigest()

    def summary(self) -> dict[str, Any]:
        return {"uid": self.uid, "status": self.status, "reasons": list(self.reasons),
                "n_ops": len(self.ops), "unclassified": len(self.unclassified),
                "store_origins": dict(self.store_origins),
                "prefix_plugin_calls": list(self.prefix_plugin_calls)}


def is_plugin_helper(name: str | None) -> bool:
    """Plugin udata callbacks have no name (B1 §2.5.4: name_len 0)."""
    return name is None


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------

class _Pin(Exception):
    pass


_CALLER_SAVED = frozenset({"rax", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11"})


def _replace(op: "HostOp", **kw: Any) -> "HostOp":
    from dataclasses import replace

    return replace(op, **kw)


def _regs(ci: capstone.CsInsn) -> tuple[set[str], set[str], set[str], bool]:
    """(reads, full writes, partial writes, other_regs) as GPR families."""
    try:
        rr, rw = ci.regs_access()
    except capstone.CsError:
        return set(), set(), set(), True
    rd: set[str] = set()
    wr: set[str] = set()
    pw: set[str] = set()
    other = False
    for r in rr:
        n = ci.reg_name(r)
        f = _FAMS.get(n)
        if f is None:
            if n not in ("rflags", "eflags", "flags", "rip"):
                other = True
            continue
        rd.add(f[0])
    for r in rw:
        n = ci.reg_name(r)
        f = _FAMS.get(n)
        if f is None:
            if n not in ("rflags", "eflags", "flags", "rip"):
                other = True
            continue
        (wr if f[1] >= 4 else pw).add(f[0])
    # zero idioms do not read their operand
    if ci.mnemonic in ("xor", "sub", "sbb") and len(ci.operands) == 2:
        a, b = ci.operands
        if a.type == X.X86_OP_REG and b.type == X.X86_OP_REG and a.reg == b.reg:
            if ci.mnemonic != "sbb":
                fam = _FAMS.get(ci.reg_name(a.reg))
                if fam:
                    rd.discard(fam[0])
    # conditional / possibly-unchanged destinations also read their old value
    if (ci.mnemonic.startswith("cmov") or ci.mnemonic in ("bsr", "bsf", "cmpxchg")) and \
            ci.operands and ci.operands[0].type == X.X86_OP_REG:
        fam = _FAMS.get(ci.reg_name(ci.operands[0].reg))
        if fam:
            rd.add(fam[0])
    rd |= pw
    return rd, wr, pw, other


def _flags(ci: capstone.CsInsn) -> tuple[frozenset[str], frozenset[str]]:
    ef = ci.eflags if hasattr(ci, "eflags") else 0
    r = frozenset(f for f, m in _EF_READ.items() if ef & m)
    w = frozenset(f for f, m in _EF_WRITE.items() if ef & m)
    return r, w


def _mem_access(ci: capstone.CsInsn, op: Any) -> str:
    acc = getattr(op, "access", 0)
    r = bool(acc & capstone.CS_AC_READ)
    w = bool(acc & capstone.CS_AC_WRITE)
    if ci.mnemonic == "lea":
        return ""
    if not r and not w:  # capstone sometimes leaves access unset: infer from position
        w = op is ci.operands[0] and ci.mnemonic not in ("cmp", "test", "push", "bt")
        r = not w or not ci.mnemonic.startswith("mov")
    return ("r" if r else "") + ("w" if w else "")


class _Builder:
    def __init__(self, u: Any, layout: EnvLayout | None, code_address: int):
        self.u = u
        self.lay = layout
        self.code_address = code_address
        self.base = code_address + u.tc_offset
        self.reasons: list[str] = []
        self.helpers = {h.id: h for h in u.bindings.helpers}
        self.ring: dict[int, int] = {}  # ring-store offset -> BUF of a matched LOG_REG block

    def pin(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def build(self) -> UnitHostOps:
        u = self.u
        empty = dict(uid=u.uid, blocks=(), insn0_start=u.insn0_start, body_end=u.body_end,
                     ldst_end=u.ldst_end, pool_start=u.pool_start, code_size=len(u.code),
                     icount=u.icount, cflags=u.key.cflags,
                     envlayout_id=None if self.lay is None else self.lay.envlayout_id)
        if u.meta_status != "ok":
            return UnitHostOps(status="pinned", reasons=(u.meta_status,), ops=(), **empty)
        if self.lay is None:
            return UnitHostOps(status="pinned", reasons=("guard_layout_unbound",), ops=(), **empty)
        end = u.pool_start if u.pool_start is not None else len(u.code)
        md = _md()
        cis: list[capstone.CsInsn] = []
        off = 0
        while off < end:
            got = False
            for ci in md.disasm(u.code[off:end], self.base + off):
                cis.append(ci)
                off += ci.size
                got = True
            if off < end:
                self.pin(f"decode_failed@{off:#x}")
                return UnitHostOps(status="pinned", reasons=tuple(self.reasons), ops=(), **empty)
            if not got:
                break
        # the coverage LOG_REG block (close-out; scopt.decode.logreg, fail closed) -----
        lmeta = meta_of_unit(u, self.lay, self.code_address)
        lm = match_logreg(u.code, self.base, lmeta)
        if lm:
            starts = {ci.address - self.base for ci in cis}
            if not all(lm.start + o in starts for o, _n, _t in _LOGREG_INSNS):
                lm = _NoMatch("position", "block not on decoded instruction boundaries")
        nomatch = None
        if lm:
            self.ring = {lm.ring_store: lm.ring_abs}
        # metadata indices ------------------------------------------------------
        sites: dict[int, list[Any]] = {}
        for s in u.sites:
            sites.setdefault(s.insn_off, []).append(s)
        insn_of = self._insn_ranges()
        ldst_marks = sorted((m for m in u.opmarks if m.kind in (OpMarkKind.QEMU_LD,
                                                                 OpMarkKind.QEMU_ST)),
                            key=lambda m: m.host_off)
        ldst_by_ord = {l.op_ordinal: (k, l) for k, l in enumerate(u.ldst)}
        fast: list[tuple[int, int, int, Any]] = []  # (start, raddr, k, Ldst)
        for ordn, m in enumerate(ldst_marks):
            kl = ldst_by_ord.get(ordn)
            if kl is None:
                self.pin(f"ldst_without_slow_path@{m.host_off:#x}")
                continue
            k, l = kl
            fast.append((m.host_off, l.raddr_off, k, l))
        if len(ldst_marks) != len(u.ldst):
            self.pin("ldst_count_mismatch")
        slow = [(l.slow_start, l.slow_end, k, l) for k, l in enumerate(u.ldst)]
        exits = self._exit_stub_starts()
        branch_targets: set[int] = set()
        for s in u.sites:
            if s.kind in (SiteKind.LABEL_JMP, SiteKind.LABEL_JCC, SiteKind.LDST_FAST_JCC,
                          SiteKind.LDST_RET_JMP):
                t = s.value - self.base
                if 0 <= t < len(u.code):
                    branch_targets.add(t)
        # typing ----------------------------------------------------------------
        # registers a slow path may change before it jumps back to raddr
        raddr_kill: dict[int, set[str]] = {}
        for ss, se, k, l in slow:
            kill: set[str] = set()
            for ci in cis:
                o = ci.address - self.base
                if ss <= o < se:
                    _, wr, pw, _ = _regs(ci)
                    kill |= wr | pw
                    if ci.group(capstone.CS_GRP_CALL):
                        kill |= _CALLER_SAVED
            raddr_kill[l.raddr_off] = raddr_kill.get(l.raddr_off, set()) | kill
        ops: list[HostOp] = []
        consts: dict[str, int] = {}
        envptr: dict[str, int] = {}
        stub: tuple[str, int] | None = None
        for ci in cis:
            o = ci.address - self.base
            if o in raddr_kill and o in branch_targets and not any(
                    s.kind in (SiteKind.LABEL_JMP, SiteKind.LABEL_JCC) and s.value - self.base == o
                    for s in u.sites):
                for r in raddr_kill[o]:
                    consts.pop(r, None)
                    envptr.pop(r, None)
            elif o in branch_targets:
                consts.clear()
                envptr.clear()
            if o < u.insn0_start:
                block, insn = "prefix", None
            elif o < u.body_end:
                block, insn = "insn", insn_of(o)
            elif o < u.ldst_end:
                block, insn = "ldst_slow", None
            else:
                block, insn = "pad", None
            ldk = None
            ldst_row = None
            if block == "insn":
                for st, ra, k, l in fast:
                    if st <= o < ra:
                        block, ldk, ldst_row = "ldst_fast", k, l
                        break
            elif block == "ldst_slow":
                for ss, se, k, l in slow:
                    if ss <= o < se:
                        ldk, ldst_row = k, l
                        break
                else:
                    if ci.mnemonic != "nop":
                        self.pin(f"unexplained_gap@{o:#x}")
                    block = "pad"
            if o in exits:
                stub = exits[o]
            ex = None
            if stub is not None and block == "insn":
                block = "exit_stub"
                ex = f"{stub[0]}:{stub[1]}"
            op = self._type(ci, o, block, insn, ldk, ldst_row, ex, sites.get(o, ()),
                            consts, envptr)
            if op.kind == OpKind.ENV_STORE_REG and op.src_reg:
                fam = reg_family(op.src_reg)
                if fam and fam[0] in consts:
                    w = 8 * max(1, op.env[0].width)
                    op = _replace(op, imm=consts[fam[0]] & ((1 << w) - 1))
            ops.append(op)
            if stub is not None and (op.kind in (OpKind.EXIT_JMP, OpKind.GOTO_PTR) or
                                     (op.kind == OpKind.JMP and "GOTO_TB" in op.sites)):
                stub = None
            # value tracking for HOST_ABS classification and env aliases
            self._track(ci, op, consts, envptr)
        if lm:
            ops = [_replace(op, log=0, spin=op.off == lm.spin_jcc) if lm.contains(op.off) else op
                   for op in ops]
        elif looks_like_logreg(lmeta) or any(o.kind == OpKind.HOST_ABS_STORE for o in ops):
            nomatch = lm.rule
        ops_t = tuple(ops)
        self._check_ldst(ops_t, fast)
        self._origins(ops_t, ldst_marks)  # sets HostOp.origin of every ENV_STORE
        unclassified = tuple(o.off for o in ops_t for a in o.env if not a.classified)
        for off in unclassified[:1]:
            self.pin(f"unclassified_env_access@{off:#x}")
        for o in ops_t:
            if o.origin == "unexplained":
                self.pin(f"unexplained_env_store@{o.off:#x}")
                break
        prefix_calls = tuple(o.off for o in ops_t if o.kind == OpKind.CALL and o.block == "prefix")
        blocks = self._blocks(fast, slow, exits, ops_t)
        status = "ok" if not self.reasons else "pinned"
        return UnitHostOps(status=status, reasons=tuple(self.reasons), ops=ops_t,
                           unclassified=unclassified,
                           store_origins=dict(sorted(Counter(o.origin for o in ops_t
                                                             if o.origin).items())),
                           prefix_plugin_calls=prefix_calls, blocks=blocks,
                           log_blocks=(lm,) if lm else (), logreg_nomatch=nomatch,
                           **{k: v for k, v in empty.items() if k != "blocks"})

    # -- metadata helpers ---------------------------------------------------------------
    def _insn_ranges(self):
        rows = sorted((i.host_start, i.host_end, i.index) for i in self.u.insns)

        def insn_of(o: int) -> int | None:
            best = None
            for s, e, idx in rows:
                if s <= o < e:
                    best = idx  # the last non-empty range that contains o
            return best
        return insn_of

    def _exit_stub_starts(self) -> dict[int, tuple[str, int]]:
        u = self.u
        movi = {s.insn_off: s for s in u.sites if s.kind == SiteKind.MOVI_EXIT_TB_PTR}
        out: dict[int, tuple[str, int]] = {}
        for m in u.opmarks:
            if m.kind == OpMarkKind.GOTO_TB:
                out[m.host_off] = ("goto_tb", m.insn_idx)
            elif m.kind == OpMarkKind.GOTO_PTR:
                out[m.host_off] = ("goto_ptr", m.insn_idx)
            elif m.kind == OpMarkKind.EXIT_TB:
                s = movi.get(m.host_off)
                out[m.host_off] = ("exit_tb", -1 if s is None else s.aux)
        return out

    def _blocks(self, fast, slow, exits, ops: Sequence[HostOp]) -> tuple[Block, ...]:
        u = self.u
        bl = [Block("prefix", 0, u.insn0_start)]
        for i in sorted(u.insns, key=lambda r: r.index):
            bl.append(Block("insn", i.host_start, i.host_end, i.index))
        for st, ra, k, _ in fast:
            bl.append(Block("ldst_fast", st, ra, k))
        for ss, se, k, _ in slow:
            bl.append(Block("ldst_slow", ss, se, k))
        cur = None
        for o in ops:
            if o.block == "exit_stub":
                if cur is None or cur[2] != o.exit or cur[1] != o.off:
                    if cur is not None:
                        bl.append(Block("exit_stub", cur[0], cur[1], cur[2]))
                    cur = [o.off, o.end, o.exit]
                else:
                    cur[1] = o.end
            elif cur is not None:
                bl.append(Block("exit_stub", cur[0], cur[1], cur[2]))
                cur = None
        if cur is not None:
            bl.append(Block("exit_stub", cur[0], cur[1], cur[2]))
        if u.pool_start is not None:
            if u.ldst_end < u.pool_start:
                bl.append(Block("pad", u.ldst_end, u.pool_start))
            bl.append(Block("pool", u.pool_start, len(u.code)))
        return tuple(sorted(bl, key=lambda b: (b.start, b.end, b.kind)))

    # -- typing ------------------------------------------------------------------------
    def _type(self, ci, o, block, insn, ldk, ldst_row, ex, sites, consts, envptr) -> HostOp:
        rd, wr, pw, other = _regs(ci)
        fr, fw = _flags(ci)
        m = ci.mnemonic
        text = f"{m} {ci.op_str}".strip()
        kinds = tuple(SiteKind(s.kind).name for s in sites)
        common = dict(off=o, size=ci.size, text=text, block=block, insn=insn, ldst=ldk, exit=ex,
                      regs_read=frozenset(rd), regs_written=frozenset(wr),
                      regs_partial=frozenset(pw), flags_read=fr, flags_written=fw, sites=kinds,
                      site_auxs=tuple(int(s.aux) for s in sites))
        if other and m not in ("nop", "call", "ret") and not m.startswith("j"):
            self.pin(f"unsupported_register_class@{o:#x}")
        if ({"rbp", "rsp"} & (wr | pw)) and m not in ("call",):
            self.pin(f"env_reg_written@{o:#x}")
        if m in ("movs", "movsb", "movsd", "movsq", "movsw", "stos", "stosb", "stosd", "stosq",
                 "cmps", "scas", "lods", "rep movsb", "rep stosb") or m.startswith("rep"):
            self.pin(f"string_op@{o:#x}")
        if m == "nop" or m.startswith("nop"):
            return HostOp(kind=OpKind.NOP, **common)
        ops = ci.operands
        # control flow ----------------------------------------------------------
        if ci.group(capstone.CS_GRP_CALL):
            return self._call(ci, o, sites, common)
        if ci.group(capstone.CS_GRP_JUMP):
            return self._jump(ci, o, sites, common)
        if ci.group(capstone.CS_GRP_RET) or ci.group(capstone.CS_GRP_INT) or m in ("hlt", "ud2"):
            self.pin(f"unexpected_control@{o:#x}")
            return HostOp(kind=OpKind.OPAQUE, **common)
        # memory operands -------------------------------------------------------
        env: list[EnvAccess] = []
        nonenv: list[tuple[Any, str]] = []
        for op in ops:
            if op.type != X.X86_OP_MEM:
                continue
            mo = op.mem
            base = ci.reg_name(mo.base) if mo.base else ""
            index = ci.reg_name(mo.index) if mo.index else ""
            if base == "rbp" and not index:
                rw = _mem_access(ci, op)
                if m == "lea":
                    ref = self.lay.classify(mo.disp, 1)
                    env.append(self._acc(ref, mo.disp, 0, ""))
                    continue
                ref = self.lay.classify(mo.disp, op.size)
                env.append(self._acc(ref, mo.disp, op.size, rw))
            elif base == "rbp" or index == "rbp":
                env.append(EnvAccess("?", mo.disp, op.size, _mem_access(ci, op), False))
            else:
                nonenv.append((op, base))
        if env:
            return self._env_op(ci, o, env, nonenv, common)
        if nonenv:
            return self._mem_op(ci, o, nonenv, block, ldst_row, consts, envptr, common)
        # register-only -----------------------------------------------------------
        if m == "lea":
            return HostOp(kind=OpKind.OPAQUE, alu=True, dst_reg=_dst(ci), **common)
        if m in ("mov", "movabs") and len(ops) == 2:
            a, b = ops
            if b.type == X.X86_OP_IMM:
                return HostOp(kind=OpKind.CONST, imm=b.imm & ((1 << 64) - 1), dst_reg=_dst(ci),
                              **common)
            if b.type == X.X86_OP_REG:
                return HostOp(kind=OpKind.COPY, dst_reg=_dst(ci), src_reg=ci.reg_name(b.reg),
                              **common)
        if m == "xor" and len(ops) == 2 and ops[0].type == ops[1].type == X.X86_OP_REG and \
                ops[0].reg == ops[1].reg:
            return HostOp(kind=OpKind.CONST, imm=0, dst_reg=_dst(ci), **common)
        if m in ("cmp", "test", "bt") and not wr and not pw:
            return HostOp(kind=OpKind.FLAGS, **common)
        return HostOp(kind=OpKind.OPAQUE, alu=True, dst_reg=_dst(ci), **common)

    def _acc(self, ref: FieldRef | None, disp: int, width: int, rw: str) -> EnvAccess:
        if ref is None:
            return EnvAccess("?", disp, width, rw, False)
        return EnvAccess(ref.name, disp, width, rw, ref.exact, ref.region)

    def _env_op(self, ci, o, env, nonenv, common) -> HostOp:
        m = ci.mnemonic
        if nonenv:
            self.pin(f"two_memory_operands@{o:#x}")
        a = env[0]
        if m == "lea":
            return HostOp(kind=OpKind.ENV_ADDR, env=tuple(env), dst_reg=_dst(ci), **common)
        if "w" in a.rw:
            ops = ci.operands
            if m == "mov" and len(ops) == 2 and ops[1].type == X.X86_OP_IMM:
                return HostOp(kind=OpKind.ENV_STORE_IMM, env=tuple(env),
                              imm=ops[1].imm & ((1 << (8 * max(1, a.width))) - 1), **common)
            if m == "mov" and len(ops) == 2 and ops[1].type == X.X86_OP_REG:
                return HostOp(kind=OpKind.ENV_STORE_REG, env=tuple(env),
                              src_reg=ci.reg_name(ops[1].reg), **common)
            return HostOp(kind=OpKind.ENV_STORE_OTHER, env=tuple(env), alu=True, **common)
        alu = m not in ("mov", "movzx", "movsx", "movsxd")
        return HostOp(kind=OpKind.ENV_LOAD, env=tuple(env), dst_reg=_dst(ci), alu=alu, **common)

    def _mem_op(self, ci, o, nonenv, block, ldst_row, consts, envptr, common) -> HostOp:
        op, base = nonenv[0]
        mo = op.mem
        index = ci.reg_name(mo.index) if mo.index else ""
        rw = _mem_access(ci, op)
        m = ci.mnemonic
        if m == "lea":
            if base == "rip":
                tgt = ci.address + ci.size + mo.disp
                return HostOp(kind=OpKind.CONST, imm=tgt & ((1 << 64) - 1), dst_reg=_dst(ci),
                              abs_addr=tgt, **common)
            if base in envptr or index in envptr:
                self.pin(f"env_alias_access@{o:#x}")
            return HostOp(kind=OpKind.OPAQUE, alu=True, dst_reg=_dst(ci), **common)
        if base == "rip":
            tgt = ci.address + ci.size + mo.disp
            rel = tgt - self.base
            u = self.u
            if "w" in rw:
                self.pin(f"rip_store@{o:#x}")
                return HostOp(kind=OpKind.HOST_ABS_STORE, abs_addr=tgt, **common)
            if u.pool_start is not None and u.pool_start <= rel < len(u.code):
                return HostOp(kind=OpKind.POOL_LOAD, abs_addr=tgt, dst_reg=_dst(ci),
                              imm=int.from_bytes(u.code[rel:rel + 8], "little"), **common)
            return HostOp(kind=OpKind.HOST_ABS_LOAD, abs_addr=tgt, dst_reg=_dst(ci), **common)
        if base == "rsp":
            k = OpKind.FRAME_STORE if "w" in rw else OpKind.FRAME_LOAD
            return HostOp(kind=k, mem_base=base, mem_index=index or None, **common)
        if base in envptr or index in envptr:
            self.pin(f"env_alias_access@{o:#x}")
        if block == "ldst_fast" and ldst_row is not None:
            if ci.address + ci.size - self.base == ldst_row.raddr_off:
                return HostOp(kind=OpKind.GUEST_ACCESS, oi=ldst_row.oi, is_ld=bool(ldst_row.is_ld),
                              mem_base=base or None, mem_index=index or None, **common)
            if "w" in rw:
                self.pin(f"unexplained_host_store@{o:#x}")
            return HostOp(kind=OpKind.TLB_READ, mem_base=base or None, mem_index=index or None,
                          **common)
        # the ring store of a matched LOG_REG block: [BUF + sext(idx)] (the add r64,r64
        # pointer the constant tracking cannot type), explained by the matcher
        if o in self.ring and "w" in rw and block == "insn":
            return HostOp(kind=OpKind.HOST_ABS_STORE, abs_addr=self.ring[o], mem_base=base or None,
                          mem_index=index or None, **common)
        # outside the fast path: host memory through an absolute pointer
        if base and base in consts and not index:
            addr = (consts[base] + mo.disp) & ((1 << 64) - 1)
        elif base and base in consts and index:
            addr = consts[base]  # base of an indexed access (ring buffers); index unknown
        else:
            addr = None
        if "w" in rw:
            if addr is None:
                self.pin(f"unexplained_host_store@{o:#x}")
            return HostOp(kind=OpKind.HOST_ABS_STORE, abs_addr=addr, mem_base=base or None,
                          mem_index=index or None, **common)
        if addr is None and block not in ("ldst_slow",):
            self.pin(f"unexplained_host_load@{o:#x}")
        return HostOp(kind=OpKind.HOST_ABS_LOAD, abs_addr=addr, mem_base=base or None,
                      mem_index=index or None, dst_reg=_dst(ci), **common)

    def _site_target(self, s: Any) -> tuple[int | None, int]:
        t = s.value - self.base
        return (t if 0 <= t < len(self.u.code) else None), s.value

    def _call(self, ci, o, sites, common) -> HostOp:
        hs = [s for s in sites if s.kind in (SiteKind.HELPER_CALL, SiteKind.LDST_HELPER_CALL)]
        if not hs:
            self.pin(f"unexplained_branch@{o:#x}")
            return HostOp(kind=OpKind.CALL, **common)
        s = hs[0]
        if s.kind == SiteKind.HELPER_CALL:
            h = self.helpers.get(s.aux)
            return HostOp(kind=OpKind.CALL, helper_id=s.aux, target_abs=s.value,
                          helper_name=None if h is None else h.name,
                          helper_flags=None if h is None else h.flags, **common)
        return HostOp(kind=OpKind.CALL, helper_id=None, helper_name="<ldst_slow_path>",
                      target_abs=s.value, **common)

    def _jump(self, ci, o, sites, common) -> HostOp:
        kinds = {SiteKind(s.kind) for s in sites}
        ops = ci.operands
        if len(ops) == 1 and ops[0].type == X.X86_OP_REG:
            if SiteKind.GOTO_PTR not in kinds:
                self.pin(f"unexplained_branch@{o:#x}")
            return HostOp(kind=OpKind.GOTO_PTR, **common)
        for s in sites:
            k = SiteKind(s.kind)
            if k in (SiteKind.EXIT_TB_RET_JMP, SiteKind.EXIT_TB_EPI_JMP):
                return HostOp(kind=OpKind.EXIT_JMP, target_abs=s.value, **common)
            if k == SiteKind.GOTO_TB:
                return HostOp(kind=OpKind.JMP, target=None, target_abs=None, **common)
            if k in (SiteKind.LABEL_JMP, SiteKind.LABEL_JCC, SiteKind.LDST_FAST_JCC,
                     SiteKind.LDST_RET_JMP):
                t, ta = self._site_target(s)
                if t is None:
                    self.pin(f"branch_out_of_unit@{o:#x}")
                kd = OpKind.JMP if ci.mnemonic == "jmp" else OpKind.JCC
                return HostOp(kind=kd, target=t, target_abs=ta, **common)
        self.pin(f"unexplained_branch@{o:#x}")
        return HostOp(kind=OpKind.JMP if ci.mnemonic == "jmp" else OpKind.JCC, **common)

    def _track(self, ci, op: HostOp, consts: dict[str, int], envptr: dict[str, int]) -> None:
        if op.kind == OpKind.CALL:
            for r in _CALLER_SAVED:
                consts.pop(r, None)
                envptr.pop(r, None)
            return
        written = set(op.regs_written) | set(op.regs_partial)
        dst = reg_family(op.dst_reg)[0] if op.dst_reg and reg_family(op.dst_reg) else None
        for r in written:
            consts.pop(r, None)
            envptr.pop(r, None)
        if dst is None:
            return
        if op.kind in (OpKind.CONST, OpKind.POOL_LOAD) and op.imm is not None and \
                dst not in op.regs_partial:
            consts[dst] = op.imm
        elif op.kind == OpKind.COPY and op.src_reg:
            sf = reg_family(op.src_reg)
            if sf and sf[0] == "rbp":
                envptr[dst] = 0
            elif sf and sf[0] in consts:
                consts[dst] = consts[sf[0]]
            elif sf and sf[0] in envptr:
                envptr[dst] = envptr[sf[0]]
        elif op.kind == OpKind.ENV_ADDR:
            envptr[dst] = op.env[0].disp if op.env else 0

    def _check_ldst(self, ops: Sequence[HostOp], fast) -> None:
        have = {o.ldst for o in ops if o.kind == OpKind.GUEST_ACCESS}
        for _, ra, k, _l in fast:
            if k not in have:
                self.pin(f"ldst_access_not_found@{ra:#x}")

    def _origins(self, ops: Sequence[HostOp], ldst_marks) -> tuple[HostOp, ...]:
        insn_with_ldst = {m.insn_idx for m in ldst_marks}
        out = []
        for i, o in enumerate(ops):
            if not o.is_env_store:
                continue
            acc = next((a for a in o.env if "w" in a.rw), None)
            org = _origin(o, acc, insn_with_ldst)
            object.__setattr__(o, "origin", org)
            out.append(o)
        return tuple(out)


def _dst(ci: capstone.CsInsn) -> str | None:
    if ci.operands and ci.operands[0].type == X.X86_OP_REG:
        return ci.reg_name(ci.operands[0].reg)
    return None


def _origin(o: HostOp, acc: EnvAccess | None, insn_with_ldst: set[int]) -> str:
    if acc is None or not acc.classified:
        return "unexplained"
    f = acc.field
    if o.block == "prefix":
        if f == "neg.icount_decr.u16.low" and acc.exact:
            return "icount_entry"
        if f == "neg.can_do_io" and o.kind in (OpKind.ENV_STORE_IMM, OpKind.ENV_STORE_REG) and \
                o.imm in (0, 1):
            return "cdio_prefix"
        if f == "condexec_bits" and o.kind == OpKind.ENV_STORE_IMM and o.imm == 0:
            return "condexec_prefix"
        return "unexplained"
    if o.block not in ("insn", "ldst_fast"):
        return "unexplained"
    if f in TCG_GLOBALS:
        return "global_sync" if acc.exact and o.kind != OpKind.ENV_STORE_OTHER else "unexplained"
    if f == "neg.can_do_io":
        return "cdio_insn" if o.kind in (OpKind.ENV_STORE_IMM, OpKind.ENV_STORE_REG) and \
            o.imm in (0, 1) else "unexplained"
    if f in ("neg.plugin_mem_value_low", "neg.plugin_mem_value_high"):
        return "plugin_mem_value" if o.insn in insn_with_ldst else "unexplained"
    if f == "neg.plugin_mem_cbs":
        return "plugin_mem_cbs"
    if acc.region == "env" and acc.exact:
        return "explicit_st"
    return "unexplained"


def build_hostops(unit: Any, layout: EnvLayout | None, code_address: int) -> UnitHostOps:
    """HostOps of one TBUnit (no store; for analyses use ``hostops@1``)."""
    return _Builder(unit, layout, code_address).build()


# ---------------------------------------------------------------------------
# analysis + census
# ---------------------------------------------------------------------------

def _compute(ctx: Any, scope: ScopeRef) -> UnitHostOps:
    lay = ctx.analysis("envlayout", ScopeRef("snapshot", ctx.snapshot_id)).value
    u = ctx.unit(scope.id)
    return build_hostops(u, lay.get("layout"), ctx.header.code_address)


HOSTOPS = AnalysisSpec(name="hostops", version=1, scope="unit", requires=(("envlayout", 1),),
                       inputs=frozenset({"code", "meta"}), compute=_compute,
                       result_schema=UnitHostOps)
ANALYSES = (HOSTOPS,)


def census(units: Iterable[Any], layout: EnvLayout | None, code_address: int,
           *, keep: bool = False) -> dict[str, Any]:
    """HostOps over every unit: pin reasons, store origins, unclassified rbp operands.

    The WP17 acceptance numbers (``unexplained_env_stores`` must be 0;
    ``unclassified_rbp_operands`` is a reported outcome, target 0).
    """
    by_status: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    origins: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    fields_stored: Counter[str] = Counter()
    n_env_ops = n_unclassified = n_prefix_calls = 0
    n_log = 0
    log_unmatched: Counter[str] = Counter()
    triples: Counter[tuple[int, int, int]] = Counter()
    examples: dict[str, list[str]] = {}
    built = []
    n_units = 0
    for u in units:
        n_units += 1
        h = build_hostops(u, layout, code_address)
        if keep:
            built.append(h)
        by_status[h.status] += 1
        for r in h.reasons:
            key = r.split("@", 1)[0]
            reasons[key] += 1
            examples.setdefault(key, [])
            if len(examples[key]) < 5:
                examples[key].append(f"{u.uid}:{r}")
        for o in h.ops:
            kinds[str(o.kind)] += 1
            if o.env:
                n_env_ops += 1
            n_unclassified += sum(1 for a in o.env if not a.classified)
            if o.origin:
                origins[o.origin] += 1
                fields_stored[o.store_field or "?"] += 1
        n_prefix_calls += len(h.prefix_plugin_calls)
        n_log += len(h.log_blocks)
        for lb in h.log_blocks:
            triples[lb.triple] += 1
        if h.logreg_nomatch is not None:
            log_unmatched[h.logreg_nomatch] += 1
    out = {
        "units": n_units, "status": dict(by_status), "pin_reasons": dict(reasons.most_common()),
        "pin_examples": examples,
        "env_store_origins": {k: origins.get(k, 0) for k in STORE_ORIGINS},
        "env_stores": sum(origins.values()),
        "unexplained_env_stores": origins.get("unexplained", 0),
        "rbp_operands": n_env_ops, "unclassified_rbp_operands": n_unclassified,
        "prefix_plugin_calls": n_prefix_calls,
        "op_kinds": dict(kinds.most_common()),
        "fields_stored": dict(fields_stored.most_common()),
        "envlayout_id": None if layout is None else layout.envlayout_id,
        # close-out: LOG_REG blocks matched, loop-shaped units refused (rule), and the
        # (IDX, CONS, BUF) triples; more than one triple violates rule 7 (validate_b3 V15
        # then refuses every edit of a unit with a block)
        "log_blocks_matched": n_log,
        "logreg_unmatched": dict(log_unmatched.most_common()),
        "logreg_triples": {",".join(hex(x) for x in k): v for k, v in sorted(triples.items())},
        "logreg_triple_consistent": len(triples) <= 1,
    }
    if keep:
        out["_built"] = built
    return out


def tier0_prefix_plugin_calls(units: Iterable[Any]) -> list[tuple[str, int, int | None]]:
    """Tier-0 evidence of TB-start plugin callbacks (design §7.2, V20 cross-check).

    A TB-start callback is emitted after the entry check and before the first
    ``insn_start`` (G30), i.e. inside ``[0, insn0_start)``. Returns
    ``(uid, host_off, helper_id)`` for every CALL OpMark there whose helper
    is unnamed (a plugin callback) or unknown. Metadata only (no decoding).
    """
    out = []
    for u in units:
        if getattr(u, "meta_status", "ok") != "ok":
            continue
        names = {h.id: h.name for h in u.bindings.helpers}
        for m in u.opmarks:
            if m.kind != OpMarkKind.CALL or m.host_off >= u.insn0_start:
                continue
            if m.helper_id is None or names.get(m.helper_id, None) is None:
                out.append((u.uid, m.host_off, m.helper_id))
    return out


__all__ += ["tier0_prefix_plugin_calls"]
