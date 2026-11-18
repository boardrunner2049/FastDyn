"""Path enumeration of ``hostsym`` over one :class:`~.program.Program` (design §9.2). WP19.

:func:`explore` symbolically executes a program from host offset 0 under a
precondition ``P`` (env byte offset -> concrete value) and returns every
complete path (budget 256), each a :class:`~.state.State` whose ``events``
are the observable trace and whose ``cond`` is the list of symbolic branch
outcomes (the path condition; concrete branches are followed, not recorded).

Abstractions (§9.2):

* **LDST block k** (from its fast-path start): one ``guest`` event
  ``(k, oi, ld/st, address, store value)`` with the env snapshot (compared at
  S5), a fresh ``("gld", k, oi, addr, seq)`` load result, every register the
  fast or slow path writes (except the load destination) and the flags
  havocked, env bytes other than the TCG globals and ``neg.can_do_io``
  havocked (``hv``), then ``pc := raddr``. The block's bytes are not executed; :mod:`.equiv` checks
  they are identical in both versions (else ``edit_inside_ldst_block``).
* **call** (not in a slow path): a ``call`` event (target, class, the six
  SysV argument registers, env and frame snapshots); caller-saved registers
  and flags havocked, ``rax := ("ret", seq)``; env havocked per the helper
  class (flags-0 / plugin / noreturn: all but ``neg.can_do_io``; unknown
  target: all; ``NO_WG``: all but the globals and ``neg.can_do_io``; lookup:
  all but the globals; ``NO_RWG``: none).
* **can_do_io across a returning access or helper** (WP19 integration): the
  only writers of ``cpu->neg.can_do_io`` are generated code, ``cpu_tb_exec``,
  ``helper_lookup_tb_ptr`` (sets it true), ``cpu_loop_exit`` (longjmps, never
  returns into the TB), ``cpu_reset`` and the vCPU thread start
  (accel/tcg/cpu-exec.c:392,452, cpu-exec-common.c:70, hw/core/cpu-common.c:132,
  tcg-accel-ops-rr.c:195,336, tcg-accel-ops-mttcg.c:82); the MMIO path only
  reads it (cputlb.c:1283 ``io_prepare`` -> ``cpu_io_recompile``, which does
  not return). So an LDST block or a call to a known helper that returns
  leaves it unchanged; only the lookup call (which sets it) and an unknown call
  target havoc it.
* **exits**: registered goto_tb jmp -> ``goto_tb``; a guarded slot jmp ->
  ``guarded_slot``; ``jmp reg`` -> ``goto_ptr``; ``jmp tb_ret`` with
  ``rax = own+3`` -> ``exit_requested``, other ``rax`` -> ``exit_tb``;
  ``jmp epilogue`` -> ``exit_null``; a noreturn call -> ``noreturn``; ``int3``
  -> ``trap``.
* **coverage LOG_REG block** (close-out, design/closeout-logreg.md §A.5; only a
  block :func:`scopt.decode.logreg.match_logreg` matches on the program's own
  bytes): at its start ``S`` the loop head ``[S, S+0x22)`` is executed with the
  kernel's own semantics, except that its two ``movzx`` loads of the ring index
  and the consumer index return fresh ``("logld", S, "idx"|"cons", seq, w)``
  terms (cells shared with the tracer thread: no equality with any other host
  memory term is assumed). The spin ``je`` is neither forked nor executed: the
  loop body has only loads, so one iteration with fresh loads reaches every exit
  state; the path records ``("logreg_spin_exit", S)`` in ``assumes`` (not in
  the signature) and continues at ``S+0x24``. The rest of the block runs
  normally, except the ring store ``mov [r13],r14d``, which becomes one
  ``host_log`` event ``(BUF, value)`` (an ``abs_refs`` entry at BUF, class
  ``abs.logreg_ring``; ``hver += 1``). The index store stays an ordinary
  ``host_store`` at IDX.
* Unsupported (the path ends ``UNSUPPORTED``; :mod:`.equiv` gives
  ``validation_inconclusive``): undecodable bytes, a backward branch (slow-path
  returns are inside the LDST abstraction; the matched spin ``je`` is the only
  other exemption), a jump into the middle of an LDST block or of a LOG_REG
  block (``enter_log_block_middle``), a branch leaving the unit other than the exits above, ``ret``,
  string/locked ops, an ``rbp``/``rsp`` write, an env access through a
  register other than ``rbp`` (env alias), the step budget.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import capstone
from capstone import x86 as X

from ..analysis.effects import call_class
from .program import LdstBlock, Program
from .semantics import Unsupported, apply_cc, cond_of, execute, lea_addr, rd_reg, reg_info
from .state import ENV, FRAME, Event, State, Term, is_const, mentions

__all__ = ["Exploration", "explore", "decode", "block_insns", "MAX_PATHS", "MAX_STEPS",
           "CALLER_SAVED", "pre_bytes", "globals_keep", "cdio_bytes", "havoc_keeps"]

MAX_PATHS = 256
MAX_STEPS = 4096
CALLER_SAVED = ("rax", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11")
ARG_REGS = ("rdi", "rsi", "rdx", "rcx", "r8", "r9")
_FLAGS = ("ZF", "SF", "CF", "OF", "PF", "AF")
# TCG globals of the ARM frontend (G21): regs[0..15] 0x00-0x3f, CF/VF/NF/ZF 0x210-0x21f,
# exclusive_addr/val (EnvLayout gives their offsets when available).
_DEFAULT_GLOBAL_RANGES = ((0x0, 0x40), (0x210, 0x220))


@lru_cache(maxsize=1)
def _md() -> capstone.Cs:
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    return md


_DEC: dict[tuple[bytes, int], Any] = {}


def decode(p: Program, off: int) -> Any | None:
    """The instruction at host offset ``off`` of ``p`` (absolute addresses), or None."""
    k = (p.code[off:off + 15], p.base + off)
    ci = _DEC.get(k)
    if ci is None:
        if len(_DEC) > 200_000:
            _DEC.clear()
        ci = next(_md().disasm(k[0], k[1], 1), None)
        if ci is None:
            return None
        _DEC[k] = ci
    return ci


def block_insns(p: Program, start: int, end: int) -> list[Any] | None:
    out = []
    off = start
    while off < end:
        ci = decode(p, off)
        if ci is None:
            return None
        out.append(ci)
        off += ci.size
    return out if off == end else None


def pre_bytes(pre: Mapping[tuple[int, int], int] | None) -> dict[int, int]:
    """``{(off, width): value}`` -> per-byte concrete values (little endian)."""
    out: dict[int, int] = {}
    for (off, w), v in (pre or {}).items():
        for i in range(w):
            out[off + i] = (v >> (8 * i)) & 0xFF
    return out


def cdio_bytes(layout: Any = None) -> frozenset[int]:
    """Env byte offsets of ``neg.can_do_io`` (G21 default -4, width 1)."""
    if layout is not None and layout.has_field("neg.can_do_io"):
        f = layout.field("neg.can_do_io")
        return frozenset(range(f.off, f.off + f.size))
    return frozenset({-4})


def havoc_keeps(layout: Any = None) -> tuple[Any, Any, Any]:
    """``(globals, ldst, helper)`` keep predicates: the TCG globals (lookup and
    ``NO_WG`` calls); globals + can_do_io (LDST blocks, ``NO_WG`` known helpers);
    can_do_io only (flags-0 / plugin known helpers)."""
    g = globals_keep(layout)
    cd = cdio_bytes(layout)

    def ldst(b: int) -> bool:
        return b in cd or g(b)

    def helper(b: int) -> bool:
        return b in cd
    return g, ldst, helper


def globals_keep(layout: Any = None):
    ranges: list[tuple[int, int]] = []
    if layout is not None:
        from ..analysis.hostops import TCG_GLOBALS
        for n in TCG_GLOBALS:
            if layout.has_field(n):
                f = layout.field(n)
                ranges.append((f.off, f.off + f.size))
    if not ranges:
        ranges = list(_DEFAULT_GLOBAL_RANGES)
    ranges = tuple(sorted(ranges))

    def keep(b: int) -> bool:
        for a, e in ranges:
            if a <= b < e:
                return True
        return False
    return keep


@dataclass
class Exploration:
    uid: str
    paths: list[State] = field(default_factory=list)
    unsupported: list[tuple[str, int]] = field(default_factory=list)
    budget_exhausted: bool = False

    @property
    def complete(self) -> bool:
        return not self.unsupported and not self.budget_exhausted

    def exits(self) -> list[State]:
        return [s for s in self.paths if s.end and s.end[0] == "exit"]


class _Mem:
    """Memory operands of ordinary instructions (env, frame, pool, host memory)."""

    def __init__(self, p: Program, in_stub):
        self.p = p
        self.in_stub = in_stub

    def _rip(self, ci: Any, op: Any) -> int:
        return ci.address + ci.size + op.mem.disp

    def rip_addr(self, st: State, ci: Any, op: Any) -> Term:
        return self.p.norm_addr(self._rip(ci, op))

    def _note_stub(self, st: State, ci: Any) -> None:
        off = ci.address - self.p.base
        if self.in_stub(off):
            st.stub_writes.append(off)

    def read(self, st: State, ci: Any, op: Any, w: int) -> Term:
        mo = op.mem
        if mo.segment != 0:
            raise Unsupported("segment")
        base = ci.reg_name(mo.base) if mo.base else ""
        index = ci.reg_name(mo.index) if mo.index else ""
        if base == "rip":
            a = self._rip(ci, op)
            rel = a - self.p.base
            if 0 <= rel and rel + w <= len(self.p.code):
                return int.from_bytes(self.p.code[rel:rel + w], "little")
            if self.p.struct_base <= a < self.p.own_end:
                raise Unsupported("own_struct_read")
            st.abs_refs.append((ci.address - self.p.base, a, "r", a))
            return ("hmem", st.hver, a, w)
        if base == "rbp" and not index:
            return st.env.read(mo.disp, w)
        if base == "rsp" and not index:
            return st.frame.read(mo.disp, w)
        if "rbp" in (base, index) or "rsp" in (base, index):
            raise Unsupported("env_indexed")
        ea = lea_addr(st, ci, op)
        if mentions(ea, ENV) or mentions(ea, FRAME):
            raise Unsupported("env_alias")
        ea = self.p.norm_addr(ea) if is_const(ea) else ea
        st.abs_refs.append((ci.address - self.p.base, ea, "r", self._base_ptr(st, ci, op)))
        return ("hmem", st.hver, ea, w)

    def _base_ptr(self, st: State, ci: Any, op: Any) -> Term | None:
        """The base register's value: the host pointer the importer bound (movabs/pool)."""
        mo = op.mem
        if not mo.base:
            return None
        return st.regs[reg_info(ci.reg_name(mo.base))[0]]

    def write(self, st: State, ci: Any, op: Any, w: int, v: Term) -> None:
        mo = op.mem
        if mo.segment != 0:
            raise Unsupported("segment")
        base = ci.reg_name(mo.base) if mo.base else ""
        index = ci.reg_name(mo.index) if mo.index else ""
        self._note_stub(st, ci)
        if base == "rip":
            raise Unsupported("rip_store")
        if base == "rbp" and not index:
            st.env.write(mo.disp, w, v)
            return
        if base == "rsp" and not index:
            st.frame.write(mo.disp, w, v)
            return
        if "rbp" in (base, index) or "rsp" in (base, index):
            raise Unsupported("env_indexed")
        ea = lea_addr(st, ci, op)
        if mentions(ea, ENV) or mentions(ea, FRAME):
            raise Unsupported("env_alias")
        ea = self.p.norm_addr(ea) if is_const(ea) else ea
        st.abs_refs.append((ci.address - self.p.base, ea, "w", self._base_ptr(st, ci, op)))
        st.events.append(Event("host_store", "host", (ea, w, v), pc=ci.address - self.p.base))
        st.hver += 1


