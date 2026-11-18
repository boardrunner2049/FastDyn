"""Tests for hash-bound publication of frozen BOLT policy choices."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastdyn.preflight.bolt_policy import (
    load_bolt_deployment,
    publish_bolt_deployment,
    publish_fixed_layout_firmware_candidate,
    publish_relocatable_firmware_candidate,
)
from fastdyn.preflight.candidate import load_firmware_candidate
from fastdyn.preflight.model import (
    BundleValidationError,
    TargetDescription,
    hash_file,
)


def _evidence() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[2]
    directory = (
        root
        / "build/preflight/recovered-firmware-corpus-2026-11-16"
        / "bolt-firmware-wrapper"
    )
    selection = directory / "policy-selection.json"
    evidence = directory / "held-out-policy-report.json"
    if not selection.is_file() or not evidence.is_file():
        pytest.skip("recovered-corpus BOLT policy evidence is absent")
    return selection, evidence


@pytest.mark.parametrize(
    ("workload", "layout_mode", "expected_choice", "artifact_key"),
    (
        ("dshotcrc", "relocatable", "full_fop", "full_relocatable"),
        ("crc8", "relocatable", "reference", "reference"),
        (
            "vListInsertEnd", "fixed_layout", "compact_fop",
            "compact_fixed_layout",
        ),
        ("dshotcrc", "fixed_layout", "reference", "reference"),
    ),
)
def test_publish_frozen_choice(
    tmp_path: Path,
    workload: str,
    layout_mode: str,
    expected_choice: str,
    artifact_key: str,
) -> None:
    selection, evidence = _evidence()
    evidence_doc = json.loads(evidence.read_text())
    published = publish_bolt_deployment(
        tmp_path / "published",
        selection_path=selection,
        evidence_path=evidence,
        workload_id=workload,
        layout_mode=layout_mode,
    )
    assert published.choice == expected_choice
    assert published.image_hash == evidence_doc["artifacts"][artifact_key]["sha256"]
    assert hash_file(published.image_path) == published.image_hash
    assert load_bolt_deployment(published.path) == published


def test_load_rejects_tampered_selected_image(tmp_path: Path) -> None:
    selection, evidence = _evidence()
    published = publish_bolt_deployment(
        tmp_path / "published",
        selection_path=selection,
        evidence_path=evidence,
        workload_id="crc8",
        layout_mode="relocatable",
    )
    published.image_path.write_bytes(published.image_path.read_bytes() + b"tamper")
    with pytest.raises(BundleValidationError, match="image hash mismatch"):
        load_bolt_deployment(published.path)


def test_publish_rejects_selection_not_bound_by_evidence(tmp_path: Path) -> None:
    selection, evidence = _evidence()
    changed = json.loads(selection.read_text())
    changed["minimum_useful_cpu_ratio"] = 2.0
    changed_path = tmp_path / "changed-selection.json"
    changed_path.write_text(json.dumps(changed, sort_keys=True) + "\n")
    with pytest.raises(BundleValidationError, match="does not bind the selection"):
        publish_bolt_deployment(
            tmp_path / "published",
            selection_path=changed_path,
            evidence_path=evidence,
            workload_id="crc8",
            layout_mode="relocatable",
        )


def test_publish_is_non_overwriting(tmp_path: Path) -> None:
    selection, evidence = _evidence()
    destination = tmp_path / "published"
    publish_bolt_deployment(
        destination,
        selection_path=selection,
        evidence_path=evidence,
        workload_id="crc8",
        layout_mode="relocatable",
    )
    with pytest.raises(FileExistsError):
        publish_bolt_deployment(
            destination,
            selection_path=selection,
            evidence_path=evidence,
            workload_id="crc8",
            layout_mode="relocatable",
        )


def test_complete_published_policy_is_loadable() -> None:
    selection, _evidence_path = _evidence()
    root = selection.parent / "published-policy"
    if not root.is_dir():
        pytest.skip("complete BOLT policy publication is absent")
    index = json.loads((root / "index.json").read_text())
    assert index["status"] == "atomically_published"
    assert index["deployment_count"] == 18
    deployments = [
        load_bolt_deployment(root / item["relative_path"])
        for item in index["deployments"]
    ]
    assert len({item.decision_id for item in deployments}) == 18
    assert sum(
        item.layout_mode == "relocatable" and item.choice == "full_fop"
        for item in deployments
    ) == 6
    assert sum(
        item.layout_mode == "relocatable" and item.choice == "reference"
        for item in deployments
    ) == 3
    assert sum(
        item.layout_mode == "fixed_layout" and item.choice == "compact_fop"
        for item in deployments
    ) == 3
    assert sum(
        item.layout_mode == "fixed_layout" and item.choice == "reference"
        for item in deployments
    ) == 6


def test_fixed_layout_deployment_publishes_standard_firmware_candidate(
    tmp_path: Path,
) -> None:
    selection, _evidence_path = _evidence()
    deployment = (
        selection.parent
        / "published-policy/vListInsertEnd/fixed_layout"
    )
    if not deployment.is_dir():
        pytest.skip("complete fixed-layout policy publication is absent")
    candidate = publish_fixed_layout_firmware_candidate(
        deployment,
        tmp_path / "candidate",
        target=TargetDescription(
            "x86_64", "long", "little", "x86-64-v1", ()
        ),
    )
    assert candidate.original_image_hash != candidate.image_hash
    assert candidate.image_hash == load_bolt_deployment(deployment).image_hash
    assert load_firmware_candidate(candidate.path) == candidate
    report = json.loads(
        (candidate.path / "optimization-report.json").read_text()
    )
    assert report["address_preserving"] is True
    assert report["choice"] == "compact_fop"
    assert report["patch_contract"] == "empty_phase_one"


def test_relocatable_deployment_requires_nonidentity_origin_adapter(
    tmp_path: Path,
) -> None:
    selection, _evidence_path = _evidence()
    deployment = selection.parent / "published-policy/dshotcrc/relocatable"
    if not deployment.is_dir():
        pytest.skip("complete relocatable policy publication is absent")
    with pytest.raises(BundleValidationError, match="only address-preserving"):
        publish_fixed_layout_firmware_candidate(
            deployment,
            tmp_path / "candidate",
            target=TargetDescription(
                "x86_64", "long", "little", "x86-64-v1", ()
            ),
        )


def test_bat_relocatable_candidate_has_complete_instruction_origins(
    tmp_path: Path,
) -> None:
    selection, _evidence_path = _evidence()
    wrapper = selection.parent
    deployment = wrapper / "published-policy/dshotcrc/relocatable"
    if not deployment.is_dir():
        pytest.skip("complete relocatable policy publication is absent")
    root = Path(__file__).resolve().parents[2]
    candidate = publish_relocatable_firmware_candidate(
        deployment,
        tmp_path / "candidate",
        target=TargetDescription(
            "x86_64", "long", "little", "x86-64-v1", ()
        ),
        bat_dump=root / "third_party/preflight/llvm-build/bin/llvm-bat-dump",
        native_report=wrapper / "bolt-post-optimization-report.json",
    )
    assert load_firmware_candidate(candidate.path) == candidate
    report = json.loads(
        (candidate.path / "optimization-report.json").read_text()
    )
    assert report["bat_instruction_origin_count"] == 534
    assert report["entry_origin_complete"] is True
    origins = json.loads((candidate.path / "origin-map.json").read_text())[
        "ranges"
    ]
    assert sum(
        item["mapping"] == "bolt-bat-instruction" for item in origins
    ) == 534
    assert sum(item["mapping"] == "identity" for item in origins) == 4


def test_bat_source_must_have_identical_executable_sections(tmp_path: Path) -> None:
    selection, evidence_path = _evidence()
    wrapper = selection.parent
    deployment = wrapper / "published-policy/dshotcrc/relocatable"
    if not deployment.is_dir():
        pytest.skip("complete relocatable policy publication is absent")
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(BundleValidationError, match="identical executable sections"):
        publish_relocatable_firmware_candidate(
            deployment,
            tmp_path / "candidate",
            target=TargetDescription(
                "x86_64", "long", "little", "x86-64-v1", ()
            ),
            bat_dump=root / "third_party/preflight/llvm-build/bin/llvm-bat-dump",
            native_report=wrapper / "bolt-post-optimization-report.json",
            bat_source_image=Path(
                json.loads(evidence_path.read_text())["artifacts"]["reference"][
                    "path"
                ]
            ),
        )


@pytest.mark.parametrize(
    ("relative_root", "expected_count"),
    (
        (
            "build/preflight/recovered-firmware-corpus-2026-11-16/"
            "bolt-firmware-wrapper/fixed-layout-firmware-candidates",
            9,
        ),
        (
            "build/preflight/lemix-queue/bolt-firmware-wrapper/"
            "fixed-layout-firmware-candidates",
            1,
        ),
    ),
)
def test_complete_fixed_layout_candidates_are_consumer_loadable(
    relative_root: str, expected_count: int
) -> None:
    root = Path(__file__).resolve().parents[2] / relative_root
    if not root.is_dir():
        pytest.skip("complete fixed-layout FirmwareCandidate publication is absent")
    index = json.loads((root / "index.json").read_text())
    assert index["status"] == "atomically_published"
    assert index["candidate_count"] == expected_count
    candidates = [
        load_firmware_candidate(root / item["relative_path"])
        for item in index["candidates"]
    ]
    assert len(candidates) == expected_count
    assert len({item.candidate_id for item in candidates}) == expected_count


@pytest.mark.parametrize(
    ("relative_root", "expected_count", "expected_bat"),
    (
        (
            "build/preflight/recovered-firmware-corpus-2026-11-16/"
            "bolt-firmware-wrapper/relocatable-firmware-candidates",
            9,
            6,
        ),
        (
            "build/preflight/lemix-queue/bolt-firmware-wrapper/"
            "relocatable-firmware-candidates",
            1,
            1,
        ),
    ),
)
def test_complete_relocatable_candidates_are_consumer_loadable(
    relative_root: str, expected_count: int, expected_bat: int
) -> None:
    root = Path(__file__).resolve().parents[2] / relative_root
    if not root.is_dir():
        pytest.skip("complete relocatable FirmwareCandidate publication is absent")
    index = json.loads((root / "index.json").read_text())
    assert index["status"] == "atomically_published"
    assert index["candidate_count"] == expected_count
    assert sum(
        item["origin_policy"] == "bolt_bat_instruction_origins"
        for item in index["candidates"]
    ) == expected_bat
    candidates = [
        load_firmware_candidate(root / item["relative_path"])
        for item in index["candidates"]
    ]
    assert len(candidates) == expected_count
    assert len({item.candidate_id for item in candidates}) == expected_count


def test_smartspeaker_stock_fallback_and_bat_candidate_are_loadable() -> None:
    root = (
        Path(__file__).resolve().parents[2]
        / "build/preflight/lemix-smartspeaker-hot-fop-2026-11-17"
    )
    if not (root / "published-policy/index.json").is_file():
        pytest.skip("SmartSpeaker stock-BOLT publication is absent")
    policy = json.loads((root / "published-policy/index.json").read_text())
    assert policy["deployment_count"] == 1
    record = policy["deployments"][0]
    deployment = load_bolt_deployment(
        root / "published-policy" / record["relative_path"]
    )
    assert deployment.choice == "stock_bolt"
    candidates = json.loads(
        (root / "relocatable-firmware-candidates/index.json").read_text()
    )
    assert candidates["candidate_count"] == 1
    candidate = load_firmware_candidate(
        root / "relocatable-firmware-candidates"
        / candidates["candidates"][0]["relative_path"]
    )
    assert candidate.image_hash == deployment.image_hash
    report = json.loads(
        (candidate.path / "optimization-report.json").read_text()
    )
    assert report["bat_instruction_origin_count"] == 85_731
    assert report["bat_source_is_deployment_image"] is False
    assert report["entry_origin_complete"] is True
