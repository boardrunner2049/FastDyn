"""Search-data codec and unwind (WP1; design §2.7 steps 3 and 7)."""

from __future__ import annotations

import pytest

from scopt.decode.search import (
    GETPC_ADJ, SearchDecodeError, SearchRow, decode_search, decode_sleb128, encode_search,
    encode_sleb128, is_pcrel, unwind,
)

# ---------------------------------------------------------------------------
# sleb128
# ---------------------------------------------------------------------------

SLEB = [
    (0, b"\x00"), (1, b"\x01"), (63, b"\x3f"), (64, b"\xc0\x00"), (127, b"\xff\x00"),
    (128, b"\x80\x01"), (-1, b"\x7f"), (-64, b"\x40"), (-65, b"\xbf\x7f"),
    (-128, b"\x80\x7f"), (832, b"\xc0\x06"), (-7272, b"\x98\x47"),
]


@pytest.mark.parametrize("val,enc", SLEB)
def test_sleb128_vectors(val, enc):
    assert encode_sleb128(val) == enc
    assert decode_sleb128(enc) == (val, len(enc))


@pytest.mark.parametrize("val", [2**63 - 1, -(2**63), 2**31, -(2**31), 10**12, -(10**12)])
def test_sleb128_roundtrip_extremes(val):
    enc = encode_sleb128(val)
    assert decode_sleb128(enc + b"\xaa") == (val, len(enc))


def test_sleb128_takes_uint64_as_int64():
    # curr - prev is computed in uint64_t and passed as int64_t
    assert encode_sleb128((1 << 64) - 1) == encode_sleb128(-1)
    assert encode_sleb128(1 << 63) == encode_sleb128(-(1 << 63))


def test_sleb128_truncated():
    with pytest.raises(SearchDecodeError):
        decode_sleb128(b"\x80\x80")
    with pytest.raises(SearchDecodeError):
        decode_sleb128(b"")


# ---------------------------------------------------------------------------
# Table codec, hand-built
# ---------------------------------------------------------------------------

# Two Thumb insns at page offsets 0x340 / 0x344 (PCREL: word 0 seeded with 0),
# host code ends at 85 and 125. Encoded by hand from encode_search's rules:
#   row 0: +0x340, +0, +0x1c68, +85
#   row 1: +4,     +0, -0x1c68, +40
HAND_ROWS = [SearchRow((0x340, 0, 0x1C68), 85), SearchRow((0x344, 0, 0), 125)]
HAND_BYTES = bytes.fromhex("c0 06 00 e8 38 d5 00" "04 00 98 47 28")


def test_encode_hand_built():
    assert encode_search(HAND_ROWS) == HAND_BYTES


def test_decode_hand_built_ignores_padding():
    rows = decode_search(HAND_BYTES + b"\x00" * 7, 2)
    assert list(rows) == HAND_ROWS
    assert rows.nbytes == len(HAND_BYTES)


def test_decode_accepts_tuples_for_encode():
    assert encode_search([((0x340, 0, 0x1C68), 85), ((0x344, 0, 0), 125)]) == HAND_BYTES


def test_decode_truncated_raises():
    with pytest.raises(SearchDecodeError):
        decode_search(HAND_BYTES[:-1], 2)
    with pytest.raises(SearchDecodeError):
        decode_search(HAND_BYTES, 3)


def test_decode_zero_icount():
    rows = decode_search(b"", 0)
    assert list(rows) == [] and rows.nbytes == 0


def test_non_pcrel_seed_is_pc():
    pc = 0x0800_1000
    rows = [SearchRow((pc, 0, 0), 10), SearchRow((pc + 2, 0, 0), 20)]
    enc = encode_search(rows, pcrel=False, pc=pc)
    # word 0 of row 0 is encoded as a delta from pc, i.e. 0
    assert enc[:1] == b"\x00"
    assert list(decode_search(enc, 2, pcrel=False, pc=pc)) == rows
    # decoding as PCREL gives the raw deltas instead
    assert decode_search(enc, 2, pcrel=True)[0].words[0] == 0