class _LoadHook:
    """Memory interface for executing an LDST access instruction: the load value."""

    def __init__(self, value: Term):
        self.value = value

    def read(self, st: State, ci: Any, op: Any, w: int) -> Term:
        return self.value

    def write(self, st: State, ci: Any, op: Any, w: int, v: Term) -> None:  # pragma: no cover
        raise Unsupported("ldst_store_hook")

    def rip_addr(self, st, ci, op):  # pragma: no cover
        raise Unsupported("ldst_rip")


@dataclass(frozen=True)
class _BlockInfo:
    fast: tuple[Any, ...]  # fast-path insns before the access
    skip: frozenset[int]  # indices in ``fast`` of the jcc(s) to the slow path
    access: Any  # capstone insn of the guest access (ends at raddr)
    addr_regs: tuple[str, ...]  # base/index register families of the access
    data_src: str | int | None  # store: source register name or immediate
    load_dst: str | None
    clobbers: frozenset[str]  # differ between the fast and the slow path at raddr


_BLOCKS: dict[tuple[str, bytes, int], _BlockInfo | str] = {}


def block_info(p: Program, b: LdstBlock) -> _BlockInfo:
    key = (p.uid, p.code[b.fast_start:b.raddr] + p.code[b.slow_start:b.slow_end], p.base)
    got = _BLOCKS.get(key)
    if got is None:
        try:
            got = _block_info(p, b)
        except Unsupported as e:
            got = e.why
        if len(_BLOCKS) > 50_000:
            _BLOCKS.clear()
        _BLOCKS[key] = got
    if isinstance(got, str):
        raise Unsupported(got)
    return got


