"""WP25: icount-entry-sfb@1, the baseline-TCG entry fix (LLVM survey §3c B4-1).

Not a B3/B4 pass: own pipeline, own packager, own validation rows.
"""

from __future__ import annotations

import os
import shutil
import struct
from pathlib import Path

import pytest

from scopt.decode.x86 import sweep
from scopt.passes import icount_entry_sfb as F

ROOT = Path(os.environ.get("FASTDYN_ROOT", "/data/extra_copy/fastdyn"))
BLINKY = ROOT / ("runs/b2/blinky/store/snapshots/"
                 "4879c8f7d4c10a5af3091555bba1f7744d06d95072bed87a219408dc8935ef2f")
WP16_SFB = ROOT / "runs/b3-explore/wp16/copies/b4/sfb/cache.sc"
COMPUTE = ROOT / ("runs/b2/compute/store/snapshots/"
                  "53b4132fc05526ff2925cd3acc610bf613e8b11e2315e15e520bff7fe080dab2")


def _old_entry(reg: str, n: int) -> bytes:
    """TCG's entry (gen_tb_start) for ``reg`` with the exit right after it (+ 4 nops)."""
    if reg == "ebx":
        load, test, st = bytes.fromhex("8b5df8"), bytes.fromhex("85db"), bytes.fromhex("66895df8")
        add = bytes.fromhex("ffcb") if n == 1 else bytes.fromhex("83c3") + struct.pack("<b", -n)
    else:
        load, test, st = (bytes.fromhex("448b65f8"), bytes.fromhex("4585e4"),
                          bytes.fromhex("66448965f8"))
        add = bytes.fromhex("41ffcc") if n == 1 else bytes.fromhex("4183c4") + struct.pack("<b", -n)
    pre = load + add + test
    total = len(pre) + 6 + len(st)
    jl = b"\x0f\x8c" + struct.pack("<i", (total + 4) - (len(pre) + 6))
    return pre + jl + st


class _U:
    def __init__(self, code: bytes, insn0: int):
        self.code = code + b"\x90" * 4
        self.insn0_start = insn0


@pytest.mark.parametrize("reg,n", [("ebx", 1), ("ebx", 8), ("ebx", 100), ("r12d", 1),
                                   ("r12d", 9)])
def test_entry_parse_encode_and_native_equivalence(reg, n):
    old = _old_entry(reg, n)
    e = F.parse_entry(_U(old, len(old)))
    assert e is not None and (e.reg, e.n, e.length) == (reg, n, len(old))
    new, jf = F.encode_sfb_entry(reg, n, e.exit_target + F.SFB_SHIFT, len(old) + F.SFB_SHIFT)
    assert len(new) == len(old) + 16
    assert F.check_sfb_entry(new, e) == []
    ins = sweep(new, 0)
    assert [i.mnemonic for i in ins] == ["movzx", "cmp", "jne", "sub", "jb", "mov"]
    assert {ins[2].address + 2, ins[4].address + 2} == set(jf)
    # exhaustive: every low for high in {0, 0xffff}
    assert F.entry_semantics(old, new, reg) == []


def test_native_check_catches_wrong_entries():
    old = _old_entry("ebx", 8)
    e = F.parse_entry(_U(old, len(old)))
    new, _ = F.encode_sfb_entry("ebx", 8, e.exit_target + 16, len(old) + 16)
    for i in sweep(new, 0):
        if i.mnemonic == "sub":  # wrong decrement
            bad = bytearray(new)
            bad[i.address + i.size - 1] ^= 1
            assert F.entry_semantics(old, bytes(bad), "ebx", lows=range(0, 64))
        if i.mnemonic == "cmp":  # check the low half instead of the high half
            bad = bytearray(new)
            bad[i.address + 3] = 0xF8  # [rbp-8]
            assert F.entry_semantics(old, bytes(bad), "ebx", lows=range(0, 64))
        if i.mnemonic == "jb":  # jb -> jbe: exits one count early
            bad = bytearray(new)
            bad[i.address + 1] = 0x86
            assert F.entry_semantics(old, bytes(bad), "ebx", lows=range(0, 64))


def test_unsupported_shapes():
    assert F.parse_entry(_U(bytes.fromhex("8b5df885db0f8c00000000"), 11)) is None  # noicount
    old = _old_entry("ebx", 8)
    assert F.parse_entry(_U(old, len(old) - 1)) is None  # entry not inside the prefix


def _snap(p):
    if not (p / "snapshot.json").is_file():
        pytest.skip(f"{p} not present")
    from scopt.importer.import_cache import open_snapshot

    return open_snapshot(p)


@pytest.mark.real
@pytest.mark.slow
def test_package_blinky(tmp_path):
    s = _snap(BLINKY)
    prop = F.propose_sfb(s)
    assert prop.units and set(prop.skips.values()) <= {"cpu_noirq_shape"}
    for uid in list(prop.units)[:3]:
        assert prop.units[uid]["label"] == F.LABEL
    r = F.package_sfb(s, prop, tmp_path, candidate_id="t-sfb-blinky",
                      native_lows=range(0, 65536, 251))
    assert r.status == "published", (r.reason, r.validation and r.validation.rows)
    codes = {(x["code"], x["check"]): x["result"] for x in r.validation.rows}
    assert all(v == "pass" for v in codes.values()), codes
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.mark.real
@pytest.mark.slow
def test_compute_matches_wp16_timed_copy(tmp_path):
    """The rebuild is byte-identical to WP16's timed ``sfb`` copy in every record field
    except end_offset (16-aligned search padding here) and in every code/search byte."""
    if not WP16_SFB.is_file():
        pytest.skip("WP16 copy not present")
    from scopt.formats.sc_v5 import ScRecord, read_sc, write_sc

    s = _snap(COMPUTE)
    prop = F.propose_sfb(s)
    rebuilt = {uid: F.sfb_rebuild(s.unit(uid), s.header.code_address) for uid in prop.units}
    link = F.link_sfb(s, rebuilt, derive=False)
    assert link.ok and len(link.moved) == 479
    out = tmp_path / "cache.sc"
    write_sc(out, s.header, [(x.record, x.body) for x in link.units])
    fa, fb = WP16_SFB.read_bytes(), out.read_bytes()
    a, b = read_sc(WP16_SFB), read_sc(out)
    for ra, rb in zip(a.record_bytes, b.record_bytes):
        A, B = ScRecord.from_bytes(ra), ScRecord.from_bytes(rb)
        assert A.replace(end_offset=0).to_bytes(fix_checksum=True) == \
            B.replace(end_offset=0).to_bytes(fix_checksum=True)
        base = 0x10000
        assert fa[base + A.tc_offset:base + A.tc_offset + A.tc_size] == \
            fb[base + B.tc_offset:base + B.tc_offset + B.tc_size]
