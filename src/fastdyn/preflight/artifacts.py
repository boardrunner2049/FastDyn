"""Phase-two FastDyn ExecutionBundle artifacts and atomic publication."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
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
MANIFEST_FILE = "execution-bundle.json"
PATCH_PLAN_FILE = "patch-plan.json"
PATCH_BINDINGS_FILE = "patch-bindings.json"
ORIGIN_MAP_FILE = "origin-map.json"
MEMORY_MAP_FILE = "memory-map.json"
INSTRUMENTATION_FILE = "instrumentation.json"
DEVICE_MODEL_FILE = "device-model.json"


@dataclass(frozen=True)
class PatchBinding:
    """One exact patch anchor and its required execution semantics."""

    binding_id: str
    original_identity: str
    original_pc: int
    current_pc: int
    isa: str
    mode: str
    trigger_phase: str
    kind: str
    action: Mapping[str, Any]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    multiplicity: str = "each_execution"
    placement: str = "exact_anchor"
    provenance: str = "identity"
    clone_policy: str = "forbidden"

    def __post_init__(self) -> None:
        if not self.binding_id or not self.original_identity:
            raise BundleValidationError("patch bindings require stable identities")
        if self.original_pc < 0 or self.current_pc < 0:
            raise BundleValidationError("patch PCs must be non-negative")
        if self.placement != "exact_anchor":
            raise BundleValidationError("nearest-address or inexact patch placement is forbidden")
        if self.clone_policy not in {"forbidden", "replicate_each_clone", "selected_clone"}:
            raise BundleValidationError("unknown patch clone policy")

    def to_dict(self) -> dict[str, Any]:
        """Return the schema representation of this binding."""

        value = asdict(self)
        value["action"] = dict(self.action)
        value["inputs"] = list(self.inputs)
        value["outputs"] = list(self.outputs)
        return value


@dataclass(frozen=True)
class ExecutionBundle:
    """Loaded and validated ExecutionBundle v1 metadata."""

    path: Path
    schema_version: int
    bundle_id: str
    original_image_hash: str
    image_hash: str
    patch_plan_hash: str
    patch_bindings_hash: str
    origin_map_hash: str
    target: TargetDescription
    entry_points: tuple[int, ...]
    memory_map_hash: str
    contract_id: str
    instrumentation_hash: str
    device_model_hash: str
    image_file: str

    @property
    def image_path(self) -> Path:
        """Return the exact executable image belonging to this bundle."""

        return self.path / self.image_file

    def identity_material(self) -> dict[str, Any]:
        """Return the content fields from which ``bundle_id`` is derived."""

        return {
            "schema_version": self.schema_version,
            "original_image_hash": self.original_image_hash,
            "image_hash": self.image_hash,
            "patch_plan_hash": self.patch_plan_hash,
            "patch_bindings_hash": self.patch_bindings_hash,
            "origin_map_hash": self.origin_map_hash,
            "target": self.target.to_dict(),
            "entry_points": list(self.entry_points),
            "memory_map_hash": self.memory_map_hash,
            "contract_id": self.contract_id,
            "instrumentation_hash": self.instrumentation_hash,
            "device_model_hash": self.device_model_hash,
        }


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _binding_is_covered(binding: PatchBinding, ranges: Iterable[OriginRange]) -> bool:
    return any(
        item.original_start <= binding.original_pc < item.original_end
        and item.emitted_start <= binding.current_pc < item.emitted_end
        for item in ranges
    )


def publish_execution_bundle(
    destination: Path,
    *,
    image: Path,
    original_image_hash: str,
    target: TargetDescription,
    entry_points: Iterable[int],
    memory_map: Iterable[MemoryRange],
    patch_plan: Mapping[str, Any],
    patch_bindings: Iterable[PatchBinding],
    origin_ranges: Iterable[OriginRange],
    contract_id: str,
    instrumentation: Mapping[str, Any],
    device_model: Mapping[str, Any],
) -> ExecutionBundle:
    """Validate and atomically publish one immutable ExecutionBundle directory.

    The destination must not already exist. All files are first written to a sibling
    staging directory and the complete directory is renamed into place only after it
    validates successfully.
    """

    destination = destination.resolve()
    image = image.resolve()
    if not image.is_file():
        raise FileNotFoundError(image)
    if destination.exists():
        raise FileExistsError(f"ExecutionBundle destination already exists: {destination}")
    if not contract_id:
        raise BundleValidationError("contract_id must not be empty")
    require_hex_digest("original_image_hash", original_image_hash)

    bindings = tuple(patch_bindings)
    ranges = tuple(origin_ranges)
    memory = tuple(memory_map)
    if len({item.binding_id for item in bindings}) != len(bindings):
        raise BundleValidationError("patch binding IDs must be unique")
    if any(not _binding_is_covered(binding, ranges) for binding in bindings):
        raise BundleValidationError("every patch binding must have an exact origin mapping")

    image_hash = hash_file(image)
    patch_plan_doc = {"schema_version": SCHEMA_VERSION, **dict(patch_plan)}
    bindings_doc = {
        "schema_version": SCHEMA_VERSION,
        "bindings": [item.to_dict() for item in bindings],
    }
    origin_doc = {
        "schema_version": SCHEMA_VERSION,
        "ranges": [item.to_dict() for item in ranges],
    }
    memory_doc = {
        "schema_version": SCHEMA_VERSION,
        "ranges": [item.to_dict() for item in memory],
    }
    instrumentation_doc = dict(instrumentation)
    device_model_doc = dict(device_model)
    entry_values = tuple(sorted(set(int(value) for value in entry_points)))
    if any(value < 0 for value in entry_values):
        raise BundleValidationError("entry points must be non-negative")

    identity = {
        "schema_version": SCHEMA_VERSION,
        "original_image_hash": original_image_hash,
        "image_hash": image_hash,
        "patch_plan_hash": hash_json(patch_plan_doc),
        "patch_bindings_hash": hash_json(bindings_doc),
        "origin_map_hash": hash_json(origin_doc),
        "target": target.to_dict(),
        "entry_points": list(entry_values),
        "memory_map_hash": hash_json(memory_doc),
        "contract_id": contract_id,
        "instrumentation_hash": hash_json(instrumentation_doc),
        "device_model_hash": hash_json(device_model_doc),
    }
    bundle_id = hash_json(identity)
    suffix = image.suffix if image.suffix and len(image.suffix) <= 16 else ".bin"
    image_file = f"image{suffix}"
    manifest = {
        **identity,
        "bundle_id": bundle_id,
        "image_file": image_file,
        "artifacts": {
            "patch_plan": PATCH_PLAN_FILE,
            "patch_bindings": PATCH_BINDINGS_FILE,
            "origin_map": ORIGIN_MAP_FILE,
            "memory_map": MEMORY_MAP_FILE,
            "instrumentation": INSTRUMENTATION_FILE,
            "device_model": DEVICE_MODEL_FILE,
        },
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        shutil.copyfile(image, staging / image_file)
        _write_json(staging / PATCH_PLAN_FILE, patch_plan_doc)
        _write_json(staging / PATCH_BINDINGS_FILE, bindings_doc)
        _write_json(staging / ORIGIN_MAP_FILE, origin_doc)
        _write_json(staging / MEMORY_MAP_FILE, memory_doc)
        _write_json(staging / INSTRUMENTATION_FILE, instrumentation_doc)
        _write_json(staging / DEVICE_MODEL_FILE, device_model_doc)
        _write_json(staging / MANIFEST_FILE, manifest)
        load_execution_bundle(staging)
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return load_execution_bundle(destination)


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


def load_execution_bundle(path: Path) -> ExecutionBundle:
    """Load an ExecutionBundle and verify all content identities."""

    path = path.resolve()
    manifest = _load_json(path / MANIFEST_FILE)
    try:
        bundle = ExecutionBundle(
            path=path,
            schema_version=int(manifest["schema_version"]),
            bundle_id=str(manifest["bundle_id"]),
            original_image_hash=str(manifest["original_image_hash"]),
            image_hash=str(manifest["image_hash"]),
            patch_plan_hash=str(manifest["patch_plan_hash"]),
            patch_bindings_hash=str(manifest["patch_bindings_hash"]),
            origin_map_hash=str(manifest["origin_map_hash"]),
            target=_target_from_dict(manifest["target"]),
            entry_points=tuple(int(item) for item in manifest["entry_points"]),
            memory_map_hash=str(manifest["memory_map_hash"]),
            contract_id=str(manifest["contract_id"]),
            instrumentation_hash=str(manifest["instrumentation_hash"]),
            device_model_hash=str(manifest["device_model_hash"]),
            image_file=str(manifest["image_file"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleValidationError(f"invalid ExecutionBundle manifest: {exc}") from exc

    if bundle.schema_version != SCHEMA_VERSION:
        raise BundleValidationError(
            f"unsupported ExecutionBundle schema {bundle.schema_version}; expected {SCHEMA_VERSION}"
        )
    for name in (
        "bundle_id",
        "original_image_hash",
        "image_hash",
        "patch_plan_hash",
        "patch_bindings_hash",
        "origin_map_hash",
        "memory_map_hash",
        "instrumentation_hash",
        "device_model_hash",
    ):
        require_hex_digest(name, getattr(bundle, name))
    image_name = Path(bundle.image_file)
    if image_name.name != bundle.image_file or bundle.image_file in {"", ".", ".."}:
        raise BundleValidationError("image_file must be a simple relative filename")
    expected_artifacts = {
        "patch_plan": PATCH_PLAN_FILE,
        "patch_bindings": PATCH_BINDINGS_FILE,
        "origin_map": ORIGIN_MAP_FILE,
        "memory_map": MEMORY_MAP_FILE,
        "instrumentation": INSTRUMENTATION_FILE,
        "device_model": DEVICE_MODEL_FILE,
    }
    if manifest.get("artifacts") != expected_artifacts:
        raise BundleValidationError("ExecutionBundle artifact names do not match schema v1")

    patch_plan = _load_json(path / PATCH_PLAN_FILE)
    bindings = _load_json(path / PATCH_BINDINGS_FILE)
    origins = _load_json(path / ORIGIN_MAP_FILE)
    memory_map = _load_json(path / MEMORY_MAP_FILE)
    instrumentation = _load_json(path / INSTRUMENTATION_FILE)
    device_model = _load_json(path / DEVICE_MODEL_FILE)
    checks = {
        "image_hash": hash_file(bundle.image_path),
        "patch_plan_hash": hash_json(patch_plan),
        "patch_bindings_hash": hash_json(bindings),
        "origin_map_hash": hash_json(origins),
        "memory_map_hash": hash_json(memory_map),
        "instrumentation_hash": hash_json(instrumentation),
        "device_model_hash": hash_json(device_model),
        "bundle_id": hash_json(bundle.identity_material()),
    }
    for name, actual in checks.items():
        expected = getattr(bundle, name)
        if actual != expected:
            raise BundleValidationError(f"{name} mismatch: expected {expected}, got {actual}")

    try:
        if any(
            document.get("schema_version") != SCHEMA_VERSION
            for document in (patch_plan, bindings, origins, memory_map)
        ):
            raise BundleValidationError("artifact schema version does not match bundle schema")
        loaded_ranges = tuple(OriginRange(**item) for item in origins["ranges"])
        loaded_memory = tuple(MemoryRange(**item) for item in memory_map["ranges"])
        loaded_bindings = tuple(
            PatchBinding(
                binding_id=str(item["binding_id"]),
                original_identity=str(item["original_identity"]),
                original_pc=int(item["original_pc"]),
                current_pc=int(item["current_pc"]),
                isa=str(item["isa"]),
                mode=str(item["mode"]),
                trigger_phase=str(item["trigger_phase"]),
                kind=str(item["kind"]),
                action=item["action"],
                inputs=tuple(str(value) for value in item["inputs"]),
                outputs=tuple(str(value) for value in item["outputs"]),
                multiplicity=str(item.get("multiplicity", "each_execution")),
                placement=str(item.get("placement", "exact_anchor")),
                provenance=str(item.get("provenance", "identity")),
                clone_policy=str(item.get("clone_policy", "forbidden")),
            )
            for item in bindings["bindings"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleValidationError(f"invalid bundle artifact schema: {exc}") from exc
    if len({item.name for item in loaded_memory}) != len(loaded_memory):
        raise BundleValidationError("memory range names must be unique")
    if len({item.binding_id for item in loaded_bindings}) != len(loaded_bindings):
        raise BundleValidationError("patch binding IDs must be unique")
    if any(not _binding_is_covered(item, loaded_ranges) for item in loaded_bindings):
        raise BundleValidationError("every patch binding must have an exact origin mapping")
    if any(
        item.isa != bundle.target.isa or item.mode != bundle.target.mode
        for item in loaded_bindings
    ):
        raise BundleValidationError("patch binding target does not match bundle target")
    return bundle
