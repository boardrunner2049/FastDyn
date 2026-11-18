"""Golden vectors for FNV-1a-64, the record checksum fold and the id functions (WP0).

The tb_uid / SnapshotId goldens for ``runs/b0/cache/minimal.sc`` were computed
by hand (plain ``struct`` + ``hashlib``, no scopt code) and are pinned here as
literals; the tests also recompute them independently of ``scopt.ids``.
"""

from __future__ import annotations

import hashlib
import struct

import pytest

from scopt import canon, ids
from scopt.formats.sc_v5 import ScHeader, ScRecord

# ---------------------------------------------------------------------------
# FNV-1a-64 (published test vectors + QEMU constants)
# ---------------------------------------------------------------------------

# QEMU's offset basis is 1469598103934665603, not the standard 0xcbf29ce484222325,
# so the published FNV-1a-64 vectors do not apply. These were computed with the
# independent implementation in independent/scparse.py.
FNV_VECTORS = [
    (b"", 0x14650FB0739D0383),
    (b"a", 0x44BD8AD473CD9906),
    (b"foobar", 0x88FAD7C0A8FF07F2),
]


@pytest.mark.parametrize("data,expected", FNV_VECTORS)
def test_fnv1a64_vectors(data, expected):
    assert canon.fnv1a64(data) == expected


def test_fnv_constants_match_qemu(fastdyn_root):
    assert canon.FNV64_OFFSET == 1469598103934665603  # SUPERCACHE_FNV_OFFSET
    assert canon.FNV64_PRIME == 1099511628211  # SUPERCACHE_FNV_PRIME
    assert canon.FNV64_OFFSET != canon.FNV64_OFFSET_STANDARD
    src = fastdyn_root / "src/qemu/tcg/supercache.c"
    if src.exists():
        text = src.read_text()
        assert "SUPERCACHE_FNV_OFFSET     UINT64_C(1469598103934665603)" in text
        assert "SUPERCACHE_FNV_PRIME      UINT64_C(1099511628211)" in text


def test_fnv_matches_critic_scparse(scparse_path):
    # scparse.py runs its CLI at import time, so pull out just its fnv() definition
    src = scparse_path.read_text().split("path=sys.argv[1]")[0]
    ns: dict = {}
    exec(compile(src, str(scparse_path), "exec"), ns)  # noqa: S102 - trusted repo file
    for data in (b"", b"a", b"foobar", bytes(range(256))):
        assert ns["fnv"](data) == canon.fnv1a64(data)


def test_fnv_is_incremental():
    assert canon.fnv1a64(b"bar", canon.fnv1a64(b"foo")) == canon.fnv1a64(b"foobar")


def test_record_checksum_fold():
    h = 0x0123456789ABCDEF
    assert canon.record_checksum_fold(h) == (0x89ABCDEF ^ 0x01234567)
    assert canon.record_checksum_fold(0xFFFFFFFF00000000) == 0xFFFFFFFF
    assert 0 <= canon.record_checksum_fold(canon.fnv1a64(b"x")) <= 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Canonical JSON and content ids
# ---------------------------------------------------------------------------

def test_canonical_json_rules():
    assert canon.canonical_json({"b": 1, "a": [1, "x", None, True]}) == b'{"a":[1,"x",null,true],"b":1}'
    assert canon.canonical_json({"k": "é"}) == '{"k":"é"}'.encode()
    for bad in ({1: 2}, {"f": float("nan")}, {"b": b"x"}, {"s": {1, 2}}):
        with pytest.raises(canon.CanonicalJSONError):
            canon.canonical_json(bad)


def test_content_ids_drop_their_id_field():
    body = {"schema_version": 1, "x": "0x10"}
    expect = "sha256:" + hashlib.sha256(b'{"schema_version":1,"x":"0x10"}').hexdigest()
    assert ids.bundle_id(body) == expect
    assert ids.bundle_id({**body, "bundle_id": "sha256:" + "0" * 64}) == expect
    assert ids.profile_id({**body, "profile_id": "whatever"}) == expect
    assert ids.runtime_identity_id({**body, "runtime_identity_id": "z"}) == expect
    # regions additionally drop provenance
    assert ids.region_id({**body, "region_id": "r", "provenance": {"passes": []}}) == expect
    assert ids.artifact_id(b"abc  cache.sc\n") == "sha256:" + hashlib.sha256(b"abc  cache.sc\n").hexdigest()
    assert canon.is_sha256_id(expect) and not canon.is_sha256_id(expect.upper())


def test_hexaddr():
    assert canon.hexaddr(0x8018766) == "0x8018766"
    assert canon.parse_hex("0xff020000") == 0xFF020000
    with pytest.raises(ValueError):
        canon.hexaddr(-1)


# ---------------------------------------------------------------------------
# tb_uid / UnitId on synthetic values
# ---------------------------------------------------------------------------

def test_tb_uid_layout_and_none_page():
    key = ids.TBKey(0x28098740, None, 0x110, 0x800400, 0xFF020000)
    msg = ids.tb_uid_bytes(key, 8, 2, 0x45AB127C5D80DEF9)
    assert len(msg) == 3 + 8 + 8 + 4 + 8 + 4 + 2 + 2 + 8 == 47
    assert msg[:3] == b"tb1"
    assert msg[11:19] == b"\xff" * 8  # page_addr1 None -> (tb_page_addr_t)-1
    assert ids.TBKey.from_raw(0x28098740, ids.PAGE_ADDR_NONE, 0x110, 0x800400, 0xFF020000) == key
    uid = ids.tb_uid(key, 8, 2, 0x45AB127C5D80DEF9)
    assert uid == "tb:" + hashlib.sha256(msg).digest()[:16].hex()
    assert len(uid) == 3 + 32


