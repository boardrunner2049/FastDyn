"""A synthetic TB that exercises every Tier-0 site kind the importer handles (WP7 tests).

:func:`rich_tb` lays out x86-64 bytes the way QEMU's backend emits them for a
4-insn Thumb block, together with a capture that describes them exactly::

    off  bytes                         site / opmark                       guest insn
      0  mov ebx,[rbp-8]; test ebx,ebx                                     (TB start)
      5  jl L_exit                     LABEL_JCC -> 68
     11  mov eax,[rbp+0x3c]            QEMU_LD opmark                      0
     14  jne slow                      LDST_FAST_JCC -> 85
     20  mov ecx,[rax]
     22  lea rsi,[rip+plugin_data]     MOVI_CONST RIPREL32 (ext.ptr_riprel) 1 (CALL opmark @22)
     29  call plugin_func              HELPER_CALL REL32 (helper 0, unnamed)
     34  mov dword [rbp+0x3c], imm32
     41  nop2; jmp +0                  GOTO_TB slot 0 (field 44)           2
     48  lea rax,[rip+tb+0]            MOVI_EXIT_TB_PTR(0)
     55  jmp tb_ret                    EXIT_TB_RET_JMP(0)
     60  call [rip+pool0]              HELPER_CALL POOL_INDIRECT (helper 1) 3
     66  jmp rax                       GOTO_PTR
     68  L_exit: lea rax,[rip+tb+3]    MOVI_EXIT_TB_PTR(3)
     75  jmp tb_ret                    EXIT_TB_RET_JMP(3)
     80  jmp epilogue                  EXIT_TB_EPI_JMP
     85  slow: lea rcx,[rip+raddr]     MOVI_LDST_RA (raddr = 22)           (insn 0)
     92  call [rip+pool1]              LDST_HELPER_CALL POOL_INDIRECT
     98  jmp raddr                     LDST_RET_JMP
    103  nop                           pad to the 8-aligned pool
    104  pool: lookup_tb_ptr, ldst helper (2 x 8 bytes)

The external targets live in :data:`RICH_MAPS`: the QEMU executable (inode
4242 = the builders' RUN_INFO ``exe_st_ino``) far from the arena, so its
helpers are pool-indirect, and ``/synth/libfastdyn.so`` within rel32 range of
the arena, so the plugin callback is a direct ``call rel32``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from scopt.formats import scm_v1 as S
from scopt.formats.enums import Encoding, OpMarkKind, SiteKind
from scopt.ids import TBKey

from . import builders as B

PLUGIN_FUNC = 0x7FFF1F025757
PLUGIN_DATA = 0x7FFF1F03A010
LOOKUP_FUNC = 0x555555600040
LDST_FUNC = 0x555555600080

RICH_MAPS = (
    "555555554000-555555c00000 r-xp 00000000 103:03 4242      /synth/qemu-system-arm\n"
    "555555c00000-555555d00000 rw-p 006ac000 103:03 4242      /synth/qemu-system-arm\n"
    "555556000000-555556100000 rw-p 00000000 00:00 0          [heap]\n"
    "7fff1f000000-7fff1f02c000 r-xp 00000000 103:03 77        /synth/libfastdyn.so\n"
    "7fff1f039000-7fff1f03b000 rw-p 00038000 103:03 77        /synth/libfastdyn.so\n"
    f"{B.DEFAULT_CODE_ADDRESS:x}-{B.DEFAULT_CODE_ADDRESS + (1 << 30):x} rwxp 00000000 00:00 0 \n"
    "7ffff7c00000-7ffff7c28000 r-xp 00000000 103:03 78        /usr/lib/libc.so.6\n"
).encode()

RICH_HELPERS = (
    {"helper_id": 0, "name": None, "func": PLUGIN_FUNC, "tcg_flags": 0},
    {"helper_id": 1, "name": "lookup_tb_ptr", "func": LOOKUP_FUNC, "tcg_flags": 6},
)


def _rel32(target: int, next_ip: int) -> bytes:
    return (target - next_ip).to_bytes(4, "little", signed=True)


def rich_tb(tb_offset: int, *, key: TBKey = B.DEFAULT_KEY, vaddr: int | None = None,
            code_address: int = B.DEFAULT_CODE_ADDRESS, goto_dest: int | None = None,
            offpage_dest: int | None = None, guest_bytes: bytes = b"\x00\xbf" * 4,
            page_bits: int = B.DEFAULT_TARGET_PAGE_BITS) -> B.SynthUnit:
    """The TB of the module docstring at ``tb_offset`` (64-aligned)."""
    pmask = (1 << page_bits) - 1
    if vaddr is None:
        vaddr = 0x08000000 | (key.page_addr0 & pmask)
    tc = tb_offset + B.round_up(B.DEFAULT_TB_SIZE, B.DEFAULT_ICACHE_LINESIZE)
    base = code_address + tc
    tb_abs = code_address + tb_offset
    tb_ret = code_address + B.SYNTH_TB_RET_OFF
    epi = code_address + B.SYNTH_EPILOGUE_OFF
    c = bytearray()

    def emit(b: bytes) -> int:
        at = len(c)
        c.extend(b)
        return at

    emit(bytes.fromhex("8b5df8" "85db"))                                   # 0
    emit(b"\x0f\x8c" + _rel32(base + 68, base + 11))                        # 5
    emit(bytes.fromhex("8b453c"))                                           # 11
    emit(b"\x0f\x85" + _rel32(base + 85, base + 20))                        # 14
    emit(bytes.fromhex("8b08"))                                             # 20
    emit(b"\x48\x8d\x35" + _rel32(PLUGIN_DATA, base + 29))                  # 22
    emit(b"\xe8" + _rel32(PLUGIN_FUNC, base + 34))                          # 29
    emit(b"\xc7\x45\x3c" + (vaddr + 6).to_bytes(4, "little"))               # 34
    emit(b"\x66\x90" + b"\xe9" + b"\0\0\0\0")                               # 41 (E9 at 43)
    emit(b"\x48\x8d\x05" + _rel32(tb_abs + 0, base + 55))                   # 48
    emit(b"\xe9" + _rel32(tb_ret, base + 60))                               # 55
    emit(b"\xff\x15" + _rel32(base + 104, base + 66))                       # 60
    emit(b"\xff\xe0")                                                       # 66
    emit(b"\x48\x8d\x05" + _rel32(tb_abs + 3, base + 75))                   # 68
    emit(b"\xe9" + _rel32(tb_ret, base + 80))                               # 75
    emit(b"\xe9" + _rel32(epi, base + 85))                                  # 80
    emit(b"\x48\x8d\x0d" + _rel32(base + 22, base + 92))                    # 85
    emit(b"\xff\x15" + _rel32(base + 112, base + 98))                       # 92
    emit(b"\xe9" + _rel32(base + 22, base + 103))                           # 98
    emit(b"\x90")                                                           # 103
    emit(LOOKUP_FUNC.to_bytes(8, "little") + LDST_FUNC.to_bytes(8, "little"))  # 104
    assert len(c) == 120

    R = Encoding
    K = SiteKind

    def site(field: int, insn: int, enc: Encoding, kind: SiteKind, value: int, idx: int | None,
             aux: int = 0, width: int = 4) -> dict[str, Any]:
        return {"field_off": field, "insn_off": insn, "width": width, "enc": enc, "kind": kind,
                "aux": aux, "value": value, "insn_idx": idx}

    sites = [
        site(7, 5, R.REL32, K.LABEL_JCC, base + 68, None),
        site(16, 14, R.REL32, K.LDST_FAST_JCC, base + 85, 0),
        site(25, 22, R.RIPREL32, K.MOVI_CONST, PLUGIN_DATA, 1),
        site(30, 29, R.REL32, K.HELPER_CALL, PLUGIN_FUNC, 1, aux=0),
        site(44, 43, R.GOTO_TB_REL32, K.GOTO_TB, base + 48, 2, aux=0),
        site(51, 48, R.RIPREL32, K.MOVI_EXIT_TB_PTR, tb_abs + 0, 2, aux=0),
        site(56, 55, R.REL32, K.EXIT_TB_RET_JMP, tb_ret, 2, aux=0),
        site(62, 60, R.POOL_INDIRECT, K.HELPER_CALL, LOOKUP_FUNC, 3, aux=1),
        site(66, 66, R.NONE, K.GOTO_PTR, 0, 3, aux=0, width=0),
        site(71, 68, R.RIPREL32, K.MOVI_EXIT_TB_PTR, tb_abs + 3, 3, aux=3),
        site(76, 75, R.REL32, K.EXIT_TB_RET_JMP, tb_ret, 3, aux=3),
        site(81, 80, R.REL32, K.EXIT_TB_EPI_JMP, epi, 3),
        site(88, 85, R.RIPREL32, K.MOVI_LDST_RA, base + 22, 0, aux=0),
        site(94, 92, R.POOL_INDIRECT, K.LDST_HELPER_CALL, LDST_FUNC, 0, aux=0),
        site(99, 98, R.REL32, K.LDST_RET_JMP, base + 22, 0, aux=0),
    ]
    pool = [{"user_field_off": 62, "slot_off": 104, "data0": LOOKUP_FUNC},
            {"user_field_off": 94, "slot_off": 112, "data0": LDST_FUNC}]
    ldst = [{"jcc_field": (16, None), "slow_start": 85, "slow_end": 103, "raddr_off": 22,
             "oi": 0x22, "op_ordinal": 0, "is_ld": 1}]
    O = OpMarkKind
    opmarks = [{"host_off": 11, "kind": O.QEMU_LD, "insn_idx": 0},
               {"host_off": 22, "kind": O.CALL, "insn_idx": 1, "helper_id": 0},
               {"host_off": 41, "kind": O.GOTO_TB, "insn_idx": 2},
               {"host_off": 48, "kind": O.EXIT_TB, "insn_idx": 2},
               {"host_off": 60, "kind": O.CALL, "insn_idx": 3, "helper_id": 1},
               {"host_off": 66, "kind": O.GOTO_PTR, "insn_idx": 3},
               {"host_off": 68, "kind": O.EXIT_TB, "insn_idx": 3},
               {"host_off": 80, "kind": O.EXIT_TB, "insn_idx": 3}]
    page = vaddr & ~pmask
    if goto_dest is None:
        goto_dest = page | ((vaddr + 0x40) & pmask)
    if offpage_dest is None:
        offpage_dest = page + (1 << page_bits) + 0x10
    static_exits = [{"slot": 0, "direct": 1, "insn_idx": 2, "dest_vaddr": goto_dest},
                    {"slot": 0xFF, "direct": 0, "insn_idx": 3, "dest_vaddr": offpage_dest}]
    w0 = vaddr & pmask
    insns = [S.ScmInsn((w0, 0, 0), 11, 22), S.ScmInsn((w0 + 2, 0, 0), 22, 41),
             S.ScmInsn((w0 + 4, 0, 0), 41, 60), S.ScmInsn((w0 + 6, 0, 0), 60, 85)]
    return B.SynthUnit(
        key=key, tb_offset=tb_offset, code=bytes(c), guest_bytes=guest_bytes, icount=4,
        vaddr=vaddr, jmp_insn_offset=(44, 0xFFFF), jmp_reset_offset=(48, 0xFFFF),
        meta={"insns": insns, "sites": sites, "pool": pool, "ldst": ldst, "opmarks": opmarks,
              "static_exits": static_exits, "insn0_start": 11, "body_end": 85,
              "ldst_end": 103, "pool_start": 104})


def rich_units(n: int, *, start: int = 64, **kw: Any) -> list[B.SynthUnit]:
    """``n`` distinct rich TBs back to back (distinct page_addr0, same page)."""
    base_key = kw.pop("key", B.DEFAULT_KEY)
    out = []
    off = B.round_up(start, B.DEFAULT_ICACHE_LINESIZE)
    for i in range(n):
        key = replace(base_key, page_addr0=base_key.page_addr0 + 8 * i)
        u = rich_tb(off, key=key, **kw)
        out.append(u)
        off = B.round_up(B.resolve_unit(u).end_offset, B.DEFAULT_ICACHE_LINESIZE)
    return out


def with_meta(u: B.SynthUnit, **changes: Any) -> B.SynthUnit:
    m = dict(u.meta)
    m.update(changes)
    return replace(u, meta=m)


def write_rich_pair(units: list[B.SynthUnit], directory: str, *, name: str = "rich",
                    sc_header: dict | None = None, **scm_kw: Any) -> tuple[str, str]:
    scm_kw.setdefault("maps", RICH_MAPS)
    scm_kw.setdefault("helpers", RICH_HELPERS)
    return B.build_pair(units, directory, name=name, sc_header=sc_header, **scm_kw)


__all__ = ["rich_tb", "rich_units", "with_meta", "write_rich_pair", "RICH_MAPS", "RICH_HELPERS",
           "PLUGIN_FUNC", "PLUGIN_DATA", "LOOKUP_FUNC", "LDST_FUNC"]
