""".scm v1 reader/writer (design §2.5; WP4 acceptance)."""

from __future__ import annotations

import struct
from dataclasses import replace

import pytest

from scopt.canon import fnv1a64
from scopt.formats import layouts as L
from scopt.formats import scm_v1 as S
from scopt.formats.enums import ChunkType, Encoding, MapsTag, OpMarkKind, SiteKind, TbFlag
from tests.synth import builders as B


def _rich_units():
    """Units exercising every TB array, a NOIRQ TB and a TB without metadata."""
    us = B.simple_units(3)
    code = bytes(range(64)) * 2
    rich = B.SynthUnit(
        key=replace(B.DEFAULT_KEY, page_addr0=0x2809A000), tb_offset=0, code=code,
        guest_bytes=b"\x01\x02\x03",  # odd length: guest_bytes needs a pad
        icount=3, vaddr=0x2809A000,
        meta={
            "insn0_start": 11, "body_end": 80, "ldst_end": 100, "pool_start": 112,
            "sites": [{"field_off": 13, "kind": SiteKind.HELPER_CALL, "enc": Encoding.POOL_INDIRECT,
                       "aux": 1, "value": 0x555555601234, "insn_idx": 0},
                      {"field_off": 90, "kind": SiteKind.LDST_RET_JMP, "insn_idx": 2,
                       "value": 0x7FFF6C000123, "final_field": 0xDEADBEEF}],
            "pool": [{"user_field_off": 13, "slot_off": 112, "data0": 0x555555601234}],
            "ldst": [{"jcc_field": (40, None), "slow_start": 80, "slow_end": 100,
                      "raddr_off": 44, "oi": 0x22, "is_ld": 1}],
            "opmarks": [{"host_off": 11, "kind": OpMarkKind.CALL, "insn_idx": 0, "helper_id": 1},
                        {"host_off": 30, "kind": OpMarkKind.QEMU_LD, "insn_idx": 1}],
            "static_exits": [{"slot": 0, "direct": 1, "insn_idx": 2, "dest_vaddr": 0x2809A010},
                             {"slot": 0xFF, "direct": 0, "insn_idx": 2,
                              "dest_vaddr": 0x2809B000}],
            "attempts": 2,
        })
    noirq = B.simple_tb(0, irq_check=False, key=replace(B.DEFAULT_KEY, page_addr0=0x2809C000))
    nometa = B.simple_tb(0, key=replace(B.DEFAULT_KEY, page_addr0=0x2809D000))
    nometa = replace(nometa, meta={**nometa.meta, "scm": False})
    return B.auto_place(us + [rich, noirq, nometa])


HELPERS = [{"name": "lookup_tb_ptr", "tcg_flags": 0x10, "typemask": 0x3B, "func": 0x5555556000A0},
           {"name": b"", "tcg_flags": 0, "typemask": 0x7, "func": 0x7FFFF0001000},
           {"name": "st_i32", "tcg_flags": 0, "typemask": 0x1, "func": 0x555555601234}]


@pytest.fixture
def rich_scm() -> bytes:
    return B.build_scm(_rich_units(), helpers=HELPERS)


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["plain", "rich", "no_end", "unknown_chunks", "first_slot"])
def test_round_trip_bytes(variant, tmp_path):
    units = B.simple_units(2) if variant != "rich" else _rich_units()
    kw = {}
    if variant == "rich":
        kw["helpers"] = HELPERS
    if variant == "no_end":
        kw["with_end"] = False
    if variant == "unknown_chunks":
        kw["extra_chunks"] = [(99, b"future"), (int(ChunkType.OPS), b"\x01" * 13), (2, b"")]
    if variant == "first_slot":
        kw["first_slot"] = 7
    x = B.build_scm(units, **kw)
    f = S.parse_scm(x)
    assert f.rejected_chunks == ()
    assert S.encode_scm(f) == x
    # write(read(x)) == x through the file API too
    p = tmp_path / "a.scm"
    p.write_bytes(x)
    q = tmp_path / "b.scm"
    S.write_scm(q, S.read_scm(p))
    assert q.read_bytes() == x
    assert S.read_scm(q).tbs == f.tbs