def test_unit_ids_occurrence_index():
    a, b = "tb:" + "a" * 32, "tb:" + "b" * 32
    assert ids.unit_ids([a, b, a, a, b]) == [f"{a}#0", f"{b}#0", f"{a}#1", f"{a}#2", f"{b}#1"]
    assert ids.parse_unit_id(f"{a}#12") == (a, 12)
    for bad in (a, f"{a}#", f"{a}#01", f"{a}#-1", "tb:abc#0", f"xx:{'a' * 32}#0"):
        assert not ids.is_unit_id(bad)


def test_snapshot_id_synthetic():
    header = bytes(range(80))
    rec = bytes(104)
    code = b"\x90" * 7
    h = hashlib.sha256(header[:72] + rec[:92] + hashlib.sha256(code).digest()).hexdigest()
    assert ids.snapshot_id(header, [(rec, code)]) == "sha256:" + h
    assert ids.snapshot_id(header, []) == "sha256:" + hashlib.sha256(header[:72]).hexdigest()


# ---------------------------------------------------------------------------
# Real golden: runs/b0/cache/minimal.sc
# ---------------------------------------------------------------------------

GOLDEN = {
    # slot: (tb_uid, page_addr0, guest_size, icount, guest_hash)
    0: ("tb:dc8f22270b9cf2c0f14137dd01fc6fd9", 0x28098740, 8, 2, 0x45AB127C5D80DEF9),
    11: ("tb:6af7da373b90d84ea38b0bf67cc1d857", 0x28098766, 8, 4, 0xA16F58218A9FA0E7),
}
GOLDEN_SNAPSHOT_ID = "sha256:3a96366bb7f3d5dae72dec5a90dc45dd1fff7ca01962c08f7e1661653ea627ae"
GOLDEN_HEADER = dict(code_size=0x80000000, code_address=0x7FFF6C000000, prologue_end=0x2D,
                     record_count=163, runtime_signature=0xCD59117CBC9F8837,
                     host_page_size=4096, target_page_bits=10, tb_size=152,
                     checksum=0x1A7C702B57653DE8)


def _read_minimal(path):
    with open(path, "rb") as f:
        hdr = f.read(80)
        code_size = struct.unpack_from("<Q", hdr, 16)[0]
        count = struct.unpack_from("<Q", hdr, 40)[0]
        f.seek(0x10000 + code_size)
        recs = [f.read(104) for _ in range(count)]
        codes = []
        for r in recs:
            _tb, end, tc = struct.unpack_from("<QQQ", r)
            f.seek(0x10000 + tc)
            codes.append(f.read(end - tc))
    return hdr, recs, codes


@pytest.fixture(scope="module")
def minimal(minimal_sc):
    return _read_minimal(minimal_sc)


def test_minimal_header_and_checksums(minimal):
    hdr, recs, _ = minimal
    h = ScHeader.from_bytes(hdr)
    for k, v in GOLDEN_HEADER.items():
        assert getattr(h, k) == v, k
    assert h.magic == b"QSCACHE1" and h.version == 5 and h.header_size == 80
    assert canon.sc_header_checksum(hdr) == h.checksum
    assert h.checksum_ok and h.to_bytes() == hdr
    assert len(recs) == 163
    for i, r in enumerate(recs):
        rec = ScRecord.from_bytes(r)
        assert rec.to_bytes() == r, i
        assert rec.committed == 0x53435231, i
        assert canon.sc_record_checksum(r) == rec.checksum, i
        assert rec.checksum_ok
        assert rec.pc == 0  # CF_PCREL (G2)


@pytest.mark.parametrize("slot", sorted(GOLDEN))
def test_minimal_tb_uid_golden(minimal, slot):
    _, recs, _ = minimal
    r = recs[slot]
    exp_uid, pa0, gs, ic, gh = GOLDEN[slot]
    # hand computation straight from the record bytes (§4.1)
    (_tb, _end, _tc, _tcs, _pc, cs_base, page_addr0, page_addr1, guest_hash, flags, cflags,
     guest_size, icount) = struct.unpack_from("<9QIIHH", r)
    assert (page_addr0, guest_size, icount, guest_hash) == (pa0, gs, ic, gh)
    msg = b"tb1" + struct.pack("<QQIQIHHQ", page_addr0, page_addr1, flags, cs_base, cflags,
                               guest_size, icount, guest_hash)
    by_hand = "tb:" + hashlib.sha256(msg).digest()[:16].hex()
    assert by_hand == exp_uid
    # scopt.ids
    rec = ScRecord.from_bytes(r)
    assert rec.key.page_addr1 is None
    assert ids.tb_uid(rec.key, rec.guest_size, rec.icount, rec.guest_hash) == exp_uid


def test_minimal_unit_ids_unique(minimal):
    _, recs, _ = minimal
    uids = [ids.tb_uid(ScRecord.from_bytes(r).key, *struct.unpack_from("<HH", r, 80),
                       struct.unpack_from("<Q", r, 64)[0]) for r in recs]
    units = ids.unit_ids(uids)
    assert len(set(uids)) == 163  # no duplicate keys in minimal (critic A1)
    assert all(u.endswith("#0") for u in units)
    assert units[0] == GOLDEN[0][0] + "#0" and units[11] == GOLDEN[11][0] + "#0"


def test_minimal_snapshot_id_golden(minimal):
    hdr, recs, codes = minimal
    h = hashlib.sha256(hdr[:72])
    for r, c in zip(recs, codes):
        h.update(r[:92])
        h.update(hashlib.sha256(c).digest())
    assert "sha256:" + h.hexdigest() == GOLDEN_SNAPSHOT_ID
    assert ids.snapshot_id(hdr, zip(recs, codes)) == GOLDEN_SNAPSHOT_ID
