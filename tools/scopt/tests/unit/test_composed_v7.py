"""Composed canonical v7: a B3 semantic extension and trusted firmware regions.

The semantic payload keeps 0x100 (arrays ending at or before 0xFF00) and the
v7 directory header moves to 0xFF00; ``reserved = 1<<31 | 1<<30 | sc6 bits``.
These are the scopt-side mirrors of QEMU's composed loader checks
(tcg/supercache.c sc7_load_ext(dir_offset, composed) and the feature dispatch).
"""

from __future__ import annotations

import json
from pathlib import Path
import struct
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.analysis.firmware_calls import discover_firmware_calls
from scopt.canon import fnv1a64
from scopt.formats.layouts import SC_CODE_OFFSET
from scopt.formats.sc_records import ScFormatError, ScRecord, write_sc
from scopt.formats.sc_semantic import (ARRAYS_OFFSET, COMPOSED_DIR_OFFSET, EXIT_STATIC, EXT_OFFSET,
                                 KIND_GENERAL, KIND_SPEC, SC7_SC6_REGIONS_MARKER,
                                 SC7_SEMANTIC_MARKER, ExtTable, LinkEntry, ScExtRefused,
                                 SlotEntry, VariantEntry, max_ext_entries, read_semantic_cache,
                                 semantic_snapshot_id)
from scopt.formats.supercache import (LOADER_V7_TRUSTED, LOADER_V7_TRUSTED_SC6,
                                 LOADER_V7_TRUSTED_SC6_MEMBER_SKIP, SC7_COMPOSED_DIR_OFFSET,
                                 SC7_FEATURE_SC6_REGIONS, SC7_MAGIC, build_directory,
                                 canonical_snapshot_id, decode_directory, directory_size,
                                 is_composed_features, loader_requirement, read_region_directory,
                                 region_snapshot_id, write_region_cache, write_composed_cache,
                                 write_semantic_cache)
from scopt.link import B3Plan, NativeLinkerV2, relocate_unit, round_up

from .test_firmware_inline import (_TRUSTED_PREFIX, _b3_grown_unit, _link_trusted, _snapshot,
                                   _trusted_member_unit, _trusted_two_member_region)

_DIR_CHECKSUM = 64  # offset of the checksum in the 80-byte directory header


def _fixture(*, variant_tb: int = 0x1000, member_descriptors: bool = True,
             table_gap: int = 0):
    """One public record R (= C) that B3 grew: its free slot 1 is a SPEC slot
    at a guard stub past the pre-B3 code, linked to a variant C' of R.  One
    trusted two-member region copies R's pre-B3 bytes.  With
    ``member_descriptors`` the members skip slot 1 (flags 2, the sc6 slot set
    of R); without it the region keeps the legacy shared descriptor."""
    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    grown = _b3_grown_unit(unit)
    region = _trusted_two_member_region(snapshot, call, unit)
    linked, _ = _link_trusted(snapshot, region, unit,
                              {"grow": {unit.uid: ()}, "member_slot_skip": member_descriptors},
                              {unit.uid: grown.record})
    assert linked.member_descriptors == member_descriptors
    public = relocate_unit(grown, grown.tb_offset)
    if variant_tb < 0:                     # the last 64-aligned TB slot inside the region
        variant_tb = (linked.end_offset - 1) // 64 * 64
    rec = ScRecord.from_bytes(public.record)
    body = bytes(public.body)
    tb_struct = round_up(snapshot.header.tb_size, 64)
    pmask = (1 << snapshot.header.target_page_bits) - 1
    variant = VariantEntry(rec.tb_offset, variant_tb, variant_tb + tb_struct, rec.tc_size,
                           variant_tb + tb_struct + len(body), fnv1a64(body), 0)
    slot = SlotEntry(rec.tb_offset, 1, KIND_SPEC, EXIT_STATIC, rec.page_addr0 & pmask, 0,
                     rec.tc_size, fnv1a64(body[:rec.tc_size]))
    ext = ExtTable((variant,), (slot,), (LinkEntry(0, 0),), artifact_id8=b"COMPOSED")
    link = SimpleNamespace(units=(public,), firmware_regions=(linked,), ext=ext,
                           firmware_table_offset=round_up(linked.end_offset, 8) + table_gap)
    link.firmware_table_size = directory_size(link.firmware_regions)
    # The same region without B3 (pre-B3 public record, no skip flags).
    plain, _ = _link_trusted(snapshot, region, unit)
    plain_public = relocate_unit(unit, unit.tb_offset)
    plain_link = SimpleNamespace(units=(plain_public,), firmware_regions=(plain,),
                                 firmware_table_offset=round_up(plain.end_offset, 8),
                                 firmware_table_size=directory_size((plain,)))
    return SimpleNamespace(snapshot=snapshot, header=snapshot.header, unit=unit, grown=grown,
                           region=region, linked=linked, public=public, link=link,
                           parts=((public.record, public.body),), bodies=[body], ext=ext,
                           plain_link=plain_link,
                           plain_parts=((plain_public.record, plain_public.body),))


