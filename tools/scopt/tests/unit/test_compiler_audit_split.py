"""Compiler/load/audit separation for ordinary scopt optimization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scopt import validate as V
from scopt.canon import hexaddr, sha256_id
from scopt.cli import cli, load_commands
from scopt.formats import artifact as A
from scopt.formats.sc_records import read_sc, sparse_copy
from scopt.importer.compiler_ir import NAME as COMPILER_IR, write_compiler_ir
from scopt.importer.import_cache import open_compiler_snapshot
from scopt.model.cache_snapshot import CacheSnapshot
from tests.synth import rich_tb as RT
from tests.unit.test_wp10_link import make_snapshot, pipeline


@pytest.fixture
def compiled(tmp_path, monkeypatch):
    """Compile an identity artifact while making the audit path fatal."""
    snap = make_snapshot(tmp_path, RT.rich_units(2))
    validate_static = V.validate_static
    reimport = V.reimport

    def audit_must_be_explicit(*_args, **_kwargs):
        pytest.fail("optimize invoked the normal audit/proof path")

    monkeypatch.setattr(V, "validate_static", audit_must_be_explicit)
    monkeypatch.setattr(V, "reimport", audit_must_be_explicit)
    result = V.optimize(snap, pipeline("identity", caps=()), tmp_path / "compiled")
    monkeypatch.setattr(V, "validate_static", validate_static)
    monkeypatch.setattr(V, "reimport", reimport)
    assert result.published, result.reason
    return snap, result, Path(result.artifact_dir)


def test_optimize_emits_unaudited_compiler_receipt_without_normal_proof(compiled):
    snap, result, artifact = compiled
    receipt_path = artifact / "compile-receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    sidecar = json.loads((artifact / A.SIDECAR).read_text())

    assert result.validation is None
    assert result.output_snapshot is not None
    assert result.output_snapshot.snapshot_id == result.output_snapshot_id
    assert receipt["schema"] == "scopt.compile-receipt"
    assert receipt["assurance"] == "compiler-produced/user-vouched"
    assert receipt["audited"] is False
    assert receipt["input_snapshot_id"] == snap.snapshot_id
    assert receipt["output_snapshot_id"] == result.output_snapshot_id
    assert receipt["audit_command"].startswith("scopt audit ")
    assert sidecar["x_assurance"] == receipt["assurance"]
    assert sidecar["x_audited"] is False
    assert sidecar["x_receipt"] == receipt_path.name
    assert sidecar["validation_sha256"] == sha256_id(receipt_bytes)
    assert not (artifact / A.VALIDATION).exists()
    assert A.verify_manifest(artifact) == []


def test_load_accepts_matching_runtime_and_refuses_mismatch(compiled, tmp_path):
    _snap, _result, artifact = compiled
    header = read_sc(artifact / "cache.sc").header
    runtime = json.loads((artifact / "runtime_identity.json").read_text())
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(json.dumps(runtime))

    runner, commands = CliRunner(), load_commands(cli)
    good = runner.invoke(commands, [
        "load", str(artifact), "--runtime-identity", str(runtime_path), "--json",
    ])
    assert good.exit_code == 0, good.output
    doc = json.loads(good.output)
    assert doc["loadable"] is True and doc["problems"] == []
    assert doc["assurance"] == "compiler-produced/user-vouched"
    assert doc["audited"] is False

    runtime["runtime_signature"] = hexaddr(header.runtime_signature ^ 1)
    runtime_path.write_text(json.dumps(runtime))
    bad = runner.invoke(commands, [
        "load", str(artifact), "--runtime-identity", str(runtime_path), "--json",
    ])
    assert bad.exit_code == 2, bad.output
    refused = json.loads(bad.output)
    assert refused["loadable"] is False
    assert any("runtime mismatch: runtime_signature" in problem
               for problem in refused["problems"])


def test_audit_is_a_discoverable_cli_command():
    result = CliRunner().invoke(load_commands(cli), ["audit", "--help"])
    assert result.exit_code == 0, result.output
    assert "Explicitly audit a published ARTIFACT" in result.output
    assert "--dynamic" in result.output


def test_explicit_audit_reconstructs_evidence_not_shipped_by_compiler(compiled):
    snap, _result, artifact = compiled
    assert not list(artifact.glob("*.scm"))
    assert not (artifact / A.VALIDATION).exists()

    doc = V.validate_artifact(artifact, snap)

    assert doc["verdict"] == "accepted"
    assert doc["stored_codes_agree"] is None
    assert doc["codes"]["V8"] == "pass"
    # Audit evidence is temporary: the compiler artifact remains unchanged.
    assert not list(artifact.glob("*.scm"))
    assert not (artifact / A.VALIDATION).exists()


def test_frozen_compiler_ir_opens_without_reimport(tmp_path, monkeypatch):
    source = make_snapshot(tmp_path / "source", RT.rich_units(2))
    store = tmp_path / "snapshot"
    store.mkdir()
    snapshot = CacheSnapshot(source.snapshot_id, source.header, source.header.to_bytes(),
                             source.units(), runtime=source.runtime, path=str(store))
    (store / "runtime_identity.json").write_text(json.dumps(dict(source.runtime)))
    write_compiler_ir(store / COMPILER_IR, snapshot)

    def no_audit(*_args, **_kwargs):
        pytest.fail("frozen compiler IR invoked the importer")

    monkeypatch.setattr("scopt.importer.import_cache.open_snapshot", no_audit)
    opened = open_compiler_snapshot(store)
    assert opened.snapshot_id == source.snapshot_id
    assert opened.units() == source.units()


def test_trusted_load_skips_only_record_checksum_and_restores_default(compiled, tmp_path):
    _, _, source = compiled
    artifact = tmp_path / "altered"
    artifact.mkdir()
    cache = artifact / "cache.sc"
    sparse_copy(source / "cache.sc", cache)
    parsed = read_sc(cache)
    rec = parsed.records[0]
    runner, commands = CliRunner(), load_commands(cli)

    def load(*options):
        result = runner.invoke(commands, ["load", str(artifact), "--json", *options])
        return result.exit_code, json.loads(result.output)

    with cache.open("r+b") as stream:
        stream.seek(parsed.header.records_offset)
        stream.write(rec.replace(checksum=rec.checksum ^ 1).to_bytes())
    assert load("--trusted")[1]["loadable"]
    code, checked = load()
    assert code == 2 and any("record_checksum" in p for p in checked["problems"])
    with cache.open("r+b") as stream:
        stream.seek(parsed.header.records_offset)
        stream.write(rec.replace(tc_offset=rec.end_offset).to_bytes())
    code, invalid = load("--trusted")
    assert code == 2 and any("bounds" in p for p in invalid["problems"])
