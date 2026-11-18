"""The §9.3 mutation suite of ``hostsym`` (must be rejected 100 %). WP19.

Two sources of mutants:

* **synthetic units** (:func:`synthetic_suite`), hand-assembled with
  :mod:`.asm` in the shapes TCG emits for the ARM frontend (icount prefix,
  env syncs, LDST blocks with slow paths, lookup tail, goto_tb exits,
  FastDyn-style host stores). They need no snapshot, so V18 runs them for
  every scopt version.
* **real units** (:func:`real_mutants`), same-length mutations of captured
  units of a snapshot (compute, fault, ...): shapes found by scanning
  HostOps, so the suite is also exercised on real TCG output.

Every mutant is checked with the kernel exactly as a pass candidate would be
(V12 :func:`~.equiv.check_equiv`, the guarded-chain rule
:func:`~.equiv.check_guarded_chain`, V14 :func:`~.equiv.certify_fact`); it is
*rejected* when the verdict is not ``equivalent`` (or the fact is not
certified with the claimed value). The design wants a static *failure*
(``validation_mismatch`` or a more specific failed code) — the result
records the verdict so an ``inconclusive`` rejection is visible.

The guard-stub mutants (atom drops, guard-before-effect, falsified-fact
link, shared variant) need a guard emitter. The integration acceptance uses
WP18's emitter; :func:`grow_guarded` here is WP19's own reference emitter
of the §4.1 template, used so that the guarded-chain rule is exercised before
WP18 lands (``needs_emitter: true`` in the results).

**Static-only classification** (§9.3, R6): every mutant names its dynamic
detector (the fixture of §9.5) or, for ``static_only`` mutants, why no
admitted execution can distinguish it.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .asm import Asm, nop
from .equiv import Verdict, certify_fact, check_equiv, check_guarded_chain
from .program import GSlot, GuardAtom, HelperRef, LdstBlock, Program, patched

__all__ = ["MutantResult", "SuiteResult", "synthetic_suite", "real_mutants", "SynthUnit",
           "grow_guarded", "DETECTORS", "STATIC_ONLY", "SynthContract", "CODE_ADDRESS",
           "ENV_ABS", "synthetic_units", "UNIT_TB", "MINI_LAYOUT", "GuardSpec", "unit_logreg",
           "LOG_IDX", "LOG_CONS", "LOG_BUF"]

CODE_ADDRESS = 0x7FFF00000000
TB_RET = CODE_ADDRESS + 0x18
EPILOGUE = CODE_ADDRESS + 0x10
UNIT_TB = CODE_ADDRESS + 0x100000
TB_SIZE = 152
LOOKUP = 0x555555A00000
MSR = 0x555555A01000
PLUGIN_CB = 0x7FFFF7A00000
LD_HELPER = 0x555555A02000
ST_HELPER = 0x555555A03000
RAM_PTR = 0x7FFFE0000000
RING = 0x7FFFD0000000
ENV_ABS = 0x555556000000
_ENV_LO, _ENV_HI = -11200, 0x3C00  # sizeof(CPUState), a bound above sizeof(CPUARMState)

# G21 offsets (bound to the qint/qint-v6 EnvLayout; the synthetic units use them literally)
OFF_R = {i: 4 * i for i in range(16)}
OFF_CF, OFF_VF, OFF_NF, OFF_ZF = 0x210, 0x214, 0x218, 0x21C
OFF_THUMB, OFF_FLAGS, OFF_FLAGS2, OFF_COND = 0x14D, 0x150, 0x158, 0x228
OFF_BP, OFF_ICOUNT, OFF_CDIO, OFF_PMV = -0x2980, -0x8, -0x4, -0x18

HELPERS = {LOOKUP: HelperRef(0, "lookup_tb_ptr", 6), MSR: HelperRef(1, "v7m_msr", 0),
           PLUGIN_CB: HelperRef(2, None, 0)}
ABS_CLASSES = {RAM_PTR: "abs.guest_ram", RING: "abs.plugin_data"}

# §9.3: mutant -> dynamic detector (§9.5 fixture), or None for static-only
DETECTORS: dict[str, str | None] = {
    "fold_flip": "F-B3-1 (O4)",
    "fold_without_fact": "F-B3-6 / F-B3-11 (f5 and unseen paths)",
    "fact_falsified": "F-B3-1 falsified-fact control",
    "fact_claim_mismatch": "F-B3-1 falsified-fact control",
    "guest_store_delete": "F-B4-7",
    "update_mem_delete": "F-B4-7",
    "log_reg_delete": "F-B4-7",
    "flag_store_before_ldst": "F-B4-2",
    "flag_store_before_helper": "F-B4-11 (close-out; F-B4-1 has no instance)",
    "brcond_reload": "F-B4-1 (T-B4-2b)",
    "cdio_last_insn_direct_chain": "F-B4-9",
    "nop_dead_result_ldst": "F-B3-3b",
    "it_block_d4": "F-B4-5",
    "icount_immediate": "F-B3-7 / F-B4-3 (O2b)",
    "search_row": "F-B3-9 / F-B4-2 (fault report PC)",
    "host_abs_in_env": "F-B4-7",
    "host_abs_unbound": "F-B4-7",
    "drop_g_bp": "F-B3-13(d)",
    "drop_g_thumb": "F-B3-22",
    "drop_g_pc": "F-B3-6, F-B3-15",
    "drop_g_cond": None,
    "drop_g_flags": None,
    "drop_g_flags2": None,
    "guard_before_effect": "F-B3-6",
    "guard_stub_store": "F-B3-1",
    "falsified_fact_link": "F-B3-1 falsified-fact control",
    "shared_variant_suffix": "F-B3-20",
    # close-out (design/closeout-logreg.md §A.8): the coverage LOG_REG block L1-L8; the ring is
    # guest-observable coverage output, compared by O6 on the coverage fixtures
    "logreg_ring_store_nop": "F-B3-23 / F-B4-10 (O6)",
    "logreg_index_store_nop": "F-B3-23 / F-B4-10 (O6)",
    "logreg_index_add8": "F-B3-23 / F-B4-10 (O6)",
    "logreg_spin_nop": None,
    "logreg_spin_retarget": None,
    "logreg_buf_retarget": "F-B3-23 / F-B4-10 (O6)",
    "logreg_ring_aliases_env": "F-B4-10 (O1/O6: env bytes overwritten)",
    "logreg_value_field": "F-B3-23 / F-B4-10 (O6)",
    "logreg_d1_store_hoisted": "F-B3-23 / F-B4-10 (O6)",
}
STATIC_ONLY: dict[str, str] = {
    "drop_g_cond": "a return is last in any IT block, so condexec is 0 at every return-exit "
                   "slot (U-COND); F-B3-22b is not built: no non-RW_CFI virtual writes xPSR "
                   "(WP24 implementation note 1)",
    "drop_g_flags": "cached hflags cannot change between R's key match and R's exit on "
                    "cortex-m3 (MSR/CPS end the TB, SCS writes are last-insn MMIO, gdb/plugin "
                    "writes of M system registers are no-ops, G33)",
    "drop_g_flags2": "as drop_g_flags",
    "logreg_spin_nop": "the spin only runs while the coverage ring is full (the tracer thread "
                       "is 16384 records behind); no admitted run reaches that state "
                       "deterministically, so only the static byte-freeze can see it",
    "logreg_spin_retarget": "as logreg_spin_nop (the retargeted spin also only runs on a full "
                            "ring)",
}


class SynthContract:
    """A minimal observation contract for synthetic tests (same rules as obs-contract@1)."""

    def __init__(self, tb_start_cb_possible: bool, mem_get_value_outside_mem_cb: bool | None =
                 False):
        from ..analysis.obs_contract import ObsContract

        self.c = ObsContract(None, tb_start_cb_possible, mem_get_value_outside_mem_cb,
                             {"neg.can_do_io": OFF_CDIO})
        self.offsets = self.c.offsets
        self.tb_start_cb_possible = tb_start_cb_possible

    def observable(self, fld: str, event: str, *, exit_kind: str | None = None) -> bool:
        return self.c.observable(fld, event, exit_kind=exit_kind)


class _MiniLayout:
    """Byte -> field names for the synthetic units (the contract's listed fields)."""

    _F = {**{f"regs[{i}]": (4 * i, 4) for i in range(16)}, "CF": (OFF_CF, 4), "VF": (OFF_VF, 4),
          "NF": (OFF_NF, 4), "ZF": (OFF_ZF, 4), "thumb": (OFF_THUMB, 1),
          "hflags.flags": (OFF_FLAGS, 4), "hflags.flags2": (OFF_FLAGS2, 8),
          "condexec_bits": (OFF_COND, 4), "neg.can_do_io": (OFF_CDIO, 1),
          "neg.icount_decr.u32": (OFF_ICOUNT, 4), "neg.plugin_mem_value_low": (OFF_PMV, 8),
          "cpu.breakpoints.tqh_first": (OFF_BP, 8)}

    def __init__(self) -> None:
        from ..formats.envlayout_v1 import FieldRef

        self._by = {}
        for n, (o, w) in self._F.items():
            for i in range(w):
                self._by[o + i] = FieldRef(n, "env", o, w, i, 1, w == 1)

    def classify(self, disp: int, width: int):
        return self._by.get(disp)

    def has_field(self, n: str) -> bool:
        return n in self._F

    def field(self, n: str):
        from ..formats.envlayout_v1 import FieldRef

        o, w = self._F[n]
        return FieldRef(n, "env", o, w)

    def offset(self, n: str) -> int:
        return self._F[n][0]


MINI_LAYOUT = _MiniLayout()


# ---------------------------------------------------------------------------
# synthetic unit builder
# ---------------------------------------------------------------------------

@dataclass
class GuardSpec:
    """Reference emitter input (§4.1): targets ``[(n, kind, Vc)]``, key constants, mutations."""

    targets: tuple[tuple[int, str, int], ...] = ((0, "SPEC", 0x24F0),)
    kf: int = 0x110
    kf2: int = 0x800400
    drop: frozenset[str] = frozenset()
    before_effect: bool = False
    stub_store: bool = False


class SynthUnit:
    """Builds one TB-shaped synthetic :class:`Program` (see the module doc)."""

    def __init__(self, uid: str, *, icount: int = 1, tb: int = UNIT_TB):
        self.uid = uid
        self.tb = tb
        self.a = Asm(base=tb + TB_SIZE)
        self.goto: dict[int, int] = {}
        self.ldst: dict[int, LdstBlock] = {}
        self.slow: list[Callable[[], None]] = []
        self.pool: dict[str, int] = {"p_lookup": LOOKUP, "p_msr": MSR, "p_cb": PLUGIN_CB,
                                     "p_ld": LD_HELPER, "p_st": ST_HELPER}
        self.icount = icount
        self.insn0 = 0
        self.at: dict[str, tuple[int, int]] = {}
        self.guard: GuardSpec | None = None
        self.slots: dict[int, GSlot] = {}
        self.stub: tuple[int, int] | None = None
        self.tail_body: list[Callable[[], None]] = []

    # -- recording ---------------------------------------------------------------------
    def rec(self, name: str, fn: Callable[[], Any]) -> tuple[int, int]:
        s = self.a.pc
        fn()
        self.at[name] = (s, self.a.pc - s)
        return self.at[name]

    @property
    def struct(self) -> int:
        return self.tb

    # -- TB pieces ---------------------------------------------------------------------
    def prefix(self, *, cdio0: bool = True) -> None:
        a = self.a
        a.mov_r_env("rbx", OFF_ICOUNT)
        self.rec("icount_add", lambda: a.alu_r_imm("add", "rbx", -self.icount))
        a.alu_r_r("test", "rbx", "rbx")
        a.jcc("l", "exitreq")
        a.mov_env_r(OFF_ICOUNT, "rbx", w=2)
        if cdio0:
            self.rec("cdio0", lambda: a.mov_env_imm(OFF_CDIO, 0, w=1))
        self.insn0 = a.pc

    def goto_tb(self, n: int, pc_value: int) -> None:
        a = self.a
        a.mov_env_imm(OFF_R[15], pc_value)
        self.goto[a.jmp0()] = n
        a.lea_rip("rax", target=self.struct + n)
        a.jmp(target=TB_RET)

    def lookup_tail(self) -> None:
        a = self.a
        self.rec("tail", lambda: (a.mov_rdi_rbp(), a.call_pool("p_lookup"), a.jmp_reg("rax")))

    def call_helper(self, pool_label: str) -> None:
        a = self.a
        a.mov_rdi_rbp()
        a.call_pool(pool_label)

    def ldst_block(self, k: int, *, is_ld: bool, addr: str = "r12", data: str = "r13", oi: int = 0x21,
             slow_extra: bytes = b"", nop_access: bool = False) -> None:
        a = self.a
        fs = a.pc
        a.alu_r_r("mov", "rdi", addr, w=8)
        a.shr_imm("rdi", 5)
        a.and_r_env("rdi", -0x110)
        a.add_r64_env("rdi", -0x108)
        a.lea_r_m8("rsi", addr, 3)
        a.alu_r_imm("and", "rsi", 0xFFFFFC00)
        a.cmp_r_m("rsi", "rdi", 0 if is_ld else 8)
        a.jcc("ne", f"slow{k}")
        a.mov_r64_m("rdi", "rdi", 0x18)
        s = a.pc
        if is_ld:
            a.mov_r_bi(data, addr, "rdi")
        else:
            a.mov_bi_r(addr, "rdi", data)
        self.at[f"access{k}"] = (s, a.pc - s)
        if nop_access:
            a.buf[s:a.pc] = nop(a.pc - s)
        raddr = a.label(f"raddr{k}")
        box: dict[str, int] = {}

        def emit() -> None:
            box["s"] = a.label(f"slow{k}")
            if slow_extra:
                a.raw(slow_extra)
            a.alu_r_r("mov", "rsi", addr)
            if not is_ld:
                a.alu_r_r("mov", "rdx", data)
            a.mov_rdi_rbp()
            a.mov_r_imm("rdx" if is_ld else "rcx", oi)
            a.lea_rip("rcx" if is_ld else "r8", label=f"raddr{k}")
            a.call_pool("p_ld" if is_ld else "p_st")
            if is_ld:
                a.alu_r_r("mov", data, "rax")
            a.jmp(label=f"raddr{k}")
            self.ldst[fs] = LdstBlock(k, oi, is_ld, fs, raddr, box["s"], a.pc)
        self.slow.append(emit)

    def build(self, *, search: bytes = b"\x11\x00\x02\x05", guard: GuardSpec | None = None
              ) -> Program:
        a = self.a
        a.label("exitreq")
        a.lea_rip("rax", target=self.struct + 3)
        a.jmp(target=TB_RET)
        for emit in self.slow:
            emit()
        a.align(8, b"\xcc")
        pool_start = a.pc
        for lab, v in self.pool.items():
            a.label(lab)
            a.quad(v)
        if guard is not None:
            self._stub(guard)
        code = a.finish()
        end = self.tb + TB_SIZE + len(code) + len(search)
        return Program(uid=self.uid, code=code, base=a.base, struct_base=self.tb, own_end=end,
                       insn0_start=self.insn0, pool_start=pool_start, goto_tb=dict(self.goto),
                       ldst=dict(self.ldst), helpers=dict(HELPERS), tb_ret=TB_RET,
                       epilogue=EPILOGUE, abs_classes=dict(ABS_CLASSES),
                       slots=dict(self.slots), stub=self.stub, search=search,
                       icount=self.icount, cflags=0xFF022000, exec_end=pool_start)

    # -- reference guard emitter (§4.1) ------------------------------------------------
    def tail_or_jump(self, guard: GuardSpec | None, effects: Callable[[], None]) -> None:
        """R's end: ``effects`` (R15/thumb stores) then the tail, or ``jmp S; int3 x6``."""
        a = self.a
        self.guard = guard
        if guard is None:
            effects()
            self.lookup_tail()
            return
        if guard.before_effect:
            self.tail_body.append(effects)  # the effects run only on the F path (mutant)
            a.jmp(label="S")
            a.int3(6)
            return
        effects()
        self.rec("tail", lambda: (a.jmp(label="S"), a.int3(6)))

    def _stub(self, g: GuardSpec) -> None:
        a = self.a
        s0 = a.label("S")
        if g.stub_store:
            a.mov_env_imm(OFF_R[1], 0x55)
        if "g_bp" not in g.drop:
            a.cmp_env_imm(OFF_BP, 0, w=8)
            a.jcc("ne", "F")
        if "g_thumb" not in g.drop:
            a.cmp_env_imm(OFF_THUMB, 1, w=1)
            a.jcc("ne", "F")
        if "g_cond" not in g.drop:
            a.cmp_env_imm(OFF_COND, 0)
            a.jcc("ne", "F")
        if "g_flags" not in g.drop:
            a.cmp_env_imm(OFF_FLAGS, g.kf)
            a.jcc("ne", "F")
        if "g_flags2" not in g.drop:
            a.cmp_env_imm(OFF_FLAGS2, g.kf2, w=8)
            a.jcc("ne", "F")
        for n, _kind, vc in g.targets:
            if "g_pc" not in g.drop:
                a.cmp_env_imm(OFF_R[15], vc)
                a.jcc("e", f"L{n}")
            else:
                a.jmp(label=f"L{n}")
        a.jmp(label="F")
        for n, kind, vc in g.targets:
            a.label(f"L{n}")
            while (a.base + a.pc + 1) % 4:
                a.nop(1)
            atoms = (GuardAtom("g_bp", OFF_BP, 8, 0, "zero", "cpu.breakpoints.tqh_first"),
                     GuardAtom("g_thumb", OFF_THUMB, 1, 1, "eq", "thumb"),
                     GuardAtom("g_cond", OFF_COND, 4, 0, "zero", "condexec_bits"),
                     GuardAtom("g_flags", OFF_FLAGS, 4, g.kf, "eq", "hflags.flags"),
                     GuardAtom("g_flags2", OFF_FLAGS2, 8, g.kf2, "eq", "hflags.flags2"),
                     GuardAtom("g_pc", OFF_R[15], 4, vc, "eq", "regs[15]"))
            self.slots[a.jmp0()] = GSlot(n, kind, vc, atoms)
            if kind == "GENERAL":
                a.lea_rip("rax", target=self.struct + n)
                a.jmp(target=TB_RET)
            else:
                a.jmp(label="F")
        a.label("F")
        for fn in self.tail_body:
            fn()
        a.mov_rdi_rbp()
        a.call_pool("p_lookup")
        a.jmp_reg("rax")
        self.stub = (s0, a.pc)


# ---------------------------------------------------------------------------
# the synthetic units
# ---------------------------------------------------------------------------

def unit_cont() -> tuple[Program, SynthUnit]:
    """C: ``cbz r0`` continuation (the 0x24f0 shape)."""
    u = SynthUnit("synth:cont", icount=1)
    a = u.a
    u.prefix()
    u.rec("ld_r0", lambda: a.mov_r_env("rbx", OFF_R[0]))
    u.rec("test", lambda: a.alu_r_r("test", "rbx", "rbx"))
    u.rec("jcc", lambda: a.jcc("ne", "nz"))
    a.mov_env_imm(OFF_CDIO, 1, w=1)
    u.goto_tb(0, 0x2516)
    a.label("nz")
    a.mov_env_imm(OFF_CDIO, 1, w=1)
    u.goto_tb(1, 0x24F2)
    return u.build(), u


def unit_ret(*, store_r0: int | None = 0, guard: GuardSpec | None = None,
             uid: str = "synth:ret") -> tuple[Program, SynthUnit]:
    """R: ``movs r0,#k; bx lr`` (the 0x1ede shape); ``store_r0=None`` is the suffix unit."""
    u = SynthUnit(uid, icount=2 if store_r0 is not None else 1)
    a = u.a
    u.prefix()
    if store_r0 is not None:
        u.rec("r0", lambda: a.mov_env_imm(OFF_R[0], store_r0))
    a.mov_env_imm(OFF_CDIO, 1, w=1)

    def effects() -> None:
        a.mov_r_env("rbx", OFF_R[14])
        a.alu_r_r("mov", "r12", "rbx")
        a.alu_r_imm("and", "r12", 0xFFFFFFFE)
        a.mov_env_r(OFF_R[15], "r12")
        a.alu_r_imm("and", "rbx", 1)
        a.mov_env_r(OFF_THUMB, "rbx", w=1)
    u.tail_or_jump(guard, effects)
    return u.build(guard=guard), u


def unit_mem(*, update_ptr: int = RAM_PTR, tb: int = UNIT_TB) -> tuple[Program, SynthUnit]:
    """Flag syncs around a guest store, FastDyn-style host stores and a flags-0 helper.
    ``tb`` places the unit (a relocated clone is the same unit at another ``tb``)."""
    u = SynthUnit("synth:mem" if tb == UNIT_TB else f"synth:mem@{tb:#x}", icount=4, tb=tb)
    a = u.a
    u.prefix()
    a.mov_r_env("rbx", OFF_R[1])
    u.rec("nf_before_ldst", lambda: a.mov_env_r(OFF_NF, "rbx"))
    a.mov_r_env("r12", OFF_R[2])
    a.mov_r_env("r13", OFF_R[3])
    u.ldst_block(0, is_ld=False)
    a.mov_env_r(OFF_NF, "r13")  # NF overwritten: the first store is seen only at S5
    u.rec("update_mem", lambda: (a.movabs("rax", update_ptr), a.mov_m_r("rax", 0x10, "r13")))
    u.rec("log_reg", lambda: (a.movabs("rcx", RING), a.mov_m_r("rcx", 0x0, "rbx")))
    u.rec("zf_before_helper", lambda: a.mov_env_imm(OFF_ZF, 0))
    u.call_helper("p_msr")
    a.mov_env_imm(OFF_ZF, 1)
    a.mov_env_imm(OFF_CDIO, 1, w=1)
    u.goto_tb(0, 0x3000)
    return u.build(), u


def unit_brcond() -> tuple[Program, SynthUnit]:
    """A D1 sync store at a brcond whose global is reloaded in the next BB (T-B4-2b)."""
    u = SynthUnit("synth:brcond", icount=3)
    a = u.a
    u.prefix()
    a.mov_r_env("rbx", OFF_R[2])
    a.alu_r_imm("add", "rbx", 1)
    u.rec("sync_r2", lambda: a.mov_env_r(OFF_R[2], "rbx"))
    a.alu_r_imm("cmp", "rbx", 5)
    a.jcc("e", "L")
    a.mov_env_imm(OFF_R[4], 9)
    a.label("L")
    a.mov_r_env("r12", OFF_R[2])  # TCG reloads the global after set_label
    a.alu_r_imm("add", "r12", 2)
    a.mov_env_r(OFF_R[3], "r12")
    a.mov_env_imm(OFF_R[2], 0)  # r2 dead at every exit: only the reload sees the sync
    a.mov_env_imm(OFF_CDIO, 1, w=1)
    u.goto_tb(0, 0x4000)
    return u.build(), u


def unit_cdio() -> tuple[Program, SynthUnit]:
    """A multi-insn TB whose last insn stores can_do_io = 1 before a goto_tb exit."""
    u = SynthUnit("synth:cdio", icount=2)
    a = u.a
    u.prefix()
    a.mov_env_imm(OFF_R[1], 7)
    u.rec("cdio1", lambda: a.mov_env_imm(OFF_CDIO, 1, w=1))
    u.goto_tb(0, 0x5000)
    return u.build(), u


def unit_it(*, d4: bool = False) -> tuple[Program, SynthUnit]:
    """``it eq; moveq r1,#5; ldr r2,[r3]`` (F-B4-5 shape); ``d4`` = the IT-block D4 mutant:
    the conditional store moved out of the main path into the LDST slow path."""
    u = SynthUnit("synth:it" + ("~d4" if d4 else ""), icount=3)
    a = u.a
    u.prefix()
    a.mov_r_env("rbx", OFF_ZF)
    a.alu_r_r("test", "rbx", "rbx")
    a.jcc("ne", "skip")
    store = Asm(base=0)
    store.mov_env_imm(OFF_R[1], 5)
    if d4:
        a.nop(len(store.buf))
    else:
        u.rec("moveq", lambda: a.raw(bytes(store.buf)))
    a.label("skip")
    a.mov_r_env("r12", OFF_R[3])
    u.ldst_block(0, is_ld=True, slow_extra=bytes(store.buf) if d4 else b"")
    a.mov_env_r(OFF_R[2], "r13")
    a.mov_env_imm(OFF_R[1], 7)
    a.mov_env_imm(OFF_CDIO, 1, w=1)
    u.goto_tb(0, 0x6000)
    return u.build(), u


def unit_deadld(*, nop_access: bool = False) -> tuple[Program, SynthUnit]:
    """R with a load whose result is dead (F-B3-3b): ``ldr r0,[r1]`` then r0 := 0."""
    u = SynthUnit("synth:deadld", icount=3)
    a = u.a
    u.prefix()
    a.mov_r_env("r12", OFF_R[1])
    u.ldst_block(0, is_ld=True, nop_access=nop_access)
    a.mov_env_imm(OFF_R[0], 0)
    a.mov_env_imm(OFF_CDIO, 1, w=1)
    u.goto_tb(0, 0x7000)
    return u.build(), u


# the coverage LOG_REG block (close-out): the gate binary's pointers (rel32-reachable from
# CODE_ADDRESS, like the real process layout)
LOG_IDX = 0x7FFF63F6E570
LOG_CONS = 0x7FFF43DA945C
LOG_BUF = 0x7FFF68000B70


def unit_logreg(*, uid: str = "synth:logreg", tb: int = UNIT_TB) -> tuple[Program, SynthUnit]:
    """A coverage TB: prefix, the 65-byte LOG_REG block at ``insn0_start`` (FastDyn's
    form, scopt.decode.logreg), then ``r1 += 1`` and a goto_tb exit. ``at["log_block"]``
    is the block; ``at["r1"]`` the D1 store after it."""
    from ..decode.logreg import LOGREG_LEN, LogMeta, block_sites, encode_block

    u = SynthUnit(uid, icount=2, tb=tb)
    a = u.a
    u.prefix()
    s0 = a.pc
    a.raw(encode_block(a.base + s0, LOG_IDX, LOG_CONS, LOG_BUF, OFF_R[15]))
    u.at["log_block"] = (s0, LOGREG_LEN)
    a.mov_r_env("rbx", OFF_R[1])
    a.alu_r_imm("add", "rbx", 1)
    u.rec("r1", lambda: a.mov_env_r(OFF_R[1], "rbx"))
    i1 = a.pc
    a.mov_env_imm(OFF_CDIO, 1, w=1)
    i1_end = a.pc
    u.goto_tb(0, 0x8000)
    p = u.build()
    meta = LogMeta(
        site_base=p.base, insn0_start=s0, insn_rows=((s0, i1, 0), (i1, i1_end, 1)),
        sites=block_sites(s0, p.base + s0, LOG_IDX, LOG_CONS, LOG_BUF), marks=(),
        busy=(((p.pool_start, len(p.code), "pool"),) if p.pool_start is not None else ()),
        pointers=((LOG_IDX, "abs.plugin_data", "libfastdyn.so+0x24f6e570"),
                  (LOG_CONS, "abs.plugin_data", "libfastdyn.so+0x4da945c"),
                  (LOG_BUF, "abs.other", "anon@0x7fff68000000+0xb70")),
        value_field=(OFF_R[15], 4))
    classes = {**p.abs_classes, LOG_IDX: "abs.plugin_data", LOG_CONS: "abs.plugin_data",
               LOG_BUF: "abs.other"}
    return replace(p, logmeta=meta, abs_classes=classes), u


def synthetic_units() -> dict[str, Program]:
    return {"cont": unit_cont()[0], "ret": unit_ret()[0], "mem": unit_mem()[0],
            "brcond": unit_brcond()[0], "cdio": unit_cdio()[0], "it": unit_it()[0],
            "deadld": unit_deadld()[0]}


def grow_guarded(spec: GuardSpec | None = None, **kw: Any) -> tuple[Program, Program]:
    """(R, R'): the synthetic return unit and its grown version (reference emitter)."""
    r, _ = unit_ret(**kw)
    rp, _ = unit_ret(guard=spec or GuardSpec(), uid="synth:ret'", **kw)
    return r, rp


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

@dataclass
class MutantResult:
    name: str
    unit: str
    rejected: bool
    verdict: str
    reason: str | None
    detail: str
    needs_emitter: bool = False
    source: str = "synthetic"

    @property
    def detector(self) -> str | None:
        return DETECTORS.get(self.name)

    def to_json(self) -> dict[str, Any]:
        d = {"name": self.name, "unit": self.unit, "rejected": self.rejected,
             "verdict": self.verdict, "reason": self.reason, "detail": self.detail[:300],
             "source": self.source, "needs_emitter": self.needs_emitter,
             "dynamic_detector": self.detector}
        if self.name in STATIC_ONLY:
            d["static_only"] = STATIC_ONLY[self.name]
        return d


@dataclass
class SuiteResult:
    results: list[MutantResult] = field(default_factory=list)
    controls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def all_rejected(self) -> bool:
        return bool(self.results) and all(r.rejected for r in self.results)

    @property
    def controls_ok(self) -> bool:
        return all(c["ok"] for c in self.controls)

    def to_json(self) -> dict[str, Any]:
        n = len(self.results)
        rej = sum(r.rejected for r in self.results)
        by: dict[str, int] = {}
        for r in self.results:
            by[r.verdict] = by.get(r.verdict, 0) + 1
        return {"mutants": n, "rejected": rej, "all_rejected": self.all_rejected,
                "by_verdict": by, "controls_ok": self.controls_ok,
                "results": [r.to_json() for r in self.results], "controls": self.controls}


def _res(name: str, unit: str, v: Verdict, **kw: Any) -> MutantResult:
    return MutantResult(name, unit, v.rejected, v.status, v.reason, v.detail, **kw)


def _fact_res(name: str, unit: str, cert: Any, claim: int, **kw: Any) -> MutantResult:
    ok = cert.certified and cert.value == claim
    return MutantResult(name, unit, not ok, "equivalent" if ok else "mismatch",
                        None if ok else ("fact_derivation_mismatch" if cert.certified else
                                         "fact_not_constant"),
                        f"claim {claim}; certifier {cert.status} {cert.value}: {cert.detail}",
                        **kw)


def _jcc_to_jmp(code: bytes, off: int, base: int) -> bytes:
    """``jcc rel32`` at ``off`` -> ``jmp rel32`` to the same target + 1-byte nop."""
    rel = struct.unpack_from("<i", code, off + 2)[0]
    tgt = off + 6 + rel
    return b"\xe9" + struct.pack("<i", tgt - (off + 5)) + b"\x90"


def synthetic_suite() -> SuiteResult:
    """Every unit-stage mutant on the synthetic units, plus positive controls."""
    out = SuiteResult()
    tsc = SynthContract(True)
    no_tsc = SynthContract(False)
    lay = MINI_LAYOUT
    env_abs = (ENV_ABS + _ENV_LO, ENV_ABS + _ENV_HI)

    def eq(o, n, pre=None, c=tsc, **kw):
        return check_equiv(o, n, pre, None, c, layout=lay, env_abs=kw.get("env_abs", env_abs))

    def control(name: str, v: Any, want: bool = True) -> None:
        ok = (v.ok if hasattr(v, "ok") else bool(v)) == want
        out.controls.append({"name": name, "ok": ok,
                             "verdict": getattr(v, "status", str(v)),
                             "detail": getattr(v, "detail", "")[:200]})

    # fold / fact (C, the 0x24f0 shape)
    c, cu = unit_cont()
    P0 = {(OFF_R[0], 4): 0}
    P1 = {(OFF_R[0], 4): 1}
    ld, test, jcc = cu.at["ld_r0"], cu.at["test"], cu.at["jcc"]
    fold = patched(c, [(ld[0], nop(ld[1])), (test[0], nop(test[1])), (jcc[0], nop(jcc[1]))])
    control("fold_under_fact_equivalent", eq(c, fold, P0))
    out.results.append(_res("fold_without_fact", c.uid, eq(c, fold, None)))
    out.results.append(_res("fact_falsified", c.uid, eq(c, fold, P1)))
    flip = patched(c, [(jcc[0], _jcc_to_jmp(c.code, jcc[0], c.base))])
    out.results.append(_res("fold_flip", c.uid, eq(c, flip, P0)))
    # fact certification (R, the 0x1ede shape)
    r, _ = unit_ret()
    cert = certify_fact(r, None, (OFF_R[0], 4), layout=lay)
    control("fact_certified_r0_eq_0", cert.certified and cert.value == 0)
    out.results.append(_fact_res("fact_claim_mismatch", r.uid, cert, 1))
    # guest / host stores, flag syncs, helper
    m, mu = unit_mem()
    control("identity_mem_equivalent", eq(m, m))
    acc = mu.at["access0"]
    out.results.append(_res("guest_store_delete", m.uid,
                            eq(m, patched(m, [(acc[0], nop(acc[1]))]))))
    um = mu.at["update_mem"]
    # the movabs (10 bytes) stays; the store after it is nopped
    out.results.append(_res("update_mem_delete", m.uid,
                            eq(m, patched(m, [(um[0] + 10, nop(um[1] - 10))]))))
    lr = mu.at["log_reg"]
    out.results.append(_res("log_reg_delete", m.uid,
                            eq(m, patched(m, [(lr[0] + 10, nop(lr[1] - 10))]))))
    nf = mu.at["nf_before_ldst"]
    out.results.append(_res("flag_store_before_ldst", m.uid,
                            eq(m, patched(m, [(nf[0], nop(nf[1]))]))))
    zf = mu.at["zf_before_helper"]
    out.results.append(_res("flag_store_before_helper", m.uid,
                            eq(m, patched(m, [(zf[0], nop(zf[1]))]))))
    bad_ptr = struct.pack("<Q", ENV_ABS + 0x40)
    out.results.append(_res("host_abs_in_env", m.uid,
                            eq(m, patched(m, [(um[0] + 2, bad_ptr)]))))
    unb = struct.pack("<Q", 0x7FFFC0000000)
    out.results.append(_res("host_abs_unbound", m.uid,
                            eq(m, patched(m, [(um[0] + 2, unb)]))))
    # brcond reload (T-B4-2b)
    b, bu = unit_brcond()
    s = bu.at["sync_r2"]
    out.results.append(_res("brcond_reload", b.uid, eq(b, patched(b, [(s[0], nop(s[1]))]))))
    # last-insn can_do_io before a direct-chain exit (F-B4-9)
    cd, cdu = unit_cdio()
    s = cdu.at["cdio1"]
    cdn = patched(cd, [(s[0], nop(s[1]))])
    out.results.append(_res("cdio_last_insn_direct_chain", cd.uid, eq(cd, cdn)))
    control("cdio_last_insn_without_tb_start_cb_equivalent", eq(cd, cdn, c=no_tsc))
    s0 = cdu.at["cdio0"]
    control("cdio_prefix_store_deletable",
            eq(cd, patched(cd, [(s0[0], nop(s0[1]))])))
    # IT-block D4 (F-B4-5)
    it, _ = unit_it()
    itd, _ = unit_it(d4=True)
    out.results.append(_res("it_block_d4", it.uid, eq(it, replace(itd, uid=it.uid + "'"))))
    # dead-result LDST nopped (F-B3-3b, R16)
    dl, _ = unit_deadld()
    dln, _ = unit_deadld(nop_access=True)
    out.results.append(_res("nop_dead_result_ldst", dl.uid, eq(dl, dln)))
    # icount immediate, search row
    ia = cdu.at["icount_add"]
    out.results.append(_res("icount_immediate", cd.uid, eq(cd, patched(
        cd, [(ia[0] + ia[1] - 4, struct.pack("<i", -3))]))))
    out.results.append(_res("search_row", cd.uid, eq(cd, replace(cd, search=b"\x11\x00\x03\x05",
                                                                  uid=cd.uid + "'"))))
    # the coverage LOG_REG block (close-out L1-L8, design/closeout-logreg.md §A.8)
    _logreg_mutants(out, eq, control)
    # guard-stub mutants with WP19's reference emitter (WP18's emitter: integration stage)
    for name, spec in _guard_specs():
        rr, rp = grow_guarded(spec)
        out.results.append(_res(name, rp.uid, check_guarded_chain(rr, rp, tsc, layout=lay),
                                needs_emitter=True))
    rr, rp = grow_guarded()
    gv = check_guarded_chain(rr, rp, tsc, layout=lay)
    control("guarded_chain_reference_equivalent", gv)
    gcert = certify_fact(rp, 0, (OFF_R[0], 4), layout=lay)
    control("fact_certified_at_slot", gcert.certified and gcert.value == 0)
    out.results.append(_fact_res("falsified_fact_link", rp.uid, gcert, 1, needs_emitter=True))
    _, sp = grow_guarded(store_r0=None)
    scert = certify_fact(sp, 0, (OFF_R[0], 4), layout=lay)
    out.results.append(_fact_res("shared_variant_suffix", sp.uid, scert, 0, needs_emitter=True))
    return out


def _logreg_mutants(out: SuiteResult, eq: Callable[..., Verdict],
                    control: Callable[..., None]) -> None:
    """L1-L8 on :func:`unit_logreg`: every change inside the block is rejected (a candidate
    may not gain, lose or change a block: ``edit_inside_log_block``); a ring whose extent
    meets env is ``host_abs_aliases_env``. Controls: the block is matched, the identity is
    equivalent, a D1 deletion after the block is still observed (mismatch), and a block the
    matcher refuses explores to ``backward_branch`` (unsupported, fail closed)."""
    from ..decode.logreg import IDX_STORE, RING_STORE, SPIN_JCC, VALUE_LOAD
    from .paths import explore

    lg, lu = unit_logreg()
    s0 = lu.at["log_block"][0]
    control("logreg_block_matched", bool(lg.logblocks) and s0 in lg.logblocks)
    control("logreg_identity_equivalent", eq(lg, lg))
    r1 = lu.at["r1"]
    # hostsym still sees the code after the block: the D1 store before the exit is observed
    control("logreg_d1_after_block_observed", eq(lg, patched(lg, [(r1[0], nop(r1[1]))])),
            want=False)
    hoist = bytes.fromhex("44896520")  # mov [rbp+0x20],r12d: a regs[8] store in the block
    retgt = struct.pack("<i", struct.unpack_from("<i", lg.code, s0 + 0x2A)[0] + 0x20000)
    muts = (
        ("logreg_ring_store_nop", [(s0 + RING_STORE, nop(4))]),
        ("logreg_index_store_nop", [(s0 + IDX_STORE, nop(4))]),
        ("logreg_index_add8", [(s0 + 0x3C, b"\x08")]),
        ("logreg_spin_nop", [(s0 + SPIN_JCC, nop(2))]),
        ("logreg_spin_retarget", [(s0 + SPIN_JCC + 1, bytes([(0x07 - HEAD) & 0xFF]))]),
        ("logreg_buf_retarget", [(s0 + 0x2A, retgt)]),
        ("logreg_value_field", [(s0 + VALUE_LOAD + 3, b"\x38")]),
        ("logreg_d1_store_hoisted", [(s0 + 0x39, hoist)]),
    )
    for name, ps in muts:
        m = patched(lg, ps)
        out.results.append(_res(name, lg.uid, eq(lg, m)))
        if name == "logreg_spin_retarget":
            ex = explore(m, layout=MINI_LAYOUT)
            control("logreg_unmatched_block_is_backward_branch",
                    not m.logblocks and any(w == "backward_branch" for w, _pc in ex.unsupported))
    # L6 numeric: the ring extent meets env (env_abs known, gdb diag path)
    out.results.append(_res("logreg_ring_aliases_env", lg.uid,
                            eq(lg, lg, env_abs=(LOG_BUF + 0x8000, LOG_BUF + 0x9000))))


HEAD = 0x24  # end of the spin je (its rel8 is relative to S + 0x24)


def _guard_specs() -> Iterable[tuple[str, GuardSpec]]:
    for atom in ("g_bp", "g_thumb", "g_cond", "g_flags", "g_flags2", "g_pc"):
        yield f"drop_{atom}", GuardSpec(drop=frozenset({atom}))
    yield "guard_before_effect", GuardSpec(before_effect=True)
    yield "guard_stub_store", GuardSpec(stub_store=True)


# ---------------------------------------------------------------------------
# real-unit mutants
# ---------------------------------------------------------------------------

def real_mutants(programs: Mapping[str, Program], hostops: Mapping[str, Any], contract: Any,
                 layout: Any, *, limit_per_kind: int = 25,
                 cdio_contract: Any = None) -> SuiteResult:
    """Same-length mutants of captured units (shapes found in their HostOps).

    ``programs``/``hostops``: uid -> :class:`Program` / ``UnitHostOps`` of the
    snapshot. ``cdio_contract`` is a contract with ``tb_start_cb_possible``
    true (the F-B4-9 mutant needs it; the captured snapshots are all false).
    """
    from ..analysis.hostops import OpKind

    out = SuiteResult()
    counts: dict[str, int] = {}

    def want(kind: str) -> bool:
        return counts.get(kind, 0) < limit_per_kind

    def add(kind: str, uid: str, v: Verdict) -> None:
        counts[kind] = counts.get(kind, 0) + 1
        out.results.append(_res(kind, uid, v, source="real"))

    def eq(o, n, c=None):
        return check_equiv(o, n, None, None, c or contract, layout=layout)

    flags = {"CF", "VF", "NF", "ZF"}
    for uid, h in hostops.items():
        if not h.ok:
            continue
        p = programs[uid]
        ops = h.ops
        idx_ldst = [i for i, o in enumerate(ops) if o.kind == OpKind.GUEST_ACCESS]
        # guest store deletion
        if want("guest_store_delete"):
            for i in idx_ldst:
                o = ops[i]
                if o.is_ld is False:
                    add("guest_store_delete", uid, eq(p, patched(p, [(o.off, nop(o.size))])))
                    break
        # icount immediate (prefix "add ebx, imm")
        if want("icount_immediate"):
            for o in ops:
                if o.block == "prefix" and o.text.startswith("add") and o.size >= 3:
                    code = bytearray(p.code[o.off:o.off + o.size])
                    code[-1] ^= 0x01
                    add("icount_immediate", uid, eq(p, patched(p, [(o.off, bytes(code))])))
                    break
        if want("search_row") and p.search:
            s = bytearray(p.search)
            s[0] ^= 0x01
            add("search_row", uid, eq(p, replace(p, search=bytes(s), uid=uid + "'")))
        # flag store with an LDST / non-pure call next (no re-store in between)
        for kind, stop in (("flag_store_before_ldst", lambda o: o.kind == OpKind.GUEST_ACCESS),
                           ("flag_store_before_helper", lambda o: o.kind == OpKind.CALL and
                            o.helper_name not in (None, "lookup_tb_ptr") and
                            (o.helper_flags or 0) & 1 == 0)):
            if not want(kind):
                continue
            for i, o in enumerate(ops):
                if not (o.is_env_store and o.store_field in flags and o.block == "insn"):
                    continue
                f = o.store_field
                hit = False
                for q in ops[i + 1:]:
                    if q.is_env_store and q.store_field == f:
                        break
                    if q.kind in (OpKind.JMP, OpKind.JCC, OpKind.EXIT_JMP, OpKind.GOTO_PTR):
                        break
                    if stop(q):
                        hit = True
                        break
                if hit:
                    add(kind, uid, eq(p, patched(p, [(o.off, nop(o.size))])))
                    break
        # brcond sync store reloaded after a label (T-B4-2b)
        if want("brcond_reload"):
            targets = {o.target for o in ops if o.target is not None}
            for i, o in enumerate(ops):
                if not (o.is_env_store and o.origin == "global_sync" and o.block == "insn"):
                    continue
                f = o.store_field
                seen_label = False
                hit = False
                for q in ops[i + 1:]:
                    if q.off in targets or q.kind == OpKind.JCC:
                        seen_label = True
                    if q.is_env_store and q.store_field == f:
                        break
                    if q.kind in (OpKind.EXIT_JMP, OpKind.GOTO_PTR, OpKind.CALL) or \
                            (q.kind == OpKind.JMP and "GOTO_TB" in q.sites):
                        break
                    if seen_label and q.kind == OpKind.ENV_LOAD and q.env and \
                            q.env[0].field == f:
                        hit = True
                        break
                if hit:
                    add("brcond_reload", uid, eq(p, patched(p, [(o.off, nop(o.size))])))
                    break
        # last-insn can_do_io = 1 before a direct-chain (goto_tb) exit
        if want("cdio_last_insn_direct_chain") and p.goto_tb and cdio_contract is not None:
            for o in ops:
                if o.origin == "cdio_insn" and o.imm == 1:
                    add("cdio_last_insn_direct_chain", uid,
                        eq(p, patched(p, [(o.off, nop(o.size))]), cdio_contract))
                    break
    return out