def test_round_trip_reencodes_every_body(rich_scm):
    """Each decoded body re-encodes to exactly its payload (not just a byte copy)."""
    f = S.parse_scm(rich_scm)
    for c in B.chunk_table(rich_scm):
        ch = f.chunks[c["seq"]]
        payload = rich_scm[c["offset"] + 16: c["offset"] + 16 + c["payload_len"]]
        assert ch.body.encode() + b"\0" * ch.pad_in_payload == payload
        assert not isinstance(ch.body, S.RawChunkBody)


def test_round_trip_make_scm_and_modification(rich_scm):
    f = S.parse_scm(rich_scm)
    g = S.make_scm([c.body for c in f.chunks], f.header)
    assert S.encode_scm(g) == rich_scm
    tb = f.tbs[3]
    tb2 = replace(tb, attempts=9, sites=tb.sites[:1])
    seq = next(c.seq for c in f.chunks if c.body is tb)
    h = S.parse_scm(S.encode_scm(S.replace_body(f, seq, tb2)))
    assert h.tbs[3] == tb2 and h.tbs[3].attempts == 9 and len(h.tbs[3].sites) == 1
    assert h.rejected_chunks == ()


def test_round_trip_tolerated_quirks():
    """payload_len including the 8-pad and non-zero framing pad both round-trip exactly."""
    x = B.build_scm(_rich_units(), helpers=HELPERS)
    f = S.parse_scm(x)
    # Re-frame the odd-length TB chunk (rich unit, slot 3) with the pad inside payload_len,
    # and a helper chunk with a non-zero framing pad.
    chunks = []
    for ch in f.chunks:
        if isinstance(ch.body, S.TbChunk) and ch.body.slot == 3:
            ch = replace(ch, pad_in_payload=L.align8(len(ch.body.encode())) - len(ch.body.encode()))
            assert ch.pad_in_payload == 5
        if isinstance(ch.body, S.HelperInfo) and ch.body.helper_id == 0:
            n = L.align8(len(ch.body.encode())) - len(ch.body.encode())
            assert n
            ch = replace(ch, frame_pad=b"\xAA" * n)
        chunks.append(ch)
    y = S.encode_scm((f.header, chunks))
    assert y != x
    g = S.parse_scm(y)
    assert g.rejected_chunks == ()
    assert g.tbs == f.tbs and g.helpers == f.helpers
    assert any(w.startswith("nonzero_frame_pad@") for w in g.warnings)
    assert S.encode_scm(g) == y


# --------------------------------------------------------------------------
# Byte layout, independently of the encoder
# --------------------------------------------------------------------------

def _hand_chunk(t, seq, payload):
    pad = (-len(payload)) % 8
    body = struct.pack("<IIQ", t, len(payload), seq) + payload + b"\0" * pad
    return body + struct.pack("<Q", fnv1a64(body))