def _written(ci: Any) -> set[str]:
    try:
        _, rw = ci.regs_access()
    except capstone.CsError as e:  # pragma: no cover
        raise Unsupported("regs_access") from e
    out = set()
    for r in rw:
        n = ci.reg_name(r)
        if n in ("rflags", "eflags", "flags", "rip"):
            continue
        out.add(reg_info(n)[0])
    return out


def _block_info(p: Program, b: LdstBlock) -> _BlockInfo:
    """Split an LDST block: what runs before the access, what differs at raddr.

    The OpMark starts the block at the op's first host byte, so the fast path
    also holds the register allocator's input loads (``mov r13d,[rbp+0xc]``)
    and constants. Those run before the TLB-miss ``jcc`` and so reach the
    slow path too: a register is clobbered at ``raddr`` only if the fast path
    writes it after the first slow-path ``jcc`` or the slow path writes it.
    """
    fast = block_insns(p, b.fast_start, b.raddr)
    slow = block_insns(p, b.slow_start, b.slow_end)
    if not fast or slow is None:
        raise Unsupported("ldst_block_decode")
    acc = fast[-1]
    mems = [o for o in acc.operands if o.type == X.X86_OP_MEM]
    if len(mems) != 1:
        raise Unsupported("ldst_access_shape")
    mo = mems[0].mem
    slow_abs = p.base + b.slow_start
    skip = frozenset(i for i, ci in enumerate(fast[:-1])
                     if ci.group(capstone.CS_GRP_JUMP) and ci.mnemonic != "jmp" and
                     ci.operands and ci.operands[0].type == X.X86_OP_IMM and
                     ci.operands[0].imm == slow_abs)
    if not skip:
        raise Unsupported("ldst_no_slow_jcc")
    for i, ci in enumerate(fast[:-1]):
        if i not in skip and (ci.group(capstone.CS_GRP_JUMP) or ci.group(capstone.CS_GRP_CALL)):
            raise Unsupported("ldst_fast_control")
    first = min(skip)
    clob: set[str] = set()
    for ci in fast[first + 1:]:
        clob |= _written(ci)
    for ci in slow:
        clob |= _written(ci)
        if ci.group(capstone.CS_GRP_CALL):
            clob |= set(CALLER_SAVED)
    addr_regs = tuple(reg_info(acc.reg_name(r))[0] for r in (mo.base, mo.index) if r)
    data_src: str | int | None = None
    load_dst = None
    ops = acc.operands
    if ops[0].type == X.X86_OP_MEM:
        if len(ops) != 2:
            raise Unsupported("ldst_access_shape")
        src = ops[1]
        if src.type == X.X86_OP_REG:
            data_src = acc.reg_name(src.reg)
        elif src.type == X.X86_OP_IMM:
            data_src = src.imm & ((1 << (8 * ops[0].size)) - 1)
        else:
            raise Unsupported("ldst_access_shape")
    else:
        if ops[0].type != X.X86_OP_REG:
            raise Unsupported("ldst_access_shape")
        load_dst = acc.reg_name(ops[0].reg)
        clob.discard(reg_info(load_dst)[0])
    clob -= {"rbp", "rsp"}
    return _BlockInfo(tuple(fast[:-1]), skip, acc, addr_regs, data_src, load_dst,
                      frozenset(clob))


