"""The canonical guard template of B3/B4 design §4.1: emitter and strict decoder. WP18.

A guarded exit replaces R's 11-byte lookup tail ``mov rdi,rbp; call
[rip+d]; jmp rax`` at offset ``T`` by ``jmp rel32 S`` + 6 x ``int3`` and
appends the stub ``S`` after R's pool (template id :data:`GUARD_TEMPLATE_ID`)::

    S:  cmp  qword [rbp+bp], 0        ; g_bp     48 83 bd d32 00     ; jne F (0f 85 rel32)
        cmp  byte  [rbp+thumb], 1     ; g_thumb  80 bd d32 01        ; jne F
        cmp  dword [rbp+cond], 0      ; g_cond   83 bd d32 00        ; jne F
        cmp  dword [rbp+flags], Kf    ; g_flags  81 bd d32 imm32     ; jne F
        cmp  qword [rbp+flags2], Kf2  ; g_flags2 48 81 bd d32 imm32  ; jne F
            (Kf2 >= 2^31: mov rax, imm64 (48 b8 imm64); cmp qword [rbp+flags2], rax (48 39 85 d32))
        cmp  dword [rbp+pc], Vc_k     ; g_pc     81 bd d32 imm32     ; je L_k (0f 84 rel32)   (k = 1..2)
        jmp  F                                                        (e9 rel32)
    L_k: nop* ; jmp rel32 0            ; the slot: rel32 field 4-aligned (relative to tc)
        reset GENERAL (registered): lea rax,[rip -> R_struct + n] (48 8d 05 d32) ; jmp tb_ret (e9 rel32)
        reset SPEC, or any unregistered slot: jmp F (e9 rel32)
    F:  mov rdi,rbp ; call [rip+d'] ; jmp rax    ; the original tail (same pool slot, d' re-resolved)

Every branch uses the rel32 form, so the stub's length depends only on the
number of targets, ``Kf2 >= 2^31`` and the alignment padding.

An **unregistered** slot (v5 artifacts: the record keeps
``jmp_insn_offset[n] == 0xffff``) must never exit with ``exit_tb(R|n)``: the
main loop would ``tb_add_jump(R, n, …)`` and patch ``tc.ptr + 0xffff``
(``tb_set_jmp_target`` does not check the offset). Its reset path is therefore
``jmp F`` whatever its kind, and nothing can ever link it: the guard is
executed and always ends in the original lookup (behaviour equals the
baseline).

Offsets are relative to the unit's ``tc`` (first code byte). The only
position-dependent field is the GENERAL reset's ``jmp tb_ret`` (a
``rel_minus_delta`` fixup); the struct LEA is RIP-relative to R's own struct and
the pool call is RIP-relative to R's own pool, so both move with the unit.

:func:`decode_guard` parses a stub back strictly against the template (byte
patterns, not capstone) and returns the atoms, targets, reset kinds and ``F``;
V13 compares that with the ext slot entries and the continuation key.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "GUARD_TEMPLATE_ID", "NOTCACHED2", "PSTATE_SS", "SS_ACTIVE", "CS_THUMB", "CS_CONDEXEC_MASK",
    "GuardOffsets", "GuardTarget", "SlotInfo", "GuardStub", "GuardError", "emit_guard",
    "decode_guard", "DecodedGuard", "guard_key", "TAIL_LEN", "ATOMS", "guard_atoms",
]

GUARD_TEMPLATE_ID = "guard-v1"
TAIL_LEN = 11
#: cs_base bits the lookup ORs into a local copy only (design §4.1, G17)
CS_THUMB = 1 << 23
CS_CONDEXEC_MASK = 0xFF << 24
NOTCACHED2 = CS_THUMB | CS_CONDEXEC_MASK | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5)
#: cs_base (flags2) bits 2-5 of NOTCACHED2: LSPACT, NEW_FP_CTXT_NEEDED, FPCCR_S_WRONG,
#: MVE_NO_PRED (target/arm/cpu.h TBFLAG_M32)
CS_M_FP_BITS = (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5)
PSTATE_SS = 1 << 2  # TBFLAG_ANY PSTATE__SS (not cached), target/arm/cpu.h:2958
SS_ACTIVE = 1 << 1  # TBFLAG_ANY SS_ACTIVE, target/arm/cpu.h:2957
ATOMS = ("g_bp", "g_thumb", "g_cond", "g_flags", "g_flags2", "g_pc")

_JNE, _JE = 0x85, 0x84
_MOV_RDI_RBP = (b"\x48\x8b\xfd", b"\x48\x89\xef")


class GuardError(ValueError):
    """The guard cannot be emitted (``reason`` is a stable B3 reason code)."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class GuardOffsets:
    """Env displacements from ``%rbp`` (design §0 G21), bound to an EnvLayout."""

    bp: int = -0x2980
    thumb: int = 0x14D
    condexec: int = 0x228
    flags: int = 0x150
    flags2: int = 0x158
    pc: int = 0x3C
    envlayout_id: str | None = None

    #: EnvLayout element names (``scopt.formats.envlayout_v1.G21_NAMES`` values)
    FIELDS = {"bp": "cpu.breakpoints.tqh_first", "thumb": "thumb",
              "condexec": "condexec_bits", "flags": "hflags.flags",
              "flags2": "hflags.flags2", "pc": "regs[15]"}

    @classmethod
    def from_layout(cls, layout: Any) -> "GuardOffsets":
        """From an :class:`~scopt.formats.envlayout_v1.EnvLayout` (``guard_layout_unbound``
        when a field is missing)."""
        if layout is None:
            raise GuardError("guard_layout_unbound", "no EnvLayout")
        try:
            vals = {k: layout.offset(n) for k, n in cls.FIELDS.items()}
        except KeyError as e:
            raise GuardError("guard_layout_unbound", str(e)) from None
        return cls(**vals, envlayout_id=getattr(layout, "envlayout_id", None))

    def env_field(self, atom: str) -> str:
        return self.FIELDS[{"g_bp": "bp", "g_thumb": "thumb", "g_cond": "condexec",
                            "g_flags": "flags", "g_flags2": "flags2", "g_pc": "pc"}[atom]]


