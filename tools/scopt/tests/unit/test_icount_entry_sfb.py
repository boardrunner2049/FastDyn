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
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
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
        pytest.skip(f"{WP16_SFB} not present")
    from scopt.formats.sc_records import ScRecord, read_sc, write_sc

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


def test_link_sfb_last_unit_grows_after_a_move(tmp_path):
    """A rebuilt unit that grows past its neighbour moves to the arena end; the last
    unit by address, rebuilt after that move, may grow in place only up to the
    input arena end, never into the moved unit (prepare with several inputs puts
    warm-appended units last: "units 2014 and 1 overlap")."""
    from dataclasses import replace

    from scopt.formats.sc_records import ScRecord, write_sc
    from tests.synth.builders import simple_units
    from tests.unit.test_wp10_link import make_snapshot

    snap = make_snapshot(tmp_path, simple_units(3), identity=False)
    units = sorted(snap.units(), key=lambda u: u.tb_offset)
    assert [u.slot for u in units] == [0, 1, 2]
    arena_end = -(-max(u.end_offset for u in units) // 64) * 64

    def grown(u, extra):
        code = u.code + b"\x90" * extra
        rec = ScRecord.from_bytes(u.record)
        rec = rec.replace(tc_size=len(code), end_offset=u.end_offset + extra)
        return F.SfbUnit(u.uid, None, b"", (0, 0),
                         replace(u, code=code, end_offset=u.end_offset + extra,
                                 record=rec.to_bytes(fix_checksum=True)), u)

    first, last = units[0], units[-1]
    # slot 0 outgrows its neighbour and moves; the last unit grows past the arena end
    rebuilt = {first.uid: grown(first, 0x200),
               last.uid: grown(last, arena_end - last.end_offset + 16)}
    link = F.link_sfb(snap, rebuilt, derive=False)
    assert link.ok, link.failure
    assert set(link.moved) == {first.uid, last.uid}
    unit = {x.uid: (rebuilt[x.uid].unit if x.uid in rebuilt else x) for x in snap.units()}
    spans = sorted((lk.new_tb_offset,
                    lk.new_tb_offset + unit[lk.uid].end_offset - unit[lk.uid].tb_offset)
                   for lk in link.units)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), spans
    write_sc(tmp_path / "out.sc", snap.header, [(x.record, x.body) for x in link.units],
             fsync=False)


def _profiled_entries(tmp_path, *, clean=True):
    import json
    from scopt.canon import sha256_id
    from scopt.formats.profile_v1 import ingest
    from scopt.importer.import_cache import import_snapshot
    from tests.unit.test_wp7_r2_identity import _bundle, _manifest, _ri
    from tests.synth import builders as B
    from tests.unit.test_wp10_link import make_snapshot

    units = B.simple_units(4, icount_entry=True)
    make_snapshot(tmp_path, units)
    sc = tmp_path / "prep-s" / "s.sc"
    manifest = _manifest()
    Path(str(sc) + ".fastdyn.json").write_text(json.dumps(manifest))
    snap = import_snapshot(sc.parent, bundle=_bundle(manifest, sha256_id(b"firmware")),
                           runtime_identity=_ri(sc, sc.with_suffix(".scm")),
                           store=tmp_path / "bound").snapshot
    raw = tmp_path / "training.qprof"
    # Hottest is last by capture order. Two eligible entries tie and one is cold.
    dispatch = [B.Dispatch(units[i].tb_offset) for i in (3, 1, 2, 3, 1, 2, 3, 3)]
    phases = [{"name": "boot", "marker": 0}, {"name": "steady", "marker": 0x100}]
    binding = B.qprof_binding(sc)
    if not clean:
        binding["flags"] = 0
    B.simulate_qprof(B.qprof_rows_for(units), dispatch, phases=phases,
                     phase_starts={1: 0}, header=binding, path=raw)
    profile = ingest(raw, sc, snap, bundle_id=snap.bundle["bundle_id"],
                     contract_id=snap.bundle["contract_id"],
                     runtime_identity=snap.runtime["runtime_identity_id"])
    return snap, profile


def test_package_profiled_subset(tmp_path):
    import json
    from scopt.formats import artifact as A

    snap, prof = _profiled_entries(tmp_path)
    ids = [u.uid for u in snap.units()]
    all_entries = F.propose_sfb(snap)
    assert set(all_entries.units) == set(ids)
    subset = F.propose_sfb(snap, profile=prof, entry_coverage=50)
    assert subset.selection["selected"] == {ids[-1]: 4}
    assert subset.selection["eligible_exact_entries"] == 8
    assert subset.skips[ids[0]] == "zero_entries"
    assert subset.skips[ids[1]] == subset.skips[ids[2]] == "coverage_reached"
    result = F.package_sfb(snap, subset, tmp_path / "sfb", candidate_id="selective",
                           native_lows=(0, 1, 2, 3, 65535))
    assert result.status == "published", (result.reason, result.validation and result.validation.rows)
    assert subset.selection["moved_units"] == len(result.link.moved)
    assert subset.selection["code_growth_bytes"] == 16
    sidecar = json.loads((Path(result.path) / A.SIDECAR).read_text())
    assert sidecar["x_sfb"]["selection"] == subset.selection
    assert sidecar["profile_ids"][0]["profile_id"] == prof.profile_id
    for linked in result.link.units:
        if linked.uid != ids[-1]:
            unit = snap.unit(linked.uid)
            assert linked.record == unit.record
            assert linked.body == unit.code + unit.search


def test_profiled_growth_budget_and_tie_break(tmp_path):
    snap, prof = _profiled_entries(tmp_path)
    ids = [u.uid for u in snap.units()]
    prop = F.propose_sfb(snap, profile=prof, max_growth_bytes=32)
    assert list(prop.units) == [ids[3], min(ids[1:3])]
    assert prop.skips[max(ids[1:3])] == "code_growth_budget"
    assert not F.propose_sfb(snap, profile=prof, max_growth_bytes=15).units
    assert not F.propose_sfb(snap, profile=prof, max_units=0).units
    no_moves = F.propose_sfb(snap, profile=prof, max_moved_units=0)
    assert not no_moves.units and no_moves.skips[ids[3]] == "moved_units_budget"
    with pytest.raises(ValueError, match="require a training profile"):
        F.propose_sfb(snap, entry_coverage=50)
    with pytest.raises(ValueError, match="missing or unreached"):
        F.propose_sfb(snap, profile=prof, phase="missing")


def test_sfb_package_command_with_profile(tmp_path):
    import json
    from click.testing import CliRunner
    from scopt.commands.b4 import sfb

    snap, profile = _profiled_entries(tmp_path)
    path = tmp_path / "train.profile.json"
    path.write_bytes(profile.to_bytes())
    result = CliRunner().invoke(sfb, ["package", snap.path, "--store", str(tmp_path / "art"),
                                      "--profile", str(path), "--entry-coverage", "50"])
    assert result.exit_code == 0, (result.output, result.exception)
    doc = json.loads(result.output)
    assert doc["status"] == "published" and doc["proposal"]["units"] == 1
    assert doc["proposal"]["selection"]["profile_id"] == profile.profile_id
    assert not any(row["result"] == "fail" for row in doc["validation"])


def test_selective_sfb_refuses_lower_bound_only_profile(tmp_path):
    snap, prof = _profiled_entries(tmp_path, clean=False)
    with pytest.raises(ValueError, match="requires exact entry counts"):
        F.propose_sfb(snap, profile=prof, entry_coverage=90)