def test_hand_assembled_file_decodes():
    hdr = b"QSCMETA1" + struct.pack("<IIIIQ3Q", 1, 64, 1, 777, 123456789, 0, 0, 0)
    hdr += struct.pack("<Q", fnv1a64(hdr))
    assert len(hdr) == 64
    prologue = b"\x55\xc3\xcc"
    run_info = struct.pack("<II9QIIIIIIQQQQII", 1, 1, 0x7FFF6C000000, 0x10000, 3, 1, 2,
                           0x1122334455667788, 5, 0x400, 0xFC00, 64, 152, 4096, 10, 3, 0xABC,
                           1, 2, 3, 4, 1, len(prologue)) + prologue
    helper = struct.pack("<IIIIQ", 0, 0x10, 0x3B, 3, 0x5555_5555_0000) + b"foo"
    record = bytes(range(104))
    tb_hdr = struct.pack("<IIQ104sQQIIIIIIIHHIIIIIIQ", 1, 5, 0, record, 0x2000_0100, 0xFEED,
                         1, 1, 11, 30, 30, 0xFFFFFFFF, 30, 1, 3, 1, 0, 1, 1, 1, 2, 0)
    assert len(tb_hdr) == 200
    insn = struct.pack("<3QII", 0x100, 0, 0, 11, 30)
    site = struct.pack("<IIBBHIQII", 21, 18, 4, 3, 9, 0, 0x7FFF6C000040, 0, 0xFFFFFFE7)
    ldst = struct.pack("<2IIIIIIB3x", 40, 0xFFFFFFFF, 30, 50, 44, 0x22, 0, 1)
    opm = struct.pack("<IHHII", 18, 4, 0, 0xFFFFFFFF, 0)
    sx = struct.pack("<BBHIQ", 0xFF, 0, 0, 0, 0x2000_0400)
    tb = tb_hdr + insn + site + ldst + opm + sx + b"\x00\xbf"
    end = struct.pack("<6QI", 1, 2, 3, 4, 5, 6, 1)
    data = (hdr + _hand_chunk(1, 0, run_info) + _hand_chunk(3, 1, helper) + _hand_chunk(4, 2, tb)
            + _hand_chunk(10, 3, end))
    f = S.parse_scm(data)
    assert f.rejected_chunks == () and not f.incomplete and f.clean
    assert f.header.pid == 777 and f.header.created_realtime_ns == 123456789
    ri = f.run_info
    assert (ri.code_address, ri.code_size, ri.prologue_end, ri.tb_ret_off, ri.epilogue_off) == (
        0x7FFF6C000000, 0x10000, 3, 1, 2)
    assert ri.runtime_signature == 0x1122334455667788 and ri.header_record_count_at_load == 5
    assert (ri.icache_linesize, ri.tb_struct_size, ri.target_page_bits, ri.cpuinfo) == (
        64, 152, 10, 0xABC)
    assert (ri.exe_st_dev, ri.exe_st_ino, ri.exe_st_size, ri.exe_st_mtim_ns, ri.flags) == (
        1, 2, 3, 4, 1)
    assert ri.prologue == prologue
    assert f.helpers[0].name_str == "foo" and f.helpers[0].func == 0x5555_5555_0000
    t = f.tbs[5]
    assert t.record == record and t.vaddr == 0x2000_0100 and t.code_fnv64 == 0xFEED
    assert (t.insn0_start, t.body_end, t.ldst_end, t.pool_start_or_none, t.code_size) == (
        11, 30, 30, None, 30)
    assert t.insns == (S.ScmInsn((0x100, 0, 0), 11, 30),)
    assert t.sites[0].kind == SiteKind.MOVI_EXIT_TB_PTR and t.sites[0].enc == Encoding.RIPREL32
    assert t.ldst[0].jcc_field == (40, 0xFFFFFFFF) and t.ldst[0].is_ld == 1
    assert t.opmarks[0].kind == OpMarkKind.EXIT_TB
    assert t.static_exits[0].slot == 0xFF and t.static_exits[0].dest_vaddr == 0x2000_0400
    assert t.guest_bytes == b"\x00\xbf" and t.tb_flags == TbFlag.IRQ_CHECK
    assert (f.end.committed, f.end.helpers, f.end.clean) == (1, 6, 1)
    assert S.encode_scm(f) == data
    m = t.model_arrays()
    assert m["ldst"][0].jcc_fields == (40, None) and m["opmarks"][0].helper_id is None
    assert m["sites"][0].insn_idx == 0 and m["insns"][0].index == 0


# --------------------------------------------------------------------------
# Reader rules: header, prefix semantics, incomplete, unknown, malformed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d[:63], "shorter"),
    (lambda d: b"XSCMETA1" + d[8:], "magic"),
    (lambda d: B.flip_byte(d, 20), "checksum"),
    (lambda d: B.flip_byte(d, 60), "checksum"),
])
def test_bad_file_header_rejects_whole_file(rich_scm, mutate, msg):
    with pytest.raises(S.ScmHeaderError, match=msg):
        S.parse_scm(mutate(rich_scm))


def test_bad_version_rejected(rich_scm):
    h = S.ScmFileHeader(version=2)
    with pytest.raises(S.ScmHeaderError, match="version"):
        S.parse_scm(h.encode() + rich_scm[64:])


def _tb_seqs(scm_bytes):
    return {c["seq"]: c for c in B.chunk_table(scm_bytes)}