def _write(tmp_path, fx, name="composed.sc", **kw):
    path = tmp_path / name
    write_composed_cache(path, fx.header, fx.parts, fx.link, fx.bodies, fsync=False, **kw)
    return path


def _patch_directory(path, offset, data):
    """Overwrite directory header bytes at 0xFF00 + ``offset`` and re-seal its checksum."""
    with path.open("r+b") as stream:
        stream.seek(SC7_COMPOSED_DIR_OFFSET)
        header = bytearray(stream.read(80))
        header[offset:offset + len(data)] = data
        table_offset, table_size = struct.unpack_from("<QQ", header, 32)
        stream.seek(SC_CODE_OFFSET + table_offset)
        table = stream.read(table_size)
        header[_DIR_CHECKSUM:_DIR_CHECKSUM + 8] = struct.pack(
            "<Q", fnv1a64(table, fnv1a64(bytes(header[:64]))))
        stream.seek(SC7_COMPOSED_DIR_OFFSET)
        stream.write(header)


@pytest.mark.parametrize("member_descriptors", [True, False])
def test_composed_round_trip(tmp_path, member_descriptors):
    fx = _fixture(member_descriptors=member_descriptors)
    path = _write(tmp_path, fx)
    semantic = read_semantic_cache(path)
    assert semantic.problem is None and semantic.is_composed
    assert semantic.sc.header.reserved == (SC7_SEMANTIC_MARKER | SC7_SC6_REGIONS_MARKER |
                                           fx.ext.features())
    assert semantic.ext.variants == fx.ext.variants
    directory = read_region_directory(path)
    assert directory.composed and directory.trusted
    assert loader_requirement(directory) == (LOADER_V7_TRUSTED_SC6_MEMBER_SKIP
                                             if member_descriptors else LOADER_V7_TRUSTED_SC6)
    members = decode_directory(directory)["members"]
    assert [m["flags"] for m in members] == ([2, 2] if member_descriptors else [0, 0])
    with path.open("rb") as stream:
        stream.seek(SC7_COMPOSED_DIR_OFFSET)
        assert stream.read(80) == directory.header
        assert directory.header[:8] == SC7_MAGIC
        stream.seek(EXT_OFFSET)
        assert stream.read(8) == b"QSCX6EXT"
    assert is_composed_features(semantic.sc.header.reserved)
    # The composed identity covers both payloads and differs from either
    # single-payload artifact of the same parts.
    composed_id = canonical_snapshot_id(path)
    assert composed_id == region_snapshot_id(path)
    assert composed_id != semantic_snapshot_id(semantic)
    semantic_only = tmp_path / "semantic.sc"
    write_semantic_cache(semantic_only, fx.header, fx.parts, fx.ext, fx.bodies, fsync=False)
    firmware_only = tmp_path / "firmware.sc"
    write_region_cache(firmware_only, fx.header, fx.plain_parts, fx.plain_link, fsync=False)
    assert loader_requirement(read_region_directory(firmware_only)) == LOADER_V7_TRUSTED
    assert len({composed_id, canonical_snapshot_id(semantic_only),
                canonical_snapshot_id(firmware_only)}) == 3


