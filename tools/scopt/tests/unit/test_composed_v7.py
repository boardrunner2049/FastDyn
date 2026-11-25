"""Composed canonical v7: a B3 semantic extension and trusted firmware regions.

The semantic payload keeps 0x100 (arrays ending at or before 0xFF00) and the
v7 directory header moves to 0xFF00; ``reserved = 1<<31 | 1<<30 | sc6 bits``.
These are the scopt-side mirrors of QEMU's composed loader checks
(tcg/supercache.c sc7_load_ext(dir_offset, composed) and the feature dispatch).
"""

from __future__ import annotations

import json
import struct
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.analysis.firmware_calls import discover_firmware_calls
from scopt.canon import fnv1a64
from scopt.formats.layouts import SC_CODE_OFFSET
from scopt.formats.sc_v5 import ScFormatError, ScRecord, write_sc
from scopt.formats.sc_v6 import (ARRAYS_OFFSET, COMPOSED_DIR_OFFSET, EXIT_STATIC, EXT_OFFSET,
                                 KIND_GENERAL, KIND_SPEC, SC7_SC6_REGIONS_MARKER,
                                 SC7_SEMANTIC_MARKER, ExtTable, LinkEntry, ScExtRefused,
                                 SlotEntry, VariantEntry, max_ext_entries, read_sc6,
                                 sc6_snapshot_id)
from scopt.formats.sc_v7 import (LOADER_V7_TRUSTED, LOADER_V7_TRUSTED_SC6,
                                 SC7_COMPOSED_DIR_OFFSET, SC7_FEATURE_SC6_REGIONS, SC7_MAGIC,
                                 canonical_snapshot_id, directory_size, is_composed_features,
                                 loader_requirement, read_sc7, sc7_snapshot_id, write_sc7,
                                 write_sc7_composed, write_sc7_semantic)
from scopt.link import B3Plan, NativeLinkerV2, relocate_unit, round_up

from .test_firmware_inline import (_link_trusted, _snapshot, _trusted_member_unit,
                                   _trusted_two_member_region)

_DIR_CHECKSUM = 64  # offset of the checksum in the 80-byte directory header


def _fixture(*, variant_tb: int = 0x1000, member_descriptors: bool = True,
             table_gap: int = 0):
    """One public record R (= C), one SPEC slot of R linked to a variant C' of
    R, and one trusted two-member region of R's copies."""
    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    region = _trusted_two_member_region(snapshot, call, unit)
    linked, _ = _link_trusted(snapshot, region, unit,
                              None if member_descriptors else {"rewrites": {unit.uid: ()}})
    public = relocate_unit(unit, unit.tb_offset)
    if variant_tb < 0:                     # the last 64-aligned TB slot inside the region
        variant_tb = (linked.end_offset - 1) // 64 * 64
    rec = ScRecord.from_bytes(public.record)
    body = bytes(public.body)
    tb_struct = round_up(snapshot.header.tb_size, 64)
    pmask = (1 << snapshot.header.target_page_bits) - 1
    variant = VariantEntry(rec.tb_offset, variant_tb, variant_tb + tb_struct, rec.tc_size,
                           variant_tb + tb_struct + len(body), fnv1a64(body), 0)
    slot = SlotEntry(rec.tb_offset, 0, KIND_SPEC, EXIT_STATIC, rec.page_addr0 & pmask, 0,
                     rec.tc_size, fnv1a64(body[:rec.tc_size]))
    ext = ExtTable((variant,), (slot,), (LinkEntry(0, 0),), artifact_id8=b"COMPOSED")
    link = SimpleNamespace(units=(public,), firmware_regions=(linked,), ext=ext,
                           firmware_table_offset=round_up(linked.end_offset, 8) + table_gap)
    link.firmware_table_size = directory_size(link.firmware_regions)
    return SimpleNamespace(snapshot=snapshot, header=snapshot.header, unit=unit,
                           region=region, linked=linked, public=public, link=link,
                           parts=((public.record, public.body),), bodies=[body], ext=ext)


def _write(tmp_path, fx, name="composed.sc", **kw):
    path = tmp_path / name
    write_sc7_composed(path, fx.header, fx.parts, fx.link, fx.bodies, fsync=False, **kw)
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
    semantic = read_sc6(path)
    assert semantic.problem is None and semantic.is_composed
    assert semantic.sc.header.reserved == (SC7_SEMANTIC_MARKER | SC7_SC6_REGIONS_MARKER |
                                           fx.ext.features())
    assert semantic.ext.variants == fx.ext.variants
    directory = read_sc7(path)
    assert directory.composed and directory.trusted
    assert loader_requirement(directory) == LOADER_V7_TRUSTED_SC6
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
    assert composed_id == sc7_snapshot_id(path)
    assert composed_id != sc6_snapshot_id(semantic)
    semantic_only = tmp_path / "semantic.sc"
    write_sc7_semantic(semantic_only, fx.header, fx.parts, fx.ext, fx.bodies, fsync=False)
    firmware_only = tmp_path / "firmware.sc"
    write_sc7(firmware_only, fx.header, fx.parts, fx.link, fsync=False)
    assert loader_requirement(read_sc7(firmware_only)) == LOADER_V7_TRUSTED
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
    assert read_sc6(path).problem is None                  # sc6 alone cannot see regions
    with pytest.raises(ScFormatError, match="overlap"):
        read_sc7(path)