def test_bad_trailer_drops_chunk_and_suffix(rich_scm):
    full = S.parse_scm(rich_scm)
    tab = B.chunk_table(rich_scm)
    tb_chunks = [c for c in tab if c["type"] == ChunkType.TB]
    victim = tb_chunks[2]  # third TB chunk
    for how in ("trailer", "payload"):
        if how == "trailer":
            bad = B.corrupt_trailer(rich_scm, victim["seq"])
        else:
            bad = B.flip_byte(rich_scm, victim["offset"] + 16 + 130)  # inside the payload
        f = S.parse_scm(bad)
        assert [c.seq for c in f.chunks] == list(range(victim["seq"]))
        assert f.rejected_chunks[0].reason == "bad_trailer"
        assert f.rejected_chunks[0].seq == victim["seq"] and f.rejected_chunks[0].prefix
        # every later chunk is reported, all with prefix semantics
        assert [r.seq for r in f.rejected_chunks] == [c["seq"] for c in tab[victim["seq"]:]]
        assert all(r.prefix for r in f.rejected_chunks)
        assert all(r.reason == "after_rejected" for r in f.rejected_chunks[1:])
        assert set(f.tbs) == {full.chunks[c["seq"]].body.slot for c in tb_chunks[:2]}
        assert f.incomplete and f.end is None
        assert f.valid_size == victim["offset"]
        # the earlier TB chunks are untouched
        for s in f.tbs:
            assert f.tbs[s] == full.tbs[s]
        # write(read(bad)) == the valid prefix
        assert S.encode_scm(f) == rich_scm[: victim["offset"]]


def test_truncated_chunk_is_a_torn_tail(rich_scm):
    tab = B.chunk_table(rich_scm)
    tb = [c for c in tab if c["type"] == ChunkType.TB][-1]
    for keep in (0, 5, 16, 100, tb["length"] - 1):
        cut = B.truncate_chunk(rich_scm, tb["seq"], keep)
        f = S.parse_scm(cut)
        assert len(f.chunks) == tb["seq"]
        assert len(f.rejected_chunks) == (0 if keep == 0 else 1)
        if keep:
            r = f.rejected_chunks[0]
            assert r.prefix and r.reason == ("truncated_header" if keep < 16 else "truncated")
            assert r.length == keep
        assert f.incomplete


def test_seq_gap_rejects_suffix(rich_scm):
    tab = B.chunk_table(rich_scm)
    k = 4
    # (a) a chunk is missing entirely
    f = S.parse_scm(B.drop_chunk(rich_scm, k))
    assert len(f.chunks) == k
    assert f.rejected_chunks[0].reason == f"seq_gap:expected {k} got {k + 1}"
    assert len(f.rejected_chunks) == len(tab) - k - 1 and f.incomplete
    # (b) a chunk with a valid trailer but the wrong seq
    f = S.parse_scm(B.set_chunk_seq(rich_scm, k, 40))
    assert len(f.chunks) == k and f.rejected_chunks[0].reason.startswith("seq_gap")
    assert len(f.rejected_chunks) == len(tab) - k
    # (c) the first chunk must have seq 0
    f = S.parse_scm(B.set_chunk_seq(rich_scm, 0, 1))
    assert f.chunks == () and f.run_info is None and f.tbs == {}


def test_missing_end_is_incomplete_but_tbs_usable():
    units = B.simple_units(4)
    full = S.parse_scm(B.build_scm(units))
    x = B.build_scm(units, with_end=False)
    f = S.parse_scm(x)
    assert f.incomplete and not f.clean and f.rejected_chunks == ()
    assert f.tbs == full.tbs and len(f.tbs) == 4
    # dropping END (last chunk) from a complete file gives the same result
    tab = B.chunk_table(B.build_scm(units))
    g = S.parse_scm(B.drop_chunk(B.build_scm(units), tab[-1]["seq"]))
    assert g.incomplete and g.tbs == full.tbs and g.rejected_chunks == ()


def test_chunks_after_end_are_rejected():
    x = B.build_scm(B.simple_units(2))
    y = B.append_raw_chunk(x, int(ChunkType.TB), b"\0" * 8)
    f = S.parse_scm(y)
    assert not f.incomplete
    assert [r.reason for r in f.rejected_chunks] == ["after_end"]


def test_unknown_and_tier1_chunks_are_skipped():
    x = B.build_scm(B.simple_units(2),
                    extra_chunks=[(99, b"x" * 5), (int(ChunkType.PLUGIN_CBS), b"y"), (2, b"")])
    f = S.parse_scm(x)
    assert f.rejected_chunks == () and not f.incomplete
    assert [c.type for c in f.skipped_chunks] == [99, int(ChunkType.PLUGIN_CBS), 2]
    assert len(f.tbs) == 2


