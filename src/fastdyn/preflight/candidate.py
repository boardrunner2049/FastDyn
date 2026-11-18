"""Patch-independent FirmwareCandidate v1 artifacts for phase-one preflight."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import (
    BundleValidationError,
    MemoryRange,
    OriginInstruction,
    OriginRange,
    TargetDescription,
    canonical_json_bytes,
    hash_file,
    hash_json,
    require_hex_digest,
)


SCHEMA_VERSION = 1
MANIFEST_FILE = "firmware-candidate.json"
ORIGIN_MAP_FILE = "origin-map.json"
MEMORY_MAP_FILE = "memory-map.json"
OPTIMIZATION_REPORT_FILE = "optimization-report.json"


@dataclass(frozen=True)
class FirmwareCandidate:
    """A validated phase-one image and its consumer-neutral provenance."""

    path: Path
    schema_version: int
    candidate_id: str
    original_image_hash: str
    image_hash: str
    target: TargetDescription
    entry_points: tuple[int, ...]
    origin_map_hash: str
    memory_map_hash: str
    optimization_report_hash: str
    image_file: str

    @property
    def image_path(self) -> Path:
        """Return the exact candidate image."""

        return self.path / self.image_file

    def identity_material(self) -> dict[str, Any]:
        """Return the fields from which ``candidate_id`` is derived."""

        return {
            "schema_version": self.schema_version,
            "artifact_kind": "firmware_candidate",
            "original_image_hash": self.original_image_hash,
            "image_hash": self.image_hash,
            "target": self.target.to_dict(),
            "entry_points": list(self.entry_points),
            "origin_map_hash": self.origin_map_hash,
            "memory_map_hash": self.memory_map_hash,
            "optimization_report_hash": self.optimization_report_hash,
        }


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleValidationError(f"cannot read {path.name}: {exc}") from exc


def _target_from_dict(value: Mapping[str, Any]) -> TargetDescription:
    try:
        return TargetDescription(
            isa=str(value["isa"]),
            mode=str(value["mode"]),
            endianness=str(value["endianness"]),
            cpu=str(value["cpu"]),
            features=tuple(str(item) for item in value.get("features", ())),
        )
    except (KeyError, TypeError) as exc:
        raise BundleValidationError(f"invalid target description: {exc}") from exc


def publish_firmware_candidate(
    destination: Path,
    *,
    image: Path,
    original_image_hash: str,
    target: TargetDescription,
    entry_points: Iterable[int],
    memory_map: Iterable[MemoryRange],
    origin_ranges: Iterable[OriginRange],
    origin_instructions: Iterable[OriginInstruction] = (),
    optimization_report: Mapping[str, Any],
) -> FirmwareCandidate:
    """Atomically publish a patch-free phase-one firmware candidate.

    The schema intentionally contains no patch plan, hook binding, device model,
    instrumentation configuration or FastDyn contract. Those inputs belong to a
    separate phase-two consumer.
    """

    destination = destination.resolve()
    image = image.resolve()
    if not image.is_file():
        raise FileNotFoundError(image)
    if destination.exists():
        raise FileExistsError(f"FirmwareCandidate destination already exists: {destination}")
    require_hex_digest("original_image_hash", original_image_hash)
    ranges = tuple(origin_ranges)
    if not ranges:
        raise BundleValidationError("FirmwareCandidate requires an origin map")
    memory = tuple(memory_map)
    entries = tuple(sorted(set(int(value) for value in entry_points)))
    if any(value < 0 for value in entries):
        raise BundleValidationError("entry points must be non-negative")

    instructions = tuple(origin_instructions)
    origin_doc = {
        "schema_version": SCHEMA_VERSION,
        "ranges": [item.to_dict() for item in ranges],
    }
    if instructions:
        emitted = [
            (item.emitted_address, item.emitted_address + item.emitted_size)
            for item in instructions
        ]
        if len(set(emitted)) != len(emitted):
            raise BundleValidationError("instruction origins overlap or repeat")
        origin_doc["instructions"] = [item.to_dict() for item in instructions]
    memory_doc = {
        "schema_version": SCHEMA_VERSION,
        "ranges": [item.to_dict() for item in memory],
    }
    report_doc = {
        **dict(optimization_report),
        "schema_version": SCHEMA_VERSION,
    }
    image_hash = hash_file(image)
    identity = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "firmware_candidate",
        "original_image_hash": original_image_hash,
        "image_hash": image_hash,
        "target": target.to_dict(),
        "entry_points": list(entries),
        "origin_map_hash": hash_json(origin_doc),
        "memory_map_hash": hash_json(memory_doc),
        "optimization_report_hash": hash_json(report_doc),
    }
    candidate_id = hash_json(identity)
    suffix = image.suffix if image.suffix and len(image.suffix) <= 16 else ".bin"
    image_file = f"image{suffix}"
    manifest = {
        **identity,
        "candidate_id": candidate_id,
        "image_file": image_file,
        "artifacts": {
            "origin_map": ORIGIN_MAP_FILE,
            "memory_map": MEMORY_MAP_FILE,
            "optimization_report": OPTIMIZATION_REPORT_FILE,
        },
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        shutil.copyfile(image, staging / image_file)
        _write_json(staging / ORIGIN_MAP_FILE, origin_doc)
        _write_json(staging / MEMORY_MAP_FILE, memory_doc)
        _write_json(staging / OPTIMIZATION_REPORT_FILE, report_doc)
        _write_json(staging / MANIFEST_FILE, manifest)
        load_firmware_candidate(staging)
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return load_firmware_candidate(destination)


def load_firmware_candidate(path: Path) -> FirmwareCandidate:
    """Load and rehash a patch-independent FirmwareCandidate directory."""

    path = path.resolve()
    manifest = _load_json(path / MANIFEST_FILE)
    try:
        candidate = FirmwareCandidate(
            path=path,
            schema_version=int(manifest["schema_version"]),
            candidate_id=str(manifest["candidate_id"]),
            original_image_hash=str(manifest["original_image_hash"]),
            image_hash=str(manifest["image_hash"]),
            target=_target_from_dict(manifest["target"]),
            entry_points=tuple(int(value) for value in manifest["entry_points"]),
            origin_map_hash=str(manifest["origin_map_hash"]),
            memory_map_hash=str(manifest["memory_map_hash"]),
            optimization_report_hash=str(manifest["optimization_report_hash"]),
            image_file=str(manifest["image_file"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleValidationError(f"invalid FirmwareCandidate manifest: {exc}") from exc
    if candidate.schema_version != SCHEMA_VERSION:
        raise BundleValidationError(
            f"unsupported FirmwareCandidate schema {candidate.schema_version}; expected {SCHEMA_VERSION}"
        )
    if manifest.get("artifact_kind") != "firmware_candidate":
        raise BundleValidationError("artifact is not a FirmwareCandidate")
    for name in (
        "candidate_id",
        "original_image_hash",
        "image_hash",
        "origin_map_hash",
        "memory_map_hash",
        "optimization_report_hash",
    ):
        require_hex_digest(name, getattr(candidate, name))
    image_name = Path(candidate.image_file)
    if image_name.name != candidate.image_file or candidate.image_file in {"", ".", ".."}:
        raise BundleValidationError("image_file must be a simple relative filename")
    if manifest.get("artifacts") != {
        "origin_map": ORIGIN_MAP_FILE,
        "memory_map": MEMORY_MAP_FILE,
        "optimization_report": OPTIMIZATION_REPORT_FILE,
    }:
        raise BundleValidationError("FirmwareCandidate artifact names do not match schema v1")

    origin_doc = _load_json(path / ORIGIN_MAP_FILE)
    memory_doc = _load_json(path / MEMORY_MAP_FILE)
    report_doc = _load_json(path / OPTIMIZATION_REPORT_FILE)
    if hash_file(candidate.image_path) != candidate.image_hash:
        raise BundleValidationError("image_hash mismatch")
    if hash_json(origin_doc) != candidate.origin_map_hash:
        raise BundleValidationError("origin_map_hash mismatch")
    if hash_json(memory_doc) != candidate.memory_map_hash:
        raise BundleValidationError("memory_map_hash mismatch")
    if hash_json(report_doc) != candidate.optimization_report_hash:
        raise BundleValidationError("optimization_report_hash mismatch")
    if hash_json(candidate.identity_material()) != candidate.candidate_id:
        raise BundleValidationError("candidate_id mismatch")
    return candidate