def test_writer_refuses_a_variant_overlapping_a_region(tmp_path):
    with pytest.raises(ScFormatError, match="overlaps"):
        _write(tmp_path, _fixture(variant_tb=0x4000))      # the region's start
    # A variant whose (unwritten) TB struct area covers the region's tail:
    # every byte reads back intact, so only the arena proof can refuse it,
    # as QEMU's sc7 occupied set does with the sc6 variant spans.
    fx = _fixture(variant_tb=-1, table_gap=0x400)
    with pytest.raises(ScFormatError, match="overlaps"):
        _write(tmp_path, fx)
    path = _write(tmp_path, fx, "unchecked.sc", check=False)
    assert read_semantic_cache(path).problem is None                  # sc6 alone cannot see regions
    with pytest.raises(ScFormatError, match="overlap"):
        read_region_directory(path)


def test_writer_refuses_safe_regions(tmp_path):
    fx = _fixture()
    fx.link.firmware_regions = (replace(fx.linked, trusted=False, member_descriptors=False),)
    with pytest.raises(ScFormatError):
        _write(tmp_path, fx)


def test_reader_refuses_a_safe_composed_directory(tmp_path):
    path = _write(tmp_path, _fixture(member_descriptors=False))
    _patch_directory(path, 8, struct.pack("<I", 2))        # ext_version: safe
    with pytest.raises(ScFormatError, match="trusted firmware directory"):
        read_region_directory(path)


def test_reader_refuses_a_flipped_directory_header_byte(tmp_path):
    path = _write(tmp_path, _fixture())
    with path.open("r+b") as stream:
        stream.seek(SC7_COMPOSED_DIR_OFFSET + 56)          # artifact_id8
        byte = stream.read(1)
        stream.seek(SC7_COMPOSED_DIR_OFFSET + 56)
        stream.write(bytes([byte[0] ^ 1]))
    assert read_semantic_cache(path).problem is None                  # outside the sc6 checksum
    with pytest.raises(ScFormatError, match="checksum mismatch"):
        read_region_directory(path)


def test_arrays_must_end_before_the_composed_directory(tmp_path):
    # 1 variant + 2032 slots + 1 link: fits before 0x10000, not before 0xFF00.
    n_slots = 2032
    assert ARRAYS_OFFSET + 64 + 32 * n_slots + 16 > COMPOSED_DIR_OFFSET
    assert max_ext_entries(1, n_slots, 1)
    assert not max_ext_entries(1, n_slots, 1, limit=COMPOSED_DIR_OFFSET)
    fx = _fixture()
    general = SlotEntry(fx.ext.slots[0].src_tb_offset, 1, KIND_GENERAL, EXIT_STATIC, 0, 0, 1, 0)
    big = replace(fx.ext, slots=fx.ext.slots + (general,) * (n_slots - 1))
    fx.link.ext = big
    with pytest.raises(ScExtRefused) as refused:
        _write(tmp_path, fx)
    assert refused.value.code == "ext_table_too_large"
    # A file carrying such arrays is refused by the composed sc6 reader.
    hdr = fx.header.replace(version=7, reserved=(SC7_SEMANTIC_MARKER | SC7_SC6_REGIONS_MARKER |
                                                 big.features()))
    path = tmp_path / "big.sc"
    write_sc(path, hdr, fx.parts, check=False, fsync=False,
             extra=[(EXT_OFFSET, big.encode())])
    assert read_semantic_cache(path).problem[0] == "ext_table_too_large"


def test_dump_identifies_all_payloads_without_reporting_them_as_padding(tmp_path):
    from click.testing import CliRunner
    from scopt.commands.sc import sc
    import json

    path = _write(tmp_path, _fixture())
    result = CliRunner().invoke(sc, ["dump", str(path), "--check", "--no-records", "--json"])
    assert result.exit_code == 0, result.output
    dump = json.loads(result.output)
    assert dump["snapshot_id"] == canonical_snapshot_id(path)
    assert dump["structural_findings"] == []


def test_bit_30_alone_is_unknown(tmp_path):
    fx = _fixture()
    path = tmp_path / "bit30.sc"
    write_sc(path, fx.header.replace(version=7, reserved=SC7_FEATURE_SC6_REGIONS), fx.parts,
             fsync=False, check=False)
    assert not is_composed_features(SC7_FEATURE_SC6_REGIONS)
    assert not read_semantic_cache(path).is_semantic_v7
    with pytest.raises(ScFormatError, match="feature bits"):
        read_region_directory(path)
    with pytest.raises(ScFormatError, match="feature bits"):
        canonical_snapshot_id(path)


