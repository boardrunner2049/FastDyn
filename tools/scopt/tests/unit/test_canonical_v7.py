"""Canonical top-level v7 publication formats."""

from scopt.formats.sc_records import ScHeader, read_sc
from scopt.formats.sc_semantic import ExtTable, read_semantic_cache, semantic_snapshot_id
from scopt.formats.supercache import (canonical_snapshot_id, write_public_cache,
                                 write_semantic_cache)
from tests.synth import builders as B


def _fixture():
    units = B.simple_units(2)
    fields = B.sc_header_fields(units, {})
    fields.pop("checksum")
    header = ScHeader(**fields, checksum=0).with_checksum()
    resolved = [B.resolve_unit(unit) for unit in units]
    parts = [(unit.record_bytes, unit.arena_bytes) for unit in resolved]
    return header, parts


def test_public_only_is_v7_without_features(tmp_path):
    header, parts = _fixture()
    path = tmp_path / "public.sc"
    write_public_cache(path, header, parts, fsync=False)
    parsed = read_sc(path)
    assert (parsed.header.version, parsed.header.reserved) == (7, 0)
    assert canonical_snapshot_id(path) == parsed.snapshot_id()


def test_semantic_extension_is_marked_v7_and_round_trips(tmp_path):
    header, parts = _fixture()
    path = tmp_path / "semantic.sc"
    ext = ExtTable(artifact_id8=b"canon-v7")
    write_semantic_cache(path, header, parts, ext, fsync=False)
    parsed = read_semantic_cache(path)
    assert parsed.is_semantic_v7 and parsed.problem is None
    assert parsed.ext is not None
    assert (parsed.ext.variants, parsed.ext.slots, parsed.ext.links,
            parsed.ext.artifact_id8) == ((), (), (), b"canon-v7")
    assert canonical_snapshot_id(path) == semantic_snapshot_id(parsed)


def test_identity_copy_preserves_extended_cache(tmp_path):
    from scopt.formats.sc_records import identity_copy
    from scopt.formats.profile_v1 import SnapshotUnits

    header, parts = _fixture()
    source, copy = tmp_path / "semantic.sc", tmp_path / "copy.sc"
    write_semantic_cache(source, header, parts, ExtTable(artifact_id8=b"identity"), fsync=False)
    result = identity_copy(source, copy)
    assert result.status == "identity"
    assert result.in_snapshot_id == canonical_snapshot_id(copy)
    assert SnapshotUnits.from_sc(read_sc(copy)).snapshot_id == result.in_snapshot_id
    assert source.read_bytes() == copy.read_bytes()


def test_retired_headers_refused_by_all_identity_paths(tmp_path):
    import pytest
    from scopt.formats.sc_records import HEADER_SIZE, ScFormatError, identity_copy

    header, parts = _fixture()
    path = tmp_path / "old.sc"
    good = write_public_cache(path, header, parts, fsync=False)
    for version in (5, 6):
        with path.open("r+b") as stream:
            assert stream.write(good.replace(version=version).with_checksum().to_bytes()) == HEADER_SIZE
        assert read_sc(path).header_problems == ("version",)
        with pytest.raises(ScFormatError):
            canonical_snapshot_id(path)
        with pytest.raises(ScFormatError):
            identity_copy(path, tmp_path / "copy.sc")


def test_independent_semantic_parser_accepts_public_cache(tmp_path):
    from scopt.formats.sc_semantic import sc6parse_compare

    header, parts = _fixture()
    path = tmp_path / "public.sc"
    write_public_cache(path, header, parts, fsync=False)
    agree, reason, result = sc6parse_compare(path, read_semantic_cache(path))
    assert agree, reason
    assert result["verdict"] == "v7_no_semantic_table"