class _BlockMem(_Mem):
    """Fast-path memory: TLB reads are host reads (not host_abs refs); no host store."""

    def read(self, st: State, ci: Any, op: Any, w: int) -> Term:
        mo = op.mem
        base = ci.reg_name(mo.base) if mo.base else ""
        index = ci.reg_name(mo.index) if mo.index else ""
        if base in ("rip", "rbp", "rsp") or "rbp" in (index,) or "rsp" in (index,):
            return super().read(st, ci, op, w)
        ea = lea_addr(st, ci, op)
        if mentions(ea, ENV) or mentions(ea, FRAME):
            raise Unsupported("env_alias")
        return ("hmem", st.hver, ea, w)

    def write(self, st: State, ci: Any, op: Any, w: int, v: Term) -> None:
        mo = op.mem
        base = ci.reg_name(mo.base) if mo.base else ""
        if base not in ("rbp", "rsp"):
            raise Unsupported("ldst_fast_store")
        super().write(st, ci, op, w, v)


def explore(p: Program, pre: Mapping[tuple[int, int], int] | None = None, *, layout: Any = None,
            max_paths: int = MAX_PATHS, max_steps: int = MAX_STEPS) -> Exploration:
    """Every path of ``p`` from offset 0 (see the module doc)."""
    ex = Exploration(p.uid)
    keeps = havoc_keeps(layout)
    mem = _Mem(p, p.in_stub)
    work: list[tuple[int, State]] = [(0, State.initial(pre_bytes(pre)))]
    while work:
        pc, st = work.pop()
        while True:
            st.steps += 1
            if st.steps > max_steps:
                _unsup(ex, st, "step_budget", pc)
                break
            try:
                nxt = _step(p, st, pc, mem, keeps, work)
            except Unsupported as e:
                _unsup(ex, st, e.why, pc)
                break
            if nxt is None:
                break
            pc = nxt
        ex.paths.append(st)
        if len(ex.paths) > max_paths:
            ex.budget_exhausted = True
            break
    return ex