def test_deploy_and_proof_check_both_payloads(tmp_path):
    from scopt.run.deploy import artifact_refusals
    from scopt.run.proof import deployed_state, prove

    fx = _fixture()
    art = tmp_path / "artifact"
    art.mkdir()
    path = _write(art, fx, "cache.sc")
    directory = read_region_directory(path)
    sidecar = {"loader_requirement": LOADER_V7_TRUSTED_SC6_MEMBER_SKIP,
               "x_ext_artifact_id8": fx.ext.artifact_id8.hex(),
               "x_firmware_artifact_id8": directory.artifact_id8.hex()}
    (art / "cache.sc.scopt.json").write_text(json.dumps(sidecar))
    header = read_semantic_cache(path).sc.header
    entry = {"path": str(art), "sc": str(path), "kind": "artifact_dir"}
    reasons, info = artifact_refusals(entry, header)
    assert reasons == [] and info["firmware_artifact_id8"] == directory.artifact_id8.hex()
    # A sidecar that hides the member flags (plain '+sc6') is refused: deploy
    # recomputes the requirement from the file.
    (art / "cache.sc.scopt.json").write_text(json.dumps(
        {**sidecar, "loader_requirement": LOADER_V7_TRUSTED_SC6}))
    assert "loader_requirement_mismatch" in artifact_refusals(entry, header)[0]
    for key, reason in (("x_ext_artifact_id8", "ext_artifact_id_mismatch"),
                        ("x_firmware_artifact_id8", "firmware_artifact_id_mismatch"),
                        ("loader_requirement", "loader_requirement_mismatch")):
        (art / "cache.sc.scopt.json").write_text(json.dumps({**sidecar, key: "00" * 8}))
        assert reason in artifact_refusals(entry, header)[0]
    state = deployed_state(path, None, frontend="none")
    assert state["v7_kind"] == "composed"
    assert state["ext_sha256"] and state["firmware_snapshot_id"] == region_snapshot_id(path)
    assert all(c["ok"] for c in prove(state)["checks"])
    with path.open("r+b") as stream:                       # inside [0xFF00, 0xFF50)
        stream.seek(SC7_COMPOSED_DIR_OFFSET + 56)
        byte = stream.read(1)
        stream.seek(SC7_COMPOSED_DIR_OFFSET + 56)
        stream.write(bytes([byte[0] ^ 1]))
    failed = {c["check"] for c in prove(state)["checks"] if not c["ok"]}
    assert {"ext", "firmware_regions"} <= failed


def test_b3_semantic_edits_still_require_trusted_regions():
    fx = _fixture()
    uid = fx.unit.uid
    safe = SimpleNamespace(**{**vars(fx.region), "trusted": False})
    snapshot = fx.snapshot
    edited = NativeLinkerV2(snapshot, B3Plan(firmware_regions=(safe,), rewrites={uid: ()}),
                            derive_captures=False).link()
    assert not edited.ok
    assert edited.failures[0].reason == "firmware_region_safe_composition"