def test_malformed_payload_rejects_only_that_chunk():
    units = B.simple_units(3)
    x = B.build_scm(units)
    f0 = S.parse_scm(x)
    tab = B.chunk_table(x)
    tbc = [c for c in tab if c["type"] == ChunkType.TB][1]
    # a TB payload with a wrong length but a valid trailer
    payload = x[tbc["offset"] + 16: tbc["offset"] + 16 + tbc["payload_len"]]
    for bad_payload, reason in (
            (payload + b"\x01" * 8, "payload_len"),
            (payload[:150], "payload_len"),
            # v3 requires a nonempty native island trailer and a span boundary.
            (struct.pack("<I", 3) + payload[4:], "tb_chunk_v3_native_layout"),
            (struct.pack("<I", 4) + payload[4:], "tb_chunk_v4_native_layout"),
            (struct.pack("<I", 5) + payload[4:], "unsupported_tb_chunk_version:5"),
            (payload[:166] + struct.pack("<H", 4) + payload[168:], "insn_start_words:4")):
        framed = S.frame_chunk(int(ChunkType.TB), tbc["seq"], bad_payload)
        y = x[: tbc["offset"]] + framed + x[tbc["offset"] + tbc["length"]:]
        f = S.parse_scm(y)
        assert len(f.rejected_chunks) == 1, reason
        r = f.rejected_chunks[0]
        assert not r.prefix and r.reason.startswith(reason) and r.seq == tbc["seq"]
        bad_slot = f0.chunks[tbc["seq"]].body.slot
        assert bad_slot not in f.tbs and len(f.tbs) == 2
        assert not f.incomplete
        assert S.encode_scm(f) == y  # lossless even with a malformed chunk


def test_duplicate_run_info_and_slot_conflicts():
    units = B.simple_units(2)
    x = B.build_scm(units, with_end=False)
    f = S.parse_scm(x)
    ri_payload = f.run_info.encode()
    y = B.append_raw_chunk(x, int(ChunkType.RUN_INFO), ri_payload)
    g = S.parse_scm(y)
    assert [r.reason for r in g.rejected_chunks] == ["duplicate_run_info"]
    assert g.run_info == f.run_info
    # equal duplicate TB chunk: kept, warned
    tb0 = f.tbs[0]
    z = B.append_raw_chunk(x, int(ChunkType.TB), tb0.encode())
    h = S.parse_scm(z)
    assert h.rejected_chunks == () and "duplicate_slot_equal:0" in h.warnings
    # conflicting duplicate: slot dropped (fail closed)
    z2 = B.append_raw_chunk(x, int(ChunkType.TB), replace(tb0, attempts=7).encode())
    h2 = S.parse_scm(z2)
    assert [r.reason for r in h2.rejected_chunks] == ["duplicate_slot_conflict"]
    assert 0 not in h2.tbs and 1 in h2.tbs


def test_capture_contents(rich_scm):
    units = _rich_units()
    f = S.parse_scm(rich_scm)
    assert f.header.capture_level == 1 and f.clean
    assert sorted(f.tbs) == [0, 1, 2, 3, 4]  # slot 5 has no metadata
    assert f.end.committed == 5 and f.end.helpers == 3
    assert [h.name_str for h in f.helpers.values()] == ["lookup_tb_ptr", None, "st_i32"]
    assert [m.tag for m in f.maps] == [MapsTag.FIRST_COMMIT, MapsTag.EXIT]
    assert f.maps_by_tag(MapsTag.EXIT).text.count("\n") == 3
    assert f.ramblocks.rows[0].name == "mps.ram"
    rich = f.tbs[3]
    assert rich.attempts == 2 and rich.pool_start == 112 and rich.ldst_end == 100
    assert rich.guest_bytes == b"\x01\x02\x03" and len(rich.static_exits) == 2
    assert rich.sites[1].final_field == 0xDEADBEEF
    assert rich.sites[0].final_field == int.from_bytes(units[3].code[13:17], "little")
    assert not f.tbs[4].flags & TbFlag.IRQ_CHECK  # CF_NOIRQ unit
    assert all(f.tbs[i].flags & TbFlag.IRQ_CHECK for i in (0, 1, 2))
    m = rich.model_arrays()
    assert m["static_exits"][1].direct is False and m["ldst"][0].jcc_fields == (40, None)
    assert m["opmarks"][0].helper_id == 1


