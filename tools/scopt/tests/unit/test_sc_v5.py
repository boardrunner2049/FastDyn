""".sc v5 reader/writer (WP1; T-B1-2 and the static half of V9).

Real-data tests use the read-only golden ``runs/b0/cache/minimal.sc``; every
corruption is applied to a sparse copy in ``tmp_path``.
"""

from __future__ import annotations

import ast
import os
import pickle
import re
import struct
import subprocess
import sys

import pytest
from click.testing import CliRunner

from scopt import canon
from scopt.formats.sc_v5 import (
    HEADER_SIZE, RECORD_SIZE, SC_CODE_OFFSET, SC_RECORD_COMMIT, ScFormatError, ScHeader,
    ScRecord, ScUnit, identity_copy, read_sc, sha256_sparse, sparse_copy, structural_findings,
    write_sc,
)

N_MINIMAL = 163


def _patch(path, off, data):
    fd = os.open(path, os.O_WRONLY)
    try:
        os.pwrite(fd, data, off)
    finally:
        os.close(fd)


def _set_record(path, sc, slot, rec: ScRecord, *, fix=True):
    raw = rec.to_bytes(fix_checksum=fix)
    _patch(path, sc.header.records_offset + slot * RECORD_SIZE, raw)


def _set_header(path, hdr: ScHeader, *, fix=True):
    _patch(path, 0, hdr.to_bytes(fix_checksum=fix))


@pytest.fixture
def copy(minimal_sc, tmp_path):
    dst = tmp_path / "copy.sc"
    sparse_copy(minimal_sc, dst)
    return dst


# ---------------------------------------------------------------------------
# Golden (T-B1-2)
# ---------------------------------------------------------------------------

@pytest.mark.real
def test_minimal_golden(minimal_sc):
    sc = read_sc(minimal_sc)
    h = sc.header
    assert sc.loadable and not sc.header_problems and not sc.problems and not sc.skipped
    assert (h.magic, h.version, h.header_size) == (b"QSCACHE1", 5, 80)
    assert h.tb_size == 152 and h.target_page_bits == 10 and h.host_page_size == 4096
    assert h.record_count == N_MINIMAL == sc.count == len(sc.valid_slots)
    assert sc.file_size == sc.expected_file_size == SC_CODE_OFFSET + h.code_size + 163 * 104
    for r in sc.records:
        assert r.tb_offset % 64 == 0
        assert r.tc_offset - r.tb_offset == 192
        assert r.pc == 0
    assert structural_findings(sc) == []
    assert sc.max_end == max(r.end_offset for r in sc.records)
    uids = sc.unit_ids()
    assert len(uids) == 163 and all(u.endswith("#0") for u in uids)


@pytest.mark.real
def test_matches_scparse(minimal_sc, scparse_path, tmp_path):
    """Every field scparse.py prints or pickles agrees with read_sc."""
    pkl = tmp_path / "scparse.pkl"
    out = subprocess.run([sys.executable, "-I", str(scparse_path), str(minimal_sc), str(pkl)],
                         capture_output=True, text=True, check=True, cwd=tmp_path).stdout
    sc = read_sc(minimal_sc)
    h = sc.header

    line = out.splitlines()[0]
    m = re.fullmatch(
        r"magic (b'.*?') ver (\d+) hsz (\d+) code_size (0x[0-9a-f]+) code_addr (0x[0-9a-f]+) "
        r"prologue_end (0x[0-9a-f]+) records (\d+) sig (0x[0-9a-f]+) host_page (\d+) "
        r"target_page_bits (\d+) tb_size (\d+) hdr_ck_ok (True|False)", line)
    assert m, line
    g = m.groups()
    assert ast.literal_eval(g[0]) == h.magic
    assert (int(g[1]), int(g[2])) == (h.version, h.header_size)
    assert int(g[3], 16) == h.code_size and int(g[4], 16) == h.code_address
    assert int(g[5], 16) == h.prologue_end and int(g[6]) == h.record_count
    assert int(g[7], 16) == h.runtime_signature
    assert (int(g[8]), int(g[9]), int(g[10])) == (h.host_page_size, h.target_page_bits, h.tb_size)
    assert (g[11] == "True") == h.checksum_ok is True
    assert f"record bytes {163 * 104} n 163" in out
    assert "bad records []" in out

    code_addr, prol_end, recs = pickle.loads(pkl.read_bytes())
    assert (code_addr, prol_end) == (h.code_address, h.prologue_end)
    assert len(recs) == len(sc.records) == 163
    for d, r in zip(recs, sc.records):
        assert d["ok"] is (r is not None)
        assert d == {
            "i": d["i"], "tb": r.tb_offset, "end": r.end_offset, "tc": r.tc_offset,
            "tcs": r.tc_size, "pc": r.pc, "cs": r.cs_base, "pa0": r.page_addr0,
            "pa1": r.page_addr1, "fl": r.flags, "cf": r.cflags, "gs": r.guest_size,
            "ic": r.icount, "jr": r.jmp_reset_offset, "ji": r.jmp_insn_offset, "ok": True,
        }
    assert [d["i"] for d in recs] == list(range(163))


