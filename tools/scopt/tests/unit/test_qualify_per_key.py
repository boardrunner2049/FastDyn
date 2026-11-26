"""WP8: T-B1-1 weaker per-key comparison (§2.6) and the RUN_INFO/MAPS checks.

Real evidence: the WP-QINT ``scopt qualify-capture`` run on minimal
(``runs/qint/t-b1-1-minimal``: three capture-off and two capture-on cold runs
of the integrated binary, one ``.scm`` per on run). The off/on runs there are
byte-identical, so the weaker mode is exercised by relocating units of an off
run synthetically (as a nondeterministic run would place them) and checking
that only self-relative fields are normalized while every absolute host value
must still be equal.
"""

from __future__ import annotations

import struct
from dataclasses import replace
from pathlib import Path

import pytest

from scopt.formats.enums import Encoding, SiteKind
from scopt.formats.sc_records import read_sc
from scopt.formats.scm_v1 import read_scm
from scopt.run.identity_cmp import maps_layout, per_key_compare, scm_checks, unit_images
from scopt.paths import fastdyn_root

pytestmark = pytest.mark.real

QINT = (fastdyn_root() / "runs/qint/t-b1-1-minimal")
B0_SC = (fastdyn_root() / "runs/b0/cache/minimal.sc")


@pytest.fixture(scope="module")
def ev():
    p = {n: QINT / f"archive/{n}.sc" for n in ("on1", "off1")}
    scm = QINT / "scm/on1.scm"
    if not (all(x.exists() for x in p.values()) and scm.exists()):
        pytest.skip(f"{p['on1']}, {p['off1']} or {scm} not present")
    on, off = read_sc(p["on1"]), read_sc(p["off1"])
    return on, off, read_scm(scm)


def _relocate(img, d, sites):
    """Move a unit by ``d`` bytes in the arena, fixing only what a move changes:
    rel32/disp32 fields that leave the unit (prologue and external targets)
    and IMM64 self pointers."""
    body = bytearray(img.body)
    tc_size = img.record.tc_size
    end_rel = img.record.end_offset - img.record.tc_offset
    tb_rel = img.record.tb_offset - img.record.tc_offset
    for s in sites:
        if s.width == 4 and s.enc in (Encoding.REL32, Encoding.RIPREL32, Encoding.GOTO_TB_REL32):
            disp = struct.unpack_from("<i", body, s.field_off)[0]
            if not (tb_rel <= s.field_off + 4 + disp < end_rel):
                struct.pack_into("<i", body, s.field_off, disp - d)
        elif s.width == 8 and s.enc == Encoding.IMM64 and s.kind in (
                SiteKind.MOVI_EXIT_TB_PTR, SiteKind.MOVI_LDST_RA):
            v = struct.unpack_from("<Q", body, s.field_off)[0]
            struct.pack_into("<Q", body, s.field_off, v + d)
    assert tc_size <= len(body)
    rec = replace(img.record, tb_offset=img.record.tb_offset + d,
                  tc_offset=img.record.tc_offset + d, end_offset=img.record.end_offset + d)
    return replace(img, record=rec, body=bytes(body))


def test_identical_runs_compare_equal(ev):
    on, off, scm = ev
    c = per_key_compare(unit_images(on), unit_images(off), scm.tbs)
    assert c["ok"] and c["common_units"] == 163 and c["mismatches"] == 0
    assert c["normalized_units"] == 0  # byte-identical: nothing to normalize


def test_relocated_units_pass_only_via_self_relative_normalization(ev):
    on, off, scm = ev
    a = unit_images(on)
    b = unit_images(off)
    moved = 0
    for uid, img in list(b.items()):
        if img.slot % 3 == 0:
            b[uid] = _relocate(img, 0x40000 + 64 * img.slot, scm.tbs[img.slot].sites)
            moved += 1
    c = per_key_compare(a, b, scm.tbs)
    assert c["ok"], c["examples"]
    # every moved unit with a prologue jump or helper call needed normalization
    assert 0 < c["normalized_units"] <= moved

    # an absolute host value that changed (external helper call target) is a mismatch
    uid, img = next((u, i) for u, i in b.items() if any(
        s.kind == SiteKind.HELPER_CALL and s.enc == Encoding.REL32
        for s in scm.tbs[i.slot].sites))
    s = next(s for s in scm.tbs[img.slot].sites
             if s.kind == SiteKind.HELPER_CALL and s.enc == Encoding.REL32)
    body = bytearray(img.body)
    struct.pack_into("<i", body, s.field_off, struct.unpack_from("<i", body, s.field_off)[0] + 16)
    b2 = {**b, uid: replace(img, body=bytes(body))}
    c = per_key_compare(a, b2, scm.tbs)
    assert not c["ok"] and c["mismatches"] == 1
    assert c["examples"][0]["uid"] == uid and "sites" in c["examples"][0]["why"][0]


