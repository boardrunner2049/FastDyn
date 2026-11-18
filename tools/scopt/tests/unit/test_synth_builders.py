"""Synthetic builders (WP4): the .sc and .scm they produce agree with each other,
with QEMU's layout rules, and with the independent critic parser."""

from __future__ import annotations

import pickle
import subprocess
import sys
from dataclasses import replace

import pytest

from scopt.canon import fnv1a64
from scopt.formats import layouts as L
from scopt.formats import scm_v1 as S
from scopt.formats.enums import Encoding, SiteKind
from scopt.formats.sc_v5 import ScHeader, ScRecord
from tests.synth import builders as B


def _decode_sleb(buf: bytes, pos: int) -> tuple[int, int]:
    v = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        v |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            if b & 0x40:
                v -= 1 << shift
            return v, pos


def _decode_search(buf: bytes, icount: int) -> tuple[list[tuple[int, int, int, int]], int]:
    """cpu_unwind_data_from_tb's walk (PCREL): rows of (w0, w1, w2, host_end)."""
    rows, data, it, pos = [], [0, 0, 0], 0, 0
    for _ in range(icount):
        for j in range(3):
            d, pos = _decode_sleb(buf, pos)
            data[j] += d
        d, pos = _decode_sleb(buf, pos)
        it += d
        rows.append((*data, it))
    return rows, pos


def test_sleb128_matches_qemu_encoding():
    assert B.encode_sleb128(0) == b"\x00"
    assert B.encode_sleb128(63) == b"\x3f"
    assert B.encode_sleb128(64) == b"\xc0\x00"
    assert B.encode_sleb128(-1) == b"\x7f"
    assert B.encode_sleb128(-64) == b"\x40"
    assert B.encode_sleb128(-65) == b"\xbf\x7f"
    for v in (0, 1, 2, 100, 1000, -2, -1000, 1 << 40, -(1 << 40)):
        assert _decode_sleb(B.encode_sleb128(v), 0) == (v, len(B.encode_sleb128(v)))


def test_build_sc_layout_and_records(tmp_path):
    units = B.simple_units(4)
    p = tmp_path / "s.sc"
    data = B.build_sc(units, path=str(p))
    assert p.read_bytes() == data
    h = ScHeader.from_bytes(data)
    assert h.checksum_ok and h.magic == L.SC_MAGIC and h.version == 5 and h.record_count == 4
    assert h.code_address == B.DEFAULT_CODE_ADDRESS and h.tb_size == 152
    assert len(data) == L.SC_CODE_OFFSET + h.code_size + 4 * 104
    prev_end = 0
    for i, u in enumerate(units):
        r = ScRecord.from_bytes(data[h.records_offset + 104 * i:])
        assert r.checksum_ok and r.committed == L.SC_RECORD_COMMIT
        assert r.tb_offset == u.tb_offset and r.tb_offset % 64 == 0 and r.tb_offset >= prev_end
        assert r.tc_offset - r.tb_offset == 192
        assert r.end_offset % 16 == 0 and r.pc == 0 and r.key == u.key
        assert r.guest_hash == fnv1a64(u.guest_bytes) and r.guest_size == len(u.guest_bytes)
        code = data[L.SC_CODE_OFFSET + r.tc_offset:][: r.tc_size]
        assert code == u.code
        # search data at tc+tc_size decodes to the unit's insns, last host_end == body_end
        tail = data[L.SC_CODE_OFFSET + r.tc_offset + r.tc_size: L.SC_CODE_OFFSET + r.end_offset]
        rows, n = _decode_search(tail, r.icount)
        assert [(*i_.words, i_.host_end) for i_ in u.meta["insns"]] == rows
        assert rows[-1][3] == u.meta["body_end"]
        assert r.tc_offset + r.tc_size + n <= r.end_offset < r.tc_offset + r.tc_size + n + 16
        # the TB struct area is not in the file (QEMU pwrites only [tc, end))
        assert not any(data[L.SC_CODE_OFFSET + r.tb_offset: L.SC_CODE_OFFSET + r.tc_offset])
        prev_end = r.end_offset


def test_build_sc_tampering_options():
    units = B.simple_units(3)
    good = B.build_sc(units)
    h = ScHeader.from_bytes(good)
    torn = B.build_sc(units, record_count=5, extra_records=b"\xff" * 50)
    assert ScHeader.from_bytes(torn).record_count == 5 and ScHeader.from_bytes(torn).checksum_ok
    assert torn.endswith(b"\xff" * 50)
    bad_hdr = B.build_sc(units, {"checksum": 1})
    assert not ScHeader.from_bytes(bad_hdr).checksum_ok
    u1 = replace(units[1], meta={**units[1].meta, "record_checksum": 0, "committed": 0})
    t = B.build_sc([units[0], u1, units[2]])
    r1 = ScRecord.from_bytes(t[h.records_offset + 104:])
    assert not r1.checksum_ok and r1.committed == 0
    with pytest.raises(ValueError, match="overlap"):
        B.build_sc([units[0], replace(units[1], tb_offset=units[0].tb_offset + 64)])


def test_build_sc_matches_critic_parser(tmp_path, scparse_path):
    units = B.simple_units(5)
    p = tmp_path / "c.sc"
    B.build_sc(units, path=str(p))
    out = tmp_path / "c.pkl"
    res = subprocess.run([sys.executable, str(scparse_path), str(p), str(out)],
                         capture_output=True, text=True, check=True)
    assert "hdr_ck_ok True" in res.stdout and "bad records []" in res.stdout
    assert "tc-tb deltas Counter({192: 5})" in res.stdout
    code_addr, prol_end, recs = pickle.loads(out.read_bytes())
    assert code_addr == B.DEFAULT_CODE_ADDRESS and prol_end == 45
    assert [r["tb"] for r in recs] == [u.tb_offset for u in units]