@dataclass(frozen=True)
class GuardTarget:
    """One guarded target of an exit: slot ``n`` jumps there when ``regs[15] == vaddr``."""

    vaddr: int
    slot_n: int
    kind: str  # "GENERAL" | "SPEC"
    registered: bool = True


@dataclass(frozen=True)
class SlotInfo:
    """Where a target's slot ended up inside the stub (offsets relative to tc)."""

    vaddr: int
    slot_n: int
    kind: str
    registered: bool
    pc_cmp_off: int  # the g_pc compare of this target
    label_off: int  # L_k (first pad byte or the jmp)
    jmp_off: int  # the e9 of the slot jump
    field_off: int  # jmp_insn_offset[n] (the rel32 field)
    reset_off: int  # jmp_reset_offset[n] == field_off + 4
    reset_kind: str  # "exit_tb" | "jmp_F"


@dataclass(frozen=True)
class GuardStub:
    """An emitted stub (bytes at ``off`` relative to tc) and its metadata."""

    off: int
    code: bytes
    f_off: int
    kf: int
    kf2: int
    slots: tuple[SlotInfo, ...]
    sites: tuple[tuple, ...]  # (field_off, insn_off, width, enc, kind, aux, value)
    opmarks: tuple[tuple, ...]  # (host_off, kind, helper_id)
    fixups: tuple[tuple, ...]  # (field_off, width, rule, site_kind, target_class, target_abs)
    offsets: GuardOffsets = field(default_factory=GuardOffsets)
    template_id: str = GUARD_TEMPLATE_ID

    @property
    def end(self) -> int:
        return self.off + len(self.code)

    @property
    def guard_len(self) -> int:
        return len(self.code)  # S through F inclusive


def guard_key(key_flags: int, key_cs_base: int) -> tuple[int, int]:
    """``(Kf, Kf2)`` for a continuation key; raises ``continuation_key_mismatch``
    unless THUMB = 1, CONDEXEC = 0, PSTATE__SS = 0, SS_ACTIVE = 0 and the M-profile
    FP/MVE cs_base bits (:data:`CS_M_FP_BITS`) are 0 (design §3.1,
    §4.1: with SS_ACTIVE = 1 the lookup ORs PSTATE__SS in from ``env->pstate``, so
    a guard pass would not imply an equal key)."""
    if not key_cs_base & CS_THUMB:
        raise GuardError("continuation_key_mismatch", "THUMB clear in the continuation key")
    if key_cs_base & CS_CONDEXEC_MASK:
        raise GuardError("continuation_key_mismatch", "CONDEXEC set in the continuation key")
    if key_flags & PSTATE_SS:
        raise GuardError("continuation_key_mismatch", "PSTATE__SS set in the continuation key")
    if key_flags & SS_ACTIVE:
        raise GuardError("continuation_key_mismatch", "SS_ACTIVE set in the continuation key")
    if key_cs_base & CS_M_FP_BITS:
        # LSPACT / NEW_FP_CTXT_NEEDED / FPCCR_S_WRONG / MVE_NO_PRED are not
        # compared by the guard (NOTCACHED2: the lookup recomputes them from env).
        # They are never set on the admitted core (cortex-m3, V20); refuse a key
        # that has them instead of silently masking them out.
        raise GuardError("continuation_key_mismatch",
                         f"M-profile FP/MVE cs_base bits 0x{key_cs_base & CS_M_FP_BITS:x} set "
                         "in the continuation key (cortex-m3 only)")
    return key_flags & ~PSTATE_SS & 0xFFFFFFFF, key_cs_base & ~NOTCACHED2 & ((1 << 64) - 1)