def _unsup(ex: Exploration, st: State, why: str, pc: int) -> None:
    st.unsupported = (why, pc)
    st.end = ("unsupported", why, pc)
    ex.unsupported.append((why, pc))


def _exit(p: Program, st: State, kind: str, data: tuple, pc: int) -> None:
    st.events.append(Event("exit", kind, data, env=st.env.copy(), exit_kind=kind, pc=pc))
    st.end = ("exit", kind, pc)


def _step(p: Program, st: State, pc: int, mem: _Mem, keeps, work) -> int | None:
    b = p.ldst.get(pc)
    if b is not None:
        return _ldst(p, st, b, keeps[1])
    if pc in p.slots:
        ci = decode(p, pc)
        if ci is None or ci.mnemonic != "jmp" or ci.size != 5:
            raise Unsupported("slot_not_jmp_rel32")
        s = p.slots[pc]
        _exit(p, st, "guarded_slot", (s.n, s.kind, s.target_vaddr), pc)
        return None
    if pc in p.goto_tb:
        ci = decode(p, pc)
        if ci is None or ci.mnemonic != "jmp" or ci.size != 5:
            raise Unsupported("goto_tb_not_jmp_rel32")
        _exit(p, st, "goto_tb", (p.goto_tb[pc],), pc)
        return None
    if not p.executable(pc):
        raise Unsupported("pc_outside_code")
    if p.block_of(pc) is not None:
        raise Unsupported("enter_ldst_block_middle")
    lbs = p.logblocks
    if lbs:
        lb = lbs.get(pc)
        if lb is not None:
            return _log_head(p, st, lb)
        for lb in lbs.values():
            if pc == lb.ring_store:
                return _log_store(p, st, lb, pc)
    ci = decode(p, pc)
    if ci is None:
        raise Unsupported("undecodable")
    nxt = pc + ci.size
    m = ci.mnemonic
    if ci.group(capstone.CS_GRP_JUMP):
        return _jump(p, st, ci, pc, nxt, work)
    if ci.group(capstone.CS_GRP_CALL):
        return _call(p, st, ci, pc, nxt, keeps)
    if m == "int3":
        _exit(p, st, "trap", (), pc)
        return None
    if ci.group(capstone.CS_GRP_RET) or ci.group(capstone.CS_GRP_INT) or m in ("hlt", "ud2"):
        raise Unsupported(f"control:{m}")
    if (ci.prefix[0] == 0xF0 or m.startswith("lock ")) and _is_barrier(ci):
        # TCG's memory barrier (tcg_out_mb: lock orl $0,(%rsp)): an ordering event
        if p.in_stub(pc):
            st.stub_writes.append(pc)
        st.events.append(Event("barrier", "mb", (), pc=pc))
        execute(st, ci, mem, allow_lock=True)
        return nxt
    execute(st, ci, mem)
    return nxt