def test_scm_binds_to_sc():
    """§2.5.8 binding conditions hold between build_sc and build_scm outputs."""
    units = B.simple_units(4)
    sc = B.build_sc(units)
    scm = S.parse_scm(B.build_scm(units))
    h = ScHeader.from_bytes(sc)
    ri = scm.run_info
    assert (ri.code_address, ri.code_size, ri.prologue_end, ri.runtime_signature) == (
        h.code_address, h.code_size, h.prologue_end, h.runtime_signature)
    assert ri.tb_struct_size == h.tb_size and ri.prologue == B.SYNTH_PROLOGUE
    for slot, tb in scm.tbs.items():
        rec = sc[h.records_offset + 104 * slot: h.records_offset + 104 * (slot + 1)]
        assert tb.record == rec
        r = ScRecord.from_bytes(rec)
        arena = sc[L.SC_CODE_OFFSET + r.tc_offset: L.SC_CODE_OFFSET + r.end_offset]
        assert tb.code_fnv64 == fnv1a64(arena)
        assert fnv1a64(tb.guest_bytes) == r.guest_hash
        assert tb.code_size == r.tc_size and tb.icount == r.icount
        assert tb.insns[0].words[0] == tb.vaddr & ((1 << ri.target_page_bits) - 1)


def test_simple_tb_sites_match_capstone():
    """Every reference in simple_tb's code is a captured site with the right value."""
    capstone = pytest.importorskip("capstone")
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    for irq in (True, False):
        u = B.simple_tb(0x1000, irq_check=irq)
        tb = S.parse_scm(B.build_scm([u])).tbs[0]
        base = B.DEFAULT_CODE_ADDRESS + 0x1000 + 192
        insns = list(md.disasm(u.code, base))
        assert sum(i.size for i in insns) == len(u.code)  # tiles exactly
        starts = {i.address - base: i for i in insns}
        refs = {}
        for i in insns:
            off = i.address - base
            if i.mnemonic in ("jmp", "jl", "call"):
                refs[off] = int(i.op_str, 16)
            elif i.mnemonic == "lea" and "rip" in i.op_str:
                refs[off] = i.address + i.size + i.disp
        assert {s.insn_off: s.value for s in tb.sites} == refs
        for s in tb.sites:
            ins = starts[s.insn_off]
            assert s.insn_off < s.field_off and s.field_off + s.width <= s.insn_off + ins.size
            assert s.final_field == int.from_bytes(u.code[s.field_off: s.field_off + 4], "little")
        kinds = sorted(SiteKind(s.kind).name for s in tb.sites)
        assert kinds.count("EXIT_TB_RET_JMP") == (2 if irq else 1)
        assert ("LABEL_JCC" in kinds) == irq
        assert all(s.value == B.DEFAULT_CODE_ADDRESS + B.SYNTH_TB_RET_OFF
                   for s in tb.sites if s.kind == SiteKind.EXIT_TB_RET_JMP)
        assert {s.aux for s in tb.sites if s.kind == SiteKind.MOVI_EXIT_TB_PTR} == (
            {0, 3} if irq else {0})
        assert all(s.enc == Encoding.RIPREL32 for s in tb.sites
                   if s.kind == SiteKind.MOVI_EXIT_TB_PTR)
        assert tb.insn0_start == (11 if irq else 0) and tb.body_end == len(u.code)


def test_synthetic_prologue_decodes():
    capstone = pytest.importorskip("capstone")
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    ins = list(md.disasm(B.SYNTH_PROLOGUE, 0))
    assert sum(i.size for i in ins) == len(B.SYNTH_PROLOGUE)
    at = {i.address: i.mnemonic for i in ins}
    assert at[B.SYNTH_EPILOGUE_OFF] == "xor" and at[B.SYNTH_TB_RET_OFF] == "add"


def test_corruption_helpers():
    units = B.simple_units(3)
    x = B.build_scm(units)
    f = S.parse_scm(x)
    # drop_site: still a valid file, one site fewer in that slot only
    y = S.parse_scm(B.drop_site(x, 1, 0))
    assert y.rejected_chunks == () and len(y.tbs[1].sites) == len(f.tbs[1].sites) - 1
    assert y.tbs[1].sites == f.tbs[1].sites[1:] and y.tbs[0] == f.tbs[0]
    with pytest.raises(KeyError):
        B.drop_site(x, 9, 0)
    # swap_run: parses cleanly but no longer matches the .sc header
    z = S.parse_scm(B.swap_run(x, {"code_address": 0x7FFF00000000, "runtime_signature": 1}))
    assert z.rejected_chunks == () and z.run_info.code_address == 0x7FFF00000000
    assert z.tbs == f.tbs
    # flip_byte
    assert B.flip_byte(b"\x00\x01", 1, 0x80) == b"\x00\x81"
    # truncate_chunk keeps exactly the prefix
    tab = B.chunk_table(x)
    assert B.truncate_chunk(x, 2, 10) == x[: tab[2]["offset"] + 10]
    with pytest.raises(ValueError):
        B.truncate_chunk(x, 2, tab[2]["length"])


def test_no_meta_unit_and_partial_capture():
    units = B.simple_units(3)
    u = replace(units[1], meta={**units[1].meta, "scm": False})
    f = S.parse_scm(B.build_scm([units[0], u, units[2]]))
    assert sorted(f.tbs) == [0, 2] and f.end.committed == 2
    assert [t.commit_seq for t in f.tbs.values()] == [0, 1]
