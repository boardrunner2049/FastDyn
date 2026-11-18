"""What ``hostsym`` executes: one unit version (bytes + the Tier-0 facts it needs). WP19.

:class:`Program` is deliberately small and neutral so that real units
(:func:`program_of`), patched candidates (:func:`patched`), grown units with
a guard stub (:func:`with_guard`) and hand-assembled synthetic units (tests,
the V18 mutation suite) all go through the same kernel.

Metadata used (B1 §2.3-2.7; nothing comes from decoding guesses):

* ``goto_tb``: host offset of each registered ``goto_tb`` jmp -> slot n
  (``GOTO_TB`` sites);
* ``ldst``: each LDST block — fast-path start (QEMU_LD/ST OpMark), ``raddr``,
  slow path ``[slow_start, slow_end)``, ``oi``, load/store;
* ``helpers``: absolute helper address -> (id, name, TCG flags) (HELPER_CALL
  sites + bindings);
* ``tb_ret``/``epilogue``: absolute addresses of the prologue's ``tb_ret``
  and epilogue (RUN_INFO);
* ``abs_classes``: absolute host pointer value -> importer class
  (``abs.guest_ram``, ``abs.plugin_data``, ...), for the host_abs assertion;
* ``slots``/``stub``: the guarded slots of a grown unit (§4.1) and the
  guard stub's host range;
* ``logmeta``: the Tier-0 metadata of the coverage LOG_REG matcher
  (:class:`scopt.decode.logreg.LogMeta`: sites, insn rows, OpMarks, bound
  pointers, the ``regs[15]`` offset). :attr:`Program.logblocks` re-runs
  :func:`scopt.decode.logreg.match_logreg` on the program's **own current
  bytes** (a patched or placed copy never inherits the original's blocks;
  close-out, design/closeout-logreg.md §A.5).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..decode.logreg import LogBlock, LogMeta, match_logreg, meta_of_unit
from ..formats.enums import OpMarkKind, SiteKind

__all__ = ["HelperRef", "LdstBlock", "GuardAtom", "GSlot", "World", "Program", "program_of",
           "patched", "world_of", "Mu"]

Mu = Callable[[int], int]


@dataclass(frozen=True, slots=True)
class HelperRef:
    id: int | None
    name: str | None
    flags: int | None


@dataclass(frozen=True, slots=True)
class LdstBlock:
    k: int
    oi: int
    is_ld: bool
    fast_start: int
    raddr: int
    slow_start: int
    slow_end: int


@dataclass(frozen=True, slots=True)
class GuardAtom:
    """One guard compare (§4.1, §3.9 item schema): ``env[off:off+width] == value``.

    ``atom`` is g_bp | g_thumb | g_cond | g_flags | g_flags2 | g_pc; ``op`` is
    ``eq`` or ``zero`` (``value`` 0).
    """

    atom: str
    off: int
    width: int
    value: int
    op: str = "eq"
    env_field: str = ""


@dataclass(frozen=True, slots=True)
class GSlot:
    """A guarded slot: the ``jmp rel32`` at ``jmp_off`` is the slot ``n`` exit."""

    n: int
    kind: str  # GENERAL | SPEC
    target_vaddr: int
    atoms: tuple[GuardAtom, ...]


@dataclass(frozen=True)
class World:
    code_address: int
    tb_ret: int | None
    epilogue: int | None
    layout: Any = None  # EnvLayout


@dataclass(frozen=True)
class Program:
    uid: str
    code: bytes
    base: int  # absolute address of code[0]
    struct_base: int  # absolute address of the TB struct
    own_end: int  # absolute end of the unit (struct .. search data)
    insn0_start: int
    pool_start: int | None
    goto_tb: Mapping[int, int] = field(default_factory=dict)
    ldst: Mapping[int, LdstBlock] = field(default_factory=dict)  # fast_start -> block
    helpers: Mapping[int, HelperRef] = field(default_factory=dict)
    tb_ret: int | None = None
    epilogue: int | None = None
    abs_classes: Mapping[int, str] = field(default_factory=dict)
    slots: Mapping[int, GSlot] = field(default_factory=dict)  # jmp offset -> slot
    stub: tuple[int, int] | None = None
    search: bytes = b""
    icount: int = 0
    cflags: int = 0
    exec_end: int | None = None  # decoding stops here (pool start) unless inside the stub
    insn_starts: tuple[int, ...] = ()
    logmeta: LogMeta | None = None

    @property
    def logblocks(self) -> Mapping[int, LogBlock]:
        """Block start -> matched coverage LOG_REG block of the current bytes (rules 1-6;
        the guard stub, when present, is a busy range)."""
        c = self.__dict__.get("_logblocks")
        if c is None:
            c = {}
            m = self.logmeta
            if m is not None:
                if self.stub is not None:
                    m = LogMeta(m.site_base, m.insn0_start, m.insn_rows, m.sites, m.marks,
                                m.busy + ((self.stub[0], self.stub[1], "guard_stub"),),
                                m.pointers, m.value_field)
                lb = match_logreg(self.code, self.base, m)
                if lb:
                    c = {lb.start: lb}
            self.__dict__["_logblocks"] = c
        return c

    def log_block_at(self, off: int) -> LogBlock | None:
        for lb in self.logblocks.values():
            if lb.contains(off):
                return lb
        return None

    def in_stub(self, off: int) -> bool:
        return self.stub is not None and self.stub[0] <= off < self.stub[1]

    def executable(self, off: int) -> bool:
        if self.in_stub(off):
            return True
        end = self.exec_end if self.exec_end is not None else len(self.code)
        return 0 <= off < end

    def norm_addr(self, a: int) -> int | tuple:
        """An absolute address inside the unit becomes ``("own", a - struct_base)``."""
        if self.struct_base <= a < max(self.own_end, self.base + len(self.code)):
            return ("own", a - self.struct_base)
        return a

    def block_of(self, off: int) -> LdstBlock | None:
        for b in self.ldst.values():
            if b.fast_start <= off < b.raddr or b.slow_start <= off < b.slow_end:
                return b
        return None


def world_of(snapshot: Any, layout: Any = None) -> World:
    ri = snapshot.run_info
    ca = snapshot.header.code_address
    return World(ca, None if ri is None else ca + ri.tb_ret_off,
                 None if ri is None else ca + ri.epilogue_off, layout)


def program_of(u: Any, world: World) -> Program:
    """The :class:`Program` of one captured :class:`~scopt.model.unit.TBUnit`."""
    ca = world.code_address
    base = ca + u.tc_offset
    goto_tb = {s.insn_off: int(s.aux) for s in u.sites if s.kind == SiteKind.GOTO_TB}
    marks = sorted((m for m in u.opmarks if m.kind in (OpMarkKind.QEMU_LD, OpMarkKind.QEMU_ST)),
                   key=lambda m: m.host_off)
    by_ord = {l.op_ordinal: (k, l) for k, l in enumerate(u.ldst)}
    ldst: dict[int, LdstBlock] = {}
    for ordn, m in enumerate(marks):
        kl = by_ord.get(ordn)
        if kl is None:
            continue
        k, l = kl
        ldst[m.host_off] = LdstBlock(k, int(l.oi), bool(l.is_ld), m.host_off, l.raddr_off,
                                     l.slow_start, l.slow_end)
    helpers: dict[int, HelperRef] = {}
    by_id = {h.id: h for h in u.bindings.helpers}
    for h in u.bindings.helpers:
        if h.func is not None:
            helpers[h.func] = HelperRef(h.id, h.name, h.flags)
    for s in u.sites:
        if s.kind == SiteKind.HELPER_CALL and s.value not in helpers:
            h = by_id.get(s.aux)
            helpers[s.value] = HelperRef(s.aux, None if h is None else h.name,
                                         None if h is None else h.flags)
    abs_classes: dict[int, str] = {}
    for p in u.bindings.unchecked_host_pointers:
        if 0 <= p.site < len(u.sites):
            abs_classes[u.sites[p.site].value] = p.cls
    return Program(
        uid=u.uid, code=bytes(u.code), base=base, struct_base=ca + u.tb_offset,
        own_end=ca + u.end_offset, insn0_start=u.insn0_start, pool_start=u.pool_start,
        goto_tb=goto_tb, ldst=ldst, helpers=helpers, tb_ret=world.tb_ret,
        epilogue=world.epilogue, abs_classes=abs_classes, search=bytes(u.search),
        icount=u.icount, cflags=u.key.cflags,
        exec_end=u.pool_start if u.pool_start is not None else len(u.code),
        insn_starts=tuple(sorted(i.host_start for i in u.insns)),
        logmeta=meta_of_unit(u, world.layout, ca))


def patched(p: Program, patches: Sequence[tuple[int, bytes]], *, uid: str | None = None,
            **changes: Any) -> Program:
    """A same-length patched copy of ``p`` (B4 nops, CP/BF/DHO rewrites, mutants).

    The metadata is ``p``'s (identity offset map). Each patch must stay inside
    the code bytes; overlapping patches apply in order.
    """
    code = bytearray(p.code)
    for off, new in patches:
        if off < 0 or off + len(new) > len(code):
            raise ValueError(f"patch [{off:#x}, +{len(new)}) outside the code")
        code[off:off + len(new)] = new
    return replace(p, code=bytes(code), uid=uid or p.uid + "'", **changes)


def remap(p: Program, mu: Mu, *, code: bytes, base: int, struct_base: int, own_end: int,
          uid: str | None = None, **changes: Any) -> Program:
    """``p``'s metadata moved through the offset map ``mu`` onto new bytes (clones, μ)."""
    ldst = {mu(o): replace(b, fast_start=mu(b.fast_start), raddr=mu(b.raddr),
                           slow_start=mu(b.slow_start), slow_end=mu(b.slow_end))
            for o, b in p.ldst.items()}
    return replace(p, uid=uid or p.uid + "~", code=code, base=base, struct_base=struct_base,
                   own_end=own_end, insn0_start=mu(p.insn0_start),
                   pool_start=None if p.pool_start is None else mu(p.pool_start),
                   goto_tb={mu(o): n for o, n in p.goto_tb.items()}, ldst=ldst,
                   exec_end=None if p.exec_end is None else mu(p.exec_end),
                   insn_starts=tuple(mu(o) for o in p.insn_starts),
                   logmeta=None if p.logmeta is None else p.logmeta.shifted(mu), **changes)


__all__ += ["remap"]