@pytest.mark.parametrize("trusted", [False, True])
def test_layout_preserves_private_descriptors_and_recovery_contract(tmp_path, trusted):
    from scopt.decode.search import SearchRow, encode_search
    from scopt.model.snapshot import WorkingSet
    from scopt.model.unit import Fixup, RecoveryPoint
    from scopt.validate import validate_firmware_v7, write_b3_files
    from tests.synth.fake_snapshot import FakeSnapshot

    fx = _fixture()
    unit = fx.unit
    descriptor_site = unit.sites[1]
    descriptor = fx.header.code_address + unit.tb_offset
    code = bytearray(unit.code)
    struct.pack_into("<Q", code, descriptor_site.field_off, descriptor)
    fixup = Fixup(descriptor_site.field_off, 8, "abs_plus_delta", int(descriptor_site.kind),
                  "own_struct", descriptor)
    words = (unit.vaddrs[0], 0, 0)
    search = encode_search([SearchRow(words, len(code))])
    end = unit.tc_offset + len(code) + len(search)
    record = ScRecord.from_bytes(unit.record).replace(end_offset=end)
    unit = replace(unit, code=bytes(code), search=search, end_offset=end,
                   record=record.to_bytes(fix_checksum=True),
                   recovery=(RecoveryPoint("r0", 20, 0, words, "raddr"),),
                   reloc=replace(unit.reloc, fixups=(fixup,)))
    snap = FakeSnapshot((unit,), fx.snapshot.snapshot_id, fx.header)
    snap.run_info = SimpleNamespace(flags=0, tb_ret_off=0, code_address=fx.header.code_address)
    region = SimpleNamespace(**dict(vars(fx.region), trusted=trusted,
                                    trusted_entry_offset=15 if trusted else 0))
    plan = B3Plan(firmware_regions=(region,))
    placement = WorkingSet(snap, placements={fx.unit.uid: 0x4000})
    link = NativeLinkerV2(snap, plan, placement=placement, derive_captures=False).link()
    assert link.ok, link.failures
    write_b3_files(tmp_path, snap, link)
    path = tmp_path / "cache.sc"
    directory = read_region_directory(path)
    assert directory.trusted == trusted
    members = decode_directory(directory)["members"]
    assert len(members) == 2
    with path.open("rb") as stream:
        public_tc = 0x4000 + unit.tc_offset - unit.tb_offset
        stream.seek(SC_CODE_OFFSET + public_tc + descriptor_site.field_off)
        assert struct.unpack("<Q", stream.read(8))[0] == fx.header.code_address + 0x4000
        for member in members:
            assert member["source_tb_offset"] == 0x4000
            stream.seek(SC_CODE_OFFSET + member["tc_offset"] + descriptor_site.field_off)
            descriptor, = struct.unpack("<Q", stream.read(8))
            assert descriptor == fx.header.code_address + member["tb_offset"]
            if not trusted:
                stream.seek(SC_CODE_OFFSET + member["search_offset"])
                assert stream.read(len(search)) == search
    audit, imported = validate_firmware_v7(
        snap, link, tmp_path, sidecar={"loader_requirement": loader_requirement(directory)},
        runtime={"qemu_sha256": _NEW_QEMU}, v7_exes=(_NEW_QEMU,), trusted_v7_exes=(_NEW_QEMU,))
    assert audit.accepted, audit.fails()
    assert imported.snapshot.firmware_regions[0]["source_tb_offset"] == 0x4000


def test_b3_grown_regions_are_priced_unchainable(monkeypatch):
    from scopt.analysis.firmware_profit import ProfitModel, resolve_residual_goto_tb
    from scopt.manager_b3 import B3State

    fx = _fixture()
    assert resolve_residual_goto_tb("auto", fx.region) == "chained"
    assert resolve_residual_goto_tb("auto", fx.region, {fx.unit.uid}) == "unchained"
    # An explicit mode overrides the conservative rule.
    assert resolve_residual_goto_tb("chained", fx.region, {fx.unit.uid}) == "chained"
    # Rewrites and variants keep the record: only grown units block pricing.
    state = B3State(rewrites={"r": ()},
                    variants={"v": SimpleNamespace(src_uid="c")})
    assert state.edited_uids() == {"r", "c"} and state.grown_uids() == frozenset()
    rewrite_only = SimpleNamespace(**{**vars(fx.region)})
    assert resolve_residual_goto_tb("auto", rewrite_only, state.grown_uids()) == "chained"
    # Pricing never reads the member-skip capability.
    assert "member_slot_skip" not in {f for f in ProfitModel.__dataclass_fields__}
    monkeypatch.setenv("SCOPT_V7_MEMBER_SKIP_EXES", "ab" * 32)
    assert resolve_residual_goto_tb("auto", fx.region, {fx.unit.uid}) == "unchained"


@pytest.mark.parametrize("base", ["sealed-pgo-7", "sealed-whopr-7", "sealed-whopr-7-bounded",
                                  "sealed-firmlooper", "sealed-firmlooper-v2"])
def test_sealed_b3_pipelines_compose_b3_before_firmware_inline(base):
    from scopt.manager import load_pipeline
    from scopt.passes.base import EditKind

    original = load_pipeline(base)
    composed = load_pipeline(base + "-b3")
    b3 = [p.name for p in load_pipeline("b3").passes]
    prefix = [p for p in original.passes if p.name != "firmware-inline"]
    assert [p.name for p in composed.passes] == [p.name for p in prefix] + b3 + \
        ["firmware-inline"]
    assert composed.passes[-1].params == original.passes[-1].params
    assert [p.params for p in composed.passes[:len(prefix)]] == [p.params for p in prefix]
    assert composed.allowed_capabilities == original.allowed_capabilities | {
        EditKind.CLONE, EditKind.REDIRECT_EXIT, EditKind.ADD_ENTRY, EditKind.ATTACH_FACT,
        EditKind.PURE_REWRITE}


