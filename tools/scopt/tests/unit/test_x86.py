"""WP2: x86-64 sweep and reference extraction on hand-built byte sequences."""

from __future__ import annotations

import pytest

from scopt.decode.symbols import MapEntry, Maps
from scopt.decode.x86 import (Arena, RefKind, bad_insns, extract_refs, sweep, sweep_to_pool,
                              tiles)
from scopt.formats.enums import Encoding

BASE = 0x7FFF6C000940
ARENA = Arena(0x7FFF6C000000, 0x80000000)


def h(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


def test_sweep_tiles_and_boundaries():
    code = h("8b5df8 85db 0f8c26000000 c645fc01 488bfd ffe0")
    insns = sweep(code, BASE)
    assert [i.size for i in insns] == [3, 2, 6, 4, 3, 2]
    assert [i.mnemonic for i in insns] == ["mov", "test", "jl", "mov", "mov", "jmp"]
    assert tiles(insns, BASE, BASE + len(code))
    assert not bad_insns(insns)
    assert insns[2].branch_target == BASE + 5 + 6 + 0x26
    assert insns[2].is_jcc and not insns[5].is_jcc
    assert insns[5].reg_target == "rax"


def test_sweep_bad_bytes_continue():
    # 0x06 (push es) and 0x07 are invalid in 64-bit mode
    code = h("90 06 07 90")
    insns = sweep(code, 0x1000)
    assert [(i.address, i.bad) for i in insns] == [(0x1000, False), (0x1001, True),
                                                   (0x1002, True), (0x1003, False)]
    assert [i.mnemonic for i in bad_insns(insns)] == ["(bad)", "(bad)"]
    assert not tiles(insns, 0x1000, 0x1004)


def test_sweep_end_bound_and_straddle():
    code = h("488bfd 90")  # 3-byte mov then nop
    insns = sweep(code, 0x1000, 0x1002)  # end inside the mov: it straddles
    assert len(insns) == 1 and insns[0].end == 0x1003
    assert not tiles(insns, 0x1000, 0x1002)
    with pytest.raises(ValueError):
        sweep(code, 0x1000, 0x1005)


def test_sweep_to_pool_stops_at_lowest_rip_memory_target():
    # call [rip+0x11] at +0x29 -> slot at +0x40; lea rip into the struct is ignored
    pre = h("8b5df8 85db 0f8c26000000 c645fc01 8b5d38 448be3 4183e4fe 4489653c 83e301"
            "889d4d010000 488bfd")
    assert len(pre) == 0x29
    code = (pre + h("ff1511000000 ffe0 488d050bffffff e99bf6ffff 909090")
            + h("b0cf000000000000"))  # pool data that would decode as garbage
    assert len(code) == 0x48
    insns, pool = sweep_to_pool(code, BASE)
    assert pool == BASE + 0x40
    assert tiles(insns, BASE, pool)
    assert insns[-1].mnemonic == "nop" and insns[-1].end == pool


def test_sweep_to_pool_without_pool():
    code = h("488bfd ffe0")
    insns, pool = sweep_to_pool(code, 0x1000)
    assert pool == 0x1000 + len(code) and len(insns) == 2


def test_extract_refs_kinds_fields_values():
    maps = Maps([MapEntry(0x555555554000, 0x555556000000, "r-xp", path="/q"),
                 MapEntry(0x10000000, 0x10001000, "rw-p")])
    parts = [
        ("eb02", RefKind.JMP_REL8, Encoding.REL8, 1, 1),
        ("7405", RefKind.JCC_REL8, Encoding.REL8, 1, 1),
        ("0f8410000000", RefKind.JCC_REL32, Encoding.REL32, 2, 4),
        ("e900000000", RefKind.JMP_REL32, Encoding.REL32, 1, 4),
        ("e8f0ffffff", RefKind.CALL_REL32, Encoding.REL32, 1, 4),
        ("488d05f0ffffff", RefKind.RIP_LEA, Encoding.RIPREL32, 3, 4),
        ("ff1510000000", RefKind.RIP_CALL, Encoding.RIPREL32, 2, 4),
        ("ff2508000000", RefKind.RIP_JMP, Encoding.RIPREL32, 2, 4),
        ("4c8b2508000000", RefKind.RIP_MEM, Encoding.RIPREL32, 3, 4),
        ("48b8efcdab8967452301", RefKind.MOVABS, Encoding.IMM64, 2, 8),
        ("b800000010", RefKind.IMM32, Encoding.IMM32_ZX, 1, 4),  # mov eax, 0x10000000 (in maps)
        ("b812345678", None, None, 0, 0),  # mov eax, 0x78563412: not in arena/maps
        ("ffe0", RefKind.JMP_REG, Encoding.NONE, 0, 0),
        ("ffd1", RefKind.CALL_REG, Encoding.NONE, 0, 0),
        ("8b0425" + "00000010", RefKind.DISP32, Encoding.IMM32_ZX, 3, 4),  # mov eax,[0x10000000]
        ("8b4508", None, None, 0, 0),  # mov eax,[rbp+8]: disp8, never a ref
        ("ff6008", RefKind.JMP_MEM, Encoding.NONE, 0, 0),  # jmp [rax+8]
        ("ff5508", RefKind.CALL_MEM, Encoding.NONE, 0, 0),  # call [rbp+8]
    ]
    code = b"".join(h(p[0]) for p in parts)
    insns = sweep(code, BASE)
    assert len(insns) == len(parts) and tiles(insns, BASE, BASE + len(code))
    refs = extract_refs(insns, ARENA, maps)
    by_insn = {r.insn_addr: r for r in refs}
    assert len(by_insn) == len(refs)  # one ref per insn here
    for ins, (_hex, kind, enc, foff, width) in zip(insns, parts):
        r = by_insn.get(ins.address)
        if kind is None:
            assert r is None, ins.text()
            continue
        assert r is not None, ins.text()
        assert (r.kind, r.enc, r.field_addr - ins.address, r.width) == (kind, enc, foff, width), \
            ins.text()
    jmp8 = by_insn[insns[0].address]
    assert jmp8.value == insns[0].end + 2 and jmp8.region == "arena"
    jmp32 = by_insn[insns[3].address]
    assert jmp32.value == insns[3].end  # rel32 0: next insn
    lea = by_insn[insns[5].address]
    assert lea.value == insns[5].end - 0x10 and lea.reg == "rax"
    mov = by_insn[insns[9].address]
    assert mov.value == 0x0123456789ABCDEF and mov.region == ""
    imm = by_insn[insns[10].address]
    assert imm.value == 0x10000000 and imm.region == "maps"
    assert by_insn[insns[12].address].reg == "rax"


def test_extract_refs_sign_extended_imm32():
    # mov rax, imm32 (REX.W C7 /0) with 0x80000000 -> sign-extended
    insns = sweep(h("48c7c000000080"), 0x1000)
    maps = Maps([MapEntry(0xFFFFFFFF80000000, 0xFFFFFFFF80001000, "r--p")])
    refs = extract_refs(insns, None, maps)
    assert len(refs) == 1
    assert refs[0].enc == Encoding.IMM32_SX and refs[0].value == 0xFFFFFFFF80000000
    assert extract_refs(insns, None, None) == []
    # the zero-extended reading of a 32-bit mov is not sign-extended
    insns = sweep(h("b800000080"), 0x1000)
    assert extract_refs(insns, None, maps) == []
    assert extract_refs(insns, None, Maps([MapEntry(0x80000000, 0x80001000)]))[0].enc \
        == Encoding.IMM32_ZX


def test_extract_refs_tuple_arena_and_bad_skipped():
    insns = sweep(h("06 e900000000"), BASE)
    refs = extract_refs(insns, (ARENA.base, ARENA.size))
    assert [r.kind for r in refs] == [RefKind.JMP_REL32]
    assert refs[0].region == "arena"
