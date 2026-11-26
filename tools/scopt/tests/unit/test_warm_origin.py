import json
import shutil
from pathlib import Path

from click.testing import CliRunner

from scopt.commands.optimize import optimize_cmd
from scopt.formats.sc_records import sha256_sparse
from scopt.importer.import_cache import import_snapshot
from scopt.importer.warm_origin import ordinary_warm
from tests.synth import rich_tb as RT
from tests.unit.test_wp10_link import make_snapshot, run as package


def test_capture_and_staged_fallback_follow_real_cache_bytes(tmp_path):
    snap = make_snapshot(tmp_path, RT.rich_units(2))
    warm = Path(snap.path)
    prep = tmp_path / "prep-s"
    (prep / "prep.json").write_text(json.dumps({
        "schema": "scopt.prepare/1", "verdict": "PASS", "runs": [{"kind": "cold"}],
        "sc_sha256": "sha256:" + sha256_sparse(warm / "cache.sc"),
    }))
    assert ordinary_warm(warm) is None
    assert ordinary_warm(warm, prep=prep).path == warm
    prepared = import_snapshot(prep, store=tmp_path / "store")
    assert prepared.snapshot.snapshot_id == snap.snapshot_id
    assert ordinary_warm(warm).path == warm
    unit = snap.units()[0]
    moved = package(snap, tmp_path/"artifacts", ("move-units", {"deltas":{unit.uid: 64*50}}))
    assert moved.published
    artifact = Path(moved.artifact_dir)
    child = Path(import_snapshot(sc=str(artifact / "cache.sc"), scms=list(artifact.glob("*.scm")),
                                 store=tmp_path/"derived").store_dir)
    assert ordinary_warm(child) is None
    assert ordinary_warm(child, baseline=child, prep=prep) is None
    assert ordinary_warm(child, baseline=warm).path == warm

    # Old imports have no auxiliary origin index. A verified package bridges the stores.
    (tmp_path/"legacy/snapshots").mkdir(parents=True)
    legacy = tmp_path / "legacy/snapshots" / child.name
    shutil.copytree(child, legacy)
    assert ordinary_warm(legacy, baseline=warm, prep=prep) is None
    assert ordinary_warm(legacy, baseline=warm, prep=prep, stages=[artifact]).path == warm
    assert ordinary_warm(warm, baseline=child, prep=prep, stages=[artifact]) is None
    cases = tmp_path / "cases.json"
    cases.write_text('{"inputs":[]}')
    result = CliRunner().invoke(optimize_cmd, [str(legacy), "--store", str(tmp_path/"out"),
        "--pipeline", "identity", "--warm-baseline", str(warm), "--select-with", str(cases),
        "--selection-out", str(tmp_path/"timings")])
    assert result.exit_code == 2
    assert "Cannot establish the original warm baseline" in result.output


def test_warm_identity_stays_bound_between_packaging_and_timing(tmp_path):
    from scopt.run.warm_selection import select_warm_cache

    warm, candidate = tmp_path / "warm.sc", tmp_path / "candidate.sc"
    warm.write_bytes(b"capture")
    digest = "sha256:" + sha256_sparse(warm)
    warm.write_bytes(b"replacement")
    candidate.write_bytes(warm.read_bytes())
    result = select_warm_cache(warm, candidate, tmp_path/"selection",
                               inputs=[], expected_warm_sha256=digest)
    assert result["reason"] == "warm_baseline_changed"
    assert result["selected_artifact"] is None


def test_convergent_sources_require_an_explicit_capture(tmp_path):
    from scopt.importer.warm_origin import record_import
    import pytest

    captures = []
    for n in (1, 2):
        snap = make_snapshot(tmp_path, RT.rich_units(n), name=str(n))
        path, prep = Path(snap.path), tmp_path / f"prep-{n}"
        proof = {"schema": "scopt.prepare/1", "verdict": "PASS", "runs": [{"kind": "cold"}],
                 "sc_sha256": "sha256:" + sha256_sparse(path / "cache.sc")}
        (prep/"prep.json").write_text(json.dumps(proof))
        record_import(path, path/"cache.sc", prep)
        record_import(path, path/"cache.sc", prep)
        captures.append((snap.snapshot_id, path))
    target = make_snapshot(tmp_path, RT.rich_units(3), name="target")
    target_path = Path(target.path)
    edges = tmp_path/"store/warm-origins"/target.snapshot_id[7:]
    edges.mkdir(parents=True)
    for parent, _ in captures:
        (edges/(parent[7:]+".json")).write_text(json.dumps(
            {"snapshot_id": target.snapshot_id, "parent": parent}))
    assert ordinary_warm(target_path) is None
    for _, path in captures:
        assert ordinary_warm(target_path, baseline=path).path == path

    first = captures[0][1]
    assert ordinary_warm(target_path, baseline=first, prep=tmp_path/"prep-2") is None
    (tmp_path/"prep-1/prep.json").write_text('{"verdict":"FAIL"}')
    with pytest.raises(ValueError, match="invalid ordinary capture receipt"):
        ordinary_warm(target_path, baseline=first, prep=tmp_path/"prep-1")