def test_words_wrap_mod_2_64():
    rows = [SearchRow((0xFFFF_FFFF_FFFF_FFFF, 1, 0), 3), SearchRow((0, 0, 0), 4)]
    enc = encode_search(rows)
    assert list(decode_search(enc, 2)) == rows


def test_is_pcrel():
    assert is_pcrel(0xFF020000) and is_pcrel(0xFF031001)
    assert not is_pcrel(0xFF010000)


# ---------------------------------------------------------------------------
# unwind (cpu_unwind_data_from_tb), hand-built rows
# ---------------------------------------------------------------------------

UNWIND_ROWS = [
    SearchRow((0x100, 0, 0), 10),
    SearchRow((0x102, 0, 7), 25),
    SearchRow((0x106, 0, 0), 25),  # an insn that emitted no host code
    SearchRow((0x108, 0, 0), 40),
]


@pytest.mark.parametrize("ret_off,expected", [
    (0, None),            # h = -2: before the TB
    (1, None),            # h = -1
    (2, 0),               # h = 0
    (11, 0),              # h = 9 < 10
    (12, 1),              # h = 10: not > 10, so row 1
    (26, 1),              # h = 24
    (27, 3),              # h = 25: row 1 and the empty row 2 both end at 25
    (41, 3),              # h = 39
    (42, None),           # h = 40: past the last insn
    (1000, None),
])
def test_unwind_hand_built(ret_off, expected):
    res = unwind(UNWIND_ROWS, ret_off)
    if expected is None:
        assert res is None
    else:
        assert res == (expected, UNWIND_ROWS[expected].words)


def test_unwind_uses_getpc_adj_2():
    assert GETPC_ADJ == 2
    # a call whose return address is exactly host_end of insn 0 still belongs to insn 0
    assert unwind(UNWIND_ROWS, 10)[0] == 0
    assert unwind(UNWIND_ROWS, 10 + GETPC_ADJ)[0] == 1


def test_unwind_after_decode():
    rows = decode_search(encode_search(UNWIND_ROWS), len(UNWIND_ROWS))
    assert unwind(rows, 27) == (3, (0x108, 0, 0))


# ---------------------------------------------------------------------------
# Real data: every record of the B0 minimal snapshot
# ---------------------------------------------------------------------------

@pytest.mark.real
def test_minimal_search_data(minimal_sc):
    from scopt.formats.sc_v5 import read_sc

    sc = read_sc(minimal_sc)
    page = 1 << sc.header.target_page_bits
    units = sc.units()
    assert len(units) == 163
    for u in units:
        r = u.record
        assert is_pcrel(r.cflags)
        rows = decode_search(u.search, r.icount, pcrel=True)
        assert len(rows) == r.icount
        # §2.7 step 3: tc+tc_size+len <= end < tc+tc_size+len+16 (CODE_GEN_ALIGN)
        s0 = r.tc_offset + r.tc_size
        assert s0 + rows.nbytes <= r.end_offset < s0 + rows.nbytes + 16
        assert r.end_offset % 16 == 0
        assert not any(u.search[rows.nbytes:]), "alignment pad must be zero"
        # host ends are non-decreasing and within the code
        ends = [x.host_end for x in rows]
        assert ends == sorted(ends) and 0 < ends[-1] <= r.tc_size
        # PCREL: word 0 is the in-page offset of the insn
        assert all(x.words[0] < page for x in rows)
        # the codec is exact
        assert encode_search(rows) == u.search[:rows.nbytes]


@pytest.mark.real
def test_minimal_slot0_matches_hand_vector(minimal_sc):
    from scopt.formats.sc_v5 import read_sc

    u = read_sc(minimal_sc).units()[0]
    assert u.record.icount == 2
    assert u.search.startswith(HAND_BYTES)
