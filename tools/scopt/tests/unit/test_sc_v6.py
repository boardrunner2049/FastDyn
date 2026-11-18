"""WP18 T-B3-1: ``.sc`` v6 writer, reader and the independent parser (design §5.2-5.4, V11).

Round trip through ``scopt.formats.sc_v6`` and ``notes/critic-scripts/sc6parse.py``
(written from the C loader of ``wt/qemu-v6``) on synthetic and real tables;
every corruption case gives the loader's reason (the message ``sc6_die``
prints) in both parsers.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from scopt.canon import fnv1a64
from scopt.formats.sc_v5 import ScFormatError, ScRecord, read_sc, write_sc
from scopt.formats.sc_v6 import (FEAT_EXT_TABLE, FEAT_GUARDED_SLOTS, FEAT_VARIANTS,
                                 KIND_GENERAL, SC6PARSE, ExtTable, LinkEntry, SlotEntry,
                                 VariantEntry, ext_problem, read_sc6, sc6_snapshot_id,
                                 sc6parse_compare, write_sc6)
from scopt.link import B3Plan, NativeLinkerV2, arena_limit
from tests.synth import b3 as H

pytestmark = pytest.mark.skipif(not SC6PARSE.is_file(), reason="sc6parse.py not present")


# ---------------------------------------------------------------------------
# the base artifact: minimal snapshot, SPEC links with variants + GENERAL slots
# ---------------------------------------------------------------------------

def _base(name: str = "minimal"):
    s = H.snap(name)
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts[:2]]
    grow, vs = H.spec_pairs(s, hosts[:2], cs)
    gen = H.dummy_specs(s, limit=6)
    for k, v in gen.items():
        if k not in grow:
            grow[k] = v
            break
    plan = B3Plan(grow=grow, variants=tuple(vs))
    res = NativeLinkerV2(s, plan, offsets=H.offsets_for(s), derive_captures=False).link()
    assert res.ok, res.failures
    assert res.version == 6 and res.ext.links and res.ext.variants
    return s, res


def _write(tmp: Path, s, res, *, ext=None, features=None, parts=None, bodies=None,
           check=False, header=None) -> Path:
    p = tmp / "cache.sc"
    write_sc6(p, header or s.header, parts if parts is not None else [(u.record, u.body) for u in res.units],
              ext if ext is not None else res.ext,
              bodies if bodies is not None else [v.body for v in res.variants],
              features=features, check=check, fsync=False)
    return p


def _both(p: Path):
    f = read_sc6(p)
    ok, det, doc = sc6parse_compare(p, f)
    return f, ok, det, doc


def test_roundtrip_real_minimal(tmp_path):
    s, res = _base()
    p = _write(tmp_path, s, res, check=True)
    f, ok, det, doc = _both(p)
    assert f.problem is None and f.is_v6, f.problem
    assert ok, det
    assert doc["verdict"] == "v6_ok"
    assert f.ext.variants == res.ext.variants and f.ext.slots == res.ext.slots
    assert f.ext.links == res.ext.links
    assert list(f.variant_bodies) == [v.body for v in res.variants]
    # header: version 6, feature flags, covered by the checksum
    assert f.sc.header.version == 6 and f.sc.header.checksum_ok
    assert f.features == FEAT_EXT_TABLE | FEAT_VARIANTS | FEAT_GUARDED_SLOTS
    # the stock (v5) reader refuses the file, as the v5 loader does
    assert read_sc(p).header_problems == ("version",)
    # canonical: nothing but header, ext, variants, records
    assert not f.structural_findings()
    assert sc6_snapshot_id(f) != f.sc.snapshot_id()


@pytest.mark.real
@pytest.mark.slow
def test_roundtrip_real_compute(tmp_path):
    s, res = _base("compute")
    p = _write(tmp_path, s, res, check=True)
    f, ok, det, _doc = _both(p)
    assert f.problem is None and ok, (f.problem, det)


def test_identity_v6_and_v5(tmp_path):
    s = H.snap("minimal")
    res = NativeLinkerV2(s, B3Plan(force_v6=True), derive_captures=False).link()
    assert res.ok and res.version == 6 and not res.ext.variants and not res.ext.slots
    p = _write(tmp_path, s, res, check=True)
    f, ok, det, _ = _both(p)
    assert f.problem is None and ok, det
    assert f.features == FEAT_EXT_TABLE
    assert [f.sc.record_bytes[i] for i in f.sc.valid_slots] == [u.record for u in s.units()]
    res5 = NativeLinkerV2(s, B3Plan(), derive_captures=False).link()
    assert res5.version == 5 and res5.ext is None


def test_synthetic_table_roundtrip(tmp_path):
    """A hand-made table on a synthetic file (no snapshot): one GENERAL slot on a
    synthetic record whose code has a registered jump inside the 'guard'."""
    from tests.synth import builders as B

    units = B.simple_units(3)
    h = B.sc_header_fields(units, {})
    h.pop("checksum")
    from scopt.formats.sc_v5 import ScHeader

    hdr = ScHeader(**h, checksum=0).with_checksum()
    res = [B.resolve_unit(u) for u in units]
    parts = []
    for i, r in enumerate(res):
        rec = ScRecord.from_bytes(r.record_bytes)
        if i == 0:  # register jump 0 at a 4-aligned field inside [0, tc_size)
            jo = 8 if rec.tc_size >= 16 else 0
            rec = rec.replace(jmp_insn_offset=(jo, rec.jmp_insn_offset[1]),
                              jmp_reset_offset=(jo + 4, rec.jmp_reset_offset[1]))
        parts.append((rec.to_bytes(fix_checksum=True), r.arena_bytes))
    r0 = ScRecord.from_bytes(parts[0][0])
    glen = r0.tc_size
    ext = ExtTable(slots=(SlotEntry(r0.tb_offset, 0, KIND_GENERAL, 2, 0x100, 0, glen,
                                    fnv1a64(parts[0][1][:glen])),), artifact_id8=b"WP18TEST")
    p = tmp_path / "s.sc"
    write_sc6(p, hdr, parts, ext, [])
    f, ok, det, _ = _both(p)
    assert f.problem is None and ok, (f.problem, det)
    assert f.ext.slots == ext.slots


# ---------------------------------------------------------------------------
# corruption cases: (name, mutate(ctx) -> kwargs for _write, expected loader text)
# ---------------------------------------------------------------------------

def _ext(**kw):
    return lambda c: {"ext": replace(c["res"].ext, **kw)}


def _var(i=0, **kw):
    def f(c):
        vs = list(c["res"].ext.variants)
        vs[i] = replace(vs[i], **kw)
        return {"ext": replace(c["res"].ext, variants=tuple(vs))}
    return f


def _slot(i=0, **kw):
    def f(c):
        ss = list(c["res"].ext.slots)
        ss[i] = replace(ss[i], **kw)
        return {"ext": replace(c["res"].ext, slots=tuple(ss))}
    return f


def _link(i=0, **kw):
    def f(c):
        ls = list(c["res"].ext.links)
        ls[i] = replace(ls[i], **kw)
        return {"ext": replace(c["res"].ext, links=tuple(ls))}
    return f


def _feat(fn):
    return lambda c: {"features": fn(c["feat"])}


def _general_slot_index(c) -> int:
    return next(i for i, s in enumerate(c["res"].ext.slots) if s.kind == KIND_GENERAL)


def _unregistered(c):
    res = c["res"]
    rec = next(ScRecord.from_bytes(u.record) for u in res.units
               if ScRecord.from_bytes(u.record).jmp_insn_offset == (0xFFFF, 0xFFFF))
    ss = list(res.ext.slots)
    ss[0] = replace(ss[0], src_tb_offset=rec.tb_offset)
    return {"ext": replace(res.ext, slots=tuple(ss))}


def _misalign_jump(c):
    res = c["res"]
    s0 = res.ext.slots[0]
    parts = [(u.record, u.body) for u in res.units]
    for i, (rb, body) in enumerate(parts):
        r = ScRecord.from_bytes(rb)
        if r.tb_offset == s0.src_tb_offset:
            jio = list(r.jmp_insn_offset)
            jio[s0.slot_n] += 1
            parts[i] = (r.replace(jmp_insn_offset=tuple(jio)).to_bytes(fix_checksum=True), body)
    return {"parts": parts}


def _dup_record(c):
    parts = [(u.record, u.body) for u in c["res"].units]
    return {"parts": parts + [parts[-1]]}


def _flip_variant(c):
    bodies = [bytearray(v.body) for v in c["res"].variants]
    bodies[0][3] ^= 0xFF
    return {"bodies": [bytes(b) for b in bodies]}


def _flip_guard(c):
    res = c["res"]
    s0 = res.ext.slots[0]
    parts = [(u.record, bytearray(u.body)) for u in res.units]
    for rb, body in parts:
        if ScRecord.from_bytes(rb).tb_offset == s0.src_tb_offset:
            body[s0.guard_off + 2] ^= 0x01
    return {"parts": [(r, bytes(b)) for r, b in parts]}


def _end_at_bound(c):
    """Variant 0 ends exactly at the bound ``code_size - host_page - 1024`` (the
    rule is strict ``<``): move it to a 64-aligned tb near the arena end and
    pick ``code_size`` so that the bound equals its end."""
    s, res = c["s"], c["res"]
    h = s.header
    v = res.ext.variants[0]
    span = v.end_offset - v.tb_offset
    tb = (arena_limit(h) - span - 4096) // 64 * 64
    code_size = tb + span + h.host_page_size + 1024
    vs = list(res.ext.variants)
    d = tb - v.tb_offset
    vs[0] = replace(v, tb_offset=tb, tc_offset=v.tc_offset + d, end_offset=v.end_offset + d)
    assert vs[0].end_offset == code_size - h.host_page_size - 1024
    return {"ext": replace(res.ext, variants=tuple(vs), variants_max_end=None),
            "header": h.replace(code_size=code_size)}


def _overlap_record(c):
    res = c["res"]
    r = ScRecord.from_bytes(res.units[3].record)
    v = res.ext.variants[0]
    d = r.tb_offset - v.tb_offset
    return _shift_variant(c, d)


def _shift_variant(c, d, i=0):
    res = c["res"]
    vs = list(res.ext.variants)
    v = vs[i]
    vs[i] = replace(v, tb_offset=v.tb_offset + d, tc_offset=v.tc_offset + d,
                    end_offset=v.end_offset + d)
    return {"ext": replace(res.ext, variants=tuple(vs), variants_max_end=None)}


def _overlap_variant(c):
    res = c["res"]
    v0, v1 = res.ext.variants[0], res.ext.variants[1]
    return _shift_variant(c, v0.tb_offset - v1.tb_offset, 1)


def _two_links_slot(c):
    res = c["res"]
    ls = list(res.ext.links)
    ls[1] = replace(ls[1], slot_index=ls[0].slot_index)
    return {"ext": replace(res.ext, links=tuple(ls))}


def _two_links_variant(c):
    res = c["res"]
    ls = list(res.ext.links)
    ls[1] = replace(ls[1], variant_index=ls[0].variant_index)
    return {"ext": replace(res.ext, links=tuple(ls))}


def _link_general(c):
    return _link(0, slot_index=_general_slot_index(c))(c)


def _drop_link(c):
    res = c["res"]
    return {"ext": replace(res.ext, links=res.ext.links[1:])}


def _page_offset(c):
    res = c["res"]
    k = res.ext.links[0].slot_index
    return _slot(k, target_vaddr=res.ext.slots[k].target_vaddr + 2)(c)


def _dup_slot(c):
    res = c["res"]
    return {"ext": replace(res.ext, slots=res.ext.slots + (res.ext.slots[0],)),
            "features": None}


def _too_large(c):
    return {"ext": replace(c["res"].ext, n_variants=2000, checksum=0)}


CASES = [
    ("unknown_feature_bit", _feat(lambda f: f | 0x10), "unknown feature bits 0x10"),
    ("ext_table_clear", _feat(lambda f: f & ~FEAT_EXT_TABLE), "feature EXT_TABLE clear"),
    ("bad_magic", _ext(magic=b"QSCX6EXX"), "bad ext magic"),
    ("ext_version", _ext(ext_version=2), "bad ext_version 2"),
    ("ext_header_size", _ext(ext_header_size=72), "bad ext_header_size 72"),
    ("entry_sizes", _ext(slot_entry_size=40), "bad entry sizes 64/40/16"),
    ("ext_reserved", _ext(reserved1=1), "reserved header fields not zero"),
    ("too_large", _too_large, "ext table too large"),
    ("checksum", _ext(checksum=0x1234), "ext checksum mismatch"),
    ("feature_variants", _feat(lambda f: f & ~FEAT_VARIANTS), "feature VARIANTS disagrees"),
    ("feature_slots", _feat(lambda f: f & ~FEAT_GUARDED_SLOTS),
     "feature GUARDED_SLOTS disagrees"),
    ("artifact_id_zero", _ext(artifact_id8=b"\0" * 8), "artifact id is zero"),
    ("duplicate_record", _dup_record, "duplicate record tb_offset"),
    ("variant_general", _var(general_tb_offset=0x40), "names no valid record"),
    ("variant_id", _var(variant_id=5), "variant 0: variant_id 5 is not dense"),
    ("variant_flags", _var(flags=2), "variant 0: unknown flags 0x2"),
    ("variant_reserved", _var(reserved=1), "variant 0: reserved not zero"),
    ("variant_align", lambda c: _shift_variant(c, 16), "not 64-aligned"),
    ("variant_tc_gap", lambda c: _var(tc_offset=c["res"].ext.variants[0].tc_offset + 64)(c),
     "tc_offset - tb_offset != 192"),
    ("variant_tc_size", lambda c: _var(tc_size=c["res"].ext.variants[0].tc_size + 1)(c),
     "variant 0: tc_size 0x"),
    ("variant_extent", lambda c: _var(end_offset=c["res"].ext.variants[0].end_offset + 16)(c),
     "end_offset does not match C's extent"),
    ("variant_below_prologue", lambda c: _shift_variant(c, -c["res"].ext.variants[0].tb_offset),
     "below the prologue"),
    ("variant_end_at_bound", _end_at_bound, "out of range"),
    ("variant_overlaps_record", _overlap_record, "overlaps record at tb_offset"),
    ("variant_overlaps_variant", _overlap_variant, "variant 1: overlaps variant 0"),
    ("variants_max_end", lambda c: _ext(variants_max_end=c["res"].ext.computed_max_end + 64)(c),
     "variants_max_end 0x"),
    ("slot_src", _slot(src_tb_offset=0x40), "slot 0: src_tb_offset 0x40 names no valid record"),
    ("slot_n", _slot(slot_n=2), "slot 0: slot_n 2"),
    ("slot_kind", _slot(kind=3), "slot 0: bad kind 3"),
    ("slot_exit_class", _slot(exit_class=0), "slot 0: bad exit_class 0"),
    ("slot_reserved", _slot(reserved=1), "slot 0: reserved not zero"),
    ("slot_unregistered", _unregistered, "R has no registered jump"),
    ("guard_range", lambda c: _slot(guard_len=0x100000)(c), "outside R's code"),
    ("jump_outside_guard", _slot(guard_len=1), "outside the guard"),
    ("jump_align", _misalign_jump, "jump field not 4-aligned"),
    ("slot_duplicate", _dup_slot, "duplicates slot 0"),
    ("link_reserved", _link(reserved=1), "link 0: reserved not zero"),
    ("link_slot_range", _link(slot_index=99), "link 0: slot_index 99 out of range"),
    ("link_variant_range", _link(variant_index=99), "link 0: variant_index 99 out of range"),
    ("link_not_spec", _link_general, "is not SPEC"),
    ("slot_two_links", _two_links_slot, "more than one link"),
    ("variant_two_links", _two_links_variant, "more than one link"),
    ("link_page_offset", _page_offset, "is not at C's page offset"),
    ("spec_unlinked", _drop_link, "SPEC slot has no link"),
    ("variant_code_hash", _flip_variant, "variant 0: code hash mismatch"),
    ("guard_hash", _flip_guard, "slot 0: guard hash mismatch"),
]


@pytest.fixture(scope="module")
def base():
    return _base()


@pytest.mark.parametrize("name,mut,want", CASES, ids=[c[0] for c in CASES])
def test_corruption_gives_loader_reason(tmp_path, base, name, mut, want):
    s, res = base
    ctx = {"s": s, "res": res, "feat": res.ext.features(rewritten=True)}
    kw = mut(ctx)
    if "features" not in kw or kw["features"] is None:
        kw["features"] = ctx["feat"] if "features" not in kw else None
    p = _write(tmp_path, s, res, **kw)
    f, ok, det, doc = _both(p)
    assert f.problem is not None, f"{name}: accepted"
    assert want in f.problem[1], (name, f.problem)
    assert doc.get("die") == f.problem[1], (name, doc.get("die"), f.problem)
    assert ok, det  # every header field, record and entry agrees too


def test_writer_refuses_a_bad_table(tmp_path, base):
    s, res = base
    with pytest.raises(ScFormatError, match="ext checksum mismatch|variants_max_end"):
        _write(tmp_path, s, res, ext=replace(res.ext, variants_max_end=1), check=True)


def test_ext_problem_reads_hashes_lazily(base):
    """``check_hashes=False`` stops before the code/guard hashes (loader order)."""
    s, res = base
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = _write(Path(td), s, res, **_flip_variant({"res": res}),
                   features=res.ext.features(rewritten=True))
        f = read_sc(p, versions=(5, 6))
        fd = os.open(p, os.O_RDONLY)
        try:
            def rd(off, n):
                b = os.pread(fd, n, off)
                return b + bytes(n - len(b))
            loaded = [f.records[i] for i in f.valid_slots]
            assert ext_problem(f.header, loaded, rd, check_hashes=False)[1] is None
            assert ext_problem(f.header, loaded, rd)[1][0] == "variant_code_hash"
        finally:
            os.close(fd)


def test_v5_writer_unchanged(tmp_path):
    """The v5 path of write_sc keeps rejecting a v6 header without ``versions``."""
    s = H.snap("minimal")
    with pytest.raises(ScFormatError):
        write_sc(tmp_path / "x.sc", s.header.replace(version=6),
                 [(u.record, u.code + u.search) for u in s.units()])
    assert LinkEntry(0, 0).encode() == b"\0" * 16
    assert len(VariantEntry(0, 0, 0, 0, 0, 0, 0).encode()) == 64


@pytest.mark.parametrize("name,mut,want", CASES, ids=[c[0] for c in CASES])
def test_validate_b3_rows_not_raise_on_refused_tables(tmp_path, base, name, mut, want):
    """A standalone validate_b3 on a file the loader refuses emits fail rows and
    never raises (review rework 2: duplicate_record, link_slot_range and
    link_variant_range used to raise KeyError/IndexError)."""
    from scopt.validate import validate_b3, write_b3_files

    s, res = base
    ctx = {"s": s, "res": res, "feat": res.ext.features(rewritten=True)}
    kw = mut(ctx)
    if "features" not in kw or kw["features"] is None:
        kw["features"] = ctx["feat"] if "features" not in kw else None
    d = tmp_path / "art"
    write_b3_files(d, s, res)
    (d / "cache.sc").unlink()
    _write(d, s, res, **kw)
    skip = name not in ("duplicate_record", "link_slot_range", "link_variant_range")
    v, _ = validate_b3(s, res, d, skip_reimport=skip)
    assert not v.accepted, name
    assert any(r["code"] == "V11" and r["result"] == "fail" for r in v.rows), name