def _is_barrier(ci: Any) -> bool:
    ops = ci.operands
    return (ci.mnemonic in ("or", "lock or") and len(ops) == 2 and ops[0].type == X.X86_OP_MEM and
            ops[0].mem.base == X.X86_REG_RSP and ops[0].mem.index == 0 and
            ops[1].type == X.X86_OP_IMM and ops[1].imm == 0)


def _target(p: Program, ci: Any) -> int:
    op = ci.operands[0]
    if op.type != X.X86_OP_IMM:
        raise Unsupported("indirect_branch")
    return op.imm


def _spin_ok(p: Program, pc: int, off: int) -> bool:
    """The backward-branch exemption: only the matched LOG_REG spin ``je`` to its block start
    (never reached: the head abstraction skips it; kept for defence)."""
    lb = p.logblocks.get(off)
    return lb is not None and pc == lb.spin_jcc


def _no_middle_entry(p: Program, off: int) -> None:
    for lb in p.logblocks.values():
        if lb.start < off < lb.end:
            raise Unsupported("enter_log_block_middle")


def _jump(p: Program, st: State, ci: Any, pc: int, nxt: int, work) -> int | None:
    m = ci.mnemonic
    if m == "jmp":
        op = ci.operands[0]
        if op.type == X.X86_OP_REG:
            _exit(p, st, "goto_ptr", (rd_reg(st, ci.reg_name(op.reg)),), pc)
            return None
        a = _target(p, ci)
        off = a - p.base
        if p.executable(off) and off != pc:
            if off < pc and not (p.in_stub(off) and not p.in_stub(pc)) and \
                    not _spin_ok(p, pc, off):
                raise Unsupported("backward_branch")
            _no_middle_entry(p, off)
            return off
        if p.tb_ret is not None and a == p.tb_ret:
            rax = st.regs["rax"]
            kind = "exit_requested" if rax == ("own", 3) else "exit_tb"
            _exit(p, st, kind, (rax,), pc)
            return None
        if p.epilogue is not None and a == p.epilogue:
            _exit(p, st, "exit_null", (), pc)
            return None
        raise Unsupported("jmp_target")
    if m in ("jrcxz", "jecxz", "jcxz", "loop", "loope", "loopne"):
        raise Unsupported(f"control:{m}")
    a = _target(p, ci)
    off = a - p.base
    if not p.executable(off):
        raise Unsupported("jcc_target")
    if off <= pc and not (p.in_stub(off) and not p.in_stub(pc)) and not _spin_ok(p, pc, off):
        raise Unsupported("backward_branch")
    _no_middle_entry(p, off)
    t = apply_cc(st, cond_of(st, m, "j"))
    if is_const(t):
        return off if t else nxt
    base, val = (t[1], False) if t[0] == "bnot" else (t, True)
    other = st.fork()
    # taken: t is true
    other.cond.append((base, val, pc))
    other.known[base] = val
    work.append((off, other))
    st.cond.append((base, not val, pc))
    st.known[base] = not val
    return nxt