# -- member skip flags (B3-registered slots of pre-B3 member copies) --------

_NEW_QEMU = "ab" * 32


def _grown_member_plan(member_slot_skip: bool):
    """A trusted-entry member source M (one lookup tail, both goto_tb slots
    free) that B3 grows with a SPEC slot 0 (linked to a variant of T) and a
    GENERAL slot 1, and one trusted two-member region of M's copies."""
    from scopt.guard import GuardTarget
    from scopt.link import GuardedExitSpec, VariantSpec
    from scopt.formats.enums import Encoding, SiteKind
    from scopt.model.firmware_region import FirmwareRegionMember
    from scopt.model.unit import HelperBinding, PoolEntry, Site
    from tests.synth.fake_snapshot import FakeSnapshot, fake_snapshot

    base = fake_snapshot(3)
    member, target, other = base.units()
    lookup = 0x2000
    tail = len(_TRUSTED_PREFIX)
    pool = tail + 11
    code = (_TRUSTED_PREFIX + bytes.fromhex("488bfdff15") + (2).to_bytes(4, "little") +
            bytes.fromhex("ffe0") + lookup.to_bytes(8, "little"))
    rec = ScRecord.from_bytes(member.record).replace(
        tc_size=len(code), end_offset=member.tc_offset + len(code) + len(member.search),
        jmp_insn_offset=(0xFFFF, 0xFFFF), jmp_reset_offset=(0xFFFF, 0xFFFF))
    sites = (Site(tail + 5, tail + 3, 4, Encoding.POOL_INDIRECT, SiteKind.HELPER_CALL,
                  1, lookup, 0, 2),
             Site(tail + 9, tail + 9, 0, Encoding.NONE, SiteKind.GOTO_PTR, 0, 0, 0, 0))
    member = replace(member, record=rec.to_bytes(fix_checksum=True), code=code,
                     tc_size=len(code), end_offset=rec.end_offset, insn0_start=15,
                     sites=sites, pool_start=pool,
                     pool=(PoolEntry(tail + 5, pool, 0, 1, 0, lookup),),
                     bindings=replace(member.bindings, helpers=(HelperBinding(
                         1, "lookup_tb_ptr", "qemu", "helper_lookup_tb_ptr", 0, lookup),)))
    snap = FakeSnapshot((member, target, other), base.snapshot_id, base.header)
    snap.run_info = SimpleNamespace(code_address=base.header.code_address, tb_ret_off=0,
                                    flags=0)
    spec = GuardedExitSpec(tail, "return", (GuardTarget(target.vaddrs[0], 0, "SPEC"),
                                            GuardTarget(other.vaddrs[0], 1, "GENERAL")),
                           target.key.flags, target.key.cs_base)
    region = SimpleNamespace(
        members=(FirmwareRegionMember(member.uid, member.content_hash, member.key, (), "caller"),
                 FirmwareRegionMember(member.uid, member.content_hash, member.key, ("c",),
                                      "callee")),
        trusted=True, edges=(), region_id="sha256:" + "e" * 64,
        callsite=SimpleNamespace(callsite_id="sha256:" + "c" * 64), entry_member=0,
        source_uids=(member.uid,), trusted_entry_offset=15)
    plan = B3Plan(grow={member.uid: (spec,)},
                  variants=(VariantSpec(target.uid, member.uid, 0, key="v0"),),
                  firmware_regions=(region,), member_slot_skip=member_slot_skip)
    return snap, member, plan


def _guard_layout():
    """An EnvLayout stand-in with the default guard displacements."""
    from scopt.guard import GuardOffsets

    defaults = GuardOffsets()
    names = {name: getattr(defaults, key) for key, name in GuardOffsets.FIELDS.items()}
    return SimpleNamespace(offset=names.__getitem__, envlayout_id=None)