def guard_atoms(offsets: GuardOffsets, kf: int, kf2: int,
                vaddrs: Sequence[int]) -> list[dict[str, Any]]:
    """The GuardAtom items (CacheRegion ``extra_guards``, design §3.9)."""
    o = offsets
    out = [{"atom": "g_bp", "env_field": o.FIELDS["bp"], "width": 8, "op": "zero",
            "value": "0x0"},
           {"atom": "g_thumb", "env_field": o.FIELDS["thumb"], "width": 1, "op": "eq",
            "value": "0x1"},
           {"atom": "g_cond", "env_field": o.FIELDS["condexec"], "width": 4, "op": "zero",
            "value": "0x0"},
           {"atom": "g_flags", "env_field": o.FIELDS["flags"], "width": 4, "op": "eq",
            "value": hex(kf)},
           {"atom": "g_flags2", "env_field": o.FIELDS["flags2"], "width": 8, "op": "eq",
            "value": hex(kf2)}]
    out += [{"atom": "g_pc", "env_field": o.FIELDS["pc"], "width": 4, "op": "eq",
             "value": hex(v)} for v in vaddrs]
    return out


# ---------------------------------------------------------------------------
# emitter
# ---------------------------------------------------------------------------

def _d32(v: int) -> bytes:
    if not -(1 << 31) <= v < (1 << 31):
        raise GuardError("rel32_range", f"displacement {v:#x}")
    return struct.pack("<i", v)


def _u32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


# site kinds / encodings / opmark kinds (scopt.formats.enums numbering)
_LABEL_JMP, _LABEL_JCC, _HELPER_CALL, _EXIT_RET, _MOVI_EXIT, _GOTO_TB, _GOTO_PTR = (
    0x01, 0x02, 0x06, 0x07, 0x09, 0x0D, 0x0E)
_E_NONE, _E_REL32, _E_RIPREL32, _E_POOL_IND, _E_GOTO_TB = 0, 2, 3, 8, 9
_M_CALL, _M_EXIT_TB, _M_GOTO_TB, _M_GOTO_PTR = 1, 4, 5, 6