def _call(p: Program, st: State, ci: Any, pc: int, nxt: int, keeps) -> int | None:
    op = ci.operands[0]
    if op.type == X.X86_OP_IMM:
        a = op.imm
    elif op.type == X.X86_OP_MEM and op.mem.base == X.X86_REG_RIP and op.mem.index == 0:
        slot = ci.address + ci.size + op.mem.disp
        rel = slot - p.base
        if not (0 <= rel and rel + 8 <= len(p.code)):
            raise Unsupported("call_slot_outside_unit")
        a = int.from_bytes(p.code[rel:rel + 8], "little")
    else:
        raise Unsupported("indirect_call")
    h = p.helpers.get(a)
    if h is None:
        cls, tgt = "unknown", ("abs", a)
    else:
        cls = call_class(h.name, h.flags)
        tgt = ("helper", h.name if h.name is not None else f"#{h.id}", h.flags)
    if cls == "ldst_slow":  # pragma: no cover - slow paths are never executed
        raise Unsupported("slow_path_call")
    seq = st.seq
    args = tuple(st.regs[r] for r in ARG_REGS)
    if p.in_stub(pc):
        st.stub_writes.append(pc)
    st.events.append(Event("call", cls, (tgt,) + args, env=st.env.copy(), frame=st.frame.copy(),
                           pc=pc))
    for r in CALLER_SAVED:
        st.regs[r] = ("clob", "call", seq, r)
    st.regs["rax"] = ("ret", seq, tgt)
    for f in _FLAGS:
        st.flags[f] = ("clob", "call", seq, f)
    keep_globals, keep_ldst, keep_helper = keeps
    if cls in ("pure", "raising"):
        pass
    elif cls == "lookup":
        st.env.havoc(("call", seq), keep_globals)  # the lookup itself sets can_do_io
        st.hver += 1
    elif h is not None and h.flags is not None and h.flags & 0x2 and cls == "global":
        st.env.havoc(("call", seq), keep_ldst)
        st.hver += 1
    elif h is not None and cls != "noreturn":
        st.env.havoc(("call", seq), keep_helper)
        st.hver += 1
    else:
        st.env.havoc(("call", seq), None)
        st.hver += 1
    if cls == "noreturn":
        _exit(p, st, "noreturn", (tgt,), pc)
        return None
    return nxt


def _ldst(p: Program, st: State, b: LdstBlock, keep_ldst) -> int:
    bi = block_info(p, b)
    bm = _BlockMem(p, p.in_stub)
    for i, ci in enumerate(bi.fast):
        if i in bi.skip:
            continue  # the TLB-miss branch: hit and miss are one atomic access (S5)
        execute(st, ci, bm)
    cands = [r for r in bi.addr_regs
             if not (isinstance(st.regs[r], tuple) and st.regs[r][0] == "hmem")]
    if len(cands) != 1:
        raise Unsupported("ldst_address_register")
    addr = st.regs[cands[0]]
    if b.is_ld != (bi.load_dst is not None):
        raise Unsupported("ldst_direction")
    seq = st.seq
    if bi.load_dst is None:
        val: Term | None = rd_reg(st, bi.data_src) if isinstance(bi.data_src, str) \
            else bi.data_src
    else:
        val = None
    st.events.append(Event("guest", "S5", (b.k, b.oi, "ld" if b.is_ld else "st", addr, val),
                           env=st.env.copy(), pc=b.fast_start))
    if bi.load_dst is not None:
        mo = [o for o in bi.access.operands if o.type == X.X86_OP_MEM][0]
        execute(st, bi.access, _LoadHook(("gld", b.k, b.oi, addr, seq, mo.size)))
    for r in bi.clobbers:
        st.regs[r] = ("clob", "ldst", b.k, seq, r)
    for f in _FLAGS:
        st.flags[f] = ("clob", "ldst", b.k, seq, f)
    st.env.havoc(("ldst", b.k, seq), keep_ldst)
    st.hver += 1
    return b.raddr