@pytest.mark.real
def test_raw_fields_independent_unpack(minimal_sc):
    """guest_hash, checksum and committed (not exported by scparse), via scparse's own format."""
    sc = read_sc(minimal_sc)
    with open(minimal_sc, "rb") as f:
        f.seek(SC_CODE_OFFSET + sc.header.code_size)
        blob = f.read()
    assert len(blob) == 163 * 104
    for i, r in enumerate(sc.records):
        v = struct.unpack("<QQQQQQQQQIIHHHHHHII4x", blob[i * 104:(i + 1) * 104])
        assert (v[8], v[17], v[18]) == (r.guest_hash, r.checksum, r.committed)
        assert r.committed == SC_RECORD_COMMIT
        assert sc.record_bytes[i] == blob[i * 104:(i + 1) * 104]
        assert ScRecord.from_bytes(sc.record_bytes[i]).to_bytes() == sc.record_bytes[i]
    assert ScHeader.from_bytes(sc.header_bytes).to_bytes() == sc.header_bytes


@pytest.mark.real
def test_bodies_match_file_bytes(minimal_sc):
    sc = read_sc(minimal_sc)
    with open(minimal_sc, "rb") as f:
        for u in sc.units():
            f.seek(SC_CODE_OFFSET + u.record.tc_offset)
            assert f.read(len(u.body)) == u.body
            assert len(u.code) == u.record.tc_size
            assert u.code + u.search == u.body


# ---------------------------------------------------------------------------
# Round trip, sparse copy, sha256 (V9 static half)
# ---------------------------------------------------------------------------

@pytest.mark.real
@pytest.mark.slow
def test_identity_roundtrip_sha256(minimal_sc, tmp_path):
    out = tmp_path / "out.sc"
    res = identity_copy(minimal_sc, out)
    assert res.status == "identity", res.to_json()
    assert res.in_sha256 == res.out_sha256
    assert res.in_snapshot_id == res.out_snapshot_id
    assert res.n_units == 163 and res.dropped == () and res.findings == ()
    # sha256_sparse equals a plain full read of both files
    assert canon.sha256_file(str(out)) == res.out_sha256 == canon.sha256_file(str(minimal_sc))
    # the output is sparse: a 2 GiB logical file with well under 1 MiB allocated
    st = os.stat(out)
    assert st.st_size == os.stat(minimal_sc).st_size
    assert st.st_blocks * 512 < (1 << 20)


@pytest.mark.real
def test_write_read_explicit(minimal_sc, tmp_path):
    sc = read_sc(minimal_sc)
    out = tmp_path / "w.sc"
    hdr = write_sc(out, sc.header, sc.units())
    assert hdr.to_bytes() == sc.header_bytes
    back = read_sc(out)
    assert back.header_bytes == sc.header_bytes and back.record_bytes == sc.record_bytes
    assert back.bodies == sc.bodies and back.snapshot_id() == sc.snapshot_id()


@pytest.mark.real
@pytest.mark.slow
def test_sparse_copy(minimal_sc, tmp_path):
    dst = tmp_path / "c.sc"
    sparse_copy(minimal_sc, dst)
    assert sha256_sparse(dst) == sha256_sparse(minimal_sc)
    assert os.stat(dst).st_blocks * 512 < (1 << 20)
    assert os.stat(dst).st_mode & 0o200  # writable copy for QEMU's O_RDWR