def test_writer_refuses_safe_regions(tmp_path):
    fx = _fixture()
    fx.link.firmware_regions = (replace(fx.linked, trusted=False, member_descriptors=False),)
    with pytest.raises(ScFormatError):
        _write(tmp_path, fx)


def test_reader_refuses_a_safe_composed_directory(tmp_path):
    path = _write(tmp_path, _fixture(member_descriptors=False))
    _patch_directory(path, 8, struct.pack("<I", 2))        # ext_version: safe
    with pytest.raises(ScFormatError, match="trusted firmware directory"):
        read_sc7(path)


def test_reader_refuses_a_flipped_directory_header_byte(tmp_path):
    path = _write(tmp_path, _fixture())
    with path.open("r+b") as stream:
        stream.seek(SC7_COMPOSED_DIR_OFFSET + 56)          # artifact_id8
        byte = stream.read(1)
        stream.seek(SC7_COMPOSED_DIR_OFFSET + 56)
        stream.write(bytes([byte[0] ^ 1]))
    assert read_sc6(path).problem is None                  # outside the sc6 checksum
    with pytest.raises(ScFormatError, match="checksum mismatch"):
        read_sc7(path)


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
    write_sc(path, hdr, fx.parts, check=False, fsync=False, versions=(7,),
             extra=[(EXT_OFFSET, big.encode())])
    assert read_sc6(path).problem[0] == "ext_table_too_large"


def test_bit_30_alone_is_unknown(tmp_path):
    fx = _fixture()
    path = tmp_path / "bit30.sc"
    write_sc(path, fx.header.replace(version=7, reserved=SC7_FEATURE_SC6_REGIONS), fx.parts,
             fsync=False, versions=(7,))
    assert not is_composed_features(SC7_FEATURE_SC6_REGIONS)
    assert not read_sc6(path).is_semantic_v7
    with pytest.raises(ScFormatError, match="feature bits"):
        read_sc7(path)
    with pytest.raises(ScFormatError, match="feature bits"):
        canonical_snapshot_id(path)


def test_deploy_and_proof_check_both_payloads(tmp_path):
    from scopt.run.deploy import artifact_refusals
    from scopt.run.proof import deployed_state, prove

    fx = _fixture()
    art = tmp_path / "artifact"
    art.mkdir()
    path = _write(art, fx, "cache.sc")
    directory = read_sc7(path)
    sidecar = {"loader_requirement": LOADER_V7_TRUSTED_SC6,
               "x_ext_artifact_id8": fx.ext.artifact_id8.hex(),
               "x_firmware_artifact_id8": directory.artifact_id8.hex()}
    (art / "cache.sc.scopt.json").write_text(json.dumps(sidecar))
    header = read_sc6(path).sc.header
    entry = {"path": str(art), "sc": str(path), "kind": "artifact_dir"}
    reasons, info = artifact_refusals(entry, header)
    assert reasons == [] and info["firmware_artifact_id8"] == directory.artifact_id8.hex()
    for key, reason in (("x_ext_artifact_id8", "ext_artifact_id_mismatch"),
                        ("x_firmware_artifact_id8", "firmware_artifact_id_mismatch"),
                        ("loader_requirement", "loader_requirement_mismatch")):
        (art / "cache.sc.scopt.json").write_text(json.dumps({**sidecar, key: "00" * 8}))
        assert reason in artifact_refusals(entry, header)[0]
    state = deployed_state(path, None, frontend="none")
    assert state["v7_kind"] == "composed"
    assert state["ext_sha256"] and state["firmware_snapshot_id"] == sc7_snapshot_id(path)
    assert all(c["ok"] for c in prove(state)["checks"])
    with path.open("r+b") as stream:                       # inside [0xFF00, 0xFF50)
        stream.seek(SC7_COMPOSED_DIR_OFFSET + 56)
        byte = stream.read(1)
        stream.seek(SC7_COMPOSED_DIR_OFFSET + 56)
        stream.write(bytes([byte[0] ^ 1]))
    failed = {c["check"] for c in prove(state)["checks"] if not c["ok"]}
    assert {"ext", "firmware_regions"} <= failed


def test_linker_composes_only_trusted_regions_without_placement():
    fx = _fixture()
    uid = fx.unit.uid
    safe = SimpleNamespace(**{**vars(fx.region), "trusted": False})
    snapshot = fx.snapshot
    edited = NativeLinkerV2(snapshot, B3Plan(firmware_regions=(safe,), rewrites={uid: ()}),
                            derive_captures=False).link()
    assert not edited.ok
    assert edited.failures[0].reason == "firmware_region_safe_composition"
    placement = SimpleNamespace(base=snapshot)
    placed = NativeLinkerV2(snapshot, B3Plan(firmware_regions=(fx.region,)),
                            placement=placement, derive_captures=False).link()
    assert not placed.ok and placed.failures[0].reason == "firmware_region_terminal_stage"


def test_b3_edited_regions_are_priced_unchainable():
    from scopt.analysis.firmware_profit import resolve_residual_goto_tb
    from scopt.manager_b3 import B3State

    fx = _fixture()
    assert resolve_residual_goto_tb("auto", fx.region) == "chained"
    assert resolve_residual_goto_tb("auto", fx.region, {fx.unit.uid}) == "unchained"
    state = B3State(rewrites={"r": ()},
                    variants={"v": SimpleNamespace(src_uid="c")})
    assert state.edited_uids() == {"r", "c"}


@pytest.mark.parametrize("base", ["sealed-pgo-7", "sealed-whopr-7", "sealed-firmlooper",
                                  "sealed-firmlooper-v2"])
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