def test_moved_region_source_and_variant_round_trip(tmp_path):
    from scopt.formats.region_v1 import unit_exits
    from scopt.model.snapshot import WorkingSet
    from scopt.validate import write_b3_files

    snap, source, plan = _grown_member_plan(member_slot_skip=True)
    target = snap.unit(plan.variants[0].src_uid)
    placement = WorkingSet(snap, placements={source.uid: 0x4000, target.uid: 0x8000})
    exit_, = unit_exits(source)
    region, = plan.firmware_regions
    edge = SimpleNamespace(source_member=0, target_member=1, exit_id=exit_.id, kind="call")
    region = SimpleNamespace(**{**vars(region), "edges": (edge,)})
    plan = replace(plan, firmware_regions=(region,))
    link = NativeLinkerV2(snap, plan, placement=placement, derive_captures=False).link()
    assert link.ok, link.failures
    write_b3_files(tmp_path, snap, link)
    path = tmp_path / "cache.sc"
    semantic = read_semantic_cache(path)
    assert semantic.problem is None
    assert {s.src_tb_offset for s in semantic.ext.slots} == {0x4000}
    variant, = semantic.ext.variants
    assert variant.general_tb_offset == 0x8000
    rows = decode_directory(read_region_directory(path))
    emitted, = rows["regions"]
    assert emitted["start_offset"] >= variant.end_offset
    assert emitted["source_tb_offset"] == 0x4000
    caller, callee = rows["members"]
    assert caller["source_tb_offset"] == callee["source_tb_offset"] == 0x4000
    assert caller["flags"] == callee["flags"] == 3
    internal, = rows["edges"]
    field = caller["tc_offset"] + internal["field_offset"]
    with path.open("rb") as stream:
        stream.seek(SC_CODE_OFFSET + field)
        displacement, = struct.unpack("<i", stream.read(4))
    assert field + 4 + displacement == callee["tc_offset"] + region.trusted_entry_offset


@pytest.mark.parametrize("member_slot_skip", [True, False])
def test_composed_member_skip_flags_equal_sc6_slots(tmp_path, member_slot_skip):
    from scopt.guard import GuardOffsets
    from scopt.validate import package_b3

    snap, member, plan = _grown_member_plan(member_slot_skip)
    link = NativeLinkerV2(snap, plan, offsets=GuardOffsets(), derive_captures=False).link()
    assert link.ok, link.failures
    out_tb = link.by_uid()[member.uid].new_tb_offset
    sc6_mask = 0
    for slot in link.ext.slots:
        if slot.src_tb_offset == out_tb:
            sc6_mask |= 1 << slot.slot_n
    assert sc6_mask == 3 and {s.kind for s in link.ext.slots} == {KIND_SPEC, KIND_GENERAL}
    assert len(link.variants) == 1
    region, = link.firmware_regions
    if member_slot_skip:
        assert region.member_descriptors and region.member_descriptor_refusal is None
        assert [m.skip_slots for m in region.members] == [sc6_mask, sc6_mask]
        assert (region.skipped_b3_slots, region.chainable_residual_slots) == (4, 0)
    else:
        assert region.member_descriptor_refusal == "b3_registered_slot"
        assert [m.skip_slots for m in region.members] == [0, 0]
    # write_composed_cache reads the file back with both readers (sc6 equality).
    from scopt.validate import write_b3_files

    write_b3_files(tmp_path / "art", snap, link)
    directory = read_region_directory(tmp_path / "art" / "cache.sc")
    decoded = decode_directory(directory)
    want = LOADER_V7_TRUSTED_SC6_MEMBER_SKIP if member_slot_skip else LOADER_V7_TRUSTED_SC6
    assert loader_requirement(directory) == want
    assert decoded["regions"][0]["flags"] == (7 if member_slot_skip else 3)
    assert [m["flags"] for m in decoded["members"]] == ([3, 3] if member_slot_skip else [0, 0])
    assert all(m["source_tb_offset"] == out_tb for m in decoded["members"])
    # package_b3 publishes the same requirement and per-region outcomes.
    runtime = {"qemu_sha256": "sha256:" + _NEW_QEMU}
    res = package_b3(snap, plan, tmp_path / "store", candidate_id="sha256:" + "51" * 32,
                     runtime=runtime, member_skip_exes=(_NEW_QEMU,), layout=_guard_layout())
    assert res.status == "published", res.reason
    side = json.loads((Path(res.artifact_dir) / "cache.sc.scopt.json").read_text())
    assert side["loader_requirement"] == want
    from scopt.formats.artifact import sidecar_errors

    # The schema enum has both '+sc6' values (the fake snapshot has no
    # bundle or RuntimeIdentity id, which the schema also requires).
    assert not [e for e in sidecar_errors(side) if e.startswith("loader_requirement")]
    assert side["x_b3"]["member_slot_skip"] is member_slot_skip
    row, = side["x_b3"]["firmware_region_links"]
    assert row == {"region_id": region.region_id, "member_descriptors": member_slot_skip,
                   "member_descriptor_refusal": None if member_slot_skip
                   else "b3_registered_slot",
                   "skipped_b3_slots": 4 if member_slot_skip else 0,
                   "chainable_residual_slots": 0}


