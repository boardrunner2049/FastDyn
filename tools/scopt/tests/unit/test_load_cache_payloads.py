"""The load command checks each payload named by the SuperCache header."""
import json

import pytest
from click.testing import CliRunner

from scopt.commands.optimize import load_cmd
from scopt.formats import sc_semantic, supercache
from scopt.formats.layouts import SC_HEADER
from scopt.formats.sc_records import ScHeader
from .test_composed_v7 import _fixture, _write


def load(directory, *, trusted):
    result = CliRunner().invoke(load_cmd, [str(directory), "--json", *(["--trusted"] if trusted else [])])
    assert result.exit_code in (0, 2), result.exception
    return result.exit_code, json.loads(result.output)


@pytest.mark.parametrize("shape", ["public", "semantic", "regions", "composed"])
def test_load_checks_all_payloads_and_reports_final_extent(tmp_path, shape, monkeypatch):
    fx = _fixture()
    path = tmp_path / "cache.sc"
    if shape == "public":
        supercache.write_public_cache(path, fx.header, fx.parts)
        end = fx.grown.end_offset
    elif shape == "semantic":
        supercache.write_semantic_cache(path, fx.header, fx.parts, fx.ext, fx.bodies)
        end = fx.ext.variants[0].end_offset
    elif shape == "regions":
        supercache.write_region_cache(path, fx.header, fx.plain_parts, fx.plain_link)
        end = fx.plain_link.firmware_table_offset + fx.plain_link.firmware_table_size
    else:
        _write(tmp_path, fx, "cache.sc")
        end = max(fx.ext.variants[0].end_offset, fx.link.firmware_table_offset + fx.link.firmware_table_size)
    code, doc = load(tmp_path, trusted=False)
    assert code == 0, doc["problems"]
    assert doc["max_end"] == end

    def unexpected_hash(*args):
        pytest.fail("trusted load hashed extension contents")
    monkeypatch.setattr(supercache, "_artifact_id8", unexpected_hash)
    monkeypatch.setattr(supercache, "fnv1a64", unexpected_hash)
    monkeypatch.setattr(sc_semantic, "fnv1a64", unexpected_hash)
    code, doc = load(tmp_path, trusted=True)
    assert code == 0, doc["problems"]
    assert doc["max_end"] == end


@pytest.mark.parametrize(("shape", "problem"), [("unknown", "feature bits"), ("semantic", "ext magic"),
                                                 ("regions", "directory header"), ("composed", "directory header")])
@pytest.mark.parametrize("trusted", [False, True])
def test_load_refuses_malformed_payloads(tmp_path, shape, problem, trusted):
    fx = _fixture()
    path = tmp_path / "cache.sc"
    if shape == "composed":
        _write(tmp_path, fx, "cache.sc")
        with path.open("r+b") as stream:
            stream.seek(supercache.SC7_COMPOSED_DIR_OFFSET)
            stream.write(b"BADMAGIC")
    else:
        supercache.write_public_cache(path, fx.header, fx.plain_parts)
        flags = {"unknown": 1 << 22, "semantic": sc_semantic.SC7_SEMANTIC_MARKER | sc_semantic.FEAT_EXT_TABLE,
                 "regions": supercache.SC7_FEATURE_FIRMWARE_REGIONS}[shape]
        with path.open("r+b") as stream:
            h = ScHeader.from_bytes(stream.read(SC_HEADER.size))
            stream.seek(0)
            stream.write(h.replace(reserved=flags).with_checksum().to_bytes())
    code, doc = load(tmp_path, trusted=trusted)
    assert code == 2 and any(problem in p for p in doc["problems"]), doc
