"""WP3: the standalone ``.scm`` smoke validator and the C/Python contract.

* synthetic files: a minimal valid capture passes; a bad trailer, a seq gap, a
  missing END and a bad header checksum each fail;
* the C enumerations and staging capacities in ``qemu-sc-capture.patch`` equal
  the frozen Python ones (``scopt.formats.enums`` / ``layouts``);
* real data (marker ``real``): a capture from the WP3 worktree binary passes
  with its ``.sc`` bound.
"""

from __future__ import annotations

import importlib.util
import re
import struct
from pathlib import Path

import pytest

from scopt.canon import fnv1a64
from scopt.formats import enums, layouts as L

HERE = Path(__file__).resolve().parent
SMOKE = HERE.parent / "qemu" / "scm_smoke.py"
ROOT = Path("/data/extra_copy/fastdyn")
CAPTURE_PATCH = ROOT / "patches/qemu-sc-capture.patch"
REAL_RUN = ROOT / "runs/wp3/identity-2"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("scm_smoke", SMOKE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scm_smoke = _load_smoke()


def _chunk(ctype: int, seq: int, payload: bytes) -> bytes:
    body = struct.pack("<IIQ", ctype, len(payload), seq) + payload
    body += bytes(-len(body) & 7)
    return body + struct.pack("<Q", fnv1a64(body))


def _header() -> bytes:
    h = L.SCM_FILE_HEADER.pack(L.SCM_MAGIC, 1, 64, 1, 1234, 5678, 0, 0, 0, 0)
    return h[:56] + struct.pack("<Q", fnv1a64(h[:56]))


def _run_info() -> bytes:
    prologue = b"\x55" * 45
    f = [1, 1, 0x7FFF6C000000, 1 << 31, 45, 24, 22, 0xABC, 0, 0, 1 << 31,
         64, 152, 4096, 10, 3, 0, 0, 0, 0, 0, 0, 45]
    return L.SCM_RUN_INFO.pack(*f) + prologue


def _maps(tag: int) -> bytes:
    raw = b"00400000-00401000 r-xp 00000000 00:00 0 /x\n"
    return L.SCM_MAPS_HEADER.pack(tag, 0, len(raw), 0) + raw


def _end(committed=0, helpers=0) -> bytes:
    return L.SCM_END.pack(committed, 0, 0, 0, 0, helpers, 1)


def _valid() -> bytes:
    return (_header() + _chunk(1, 0, _run_info()) + _chunk(7, 1, _maps(2))
            + _chunk(10, 2, _end()))


def _write(tmp_path: Path, data: bytes) -> str:
    p = tmp_path / "x.scm"
    p.write_bytes(data)
    return str(p)


def test_valid_minimal_file_passes(tmp_path):
    sm = scm_smoke.run(_write(tmp_path, _valid()), None, False)
    assert sm.failures == []
    assert sm.stats["end"]["clean"] == 1


def test_bad_trailer_fails(tmp_path):
    data = bytearray(_valid())
    data[64 + 20] ^= 1  # inside the RUN_INFO payload
    sm = scm_smoke.run(_write(tmp_path, bytes(data)), None, False)
    assert any("bad trailer" in f for f in sm.failures)


def test_seq_gap_fails(tmp_path):
    data = _header() + _chunk(1, 0, _run_info()) + _chunk(7, 2, _maps(2)) + _chunk(10, 3, _end())
    sm = scm_smoke.run(_write(tmp_path, data), None, False)
    assert any("seq 2 != 1" in f for f in sm.failures)


def test_missing_end_fails(tmp_path):
    data = _header() + _chunk(1, 0, _run_info()) + _chunk(7, 1, _maps(2))
    sm = scm_smoke.run(_write(tmp_path, data), None, False)
    assert any("no END" in f for f in sm.failures)


def test_bad_header_checksum_fails(tmp_path):
    data = bytearray(_valid())
    data[20] ^= 1  # pid
    sm = scm_smoke.run(_write(tmp_path, bytes(data)), None, False)
    assert sm.failures == ["header checksum"]


def test_end_counter_mismatch_fails(tmp_path):
    data = (_header() + _chunk(1, 0, _run_info()) + _chunk(7, 1, _maps(2))
            + _chunk(10, 2, _end(committed=1)))
    sm = scm_smoke.run(_write(tmp_path, data), None, False)
    assert any("END committed 1" in f for f in sm.failures)


def _patch_text() -> str:
    if not CAPTURE_PATCH.exists():
        pytest.skip(f"{CAPTURE_PATCH} not present")
    return CAPTURE_PATCH.read_text()


def test_c_site_kinds_and_encodings_match_python():
    text = _patch_text()
    kinds = {m[1]: int(m[2], 0) for m in re.finditer(r"SCC_SITE_(\w+)\s*=\s*(0x[0-9A-Fa-f]+)", text)}
    assert kinds == {k.name: int(k) for k in enums.SiteKind}
    encs = {m[1]: int(m[2]) for m in re.finditer(r"SCC_ENC_(\w+)\s*=\s*(\d+)", text)}
    assert encs == {e.name: int(e) for e in enums.Encoding}


def test_c_capacities_match_python():
    text = _patch_text()
    caps = dict(re.findall(r"#define SCC_CAP_(\w+)\s+(.+)", text))
    py = {
        "SITES": L.SCM_CAP_SITES, "INSNS": L.SCM_CAP_INSNS, "POOL": L.SCM_CAP_POOL,
        "LDST": L.SCM_CAP_LDST, "OPMARKS": L.SCM_CAP_OPMARKS,
        "STATIC_EXITS": L.SCM_CAP_STATIC_EXITS, "GUEST_BYTES": L.SCM_CAP_GUEST_BYTES,
        "CHUNK_BUF": L.SCM_CAP_CHUNK_BUF, "MAPS_BUF": L.SCM_CAP_MAPS_BUF,
        "HELPERS": L.SCM_CAP_HELPERS,
    }
    for name, value in py.items():
        assert eval(caps[name].strip()) == value, name  # noqa: S307 - literal C ints


def test_c_fnv_offset_is_qemus():
    text = _patch_text()
    m = re.search(r"#define SCC_FNV_OFFSET\s+UINT64_C\((\d+)\)", text)
    assert m and int(m[1]) == 1469598103934665603


@pytest.mark.real
def test_real_capture_binds_to_its_cache():
    scm = REAL_RUN / "scm/on1.scm"
    sc = REAL_RUN / "sc/on1.sc"
    if not (scm.exists() and sc.exists()):
        pytest.skip("WP3 identity run not present")
    sm = scm_smoke.run(str(scm), str(sc), False)
    assert sm.failures == []
    assert sm.stats["binding"]["bound_tb_chunks"] == sm.stats["binding"]["sc_record_count"] > 0
