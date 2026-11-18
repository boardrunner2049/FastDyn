"""WP22: offline checks of the hand-made v6 file generator (tests/qemu/v6_synth.py).

The generator is the test input of the QEMU loader unit tests, so its layout
must match design §5.2-5.3 byte for byte. These tests re-parse what it writes
with plain ``struct`` offsets taken from the design tables (not from the
generator's own Struct objects) and run the guard assembler on fixed inputs.
No QEMU is needed.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "qemu"
sys.path.insert(0, str(HERE))

import v6_synth as V  # noqa: E402

from scopt.canon import fnv1a64  # noqa: E402
from scopt.formats.layouts import SC_HEADER  # noqa: E402

FNV = 1469598103934665603


def _base_sc(tmp_path: Path) -> Path:
    """A minimal valid-looking v5 file: header only, no records (sparse)."""
    p = tmp_path / "base.sc"
    code_size = 0x200000
    hdr = bytearray(SC_HEADER.pack(b"QSCACHE1", 5, 80, code_size, 0x7FFF6C000000, 0x2D, 0,
                                   0x1234, 4096, 10, 152, 0, 0))
    hdr[72:80] = struct.pack("<Q", fnv1a64(bytes(hdr[:72])))
    with open(p, "wb") as f:
        f.truncate(0x10000 + code_size)
        f.write(hdr)
    return p


def _read_ext(p: Path) -> tuple[dict, bytes, bytes]:
    with open(p, "rb") as f:
        f.seek(0x100)
        h = f.read(64)
        hd = {
            "magic": h[0:8], "ext_version": struct.unpack_from("<I", h, 8)[0],
            "ext_header_size": struct.unpack_from("<I", h, 12)[0],
            "n_variants": struct.unpack_from("<I", h, 16)[0],
            "n_slots": struct.unpack_from("<I", h, 20)[0],
            "n_links": struct.unpack_from("<I", h, 24)[0],
            "sizes": struct.unpack_from("<HHH", h, 28), "res16": struct.unpack_from("<H", h, 34)[0],
            "res32": struct.unpack_from("<I", h, 36)[0],
            "variants_max_end": struct.unpack_from("<Q", h, 40)[0],
            "artifact_id8": h[48:56], "checksum": struct.unpack_from("<Q", h, 56)[0],
        }
        n = 64 * hd["n_variants"] + 32 * hd["n_slots"] + 16 * hd["n_links"]
        f.seek(0x140)
        arrays = f.read(n)
    return hd, arrays, h


def test_identity_v6_layout(tmp_path):
    base = _base_sc(tmp_path)
    out = tmp_path / "id.sc"
    V.identity_model(str(base), b"ABCDEFGH").write(out)
    raw = open(out, "rb").read(80)
    version, = struct.unpack_from("<I", raw, 8)
    feat, = struct.unpack_from("<I", raw, 68)          # reserved -> feature_flags (G1)
    ck, = struct.unpack_from("<Q", raw, 72)
    assert version == 6 and feat == 1                    # EXT_TABLE only
    assert ck == fnv1a64(raw[:72])
    hd, arrays, h = _read_ext(out)
    assert hd["magic"] == b"QSCX6EXT" and hd["ext_version"] == 1 and hd["ext_header_size"] == 64
    assert hd["sizes"] == (64, 32, 16) and hd["res16"] == 0 and hd["res32"] == 0
    assert (hd["n_variants"], hd["n_slots"], hd["n_links"]) == (0, 0, 0)
    assert hd["variants_max_end"] == 0 and hd["artifact_id8"] == b"ABCDEFGH"
    assert hd["checksum"] == fnv1a64(arrays, fnv1a64(h[:56]))
    assert os.path.getsize(out) == os.path.getsize(base)


def test_entry_layout_and_checksum(tmp_path):
    base = _base_sc(tmp_path)
    m = V.identity_model(str(base))
    m.variants.append({"general_tb_offset": 0x1000, "tb_offset": 0x8000, "tc_offset": 0x80C0,
                       "tc_size": 0x40, "end_offset": 0x8120, "code_fnv64": 0x1122334455667788,
                       "variant_id": 0, "flags": 1, "reserved": 0})
    m.slots.append({"src_tb_offset": 0x2000, "slot_n": 1, "kind": 2, "exit_class": 1,
                    "reserved": 0, "target_vaddr": 0x80039D4, "guard_off": 0x1B8,
                    "guard_len": 116, "guard_fnv64": 0xAABBCCDDEEFF0011})
    m.links.append({"slot_index": 0, "variant_index": 0, "reserved": 0})
    m.features = 0xF
    out = tmp_path / "e.sc"
    m.write(out)
    hd, a, h = _read_ext(out)
    assert hd["variants_max_end"] == 0x8120
    assert hd["checksum"] == fnv1a64(a, fnv1a64(h[:56]))
    # VariantEntry (§5.3 table offsets)
    assert struct.unpack_from("<QQQQQQ", a, 0) == (0x1000, 0x8000, 0x80C0, 0x40, 0x8120,
                                                   0x1122334455667788)
    assert struct.unpack_from("<IIQ", a, 48) == (0, 1, 0)
    # GuardedSlotEntry at 64
    s = a[64:96]
    assert struct.unpack_from("<Q", s, 0)[0] == 0x2000
    assert (s[8], s[9], s[10], s[11]) == (1, 2, 1, 0)
    assert struct.unpack_from("<III", s, 12) == (0x80039D4, 0x1B8, 116)
    assert struct.unpack_from("<Q", s, 24)[0] == 0xAABBCCDDEEFF0011
    # LinkEntry at 96
    assert struct.unpack_from("<IIQ", a, 96) == (0, 0, 0)


def test_guard_encoding():
    """§4.1 stub: compares, aligned slot rel32, SPEC reset = jmp F, F = original tail."""
    g = V.build_guard(s_off=0x1B8, kf=0x110, kf2=0x400, vc=0x80068B0, kind=V.KIND_SPEC, slot_n=0,
                      pool_abs=0x7FFF6C0156B0, tc_abs=0x7FFF6C015500, struct_abs=0x7FFF6C015440,
                      tb_ret_abs=0x7FFF6C000018)
    c = g.code
    assert c[:8] == bytes.fromhex("4883bd80d6ffff00")           # cmp qword [rbp-0x2980],0
    assert bytes.fromhex("80bd4d01000001") in c                 # cmp byte [rbp+0x14d],1
    assert bytes.fromhex("83bd2802000000") in c                 # cmp dword [rbp+0x228],0
    assert bytes.fromhex("81bd5001000010010000") in c           # cmp dword [rbp+0x150],0x110
    assert bytes.fromhex("4881bd5801000000040000") in c         # cmp qword [rbp+0x158],0x400
    assert bytes.fromhex("81bd3c000000b0680008") in c           # cmp dword [rbp+0x3c],Vc
    assert g.slot_field % 4 == 0 and c[g.slot_field - 0x1B8 - 1] == 0xE9
    assert c[g.slot_field - 0x1B8:g.slot_field - 0x1B8 + 4] == b"\0\0\0\0"
    assert g.reset == g.slot_field + 4
    r = g.reset - 0x1B8
    assert c[r] == 0xE9 and r + 5 + struct.unpack_from("<i", c, r + 1)[0] == g.f_off - 0x1B8
    f = g.f_off - 0x1B8
    assert c[f:f + 5] == bytes.fromhex("488bfdff15") and c[f + 9:f + 11] == b"\xff\xe0"
    assert 0x7FFF6C015500 + g.f_off + 9 + struct.unpack_from("<i", c, f + 5)[0] == 0x7FFF6C0156B0
    assert len(c) == f + 11


def test_guard_general_reset_and_imm64():
    g = V.build_guard(s_off=0xC0, kf=0x110, kf2=0x80000400, vc=0x80039D4, kind=V.KIND_GENERAL,
                      slot_n=1, pool_abs=0x7FFF6C0156B0, tc_abs=0x7FFF6C015500,
                      struct_abs=0x7FFF6C015440, tb_ret_abs=0x7FFF6C000018)
    c = g.code
    assert bytes.fromhex("48b8") + struct.pack("<Q", 0x80000400) in c   # mov rax,imm64
    assert bytes.fromhex("48398558010000") in c                         # cmp [rbp+0x158],rax
    r = g.reset - 0xC0
    assert c[r:r + 3] == bytes.fromhex("488d05")                        # lea rax,[rip+d]
    lea_end = 0x7FFF6C015500 + g.reset + 7
    assert lea_end + struct.unpack_from("<i", c, r + 3)[0] == 0x7FFF6C015440 + 1   # R|n
    j = r + 7
    assert c[j] == 0xE9
    assert lea_end + 5 + struct.unpack_from("<i", c, j + 1)[0] == 0x7FFF6C000018


@pytest.mark.parametrize("name", sorted(V.MUTATIONS))
def test_every_mutation_names_a_reason(name):
    needs, fn, reason = V.MUTATIONS[name]
    assert needs in ("i", "s", "sg") and callable(fn) and reason