def test_model_arrays_rejects_unknown_enum():
    tb = S.parse_scm(B.build_scm(B.simple_units(1))).tbs[0]
    bad = replace(tb, sites=(replace(tb.sites[0], kind=0x55),))
    with pytest.raises(ValueError):
        bad.model_arrays()


# --------------------------------------------------------------------------
# Merge (§2.5.8)
# --------------------------------------------------------------------------

def test_merge_disjoint_and_equal_overlap(tmp_path):
    units = B.simple_units(5)
    a = S.parse_scm(B.build_scm(units[:3], all_units=units), path="a.scm")
    b = S.parse_scm(B.build_scm(units[3:], first_slot=3, all_units=units,
                                run_info={"loaded_max_end": 0x400}), path="b.scm")
    m = S.merge_scm([a, b])
    assert list(m) == [0, 1, 2, 3, 4]
    assert m[4] == b.tbs[4]
    src = S.merge_scm_sources([a, b])
    assert src[1][1] is a and src[3][1] is b
    # same capture twice (byte-equal overlap) is fine; paths work too
    p = tmp_path / "a.scm"
    p.write_bytes(S.encode_scm(a))
    assert S.merge_scm([a, str(p), b]) == m


def test_merge_conflict():
    units = B.simple_units(3)
    a = S.parse_scm(B.build_scm(units), path="a.scm")
    other = B.simple_units(3, start=0x1000)  # same slots, different TBs
    b = S.parse_scm(B.build_scm(other), path="b.scm")
    with pytest.raises(S.SnapshotMergeConflict) as ei:
        S.merge_scm([a, b])
    assert ei.value.reason == "snapshot_merge_conflict" == S.SNAPSHOT_MERGE_CONFLICT
    assert ei.value.slot == 0 and (ei.value.first, ei.value.second) == ("a.scm", "b.scm")
    # a single differing field (commit_seq) is already a conflict
    c = S.parse_scm(B.build_scm(units[2:], first_slot=2), path="c.scm")
    with pytest.raises(S.SnapshotMergeConflict) as ei:
        S.merge_scm([a, c])
    assert ei.value.slot == 2


def test_slice_scm():
    x = B.build_scm(_rich_units(), helpers=HELPERS)
    f = S.parse_scm(x)
    sl = S.slice_scm(f, [1, 3], keep_maps=False)
    assert sorted(sl.tbs) == [1, 3] and sl.tbs[3] == f.tbs[3]
    assert sl.helpers == f.helpers and sl.run_info == f.run_info and sl.end == f.end
    assert all(m.data == b"" and m.truncated for m in sl.maps)
    assert sl.rejected_chunks == () and S.encode_scm(S.parse_scm(S.encode_scm(sl))) == \
        S.encode_scm(sl)
    for slot in (1, 3):
        assert sl.tbs[slot].encode() == f.tbs[slot].encode()


# --------------------------------------------------------------------------
# Real capture (once WP3 produces one)
# --------------------------------------------------------------------------

def _real_scms(tests_data):
    import os
    paths = sorted(tests_data.glob("*.scm"))
    env = os.environ.get("SCOPT_REAL_SCM")
    if env:
        paths += [p for p in map(__import__("pathlib").Path, env.split(os.pathsep)) if p.exists()]
    return paths


@pytest.mark.real
def test_real_scm_parses_with_no_rejected_chunks(tests_data):
    paths = _real_scms(tests_data)
    if not paths:
        pytest.skip("no real .scm yet (tests/data/*.scm or SCOPT_REAL_SCM); needs WP3")
    for p in paths:
        f = S.read_scm(p)
        assert f.rejected_chunks == (), (p, f.rejected_chunks[:3])
        assert f.run_info is not None and f.tbs, p
        assert f.warnings == (), (p, f.warnings)
        for tb in f.tbs.values():
            tb.model_arrays()  # every enum value is in the frozen numbering
            assert tb.code_size == tb.sc_record.tc_size and tb.icount == tb.sc_record.icount
            assert tb.guest_fnv64() == tb.sc_record.guest_hash
        if f.end is not None:
            assert S.encode_scm(f) == p.read_bytes()[: f.valid_size]