def _patch_member_flags(path, flags_by_member, members):
    """Rewrite member flags in a composed directory, refreshing the checksum
    and the artifact identity (``members``: the linked member payloads)."""
    from scopt.formats.supercache import _HEADER, _MEMBER, _REGION, _artifact_id8

    with path.open("r+b") as stream:
        stream.seek(SC7_COMPOSED_DIR_OFFSET)
        fields = list(_HEADER.unpack(stream.read(_HEADER.size)))
        off, size = fields[7], fields[8]
        stream.seek(SC_CODE_OFFSET + off)
        table = bytearray(stream.read(size))
        for i, flags in flags_by_member.items():
            struct.pack_into("<I", table, _REGION.size * fields[3] + _MEMBER.size * i +
                             _MEMBER.size - 4, flags)
        stream.seek(SC_CODE_OFFSET + off)
        stream.write(table)
        fields[10] = _artifact_id8(bytes(table), [(m.descriptor, m.code, m.search)
                                                  for m in members], fields[1])
        fields[11] = 0
        prefix = _HEADER.pack(*fields)
        fields[11] = fnv1a64(bytes(table), fnv1a64(prefix[:64]))
        stream.seek(SC7_COMPOSED_DIR_OFFSET)
        stream.write(_HEADER.pack(*fields))


@pytest.mark.parametrize(("member_descriptors", "flags", "needle"), [
    (True, {0: 0}, "skip flags 0x0 != B3 slot mask 0x2"),      # a required bit cleared
    (True, {1: 3}, "skip flags 0x3 != B3 slot mask 0x2"),      # an original slot skipped
    (True, {0: 4}, "invalid region or flags"),                 # bits 2-31
    (False, {0: 2}, "skip flags 0x2 != B3 slot mask 0x0"),     # flags on a legacy region
])
def test_composed_reader_rejects_skip_mutants(tmp_path, member_descriptors, flags, needle):
    fx = _fixture(member_descriptors=member_descriptors)
    path = _write(tmp_path, fx)
    decoded = decode_directory(read_region_directory(path))
    assert [m["flags"] for m in decoded["members"]] == ([2, 2] if member_descriptors
                                                         else [0, 0])
    assert decoded["regions"][0]["flags"] == (7 if member_descriptors else 3)
    _patch_member_flags(path, flags, fx.linked.members)
    with pytest.raises(ScFormatError, match=needle):
        read_region_directory(path)
    # The unmutated table with a refreshed identity still reads.
    _patch_member_flags(path, {i: m["flags"] for i, m in enumerate(decoded["members"])},
                        fx.linked.members)
    read_region_directory(path)


def test_v7_writer_rejects_bad_member_skip_flags():
    fx = _fixture()
    source_tb = {fx.unit.uid: fx.unit.tb_offset}

    def directory(region):
        return build_directory(SimpleNamespace(
            units=(fx.public,), firmware_regions=(region,),
            firmware_table_offset=fx.link.firmware_table_offset,
            firmware_table_size=directory_size((region,))), source_tb)

    directory(fx.linked)
    bad_bit = replace(fx.linked, members=tuple(replace(m, skip_slots=4)
                                               for m in fx.linked.members))
    with pytest.raises(ScFormatError, match="invalid member flags 0x4"):
        directory(bad_bit)
    legacy = _fixture(member_descriptors=False).linked
    flagged = replace(legacy, members=tuple(replace(m, skip_slots=2) for m in legacy.members))
    with pytest.raises(ScFormatError, match="invalid member flags 0x2"):
        directory(flagged)