def emit_guard(*, stub_off: int, tail: bytes, pool_slot_off: int, lookup_helper_id: int,
               lookup_func: int, kf: int, kf2: int, targets: Sequence[GuardTarget],
               tc_abs: int, struct_abs: int, tb_ret_abs: int, goto_ptr_reg: int = 0,
               offsets: GuardOffsets = GuardOffsets()) -> GuardStub:
    """Emit the stub for one exit at ``stub_off`` (relative to tc).

    ``tail`` is the original 11-byte tail (its ``mov rdi,rbp`` encoding is kept
    in ``F``); ``pool_slot_off`` the pool slot the tail's call reads (relative to
    tc); ``tc_abs``/``struct_abs``/``tb_ret_abs`` the absolute addresses at the
    unit's current placement. ``targets``: 1 or 2, distinct slots and vaddrs.
    """
    if not 1 <= len(targets) <= 2:
        raise GuardError("slot_budget", f"{len(targets)} targets (1 or 2 per exit)")
    if len({t.slot_n for t in targets}) != len(targets) or \
            len({t.vaddr for t in targets}) != len(targets):
        raise GuardError("no_free_slot", "two targets share a slot or a vaddr")
    for t in targets:
        if t.slot_n not in (0, 1) or t.kind not in ("GENERAL", "SPEC"):
            raise GuardError("slot_kind", f"{t}")
        if t.kind == "SPEC" and not t.registered:
            raise GuardError("slot_kind", "a SPEC slot must be registered (v6)")
    if len(tail) != TAIL_LEN or tail[:3] not in _MOV_RDI_RBP or tail[3:5] != b"\xff\x15" or \
            tail[9:11] != b"\xff\xe0":
        raise GuardError("exit_not_goto_ptr", f"tail {bytes(tail).hex()}")
    o = offsets
    buf = bytearray()
    labels: dict[str, int] = {}
    fix: list[tuple[int, str]] = []
    sites: list[tuple] = []
    marks: list[tuple] = []
    fixups: list[tuple] = []

    def pos() -> int:
        return stub_off + len(buf)

    def jcc(cc: int, lab: str) -> None:
        io = pos()
        buf.extend(bytes([0x0F, cc]))
        fix.append((len(buf), lab))
        sites.append([pos(), io, 4, _E_REL32, _LABEL_JCC, 0, lab])
        buf.extend(b"\0\0\0\0")

    def jmp(lab: str) -> None:
        io = pos()
        buf.append(0xE9)
        fix.append((len(buf), lab))
        sites.append([pos(), io, 4, _E_REL32, _LABEL_JMP, 0, lab])
        buf.extend(b"\0\0\0\0")

    buf += b"\x48\x83\xbd" + _d32(o.bp) + b"\x00"
    jcc(_JNE, "F")
    buf += b"\x80\xbd" + _d32(o.thumb) + b"\x01"
    jcc(_JNE, "F")
    buf += b"\x83\xbd" + _d32(o.condexec) + b"\x00"
    jcc(_JNE, "F")
    buf += b"\x81\xbd" + _d32(o.flags) + _u32(kf)
    jcc(_JNE, "F")
    if kf2 < (1 << 31):
        buf += b"\x48\x81\xbd" + _d32(o.flags2) + _u32(kf2)
    else:
        buf += b"\x48\xb8" + struct.pack("<Q", kf2) + b"\x48\x39\x85" + _d32(o.flags2)
    jcc(_JNE, "F")
    pc_cmp = []
    for k, t in enumerate(targets):
        pc_cmp.append(pos())
        buf += b"\x81\xbd" + _d32(o.pc) + _u32(t.vaddr)
        jcc(_JE, f"L{k}")
    jmp("F")
    infos = []
    for k, t in enumerate(targets):
        labels[f"L{k}"] = len(buf)
        lab_off = pos()
        while (pos() + 1) % 4:
            buf.append(0x90)
        jo = pos()
        buf.append(0xE9)
        fo = pos()
        buf.extend(b"\0\0\0\0")
        ro = pos()
        if t.registered:
            sites.append([fo, jo, 4, _E_GOTO_TB, _GOTO_TB, t.slot_n, tc_abs + ro])
            marks.append((jo, _M_GOTO_TB, None))
        else:
            sites.append([fo, jo, 4, _E_REL32, _LABEL_JMP, 0, tc_abs + ro])
        if t.registered and t.kind == "GENERAL":
            lo = pos()
            here = tc_abs + lo + 7
            buf += b"\x48\x8d\x05" + _d32(struct_abs + t.slot_n - here)
            sites.append([lo + 3, lo, 4, _E_RIPREL32, _MOVI_EXIT, t.slot_n,
                          struct_abs + t.slot_n])
            marks.append((lo, _M_EXIT_TB, None))
            jo2 = pos()
            buf += b"\xe9" + _d32(tb_ret_abs - (tc_abs + jo2 + 5))
            sites.append([jo2 + 1, jo2, 4, _E_REL32, _EXIT_RET, t.slot_n, tb_ret_abs])
            fixups.append((jo2 + 1, 4, "rel_minus_delta", _EXIT_RET, "prologue.tb_ret",
                           tb_ret_abs))
            rk = "exit_tb"
        else:
            jmp("F")
            rk = "jmp_F"
        infos.append(SlotInfo(t.vaddr, t.slot_n, t.kind, t.registered, pc_cmp[k], lab_off, jo,
                              fo, ro, rk))
    labels["F"] = len(buf)
    f_off = pos()
    disp = pool_slot_off - (f_off + 9)
    buf += bytes(tail[:5]) + _d32(disp) + b"\xff\xe0"
    sites.append([f_off + 5, f_off + 3, 4, _E_POOL_IND, _HELPER_CALL, lookup_helper_id,
                  lookup_func])
    sites.append([f_off + 9, f_off + 9, 0, _E_NONE, _GOTO_PTR, goto_ptr_reg, 0])
    marks.append((f_off, _M_CALL, lookup_helper_id))
    marks.append((f_off + 9, _M_GOTO_PTR, None))
    for p, lab in fix:
        tgt = labels[lab]
        buf[p:p + 4] = _d32(tgt - (p + 4))
    out_sites = []
    for s in sites:
        if isinstance(s[6], str):
            s[6] = tc_abs + stub_off + labels[s[6]]
        out_sites.append(tuple(s))
    return GuardStub(stub_off, bytes(buf), f_off, kf, kf2, tuple(infos), tuple(out_sites),
                     tuple(sorted(marks)), tuple(fixups), offsets)


