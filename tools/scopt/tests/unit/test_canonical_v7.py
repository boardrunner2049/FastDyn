"""Canonical top-level v7 publication formats."""

from scopt.formats.sc_v5 import ScHeader, read_sc
from scopt.formats.sc_v6 import ExtTable, read_sc6, sc6_snapshot_id
from scopt.formats.sc_v7 import (canonical_snapshot_id, write_sc7_public,
                                 write_sc7_semantic)
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
    write_sc7_public(path, header, parts, fsync=False)
    parsed = read_sc(path, versions=(7,))
    assert (parsed.header.version, parsed.header.reserved) == (7, 0)
    assert canonical_snapshot_id(path) == parsed.snapshot_id()


def test_semantic_extension_is_marked_v7_and_round_trips(tmp_path):
    header, parts = _fixture()
    path = tmp_path / "semantic.sc"
    ext = ExtTable(artifact_id8=b"canon-v7")
    write_sc7_semantic(path, header, parts, ext, fsync=False)
    parsed = read_sc6(path)
    assert parsed.is_semantic_v7 and parsed.problem is None
    assert parsed.ext is not None
    assert (parsed.ext.variants, parsed.ext.slots, parsed.ext.links,
            parsed.ext.artifact_id8) == ((), (), (), b"canon-v7")
    assert canonical_snapshot_id(path) == sc6_snapshot_id(parsed)