@pytest.mark.real
@pytest.mark.slow
def test_cli_identity_copy_and_dump(minimal_sc, tmp_path):
    import json

    from scopt.cli import cli, load_commands

    load_commands()
    r = CliRunner().invoke(cli, ["sc", "identity-copy", str(minimal_sc), str(tmp_path / "o.sc"),
                                 "--require-identity"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert doc["status"] == "identity" and doc["sha256_equal"] and doc["n_units"] == 163
    r = CliRunner().invoke(cli, ["sc", "dump", "--json", "--search", "--check", str(minimal_sc)])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert doc["valid"] == 163 and doc["structural_findings"] == []
    assert all(len(x["search"]["rows"]) == x["icount"] for x in doc["records"])
    r = CliRunner().invoke(cli, ["sc", "dump", str(minimal_sc), "--slot", "3"])
    assert r.exit_code == 0 and "snapshot_id sha256:" in r.output


# ---------------------------------------------------------------------------
# Corruption cases (T-B1-9 subset owned by WP1)
# ---------------------------------------------------------------------------

@pytest.mark.real
def test_record_checksum(copy, tmp_path):
    sc = read_sc(copy)
    # flip one byte of slot 5's guest_hash without fixing the checksum
    _patch(copy, sc.header.records_offset + 5 * RECORD_SIZE + 64, bytes([sc.record_bytes[5][64] ^ 1]))
    bad = read_sc(copy)
    assert bad.loadable
    assert [(s.slot, s.reason) for s in bad.skipped] == [(5, "record_checksum")]
    assert bad.records[5] is None and len(bad.valid_slots) == 162
    # identity copy keeps what QEMU would load and lists the dropped slot
    res = identity_copy(copy, tmp_path / "o.sc")
    assert res.status == "identity_canonicalized"
    assert [(d.slot, d.reason) for d in res.dropped] == [(5, "record_checksum")]
    out = read_sc(tmp_path / "o.sc")
    assert out.header.record_count == 162 and not out.skipped
    assert structural_findings(out) == []
    assert [out.record_bytes[i] for i in range(162)] == [
        sc.record_bytes[i] for i in range(163) if i != 5]


@pytest.mark.real
def test_not_committed(copy):
    sc = read_sc(copy)
    _set_record(copy, sc, 7, sc.records[7].replace(committed=0))
    bad = read_sc(copy)
    assert [(s.slot, s.reason) for s in bad.skipped] == [(7, "not_committed")]


@pytest.mark.real
@pytest.mark.parametrize("change,detail", [
    (lambda r, h: {"end_offset": h.code_size + 16}, "end_offset > size"),
    (lambda r, h: {"tc_offset": r.tb_offset}, "tb_offset >= tc_offset"),
    (lambda r, h: {"tc_offset": r.end_offset}, "tc_offset >= end_offset"),
    (lambda r, h: {"tc_size": r.end_offset - r.tc_offset + 1}, "tc_size > end_offset - tc_offset"),
    (lambda r, h: {"tb_offset": h.code_size - h.tb_size + 1}, "tb_offset > size - sizeof(TranslationBlock)"),
])
def test_bounds(copy, change, detail):
    sc = read_sc(copy)
    r = sc.records[9]
    _set_record(copy, sc, 9, r.replace(**change(r, sc.header)))
    bad = read_sc(copy)
    assert [(s.slot, s.reason, s.detail) for s in bad.skipped] == [(9, "bounds", detail)]


@pytest.mark.real
def test_header_checksum(copy, tmp_path):
    sc = read_sc(copy)
    _patch(copy, 24, bytes([sc.header_bytes[24] ^ 0x40]))  # code_address byte
    bad = read_sc(copy)
    assert bad.header_problems == ("header_checksum",)
    assert not bad.loadable and bad.units() == [] and bad.valid_slots == ()
    assert bad.count == 163  # records are still parsed for diagnostics
    with pytest.raises(ScFormatError):
        identity_copy(copy, tmp_path / "o.sc")
    with pytest.raises(ScFormatError, match="header_checksum"):
        read_sc(copy, strict=True)


@pytest.mark.real
def test_version(copy):
    sc = read_sc(copy)
    _set_header(copy, sc.header.replace(version=6))
    bad = read_sc(copy)
    assert bad.header_problems == ("version",)
    assert not bad.loadable and bad.count == 0  # record layout unknown: not parsed


@pytest.mark.real
def test_magic_and_header_size(copy):
    sc = read_sc(copy)
    _set_header(copy, sc.header.replace(magic=b"QSCACHE0", header_size=96))
    bad = read_sc(copy)
    assert bad.header_problems == ("magic", "header_size")


@pytest.mark.real
def test_truncated_records(copy):
    sc = read_sc(copy)
    os.truncate(copy, sc.header.records_offset + 100 * RECORD_SIZE + 50)
    bad = read_sc(copy)
    assert bad.loadable
    assert [p.code for p in bad.problems] == ["truncated_records"]
    assert bad.count == 100 == bad.available_records == len(bad.valid_slots)
    assert bad.record_bytes == sc.record_bytes[:100]
    assert "truncated_records" in [f.code for f in structural_findings(bad)]
    with pytest.raises(ScFormatError, match="truncated_records"):
        read_sc(copy, strict=True)


@pytest.mark.real
def test_record_count_below_available(copy):
    """Records past record_count are ignored by QEMU (count=min(...))."""
    sc = read_sc(copy)
    _set_header(copy, sc.header.replace(record_count=150))
    f = read_sc(copy)
    assert f.loadable and f.count == 150 and f.available_records == 163 and not f.problems
    found = structural_findings(f)
    # the ignored tail shows up as the file size, plus the bytes of the 13
    # ignored records and of their code bodies, which nothing loads
    assert [x.code for x in found] == ["file_size", "nonzero_outside_records",
                                       "nonzero_outside_records"]
    tail_body = SC_CODE_OFFSET + sc.records[150].tc_offset
    assert found[1].detail == f"file offset 0x{tail_body:x}"
    assert found[2].detail == f"file offset 0x{sc.header.records_offset + 150 * RECORD_SIZE:x}"


@pytest.mark.real
def test_nonzero_outside_records(copy, tmp_path, minimal_sc):
    _patch(copy, SC_CODE_OFFSET + 8, b"\x01")  # inside the (never written) prologue
    f = read_sc(copy)
    assert f.loadable and not f.skipped
    codes = [x.code for x in structural_findings(f)]
    assert codes == ["nonzero_outside_records"]
    res = identity_copy(copy, tmp_path / "o.sc")
    assert res.status == "identity_canonicalized" and res.dropped == ()
    assert res.in_snapshot_id == res.out_snapshot_id  # the stray byte is not part of any unit
    # canonicalizing drops the stray byte: the output is the original golden file
    assert sha256_sparse(tmp_path / "o.sc") == sha256_sparse(minimal_sc)


@pytest.mark.real
def test_runtime_expectations(minimal_sc):
    f = read_sc(minimal_sc, expect={"tb_size": 152, "target_page_bits": 10})
    assert f.loadable
    f = read_sc(minimal_sc, expect={"tb_size": 160, "runtime_signature": 1})
    assert f.header_problems == ("runtime_mismatch:tb_size", "runtime_mismatch:runtime_signature")
    with pytest.raises(ValueError):
        read_sc(minimal_sc, expect={"code_address": 0})


# ---------------------------------------------------------------------------
# Synthetic files (no real fixture needed)
# ---------------------------------------------------------------------------

CODE_SIZE = 0x4000


def _hdr(**kw) -> ScHeader:
    base = dict(magic=b"QSCACHE1", version=7, header_size=80, code_size=CODE_SIZE,
                code_address=0x7FFF_6C00_0000, prologue_end=0x2D, record_count=0,
                runtime_signature=0x1234, host_page_size=4096, target_page_bits=10,
                tb_size=152, reserved=0, checksum=0)
    base.update(kw)
    return ScHeader(**base).with_checksum()


def _unit(slot, tb, code: bytes, search: bytes) -> ScUnit:
    tc = tb + 192
    end = tc + len(code) + len(search)
    rec = ScRecord(tb, end, tc, len(code), 0, 0x800400, 0x28080000 + tb, 2**64 - 1, 0xABCDEF,
                   0x110, 0xFF020000, 4, 1, (0xFFFF, 0xFFFF), (0xFFFF, 0xFFFF), 0,
                   SC_RECORD_COMMIT).with_checksum()
    return ScUnit(slot, rec, rec.to_bytes(), code + search)


def test_synthetic_roundtrip(tmp_path):
    units = [_unit(0, 0x40, b"\x90" * 30 + b"\xc3", b"\x00\x04\x00\x1f" + bytes(13)),
             _unit(1, 0x200, b"\xcc" * 7, b"\x02\x00\x00\x07" + bytes(5))]
    p = tmp_path / "s.sc"
    hdr = write_sc(p, _hdr(record_count=99), units)
    assert hdr.record_count == 2 and hdr.checksum_ok
    assert os.stat(p).st_size == SC_CODE_OFFSET + CODE_SIZE + 2 * RECORD_SIZE
    f = read_sc(p, strict=True)
    assert [u.body for u in f.units()] == [u.body for u in units]
    assert structural_findings(f) == []
    # unit-like objects with record/code/search (TBUnit's field names) work too
    class Like:
        def __init__(self, u):
            self.record, self.code, self.search = u.record_bytes, u.code, u.search
    p2 = tmp_path / "s2.sc"
    write_sc(p2, f.header, [Like(u) for u in f.units()])
    assert sha256_sparse(p2) == sha256_sparse(p)
    p3 = tmp_path / "s3.sc"
    write_sc(p3, f.header_bytes, [(u.record, u.body) for u in f.units()], sparse=False)
    assert sha256_sparse(p3) == sha256_sparse(p)
    assert identity_copy(p, tmp_path / "s4.sc").status == "identity"


def test_synthetic_empty(tmp_path):
    p = tmp_path / "e.sc"
    write_sc(p, _hdr(), [])
    f = read_sc(p, strict=True)
    assert f.count == 0 and f.units() == [] and structural_findings(f) == []


def test_writer_rejects_bad_input(tmp_path):
    good = _unit(0, 0x40, b"\x90" * 8, b"\x00" * 8)
    over = _unit(1, 0x80, b"\x90" * 8, b"\x00" * 8)  # [0x80, ...) overlaps [0x40, 0x118)
    with pytest.raises(ScFormatError, match="overlap"):
        write_sc(tmp_path / "a.sc", _hdr(), [good, over])
    short = ScUnit(0, good.record, good.record_bytes, good.body[:-1])
    with pytest.raises(ScFormatError, match="body"):
        write_sc(tmp_path / "b.sc", _hdr(), [short])
    unc = good.record.replace(committed=0)
    with pytest.raises(ScFormatError, match="not_committed"):
        write_sc(tmp_path / "c.sc", _hdr(), [(unc, good.body)])
    low = _unit(0, 0x0, b"\x90" * 8, b"\x00" * 8)
    with pytest.raises(ScFormatError, match="prologue"):
        write_sc(tmp_path / "d.sc", _hdr(), [low])
    with pytest.raises(ScFormatError, match="header"):
        write_sc(tmp_path / "e.sc", _hdr(version=6), [good])
    # check=False writes corrupt input as given (for corruption tests)
    write_sc(tmp_path / "f.sc", _hdr(), [(unc, good.body)], check=False)
    assert [s.reason for s in read_sc(tmp_path / "f.sc").skipped] == ["not_committed"]


def test_truncated_header(tmp_path):
    p = tmp_path / "t.sc"
    p.write_bytes(b"QSCACHE1" + bytes(20))
    f = read_sc(p)
    assert f.header_problems == ("truncated_header",) and not f.loadable and f.count == 0
    with pytest.raises(ScFormatError):
        read_sc(p, strict=True)


def test_file_smaller_than_record_offset(tmp_path):
    p = tmp_path / "s.sc"
    p.write_bytes(_hdr(record_count=3).to_bytes())
    f = read_sc(p)
    assert f.loadable and f.count == 0 and f.available_records == 0
    assert [x.code for x in f.problems] == ["truncated_records"]


def test_sha256_sparse_matches_plain(tmp_path):
    p = tmp_path / "h.bin"
    with open(p, "wb") as f:
        f.truncate(5 << 20)
        f.seek(3 << 20)
        f.write(b"abc")
    assert sha256_sparse(p) == canon.sha256_file(str(p))
    q = tmp_path / "h2.bin"
    sparse_copy(p, q)
    assert q.read_bytes() == p.read_bytes()


def test_header_record_constants():
    assert HEADER_SIZE == 80 and RECORD_SIZE == 104 and SC_CODE_OFFSET == 0x10000
