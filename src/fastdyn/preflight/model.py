"""Patch-independent data types and hashing for firmware preflight artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class BundleValidationError(ValueError):
    """A firmware artifact is malformed or mismatches its declared content."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the stable UTF-8 JSON representation used for artifact hashes."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def hash_json(value: Any) -> str:
    """Return a lowercase SHA-256 digest for a JSON-compatible value."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def hash_file(path: Path) -> str:
    """Return a lowercase SHA-256 digest for the exact bytes at ``path``."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_hex_digest(name: str, value: str) -> None:
    """Reject values that are not canonical lowercase SHA-256 digests."""

    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise BundleValidationError(f"{name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class TargetDescription:
    """Exact ISA configuration to which a firmware artifact is bound."""

    isa: str
    mode: str
    endianness: str
    cpu: str
    features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all((self.isa, self.mode, self.endianness, self.cpu)):
            raise BundleValidationError("target fields must not be empty")
        if self.endianness not in {"little", "big"}:
            raise BundleValidationError("target endianness must be 'little' or 'big'")
        if tuple(sorted(set(self.features))) != self.features:
            raise BundleValidationError("target features must be sorted and unique")

    def to_dict(self) -> dict[str, Any]:
        """Return the schema representation of this target."""

        value = asdict(self)
        value["features"] = list(self.features)
        return value


@dataclass(frozen=True)
class MemoryRange:
    """One declared firmware-memory range, with an exclusive end address."""

    name: str
    start: int
    end: int
    permissions: str
    kind: str

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise BundleValidationError(f"invalid memory range {self.name!r}")
        if not self.permissions or any(char not in "rwx-" for char in self.permissions):
            raise BundleValidationError(f"invalid permissions for memory range {self.name!r}")

    def to_dict(self) -> dict[str, Any]:
        """Return the schema representation of this range."""

        return asdict(self)


@dataclass(frozen=True)
class OriginRange:
    """An exact original-to-emitted provenance range using half-open addresses."""

    original_start: int
    original_end: int
    emitted_start: int
    emitted_end: int
    mapping: str = "identity"

    def __post_init__(self) -> None:
        original_size = self.original_end - self.original_start
        emitted_size = self.emitted_end - self.emitted_start
        if self.original_start < 0 or original_size <= 0 or emitted_size <= 0:
            raise BundleValidationError("origin ranges must be non-empty and non-negative")
        if self.mapping == "identity" and (
            original_size != emitted_size or self.original_start != self.emitted_start
        ):
            raise BundleValidationError("identity origin ranges must preserve address and size")

    def to_dict(self) -> dict[str, Any]:
        """Return the schema representation of this range."""

        return asdict(self)


@dataclass(frozen=True)
class OriginInstruction:
    """Exact original identity for one emitted instruction after reordering."""

    emitted_address: int
    emitted_size: int
    original_address: int
    original_size: int
    original_identity: str
    mapping: str = "exact"

    def __post_init__(self) -> None:
        if (
            self.emitted_address < 0
            or self.original_address < 0
            or self.emitted_size <= 0
            or self.original_size <= 0
            or not self.original_identity
        ):
            raise BundleValidationError("instruction origin is empty or invalid")
        if self.mapping not in {"exact", "synthesized_edge"}:
            raise BundleValidationError("instruction origin mapping kind is invalid")

    def to_dict(self) -> dict[str, Any]:
        """Return the stable mapping for this emitted instruction."""

        return asdict(self)