def test_unexplained_byte_and_record_field_mismatch(ev):
    on, off, scm = ev
    a, b = unit_images(on), unit_images(off)
    uid, img = next(iter(b.items()))
    # a byte that is not a declared field (here: the first code byte) must be equal
    body = bytearray(img.body)
    body[0] ^= 0xFF
    c = per_key_compare(a, {**b, uid: _relocate(replace(img, body=bytes(body)), 0x80000,
                                                 scm.tbs[img.slot].sites)}, scm.tbs)
    assert c["mismatches"] == 1 and "first_unexplained_byte" in c["examples"][0]["why"][-1]
    # records must be equal modulo offsets (jmp_reset_offset is not an offset field)
    rec = replace(img.record, jmp_reset_offset=(img.record.jmp_reset_offset[0] + 1,
                                                img.record.jmp_reset_offset[1]))
    c = per_key_compare(a, {**b, uid: replace(img, record=rec)}, scm.tbs)
    assert c["mismatches"] == 1
    assert c["examples"][0]["why"][0] == {"record_fields": ["jmp_reset_offset"]}
    # without a bound capture a differing unit cannot be normalized
    c = per_key_compare(a, {**b, uid: _relocate(img, 0x80000, scm.tbs[img.slot].sites)}, {})
    assert c["unbound"] == 1 and not c["ok"]


def test_different_binaries_never_compare_equal(ev):
    """Same-binary rule: B0's cache vs the integrated binary differs in helper addresses."""
    on, _, scm = ev
    if not B0_SC.exists():
        pytest.skip(f"{B0_SC} not present")
    c = per_key_compare(unit_images(on), unit_images(read_sc(B0_SC)), scm.tbs)
    assert c["common_units"] == 163 and c["mismatches"] > 0


def test_scm_checks(ev):
    on, off, scm = ev
    chk = scm_checks(scm, on)
    assert chk["failures"] == []
    assert chk["tb_chunks"] == chk["tb_chunks_bound"] == chk["valid_slots"] == 163
    lay = chk["maps_layout"]
    assert lay and not any(line.endswith(".scm") for line in lay)
    assert maps_layout(b"7f00-7f01 r-xp 00000000 08:02 1234 /usr/lib/x.so\n") == (
        "7f00-7f01 r-xp 00000000 /usr/lib/x.so",)
    # a capture from another run (other header) is caught by the RUN_INFO check
    other = replace(on, header=on.header.replace(runtime_signature=on.header.runtime_signature
                                                 ^ 1))
    assert any("runtime_signature" in f for f in scm_checks(scm, other)["failures"])


def _write_relocated(src_sc, scm, dst, d):
    """``src_sc`` with every unit moved by ``d`` (a nondeterministic run's placement)."""
    from scopt.formats.sc_records import write_sc

    imgs = unit_images(src_sc)
    units = []
    for img in sorted(imgs.values(), key=lambda i: i.slot):
        m = _relocate(img, d, scm.tbs[img.slot].sites)
        units.append((m.record.with_checksum(), m.body))
    write_sc(dst, src_sc.header, units, fsync=False)


@pytest.mark.slow
def test_qualify_capture_gate_per_key_mode(ev, tmp_path, monkeypatch, fake_runtime_qemu):
    """qualify_capture end to end with the launcher replaced by recorded runs.

    off1 and off2 differ as whole files (off2 has every unit moved), so the
    gate falls back to the per-key mode and passes; adding B0's cache from
    another binary as off3 makes it fail."""
    import shutil

    from scopt.formats.sc_records import sparse_copy
    from scopt.run import qualify as Q

    on, off, scm = ev
    relocated = tmp_path / "relocated.sc"
    _write_relocated(off, scm, relocated, 0x40000)
    base = (fastdyn_root() / "src/fastdyn/configs/b0-sc.toml")
    if not base.exists():
        pytest.skip(f"{base} not present")

    def run_with(sources, out):
        def fake_launch(rd, *, extra_env=None, run_json, **kw):
            name = run_json.split(".")[0]
            sparse_copy(sources[name], rd.cache, fsync=False)
            log = rd.root / f"{name}.stdout.log"
            text = "Reg 0:0x00000003\n"
            if extra_env and extra_env.get("QEMU_SC_CAPTURE"):
                shutil.copyfile(QINT / "scm/on1.scm", extra_env["QEMU_SC_CAPTURE"])
                text += Q.BANNER + "\n"
            log.write_text(text)
            return {"exit_code": 0, "stdout_log": str(log), "guest_norm": ["Reg 0:0x00000003"],
                    "stray_capture_files": [], "qemu": "/q", "qemu_sha256": "sha256:x"}

        monkeypatch.setattr(Q, "launch", fake_launch)
        return Q.qualify_capture(base, out, off=len(sources) - 2, on=2)

    srcs = {"off1": QINT / "archive/off1.sc", "off2": relocated,
            "on1": QINT / "archive/on1.sc", "on2": QINT / "archive/on2.sc"}
    doc = run_with(srcs, tmp_path / "q-pass")
    assert doc["mode"] == "per_key" and doc["weaker_mode"], doc["failures"]
    assert doc["verdict"] == "PASS", doc["failures"]
    pk = doc["per_key"]["on1~off2"]
    assert pk["ok"] and pk["common_units"] == 163 and pk["normalized_units"] > 0
    assert doc["per_key"]["on1~off1"]["normalized_units"] == 0

    if B0_SC.exists():
        doc = run_with({**srcs, "off3": B0_SC}, tmp_path / "q-fail")
        assert doc["mode"] == "per_key" and doc["verdict"] == "FAIL"
        assert any("on1 vs off3" in f for f in doc["failures"])