# ---------------------------------------------------------------------------
# the coverage LOG_REG block (close-out)
# ---------------------------------------------------------------------------

class _LogHeadMem(_Mem):
    """Memory of the loop head: the ring-index and consumer-index loads are fresh terms."""

    def __init__(self, p: Program, lb: Any, seq: int):
        super().__init__(p, p.in_stub)
        self.lb = lb
        self.seq = seq

    def read(self, st: State, ci: Any, op: Any, w: int) -> Term:
        mo = op.mem
        base = ci.reg_name(mo.base) if mo.base else ""
        if mo.segment != 0 or mo.index != 0 or mo.disp != 0 or base in ("", "rip", "rbp", "rsp"):
            raise Unsupported("logreg_head_load")
        a = st.regs[reg_info(base)[0]]
        lb = self.lb
        which = "idx" if a == lb.idx_abs else "cons" if a == lb.cons_abs else None
        if which is None or w != 2:
            raise Unsupported("logreg_head_load")
        st.abs_refs.append((ci.address - self.p.base, a, "r", a))
        return ("logld", lb.start, which, self.seq, w)

    def write(self, st: State, ci: Any, op: Any, w: int, v: Term) -> None:
        raise Unsupported("logreg_head_store")


def _log_head(p: Program, st: State, lb: Any) -> int:
    """``[S, S+0x22)`` once with fresh loads; the spin ``je`` not taken; continue at S+0x24."""
    hm = _LogHeadMem(p, lb, st.seq)
    off = lb.start
    while off < lb.spin_jcc:
        ci = decode(p, off)
        if ci is None:
            raise Unsupported("undecodable")
        if ci.group(capstone.CS_GRP_JUMP) or ci.group(capstone.CS_GRP_CALL):
            raise Unsupported("logreg_head_control")
        execute(st, ci, hm)
        off += ci.size
    ci = decode(p, lb.spin_jcc)
    if off != lb.spin_jcc or ci is None or ci.mnemonic != "je" or \
            _target(p, ci) != p.base + lb.start or lb.spin_jcc + ci.size != lb.head_end:
        raise Unsupported("logreg_head_shape")
    st.assumes.append(("logreg_spin_exit", lb.start))
    return lb.head_end


def _log_store(p: Program, st: State, lb: Any, pc: int) -> int:
    """The ring store ``mov [P], V32``: one ``host_log`` event ``(BUF, V)``."""
    ci = decode(p, pc)
    if ci is None or ci.mnemonic != "mov" or len(ci.operands) != 2 or \
            ci.operands[0].type != X.X86_OP_MEM or ci.operands[1].type != X.X86_OP_REG or \
            ci.operands[0].size != 4:
        raise Unsupported("logreg_store_shape")
    ea = lea_addr(st, ci, ci.operands[0])
    if mentions(ea, ENV) or mentions(ea, FRAME):
        raise Unsupported("env_alias")
    v = rd_reg(st, ci.reg_name(ci.operands[1].reg))
    if p.in_stub(pc):
        st.stub_writes.append(pc)
    st.abs_refs.append((pc, lb.ring_abs, "w", lb.ring_abs))
    st.events.append(Event("host_log", "coverage", (lb.ring_abs, v), pc=lb.start))
    st.hver += 1
    return pc + ci.size
