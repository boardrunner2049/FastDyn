"""Immutable firmware-region contract for PreflightPass SDK v1.

The schema is intentionally machine-level.  It does not infer source-language
validity, private stack memory, immutable data, or unreachable inputs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .model import BundleValidationError, TargetDescription, hash_json, require_hex_digest


def _tuple_is_sorted_unique(values: tuple[str, ...]) -> bool:
    return tuple(sorted(set(values))) == values


@dataclass(frozen=True)
class InstructionIdentity:
    """One exact decoded instruction and its immutable original identity."""

    identity: str
    original_identity: str
    address: int
    file_offset: int
    size: int
    encoding: str
    mnemonic: str
    operands: str = ""
    possible_faults: tuple[str, ...] = ()
    observer_boundary: bool = False

    def __post_init__(self) -> None:
        if not self.identity or not self.original_identity:
            raise BundleValidationError("instructions require current and original identities")
        if self.address < 0 or self.file_offset < 0 or self.size <= 0:
            raise BundleValidationError("instruction location and size must be valid")
        try:
            decoded = bytes.fromhex(self.encoding)
        except ValueError as exc:
            raise BundleValidationError("instruction encoding must be hexadecimal") from exc
        if len(decoded) != self.size:
            raise BundleValidationError("instruction encoding length does not match size")
        if not self.mnemonic:
            raise BundleValidationError("instruction mnemonic must not be empty")
        if not _tuple_is_sorted_unique(self.possible_faults):
            raise BundleValidationError("possible_faults must be sorted and unique")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TypedRange:
    """Typed half-open address and file range used by layout validation."""

    range_id: str
    address_start: int
    address_end: int
    file_start: int | None
    file_end: int | None
    kind: str
    permissions: str
    mutability: str
    alignment: int = 1

    def __post_init__(self) -> None:
        if not self.range_id or self.address_start < 0 or self.address_end <= self.address_start:
            raise BundleValidationError("typed range is empty or invalid")
        if (self.file_start is None) != (self.file_end is None):
            raise BundleValidationError("file range endpoints must both be present or absent")
        if self.file_start is not None and (
            self.file_start < 0 or self.file_end is None or self.file_end <= self.file_start
        ):
            raise BundleValidationError("typed file range is invalid")
        if self.kind not in {
            "code", "data", "literal", "vector", "reserved", "mmio", "runtime", "unknown"
        }:
            raise BundleValidationError(f"unknown typed range kind {self.kind!r}")
        if self.mutability not in {"immutable", "mutable", "external", "unknown"}:
            raise BundleValidationError(f"unknown range mutability {self.mutability!r}")
        if self.alignment <= 0 or self.alignment & (self.alignment - 1):
            raise BundleValidationError("range alignment must be a positive power of two")


@dataclass(frozen=True)
class ProtectedEntry:
    """Instruction/entry constraint that cannot be treated as padding."""

    constraint_id: str
    instruction_identity: str
    address: int
    kind: str
    required_features: tuple[str, ...] = ()
    runtime_enforcement: str = "unknown"

    def __post_init__(self) -> None:
        if not self.constraint_id or not self.instruction_identity or self.address < 0:
            raise BundleValidationError("protected entry constraint is invalid")
        if self.runtime_enforcement not in {"enabled", "disabled", "unknown"}:
            raise BundleValidationError("runtime_enforcement must be enabled, disabled, or unknown")
        if not _tuple_is_sorted_unique(self.required_features):
            raise BundleValidationError("required_features must be sorted and unique")


@dataclass(frozen=True)
class ReferenceEdge:
    """Semantic code/data/runtime reference, not just an encoded displacement."""

    reference_id: str
    source_identity: str
    source_offset: int
    target_kind: str
    target_identity: str | None
    target_address: int | None
    addend: int
    encoding: str
    range_min: int | None
    range_max: int | None
    binding: str
    mutability: str
    permissions: str
    status: str
    invalidated_by: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.reference_id or not self.source_identity or self.source_offset < 0:
            raise BundleValidationError("reference edge identity/location is invalid")
        if self.target_kind not in {"code", "data", "runtime", "unknown"}:
            raise BundleValidationError("reference target kind is invalid")
        if self.mutability not in {"immutable", "mutable", "external", "unknown"}:
            raise BundleValidationError("reference mutability is invalid")
        if self.status not in {"resolved", "unsupported", "unknown"}:
            raise BundleValidationError("reference status is invalid")
        if self.status == "resolved" and (
            self.target_identity is None or self.target_address is None
        ):
            raise BundleValidationError("resolved references require target identity and address")
        if self.range_min is not None and self.range_max is not None:
            if self.range_min > self.range_max:
                raise BundleValidationError("reference encoding range is invalid")
        if not _tuple_is_sorted_unique(self.invalidated_by):
            raise BundleValidationError("reference invalidations must be sorted and unique")


@dataclass(frozen=True)
class MemoryEffect:
    """One required or possible machine-memory observation."""

    effect_id: str
    instruction_identity: str
    operation: str
    space: str
    address_expression: str
    width: int
    permissions: str
    alias_class: str
    escape: str
    observable: bool
    order: int
    possible_faults: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.operation not in {"read", "write", "read_write", "unknown"}:
            raise BundleValidationError("memory effect operation is invalid")
        if self.space not in {"stack", "ram", "rom", "mmio", "runtime", "unknown"}:
            raise BundleValidationError("memory effect space is invalid")
        if self.width <= 0 or self.order < 0:
            raise BundleValidationError("memory effect width/order is invalid")
        if self.escape not in {"none", "possible", "yes", "unknown"}:
            raise BundleValidationError("memory escape fact is invalid")
        if not _tuple_is_sorted_unique(self.possible_faults):
            raise BundleValidationError("memory faults must be sorted and unique")


@dataclass(frozen=True)
class EffectSummary:
    """Conservative effects for a region and its initially empty patch unit."""

    register_reads: tuple[str, ...] = ()
    register_writes: tuple[str, ...] = ()
    flag_reads: tuple[str, ...] = ()
    flag_writes: tuple[str, ...] = ()
    memory: tuple[MemoryEffect, ...] = ()
    calls: tuple[str, ...] = ()
    observer_boundaries: tuple[str, ...] = ()
    unknown_effects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "register_reads", "register_writes", "flag_reads", "flag_writes",
            "calls", "observer_boundaries", "unknown_effects",
        ):
            if not _tuple_is_sorted_unique(getattr(self, name)):
                raise BundleValidationError(f"{name} must be sorted and unique")
        orders = [effect.order for effect in self.memory]
        if len(set(orders)) != len(orders):
            raise BundleValidationError("memory effect order values must be unique")

    def semantic_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PatchAnchorContract:
    """Initial identity field for later patch-aware transformations."""

    anchor_id: str
    original_identity: str
    instruction_identity: str
    trigger_phase: str
    action: Mapping[str, Any]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    multiplicity: str = "each_execution"


@dataclass(frozen=True)
class PatchContract:
    """Versioned patch unit; phase one uses an explicit empty instance."""

    schema_version: int = 1
    contract_id: str = "empty-patch-contract-v1"
    anchors: tuple[PatchAnchorContract, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def contract_hash(self) -> str:
        return hash_json(self.to_dict())


@dataclass(frozen=True)
class OriginStateMapping:
    """Original identity assigned to an emitted potentially faulting instruction."""

    emitted_identity: str
    original_identity: str
    fault_kinds: tuple[str, ...]
    state_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.emitted_identity or not self.original_identity:
            raise BundleValidationError("origin mapping identities must not be empty")
        if not _tuple_is_sorted_unique(self.fault_kinds):
            raise BundleValidationError("fault kinds must be sorted and unique")
        if not _tuple_is_sorted_unique(self.state_fields):
            raise BundleValidationError("state fields must be sorted and unique")


@dataclass(frozen=True)
class FirmwareUnitView:
    """Immutable input presented to every preflight pass."""

    schema_version: int
    unit_id: str
    name: str
    input_image_hash: str
    region_bytes_hash: str
    target: TargetDescription
    address_start: int
    address_end: int
    file_start: int
    file_end: int
    instructions: tuple[InstructionIdentity, ...]
    entries: tuple[int, ...]
    exits: tuple[str, ...]
    typed_ranges: tuple[TypedRange, ...]
    protected_entries: tuple[ProtectedEntry, ...]
    references: tuple[ReferenceEdge, ...]
    effects: EffectSummary
    origins: tuple[OriginStateMapping, ...]
    patch_contract: PatchContract = field(default_factory=PatchContract)
    alternate_entries_complete: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise BundleValidationError("unsupported FirmwareUnitView schema")
        require_hex_digest("input_image_hash", self.input_image_hash)
        require_hex_digest("region_bytes_hash", self.region_bytes_hash)
        if not self.unit_id or not self.name:
            raise BundleValidationError("firmware unit identity/name must not be empty")
        if self.address_start < 0 or self.address_end <= self.address_start:
            raise BundleValidationError("firmware unit address bounds are invalid")
        if self.file_start < 0 or self.file_end <= self.file_start:
            raise BundleValidationError("firmware unit file bounds are invalid")
        identities = {item.identity for item in self.instructions}
        if len(identities) != len(self.instructions):
            raise BundleValidationError("instruction identities must be unique")
        if tuple(sorted(self.instructions, key=lambda item: item.address)) != self.instructions:
            raise BundleValidationError("instructions must be sorted by address")
        previous_end = self.address_start
        for item in self.instructions:
            if item.address < previous_end or item.address + item.size > self.address_end:
                raise BundleValidationError("instruction is outside or overlaps region bounds")
            previous_end = item.address + item.size
        starts = {item.address for item in self.instructions}
        if any(entry not in starts for entry in self.entries):
            raise BundleValidationError("each entry must resolve to an exact instruction")
        if any(item.instruction_identity not in identities for item in self.protected_entries):
            raise BundleValidationError("protected entry does not resolve to an instruction")
        if any(item.source_identity not in identities for item in self.references):
            raise BundleValidationError("reference source does not resolve to an instruction")
        if any(item.instruction_identity not in identities for item in self.effects.memory):
            raise BundleValidationError("memory effect does not resolve to an instruction")
        if any(item.instruction_identity not in identities for item in self.patch_contract.anchors):
            raise BundleValidationError("patch anchor does not resolve to an instruction")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def contract_hash(self) -> str:
        return hash_json(self.to_dict())

    def instruction(self, identity: str) -> InstructionIdentity:
        for item in self.instructions:
            if item.identity == identity:
                return item
        raise KeyError(identity)
