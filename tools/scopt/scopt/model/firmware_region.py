"""Typed plans for call-site-specific firmware executable regions.

These objects describe guest firmware calls and private copies of captured
translations.  They are intentionally separate from :class:`InlineCall`,
which is the native QEMU-helper splice mechanism.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..canon import canonical_json, sha256_id
from ..ids import TBKey

__all__ = [
    "FirmwareCallSite", "FirmwareRegionMember", "FirmwareRegionEdge",
    "FirmwareOrigin", "FirmwareRegionPlan", "FirmwareInlineReceipt",
]

EdgeKind = Literal["branch", "call", "return", "side_exit"]


def _key(key: TBKey) -> dict[str, int | None]:
    return {"page_addr0": key.page_addr0, "page_addr1": key.page_addr1,
            "flags": key.flags, "cs_base": key.cs_base, "cflags": key.cflags}


@dataclass(frozen=True, slots=True)
class FirmwareCallSite:
    source_uid: str
    source_hash: str
    source_key: TBKey
    exit_id: str
    insn_index: int
    insn_vaddr: int
    insn_bytes: bytes
    target_vaddr: int
    continuation_vaddr: int
    image_sha256: str
    callee_candidates: tuple[str, ...]
    continuation_candidates: tuple[str, ...]
    refusal_reasons: tuple[str, ...] = ()

    @property
    def callsite_id(self) -> str:
        return sha256_id(canonical_json(self.to_json(include_id=False)))

    @property
    def eligible(self) -> bool:
        return not self.refusal_reasons

    def to_json(self, *, include_id: bool = True) -> dict[str, Any]:
        row = {"source_uid": self.source_uid, "source_hash": self.source_hash,
               "source_key": _key(self.source_key), "exit_id": self.exit_id,
               "insn_index": self.insn_index, "insn_vaddr": hex(self.insn_vaddr),
               "insn_bytes": self.insn_bytes.hex(), "target_vaddr": hex(self.target_vaddr),
               "continuation_vaddr": hex(self.continuation_vaddr),
               "image_sha256": self.image_sha256,
               "callee_candidates": list(self.callee_candidates),
               "continuation_candidates": list(self.continuation_candidates),
               "refusal_reasons": list(self.refusal_reasons)}
        if include_id:
            row["callsite_id"] = self.callsite_id
        return row


@dataclass(frozen=True, slots=True)
class FirmwareRegionMember:
    source_uid: str
    source_hash: str
    source_key: TBKey
    inline_context: tuple[str, ...]
    role: Literal["caller", "callee", "continuation"]

    @property
    def identity(self) -> tuple[str, tuple[str, ...]]:
        return self.source_uid, self.inline_context

    def to_json(self) -> dict[str, Any]:
        return {"source_uid": self.source_uid, "source_hash": self.source_hash,
                "source_key": _key(self.source_key),
                "inline_context": list(self.inline_context), "role": self.role}


@dataclass(frozen=True, slots=True)
class FirmwareRegionEdge:
    source_member: int
    exit_id: str
    kind: EdgeKind
    target_member: int | None
    guest_target: int | None
    guarded: bool = False
    refusal_reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"source_member": self.source_member, "exit_id": self.exit_id,
                "kind": self.kind, "target_member": self.target_member,
                "guest_target": None if self.guest_target is None else hex(self.guest_target),
                "guarded": self.guarded, "refusal_reason": self.refusal_reason}


@dataclass(frozen=True, slots=True)
class FirmwareOrigin:
    source_uid: str
    inline_context: tuple[str, ...]
    source_offset: int
    private_offset: int

    def to_json(self) -> dict[str, Any]:
        return {"source_uid": self.source_uid, "inline_context": list(self.inline_context),
                "source_offset": self.source_offset, "private_offset": self.private_offset}


@dataclass(frozen=True, slots=True)
class FirmwareRegionPlan:
    callsite: FirmwareCallSite
    members: tuple[FirmwareRegionMember, ...]
    edges: tuple[FirmwareRegionEdge, ...]
    entry_member: int
    pending: tuple[Mapping[str, Any], ...] = ()
    complete: bool = True
    trusted: bool = False
    trusted_entry_offset: int = 0

    def __post_init__(self) -> None:
        if not self.members or not 0 <= self.entry_member < len(self.members):
            raise ValueError("firmware region needs a valid entry member")
        identities = [m.identity for m in self.members]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate firmware region member identity")
        for edge in self.edges:
            if not 0 <= edge.source_member < len(self.members):
                raise ValueError("firmware region edge has an invalid source")
            if edge.target_member is not None and not 0 <= edge.target_member < len(self.members):
                raise ValueError("firmware region edge has an invalid target")
        if self.trusted_entry_offset < 0 or (not self.trusted and self.trusted_entry_offset):
            raise ValueError("invalid trusted firmware-region entry offset")
        # ``complete`` means the emitted subgraph is safe to materialize.  It
        # may still contain an explicit ordinary-lookup side exit for pending
        # nested work (for example, a call whose callee was not captured).

    @property
    def region_id(self) -> str:
        return sha256_id(canonical_json(self.to_json(include_id=False)))

    @property
    def source_uids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(m.source_uid for m in self.members))

    def to_json(self, *, include_id: bool = True) -> dict[str, Any]:
        row = {"schema": "fastdyn.firmware_region_plan", "version": 1,
               "callsite": self.callsite.to_json(),
               "members": [m.to_json() for m in self.members],
               "edges": [e.to_json() for e in self.edges],
               "entry_member": self.entry_member,
               "pending": [dict(x) for x in self.pending], "complete": self.complete,
               "trusted": self.trusted,
               "trusted_entry_offset": self.trusted_entry_offset}
        if include_id:
            row["region_id"] = self.region_id
        return row


@dataclass(frozen=True, slots=True)
class FirmwareInlineReceipt:
    discovered: tuple[FirmwareCallSite, ...]
    embedded: tuple[str, ...]
    residual: tuple[Mapping[str, Any], ...]
    pending: tuple[Mapping[str, Any], ...]
    actual_expansion_depth: int
    emitted_size: int = 0
    loaded_artifact_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"schema": "fastdyn.firmware_inline_receipt", "version": 1,
                "discovered": [x.to_json() for x in self.discovered],
                "embedded": list(self.embedded),
                "residual": [dict(x) for x in self.residual],
                "pending": [dict(x) for x in self.pending],
                "actual_expansion_depth": self.actual_expansion_depth,
                "emitted_size": self.emitted_size,
                "loaded_artifact_id": self.loaded_artifact_id}
