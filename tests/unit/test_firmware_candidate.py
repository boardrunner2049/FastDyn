"""Tests for patch-independent phase-one FirmwareCandidate artifacts."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fastdyn.preflight import (
    BundleValidationError,
    MemoryRange,
    OriginRange,
    TargetDescription,
    create_identity_firmware_candidate,
    load_firmware_candidate,
    publish_firmware_candidate,
)
from fastdyn.preflight.model import hash_file


def _publish(tmp_path: Path, *, status: str = "identity"):
    image = tmp_path / "firmware.elf"
    image.write_bytes(b"firmware-image")
    image_hash = hash_file(image)
    return publish_firmware_candidate(
        tmp_path / f"candidate-{status}",
        image=image,
        original_image_hash=image_hash,
        target=TargetDescription(
            "arm",
            "thumb",
            "little",
            "cortex-m3",
            ("armv7-m", "thumb"),
        ),
        entry_points=(0x100,),
        memory_map=(MemoryRange("flash", 0, 0x1000, "r-x", "flash"),),
        origin_ranges=(OriginRange(0x100, 0x10E, 0x100, 0x10E),),
        optimization_report={
            "status": status,
            "passes": [],
            "code_growth_bytes": 0,
            "reason_codes": [],
        },
    )


def test_candidate_schema_is_patch_independent(tmp_path):
    candidate = _publish(tmp_path)
    manifest = json.loads((candidate.path / "firmware-candidate.json").read_text())

    assert manifest["artifact_kind"] == "firmware_candidate"
    assert set(manifest["artifacts"]) == {
        "origin_map",
        "memory_map",
        "optimization_report",
    }
    serialized = json.dumps(manifest, sort_keys=True).lower()
    assert "patch" not in serialized
    assert "fastdyn" not in serialized
    assert load_firmware_candidate(candidate.path).candidate_id == candidate.candidate_id


def test_optimization_report_changes_candidate_identity(tmp_path):
    identity = _publish(tmp_path, status="identity")
    optimized = _publish(tmp_path, status="optimized")

    assert identity.image_hash == optimized.image_hash
    assert identity.optimization_report_hash != optimized.optimization_report_hash
    assert identity.candidate_id != optimized.candidate_id


def test_candidate_tampering_is_rejected(tmp_path):
    candidate = _publish(tmp_path)
    report = candidate.path / "optimization-report.json"
    report.write_text('{"schema_version":1,"status":"different"}\n')

    with pytest.raises(BundleValidationError, match="optimization_report_hash mismatch"):
        load_firmware_candidate(candidate.path)


def test_candidate_requires_provenance(tmp_path):
    image = tmp_path / "firmware.elf"
    image.write_bytes(b"firmware-image")

    with pytest.raises(BundleValidationError, match="requires an origin map"):
        publish_firmware_candidate(
            tmp_path / "candidate",
            image=image,
            original_image_hash=hash_file(image),
            target=TargetDescription(
                "arm",
                "thumb",
                "little",
                "cortex-m3",
                ("armv7-m", "thumb"),
            ),
            entry_points=(),
            memory_map=(),
            origin_ranges=(),
            optimization_report={"status": "identity"},
        )


def test_real_cortex_m_identity_candidate_requires_no_patches(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    image = repository / "tests/sample_binaries/RTOS/RTOSDemo.axf"
    candidate = create_identity_firmware_candidate(
        image,
        SimpleNamespace(arch="arm", cpu="cortex-m3", init_nsvtor=0x08000000),
        tmp_path / "real-candidate",
    )
    manifest = json.loads((candidate.path / "firmware-candidate.json").read_text())

    assert candidate.original_image_hash == candidate.image_hash
    assert candidate.target.cpu == "cortex-m3"
    assert "patch" not in json.dumps(manifest, sort_keys=True).lower()


def test_phase_one_modules_do_not_import_phase_two_services():
    repository = Path(__file__).resolve().parents[2]
    phase_one = repository / "src/fastdyn/preflight"
    forbidden = {"artifacts", "contracts", "driver", "validation"}

    for filename in ("model.py", "candidate.py", "firmware.py"):
        tree = ast.parse((phase_one / filename).read_text(encoding="utf-8"))
        imported = {
            alias.name.rsplit(".", 1)[-1]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        modules = {
            (node.module or "").rsplit(".", 1)[-1]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not (forbidden & (imported | modules)), filename