# ---------------------------------------------------------------------------
# strict decoder (V13)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DecodedGuard:
    off: int
    end: int  # first byte after F
    f_off: int
    atoms: tuple[tuple[str, int, int], ...]  # (atom, env displacement, value)
    kf: int
    kf2: int
    slots: tuple[SlotInfo, ...]
    f_bytes: bytes
    pool_slot_off: int  # the slot F's call reads (relative to tc)
    tb_ret_abs: tuple[int | None, ...] = ()  # per slot, GENERAL registered resets
    struct_rel: tuple[int | None, ...] = ()  # per slot: lea target - struct (== n)

    @property
    def vaddrs(self) -> tuple[int, ...]:
        return tuple(s.vaddr for s in self.slots)


def _i32(b: bytes, o: int) -> int:
    return struct.unpack_from("<i", b, o)[0]


def _u32r(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def decode_guard(code: bytes, off: int, *, tc_abs: int, struct_off: int,
                 offsets: GuardOffsets | None = None,
                 registered: Mapping[int, tuple[int, int]] | None = None) -> DecodedGuard:
    """Decode the stub at ``off`` (relative to tc) against the template; raises
    :class:`GuardError` (``guard_contract_violation``) on any deviation.

    ``struct_off`` is ``tb_offset - tc_offset`` (negative). ``registered`` maps a
    slot ``n`` to the record's ``(jmp_insn_offset[n], jmp_reset_offset[n])`` for
    registered slots; a slot jump whose field is listed there is registered.
    ``offsets`` (when given) must equal the decoded displacements.
    """
    b = bytes(code)
    p = off
    viol = "guard_contract_violation"

    def need(raw: bytes, what: str) -> None:
        nonlocal p
        if b[p:p + len(raw)] != raw:
            raise GuardError(viol, f"{what} @+{p:#x}: {b[p:p + len(raw) + 4].hex()}")
        p += len(raw)

    def disp_cmp(prefix: bytes, what: str) -> int:
        nonlocal p
        need(prefix, what)
        d = _i32(b, p)
        p += 4
        return d

    jcc_targets: list[tuple[int, int]] = []  # (cc, target off)

    def jcc(cc: int, what: str) -> int:
        nonlocal p
        need(bytes([0x0F, cc]), what)
        t = p + 4 + _i32(b, p)
        p += 4
        jcc_targets.append((cc, t))
        return t

    atoms = []
    d = disp_cmp(b"\x48\x83\xbd", "g_bp")
    need(b"\x00", "g_bp imm")
    atoms.append(("g_bp", d, 0))
    f1 = jcc(_JNE, "g_bp jne")
    d = disp_cmp(b"\x80\xbd", "g_thumb")
    need(b"\x01", "g_thumb imm")
    atoms.append(("g_thumb", d, 1))
    fs = [f1, jcc(_JNE, "g_thumb jne")]
    d = disp_cmp(b"\x83\xbd", "g_cond")
    need(b"\x00", "g_cond imm")
    atoms.append(("g_cond", d, 0))
    fs.append(jcc(_JNE, "g_cond jne"))
    d = disp_cmp(b"\x81\xbd", "g_flags")
    kf = _u32r(b, p)
    p += 4
    atoms.append(("g_flags", d, kf))
    fs.append(jcc(_JNE, "g_flags jne"))
    if b[p:p + 3] == b"\x48\x81\xbd":
        d = disp_cmp(b"\x48\x81\xbd", "g_flags2")
        kf2 = _u32r(b, p)
        p += 4
        if kf2 >= 1 << 31:
            raise GuardError(viol, "g_flags2 imm32 would be sign-extended")
    else:
        need(b"\x48\xb8", "g_flags2 movabs")
        kf2 = struct.unpack_from("<Q", b, p)[0]
        p += 8
        d = disp_cmp(b"\x48\x39\x85", "g_flags2 cmp rax")
        if kf2 < 1 << 31:
            raise GuardError(viol, "g_flags2 imm64 form for a small constant")
    atoms.append(("g_flags2", d, kf2))
    fs.append(jcc(_JNE, "g_flags2 jne"))
    pcs = []
    while b[p:p + 2] == b"\x81\xbd":
        co = p
        d = disp_cmp(b"\x81\xbd", "g_pc")
        v = _u32r(b, p)
        p += 4
        atoms.append(("g_pc", d, v))
        lk = jcc(_JE, "g_pc je")
        pcs.append((co, v, lk))
    if not 1 <= len(pcs) <= 2:
        raise GuardError(viol, f"{len(pcs)} g_pc compares")
    need(b"\xe9", "jmp F")
    f_target = p + 4 + _i32(b, p)
    p += 4
    slots = []
    rets: list[int | None] = []
    srel: list[int | None] = []
    reg = dict(registered or {})
    for co, v, lk in pcs:
        if p != lk:
            raise GuardError(viol, f"L for {v:#x} at +{lk:#x}, expected +{p:#x}")
        lab = p
        while b[p:p + 1] == b"\x90":
            p += 1
        if p - lab > 3 or (p + 1) % 4:
            raise GuardError(viol, f"slot pad {p - lab} bytes, field not 4-aligned")
        jo = p
        need(b"\xe9", "slot jmp")
        fo = p
        if b[fo:fo + 4] != b"\0\0\0\0":
            raise GuardError(viol, f"slot field @+{fo:#x} not 0 on disk")
        p += 4
        ro = p
        n_reg = next((n for n, (io, rs) in reg.items() if io == fo), None)
        if n_reg is not None and reg[n_reg][1] != ro:
            raise GuardError(viol, f"jmp_reset_offset[{n_reg}] {reg[n_reg][1]} != {ro}")
        if b[p:p + 3] == b"\x48\x8d\x05":
            p += 3
            lea_t = p + 4 + _i32(b, p)
            p += 4
            n = lea_t - struct_off
            need(b"\xe9", "reset jmp tb_ret")
            ret = tc_abs + p + 4 + _i32(b, p)
            p += 4
            if n not in (0, 1):
                raise GuardError(viol, f"reset lea targets struct+{n}")
            if n_reg is None or n_reg != n:
                raise GuardError(viol, f"exit_tb(R|{n}) reset on an unregistered slot field")
            slots.append(SlotInfo(v, n, "GENERAL", True, co, lab, jo, fo, ro, "exit_tb"))
            rets.append(ret)
            srel.append(n)
        else:
            need(b"\xe9", "reset jmp F")
            t = p + 4 + _i32(b, p)
            p += 4
            if t != f_target:
                raise GuardError(viol, "reset jmp does not target F")
            kind = "SPEC" if n_reg is not None else "GENERAL"
            slots.append(SlotInfo(v, -1 if n_reg is None else n_reg, kind, n_reg is not None,
                                  co, lab, jo, fo, ro, "jmp_F"))
            rets.append(None)
            srel.append(None)
    if p != f_target:
        raise GuardError(viol, f"F at +{f_target:#x}, expected +{p:#x}")
    if any(cc == _JNE and t != f_target for cc, t in jcc_targets):
        raise GuardError(viol, "a failing atom does not branch to F")
    f_off = p
    if b[p:p + 3] not in _MOV_RDI_RBP:
        raise GuardError(viol, "F: mov rdi,rbp")
    p += 3
    need(b"\xff\x15", "F: call [rip+d]")
    pool = p + 4 + _i32(b, p)
    p += 4
    need(b"\xff\xe0", "F: jmp rax")
    if offsets is not None:
        want = {"g_bp": offsets.bp, "g_thumb": offsets.thumb, "g_cond": offsets.condexec,
                "g_flags": offsets.flags, "g_flags2": offsets.flags2, "g_pc": offsets.pc}
        bad = [a for a, dd, _ in atoms if want[a] != dd]
        if bad:
            raise GuardError(viol, f"env displacements differ from the EnvLayout: {bad}")
    return DecodedGuard(off, p, f_off, tuple(atoms), kf, kf2, tuple(slots), b[f_off:p], pool,
                        tuple(rets), tuple(srel))
